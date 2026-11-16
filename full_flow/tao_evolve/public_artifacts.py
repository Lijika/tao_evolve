"""Bind a fixed-upstream HBT diagnostic to the current official parent."""
import json
from pathlib import Path
import shutil

from .adapter import digest
from .guard_runtime import file_digest
from .provenance import confined_path


def hbt_artifacts(campaign, binding, parent, candidate_digest, before, scratch):
    if binding["task"] != "full_flow" or not parent:
        return {}
    receipt = parent["receipt"]
    if receipt.get("status") != "succeeded" or not receipt.get("files_index"):
        return {}
    if parent.get("tree_digest") and parent["tree_digest"] != before["digest"]:
        raise ValueError("Parent checkout differs from its previously frozen source")
    root = Path(receipt["artifacts_dir"]).resolve()
    index_path = Path(receipt["files_index"])
    if index_path.is_symlink() or index_path.resolve() != root / "files.json":
        raise ValueError("Public artifact index is outside its evaluation")
    files = json.loads(index_path.read_text())
    cases = {row["id"]: row for row in binding["manifest"]["cases"] if row["split"] == "train"}
    seeds = {row["id"] for row in binding["manifest"]["seeds"]}
    for logical, item in sorted(files.items()):
        parts = Path(logical).parts
        if (len(parts) < 5 or parts[0] != "cells" or parts[1] not in cases or parts[2] not in seeds
                or not logical.endswith(("/50_hbt/problem.json", "/50_hbt/problem.json.gz"))):
            continue
        source = confined_path(root, item["path"])
        if not source.is_file():
            continue
        directory = Path(scratch)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ("hbt-problem.json.gz" if source.suffix == ".gz" else "hbt-problem.json")
        shutil.copyfile(str(source), str(path))
        input_digest = cases[parts[1]]["input_identity"]
        environment = digest(binding["manifest"]["incremental"])
        upstream = digest({key: value for key, value in before["entries"].items()
                           if not key.startswith("Place-3D/src/hbt/")})
        artifact = {"campaign_id": campaign, "task": binding["task"], "split": "train",
                    "case_id": parts[1], "seed": parts[2], "origin": "current_run_official",
                    "artifact_sha256": file_digest(path), "input_digest": input_digest,
                    "environment_digest": environment, "upstream_digest": upstream,
                    "units": "dbu", "stage": "hbt_problem"}
        return {"campaign_id": campaign, "parent_candidate_id": receipt["candidate_id"],
                "candidate_digest": candidate_digest, "input_digest": input_digest,
                "environment_digest": environment, "upstream_digest": upstream,
                "allowed_cases": sorted(cases), "items": [
                    {"probe": "F", "path": str(path), "producer_candidate_id": receipt["candidate_id"],
                     "candidate_digest": before["digest"], "receipt": artifact}],
                "coverage": "one_indexed_training_hbt_problem_under_aggregate_time_limit"}
    return {}
