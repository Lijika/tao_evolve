"""Filesystem binding for the source policy and immutable Agent/input manifests.

All snapshots are JSON serializable. Python source is retained as text for the
existing scoped-AST checker; large physical source files retain only a digest.
These checks detect changes in a managed run; they are not an OS sandbox.
"""
import ast
import hashlib
import json
import os
import stat
from pathlib import Path

from .preflight import check_changes
from .portability_v11 import build_portability_card


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode("utf-8")).hexdigest()


def snapshot_tree(root, retain_python=True, ignore_python_caches=True):
    """Inventory every source file, including existing contained relative links.

    Official source inventory excludes Python caches. Match that convention so
    importing a candidate does not itself become a candidate source edit.
    """
    root = Path(root).absolute()
    if root.is_symlink() or not root.is_dir():
        raise ValueError("snapshot root must be a real directory")
    base = root.resolve()
    entries, sources, errors = {}, {}, []
    for parent, directories, files in os.walk(
            str(base), followlinks=False,
            onerror=lambda exc: errors.append("inventory_walk_error:" + str(exc.filename))):
        directories[:] = sorted(d for d in directories
                                if not ignore_python_caches or d != "__pycache__")
        names = sorted(files + [d for d in directories if (Path(parent) / d).is_symlink()])
        directories[:] = [d for d in directories if not (Path(parent) / d).is_symlink()]
        for name in names:
            if ignore_python_caches and name.endswith((".pyc", ".pyo")):
                continue
            path = Path(parent) / name
            relative = path.relative_to(base).as_posix()
            try:
                info = path.lstat()
                mode = stat.S_IMODE(info.st_mode)
                if stat.S_ISLNK(info.st_mode):
                    target = os.readlink(str(path))
                    entries[relative] = {"kind": "symlink", "target": target, "mode": mode}
                    try:
                        resolved = path.resolve(strict=True)
                        if Path(target).is_absolute() or (resolved != base and base not in resolved.parents):
                            errors.append("unsafe_symlink:" + relative)
                    except (OSError, RuntimeError):
                        errors.append("unsafe_symlink:" + relative)
                elif stat.S_ISREG(info.st_mode):
                    if retain_python and name.endswith(".py"):
                        raw = path.read_bytes()
                        sha = hashlib.sha256(raw).hexdigest()
                        # surrogateescape retains even malformed source for later rejection.
                        sources[relative] = raw.decode("utf-8", errors="surrogateescape")
                    else:
                        sha = file_digest(path)
                    entries[relative] = {"kind": "file", "sha256": sha,
                                         "bytes": info.st_size, "mode": mode}
                else:
                    entries[relative] = {"kind": "special", "mode": mode}
                    errors.append("special_file:" + relative)
            except OSError as exc:
                errors.append("inventory_error:%s:%s" % (relative, type(exc).__name__))
    return {"version": 1, "root": str(base), "entries": entries,
            "python_sources": sources, "errors": errors, "digest": _digest(entries)}


def _review_flags(text):
    """Potential side effects require semantic review, never keyword conviction."""
    flags = {}
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, UnicodeError):
        return flags
    for node in ast.walk(tree):
        found = set()
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = [n.name for n in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            if any(m.split(".")[0] in {"subprocess", "socket", "requests", "urllib", "ctypes"} for m in modules):
                found.add("external_io_or_native_import")
        if isinstance(node, ast.Call):
            name = (node.func.id if isinstance(node.func, ast.Name)
                    else node.func.attr if isinstance(node.func, ast.Attribute) else "")
            if name in {"exec", "eval", "__import__", "system", "popen", "setattr", "putenv"}:
                found.add("dynamic_code_or_side_effect_call")
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Attribute):
                    if target.value.attr in {"environ", "modules", "path"}:
                        found.add("runtime_environment_or_import_mutation")
        for flag in found:
            flags[(flag, ast.dump(node, include_attributes=False))] = node.lineno
    return flags


