"""Bound current-run research claims to evidence and retain short, fallible memory.

The model supplies interpretations. The controller verifies identities, cited
values and declared contradictions; this is not a proof of algorithm quality.
"""
import math


REVIEW_VERSION = 1
REFLECTION_FIELDS = (
    "observations", "supporting_counterevidence",
    "competing_explanation", "next_discriminating_check",
)
PORTABILITY_FIELDS = (
    "units", "technology", "scale_and_population", "feature_sources",
    "randomness", "failure_behavior", "scope",
)


def research_review_shape():
    return {
        "decision": "evaluate | revise | stop",
        "reason": "Why this experiment is meaningful, needs repair, or should stop.",
        "reflection": {key: "Short current-run observation or hypothesis; mark missing evidence unknown."
                       for key in REFLECTION_FIELDS},
        "measurement_checks": [{
            "evidence_id": "ID from measurement_catalog; [] for a source-only hypothesis.",
            "observed_value": 0.0,
            "definition": "Exact catalog definition.",
            "unit": "Exact catalog unit.",
            "consumer_ref": "checkout-relative/path:line[-line]",
            "consumer_definition": "What the actual algorithm reads, including filtering and denominator.",
            "consumer_unit": "Unit used by the algorithm.",
            "population_match": "same | different | unknown",
            "use": "support | hypothesis | counterevidence",
            "explanation": "Explain the correspondence or its limits using the actual source.",
        }],
        "behavior_check": {
            "status": "observed_change | source_hypothesis | contradicted | unknown",
            "source_refs": ["checkout-relative/path:line[-line]"],
            "evidence_ids": [],
            "summary": "Check the changed decision and its trigger on available inputs; distinguish tests from inference.",
        },
        "cost_plan": {
            "model_rmb": "advisory_work_packet_envelope",
            "local_probe_seconds": 180,
            "compile_and_execute": "Expected compilation/runtime cost or unknown, with reason.",
            "repair_limit": 1,
        },
        "portability": {key: "Current source/input evidence or an explicit unknown."
                        for key in PORTABILITY_FIELDS},
        "review_flags": [{
            "finding": "Exact semantic review finding; [] when none.",
            "disposition": "explained | unknown | confirmed_violation",
            "source_refs": ["checkout-relative/path:line[-line]"],
            "reason": "Source-based explanation; a suspicious token alone is not a violation.",
        }],
    }


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def measurement_catalog(evidence):
    """Expose exact supplied observations; never recalculate an official score."""
    catalog = {}
    rows = [evidence]
    selected = evidence.get("selected_parent_evidence")
    if isinstance(selected, dict):
        rows.append(selected)
    for row in rows:
        if (row.get("task"), row.get("campaign_id")) != (
                evidence.get("task"), evidence.get("campaign_id")):
            raise ValueError("measurement catalog crosses task or campaign")
        for observed in row.get("observations", []):
            identity = observed.get("evidence_id")
            if not identity or not _number(observed.get("value")):
                continue
            contract = observed.get("contract", {})
            catalog[identity] = {
                "value": observed["value"], "definition": contract.get("definition", "unknown"),
                "unit": contract.get("unit", "unknown"),
                "coverage": contract.get("endpoint_coverage", "unknown"),
                "candidate_id": row.get("candidate_id"), "request_id": row.get("request_id"),
                "case": observed.get("case"), "seed": observed.get("seed"),
            }
        rid = row.get("request_id")
        if not rid:
            continue
        for cell in row.get("cells", []):
            for field in ("score", "raw_score", "wns_improvement", "tns_improvement", "overflow_penalty"):
                value = cell.get(field)
                if not _number(value):
                    continue
                identity = "%s:cell:%s:%s#%s" % (rid, cell.get("case"), cell.get("seed"), field)
                catalog[identity] = {
                    "value": value, "definition": "public_feedback.cells." + field,
                    "unit": "official_score" if field in ("score", "raw_score") else "ratio",
                    "coverage": "official_cell",
                    "candidate_id": row.get("candidate_id"), "request_id": rid,
                    "case": cell.get("case"), "seed": cell.get("seed"),
                }
    return catalog


