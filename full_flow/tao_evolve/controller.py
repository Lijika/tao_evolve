"""Deterministic two-task evolution using only the public competition API."""
from dataclasses import asdict
from decimal import Decimal
import json
import os
from pathlib import Path
import time
import uuid

from .adapter import PublicAdapter, TERMINAL, bind_task, digest, parse_matrix
from .budget_v11 import plan_runway
from .guard_runtime import snapshot_tree, check_tree, freeze_paths, verify_frozen
from .journal import IntentJournal
from .portfolio_v11 import champions
from .probes import run_probes
from .research import EvidenceCompiler, build_prompt, validate_model_contract
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
        self.config = read_json(self.agent_dir / "runtime.json")
        self.limit = self.config["max_evaluations"]
        if type(self.limit) is not int or not 1 <= self.limit <= 10:
            raise ValueError("max_evaluations must be an integer from 1 to 10")
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
            raise RuntimeError("Frozen Agent/input integrity failed: " + "; ".join(result["errors"]))
        if digest(self.context.source_policy) != self.binding["policy_digest"]:
            raise RuntimeError("Source policy changed during the run")

    def _guard_before_model(self):
        self.require_integrity()
        return {"control": snapshot_tree(self.control, retain_python=False)["entries"],
                "history": sorted(r["request_id"] for r in self.api.status()["history"])}

    def _guard_after_model(self, before):
        self.require_integrity()
        after = snapshot_tree(self.control, retain_python=False)
        old = dict(before["control"])
        entries = after["entries"]
        # The proposal is the sole model-writable file within participant control data.
        contract = str(Path(self.state["active"]["contract_path"]).relative_to(self.control))
        old.pop(contract, None)
        entries.pop(contract, None)
        if after["errors"] or old != entries:
            raise RuntimeError("Model changed participant control data outside its contract")
        history = sorted(r["request_id"] for r in self.api.status()["history"])
        if before["history"] != history:
            raise RuntimeError("Model dispatched an unregistered official evaluation")

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
                  "contract": (active or {}).get("contract")}
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
                                    parent_feedback=parent_record["receipt"] if parent_record else None)
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
        confirmed_error = bool(latest and (latest["status"] != "succeeded"
                               or any(c.get("valid") is False for c in latest.get("feedback", {}).get("cells", []))))
        mode = choose_research_mode(SearchState(
            self.task, remaining, delivery is not None, confirmed_error=confirmed_error,
            stagnation=self.state["stagnation"], complete_training_feedback=bool(self._rows()),
            stage_conflict_supported=bool(latest_evidence.get("stage_conflict_supported")),
            coupled_contract_supported=bool(len(previous_contract.get("edit_stages", [])) > 1
                                            and previous_contract.get("verified_impact_refs"))))
        parent = self._select_parent(mode)
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
                                        parent_feedback=previous["receipt"] if previous else None)
            evidence["selected_parent_evidence"] = selected_parent
        if latest:
            evidence["latest_contract"] = latest.get("contract")
        evidence["evidence_role"] = "latest_experiment_for_reflection"
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

    def _model(self, active):
        status = self.api.status()
        if status["unresolved_model_calls"]:
            raise RuntimeError("Model usage remains unresolved; no new model turn is allowed")
        prompt = Path(active["prompt_path"]).read_text(encoding="utf-8")
        self.event("model_edit", draft=active["key"], repair=active["repair_count"],
                   mode=active["mode"], available_rmb=status["model_cost_available_rmb"])
        before_available = status["model_cost_available_rmb"]
        turn = self.api.codex(self.journal, active["key"] + "-edit-" + str(active["repair_count"]),
                              prompt, self._guard_before_model, self._guard_after_model)
        after = self.api.status()
        self.state["budget_observations"].append({
            "draft": active["key"], "before_available_rmb": before_available,
            "after_available_rmb": after["model_cost_available_rmb"],
            "unresolved_model_calls": after["unresolved_model_calls"],
            "settled_cost_rmb": None, "semantics": "public_available_balance_not_charge"})
        active.update(turn=turn, phase="validate")
        self.save()
        self._check_pending_model_usage(active, after)

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
        if status["unresolved_model_calls"]:
            active.update(phase="validate", repair_errors=errors)
            self.save()
            raise RuntimeError("Local checks failed while model usage remains unresolved; draft preserved without repair")
        if (active["repair_count"] < 1 and status["model_dispatch_allowed"]
                and status["evaluation_dispatch_allowed"]):
            active.update(repair_count=1, mode="REPAIR_SAME_MECHANISM",
                          repair_errors=errors, phase="prepare_model")
        else:
            self.state["active"] = None
            self.state["draft_failures"] += 1
        self.save()

    def _validate(self, active):
        self.require_integrity()
        self._check_pending_model_usage(active, self.api.status())
        before = read_json(Path(active["folder"]) / "before.json")
        check = check_tree(before, active["checkout"]["path"], self.context.source_policy,
                           original_paths=self.original_paths)
        report = {k: v for k, v in check.items() if k != "snapshot"}
        errors = list(check["errors"])
        if not check["changed_paths"]:
            errors.append("no_source_change")
        if active.get("turn", {}).get("success") is not True:
            errors.append("model_turn_failed:" + str(active["turn"].get("error", "incomplete edit")))
        contract = None
        if not errors:
            try:
                contract = validate_model_contract(active["contract_path"], self.task,
                                                   active["checkout"]["path"], check["changed_paths"])
                if contract["parent_candidate_id"] != active["parent_id"]:
                    raise ValueError("contract parent differs from the official checkout")
            except (ValueError, OSError, KeyError, TypeError) as error:
                errors.append("contract_invalid:" + str(error))
        if not errors:
            probes = run_probes(self.task, active["checkout"]["path"], check["changed_paths"],
                                self.scratch / active["key"] / ("probes-" + str(active["repair_count"])),
                                artifacts=self._probe_artifacts(active, check))
            report["probes"] = probes
            errors.extend(probes["hard_errors"])
            self.require_integrity()
            post = check_tree(before, active["checkout"]["path"], self.context.source_policy,
                              original_paths=self.original_paths)
            if post["digest"] != check["digest"]:
                errors.append("candidate_changed_during_local_probe")
        if not errors and check["digest"] in {r.get("tree_digest") for r in self.state["records"]}:
            errors.append("duplicate_already_submitted_source")
        report["errors"] = errors
        write_json(Path(active["folder"]) / ("preflight-%d.json" % active["repair_count"]), report)
        if errors:
            return self._retry_or_discard(active, errors)
        active.update(tree_digest=check["digest"], changed_paths=check["changed_paths"],
                      contract=contract, phase="submit_ready")
        self.save()
        self.event("ready_for_official", draft=active["key"], changed_paths=check["changed_paths"],
                   assurance="READY_FOR_OFFICIAL_UNPROVEN_PHYSICAL")

    def _probe_artifacts(self, active, check):
        from .public_artifacts import hbt_artifacts
        parent = self._record_for(active["parent_id"])
        return hbt_artifacts(self.campaign, self.binding, parent, check["digest"],
                             read_json(Path(active["folder"]) / "before.json"),
                             self.scratch / active["key"] / "public-inputs")

    def _submit(self, active):
        self.require_integrity()
        self._require_evaluation_dispatch(active)
        current = snapshot_tree(active["checkout"]["path"], retain_python=False)
        if current["errors"] or current["digest"] != active["tree_digest"]:
            raise RuntimeError("Prepared candidate changed before submission")
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
        evidence = compiler.compile(receipt, parent_feedback=parent["receipt"] if parent else None)
        write_json(Path(active["folder"]) / "official-evidence.json", evidence)
        self.state.update(active=None, draft_failures=0)
        self.save()

    def _evaluation(self, active):
        observed = self.api.lookup(active["evaluation_request_id"])
        if observed is None:
            self._require_evaluation_dispatch(active)
            current = snapshot_tree(active["checkout"]["path"], retain_python=False)
            if current["errors"] or current["digest"] != active["tree_digest"]:
                raise RuntimeError("Prepared candidate changed before recovered submission")
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
                   "actual_model_charge_rmb": None,
                   "budget_assurance": "managed_codex_advisory_public_balance_only",
                   "active_parent_roles": self.state.get("active_parent_roles", {})}
        write_json(self.control / "summary.json", summary)
        self.event("finalize", reason=reason,
                   candidate_id=delivery.candidate_id if delivery else None)
        if delivery is None:
            raise RuntimeError("No complete official candidate has nine confirmed legal cells")
        return delivery.candidate_id

    def run(self):
        try:
            self._recover_history()
            while True:
                active = self.state["active"]
                if active:
                    phase = active["phase"]
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
                                    has_legal_champion=self._champions()[1] is not None)
                if runway.action == "reconcile":
                    return self._finalize("model_usage_pending_reconciliation")
                if runway.action in {"finalize_legal", "no_legal_candidate", "model_closed_no_legal_candidate"}:
                    return self._finalize(runway.action)
                if runway.action == "reserve_release_review":
                    self.event("release_advisory_reserve", available_rmb=status["model_cost_available_rmb"])
                    runway = plan_runway(self.api.money_state(status, remaining), reserve=Decimal("0"),
                                        has_legal_champion=self._champions()[1] is not None)
                if not status["codex_available"]:
                    return self._finalize("official_managed_codex_unavailable")
                self._new_draft(status, runway)
        finally:
            self.journal.close()
