"""Non-invasive source/client compatibility observations, not an automatic RPC fallback."""
import inspect
from typing import Mapping


def request_id_supported(method) -> bool:
    try:
        p = inspect.signature(method).parameters.get("request_id")
    except (ValueError, TypeError):
        return False
    return p is not None and p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)


def inspect_context(context) -> Mapping:
    result = {}
    for name in ("status", "checkout", "submit_evaluation", "wait_evaluation", "codex_turn"):
        fn = getattr(context, name, None)
        result[name] = {"callable": callable(fn), "request_id_keyword": request_id_supported(fn) if callable(fn) else False}
    result["warning"] = "Inspection alone is not a verified binding. Do not invoke private RPC or retry with a new ID."
    return result
