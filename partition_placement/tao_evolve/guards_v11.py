"""Advisory/deployment guards. No local check certifies all physical legality."""
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple
import hashlib

@dataclass(frozen=True)
class Finding:
    kind: str                 # invariant / proxy / observation
    status: str               # pass / fail / unknown / timeout
    contract_verified: bool
    input_current: bool
    candidate_bound: bool     # candidate code OR candidate-produced artifact
    reproducible: bool
    evidence_id: str


def blocks(f):
    return (f.kind=='invariant' and f.status=='fail' and f.contract_verified
            and f.input_current and f.candidate_bound and f.reproducible and bool(f.evidence_id))

def local_admission(findings, integrity_verified):
    if not integrity_verified:return 'BLOCK_INTEGRITY'
    if any(blocks(f) for f in findings):return 'REPAIR_OR_DISCARD'
    # Unknown, partial or absent coverage cannot be promoted to a physical-valid claim.
    return 'READY_FOR_OFFICIAL_UNPROVEN_PHYSICAL'


def safe_write_target(root, relative):
    """Resolve only beneath a participant-owned root. Call before each tool write.
    Still requires OS sandboxing against TOCTOU and arbitrary candidate syscalls.
    """
    root=Path(root)
    if root.is_symlink():raise ValueError('Symlink root')
    rel=Path(relative)
    if rel.is_absolute() or '..' in rel.parts or not rel.parts:
        raise ValueError('Invalid relative path')
    cursor=root
    for part in rel.parts:
        cursor=cursor/part
        if cursor.is_symlink():raise ValueError('Symlink component')
    base=root.resolve();target=(root/rel).resolve()
    if target==base or base not in target.parents:raise ValueError('Escaping path')
    return target


def snapshot_files(paths):
    """Read-only snapshots of EXPLICIT accessible immutable files, never budget DBs.
    The binding must supply a complete file-set manifest and compare file additions too.
    """
    paths=tuple(paths)
    if not paths:raise ValueError('Empty integrity manifest')
    out={}
    for p in map(Path,paths):
        if p.is_symlink() or not p.is_file():raise ValueError('Unsafe snapshot file')
        out[str(p)]=hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def verify_snapshot(snapshot):
    try:return snapshot_files(snapshot.keys())==snapshot
    except (OSError,ValueError):return False


def assert_training_receipt(campaign,task,split,expected_campaign,expected_task):
    if (campaign,task,split)!=(expected_campaign,expected_task,'train'):
        raise ValueError('Cross-campaign/task or forbidden data split')
