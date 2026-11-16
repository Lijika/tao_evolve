"""Official-task facts, not a library of offline EDA optimization recipes.
Profiles are bound against the installed official policy before use.
"""
from dataclasses import dataclass
from typing import FrozenSet, Tuple

@dataclass(frozen=True)
class TaskProfile:
    name: str
    endpoint: str
    editable_stages: FrozenSet[str]
    observed_stages: Tuple[str, ...]
    research_mode: str
    allowed_probes: FrozenSet[str]
    has_overflow_term: bool

PROFILES = {
    'partition_placement': TaskProfile(
        'partition_placement', 'post_opt', frozenset(('partition','placement')),
        ('partition','placement','pdn','hbt','opt'), 'bounded_upstream_coupling',
        frozenset(('A0','A1','B0','C')), False),
    'full_flow': TaskProfile(
        'full_flow', 'post_grt', frozenset(('partition','placement','hbt','opt','cts','grt')),
        ('partition','placement','pdn','hbt','opt','cts','grt'), 'endpoint_backtrace',
        frozenset(('A0','A1','B0','C','F')), True)
}

def get_profile(task):
    if task not in PROFILES:
        raise ValueError('Unrecognized task: %r' % task)
    return PROFILES[task]

def validate_edit_stages(task, stages):
    stages = frozenset(stages)
    if not stages or not stages.issubset(get_profile(task).editable_stages):
        raise ValueError('Empty or unauthorized stage scope')
    return stages

def default_probe_ids(task, available):
    # Capability checks are mandatory in addition to this task filter.
    return tuple(sorted(get_profile(task).allowed_probes.intersection(available)))
