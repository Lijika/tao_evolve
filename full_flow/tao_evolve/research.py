"""Current-campaign evidence and bounded research prompts; no evaluator calls."""
import ast
import gzip
import hashlib
import json
import math
from pathlib import Path
import re

from .context_v11 import compact_packet
from .portfolio_v11 import Cell, MatrixResult, paired_risk
from .task_profiles import get_profile, validate_edit_stages
from .tradeoff_v11 import DependencyGraph, retention_signal


PARSER_VERSION = "tao-evidence-1"
FILE_LIMIT = 512 * 1024
INDEX_LIMIT = 8 * 1024 * 1024
TOTAL_LIMIT = 8 * 1024 * 1024
BASE_STRINGS = (
    "contract_id", "task", "parent_candidate_id", "main_mechanism",
    "tradeoff_statement", "portability_obligation", "final_endpoint", "falsifier",
)
BASE_LISTS = (
    "edit_stages", "source_refs", "impact_refs", "downstream_obligations",
    "physical_invariants", "unknowns",
)
TASK_FIELDS = {
    "partition_placement": (
        "editable_scope", "fixed_downstream_contracts", "post_opt_expected_effect",
        "why_proxy_may_disagree", "coupling_required_or_not",
    ),
    "full_flow": (
        "candidate_intervention_set", "first_observed_divergence",
        "downstream_constraints", "critical_endpoint_migration",
        "endpoint_expected_effect", "alternative_explanation",
    ),
}


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _finite(value):
    return type(value) in (float, int) and math.isfinite(value)


def _read_json(path, limit, compressed=False):
    opener = gzip.open if compressed else open
    with opener(str(path), "rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("public JSON exceeds bounded reader limit")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("public JSON must be an object")
    return value, raw


def _inside(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("relative path escapes evidence/source root")
    path = root / relative
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        raise ValueError("resolved path escapes evidence/source root")
    if path.is_symlink() or not path.is_file():
        raise ValueError("evidence/source reference is not a regular file")
    return resolved


def verify_source_ref(reference, checkout_root):
    """Verify actual source bytes and line range, without claiming causality."""
    if not isinstance(reference, str):
        raise ValueError("source_ref must be a string")
    match = re.fullmatch(r"([^:]+):([1-9][0-9]*)(?:-([1-9][0-9]*))?", reference)
    if match is None:
        raise ValueError("source_ref must be relative/path:line[-line]")
    name, first, last = match.groups()
    first, last = int(first), int(last or first)
    path = _inside(Path(checkout_root), name)
    raw = path.read_bytes()
    lines = raw.decode("utf-8").splitlines()
    if first > last or last > len(lines) or last - first >= 100:
        raise ValueError("source_ref has invalid or excessively broad line range")
    return {"source_ref": reference, "sha256": _sha(raw),
            "first_line": first, "last_line": last,
            "text": "\n".join(lines[first - 1:last])}


def source_dependencies(checkout_root):
    """Discover explicit STAGE_DIRS reads in installed runner method bodies."""
    root = Path(checkout_root)
    relative = "open3dbench/runner.py"
    path = root / relative
    if not path.is_file():
        return {"status": "unknown", "reason": "official_runner_unavailable",
                "nodes": [], "edges": []}
    raw = path.read_bytes()
    try:
        tree = ast.parse(raw.decode("utf-8"))
    except (SyntaxError, UnicodeError) as error:
        return {"status": "unknown", "reason": str(error), "nodes": [], "edges": []}
    stages = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "STAGES"
                for target in node.targets):
            try:
                stages = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                pass
    if not isinstance(stages, list) or not all(isinstance(x, str) for x in stages):
        return {"status": "unknown", "reason": "no_literal_stage_order",
                "nodes": [], "edges": []}
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    edges = []
    aliases = {}
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        consumer = function.name[len("_run_"):] if function.name.startswith("_run_") else ""
        if consumer not in stages:
            continue
        if (consumer == "routing" and
                any(arg.arg == "grt_only" for arg in function.args.kwonlyargs) and
                any(isinstance(node, ast.Constant) and node.value == "--grt-only"
                    for node in ast.walk(function))):
            aliases["grt"] = "routing"
        for node in ast.walk(function):
            if not (isinstance(node, ast.Subscript) and
                    isinstance(node.value, ast.Name) and node.value.id == "STAGE_DIRS"):
                continue
            parent = parents.get(node)
            if not (isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Div) and
                    parent.right is node and isinstance(parent.left, ast.Attribute) and
                    parent.left.attr == "run_dir" and isinstance(parent.left.value, ast.Name) and
                    parent.left.value.id == "self"):
                continue
            key = node.slice
            if isinstance(key, ast.Index):  # Python 3.8
                key = key.value
            try:
                producer = ast.literal_eval(key)
            except (ValueError, TypeError):
                continue
            if producer not in stages or producer == consumer:
                continue
            edges.append({"producer": producer, "consumer": consumer,
                          "kind": "source_stage_directory_reference",
                          "source_ref": "%s:%d" % (relative, node.lineno),
                          "sha256": _sha(raw), "conditional": True})
    unique = {(x["producer"], x["consumer"], x["source_ref"]): x for x in edges}
    edges = [unique[key] for key in sorted(unique)]
    try:
        DependencyGraph((x["producer"], x["consumer"]) for x in edges)
    except ValueError as error:
        return {"status": "unknown", "reason": str(error),
                "nodes": stages, "edges": []}
    return {"status": "source_verified", "nodes": stages, "edges": edges,
            "order": stages, "source_sha256": _sha(raw),
            "task_stage_aliases": aliases,
            "meaning": "Explicit source references; branch activation and causal effects remain unproven."}


