"""Bind diagnostics to the actual public export format and current campaign."""
import gzip
import hashlib
import json
from pathlib import Path
import shutil

from .adapter import digest
from .guard_runtime import file_digest
from .guards_v11 import safe_write_target
from .provenance import confined_path

MAX_ARTIFACT_BYTES = 32 * 1024 * 1024


class ArtifactRegistry:
    """Current parent exports support F isolation, not new-draft A1/C inputs."""

    def __init__(self, campaign, binding, parent, candidate_digest, before, scratch):
        self.binding, self.parent, self.before = binding, parent, before
        self.scratch = Path(scratch)
        self.cases = {row["id"]: row for row in binding["manifest"]["cases"] if row["split"] == "train"}
        self.seeds = {row["id"] for row in binding["manifest"]["seeds"]}
        self.context = {
            "campaign_id": campaign, "task": binding["task"],
            "candidate_digest": candidate_digest,
            "input_digest": digest({key: row["input_identity"] for key, row in self.cases.items()}),
            "environment_digest": digest(binding["manifest"]["incremental"]),
            "upstream_digest": digest({key: value for key, value in before["entries"].items()
                                       if not key.startswith("Place-3D/src/hbt/")}),
            "allowed_cases": sorted(self.cases), "items": [],
            "capabilities": {
                "A1": {"status": "unknown", "reason": "no_current_candidate_assignment_and_lol_input",
                       "requires": ["current_candidate_assignment", "matching_lol_input",
                                    "verified_internal_to_DEF_name_mapping"]},
                "C": {"status": "unknown", "reason": "no_current_candidate_pin_geometry_and_capacity",
                      "requires": ["current_candidate_pin_geometry", "die_local_dbu",
                                   "verified_routing_capacity"]},
                "F": {"status": "unknown", "reason": "no_current_official_parent_hbt_problem"},
            }}

    def _published_file(self, root, logical, item):
        path = confined_path(root, item["path"])
        encoding = item.get("encoding")
        if encoding not in ("identity", "gzip"):
            raise ValueError("unsupported_public_artifact_encoding")
        if path.stat().st_size > MAX_ARTIFACT_BYTES:
            raise ValueError("public_artifact_exceeds_diagnostic_read_limit")
        raw = path.read_bytes()
        # Native .gz sources are hashed before publication; publisher-compressed
        # plain sources retain their uncompressed source hash in files.json.
        if encoding == "gzip" and not logical.endswith(".gz"):
            with gzip.open(str(path), "rb") as stream:
                source = stream.read(MAX_ARTIFACT_BYTES + 1)
        else:
            source = raw
        if (len(source) > MAX_ARTIFACT_BYTES or len(source) != item.get("source_bytes")
                or hashlib.sha256(source).hexdigest() != item.get("source_sha256")):
            raise ValueError("public_artifact_hash_or_size_differs_from_index")
        return path, raw

    def collect(self):
        result = self.context
        if self.binding["task"] != "full_flow":
            result["capabilities"]["F"] = {"status": "disabled", "reason": "disabled_for_task"}
        if not self.parent:
            return result
        receipt = self.parent["receipt"]
        feedback = receipt.get("feedback", {})
        if (receipt.get("status") != "succeeded" or not receipt.get("files_index")
                or feedback.get("task") != self.binding["task"] or feedback.get("split") != "train"
                or feedback.get("candidate_id") != receipt.get("candidate_id")
                or feedback.get("request_id") != receipt.get("request_id")
                or feedback.get("task_digest") != self.binding["task_digest"]):
            result["capabilities"]["F"]["reason"] = "public_parent_receipt_not_bound"
            return result
        if self.parent.get("tree_digest") != self.before["digest"]:
            raise ValueError("Parent checkout differs from its previously frozen source")
        root = Path(receipt["artifacts_dir"]).resolve()
        index_path = Path(receipt["files_index"])
        if index_path.is_symlink() or index_path.resolve() != root / "files.json":
            raise ValueError("Public artifact index is outside its evaluation")
        if index_path.stat().st_size > MAX_ARTIFACT_BYTES:
            raise ValueError("Public artifact index exceeds diagnostic read limit")
        index_raw = index_path.read_bytes()
        files = json.loads(index_raw)
        if not isinstance(files, dict):
            raise ValueError("Public artifact index must be a mapping")
        result["parent_candidate_id"] = receipt["candidate_id"]
        result["public_index"] = {"path": str(index_path),
                                  "sha256": hashlib.sha256(index_raw).hexdigest(),
                                  "request_id": receipt["request_id"]}
        hbt = []
        for logical, item in sorted(files.items()):
            parts = Path(logical).parts
            if (len(parts) < 5 or parts[0] != "cells" or parts[1] not in self.cases
                    or parts[2] not in self.seeds):
                continue
            if logical.endswith("/partition_assignment.csv"):
                result["capabilities"]["A1"].update(
                    reason="public_parent_assignment_is_not_current_draft_and_lacks_bound_lol_mapping",
                    observed_format="inst_name,die using DEF component names",
                    example_logical_path=logical, input_class="observation_only")
            if "/placement/" in logical or "_placement/" in logical:
                if logical.endswith("/metrics.json"):
                    result["capabilities"]["C"].update(
                        reason="public_placement_summary_has_no_current_pin_geometry_or_capacity",
                        observed_format="die-local-object-origin-hpwl",
                        example_logical_path=logical, input_class="observation_only")
            if logical.endswith(("/50_hbt/problem.json", "/50_hbt/problem.json.gz")):
                hbt.append((logical, item, parts[1], parts[2]))
        if self.binding["task"] != "full_flow":
            return result
        for logical, item, case, seed in hbt:
            try:
                source, raw = self._published_file(root, logical, item)
                self.scratch.mkdir(parents=True, exist_ok=True)
                compressed = item["encoding"] == "gzip"
                path = safe_write_target(self.scratch, "hbt-problem.json.gz" if compressed else "hbt-problem.json")
                shutil.copyfile(str(source), str(path))
                if file_digest(path) != hashlib.sha256(raw).hexdigest():
                    raise ValueError("public_artifact_changed_while_copying")
            except (OSError, KeyError, TypeError, ValueError, EOFError) as error:
                result["capabilities"]["F"].update(
                    reason="public_hbt_artifact_unavailable_or_unverified", detail=str(error)[:200])
                continue
            result["input_digest"] = self.cases[case]["input_identity"]
            artifact = {key: result[key] for key in
                        ("campaign_id", "task", "input_digest", "environment_digest", "upstream_digest")}
            artifact.update(split="train", case_id=case, seed=seed, origin="current_run_official",
                            artifact_sha256=file_digest(path), units="dbu", stage="hbt_problem")
            result["items"] = [{"probe": "F", "path": str(path),
                                "producer_candidate_id": receipt["candidate_id"],
                                "candidate_digest": self.before["digest"], "receipt": artifact,
                                "public_logical_path": logical, "public_index_sha256": result["public_index"]["sha256"],
                                "source_sha256": item["source_sha256"]}]
            result["capabilities"]["F"] = {"status": "available",
                                           "reason": "bound_public_parent_hbt_problem",
                                           "input_class": "fixed_upstream_isolation"}
            result["coverage"] = "one_indexed_training_hbt_problem_under_aggregate_time_limit"
            break
        return result


def hbt_artifacts(campaign, binding, parent, candidate_digest, before, scratch):
    """Retain the Controller entry point while reporting all artifact capabilities."""
    return ArtifactRegistry(campaign, binding, parent, candidate_digest, before, scratch).collect()