def check_tree(before, root, policy, original_paths=None, portability_context=None):
    """Check candidate source against the official public allowlist/scoped AST."""
    after = snapshot_tree(root)
    old_entries, entries = before["entries"], after["entries"]
    changed = sorted(name for name in set(old_entries) | set(entries)
                     if old_entries.get(name) != entries.get(name))
    errors = list(before.get("errors", [])) + list(after.get("errors", []))
    warnings, old_data, new_data = [], {}, {}
    for name in changed:
        old, new = old_entries.get(name), entries.get(name)
        if (old and old["kind"] == "symlink") or (new and new["kind"] == "symlink"):
            errors.append("source_symlink_changed:" + name)
        if old and new and old["kind"] != new["kind"]:
            errors.append("file_kind_changed:" + name)
        if old and new and old["mode"] != new["mode"]:
            errors.append("file_mode_changed:" + name)
        if not old and new and new.get("mode", 0) & 0o111:
            errors.append("executable_helper:" + name)
        for snapshot, entry, output in ((before, old, old_data), (after, new, new_data)):
            if entry is not None:
                source = snapshot.get("python_sources", {}).get(name)
                output[name] = (source.encode("utf-8", errors="surrogateescape") if source is not None
                                else json.dumps(entry, sort_keys=True).encode("utf-8"))
        if new and name.endswith(".py"):
            new_text = after["python_sources"].get(name, "")
            old_text = before.get("python_sources", {}).get(name, "")
            old_flags, new_flags = _review_flags(old_text), _review_flags(new_text)
            for flag, node in sorted(set(new_flags) - set(old_flags)):
                warnings.append("semantic_review:%s:%d:%s" % (name, new_flags[(flag, node)], flag))
    bound_policy = dict(policy)
    bound_policy["original_paths"] = list(original_paths if original_paths is not None
                                          else policy.get("original_paths", old_entries))
    errors.extend(check_changes(old_data, new_data, bound_policy))
    card = build_portability_card(before.get("python_sources", {}),
                                  after["python_sources"], changed, after["digest"],
                                  portability_context)
    review = {"status": "review_required" if warnings or card["review_flags"] else "no_static_findings",
              "semantic_warnings": warnings,
              "portability_flag_ids": [flag["id"] for flag in card["review_flags"]],
              "disposition": "unknown", "confirmed_violations": []}
    return {"errors": sorted(set(errors)), "warnings": warnings, "changed_paths": changed,
            "digest": after["digest"], "snapshot": after,
            "portability_card": card, "review": review,
            "assurance": "source_scope_and_integrity_only"}


def freeze_paths(paths):
    """Freeze explicit accessible files/trees, never discover private service state."""
    manifests = {}
    for raw in paths:
        path = Path(raw).absolute()
        if path.is_symlink():
            raise ValueError("immutable root must not be a symlink")
        if path.is_dir():
            snapshot = snapshot_tree(path, retain_python=False, ignore_python_caches=False)
            if snapshot["errors"]:
                raise ValueError("invalid immutable tree: " + str(path))
            manifests[str(path)] = {"kind": "tree", "entries": snapshot["entries"],
                                    "digest": snapshot["digest"]}
        elif path.is_file():
            manifests[str(path)] = {"kind": "file", "sha256": file_digest(path),
                                    "mode": stat.S_IMODE(path.stat().st_mode)}
        else:
            raise ValueError("immutable path missing: " + str(path))
    if not manifests:
        raise ValueError("empty immutable manifest")
    return {"version": 1, "paths": manifests, "digest": _digest(manifests)}


def verify_frozen(snapshot):
    errors = []
    for raw, expected in snapshot["paths"].items():
        try:
            actual = freeze_paths([raw])["paths"][raw]
            if actual != expected:
                errors.append("immutable_changed:" + raw)
        except (OSError, ValueError):
            errors.append("immutable_missing_or_unsafe:" + raw)
    return {"ok": not errors, "errors": errors}