def _stage_for_path(path):
    name = str(path).replace("\\", "/")
    if name.startswith("Place-3D/src/partition/"):
        return "partition"
    if name.startswith(("Place-3D/src/placement/", "Place-3D/thirdparty/DREAMPlace/")):
        return "placement"
    if name.startswith("Place-3D/src/hbt/"):
        return "hbt"
    for component, stage in (("rsz", "opt"), ("cts", "cts"), ("grt", "grt")):
        if name.startswith("OpenROAD-3D/openroad/src/%s/" % component):
            return stage
    raise ValueError("changed path has no authorized task-stage binding: " + name)


def contract_shape(task):
    """The v1.1 base schema plus its documented task-specific analysis object."""
    profile = get_profile(task)
    shape = {name: "required nonempty string" for name in BASE_STRINGS}
    shape.update({name: ["required nonempty string"] for name in BASE_LISTS})
    shape.update(task=task, final_endpoint=profile.endpoint, impact_refs=[], unknowns=[])
    shape["task_analysis"] = {name: "required nonempty explanation"
                              for name in TASK_FIELDS[task]}
    if task == "partition_placement":
        shape["task_analysis"]["editable_scope"] = ["partition", "placement"]
    else:
        shape["task_analysis"]["candidate_intervention_set"] = ["selected edit stage"]
    return shape


def validate_model_contract(path, task, checkout_root, changed_paths):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("contract must be a regular file, not a symlink")
    contract, _ = _read_json(path, 64 * 1024)
    expected_fields = set(BASE_STRINGS + BASE_LISTS + ("task_analysis",))
    if set(contract) != expected_fields:
        raise ValueError("contract fields differ: " +
                         str(sorted(set(contract).symmetric_difference(expected_fields))))
    for name in BASE_STRINGS:
        if not isinstance(contract[name], str) or not contract[name].strip():
            raise ValueError("contract requires nonempty " + name)
    for name in BASE_LISTS:
        value = contract[name]
        if (not isinstance(value, list) or
                any(not isinstance(x, str) or not x.strip() for x in value) or
                (name not in ("impact_refs", "unknowns") and not value)):
            raise ValueError("contract requires string array " + name)
        if len(set(value)) != len(value):
            raise ValueError("duplicate contract entries: " + name)
    if contract["task"] != task or contract["final_endpoint"] != get_profile(task).endpoint:
        raise ValueError("contract task or official endpoint differs")
    stages = validate_edit_stages(task, contract["edit_stages"])
    actual = {_stage_for_path(name) for name in changed_paths}
    if actual != set(stages):
        raise ValueError("contract edit_stages differ from actual changed stages")
    analysis = contract["task_analysis"]
    if not isinstance(analysis, dict) or set(analysis) != set(TASK_FIELDS[task]):
        raise ValueError("task_analysis requires every task-specific field")
    scope_key = "editable_scope" if task == "partition_placement" else "candidate_intervention_set"
    expected = {"partition", "placement"} if task == "partition_placement" else set(stages)
    value = analysis[scope_key]
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value) or set(value) != expected:
        raise ValueError("task_analysis scope differs from task/contract")
    for name, value in analysis.items():
        if name != scope_key and (not isinstance(value, str) or not value.strip()):
            raise ValueError("task_analysis requires explanation for " + name)
    refs = [verify_source_ref(ref, checkout_root) for ref in contract["source_refs"]]
    impacts = [verify_source_ref(ref, checkout_root) for ref in contract["impact_refs"]]
    source_stages = set()
    for ref in contract["source_refs"]:
        try:
            source_stages.add(_stage_for_path(ref.rsplit(":", 1)[0]))
        except ValueError:
            pass
    if not set(stages).issubset(source_stages):
        raise ValueError("every edited stage requires its actual algorithm source_ref")
    if len(stages) > 1:
        if not impacts:
            raise ValueError("multi-stage mechanism requires consumer impact_refs")
        graph = source_dependencies(checkout_root)
        verified = []
        for edge in graph["edges"]:
            name, line = edge["source_ref"].rsplit(":", 1)
            for ref in impacts:
                if (ref["source_ref"].rsplit(":", 1)[0] == name and
                        ref["first_line"] <= int(line) <= ref["last_line"]):
                    verified.append(edge)
        if not verified:
            raise ValueError("impact_refs must include a verified consumer dependency read")
        aliases = graph.get("task_stage_aliases", {})
        selected = {aliases.get(stage, stage) for stage in stages}
        dag = DependencyGraph((edge["producer"], edge["consumer"]) for edge in graph["edges"])
        order = graph["order"]
        if not selected.issubset(order):
            raise ValueError("multi-stage mechanism lacks installed stage binding")
        earliest = min(selected, key=order.index)
        downstream = dag.invalidated([earliest])
        if not selected.issubset(downstream) or not any(
                edge["producer"] in downstream and
                (set(dag.invalidated([edge["consumer"]])) & (selected - {earliest}))
                for edge in verified):
            raise ValueError("impact_refs do not support the declared intervention path")
    return {**contract, "verified_source_refs": refs, "verified_impact_refs": impacts}


