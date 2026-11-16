"""Small deterministic scheduler; concrete EDA mechanisms come from current-run evidence."""
from dataclasses import dataclass
from .task_profiles import get_profile

@dataclass(frozen=True)
class SearchState:
    task: str
    remaining: int
    has_legal_champion: bool
    confirmed_error: bool = False
    stagnation: int = 0
    stage_conflict_supported: bool = False
    coupled_contract_supported: bool = False
    complete_training_feedback: bool = False


def choose_research_mode(s):
    p=get_profile(s.task)
    if s.remaining<0:raise ValueError('Negative remaining evaluations')
    if s.remaining==0:return 'FINALIZE_LEGAL' if s.has_legal_champion else 'NO_LEGAL_RETURN'
    if s.confirmed_error:return 'REPAIR_SAME_MECHANISM'
    if not s.has_legal_champion:return 'BOOTSTRAP_FEASIBILITY'
    if s.remaining<=2:return 'CONSOLIDATE_SUPPORTED_MECHANISM'
    if s.stage_conflict_supported:
        if not s.coupled_contract_supported:return 'READ_DEPENDENCY_EVIDENCE'
        return 'PP_COUPLED_INTERVENTION' if s.task=='partition_placement' else 'FF_COUPLED_INTERVENTION'
    if s.stagnation>=2:return 'BRANCH_WITH_EVIDENCE'
    return 'PP_ENDPOINT_GUIDED_SEARCH' if s.task=='partition_placement' else 'FF_ENDPOINT_BACKTRACE'


def paid_prompt_mode(needs_complex_plan, budget_mode):
    # Reflection normally folds into the next editing prompt, not another API bill.
    if budget_mode in ('bounded_compact','managed_codex_budget_risk'):
        return 'ONE_COMBINED_EDIT'
    return 'OPTIONAL_PLAN_THEN_EDIT' if needs_complex_plan else 'ONE_COMBINED_EDIT'