def assess_research(contract, evidence, checkout_root, *, repair_anchor=None, diagnostics=None):
    """Return confirmed record defects for the existing bounded repair path.

    Unknown performance and adverse proxies remain eligible hypotheses. A model
    can explicitly stop or ask to revise; neither action spends an evaluation.
    """
    from .research import verify_source_ref

    result = {"version": REVIEW_VERSION, "errors": [], "decision": "revise",
              "reflection": {}, "measurement_checks": [], "unknowns": [],
              "reflection_subject": {key: evidence.get(key) for key in (
                  "task", "campaign_id", "candidate_id", "request_id", "evidence_role")},
              "assurance": "reference_and_consistency_checks_not_algorithm_quality_proof"}
    errors = result["errors"]
    review = contract.get("research_review")
    if not isinstance(review, dict):
        errors.append("research_review_missing")
        return result
    if set(review) != set(research_review_shape()):
        errors.append("research_review_fields_differ")
        return result
    if review.get("decision") not in ("evaluate", "revise", "stop"):
        errors.append("research_decision_invalid")
    else:
        result["decision"] = review["decision"]
    if not _text(review.get("reason")):
        errors.append("research_reason_missing")
    reflection = review.get("reflection")
    if (not isinstance(reflection, dict) or set(reflection) != set(REFLECTION_FIELDS)
            or not all(_text(reflection.get(key)) for key in REFLECTION_FIELDS)):
        errors.append("research_reflection_requires_four_fields")
    elif sum(len(value) for value in reflection.values()) > 2000:
        errors.append("research_reflection_exceeds_2000_characters")
    else:
        result["reflection"] = dict(reflection)

    portability = review.get("portability")
    if (not isinstance(portability, dict) or set(portability) != set(PORTABILITY_FIELDS)
            or not all(_text(portability.get(key)) for key in PORTABILITY_FIELDS)):
        errors.append("research_portability_requires_seven_fields")
    cost = review.get("cost_plan")
    if (not isinstance(cost, dict)
            or set(cost) != {"model_rmb", "local_probe_seconds", "compile_and_execute", "repair_limit"}
            or cost.get("model_rmb") != "advisory_work_packet_envelope"
            or type(cost.get("local_probe_seconds")) is not int
            or not 0 <= cost["local_probe_seconds"] <= 180
            or type(cost.get("repair_limit")) is not int or cost["repair_limit"] != 1
            or not _text(cost.get("compile_and_execute"))):
        errors.append("research_cost_plan_invalid")

    def source(reference):
        try:
            return verify_source_ref(reference, checkout_root)
        except (OSError, ValueError, TypeError, UnicodeError) as error:
            errors.append("research_source_ref_invalid:" + str(error))
            return None

    catalog = measurement_catalog(evidence)
    measurements = review.get("measurement_checks")
    if not isinstance(measurements, list) or len(measurements) > 32:
        errors.append("research_measurement_checks_invalid")
        measurements = []
    required = set(research_review_shape()["measurement_checks"][0])
    for item in measurements:
        if not isinstance(item, dict) or set(item) != required:
            errors.append("research_measurement_fields_differ")
            continue
        identity = item.get("evidence_id")
        observed = catalog.get(identity) if isinstance(identity, str) else None
        if observed is None:
            errors.append("research_measurement_unbound:" + str(identity))
            continue
        if not _number(item.get("observed_value")) or item["observed_value"] != observed["value"]:
            errors.append("research_measurement_value_differs:" + identity)
        for field in ("unit", "definition"):
            if item.get(field) != observed[field]:
                errors.append("research_measurement_%s_differs:%s" % (field, identity))
        ref = source(item.get("consumer_ref"))
        if not all(_text(item.get(key)) for key in (
                "consumer_definition", "consumer_unit", "explanation")):
            errors.append("research_consumer_definition_missing:" + identity)
        if item.get("population_match") not in ("same", "different", "unknown"):
            errors.append("research_population_status_invalid:" + identity)
        if item.get("use") not in ("support", "hypothesis", "counterevidence"):
            errors.append("research_measurement_use_invalid:" + identity)
        if item.get("use") == "support":
            if (item.get("population_match") != "same" or item.get("unit") == "unknown"
                    or item.get("consumer_unit") != observed["unit"]
                    or observed["definition"] == "unknown"):
                errors.append("research_measurement_support_incompatible:" + identity)
        if item.get("population_match") == "unknown":
            result["unknowns"].append("measurement_population:" + identity)
        result["measurement_checks"].append({
            "evidence_id": identity, "public_measurement": observed,
            "consumer_source": ref, "declared_population_match": item.get("population_match"),
            "use": item.get("use"),
            "limitation": "Consumer meaning and population correspondence remain model interpretations.",
        })

    behavior = review.get("behavior_check")
    if (not isinstance(behavior, dict) or
            set(behavior) != {"status", "source_refs", "evidence_ids", "summary"} or
            behavior.get("status") not in (
                "observed_change", "source_hypothesis", "contradicted", "unknown") or
            not _text(behavior.get("summary")) or not isinstance(behavior.get("source_refs"), list)
            or not behavior["source_refs"] or not isinstance(behavior.get("evidence_ids"), list)):
        errors.append("research_behavior_check_invalid")
    else:
        result["behavior_assessment"] = {
            "claimed_status": behavior["status"], "assurance": "model_reported",
            "current_candidate_behavior_verified": False,
            "limitation": "Source references and prior measurements do not prove current draft execution.",
        }
        for reference in behavior["source_refs"]:
            source(reference)
        for identity in behavior["evidence_ids"]:
            if not isinstance(identity, str) or identity not in catalog:
                errors.append("research_behavior_evidence_unbound:" + str(identity))
        if behavior["status"] == "contradicted" and review["decision"] == "evaluate":
            errors.append("research_behavior_contradicts_proposed_experiment")
        elif behavior["status"] == "observed_change":
            result["unknowns"].append("candidate_behavior_not_independently_verified")
        elif behavior["status"] in ("unknown", "source_hypothesis"):
            result["unknowns"].append("candidate_effect_requires_official_evaluation")

    findings = set((diagnostics or {}).get("warnings", []))
    flags = review.get("review_flags")
    handled = set()
    if not isinstance(flags, list):
        errors.append("research_review_flags_invalid")
        flags = []
    for flag in flags:
        if (not isinstance(flag, dict)
                or set(flag) != {"finding", "disposition", "source_refs", "reason"}
                or not _text(flag.get("finding")) or not _text(flag.get("reason"))
                or flag.get("disposition") not in ("explained", "unknown", "confirmed_violation")
                or not isinstance(flag.get("source_refs"), list) or not flag["source_refs"]):
            errors.append("research_review_flag_invalid")
            continue
        handled.add(flag["finding"])
        for reference in flag["source_refs"]:
            source(reference)
        if flag["disposition"] == "confirmed_violation":
            errors.append("research_confirmed_violation:" + flag["finding"])
        elif flag["disposition"] == "unknown":
            result["unknowns"].append("semantic_review_unresolved:" + flag["finding"])
    for finding in sorted(findings - handled):
        errors.append("research_review_required:" + finding)
    if repair_anchor:
        # A corrected threshold/explanation may change the mechanism text.
        for key in ("contract_id", "edit_stages"):
            if key in repair_anchor and contract.get(key) != repair_anchor[key]:
                errors.append("repair_changed_" + key)
    if review["decision"] == "revise":
        errors.append("research_requested_revision:" + str(review["reason"]))
    return result