def _raw_feedback(feedback, campaign_id, task=None):
    if not isinstance(feedback, dict):
        raise ValueError("feedback must be a public result object")
    if feedback.get("campaign_id", feedback.get("campaign", campaign_id)) != campaign_id:
        raise ValueError("cross-campaign feedback is forbidden")
    body = feedback.get("feedback", feedback)
    if not isinstance(body, dict):
        raise ValueError("feedback body must be an object")
    if body.get("campaign_id", body.get("campaign", campaign_id)) != campaign_id:
        raise ValueError("cross-campaign feedback body")
    if task is not None and body.get("task", task) != task:
        raise ValueError("cross-task feedback is forbidden")
    if body.get("split", "train") != "train":
        raise ValueError("only current public training feedback is allowed")
    return body


def _scalars(value, prefix="", depth=0):
    if depth > 4 or not isinstance(value, dict):
        return
    for key in sorted(value):
        item = value[key]
        name = prefix + "." + key if prefix else key
        if _finite(item):
            yield name, item
        elif isinstance(item, dict):
            for child in _scalars(item, name, depth + 1):
                yield child


def _metric_stage(name):
    match = re.search(r"/(?:run/)?[0-9]{2}_(partition|placement|pdn|hbt|opt|cts)/", name)
    if match:
        return match.group(1)
    if "/grt/" in name:
        return "grt"
    if "/analysis/" in name:
        return "endpoint"
    return None


