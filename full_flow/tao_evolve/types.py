from dataclasses import dataclass, field
from decimal import Decimal
from typing import Tuple, Mapping, Optional

@dataclass(frozen=True)
class OfficialResult:
    request_id: str
    candidate_id: str
    status: str
    score: Optional[float]
    # Counts are normalized by a verified adapter, never inferred from the score.
    completed_cells: int = 0
    task: str = ""

@dataclass(frozen=True)
class BudgetSnapshot:
    evaluations_remaining: int
    model_cost_available_rmb: Decimal
    model_dispatch_allowed: bool
    evaluation_dispatch_allowed: bool
    unresolved_model_calls: int = 0

@dataclass(frozen=True)
class ProbeResult:
    probe: str
    status: str                  # pass, fail, unsupported_input, timeout, error
    evidence_kind: str          # invariant, proxy, observation
    contract_verified: bool = False
    input_compatible: bool = False
    candidate_code_executed: bool = False
    reproduction_confirmed: bool = False
    hard_errors: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()
    is_official_evaluation: bool = False
    metrics: Mapping[str, float] = field(default_factory=dict)

@dataclass(frozen=True)
class ArtifactReceipt:
    campaign_id: str
    task: str
    split: str
    case_id: str
    seed: str
    origin: str                 # current_run_input, current_run_probe, current_run_official
    artifact_sha256: str
    input_digest: str
    environment_digest: str
    upstream_digest: str
    units: str
    stage: str
