"""Structural portability audit only, NOT verification on unseen PDKs."""
import ast
from dataclasses import dataclass
import difflib
import hashlib

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


CARD_ITEMS = (
    "units", "process_attributes", "scale", "feature_origin", "randomness",
    "failure_behavior", "explanation_scope",
)


def build_portability_card(before, after, changed_paths, candidate_digest, context=None):
    """Record edited-source observations; semantic conclusions need review evidence.

    ``context`` may supply training identity literals/processes from the installed
    public task manifest. Source text or a model's assertion is not that authority.
    """
    context = context or {}
    refs, flags, unknowns = [], [], []
    for name in changed_paths:
        text = after.get(name)
        if text is None:
            unknowns.append("no_current_python_source:" + name)
            continue
        old_lines, lines = before.get(name, "").splitlines(), text.splitlines()
        changed_lines = set()
        for tag, _, _, start, stop in difflib.SequenceMatcher(
                a=old_lines, b=lines, autojunk=False).get_opcodes():
            if tag != "equal":
                changed_lines.update(range(start + 1, stop + 1))
        sha = hashlib.sha256(text.encode("utf-8", errors="surrogateescape")).hexdigest()
        refs.append({"source_ref": name + ":" + str(min(changed_lines or {1})),
                     "source_sha256": sha, "changed_lines": sorted(changed_lines)})
        try:
            found = inspect_candidate(text, context.get("identity_literals", ()))
        except (SyntaxError, ValueError, UnicodeError):
            unknowns.append("unparsed_source:" + name)
            continue
        for finding in found:
            if finding.line not in changed_lines:
                continue
            category = ("feature_origin" if finding.reason == "identity-dependent comparison"
                        else "scale")
            ref = name + ":" + str(finding.line)
            flag = {"severity": "review", "category": category,
                    "source_ref": ref, "source_sha256": sha, "reason": finding.reason}
            flag["id"] = hashlib.sha256(
                (ref + ":" + sha + ":" + finding.reason).encode("utf-8")).hexdigest()[:20]
            if flag not in flags:
                flags.append(flag)
    items = {
        name: {"status": "unknown", "evidence_refs": [ref["source_ref"] for ref in refs],
               "review_flag_ids": [f["id"] for f in flags if f["category"] == name]}
        for name in CARD_ITEMS
    }
    items["units"]["unknowns"] = ["numeric_units_and_conversion_require_source_review"]
    items["process_attributes"]["unknowns"] = ["process_parameter_origin_not_semantically_verified"]
    items["scale"]["unknowns"] = ["normalization_population_and_branch_activation_not_verified"]
    items["feature_origin"]["unknowns"] = ["input_feature_information_flow_not_verified"]
    items["randomness"]["unknowns"] = ["seed_use_and_selection_behavior_not_semantically_verified"]
    items["failure_behavior"]["unknowns"] = ["only_separate_executed_contracts_can_establish_coverage"]
    items["explanation_scope"].update(
        status="source_observed", scope="observed_source_only",
        observed_processes=sorted(set(context.get("observed_processes", ()))),
        transfer_status="CROSS_PROCESS_UNVERIFIED")
    return {"schema_version": 1, "candidate_digest": candidate_digest,
            "changed_paths": list(changed_paths), "items": items,
            "source_evidence": refs,
            "review_flags": flags, "unknowns": unknowns,
            "status": "review_required" if flags else "unknown",
            "assurance": "source_observations_require_evidence_disposition"}
