"""Independent process for small candidate contracts; outputs only into scratch.

The parent checks source/input/runtime integrity after execution. This process
uses the existing container's resource/permission boundary, not a new sandbox.
"""
import sys

# Direct script execution would otherwise shadow stdlib types with our types.py.
if __name__ == "__main__" and sys.path:
    del sys.path[0]

import csv
import gzip
import hashlib
import importlib
import json
import math
import os
import random
from pathlib import Path
from types import SimpleNamespace


class InvariantViolation(Exception):
    pass


def require(condition, code):
    if not condition:
        raise InvariantViolation(code)


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def code_binding(module, root):
    file = Path(module.__file__).resolve()
    relative = file.relative_to(root).as_posix()
    helpers = file.parent / "contest_algorithms"
    hashes = {relative: digest(file)}
    if helpers.is_dir():
        for helper in sorted(helpers.rglob("*.py")):
            hashes[helper.relative_to(root).as_posix()] = digest(helper)
    return {"mode": "candidate_code_executed", "module": str(file),
            "source_sha256": hashes, "python_version": sys.version.split()[0]}


def load_candidate(root, module, dreamplace=False):
    base = root / "Place-3D"
    if dreamplace:
        base = base / "thirdparty" / "DREAMPlace"
    sys.path.insert(0, str(base))
    result = importlib.import_module(module)
    if root not in Path(result.__file__).resolve().parents:
        raise ValueError("candidate module resolved outside checkout")
    return result


def a0(root, result, artifact):
    module = load_candidate(root, "src.partition.lol.core")
    result["binding"] = code_binding(module, root)
    result.update(candidate_bound=True, input_current=True, contract_verified=True,
                  input_class="synthetic_fixture")
    names = ["a", "b", "c", "d", "e", "isolated"]
    generator = random.Random(17)
    fixtures = [[], [["a"]], [["a", "a", "b"], ["b", "c"], ["a", "c", "d", "e"]]]
    fixtures.extend([[generator.choices(names[:-1], k=generator.randrange(1, 7))
                      for _ in range(12)] for _ in range(12)])
    checks = 0
    for nets in fixtures:
        for limit in (2, 4, 16):
            modeled, incidence = module.build_unique_nets(nets, max_net_degree=limit)
            require(isinstance(modeled, (list, tuple)) and isinstance(incidence, dict),
                    "partition_net_container_contract")
            expected = {}
            for index, pins in enumerate(modeled):
                require(isinstance(pins, (list, tuple)), "partition_pin_container_contract")
                require(len(pins) == len(set(pins)), "duplicate_pin_in_modeled_net")
                require(all(pin in names for pin in pins), "unknown_pin_in_modeled_net")
                for pin in pins:
                    expected.setdefault(pin, []).append(index)
            require(all(isinstance(values, (list, tuple, set)) for values in incidence.values()),
                    "partition_incidence_container_contract")
            actual = {pin: sorted(values) for pin, values in incidence.items() if values}
            require(actual == expected, "partition_incidence_inconsistent")
            for _ in range(4):
                assignment = {name: generator.choice(("top", "bottom")) for name in names}
                for name in names:
                    for die in ("top", "bottom"):
                        after = dict(assignment)
                        after[name] = die
                        before_cut = sum(len({assignment[p] for p in pins}) > 1 for pins in modeled)
                        after_cut = sum(len({after[p] for p in pins}) > 1 for pins in modeled)
                        original = dict(assignment)
                        delta = module.move_delta_cut(name, die, assignment, modeled, incidence)
                        require(isinstance(delta, (int, float)) and math.isfinite(delta),
                                "partition_cut_delta_nonfinite")
                        require(delta == after_cut - before_cut, "partition_cut_delta_inconsistent")
                        require(assignment == original, "partition_cut_delta_mutates_assignment")
                        checks += 1
    result["metrics"] = {"cut_delta_checks": checks, "net_fixture_count": len(fixtures)}
    result["coverage"] = ["modeled_net_incidence", "move_delta_on_modeled_nets"]