class EvidenceCompiler:
    """Public reply/index in; evidence out. Caller binds campaign provenance."""

    def __init__(self, task, campaign_id, source_root=None, expected_keys=None, task_digest=None):
        get_profile(task)
        if not campaign_id:
            raise ValueError("campaign identity is required")
        self.task = task
        self.campaign_id = campaign_id
        self.source_root = Path(source_root) if source_root else None
        self.expected_keys = set(expected_keys) if expected_keys is not None else None
        self.task_digest = task_digest
        self._source_cache = {}
        installed_framework = Path("/opt/contest/AgenticBench")
        local_framework = self.source_root.parent / "framework" if self.source_root else None
        self.framework_root = (installed_framework if installed_framework.is_dir()
                               else local_framework)

    def _binding(self, relative, symbol, fragments, framework=False):
        """Bind a parser to actual current definition text, never execute source."""
        cache_key = (relative, symbol, tuple(fragments), framework)
        if cache_key in self._source_cache:
            return self._source_cache[cache_key]
        root = self.framework_root if framework else self.source_root
        result = []
        if root is not None:
            try:
                path = _inside(root, relative)
                raw = path.read_bytes()
                text = raw.decode("utf-8")
                tree = ast.parse(text)
                node = next((node for node in tree.body if
                             (isinstance(node, ast.FunctionDef) and node.name == symbol) or
                             (isinstance(node, ast.Assign) and any(
                                 isinstance(target, ast.Name) and target.id == symbol
                                 for target in node.targets))), None)
                segment = ast.get_source_segment(text, node) if node is not None else None
                if segment and all(fragment in segment for fragment in fragments):
                    for fragment in fragments:
                        line = node.lineno + segment[:segment.index(fragment)].count("\n")
                        result.append({"path": str(path), "source_ref": relative + ":" + str(line),
                                       "sha256": _sha(raw), "symbol": symbol})
            except (OSError, ValueError, SyntaxError, UnicodeError):
                pass
        self._source_cache[cache_key] = result
        return result

    def _timing_direction_source(self):
        deficit = self._binding("competition/scoring.py", "deficit",
                                ['return max(0.0, -number(slack, "setup slack"))'], True)
        score = self._binding("competition/scoring.py", "score_cell", [
            "improvements.append((base - deficit(candidate.get(metric))) / denominator)",
            "score = 100 * (0.4 * improvements[0] + 0.6 * improvements[1] - overflow_penalty)",
        ], True)
        return deficit + score if deficit and score else []

    def _known_definition(self, stage, metric, payload, receipt):
        """Small read-only parsers for current official exports, no proxy scorer."""
        if stage == "opt" and metric in ("final_global.wns_setup", "final_global.tns_setup"):
            field = metric.split(".")[-1]
            command = "worst_slack_cmd" if field == "wns_setup" else "total_negative_slack_cmd"
            source = self._binding("open3dbench/backend/three_d_opt.py", "PASSIVE_GLOBAL_STA_TCL", [
                "set_cmd_units -time ns", "read_sdc $::env(OPT_PASSIVE_SDC)",
                "open3d_check_sta_completeness passive_hbt_global 1",
                'puts $metrics "%s\\t[sta::time_sta_ui [sta::%s max]]"' % (field, command),
                'puts $metrics "sta_completeness_checks_passed\\t1"',
            ])
            direction = self._timing_direction_source()
            export = self._binding("open3dbench/backend/three_d_opt.py", "optimize", [
                '"final_global": final_snapshot["metrics"]',
                '"authoritative_timing_view": "continuous-passive-hbt"',
                '"assembly": "continuous-passive-hbt-placement-estimated-rc"',
            ])
            # Runtime output metadata supplies completeness; presence of code alone does not.
            validity = payload.get("timing_validity", {})
            timing = payload.get("final_global", {})
            parasitics = payload.get("parasitics", {})
            complete = (payload.get("status") == "success" and payload.get("passes_run") == 1 and
                        payload.get("iterations_run") == 1 and
                        payload.get("die_passes") == {"bottom": 1, "top": 1} and
                        validity.get("graph_completeness") == "passed" and
                        all(validity.get(name) == 0 for name in (
                            "unclocked_or_unconstrained_endpoints", "combinational_loops",
                            "missing_liberty_cells")) and
                        validity.get("authoritative_timing_view") == "continuous-passive-hbt" and
                        validity.get("global_wns_role") == "pre-cts-placement-estimated" and
                        timing.get("sta_completeness_checks_passed") == 1 and
                        _finite(timing.get("clock_count")) and timing["clock_count"] > 0 and
                        parasitics.get("assembly") == "continuous-passive-hbt-placement-estimated-rc")
            if source and direction and export:
                return {"definition": "continuous-passive-hbt global setup " + field,
                        "unit": "ns", "higher_is_better": True,
                        "view": validity.get("authoritative_timing_view", "unknown"),
                        "clock_model": "source_SDC_pre_CTS",
                        "parasitic_model": parasitics.get("assembly", "unknown"),
                        "coverage_complete": complete,
                        "endpoint_coverage": "all_constrained_setup_endpoints" if complete else "unknown",
                        "definition_refs": source + direction + export}
        if stage == "endpoint" and metric in (
                "timing.wns_setup", "timing.tns_setup",
                "metrics.wns_deficit_ns", "metrics.tns_deficit_ns"):
            field = "wns_setup" if "wns" in metric else "tns_setup"
            command = "worst_slack_cmd" if field == "wns_setup" else "total_negative_slack_cmd"
            source = self._binding("open3dbench/backend/post_route.py", "ASSEMBLY_STA_TCL", [
                "set_cmd_units -time ns", "set_propagated_clock [all_clocks]",
                "read_spef $::env(REPORT_PASSIVE_SPEF)",
                "open3d_check_sta_completeness post_route_assembly 1",
                'puts $metrics "%s\\t[sta::time_sta_ui [sta::%s max]]"' % (field, command),
                'puts $metrics "sta_completeness_checks_passed\\t1"',
            ])
            direction = self._timing_direction_source()
            export = self._binding("grt_bench/endpoint.py", "run", [
                '"wns_deficit_ns": max(0, -finite(timing.get("wns_setup"), "WNS"))',
                '"tns_deficit_ns": max(0, -finite(timing.get("tns_setup"), "TNS"))',
                '"parasitics": "global-route-estimated-rc"',
                '"timing": timing',
            ], True)
            timing = payload.get("timing", {})
            complete = (payload.get("status") == "success" and
                        payload.get("endpoint") == receipt.get("endpoint") and
                        bool(receipt.get("endpoint")) and
                        payload.get("parasitics") == "global-route-estimated-rc" and
                        timing.get("sta_completeness_checks_passed") == 1 and
                        timing.get("hbt_adapter_cell_count") == 0 and
                        _finite(timing.get("clock_count")) and timing["clock_count"] > 0)
            if source and direction and export:
                return {"definition": payload.get("endpoint", "unknown") + ":" + metric,
                        "unit": "ns", "higher_is_better": metric.startswith("timing."),
                        "view": payload.get("endpoint", "unknown"),
                        "clock_model": "propagated_all_clocks",
                        "parasitic_model": payload.get("parasitics", "unknown"),
                        "coverage_complete": complete,
                        "endpoint_coverage": "all_constrained_setup_endpoints" if complete else "unknown",
                        "definition_refs": source + direction + export}
        if stage == "placement" and metric in (
                "total_object_origin_hpwl_um", "cross_die_local_object_origin_hpwl_um"):
            source = self._binding("open3dbench/placement_metrics.py", "_hpwl", [
                "return max(xs) - min(xs) + max(ys) - min(ys)"])
            units = self._binding("open3dbench/placement_metrics.py", "_die_metrics", [
                '"object_origin_hpwl_um": total / dbu',
                '"missing_connection_object_count": missing_references',
                '"empty_net_count": empty'])
            export = self._binding("open3dbench/placement_metrics.py", "evaluate_placement", [
                '"metric": "die-local-object-origin-hpwl"',
                '"scope": "post-legalization-pre-hbt"',
                '"hbt_landing_segment_included": False', '"' + metric + '": sum('])
            dies = payload.get("dies", {})
            complete = (payload.get("metric") == "die-local-object-origin-hpwl" and
                        payload.get("scope") == "post-legalization-pre-hbt" and
                        payload.get("hbt_landing_segment_included") is False and
                        set(dies) == {"bottom", "top"} and
                        all(isinstance(die, dict) and
                            die.get("missing_connection_object_count") == 0 and
                            die.get("empty_net_count") == 0 and
                            _finite(die.get("dbu_per_micron")) and die["dbu_per_micron"] > 0
                            for die in dies.values()) and
                        dies["bottom"]["dbu_per_micron"] == dies["top"]["dbu_per_micron"])
            if metric.startswith("cross_die_"):
                # Counts alone cannot establish cross-die net-set identity between candidates.
                complete = False
            if source and units and export:
                return {"definition": metric + ":sum_die_local_object_origin_bounding_boxes",
                        "unit": "um", "higher_is_better": False,
                        "direction_meaning": "shorter_geometric_length;not_official_reward",
                        "view": payload.get("scope", "unknown"),
                        "clock_model": "not_applicable_geometric",
                        "parasitic_model": "not_applicable_geometric",
                        "coverage_complete": complete,
                        "endpoint_coverage": "all_present_die_local_nets;HBT_segments_excluded"
                        if complete else "unknown",
                        "definition_refs": source + units + export}
        return {}

    def _matrix(self, reply):
        body = _raw_feedback(reply, self.campaign_id, self.task)
        digest = body.get("task_digest", self.task_digest)
        if self.task_digest and digest and digest != self.task_digest:
            raise ValueError("feedback task digest differs")
        cells = []
        diagnostics = []
        keys = []
        for cell in body.get("cells", []):
            if not isinstance(cell, dict) or not isinstance(cell.get("case"), str):
                raise ValueError("malformed public cell")
            if type(cell.get("seed")) is not int:
                raise ValueError("cell seed must be the official integer")
            key = (cell["case"], cell["seed"])
            keys.append(key)
            public_cell = {name: cell.get(name) for name in
                           ("case", "seed", "score", "raw_score", "valid", "reason")}
            for name in ("status", "stage", "failure_stage"):
                if name in cell:
                    public_cell[name] = cell[name]
            if cell.get("diagnostics"):
                diagnostics.append({"case": cell["case"], "seed": cell["seed"],
                                    "diagnostics": cell["diagnostics"]})
                public_cell["diagnostic_sources"] = [
                    row.get("source") for row in cell["diagnostics"] if isinstance(row, dict)]
            cells.append(public_cell)
        unknowns = []
        if len(keys) != len(set(keys)):
            unknowns.append("duplicate_cell_keys")
        expected = self.expected_keys
        if expected is not None:
            for case, seed in sorted(expected - set(keys)):
                cells.append({"case": case, "seed": seed, "score": None,
                              "raw_score": None, "valid": None,
                              "reason": "missing_official_cell"})
            if set(keys) - expected:
                unknowns.append("unexpected_cell_keys")
        else:
            unknowns.append("expected_matrix_keys_not_bound")
        complete = (len(keys) == len(set(keys)) and len(keys) == 9 and
                    len({k[0] for k in keys}) == 3 and
                    all(sum(k[0] == case for k in keys) == 3 for case in {k[0] for k in keys}) and
                    (expected is None or set(keys) == expected) and
                    reply.get("status") == "succeeded" and _finite(body.get("score")) and
                    all(_finite(cell["score"]) for cell in cells))
        if not complete:
            unknowns.append("official_matrix_incomplete_or_unscored")
        if not digest:
            unknowns.append("task_digest_missing")
        return {"campaign_id": self.campaign_id, "task": self.task,
                "candidate_id": reply.get("candidate_id", body.get("candidate_id")),
                "request_id": reply.get("request_id", body.get("request_id")),
                "status": reply.get("status", "unknown"), "reason": reply.get("reason"),
                "official_score": body.get("score"), "task_digest": digest,
                "cells": cells, "matrix_complete": complete, "unknowns": unknowns,
                "failure_diagnostics": diagnostics, "feedback_message": body.get("message"),
                "campaign_binding": "current_controller_supplied_reply",
                "scope": "observed_training_only"}

    def _files(self, reply, summary):
        if not reply.get("files_index") or not reply.get("artifacts_dir"):
            summary["unknowns"].append("public_files_index_unavailable")
            return {}, []
        root = Path(reply["artifacts_dir"]).resolve()
        index_path = Path(reply["files_index"])
        try:
            index_name = index_path.resolve().relative_to(root)
            index, _ = _read_json(_inside(root, index_name), INDEX_LIMIT)
        except (OSError, ValueError, UnicodeError) as error:
            summary["unknowns"].append("files_index_unreadable: " + str(error))
            return {}, []
        selected = {}
        errors = []
        total = 0
        # Receipt identities bind directory labels (s1 etc.) to integer seeds.
        names = sorted(index, key=lambda name: (not name.endswith("/verified/result.json"), name))
        for name in names:
            if not isinstance(name, str) or not name.startswith("cells/"):
                continue
            receipt = name.endswith("/verified/result.json")
            metric = name.endswith("metrics.json") and _metric_stage(name) is not None
            if not (receipt or metric):
                continue
            try:
                record = index[name]
                if not isinstance(record, dict):
                    raise ValueError("malformed index entry")
                size = record.get("source_bytes")
                if type(size) is not int or size < 0:
                    raise ValueError("invalid source_bytes")
                if size > FILE_LIMIT or total + size > TOTAL_LIMIT:
                    errors.append(name + ": bounded reader skipped large evidence")
                    continue
                if record.get("encoding") not in ("identity", "gzip"):
                    raise ValueError("unknown index encoding")
                path = _inside(root, record["path"])
                value, raw = _read_json(path, FILE_LIMIT, record["encoding"] == "gzip")
                total += len(raw)
                if len(raw) != size or _sha(raw) != record.get("source_sha256"):
                    raise ValueError("public artifact hash/size differs from files_index")
                selected[name] = (value, _sha(raw))
            except (OSError, ValueError, KeyError, TypeError, UnicodeError) as error:
                errors.append(name + ": " + str(error))
        return selected, errors

    def _observations(self, files, summary):
        bindings = {}
        for name, (value, _) in files.items():
            if not name.endswith("/verified/result.json"):
                continue
            parts = name.split("/")
            seed = value.get("seed")
            if not isinstance(seed, dict):
                continue
            if (value.get("candidate") == summary["candidate_id"] and
                    value.get("case") == parts[1] and seed.get("id") == parts[2] and
                    type(seed.get("partition")) is int and value.get("split") == "train" and
                    value.get("task_digest") == summary["task_digest"] and
                    value.get("status") == "verified"):
                bindings[(parts[1], parts[2])] = value
        observations = []
        registry = {}
        keys = {(c["case"], c["seed"]) for c in summary["cells"]}
        for name, (value, digest) in files.items():
            stage = _metric_stage(name)
            if stage is None or not name.endswith("metrics.json"):
                continue
            parts = name.split("/")
            receipt = bindings.get((parts[1], parts[2]), {})
            seed = receipt.get("seed", {}).get("partition")
            if seed is None or (parts[1], seed) not in keys:
                summary["unknowns"].append(name + ": case/seed binding unavailable")
                continue
            relative_metric = "/".join(parts[3:])
            metadata = {key: value[key] for key in (
                "metric", "endpoint", "units", "coordinate_unit", "area_unit",
                "timing_validity", "parasitics", "clock_model", "endpoint_universe",
                "coverage_complete", "metric_definitions", "scope",
                "hbt_landing_segment_included") if key in value}
            definitions = value.get("metric_definitions", {})
            for metric, scalar in _scalars(value):
                if metric.startswith("metric_definitions."):
                    continue
                metric_id = stage + ":" + relative_metric + ":" + metric
                definition = definitions.get(metric, {}) if isinstance(definitions, dict) else {}
                if not isinstance(definition, dict):
                    definition = {}
                known = self._known_definition(stage, metric, value, receipt)
                if known:
                    definition = known
                unit = definition.get("unit")
                if unit is None:
                    unit = next((u for u in ("ns", "um", "dbu")
                                 if metric.lower().endswith("_" + u)), "unknown")
                direction = definition.get("higher_is_better")
                if type(direction) is not bool:
                    direction = None
                evidence_id = str(summary["request_id"] or summary["candidate_id"]) + ":" + name + "#" + metric
                contract = {"definition": definition.get("definition", relative_metric + "#" + metric),
                            "unit": unit, "direction": direction,
                            "measurement_metadata": metadata,
                            "environment": summary["task_digest"],
                            "view": definition.get("view", "unknown"),
                            "clock_model": definition.get("clock_model", value.get("clock_model", "unknown")),
                            "parasitic_model": definition.get("parasitic_model", "unknown"),
                            "endpoint_coverage": definition.get("endpoint_coverage",
                                                                value.get("endpoint_universe", "unknown")),
                            "coverage_complete": definition.get("coverage_complete",
                                                                 value.get("coverage_complete") is True),
                            "definition_refs": definition.get("definition_refs", [])}
                registered = registry.setdefault(metric_id, {
                    "stage": stage, "name": metric, "contract_variants": [],
                    "sources": [], "parser_version": PARSER_VERSION,
                    "official_endpoint": stage == "endpoint"})
                if contract not in registered["contract_variants"]:
                    registered["contract_variants"].append(contract)
                registered["sources"].append({"case": parts[1], "seed": seed,
                                               "definition_source": name + "#" + metric,
                                               "source_sha256": digest})
                observations.append({"metric_id": metric_id, "case": parts[1], "seed": seed,
                                     "stage": stage, "value": scalar, "contract": contract,
                                     "source_sha256": digest, "evidence_id": evidence_id})
        return registry, observations

    def compile(self, feedback, parent_feedback=None):
        summary = self._matrix(feedback)
        files, errors = self._files(feedback, summary)
        summary["unknowns"].extend(errors)
        registry, observations = self._observations(files, summary)
        summary.update(metric_registry=registry, observations=observations,
                       parser_version=PARSER_VERSION, tradeoff_ledger=[],
                       endpoint_migration={"status": "unknown",
                                           "reason": "no_verified_endpoint_identity_mapping"},
                       risk={"status": "unknown", "reason": "no_comparable_parent"},
                       source_dependencies=(source_dependencies(self.source_root)
                                            if self.source_root else
                                            {"status": "unknown", "reason": "source_root_unbound",
                                             "nodes": [], "edges": []}))
        if parent_feedback is not None:
            parent = self.compile(parent_feedback)
            self._pair(parent, summary)
        summary["stage_conflict_supported"] = any(
            row["local_final_relation"] in ("local_gain_not_retained", "proxy_regression_final_gain")
            for row in summary["tradeoff_ledger"])
        summary["dependency_evidence_available"] = (
            summary["source_dependencies"]["status"] == "source_verified" and
            bool(summary["source_dependencies"]["edges"]))
        return summary

    def _pair(self, parent, child):
        if parent["task_digest"] != child["task_digest"] or not child["task_digest"]:
            child["unknowns"].append("parent_metric_contract_unbound_or_different")
            return
        if parent["candidate_id"] == child["candidate_id"]:
            child["unknowns"].append("parent_and_child_are_same_candidate")
            return
        if parent["matrix_complete"] and child["matrix_complete"]:
            def matrix(row):
                return MatrixResult(row["campaign_id"], row["task"], row["candidate_id"],
                                    row["status"], row["official_score"], row["task_digest"],
                                    tuple(Cell(c["case"], c["seed"], c["score"], c["valid"])
                                          for c in row["cells"]))
            keys = {(c["case"], c["seed"]) for c in child["cells"]}
            try:
                child["risk"] = paired_risk(matrix(parent), matrix(child), keys)
                child["risk"]["status"] = "compared"
                child["risk"]["negative_cells"] = [
                    {"case": c["case"], "seed": c["seed"],
                     "gain": c["score"] - p["score"]}
                    for c in child["cells"] for p in parent["cells"]
                    if (c["case"], c["seed"]) == (p["case"], p["seed"]) and c["score"] < p["score"]]
            except ValueError as error:
                child["risk"] = {"status": "unknown", "reason": str(error)}
        lookup = {(o["case"], o["seed"], o["metric_id"]): o for o in parent["observations"]}
        final = {(c["case"], c["seed"]): c["score"] for c in child["cells"]}
        previous = {(c["case"], c["seed"]): c["score"] for c in parent["cells"]}
        for observed in child["observations"]:
            key = (observed["case"], observed["seed"], observed["metric_id"])
            old = lookup.get(key)
            if old is None:
                continue
            entry = {"case": key[0], "seed": key[1], "metric_id": key[2],
                     "stage": observed["stage"], "causal_status": "observational",
                     "evidence_ids": [old["evidence_id"], observed["evidence_id"]],
                     "observed_benefit": "unknown", "observed_cost": "unknown",
                     "local_final_relation": "insufficient_evidence",
                     "critical_set_change": "unknown"}
            if old["contract"] != observed["contract"]:
                entry.update(status="incomparable", reason="measurement_contract_mismatch")
            else:
                delta = observed["value"] - old["value"]
                entry.update(status="raw_paired_observation", delta=delta, gain=None)
                direction = observed["contract"]["direction"]
                coverage = observed["contract"]["endpoint_coverage"]
                if (direction is not None and coverage != "unknown" and
                        observed["contract"]["coverage_complete"]):
                    gain = delta * (1 if direction else -1)
                    entry.update(status="compared", gain=gain,
                                 observed_benefit=max(0, gain), observed_cost=max(0, -gain))
                    cell_key = key[:2]
                    if _finite(final.get(cell_key)) and _finite(previous.get(cell_key)):
                        entry["local_final_relation"] = retention_signal(
                            entry, {"status": "compared",
                                    "gain": final[cell_key] - previous[cell_key]})
                else:
                    entry["reason"] = "direction_or_endpoint_coverage_unknown"
            child["tradeoff_ledger"].append(entry)


