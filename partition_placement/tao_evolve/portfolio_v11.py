"""Complete official matrices; keep all scores, choose a strictly legal delivery.
No scorer, no netlist/PDK manipulation, and no rule-definition overrides.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Tuple, Optional
from statistics import mean, pstdev

@dataclass(frozen=True)
class Cell:
    case: str
    seed: int
    score: float                 # already returned by official evaluation
    physical_valid: Optional[bool]

@dataclass(frozen=True)
class MatrixResult:
    campaign: str
    task: str
    candidate_id: str
    status: str
    score: float                 # official whole-matrix score, NOT recomputed here
    definition_digest: str
    cells: Tuple[Cell, ...]

def finite_number(x):
    if isinstance(x, bool) or x is None:
        return False
    try:
        return Decimal(str(x)).is_finite()
    except (InvalidOperation, ValueError, TypeError):
        return False

def complete_official(r, campaign, task, expected_keys, definition_digest):
    expected = frozenset(expected_keys)
    keys = [(x.case,x.seed) for x in r.cells]
    return bool(expected and r.campaign == campaign and r.task == task
        and r.status == 'succeeded' and r.candidate_id
        and r.definition_digest == definition_digest
        and len(keys) == len(expected) and len(set(keys)) == len(keys)
        and frozenset(keys) == expected and finite_number(r.score)
        and all(finite_number(x.score) for x in r.cells))

def champions(rows, campaign, task, expected_keys, definition_digest):
    """Return (official_score_champion, all_cells_legal_delivery_champion).

    Penalized physical failures remain in the official archive.  The second
    selection is an explicitly stricter team delivery policy, not a rule change.
    Exact ties retain earlier list order; unknown validity is not proven legal.
    """
    best = deploy = None
    for r in rows:
        if not complete_official(r,campaign,task,expected_keys,definition_digest):
            continue
        if best is None or Decimal(str(r.score)) > Decimal(str(best.score)):
            best = r
        if all(c.physical_valid is True for c in r.cells):
            if deploy is None or Decimal(str(r.score)) > Decimal(str(deploy.score)):
                deploy = r
    return best, deploy

def paired_risk(parent, child, expected_keys):
    """Descriptive TRAIN-only paired diagnostics; no confidence/p-value claim.
    LOO means leaving out each already evaluated case in the summary, NOT
    running fewer cases or obtaining extra official evaluations.
    """
    expected_keys = tuple(expected_keys)
    if (parent.campaign,parent.task,parent.definition_digest) != (
            child.campaign,child.task,child.definition_digest):
        raise ValueError('Different campaign/task/metric definition')
    for r in (parent,child):
        if not complete_official(r,r.campaign,r.task,expected_keys,r.definition_digest):
            raise ValueError('Incomplete official matrix')
    p={(x.case,x.seed):x for x in parent.cells}
    c={(x.case,x.seed):x for x in child.cells}
    per_case={}
    for case in sorted(set(k[0] for k in p)):
        gains=[float(c[k].score)-float(p[k].score) for k in sorted(p) if k[0]==case]
        per_case[case]={'mean_gain':mean(gains),'min_gain':min(gains),
                       'seed_spread':max(gains)-min(gains),
                       'seed_std_descriptive':pstdev(gains), 'n_seeds':len(gains)}
    means={k:v['mean_gain'] for k,v in per_case.items()}
    loo={k:mean([v for j,v in means.items() if j!=k]) for k in means if len(means)>1}
    return {'scope':'observed_training_only', 'case_summary':per_case,
            'mean_paired_gain':mean(means.values()),'worst_case_gain':min(means.values()),
            'case_win_count':sum(v>0 for v in means.values()),
            'leave_one_case_out_mean':loo,
            'negative_after_omitting_best_case':bool(loo and loo[max(means,key=means.get)]<0),
            'physical_invalid_cells':sum(x.physical_valid is False for x in child.cells),
            'unknown_validity_cells':sum(x.physical_valid is None for x in child.cells),
            'cross_process_validation':'not_observed'}