def b0(root, result, artifact):
    import numpy as np

    module = load_candidate(root, "dreamplace.concurrent_lol", dreamplace=True)
    result["binding"] = code_binding(module, root)
    result.update(candidate_bound=True, input_current=True, contract_verified=True,
                  input_class="synthetic_fixture")
    for utilization in (0.0, 0.2, 0.6, 0.95, 1.0):
        for base, growth, ceiling in ((0.55, 1.0, 0.85), (0.8, 1.3, 0.9)):
            density = module.backend_aware_target_density(utilization, base, growth, ceiling)
            require(isinstance(density, (int, float)) and math.isfinite(density)
                    and 0 < density <= 1.0, "density_outside_finite_physical_range")
    assignment = {0: "bottom", 1: "top", 2: "bottom", 3: "top"}
    for multiplier in (0.2, 1.0, 1.4):
        db = SimpleNamespace(net2pin_map=[np.array([0, 1]), np.array([0, 2]),
                                         np.array([1, 3]), np.array([2, 3])],
                             pin2node_map=np.array([0, 1, 2, 3], dtype=np.int32),
                             net_weights=np.array([1.0, 2.0, 0.5, 1.0]))
        count, total = module._apply_cross_die_net_weights(db, assignment, multiplier)
        require(np.isfinite(db.net_weights).all() and (db.net_weights >= 0).all(),
                "net_weights_negative_or_nonfinite")
        require(count == 2 and math.isfinite(total)
                and math.isclose(float(total), float(np.sum(db.net_weights)), rel_tol=1e-6),
                "net_weight_return_summary_inconsistent")
    regions = {"bottom": {"xl": 0.0, "yl": 0.0, "xh": 100.0, "yh": 80.0},
               "top": {"xl": 120.0, "yl": 0.0, "xh": 220.0, "yh": 80.0}}
    initialized = []
    for _ in range(2):
        db = SimpleNamespace(num_movable_nodes=4, node_size_x=np.full(4, 2.0),
                             node_size_y=np.full(4, 3.0), node_x=np.zeros(4), node_y=np.zeros(4))
        count = module._apply_die_aware_initialization(db, assignment, regions, seed=23)
        require(isinstance(count, (int, np.integer)) and 0 <= count <= 4,
                "initialization_count_out_of_range")
        for index, die in assignment.items():
            box = regions[die]
            require(np.isfinite([db.node_x[index], db.node_y[index]]).all(),
                    "initialization_nonfinite")
            require(box["xl"] <= db.node_x[index] <= box["xh"] - db.node_size_x[index]
                    and box["yl"] <= db.node_y[index] <= box["yh"] - db.node_size_y[index],
                    "initialization_outside_assigned_region")
        initialized.append((db.node_x.tolist(), db.node_y.tolist()))
    result["metrics"] = {"density_cases": 10, "weight_cases": 3,
                         "initialization_same_seed_equal": initialized[0] == initialized[1]}
    result["coverage"] = ["density_range", "net_weight_summary", "initialization_region"]
    try:
        import torch
    except ImportError:
        result["warnings"] = ["projection_contract_unknown:torch_unavailable"]
        return
    torch.set_num_threads(min(4, len(os.sched_getaffinity(0))) if hasattr(os, "sched_getaffinity") else 4)
    for fixed in (False, True):
        pins = torch.tensor([1., 122., 3., 124., 5., 6., 7., 8.], requires_grad=True)
        original = pins.detach().clone()
        data = SimpleNamespace(concurrent_lol_projected_pin_x_offset=torch.tensor([0., 120., 0., 120.]))
        if fixed:
            data.concurrent_lol_fixed_hbt = {"fixed_pin_x": torch.tensor([9.]),
                                             "fixed_pin_y": torch.tensor([10.])}
        projected = module.project_wirelength_pin_pos(pins, data)
        expected = ([1., 2., 3., 4., 9., 5., 6., 7., 8., 10.] if fixed
                    else [1., 2., 3., 4., 5., 6., 7., 8.])
        require(tuple(projected.shape) == (len(expected),) and bool(torch.isfinite(projected).all()),
                "projected_pin_shape_or_finiteness")
        require(bool(torch.allclose(projected, torch.tensor(expected))),
                "projected_pin_coordinate_frame_or_fixed_endpoint_changed")
        require(bool(torch.equal(pins.detach(), original)), "projection_mutates_input")
        projected.sum().backward()
        require(pins.grad is not None and bool(torch.isfinite(pins.grad).all()),
                "projection_gradient_missing_or_nonfinite")
        require(bool(torch.allclose(pins.grad, torch.ones_like(pins))),
                "projection_translation_gradient_inconsistent")
    result["binding"]["torch_version"] = torch.__version__
    result["coverage"].append("pin_projection_and_gradient")


def read_json(path):
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(str(path), "rt", encoding="utf-8") as stream:
        return json.load(stream)


