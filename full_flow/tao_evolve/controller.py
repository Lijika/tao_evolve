"""Deterministic two-task evolution using only the public competition API."""
from dataclasses import asdict
from decimal import Decimal
import json
import os
from pathlib import Path
import time
import uuid

from .adapter import (PublicAdapter, TERMINAL, LocalGuardViolation, GuardStatusUnavailable,
                      bind_task, digest, parse_matrix)
from .budget_v11 import plan_runway
from .guard_runtime import snapshot_tree, check_tree, freeze_paths, verify_frozen
from .journal import IntentJournal
from .portfolio_v11 import champions
from .probes import run_probes, _cache_binding as probe_execution_binding
from .research import EvidenceCompiler, build_prompt, validate_model_contract
from .research_record import REVIEW_VERSION, assess_research, reflection_memory, record_research
from .strategy_v11 import SearchState, choose_research_mode
from .submission import SubmissionDriver


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".pending")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(str(temporary), str(path))


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class Controller:
    def __init__(self, context, agent_dir, *, workspaces_root=None, scratch_root=None):
        self.context = context
        self.agent_dir = Path(agent_dir).resolve()
        self.api = PublicAdapter(context)
        self.binding = bind_task(context)
        self.task = context.task
        training = [row for row in self.binding["manifest"]["cases"] if row["split"] == "train"]
        self.portability_context = {
            "identity_literals": sorted({row[key] for row in training for key in ("id", "design")
                                         if isinstance(row.get(key), str)}),
            "observed_processes": sorted({row["pdk"] for row in training if isinstance(row.get("pdk"), str)})}
        self.config = read_json(self.agent_dir / "runtime.json")
        self.limit = self.config["max_evaluations"]
        if type(self.limit) is not int or not 1 <= self.limit <= 10:
            raise ValueError("max_evaluations must be an integer from 1 to 10")
        self.review_after = self.config.get("review_after_evaluations")
        if self.review_after is not None and (
                type(self.review_after) is not int or not 1 <= self.review_after < self.limit):
            raise ValueError("review_after_evaluations must be null or an integer below max_evaluations")
        self.pending_usage_policy = self.config.get("pending_model_usage_policy", "stop")
        if self.pending_usage_policy not in ("stop", "continue_after_successful_turn"):
            raise ValueError("Unknown pending_model_usage_policy")
        workspaces = Path(workspaces_root or "/workspace/workspaces")
        self.control = workspaces / (".tao-evolve-" + self.task)
        self.scratch = Path(scratch_root or "/workspace/scratch/tao-evolve")
        self.control.mkdir(parents=True, exist_ok=True)
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.state_file = self.control / "state.json"
        if self.state_file.exists():
            self.state = read_json(self.state_file)
            for key, expected in (("task", self.task), ("task_digest", self.binding["task_digest"]),
                                  ("policy_digest", self.binding["policy_digest"]),
                                  ("baseline", context.baseline_candidate)):
                if self.state.get(key) != expected:
                    raise ValueError("Persistent participant state differs: " + key)
        else:
            self.state = {"version": 1, "campaign_id": uuid.uuid4().hex, "task": self.task,
                          "task_digest": self.binding["task_digest"],
                          "policy_digest": self.binding["policy_digest"],
                          "baseline": context.baseline_candidate, "sequence": 0,
                          "records": [], "active": None, "stagnation": 0,
                          "draft_failures": 0, "budget_observations": []}
            self.save()
        self.campaign = self.state["campaign_id"]
        immutable = self.control / "immutable.json"
        if immutable.exists():
            self.immutable = read_json(immutable)
            self.require_integrity()
        else:
            paths = [self.agent_dir, self.binding["manifest_path"]]
            if Path("/inputs").is_dir():
                paths.append(Path("/inputs"))
            if context.files.get("guide") and Path(context.files["guide"]).is_file():
                paths.append(context.files["guide"])
            self.immutable = freeze_paths(paths)
            write_json(immutable, self.immutable)
        self.journal = IntentJournal(self.control / "intents.sqlite", self.campaign)
        self.driver = SubmissionDriver(self.journal, self.api)
        original = self.control / "original-paths.json"
        if not original.exists():
            baseline = snapshot_tree(context.files["source"], retain_python=False)
            if baseline["errors"]:
                raise ValueError("Baseline source inventory is incomplete")
            write_json(original, sorted(baseline["entries"]))
        self.original_paths = read_json(original)
        self.last_progress = 0.0

    def save(self):
        write_json(self.state_file, self.state)

    def event(self, kind, **fields):
        row = {"time": time.time(), "event": kind, "task": self.task, **fields}
        with (self.control / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, sort_keys=True, ensure_ascii=True) + "\n")
            stream.flush()
        print(json.dumps(row, ensure_ascii=True), flush=True)

    def require_integrity(self):
        result = verify_frozen(self.immutable)
        if not result["ok"]:
            error = "Frozen Agent/input integrity failed: " + "; ".join(result["errors"])
            if any(item.startswith("immutable_changed:") for item in result["errors"]):
                raise LocalGuardViolation(error)
            raise RuntimeError(error)
        if digest(self.context.source_policy) != self.binding["policy_digest"]:
            raise LocalGuardViolation("Source policy changed during the run")

    def _guard_control_entries(self):
        snapshot = snapshot_tree(self.control, retain_python=False)
        if snapshot["errors"]:
            raise RuntimeError("Participant control inventory could not be verified")
        # Journal file bytes change under SQLite itself; its logical data is checked.
        for name in ("intents.sqlite", "intents.sqlite-wal", "intents.sqlite-shm"):
            snapshot["entries"].pop(name, None)
        return snapshot["entries"]

    def _guard_before_model(self, allow_contract=True):
        self.require_integrity()
        contract = str(Path(self.state["active"]["contract_path"]).relative_to(self.control))
        return {"control": self._guard_control_entries(), "journal": self.journal.guard_snapshot(),
                "writable_contract": contract if allow_contract else None,
                "history": sorted(r["request_id"] for r in self.api.status()["history"])}

    def _guard_after_model(self, before, *, pending_request_id=None):
        self.require_integrity()
        if "journal" not in before:
            raise RuntimeError("Historical guard snapshot lacks durable journal binding")
        old = dict(before["control"])
        entries = self._guard_control_entries()
        # The proposal is the sole model-writable file within participant control data.
        contract = before.get("writable_contract")
        if contract is not None:
            old.pop(contract, None)
            entries.pop(contract, None)
        if old != entries or not self.journal.guard_matches(before["journal"], pending_request_id):
            raise LocalGuardViolation("Model changed participant control data outside its contract")
        try:
            status = self.api.status()
        except (RuntimeError, OSError) as error:
            raise GuardStatusUnavailable("Public status unavailable during local guard verification") from error
        history = sorted(r["request_id"] for r in status["history"])
        if before["history"] != history:
            raise LocalGuardViolation("Model dispatched an unregistered official evaluation")

    def _progress(self, receipt):
        now = time.monotonic()
        if now - self.last_progress >= 60 or receipt["status"] in TERMINAL:
            self.event("official_evaluation", request_id=receipt["request_id"],
                       status=receipt["status"])
            self.last_progress = now

    def _record(self, receipt, active=None):
        rid = receipt["request_id"]
        if any(r["receipt"]["request_id"] == rid for r in self.state["records"]):
            return
        row = parse_matrix(receipt, self.campaign, self.binding)
        previous_best = self._champions()[0]
        record = {"receipt": receipt, "parent_id": (active or {}).get("parent_id"),
                  "tree_digest": (active or {}).get("tree_digest"),
                  "contract": (active or {}).get("contract"),
                  "research": record_research(active or {}, receipt, self.task, self.campaign)}
        self.state["records"].append(record)
        if row and (previous_best is None or row.score > previous_best.score):
            self.state["stagnation"] = 0
        else:
            self.state["stagnation"] += 1
        self.save()

    def _rows(self):
        return [row for row in
                (parse_matrix(r["receipt"], self.campaign, self.binding) for r in self.state["records"])
                if row is not None]

    def _champions(self):
        return champions(self._rows(), self.campaign, self.task,
                         self.binding["expected_keys"], self.binding["task_digest"])

    def _record_for(self, candidate):
        return next((r for r in reversed(self.state["records"])
                     if r["receipt"].get("candidate_id") == candidate), None)

    def _recover_history(self):
        official = self.api.status()["history"]
        ids = {r["request_id"] for r in official}
        if any(r["receipt"]["request_id"] not in ids for r in self.state["records"]):
            raise RuntimeError("Participant state belongs to a different official campaign")
        active = self.state["active"]
        for row in official:
            if any(r["receipt"]["request_id"] == row["request_id"] for r in self.state["records"]):
                continue
            receipt = self.api.wait(row["request_id"], self._progress)
            owns = active and active.get("evaluation_request_id") == row["request_id"]
            if owns:
                self._finish_evaluation(active, receipt)
            else:
                self._record(receipt)

    def _evidence(self, parent, source_root):
        record = self._record_for(parent)
        compiler = EvidenceCompiler(self.task, self.campaign, source_root=source_root,
                                    expected_keys=self.binding["expected_keys"],
                                    task_digest=self.binding["task_digest"])
        if record:
            parent_record = self._record_for(record.get("parent_id"))
            return compiler.compile(record["receipt"],
                                    parent_feedback=parent_record["receipt"] if parent_record else None,
                                    contract=record.get("contract"))
        return compiler.compile({})

    def _select_parent(self, mode):
        score, delivery = self._champions()
        # At most three roles; the official archive itself remains complete.
        rows = self._rows()
        alternatives = [r for r in rows if delivery and r.candidate_id != delivery.candidate_id
                        and all(c.physical_valid is True for c in r.cells)]
        def worst_case(row):
            return min(sum(c.score for c in row.cells if c.case == case) / 3
                       for case in {c.case for c in row.cells})
        alternative = max(alternatives, key=worst_case) if alternatives else None
        roles = {"delivery": delivery.candidate_id if delivery else None,
                 "score": score.candidate_id if score else None,
                 "exploration": alternative.candidate_id if alternative else None}
        if mode == "BRANCH_WITH_EVIDENCE" and alternative:
            parent = alternative.candidate_id
        else:
            parent = delivery.candidate_id if delivery else self.context.baseline_candidate
        self.state["active_parent_roles"] = roles
        return parent

    def _new_draft(self, status, runway):
        _, delivery = self._champions()
        remaining = min(status["evaluations_remaining"], self.limit - len(status["history"]))
        latest = self.state["records"][-1]["receipt"] if self.state["records"] else None
        latest_record = self.state["records"][-1] if self.state["records"] else None
        latest_evidence = (self._evidence(latest.get("candidate_id"), self.context.files["source"])
                           if latest and latest.get("candidate_id") else {})
        previous_contract = (latest_record or {}).get("contract") or {}
        # Official checkout accepts only succeeded historical candidates. A failed
        # transport/worker receipt cannot establish a same-mechanism repair parent.
        confirmed_error = bool(latest and latest["status"] == "succeeded"
                               and previous_contract.get("main_mechanism")
                               and previous_contract.get("edit_stages")
                               and any(c.get("valid") is False for c in latest.get("feedback", {}).get("cells", [])))
        mode = choose_research_mode(SearchState(
            self.task, remaining, delivery is not None, confirmed_error=confirmed_error,
            stagnation=self.state["stagnation"], complete_training_feedback=bool(self._rows()),
            stage_conflict_supported=bool(latest_evidence.get("stage_conflict_supported")),
            coupled_contract_supported=bool(len(previous_contract.get("edit_stages", [])) > 1
                                            and previous_contract.get("verified_impact_refs"))))
        parent = self._select_parent(mode)
        if mode == "REPAIR_SAME_MECHANISM":
            parent = latest["candidate_id"]
            self.state["active_parent_roles"]["repair"] = parent
        self.state["sequence"] += 1
        key = "draft-" + str(self.state["sequence"])
        folder = self.control / "drafts" / key
        folder.mkdir(parents=True, exist_ok=True)
        rid = self.journal.prepare("checkout", key, {"parent": parent})
        self.state["active"] = {
            "key": key, "phase": "checkout", "parent_id": parent, "checkout_request_id": rid,
            "folder": str(folder), "mode": mode, "repair_count": 0,
            "contract_path": str(folder / "contract.json"),
            # Public balance pressure is a scheduling signal, never a settled cost.
            "compact": (runway.action == "managed_codex_budget_risk"
                        or runway.per_remaining_round < Decimal("1.00")),
            "budget_envelope_rmb": str(runway.per_remaining_round)}
        if mode == "REPAIR_SAME_MECHANISM":
            self.state["active"]["repair_anchor"] = {
                key: previous_contract[key] for key in ("contract_id", "main_mechanism", "edit_stages")
                if key in previous_contract}
            self.state["active"]["repair_errors"] = [
                "official_invalid_cell:%s:%s:%s" % (cell.get("case"), cell.get("seed"),
                                                   cell.get("reason", "physical_valid=false"))
                for cell in latest["feedback"]["cells"] if cell.get("valid") is False]
        self.save()

    def _checkout(self, active):
        rid = active["checkout_request_id"]
        checkout = self.context.checkout(parent=active["parent_id"], request_id=rid)
        if checkout["checkout_id"] != rid or checkout["parent"] != active["parent_id"]:
            raise ValueError("Checkout receipt differs from prepared intent")
        before = snapshot_tree(checkout["path"])
        if before["errors"]:
            raise ValueError("Checkout has an invalid source inventory")
        write_json(Path(active["folder"]) / "before.json", before)
        self.journal.record(rid, "succeeded", checkout)
        active.update(checkout=checkout, phase="prepare_model")
        self.save()

    def _prepare_model(self, active):
        evidence = self._evidence(active["parent_id"], active["checkout"]["path"])
        latest = self.state["records"][-1] if self.state["records"] else None
        if latest and latest["receipt"]["request_id"] != evidence.get("request_id"):
            selected_parent = evidence
            compiler = EvidenceCompiler(self.task, self.campaign,
                                        source_root=active["checkout"]["path"],
                                        expected_keys=self.binding["expected_keys"],
                                        task_digest=self.binding["task_digest"])
            previous = self._record_for(latest.get("parent_id"))
            evidence = compiler.compile(latest["receipt"],
                                        parent_feedback=previous["receipt"] if previous else None,
                                        contract=latest.get("contract"))
            evidence["selected_parent_evidence"] = selected_parent
        if latest:
            evidence["latest_contract"] = latest.get("contract")
        evidence["evidence_role"] = "latest_experiment_for_reflection"
        evidence["reflection_memory"] = reflection_memory(
            self.state["records"], self.task, self.campaign)
        before_path = Path(active["folder"]) / "before.json"
        before = read_json(before_path)
        check = check_tree(before, active["checkout"]["path"],
                           self.context.source_policy, original_paths=self.original_paths,
                           portability_context=self.portability_context)
        evidence["review_context"] = {
            "before_snapshot": str(before_path), "before_digest": before["digest"],
            "changed_paths": check["changed_paths"], "errors": check["errors"],
            "warnings": check.get("warnings", []),
            "portability_card": check.get("portability_card"),
            "previous_preflight": active.get("preflight"),
            "previous_repair_errors": active.get("repair_errors", []),
            "repair_anchor": active.get("repair_anchor")}
        active["evidence_summary"] = {
            "risk": evidence.get("risk"), "unknowns": evidence.get("unknowns", []),
            "source_dependencies": evidence.get("source_dependencies", {})}
        status = self.api.status()
        prompt_state = {**status, "campaign_id": self.campaign, "parent_candidate_id": active["parent_id"],
                        "budget_envelope_rmb": active["budget_envelope_rmb"],
                        "budget_mode": "managed_codex_advisory",
                        "remaining_agent_submissions": self.limit - len(status["history"])}
        prompt = build_prompt(self.task, active["checkout"], self.context.source_policy,
                              prompt_state, evidence, active["contract_path"], active["mode"],
                              repair_errors=active.get("repair_errors"), compact=active["compact"])
        prompt += ("\nUse one compact combined Plan/Edit/Reflect turn. Work only in the specified "
                   "checkout and write the requested contract. Keep the controller, its state, "
                   "the Agent, all inputs, the bridge, and other workspaces unchanged. "
                   "Do not invoke AgentContext/bridge operations. Read only current task/campaign "
                   "training feedback. Finish terminal sessions before returning. "
                   "Do not start a full physical flow or background process. "
                   "The local code budget envelope is advisory; finish promptly.\n")
        prompt_path = Path(active["folder"]) / ("prompt-%d.txt" % active["repair_count"])
        prompt_path.write_text(prompt, encoding="utf-8")
        active.update(prompt_path=str(prompt_path), phase="model")
        self.save()

    def _pending_usage_after_success(self, prepared_request_id=None):
        if self.pending_usage_policy != "continue_after_successful_turn":
            return False
        turns = [row for row in self.journal.entries("codex")
                 if not (row["request_id"] == prepared_request_id and row["state"] == "prepared")]
        return bool(turns and all(row["state"] in {"succeeded", "failed"} for row in turns)
                    and turns[-1]["state"] == "succeeded"
                    and (turns[-1]["response"] or {}).get("success") is True)

    def _model(self, active):
        status = self.api.status()
        key = active["key"] + "-edit-" + str(active["repair_count"])
        request_id = self.journal.request_id("codex", key)
        existing = next((row for row in self.journal.entries("codex")
                         if row["request_id"] == request_id), None)
        recovering = existing is not None and existing["state"] != "prepared"
        if status["unresolved_model_calls"] and (
                self.pending_usage_policy == "stop"
                or (not recovering and not self._pending_usage_after_success(request_id))):
            raise RuntimeError("Model usage remains unresolved; no new model turn is allowed")
        # Recovery reuses the original prompt/ID; the official adapter owns execution recovery.
        if not recovering and not (status["model_dispatch_allowed"] and status["codex_available"]):
            raise RuntimeError("Official model permission or managed Codex availability is closed")
        if recovering and existing["state"] not in {"succeeded", "failed"} and not status["codex_available"]:
            raise RuntimeError("Managed Codex is unavailable for the original turn recovery")
        if not recovering:
            active["model_budget_before"] = {
                key: status[key] for key in ("model_cost_available_rmb", "unresolved_model_calls")}
            self.save()
        prompt = Path(active["prompt_path"]).read_text(encoding="utf-8")
        self.event("model_edit", draft=active["key"], repair=active["repair_count"],
                   mode=active["mode"], available_rmb=status["model_cost_available_rmb"],
                   unresolved_model_calls=status["unresolved_model_calls"],
                   pending_model_usage_policy=self.pending_usage_policy, recovering=recovering)
        turn = self.api.codex(self.journal, key,
                              prompt, self._guard_before_model, self._guard_after_model)
        after = self.api.status()
        self._observe_model(active, request_id, turn, after)
        active.update(turn=turn, phase="validate")
        self.save()
        self._check_pending_model_usage(active, after)

    def _observe_model(self, active, request_id, turn, after):
        if any(row.get("turn_request_id") == request_id for row in self.state["budget_observations"]):
            return
        before = active.get("model_budget_before", {})
        self.state["budget_observations"].append({
            "draft": active["key"], "before_available_rmb": before.get("model_cost_available_rmb"),
            "after_available_rmb": after["model_cost_available_rmb"],
            "before_unresolved_model_calls": before.get("unresolved_model_calls"),
            "unresolved_model_calls": after["unresolved_model_calls"],
            "turn_request_id": request_id, "turn_success": turn.get("success"),
            "pending_model_usage_policy": self.pending_usage_policy,
            "settled_cost_rmb": None, "semantics": "public_available_balance_not_charge"})

    def _check_pending_model_usage(self, active, status):
        # Official evaluation permission is independent of a retained model bill.
        # Only a confirmed completed edit may proceed without another model call.
        if (status["unresolved_model_calls"]
                and active.get("turn", {}).get("success") is not True):
            raise RuntimeError("Model usage remains unresolved and the editing turn is incomplete; draft preserved")

    def _require_evaluation_dispatch(self, active):
        status = self.api.status()
        self._check_pending_model_usage(active, status)
        if (not status["evaluation_dispatch_allowed"] or status["evaluations_remaining"] <= 0
                or len(status["history"]) >= self.limit):
            raise RuntimeError("Official evaluation permission or remaining submission allowance is closed")

    def _retry_or_discard(self, active, errors):
        self.event("local_check_failed", draft=active["key"], errors=errors)
        status = self.api.status()
        if status["unresolved_model_calls"] and not self._pending_usage_after_success():
            active.update(phase="validate", repair_errors=errors)
            self.save()
            raise RuntimeError("Local checks failed while model usage remains unresolved; draft preserved without repair")
        if (active["repair_count"] < 1 and status["model_dispatch_allowed"]
                and status["codex_available"] and status["evaluation_dispatch_allowed"]):
            contract = active.get("contract") or {}
            if not active.get("repair_anchor") and all(
                    contract.get(key) for key in ("main_mechanism", "edit_stages")):
                active["repair_anchor"] = {
                    key: contract[key] for key in ("contract_id", "main_mechanism", "edit_stages")
                    if key in contract}
            active.update(repair_count=1, mode="REPAIR_SAME_MECHANISM",
                          repair_errors=errors, phase="prepare_model")
        else:
            self.state["active"] = None
            self.state["draft_failures"] += 1
        self.save()

    def _run_probes(self, active, check):
        artifacts = self._probe_artifacts(active, check)
        binding, reason = probe_execution_binding(self.task, active["checkout"]["path"], artifacts or {})
        # A journal receipt only reuses a completed execution under the same
        # binding as the probe cache, including all published input descriptors.
        execution = ({"binding": binding, "artifacts": artifacts} if binding is not None else
                     {"unverified_binding": reason, "attempt": uuid.uuid4().hex})
        payload = {"workspace_digest": check["digest"], "changed_paths": check["changed_paths"],
                   "execution": execution}
        key = active["key"] + "-probes-" + str(active["repair_count"]) + "-" + digest(payload)[:16]
        rid = self.journal.prepare("probe", key, payload)
        active["probe_request_id"] = rid
        self.save()
        local = self.journal.get(rid)
        if local["state"] == "succeeded":
            return local["response"]
        if local["state"] in {"failed", "guard_rejected", "guard_pending"}:
            raise RuntimeError("Local probe requires verification: " + rid)
        self.journal.record(rid, "sending")
        frozen = self._guard_before_model(allow_contract=False)
        try:
            result = run_probes(
                self.task, active["checkout"]["path"], check["changed_paths"],
                self.scratch / active["key"] / ("probes-" + str(active["repair_count"])),
                artifacts=artifacts, cache_dir=self.scratch / self.campaign / "probe-cache")
        except Exception as error:
            result = {"probe_error": str(error)}
            self.api.verify_after(self.journal, rid, frozen, result, self._guard_after_model)
            self.journal.record(rid, "failed", result)
            raise
        self.api.verify_after(self.journal, rid, frozen, result, self._guard_after_model)
        self.journal.record(rid, "succeeded", result)
        return result

    def _validate(self, active):
        self.require_integrity()
        self._check_pending_model_usage(active, self.api.status())
        before = read_json(Path(active["folder"]) / "before.json")
        check = check_tree(before, active["checkout"]["path"], self.context.source_policy,
                           original_paths=self.original_paths, portability_context=self.portability_context)
        report = {k: v for k, v in check.items() if k != "snapshot"}
        errors = list(check["errors"])
        if active.get("turn", {}).get("success") is not True:
            errors.append("model_turn_failed:" + str(active["turn"].get("error", "incomplete edit")))
        contract = None
        if not errors:
            try:
                contract = validate_model_contract(active["contract_path"], self.task,
                                                   active["checkout"]["path"], check["changed_paths"])
                if contract["parent_candidate_id"] != active["parent_id"]:
                    raise ValueError("contract parent differs from the official checkout")
                active["contract"] = contract
            except (ValueError, OSError, KeyError, TypeError) as error:
                errors.append("contract_invalid:" + str(error))
        review = contract.get("research_review") if contract else None
        requested_stop = isinstance(review, dict) and review.get("decision") == "stop"
        if not errors and not check["changed_paths"] and not requested_stop:
            errors.append("no_source_change")
        if not errors and not requested_stop:
            probes = self._run_probes(active, check)
            report["probes"] = probes
            errors.extend(probes["hard_errors"])
            self.require_integrity()
            post = check_tree(before, active["checkout"]["path"], self.context.source_policy,
                              original_paths=self.original_paths, portability_context=self.portability_context)
            if post["digest"] != check["digest"]:
                errors.append("candidate_changed_during_local_probe")
        if (not errors and not requested_stop
                and check["digest"] in {r.get("tree_digest") for r in self.state["records"]}):
            errors.append("duplicate_already_submitted_source")
        assessment = None
        if not errors:
            evidence = read_json(Path(active["contract_path"]).with_name(
                Path(active["contract_path"]).stem + "-evidence.json"))
            assessment = assess_research(contract, evidence, active["checkout"]["path"],
                                         repair_anchor=None if requested_stop else active.get("repair_anchor"),
                                         diagnostics=report)
            active["research_assessment"] = assessment
            report["research_assessment"] = assessment
            errors.extend(assessment["errors"])
        report["errors"] = errors
        write_json(Path(active["folder"]) / ("preflight-%d.json" % active["repair_count"]), report)
        active["preflight"] = report
        if errors:
            return self._retry_or_discard(active, errors)
        if assessment["decision"] == "stop":
            active.update(phase="research_stopped", research_review_version=REVIEW_VERSION)
            self.save()
            return
        if assessment["decision"] != "evaluate":
            return self._retry_or_discard(active, ["research_decision_requires_revision"])
        active.update(tree_digest=check["digest"], changed_paths=check["changed_paths"],
                      contract=contract, phase="submit_ready", research_review_version=REVIEW_VERSION)
        self.save()
        self.event("ready_for_official", draft=active["key"], changed_paths=check["changed_paths"],
                   assurance="READY_FOR_OFFICIAL_UNPROVEN_PHYSICAL")

    def _probe_artifacts(self, active, check):
        from .public_artifacts import hbt_artifacts
        parent = self._record_for(active["parent_id"])
        return hbt_artifacts(self.campaign, self.binding, parent, check["digest"],
                             read_json(Path(active["folder"]) / "before.json"),
                             self.scratch / active["key"] / "public-inputs")

    def _check_prepared_candidate(self, active):
        current = snapshot_tree(active["checkout"]["path"], retain_python=False)
        if current["errors"] or current["digest"] != active["tree_digest"]:
            raise RuntimeError("Prepared candidate changed before submission")

    def _review_checkpoint(self, active):
        self.require_integrity()
        self._check_prepared_candidate(active)
        status = self.api.status()
        self._check_pending_model_usage(active, status)
        _, delivery = self._champions()
        report = {
            "task": self.task, "campaign_id": self.campaign,
            "review_after_evaluations": self.review_after,
            "completed_evaluations": len(self.state["records"]),
            "draft_key": active["key"], "phase": active["phase"],
            "checkout_id": active["checkout"]["checkout_id"], "parent_id": active["parent_id"],
            "tree_digest": active["tree_digest"], "contract_path": active["contract_path"],
            "preflight_path": str(Path(active["folder"]) / ("preflight-%d.json" % active["repair_count"])),
            "officially_submitted": False, "physical_validity": None, "official_score": None,
            "delivery_champion_candidate_id": delivery.candidate_id if delivery else None,
            "unresolved_model_calls": status["unresolved_model_calls"],
            "actual_model_charge_rmb": None}
        write_json(Path(active["folder"]) / "review.json", report)
        return self._finalize("design_review_checkpoint")

    def _submit(self, active):
        self.require_integrity()
        self._require_evaluation_dispatch(active)
        self._check_prepared_candidate(active)
        rid = self.journal.prepare("submit", active["key"],
                                  {"checkout_id": active["checkout"]["checkout_id"],
                                   "workspace_digest": active["tree_digest"]})
        active.update(evaluation_request_id=rid, phase="evaluation")
        self.save()
        self.event("submit", request_id=rid, draft=active["key"])
        # Journal reconciliation precedes dispatch, even after an interrupted process.
        receipt = self.driver.step(active["checkout"]["checkout_id"], active["key"], active["tree_digest"])
        if receipt["status"] in TERMINAL:
            self._finish_evaluation(active, receipt)

    def _finish_evaluation(self, active, receipt):
        self.journal.record(active["evaluation_request_id"], receipt["status"], receipt)
        self._record(receipt, active)
        compiler = EvidenceCompiler(self.task, self.campaign,
                                    source_root=active["checkout"]["path"],
                                    expected_keys=self.binding["expected_keys"],
                                    task_digest=self.binding["task_digest"])
        parent = self._record_for(active["parent_id"])
        evidence = compiler.compile(receipt, parent_feedback=parent["receipt"] if parent else None,
                                    contract=active.get("contract"))
        write_json(Path(active["folder"]) / "official-evidence.json", evidence)
        self.state.update(active=None, draft_failures=0)
        self.save()

    def _evaluation(self, active):
        observed = self.api.lookup(active["evaluation_request_id"])
        if observed is None:
            self._require_evaluation_dispatch(active)
            self._check_prepared_candidate(active)
        receipt = self.driver.step(active["checkout"]["checkout_id"], active["key"], active["tree_digest"])
        if receipt["status"] not in TERMINAL:
            receipt = self.api.wait(active["evaluation_request_id"], self._progress)
        self._finish_evaluation(active, receipt)

    def _finalize(self, reason):
        self.require_integrity()
        score, delivery = self._champions()
        status = self.api.status()
        summary = {"task": self.task, "campaign_id": self.campaign, "reason": reason,
                   "official_evaluations": len(status["history"]),
                   "score_champion": asdict(score) if score else None,
                   "delivery_champion": asdict(delivery) if delivery else None,
                   "budget_observations": self.state["budget_observations"],
                   "unresolved_model_calls": status["unresolved_model_calls"],
                   "pending_model_usage_policy": self.pending_usage_policy,
                   "review_after_evaluations": self.review_after,
                   "actual_model_charge_rmb": None,
                   "budget_assurance": "managed_codex_advisory_public_balance_only",
                   "active_parent_roles": self.state.get("active_parent_roles", {})}
        write_json(self.control / "summary.json", summary)
        self.event("finalize", reason=reason,
                   candidate_id=delivery.candidate_id if delivery else None)
        if delivery is None:
            raise RuntimeError("No complete official candidate has nine confirmed legal cells")
        return delivery.candidate_id

    def _recover_pending_guards(self):
        active = self.state["active"]
        for op in ("codex", "probe"):
            for row in self.journal.entries(op):
                if row["state"] == "guard_rejected":
                    raise RuntimeError(op + " rejected by local guard: " + row["request_id"])
                if row["state"] != "guard_pending":
                    continue
                if not active:
                    raise RuntimeError("Local guard verification has no active draft")
                suffix = "-edit-"
                expected = (self.journal.request_id(op, active["key"] + suffix + str(active["repair_count"]))
                            if op == "codex" else active.get("probe_request_id"))
                if row["request_id"] != expected:
                    raise RuntimeError("Local guard verification differs from the active draft")
                def verify_saved(frozen):
                    self._guard_after_model(frozen, pending_request_id=row["request_id"])
                if op == "codex":
                    turn = self.api.codex(
                        self.journal, active["key"] + suffix + str(active["repair_count"]),
                        Path(active["prompt_path"]).read_text(encoding="utf-8"),
                        self._guard_before_model, verify_saved)
                    self._observe_model(active, row["request_id"], turn, self.api.status())
                    active.update(turn=turn, phase="validate")
                    self.save()
                else:
                    saved = row["response"] or {}
                    if not saved.get("guard_before") or saved.get("official_response") is None:
                        raise RuntimeError("Local probe guard lacks its original evidence")
                    result = saved["official_response"]
                    self.api.verify_after(self.journal, row["request_id"], saved["guard_before"],
                                          result, verify_saved)
                    self.journal.record(row["request_id"], "failed" if "probe_error" in result else "succeeded",
                                        result)

    def run(self):
        try:
            self._recover_pending_guards()
            self._recover_history()
            while True:
                active = self.state["active"]
                if active:
                    phase = active["phase"]
                    if phase == "research_stopped":
                        return self._finalize("research_requested_stop")
                    if phase == "submit_ready" and active.get("research_review_version") != REVIEW_VERSION:
                        self._check_prepared_candidate(active)
                        active["phase"] = "validate"
                        self.save()
                        continue
                    if (phase == "submit_ready" and self.review_after is not None
                            and len(self.state["records"]) >= self.review_after):
                        return self._review_checkpoint(active)
                    handlers = {"checkout": self._checkout, "prepare_model": self._prepare_model,
                                "model": self._model, "validate": self._validate,
                                "submit_ready": self._submit, "evaluation": self._evaluation}
                    handlers[phase](active)
                    continue
                status = self.api.status()
                if len(status["history"]) >= self.limit:
                    return self._finalize("model_usage_pending_reconciliation" if status["unresolved_model_calls"]
                                          else "configured_evaluation_limit")
                if self.state["draft_failures"] >= 2:
                    return self._finalize("two_unsubmitted_drafts_exhausted")
                remaining = min(status["evaluations_remaining"], self.limit - len(status["history"]))
                runway = plan_runway(self.api.money_state(status, remaining),
                                    has_legal_champion=self._champions()[1] is not None,
                                    pending_usage_after_success=self._pending_usage_after_success())
                if runway.action == "reconcile":
                    return self._finalize("model_usage_pending_reconciliation")
                if runway.action in {"finalize_legal", "no_legal_candidate", "model_closed_no_legal_candidate"}:
                    return self._finalize(runway.action)
                if runway.action == "reserve_release_review":
                    self.event("release_advisory_reserve", available_rmb=status["model_cost_available_rmb"])
                    runway = plan_runway(self.api.money_state(status, remaining), reserve=Decimal("0"),
                                        has_legal_champion=self._champions()[1] is not None,
                                        pending_usage_after_success=self._pending_usage_after_success())
                if not status["codex_available"]:
                    return self._finalize("official_managed_codex_unavailable")
                self._new_draft(status, runway)
        finally:
            self.journal.close()
