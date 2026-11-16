"""Compare observations without treating stages as additive timing components."""
from dataclasses import dataclass
from typing import Optional, Tuple
import math

@dataclass(frozen=True)
class Observation:
    candidate: str
    campaign: str
    task: str
    case: str
    seed: int
    stage: str
    metric: str
    value: float
    higher_is_better: bool
    definition: str
    unit: str
    view: str
    clock_model: str
    parasitic_model: str
    endpoint_universe: str
    environment: str
    input_constraints: str
    evidence_id: str
    compatible: bool = True
    coverage_complete: bool = True

def paired_stage_gain(parent, child):
    fields=('campaign','task','case','seed','stage','metric','higher_is_better',
            'definition','unit','view','clock_model','parasitic_model',
            'endpoint_universe','environment','input_constraints')
    if any(getattr(parent,k)!=getattr(child,k) for k in fields):
        return {'status':'incomparable','gain':None,'reason':'measurement_contract_mismatch'}
    if not parent.evidence_id or not child.evidence_id or parent.candidate==child.candidate:
        return {'status':'incomparable','gain':None,'reason':'missing_evidence_or_same_candidate'}
    if not all((parent.compatible,child.compatible,parent.coverage_complete,child.coverage_complete)):
        return {'status':'incomparable','gain':None,'reason':'stale_or_incomplete'}
    if any(isinstance(x.value,bool) or not isinstance(x.value,(int,float)) or not math.isfinite(x.value)
           for x in (parent,child)):
        return {'status':'incomparable','gain':None,'reason':'nonfinite_or_nonnumeric'}
    gain=(child.value-parent.value)*(1 if child.higher_is_better else -1)
    return {'status':'compared','gain':gain,'evidence_ids':[parent.evidence_id,child.evidence_id]}

def retention_signal(upstream_pair, final_pair, tolerance=0.0):
    """Sign consistency across two within-stage paired comparisons, NOT causality.
    Different metrics need NOT share units: only their oriented signs are used.
    """
    if tolerance < 0 or not math.isfinite(tolerance):
        raise ValueError('Invalid tolerance')
    if any(x.get('status')!='compared' for x in (upstream_pair,final_pair)):
        return 'insufficient_evidence'
    u,f=upstream_pair['gain'],final_pair['gain']
    if u>tolerance and f < -tolerance:return 'local_gain_not_retained'
    if u>tolerance and f>tolerance:return 'gain_consistent_not_causal'
    if u < -tolerance and f>tolerance:return 'proxy_regression_final_gain'
    return 'mixed_or_no_resolved_change'

def endpoint_turnover(parent_top, child_top, mapping_verified=False):
    """Top-K endpoint turnover; absence of an identity mapping is not zero overlap."""
    if not mapping_verified:return None
    a,b=set(parent_top),set(child_top)
    if not a or not b:return None
    return 1.0-len(a&b)/float(len(a|b))

class DependencyGraph:
    """Edges MUST be constructed from current official flow/code, not model guesses."""
    def __init__(self, edges):
        self.adj={}
        for src,dst in edges:
            self.adj.setdefault(src,set()).add(dst)
            self.adj.setdefault(dst,set())
        # A flow DAG with a cycle is not silently accepted.
        visiting=set();done=set()
        def visit(n):
            if n in visiting:raise ValueError('Cyclic stage dependencies')
            if n in done:return
            visiting.add(n)
            for m in self.adj[n]:visit(m)
            visiting.remove(n);done.add(n)
        for n in self.adj:visit(n)
    def invalidated(self, changed):
        if any(s not in self.adj for s in changed):
            raise ValueError('Unknown dependency: invalidate conservatively upstream')
        result=set(changed);todo=list(changed)
        while todo:
            for n in self.adj[todo.pop()]:
                if n not in result:result.add(n);todo.append(n)
        return frozenset(result)

@dataclass(frozen=True)
class CouplingContract:
    task: str
    edit_stages: Tuple[str, ...]
    source_refs: Tuple[str, ...]
    impact_refs: Tuple[str, ...]
    downstream_obligations: Tuple[str, ...]
    tradeoff_statement: str
    final_endpoint: str
    falsifier: str
    portability_obligation: str

def validate_contract(c, verified_ref_ids):
    from .task_profiles import get_profile, validate_edit_stages
    p=get_profile(c.task);validate_edit_stages(c.task,c.edit_stages)
    if c.final_endpoint!=p.endpoint:raise ValueError('Wrong scoring endpoint')
    if not c.source_refs or not set(c.source_refs).issubset(verified_ref_ids):
        raise ValueError('No verified source support')
    if len(set(c.edit_stages))>1:
        if not c.impact_refs or not set(c.impact_refs).issubset(verified_ref_ids):
            raise ValueError('Multi-stage edit requires verified dependency evidence')
    if not c.downstream_obligations or not all((c.tradeoff_statement,c.falsifier,c.portability_obligation)):
        raise ValueError('Missing tradeoff, downstream, portability or falsifier')
    return True