def a1(root, result, artifact):
    module = load_candidate(root, "src.partition.lol.core")
    _, instances, nets = module.parse_lol_input(Path(artifact["input_path"]))
    names = {inst.name for inst in instances}
    with Path(artifact["path"]).open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    result.update(candidate_bound=True, input_current=True, contract_verified=True,
                  input_class="current_candidate")
    result["binding"] = {"mode": "candidate_artifact_used", "artifact_sha256": digest(artifact["path"]),
                         "input_sha256": digest(artifact["input_path"]),
                         "candidate_digest": artifact["candidate_digest"]}
    require(all("inst_name" in row and "die" in row for row in rows),
            "assignment_columns_missing")
    assignment = {row["inst_name"]: row["die"] for row in rows}
    if set(assignment) != names and artifact.get("assignment_names") != "internal":
        # Public DEF names may differ from LoL IDs; absent a verified mapping the
        # fixture cannot distinguish renaming from missing instance assignment.
        result["contract_verified"] = False
        raise ValueError("assignment_name_mapping_not_available")
    require(len(rows) == len(assignment) and set(assignment) == names,
            "assignment_instance_coverage")
    require(set(assignment.values()) <= {"top", "bottom"}, "assignment_invalid_die")
    result["metrics"] = {
        "instance_count": len(instances),
        "cut_nets": sum(len({assignment[p] for p in pins if p in assignment}) > 1 for pins in nets),
        "top_area": sum(inst.area for inst in instances if assignment[inst.name] == "top"),
        "bottom_area": sum(inst.area for inst in instances if assignment[inst.name] == "bottom")}
    result["coverage"] = ["instance_coverage", "die_labels", "area_and_cut_observation"]
    result["warnings"] = ["fixed_groups_and_full_partition_constraints_not_checked"]