def summarize_feedback(feedback, campaign_id, parent_feedback=None):
    body = _raw_feedback(feedback, campaign_id)
    task = body.get("task")
    if task is None:
        raise ValueError("feedback task binding is required")
    return EvidenceCompiler(task, campaign_id).compile(feedback, parent_feedback)


def suggest_parent_branches(evidence_rows, baseline_candidate):
    """At most three evaluated/legal roles, without changing final-score order."""
    legal = [row for row in evidence_rows if row.get("matrix_complete") and
             all(c.get("valid") is True for c in row["cells"]) and
             _finite(row.get("official_score"))]
    if legal:
        identity = (legal[0]["campaign_id"], legal[0]["task"], legal[0]["task_digest"])
        if any((r["campaign_id"], r["task"], r["task_digest"]) != identity for r in legal):
            raise ValueError("parent portfolio mixes campaign/task/definition")
    roles = []
    if legal:
        champion = max(legal, key=lambda row: row["official_score"])
        roles.append({"role": "legal_champion", "candidate_id": champion["candidate_id"]})
        others = [row for row in legal if row["candidate_id"] != champion["candidate_id"]]
        if others:
            def stability(row):
                cases = {}
                for cell in row["cells"]:
                    cases.setdefault(cell["case"], []).append(cell["score"])
                return min(sum(values) / len(values) for values in cases.values())
            alternate = max(others, key=stability)
            roles.append({"role": "case_balanced_alternative",
                          "candidate_id": alternate["candidate_id"],
                          "scope": "observed_training_only"})
    if baseline_candidate and baseline_candidate not in {r["candidate_id"] for r in roles}:
        roles.append({"role": "official_baseline", "candidate_id": baseline_candidate})
    return roles[:3]


