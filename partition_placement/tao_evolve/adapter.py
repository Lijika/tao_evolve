"""Bindings to the installed public AgentContext; no organizer storage access."""
import hashlib
import json
import math
import re
import time
from pathlib import Path

from .budget_v11 import MoneyState, money
from .portfolio_v11 import Cell, MatrixResult, complete_official


TERMINAL = frozenset(("succeeded", "failed", "rejected"))


class LocalGuardViolation(RuntimeError):
    pass


class GuardStatusUnavailable(RuntimeError):
    pass


def digest(value):
    # Match the documented task JSON identity, including ASCII escaping.
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def bind_task(context):
    names = {"full_flow": "full-flow-native-task.json",
             "partition_placement": "partition-placement-native-task.json"}
    task = context.task
    path = Path(context.files["framework"]) / "competition/manifests" / names[task]
    manifest = json.loads(path.read_text())
    expected_endpoint = ("post-grt-passive-hbt-timing" if task == "full_flow"
                         else "post-opt-pre-cts")
    if manifest["endpoint"] != expected_endpoint or manifest["opt_passes"] != 1:
        raise ValueError("Installed endpoint differs from the task profile")
    if manifest["optimization"]["baseline"] != context.baseline_candidate:
        raise ValueError("Initial baseline differs from the installed task")
    if set(context.source_policy["allowed_paths"]) != set(manifest["optimization"]["allowed_files"]):
        raise ValueError("Public source policy differs from the installed task")
    cases = tuple(c["id"] for c in manifest["cases"] if c["split"] == "train")
    seeds = tuple(s["partition"] for s in manifest["seeds"])
    if len(set(cases)) != 3 or seeds != (1001, 1002, 1003):
        raise ValueError("Expected the fixed three-case, three-seed training matrix")
    return {"task": task, "task_digest": digest(manifest),
            "expected_keys": tuple((c, s) for c in cases for s in seeds),
            "manifest_path": str(path), "manifest": manifest,
            "policy_digest": digest(context.source_policy)}


def parse_matrix(receipt, campaign, binding):
    if receipt.get("status") != "succeeded":
        return None
    data = receipt["feedback"]
    if (data.get("request_id") != receipt.get("request_id")
            or data.get("candidate_id") != receipt.get("candidate_id")
            or not re.fullmatch(r"[0-9a-f]{64}", str(data.get("candidate_id", "")))
            or data.get("task") != binding["task"] or data.get("split") != "train"
            or data.get("task_digest") != binding["task_digest"]
            or type(data.get("completed_cells")) is not int or data["completed_cells"] != 9):
        raise ValueError("Official feedback identity or completion differs")
    counts = (data.get("verified_cells"), data.get("invalid_cells"))
    if any(type(n) is not int or n < 0 for n in counts) or sum(counts) != 9:
        raise ValueError("Official completion counts do not cover the matrix")
    if type(data.get("score")) not in (int, float) or not math.isfinite(data["score"]):
        raise ValueError("Official score must be a finite number")
    cells = []
    for row in data.get("cells", []):
        valid = row.get("valid")
        if valid is not None and type(valid) is not bool:
            raise ValueError("Physical validity must be a boolean or unknown")
        if type(row.get("seed")) is not int:
            raise ValueError("Unexpected seed type")
        if type(row.get("score")) not in (int, float) or not math.isfinite(row["score"]):
            raise ValueError("Cell score must be a finite number")
        cells.append(Cell(row["case"], row["seed"], row["score"], valid))
    if all(c.physical_valid is not None for c in cells):
        if (sum(c.physical_valid is True for c in cells),
                sum(c.physical_valid is False for c in cells)) != counts:
            raise ValueError("Official validity flags and completion counts disagree")
    result = MatrixResult(campaign, binding["task"], data["candidate_id"], "succeeded",
                          data["score"], binding["task_digest"], tuple(cells))
    if not complete_official(result, campaign, binding["task"],
                             binding["expected_keys"], binding["task_digest"]):
        raise ValueError("Incomplete, duplicated, or malformed official matrix")
    return result


