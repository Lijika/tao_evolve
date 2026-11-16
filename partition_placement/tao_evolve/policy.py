"""Pure control policies. They do not invoke a model, a solver, or the official ledger."""
import math
from decimal import Decimal
from typing import Iterable, Optional, Tuple
from .types import OfficialResult, BudgetSnapshot, ProbeResult

TERMINAL = frozenset(("succeeded", "failed", "rejected"))
PENDING = frozenset(("accepted", "running", "uncertain", "reconciling"))


def select_champion(rows: Iterable[OfficialResult], task: str,
                    expected_cells: int = 9) -> Optional[OfficialResult]:
    """Highest official finite score only; exact ties prefer the earlier row.

    A matrix with physical-invalid cells can still be officially succeeded.
    Do not exclude it or remove its -100 cells. An unscored baseline is not final-eligible.
    """
    best = None
    for r in rows:
        if r.task != task or r.status != "succeeded" or r.completed_cells != expected_cells:
            continue
        if not r.candidate_id or r.score is None or isinstance(r.score, bool):
            continue
        try:
            score = float(r.score)
        except (ValueError, TypeError):
            continue
        if not math.isfinite(score):
            continue
        if best is None or score > float(best.score):
            best = r
    return best


def probe_blocks_submission(p: ProbeResult) -> bool:
    """Performance proxies NEVER hard-block. Unsupported/timeouts are not algorithm failures."""
    if p.is_official_evaluation:
        raise ValueError("A local probe cannot claim official authority")
    return (p.status == "fail" and p.evidence_kind == "invariant"
            and p.contract_verified and p.input_compatible
            and p.candidate_code_executed and p.reproduction_confirmed
            and bool(p.hard_errors))


def model_spend_envelope(b: BudgetSnapshot, reserve_rmb: Decimal,
                         opportunities: int, hard_cap_rmb: Decimal) -> Decimal:
    """Advisory per-edit envelope, NOT a provider charge limit or a Codex-turn limiter."""
    vals = (b.model_cost_available_rmb, reserve_rmb, hard_cap_rmb)
    if any(not v.is_finite() or v < 0 for v in vals):
        raise ValueError("Budget values must be finite and nonnegative")
    if opportunities < 1:
        raise ValueError("Need at least one opportunity")
    if not b.model_dispatch_allowed or b.unresolved_model_calls:
        return Decimal("0")
    free = max(Decimal("0"), b.model_cost_available_rmb - reserve_rmb)
    return min(hard_cap_rmb, free / Decimal(opportunities))


def next_action(b: BudgetSnapshot, pending_status: Optional[str],
                has_prepared_candidate: bool, has_champion: bool) -> str:
    """Reconcile before stopping; a model budget closure need not forbid a prepared submit."""
    if pending_status is not None and pending_status not in TERMINAL:
        return "reconcile"
    if has_prepared_candidate and b.evaluation_dispatch_allowed and b.evaluations_remaining > 0:
        return "submit_prepared"
    if (b.evaluation_dispatch_allowed and b.evaluations_remaining > 0
            and b.model_dispatch_allowed and b.unresolved_model_calls == 0):
        return "edit"
    return "finalize" if has_champion else "stop_without_eligible_candidate"


def exact_duplicate(candidate_digest: str, known_digests: Iterable[str],
                    explicit_replicate: bool = False) -> bool:
    if not candidate_digest:
        raise ValueError("Missing candidate digest")
    return (not explicit_replicate) and candidate_digest in set(known_digests)
