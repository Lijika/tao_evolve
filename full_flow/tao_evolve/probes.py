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
import hashlib
import os
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path

from .guard_runtime import file_digest, snapshot_tree
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
        capability = context.get("capabilities", {}).get(probe, {})
        return None, capability.get("reason", "no_current_artifact")
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


def _hash_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def _cache_binding(task, root, context):
    """Bind reuse to trusted public identities plus the actual executable inputs."""
    required = ("campaign_id", "input_digest", "environment_digest")
    if not all(isinstance(context.get(k), str) and context[k] for k in required):
        return None, "incomplete_campaign_input_or_environment_binding"
    source = snapshot_tree(root, retain_python=False)
    if source["errors"]:
        return None, "candidate_inventory_unverified"
    if context.get("candidate_digest") and source["digest"] != context["candidate_digest"]:
        return None, "candidate_digest_mismatch"
    return {
        "schema_version": 1, "task": task, "campaign_id": context["campaign_id"],
        "candidate_digest": source["digest"], "input_digest": context["input_digest"],
        "environment_digest": context["environment_digest"],
        "upstream_digest": context.get("upstream_digest"),
        "parent_candidate_id": context.get("parent_candidate_id"),
        "worker_files": {p.name: file_digest(p) for p in sorted(Path(__file__).parent.glob("*.py"))},
        "runtime": {"python": sys.version, "executable_sha256": file_digest(sys.executable),
                    "platform": platform.platform(),
                    "environment": {k: os.environ.get(k) for k in
                                    ("PATH", "PYTHONPATH", "LD_LIBRARY_PATH", "LANG", "LC_ALL")},
                    "cpu_affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None},
        "parameters": {"cpu_limit": 4, "probe_seconds": 60.0, "total_seconds": TOTAL_SECONDS,
                       "fixture_seeds": "frozen_worker_source"}}, "exact_execution_inputs"


def _cacheable(result):
    return (result.get("candidate_bound") and result.get("input_current")
            and not result.get("integrity_errors") and
            (result.get("status") == "pass" or
             (result.get("status") == "fail" and result.get("contract_verified")
              and result.get("reproducible") and result.get("hard_errors"))))


def _read_cache(path, binding):
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 3 * 1024 * 1024:
            return None
        record = json.loads(path.read_text())
        result = record["result"]
        if (record["binding"] != binding or record["key"] != _hash_json(binding)
                or record["result_sha256"] != _hash_json(result) or not _cacheable(result)):
            return None
        executions = record["execution_evidence"]
        if not executions or any(hashlib.sha256(e["text"].encode()).hexdigest() != e["sha256"]
                                 for e in executions):
            return None
        result["cache"] = {"status": "hit", "key": record["key"], "record": str(path),
                           "execution_reused": True, "original_evidence_id": result["evidence_id"]}
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_cache(path, binding, result):
    if not _cacheable(result):
        return False
    evidence = []
    for executed in (result, result.get("repeat")):
        if executed is None:
            continue
        original = Path(executed.get("evidence_id", ""))
        if original.is_symlink() or not original.is_file() or original.stat().st_size > 1024 * 1024:
            return False
        raw = original.read_text(encoding="utf-8")
        evidence.append({"original_path": str(original), "text": raw,
                         "sha256": hashlib.sha256(raw.encode()).hexdigest()})
    frozen = {k: v for k, v in result.items() if k != "cache"}
    record = {"key": _hash_json(binding), "binding": binding, "result": frozen,
              "result_sha256": _hash_json(frozen), "execution_evidence": evidence}
    temporary = safe_write_target(path.parent, path.name + ".pending")
    temporary.write_text(json.dumps(record, sort_keys=True, allow_nan=False), encoding="utf-8")
    os.replace(str(temporary), str(path))
    return True


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


def run_probes(task, checkout_root, changed_paths, scratch, artifacts=None, cache_dir=None):
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
    deadline = time.monotonic() + TOTAL_SECONDS
    cache_base, cache_reason = None, "cache_directory_not_supplied"
    if cache_dir is not None:
        cache_dir = Path(cache_dir)
        if cache_dir.is_symlink() or root == cache_dir.resolve() or root in cache_dir.resolve().parents:
            raise ValueError("probe cache must be outside the candidate checkout")
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_base, cache_reason = _cache_binding(task, root, context)
    wanted = {"A0": any(p.startswith(PARTITION) for p in changed),
              "B0": any(p.startswith(PLACEMENT) for p in changed),
              "A1": any(p.startswith(PARTITION) for p in changed),
              "C": any(p.startswith((PARTITION, PLACEMENT)) for p in changed),
              "F": any(p.startswith(HBT) for p in changed)}
    results, errors, pending_cache = [], [], []
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
        binding, cached, cache_path = None, None, None
        if cache_base is not None:
            inputs = ({k: v for k, v in artifact.items() if k not in ("path", "input_path")}
                      if artifact else {"input_class": "synthetic_fixture"})
            binding = {**cache_base, "probe": probe, "inputs": inputs}
            cache_path = safe_write_target(cache_dir, _hash_json(binding) + ".json")
            cached = _read_cache(cache_path, binding)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            results.append(_result(probe, "aggregate_time_limit", "timeout"))
            continue
        result = cached or _execute(request, safe_write_target(scratch, probe + "-first"), min(60.0, remaining))
        if cached is None:
            result["reproducible"] = False
        if cached is None and result.get("status") == "fail" and result.get("contract_verified"):
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
        if cached is not None:
            errors.extend(error for error in result.get("hard_errors", []) if error not in errors)
        elif binding is not None:
            result["cache"] = {"status": "miss", "key": _hash_json(binding),
                               "record": str(cache_path), "execution_reused": False}
            pending_cache.append((cache_path, binding, result))
        results.append(result)
    for probe in ("B1", "D", "E"):
        results.append(_result(probe, "disabled_by_v1_1", "disabled"))
    if pending_cache:
        after = snapshot_tree(root, retain_python=False)
        if not after["errors"] and after["digest"] == cache_base["candidate_digest"]:
            for path, binding, result in pending_cache:
                result["cache"]["stored"] = bool(_write_cache(path, binding, result))
    return {"results": results, "hard_errors": errors,
            "capabilities": {r["probe"]: {"status": r["status"], "reason": r.get("reason", "")}
                             for r in results},
            "budget_seconds": TOTAL_SECONDS,
            "cache": {"enabled": cache_base is not None, "reason": cache_reason},
            "assurance": "READY_FOR_OFFICIAL_UNPROVEN_PHYSICAL" if not errors else "REPAIR_OR_DISCARD"}