def record_research(active, receipt, task, campaign_id):
    contract = active.get("contract") or {}
    review = contract.get("research_review") or {}
    reflection = review.get("reflection") or {}
    assessment = active.get("research_assessment") or {}
    subject = assessment.get("reflection_subject")
    if subject is not None and (subject.get("task"), subject.get("campaign_id")) != (task, campaign_id):
        raise ValueError("reflection subject crosses task or campaign")
    return {
        "version": REVIEW_VERSION, "task": task, "campaign_id": campaign_id,
        "candidate_id": receipt.get("candidate_id"), "request_id": receipt.get("request_id"),
        "parent_candidate_id": active.get("parent_id"), "tree_digest": active.get("tree_digest"),
        "main_mechanism": contract.get("main_mechanism"), "edit_stages": contract.get("edit_stages", []),
        "reflection": {key: reflection[key] for key in REFLECTION_FIELDS if _text(reflection.get(key))},
        "reflection_subject": subject,
        "reflection_subject_status": "bound_to_research_input" if subject else "unknown_legacy_input",
        "falsifier": contract.get("falsifier"), "status": receipt.get("status"),
        "official_score": receipt.get("feedback", {}).get("score"),
        "interpretation_status": "model_hypothesis_not_verified_causality",
        "review": assessment,
    }


def reflection_memory(records, task, campaign_id, limit=3):
    """Return recent failure and success memories without importing other runs."""
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError("reflection memory limit must be between 1 and 10")
    result = []
    for record in records:
        research = record.get("research")
        if not research:
            continue
        receipt = record.get("receipt") or {}
        if (research.get("task"), research.get("campaign_id")) != (task, campaign_id):
            raise ValueError("reflection memory crosses task or campaign")
        if (research.get("candidate_id"), research.get("request_id")) != (
                receipt.get("candidate_id"), receipt.get("request_id")):
            raise ValueError("reflection memory differs from its official receipt")
        subject = research.get("reflection_subject")
        if subject is not None and (subject.get("task"), subject.get("campaign_id")) != (task, campaign_id):
            raise ValueError("reflection subject crosses task or campaign")
        result.append({key: research.get(key) for key in (
            "task", "campaign_id", "candidate_id", "request_id", "main_mechanism",
            "edit_stages", "reflection", "falsifier", "status", "official_score",
            "interpretation_status", "parent_candidate_id",
            "reflection_subject", "reflection_subject_status")})
    return result[-limit:]
