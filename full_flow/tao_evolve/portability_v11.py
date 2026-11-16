"""Structural portability audit only, NOT verification on unseen PDKs."""
import ast
from dataclasses import dataclass
from typing import Tuple

@dataclass(frozen=True)
class PortabilityFinding:
    line: int
    reason: str
    severity: str = 'review'


def inspect_candidate(source, identity_literals=()):
    """Apply to newly edited ALGORITHM source, not legitimate harness input routing.
    Findings are review flags: names in docstrings/tests are not proof of cheating.
    Complete static or dynamic information-flow security is outside this function.
    """
    tree=ast.parse(source);out=[];ids=set(identity_literals)
    for node in ast.walk(tree):
        if isinstance(node,ast.Compare):
            constants=[n.value for n in ast.walk(node) if isinstance(n,ast.Constant)]
            if any(isinstance(v,str) and v in ids for v in constants):
                out.append(PortabilityFinding(node.lineno,'identity-dependent comparison'))
        if isinstance(node,ast.Constant) and isinstance(node.value,(int,float)) and not isinstance(node.value,bool):
            if node.value not in (0,1,2,-1):
                out.append(PortabilityFinding(node.lineno,'numeric constant needs source/unit rationale'))
    return tuple(out)


def portability_status(observed_processes, has_case_identity_branch, constraints_modified,
                       unit_assumptions_verified):
    if constraints_modified:return 'BLOCK_IMMUTABILITY'
    if has_case_identity_branch:return 'REVIEW_IDENTITY_DEPENDENCE'
    if not unit_assumptions_verified:return 'UNKNOWN_UNIT_ASSUMPTIONS'
    return ('OBSERVED_PROCESSES_ONLY' if len(set(observed_processes))>1
            else 'CROSS_PROCESS_UNVERIFIED')