def build_prompt(task, checkout, source_policy, state, evidence, contract_path,
                 mode, repair_errors=None, compact=False):
    profile = get_profile(task)
    checkout = checkout if isinstance(checkout, dict) else {"path": str(checkout)}
    if not checkout.get("path"):
        raise ValueError("current checkout path is required")
    campaign = state.get("campaign_id", state.get("campaign"))
    if evidence and (evidence.get("task") != task or
                     (campaign and evidence.get("campaign_id") != campaign)):
        raise ValueError("prompt evidence task/campaign differs")
    destination = Path(contract_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    policy_path = destination.parent / (destination.stem + "-source-policy.json")
    evidence_path = destination.parent / (destination.stem + "-evidence.json")
    policy_raw = _dump(source_policy).encode("utf-8")
    evidence_raw = _dump(evidence or {}).encode("utf-8")
    policy_path.write_bytes(policy_raw)
    evidence_path.write_bytes(evidence_raw)
    folder = Path(__file__).resolve().parent.parent / "prompts"
    names = ["00_initial.txt", "01_plan_" + task + ".txt", "02_edit.txt",
             "04_reflect_" + task + ".txt"]
    if repair_errors:
        names.append("03_repair.txt")
    instructions = "\n".join((folder / name).read_text(encoding="utf-8") for name in names)
    mandatory = {
        "instructions": instructions,
        "reflection_binding": {
            "evidence_role": (evidence or {}).get("evidence_role", "selected_parent"),
            "evidence_candidate_id": (evidence or {}).get("candidate_id"),
            "evidence_request_id": (evidence or {}).get("request_id"),
            "selected_parent_candidate_id": state.get("parent_candidate_id", checkout.get("parent")),
            "instruction": "Reflect on the latest experiment, including failures and lower scores. "
                           "Use the selected parent checkout as the edit base. Read latest_contract and "
                           "selected_parent_evidence in the evidence file when present.",
        },
        "rule_packet": {"task": task, "endpoint": profile.endpoint,
                        "editable_stages": sorted(profile.editable_stages),
                        "source_policy_file": str(policy_path.resolve()),
                        "source_policy_sha256": _sha(policy_raw),
                        "expert_knowledge_text": ""},
        "work_packet": {"campaign_id": campaign, "checkout": checkout,
                        "parent_candidate_id": state.get("parent_candidate_id", checkout.get("parent")),
                        "mode": mode, "contract_path": str(destination.resolve()),
                        "budget": {key: state[key] for key in (
                            "evaluations_remaining", "model_cost_available_rmb",
                            "model_budget_mode", "unresolved_model_calls",
                            "budget_envelope_rmb", "budget_mode",
                            "remaining_agent_submissions") if key in state}},
        "action_contract_schema": contract_shape(task),
        "evidence_pointer": {"path": str(evidence_path.resolve()), "sha256": _sha(evidence_raw)},
        "official_score": (evidence or {}).get("official_score"),
        "all_cells": (evidence or {}).get("cells", []),
        "selected_parent_cells": (evidence or {}).get("selected_parent_evidence", {}).get("cells", []),
        "failures_and_unknowns": {"reason": (evidence or {}).get("reason"),
                                  "feedback_message": (evidence or {}).get("feedback_message"),
                                  "unknowns": (evidence or {}).get("unknowns", []),
                                  "repair_errors": repair_errors or []},
    }
    optional = [(name, evidence[name]) for name in (
        "failure_diagnostics", "risk", "tradeoff_ledger", "source_dependencies", "metric_registry",
        "observations", "endpoint_migration", "latest_contract",
        "selected_parent_evidence") if evidence and evidence.get(name)]
    # The controller adds its execution restrictions after this packet.
    return compact_packet(mandatory, optional, (10 * 1024 if compact else 16 * 1024) - 1024)