class PublicAdapter:
    idempotent_submission_verified = True

    def __init__(self, context, sleep=time.sleep):
        self.context, self.sleep = context, sleep

    def status(self):
        value = self.context.status()
        if value.get("task") != self.context.task:
            raise ValueError("Status belongs to another task")
        for key in ("evaluations_remaining", "unresolved_model_calls"):
            if type(value.get(key)) is not int or value[key] < 0:
                raise ValueError("Invalid public counter: " + key)
        for key in ("model_dispatch_allowed", "evaluation_dispatch_allowed", "codex_available"):
            if type(value.get(key)) is not bool:
                raise ValueError("Invalid public dispatch flag: " + key)
        money(value["model_cost_available_rmb"])
        rows = value["history"]
        if len({r["request_id"] for r in rows}) != len(rows):
            raise ValueError("Duplicate official request ID")
        return value

    def money_state(self, status, remaining=None):
        return MoneyState(
            money(status["model_cost_available_rmb"]),
            status["evaluations_remaining"] if remaining is None else remaining,
            status["model_dispatch_allowed"], status["evaluation_dispatch_allowed"],
            status["unresolved_model_calls"], None)

    def lookup(self, request_id):
        # Only a successful public status query can establish absence.
        if not any(row["request_id"] == request_id for row in self.status()["history"]):
            return None
        return self.context.evaluation_status(request_id)

    def submit(self, checkout_id, request_id):
        return self.context.submit_evaluation(checkout_id, request_id=request_id)

    def wait(self, request_id, callback=None):
        while True:
            result = self.context.evaluation_status(request_id)
            if result.get("request_id") != request_id:
                raise ValueError("Evaluation response belongs to another request")
            if callback:
                callback(result)
            if result["status"] in TERMINAL:
                return result
            if result["status"] not in {"accepted", "running", "uncertain", "reconciling"}:
                raise ValueError("Unknown evaluation status")
            self.sleep(5)

    def verify_after(self, journal, rid, frozen, response, after_call):
        """Keep a failed check bound to its original snapshot and original receipt."""
        if not after_call:
            return
        for attempt in range(3):
            try:
                after_call(frozen)
                return
            except LocalGuardViolation as error:
                journal.record(rid, "guard_rejected", {
                    "official_response": response, "guard_error": str(error)})
                raise
            except GuardStatusUnavailable as error:
                if attempt < 2:
                    self.sleep(1)
                    continue
                pending_error = error
            except Exception as error:
                pending_error = error
            journal.record(rid, "guard_pending", {
                "official_response": response, "guard_before": frozen,
                "guard_error": str(pending_error)})
            raise RuntimeError("Model turn local guard verification pending: " + rid) from pending_error

    def codex(self, journal, logical_key, prompt, before_call=None, after_call=None):
        rid = journal.prepare("codex", logical_key, {"prompt": prompt})
        local = journal.get(rid)
        if local["state"] == "guard_rejected":
            raise RuntimeError("Model turn rejected by local guard: " + rid)
        if local["state"] in {"succeeded", "failed"}:
            return local["response"]
        if local["state"] == "guard_pending":
            saved = local["response"] or {}
            if not after_call or not saved.get("guard_before"):
                raise RuntimeError("Model turn local guard verification pending: " + rid)
            self.verify_after(journal, rid, saved["guard_before"],
                              saved.get("official_response"), after_call)
            result = saved.get("official_response")
            if not isinstance(result, dict) or type(result.get("success")) is not bool:
                # No new model dispatch or pre-call snapshot can supply a lost receipt.
                raise RuntimeError("Original model receipt still requires reconciliation: " + rid)
            if result.get("status") in {"running", "reconciliation_required"}:
                raise RuntimeError("Original model turn still requires reconciliation: " + rid)
            journal.record(rid, "succeeded" if result["success"] else "failed", result)
            return result
        errors = 0
        while True:
            journal.record(rid, "sending")
            frozen = before_call() if before_call else None
            try:
                result = self.context.codex_turn(prompt, request_id=rid)
            except (RuntimeError, OSError):
                self.verify_after(journal, rid, frozen, None, after_call)
                # A lost reply can still have spent money and changed the workspace.
                journal.record(rid, "uncertain")
                errors += 1
                if errors >= 3:
                    raise
                self.sleep(5)
                continue
            self.verify_after(journal, rid, frozen, result, after_call)
            if result.get("status") == "running" and result.get("retryable") is True:
                journal.record(rid, "running", result)
                self.sleep(5)
                continue
            if result.get("status") == "reconciliation_required":
                journal.record(rid, "reconciling", result)
                raise RuntimeError("The official Codex turn requires reconciliation")
            if type(result.get("success")) is not bool:
                journal.record(rid, "uncertain", result)
                raise ValueError("Unrecognized Codex receipt")
            journal.record(rid, "succeeded" if result["success"] else "failed", result)
            return result
