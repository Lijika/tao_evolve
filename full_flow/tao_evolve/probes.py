"""Serial, bounded local diagnostics. No official evaluation service is invoked.

``artifacts`` is optional controller-owned context, never a model assertion::

    {
      "campaign_id": "...", "parent_candidate_id": "...",
      "candidate_digest": "<current check_tree digest>",
      "input_digest": "...", "environment_digest": "...",
      "upstream_digest": "<parent source/input dependency digest>",
      "allowed_cases": ["..."],
      "items": [{
        "probe": "F", "path": "/scratch/copied/problem.json.gz",
        "producer_candidate_id": "<parent_candidate_id>",
        "candidate_digest": "<producer tree digest>",
        "receipt": { ... ArtifactReceipt fields ... }
      }]
    }

F accepts a public training HBT problem from the current campaign's official
parent and only a candidate whose changes are confined to HBT assignment.
A1/C require artifacts produced by the exact current candidate. A1 also needs
``input_path`` and ``input_sha256`` for the matching public LoL input. C expects
explicit per-die pin/net geometry and routing capacity in its JSON artifact.
Missing or stale inputs produce UNKNOWN. The controller must freeze copies and
derive these receipts from actual public records, not untrusted proposal text.
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .guard_runtime import file_digest
from .guards_v11 import safe_write_target
from .provenance import validate_artifact
from .task_profiles import get_profile
from .types import ArtifactReceipt

TOTAL_SECONDS = 180.0
PARTITION = "Place-3D/src/partition/lol/"
PLACEMENT = "Place-3D/thirdparty/DREAMPlace/dreamplace/"
HBT = "Place-3D/src/hbt/"


def _hbt_only(changed):
    return bool(changed) and all(
        p == HBT + "assignment.py" or p.startswith(HBT + "contest_algorithms/")
        for p in changed)


def _result(probe, reason, status="unknown"):
    return {"probe": probe, "status": status, "reason": reason,
            "kind": "observation", "contract_verified": False,
            "input_current": False, "candidate_bound": False,
            "reproducible": False, "hard_errors": [], "metrics": {}}


def _artifact(probe, task, context, changed):
    items = [item for item in context.get("items", []) if item.get("probe") == probe]
    if not items:
        return None, "no_current_artifact"
    reasons = []
    for item in items:
        try:
            required = ("campaign_id", "input_digest", "environment_digest",
                        "upstream_digest", "allowed_cases", "candidate_digest")
            if not all(context.get(key) for key in required):
                reasons.append("incomplete_artifact_context")
                continue
            receipt = ArtifactReceipt(**item["receipt"])
            valid, reason = validate_artifact(
                receipt, campaign_id=context["campaign_id"], task=task,
                allowed_cases=context["allowed_cases"], seed=receipt.seed,
                input_digest=context["input_digest"],
                environment_digest=context["environment_digest"],
                upstream_digest=context["upstream_digest"],
                units="dbu" if probe in {"C", "F"} else receipt.units,
                stage={"A1": "partition", "C": "placement", "F": "hbt_problem"}[probe])
            if not valid:
                reasons.append(reason)
                continue
            path = Path(item["path"])
            if path.is_symlink() or not path.is_file() or file_digest(path) != receipt.artifact_sha256:
                reasons.append("artifact_changed_or_missing")
                continue
            if probe == "F":
                if not _hbt_only(changed):
                    reasons.append("upstream_changed_or_hbt_not_targeted")
                    continue
                if not context.get("parent_candidate_id") or item.get("producer_candidate_id") != context["parent_candidate_id"]:
                    reasons.append("not_current_official_parent")
                    continue
            elif item.get("candidate_digest") != context["candidate_digest"]:
                reasons.append("artifact_not_current_candidate")
                continue
            if probe == "A1":
                input_path = Path(item["input_path"])
                if input_path.is_symlink() or not item.get("input_sha256") or file_digest(input_path) != item["input_sha256"]:
                    reasons.append("partition_input_changed_or_missing")
                    continue
            accepted = dict(item)
            accepted["path"] = str(path.resolve())
            return accepted, "compatible"
        except (KeyError, TypeError, ValueError, OSError):
            reasons.append("invalid_artifact_receipt")
    return None, ",".join(sorted(set(reasons)))


def _execute(request, directory, timeout):
    directory.mkdir(parents=True, exist_ok=True)
    request_file = safe_write_target(directory, "request.json")
    output_file = safe_write_target(directory, "result.json")
    log_file = safe_write_target(directory, "worker.log")
    if output_file.exists():
        output_file.unlink()
    request_file.write_text(json.dumps(request, sort_keys=True), encoding="utf-8")
    # Keep native library discovery, remove inherited gateway credentials/config.
    env = {key: os.environ[key] for key in ("PATH", "PYTHONPATH", "LD_LIBRARY_PATH", "LANG", "LC_ALL")
           if key in os.environ}
    env.update({"HOME": str(directory), "TMPDIR": str(directory),
                "OMP_NUM_THREADS": "4", "MKL_NUM_THREADS": "4",
                "OPENBLAS_NUM_THREADS": "4", "NUMEXPR_NUM_THREADS": "4",
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0",
                "CUDA_VISIBLE_DEVICES": ""})
    command = [sys.executable, "-B", str(Path(__file__).with_name("probe_worker.py")),
               str(request_file), str(output_file)]
    started = time.monotonic()
    with log_file.open("wb") as log:
        process = subprocess.Popen(command, cwd=str(directory), env=env,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            process.wait(timeout=max(0.01, timeout))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            result = _result(request["probe"], "aggregate_or_probe_time_limit", "timeout")
        else:
            if process.returncode != 0 or not output_file.is_file() or output_file.stat().st_size > 1024 * 1024:
                result = _result(request["probe"], "worker_failed_or_result_missing")
            else:
                try:
                    result = json.loads(output_file.read_text(encoding="utf-8"))
                    if not isinstance(result, dict) or result.get("probe") != request["probe"]:
                        raise ValueError("invalid worker result")
                except (ValueError, OSError):
                    result = _result(request["probe"], "invalid_worker_result")
        finally:
            # Native/candidate child processes share the worker process group.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    result["evidence_id"] = str(output_file) if output_file.is_file() else str(log_file)
    return result


def run_probes(task, checkout_root, changed_paths, scratch, artifacts=None):
    """Run affected, available contracts once; repeat failures once to confirm.

    Budget covers all workers, including failure reproduction, in series.
    Imports and timeouts remain UNKNOWN. A local PASS never claims chip legality.
    """
    allowed = get_profile(task).allowed_probes
    root, scratch = Path(checkout_root).resolve(), Path(scratch).resolve()
    if root == scratch or root in scratch.parents:
        raise ValueError("probe scratch must be outside the candidate checkout")
    scratch.mkdir(parents=True, exist_ok=True)
    changed = tuple(changed_paths)
    context = artifacts or {}
    wanted = {"A0": any(p.startswith(PARTITION) for p in changed),
              "B0": any(p.startswith(PLACEMENT) for p in changed),
              "A1": any(p.startswith(PARTITION) for p in changed),
              "C": any(p.startswith((PARTITION, PLACEMENT)) for p in changed),
              "F": any(p.startswith(HBT) for p in changed)}
    results, errors = [], []
    deadline = time.monotonic() + TOTAL_SECONDS
    for probe in ("A0", "B0", "A1", "C", "F"):
        if probe not in allowed:
            results.append(_result(probe, "disabled_for_task", "disabled"))
            continue
        if not wanted[probe]:
            results.append(_result(probe, "unaffected_scope", "skipped"))
            continue
        artifact = None
        if probe in {"A1", "C", "F"}:
            artifact, reason = _artifact(probe, task, context, changed)
            if artifact is None:
                results.append(_result(probe, reason))
                continue
        request = {"probe": probe, "task": task, "checkout_root": str(root),
                   "artifact": artifact, "cpu_limit": 4}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            results.append(_result(probe, "aggregate_time_limit", "timeout"))
            continue
        result = _execute(request, safe_write_target(scratch, probe + "-first"), min(60.0, remaining))
        result["reproducible"] = False
        if result.get("status") == "fail" and result.get("contract_verified"):
            remaining = deadline - time.monotonic()
            if remaining > 0:
                repeated = _execute(request, safe_write_target(scratch, probe + "-repeat"), min(60.0, remaining))
                result["repeat"] = repeated
                first_errors = result.get("invariant_errors", [])
                result["reproducible"] = (bool(first_errors) and repeated.get("status") == "fail"
                                          and repeated.get("invariant_errors") == first_errors
                                          and repeated.get("binding") == result.get("binding"))
            if all(result.get(k) for k in ("reproducible", "candidate_bound", "input_current")):
                result["hard_errors"] = [probe + ":" + e for e in result.get("invariant_errors", [])]
                errors.extend(result["hard_errors"])
        if artifact:
            result["artifact_receipt"] = artifact["receipt"]
            frozen_inputs = [(artifact["path"], artifact["receipt"]["artifact_sha256"])]
            if probe == "A1":
                frozen_inputs.append((artifact["input_path"], artifact["input_sha256"]))
            for path, expected in frozen_inputs:
                try:
                    unchanged = Path(path).is_file() and not Path(path).is_symlink() and file_digest(path) == expected
                except OSError:
                    unchanged = False
                if not unchanged:
                    error = probe + ":immutable_probe_input_changed"
                    result.setdefault("integrity_errors", []).append(error)
                    result["status"] = "unknown"
                    result["input_current"] = False
                    result["reason"] = "probe_input_integrity_failed"
                    result.setdefault("hard_errors", []).append(error)
                    errors.append(error)
        results.append(result)
    for probe in ("B1", "D", "E"):
        results.append(_result(probe, "disabled_by_v1_1", "disabled"))
    return {"results": results, "hard_errors": errors,
            "capabilities": {r["probe"]: {"status": r["status"], "reason": r.get("reason", "")}
                             for r in results},
            "budget_seconds": TOTAL_SECONDS,
            "assurance": "READY_FOR_OFFICIAL_UNPROVEN_PHYSICAL" if not errors else "REPAIR_OR_DISCARD"}
