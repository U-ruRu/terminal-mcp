"""Compatibility projections for bounded canonical application reads."""

from terminal_mcp.core.read_contract import bound_rendered_lines, encode_cursor


def _read_error(code: str, reason: str | None = None) -> dict:
    result = {"ok": False, "code": code, "error": code}
    if reason:
        result["reason"] = reason
    return result


def _task_summary(item: dict) -> dict:
    owner = item.get("owner")
    owner_name = owner.get("agent_name") if isinstance(owner, dict) else owner
    checkpoint = item.get("checkpoint")
    return {
        "namespace": item.get("namespace"),
        "task_id": item.get("task_id"),
        "title": item.get("title"),
        "lane": item.get("lane"),
        "priority": item.get("priority"),
        "state": item.get("state"),
        "operational_status": item.get("operational_status"),
        "revision": item.get("revision"),
        "claimed_by": owner_name,
        "blocking_count": len(item.get("blocking_dependencies") or []),
        "has_checkpoint": checkpoint not in (None, "", {}, []),
    }


def _finish_cmd_read_page(result: dict, *, start: int, scope: dict) -> dict:
    raw_lines = list(result.get("lines") or [])
    lines, line_truncated = bound_rendered_lines(raw_lines)
    consumed = len(lines)
    next_offset = start + consumed
    total = result.get("overall_lines_count")
    has_more = bool(len(lines) < len(raw_lines) or (isinstance(total, int) and next_offset < total))
    result["lines"] = lines
    result["displayed_lines_count"] = len(lines)
    result["has_more"] = has_more
    result["next_cursor"] = encode_cursor(next_offset, scope) if has_more else None
    result.pop("next_offset", None)
    if line_truncated:
        result["line_truncated"] = True
    return result
