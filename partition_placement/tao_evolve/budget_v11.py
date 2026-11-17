"""RMB-first advisory scheduling. Does not access official ledger storage.
The official gateway controls actual admission and charging.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Tuple, Optional

D=Decimal

def money(x):
    if isinstance(x,bool):raise ValueError('Boolean is not money')
    try:d=D(str(x))
    except (InvalidOperation,TypeError,ValueError):raise ValueError('Bad money')
    if not d.is_finite() or d<0:raise ValueError('Nonfinite or negative money')
    return d

@dataclass(frozen=True)
class MoneyState:
    available: Decimal            # official already excludes outstanding reservations
    remaining_submissions: int
    model_allowed: bool
    evaluation_allowed: bool
    unresolved_calls: int
    minimum_request_reservation: Optional[Decimal]

@dataclass(frozen=True)
class Runway:
    action: str
    per_remaining_round: Decimal
    projected_main_total: Decimal
    shortfall: Decimal
    guaranteed_ten_rounds: bool = False

def plan_runway(s, observed_round_costs=(), reserve=D('1.00'),
                guarded_backend=False, draft_ready=False, has_legal_champion=False,
                pending_usage_after_success=False):
    a,r=money(s.available),money(reserve)
    q=None if s.minimum_request_reservation is None else money(s.minimum_request_reservation)
    # An unknown public admission reserve cannot support a bounded-backend claim.
    guarded_backend=guarded_backend and q is not None
    if s.remaining_submissions<0 or s.unresolved_calls<0:
        raise ValueError('Invalid counter')
    if s.unresolved_calls and not pending_usage_after_success:
        return Runway('reconcile',D(0),D(0),D(0))
    if draft_ready and s.evaluation_allowed and s.remaining_submissions>0:
        return Runway('submit_ready',D(0),D(0),D(0))
    if s.remaining_submissions==0 or not s.evaluation_allowed:
        return Runway('finalize_legal' if has_legal_champion else 'no_legal_candidate',D(0),D(0),D(0))
    n=D(s.remaining_submissions)
    per=max(D(0),a-r)/n
    costs=[money(c) for c in observed_round_costs]
    # Rolling max is a deliberately conservative heuristic, not a statistical quantile.
    recent=max(costs[-3:]) if costs else per
    projected=recent*n
    short=max(D(0),projected+r-a)
    if not s.model_allowed or (q is not None and a<q):
        action='finalize_legal' if has_legal_champion else 'model_closed_no_legal_candidate'
    elif per==0:
        # Reserve release is an explicit controller event, never silent overspending.
        action='reserve_release_review'
    elif short>0:
        action='bounded_compact' if guarded_backend else 'managed_codex_budget_risk'
    else:action='bounded_standard' if guarded_backend else 'managed_codex'
    return Runway(action,per,projected,short)

def request_admissible(s, verified_charge_upper, round_remaining, need_followup=False):
    """A per-request cap only when the price/token upper bound was independently verified.
    Unknown upper bound -> no claim of local hard control; managed Codex is separate.
    q is a rolling admission reserve, not q times number of future calls.
    """
    if verified_charge_upper is None or s.minimum_request_reservation is None:return False
    a,q,c,e=map(money,(s.available,s.minimum_request_reservation,verified_charge_upper,round_remaining))
    if s.remaining_submissions<=0 or s.unresolved_calls or not s.model_allowed or a<q or c>e or c>a:return False
    return not need_followup or a-c>=q

def charge_bound(input_tokens, output_tokens, input_price_per_million, output_price_per_million):
    """Use ONLY pinned official prices and validated maximum billable token counts.
    No cache discount is assumed. Thinking tokens, if charged, must be included upstream.
    """
    if any(isinstance(t,bool) or not isinstance(t,int) or t<0 for t in (input_tokens,output_tokens)):
        raise ValueError('Bad token bound')
    pi,po=money(input_price_per_million),money(output_price_per_million)
    return (D(input_tokens)*pi+D(output_tokens)*po)/D(1000000)
