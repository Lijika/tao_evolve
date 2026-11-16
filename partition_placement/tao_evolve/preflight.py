"""Conservative local preflight. Never a substitute for the official source verifier."""
import ast
from pathlib import PurePosixPath
from typing import Dict, Iterable, Mapping, List


def protected_ast(source: str, names: Iterable[str]) -> str:
    tree = ast.parse(source)
    names = set(names)
    functions = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if any(sum(n.name == name for n in functions) != 1 for name in names):
        raise ValueError("A scoped top-level function must occur exactly once")
    for node in functions:
        if node.name in names:
            node.body = [ast.Pass()]
    return ast.dump(tree, include_attributes=False)


def check_changes(before: Mapping[str, bytes], after: Mapping[str, bytes],
                  policy: Mapping) -> List[str]:
    """Inputs are trusted file snapshots from a filesystem scanner.

    Scanner must separately check modes, symlinks, special files, resource limits
    and baseline inventory. Existing original files cannot be deleted. Helper
    files generated during this campaign may be deleted only under creation_paths.
    """
    errors = []
    allowed = set(policy.get("allowed_paths", []))
    creation = policy.get("creation_paths", {})
    original = set(policy.get("original_paths", before.keys()))
    scope = policy.get("function_scope", {})

    def is_helper(name):
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            return False
        for prefix, suffixes in creation.items():
            prefix = prefix.rstrip("/") + "/"
            if not name.startswith(prefix) or path.suffix not in suffixes:
                continue
            parts = name[len(prefix):].split("/")
            parts[-1] = PurePosixPath(parts[-1]).stem
            return all(x.isascii() and x.isidentifier() and not x.startswith("__")
                       and x not in {"sitecustomize", "usercustomize", "conftest"} for x in parts)
        return False

    for name in sorted(set(before) | set(after)):
        if before.get(name) == after.get(name):
            continue
        if name not in after:
            if name in original or not is_helper(name):
                errors.append("forbidden_delete:" + name)
            continue
        if name not in before:
            if not is_helper(name):
                errors.append("forbidden_create:" + name)
        elif name not in allowed and not is_helper(name):
            errors.append("forbidden_edit:" + name)
        if name.endswith(".py"):
            try:
                text = after[name].decode("utf-8")
                ast.parse(text)
                if name in scope:
                    if name not in before or protected_ast(before[name].decode("utf-8"), scope[name]) != protected_ast(text, scope[name]):
                        errors.append("protected_ast_changed:" + name)
            except (SyntaxError, ValueError, UnicodeError):
                errors.append("python_invalid_or_scoped_function_missing:" + name)
    return errors
