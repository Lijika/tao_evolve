"""Validate experiment identity; no path is accepted based on its filename alone."""
from pathlib import Path
from typing import Iterable, Tuple
from .types import ArtifactReceipt


def validate_artifact(r: ArtifactReceipt, *, campaign_id: str, task: str,
                      allowed_cases: Iterable[str], seed: str, input_digest: str,
                      environment_digest: str, upstream_digest: str,
                      units: str, stage: str) -> Tuple[bool, str]:
    checks = (
        (r.campaign_id == campaign_id, "foreign_campaign"),
        (r.task == task, "foreign_task"),
        (r.split == "train", "non_training_input"),
        (r.case_id in set(allowed_cases), "case_not_allowed"),
        (r.origin in ("current_run_input", "current_run_probe", "current_run_official"), "offline_artifact"),
        (r.seed == seed, "seed_mismatch"),
        (r.input_digest == input_digest, "input_mismatch"),
        (r.environment_digest == environment_digest, "environment_mismatch"),
        (r.upstream_digest == upstream_digest, "stale_upstream"),
        (r.units == units, "units_mismatch"),
        (r.stage == stage, "stage_mismatch"),
        (len(r.artifact_sha256) == 64 and all(c in "0123456789abcdef" for c in r.artifact_sha256), "invalid_digest"),
    )
    for ok, reason in checks:
        if not ok:
            return False, reason
    return True, "compatible"


def confined_path(root: Path, relative: str) -> Path:
    """Reject absolute paths, parent traversal and existing symlink components.

    This is a path validator, NOT an OS sandbox, and is not race-proof against a
    concurrent malicious writer. The architecture requires a single writer.
    """
    p = Path(relative)
    if p.is_absolute() or not relative or ".." in p.parts:
        raise ValueError("Unsafe relative path")
    base = root.resolve(strict=True)
    current = base
    for part in p.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Symlinks are forbidden")
    resolved = current.resolve(strict=False)
    try:
        resolved.relative_to(base)
    except ValueError:
        raise ValueError("Path escapes root")
    return resolved