def c_probe(root, result, artifact):
    import torch
    from dreamplace.ops.rudy import rudy

    data = read_json(artifact["path"])
    if data.get("units") != "dbu" or data.get("coordinate_frame") != "die_local" or data.get("die") not in {"bottom", "top"}:
        raise ValueError("RUDY requires explicit die-local DBU geometry")
    pin_pos = torch.tensor(data["pin_pos"], dtype=torch.float64)
    starts = data["netpin_start"]
    flat = data["flat_netpin"]
    weights = data["net_weights"]
    if (pin_pos.numel() % 2 or not starts or starts[0] != 0 or starts[-1] != len(flat)
            or any(a > b for a, b in zip(starts, starts[1:])) or len(weights) != len(starts) - 1
            or not all(isinstance(i, int) and 0 <= i < pin_pos.numel() // 2 for i in flat)
            or not all(math.isfinite(w) and w >= 0 for w in weights)
            or not bool(torch.isfinite(pin_pos).all())):
        raise ValueError("invalid RUDY pin/net geometry")
    config = {key: data[key] for key in ("xl", "xh", "yl", "yh", "num_bins_x", "num_bins_y",
                                       "unit_horizontal_capacity", "unit_vertical_capacity")}
    if (config["xh"] <= config["xl"] or config["yh"] <= config["yl"]
            or not all(config[k] > 0 for k in ("num_bins_x", "num_bins_y", "unit_horizontal_capacity", "unit_vertical_capacity"))
            or config["num_bins_x"] * config["num_bins_y"] > 1024 * 1024):
        raise ValueError("invalid or excessive RUDY grid/capacity")
    operation = rudy.Rudy(torch.tensor(starts, dtype=torch.int32),
                          torch.tensor(flat, dtype=torch.int32),
                          torch.tensor(weights, dtype=torch.float64),
                          deterministic_flag=1, **config)
    congestion = operation(pin_pos)
    if not bool(torch.isfinite(congestion).all()):
        raise ValueError("RUDY returned non-finite proxy")
    result.update(kind="proxy", candidate_bound=True, input_current=True,
                  contract_verified=False, input_class="current_candidate")
    result["binding"] = {"mode": "candidate_artifact_used", "artifact_sha256": digest(artifact["path"]),
                         "candidate_digest": artifact["candidate_digest"],
                         "engine": str(Path(rudy.__file__).resolve()),
                         "engine_sha256": digest(rudy.__file__),
                         "binary_sha256": digest(rudy.rudy_cpp.__file__),
                         "torch_version": torch.__version__}
    result["metrics"] = {"rudy_max": float(congestion.max()), "rudy_mean": float(congestion.mean()),
                         "rudy_p95": float(torch.quantile(congestion.flatten(), .95))}
    result["coverage"] = ["fixed_cpu_rudy_on_current_candidate_geometry"]


def f_probe(root, result, artifact):
    payload = read_json(artifact["path"])
    grid = payload["site_grid"]
    die = grid["die"]
    width, height = grid["hbt_width"], grid["hbt_height"]
    pitch_x, pitch_y = grid["pitch_x"], grid["pitch_y"]
    spacing_x, spacing_y = grid.get("spacing_x", 0), grid.get("spacing_y", 0)
    # Verify the grid's spacing premise independently of editable candidate classes.
    if (payload.get("schema_version") != 1 or payload.get("units") != "dbu"
            or min(width, height, pitch_x, pitch_y) <= 0 or min(spacing_x, spacing_y) < 0
            or pitch_x < width + spacing_x or pitch_y < height + spacing_y):
        raise ValueError("HBT input grid/units unsupported")
    nets = {row["net_name"]: row for row in payload["nets"]}
    if len(nets) != len(payload["nets"]) or not nets:
        raise ValueError("HBT input net list invalid")
    for row in nets.values():
        if not row["bottom_pins"] or not row["top_pins"]:
            raise ValueError("HBT input cross-die pin coverage invalid")
    module = load_candidate(root, "src.hbt.assignment")
    planner = importlib.import_module("src.hbt.planner")
    problem_path = Path.cwd() / "problem.json"
    if problem_path.is_symlink():
        raise ValueError("unsafe HBT scratch path")
    problem_path.write_text(json.dumps(payload), encoding="utf-8")
    problem, _ = planner.load_problem(problem_path)
    result["binding"] = code_binding(module, root)
    result["binding"]["artifact_sha256"] = digest(artifact["path"])
    result.update(candidate_bound=True, input_current=True, contract_verified=True,
                  input_class="fixed_upstream_isolation")
    algorithm = artifact.get("algorithm", "track-aware-greedy")
    assigned = module.assign_hbts(problem, algorithm=algorithm)
    records = list(assigned.records)
    require(len(records) == len(nets) and {row.net_name for row in records} == set(nets),
            "hbt_net_coverage")
    sites, hpwl = set(), 0
    for row in records:
        x, y = row.legal.x, row.legal.y
        require(isinstance(x, int) and isinstance(y, int), "hbt_noninteger_dbu")
        require((x, y) not in sites, "hbt_duplicate_site")
        sites.add((x, y))
        require(x >= max(die["x_min"], grid["origin_x"])
                and y >= max(die["y_min"], grid["origin_y"])
                and x + width <= die["x_max"] and y + height <= die["y_max"],
                "hbt_outside_die")
        require((x - grid["origin_x"]) % pitch_x == 0 and (y - grid["origin_y"]) % pitch_y == 0,
                "hbt_off_site_grid")
        for blockage in payload.get("blockages", []):
            require(x + width <= blockage["x_min"] or blockage["x_max"] <= x
                    or y + height <= blockage["y_min"] or blockage["y_max"] <= y,
                    "hbt_overlaps_blockage")
        source = nets[row.net_name]
        if algorithm == "fixed":
            require(source.get("fixed") == [x, y], "hbt_fixed_endpoint_moved")
        for tier in ("bottom_pins", "top_pins"):
            xs = [pin[0] for pin in source[tier]] + [x]
            ys = [pin[1] for pin in source[tier]] + [y]
            hpwl += max(xs) - min(xs) + max(ys) - min(ys)
    result["metrics"] = {"assigned_count": len(records), "unassigned_count": 0,
                         "combined_hpwl_dbu": hpwl, "legal_site_count": len(problem.sites),
                         "spacing_verified_from_input_grid": True}
    result["coverage"] = ["net_coverage", "site_grid", "die_bounds", "blockages",
                          "unique_sites_and_grid_spacing"]
    if algorithm == "fixed":
        result["coverage"].append("fixed_endpoints")
    result["warnings"] = ["HBT_subproblem_only_full_chip_connectivity_and_timing_unverified"]


def main():
    request_file, output_file = map(Path, sys.argv[1:3])
    request = json.loads(request_file.read_text(encoding="utf-8"))
    if hasattr(os, "sched_getaffinity"):
        allowed = sorted(os.sched_getaffinity(0))
        os.sched_setaffinity(0, allowed[:max(1, min(4, request.get("cpu_limit", 4)))])
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_FSIZE, (8 * 1024 * 1024, 8 * 1024 * 1024))
    except (ImportError, ValueError, OSError):
        pass
    result = {"probe": request["probe"], "status": "unknown", "kind": "invariant",
              "contract_verified": False, "candidate_bound": False,
              "input_current": False, "reproducible": False, "hard_errors": [],
              "invariant_errors": [], "metrics": {}}
    try:
        {"A0": a0, "B0": b0, "A1": a1, "C": c_probe, "F": f_probe}[request["probe"]](
            Path(request["checkout_root"]).resolve(), result, request.get("artifact"))
        result["status"] = "pass"
        result["reason"] = "tested_contract_only"
    except InvariantViolation as exc:
        result["status"] = "fail"
        result["reason"] = "invariant_violation"
        result["invariant_errors"] = [str(exc)]
    except Exception as exc:
        result["status"] = "unknown"
        result["reason"] = "unavailable_or_incompatible:" + type(exc).__name__
        result["detail"] = str(exc)[:600]
    output_file.write_text(json.dumps(result, sort_keys=True, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    main()
