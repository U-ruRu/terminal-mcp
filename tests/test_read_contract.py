import json

import pytest

from terminal_mcp.core.read_contract import (
    ITEMS_BUDGET_BYTES,
    InvalidCursor,
    OutputItemTooLarge,
    bound_rendered_lines,
    bounded_page,
    decode_cursor,
    encode_cursor,
    summary_message,
)


def test_cursor_round_trip_and_filter_binding():
    scope = {"kind": "tasks", "filters": {"state": "ready"}, "detail": "summary"}
    cursor = encode_cursor(20, scope)
    assert decode_cursor(cursor, scope) == 20
    with pytest.raises(InvalidCursor):
        decode_cursor(cursor, {**scope, "detail": "full"})


@pytest.mark.parametrize("limit", [1, 20, 100])
def test_bounded_page_honors_limit_and_finishes(limit):
    rows = [{"id": i} for i in range(135)]
    scope = {"kind": "collection"}
    page, cursor = bounded_page(rows, limit=limit, cursor=None, scope=scope)
    assert len(page) == limit
    seen = [item["id"] for item in page]
    while cursor is not None:
        page, cursor = bounded_page(rows, limit=limit, cursor=cursor, scope=scope)
        seen.extend(item["id"] for item in page)
    assert seen == list(range(135))
    assert len(seen) == len(set(seen))


def test_bounded_page_dataset_smaller_than_limit():
    rows = [{"id": 1}, {"id": 2}]
    page, cursor = bounded_page(rows, limit=20, cursor=None, scope={"kind": "small"})
    assert page == rows
    assert cursor is None


def test_bounded_page_rejects_invalid_cursor():
    with pytest.raises(InvalidCursor):
        bounded_page([{"id": 1}], limit=1, cursor="not-a-cursor", scope={"kind": "x"})


def test_bounded_page_rejects_oversized_structured_item():
    with pytest.raises(OutputItemTooLarge):
        bounded_page(
            [{"payload": "x" * ITEMS_BUDGET_BYTES}],
            limit=1,
            cursor=None,
            scope={"kind": "large"},
        )


def test_message_summary_bounds_text():
    item = {
        "message_hash": "m1",
        "sender": "Alpha",
        "text": "x" * 5000,
        "created_at": "2026-10-05T00:00:00Z",
    }
    summary = summary_message(item)
    assert summary["message_id"] == "m1"
    assert len(summary["text"]) < 600
    assert summary["truncated"] is True


def test_terminal_line_budget_clips_one_oversized_line():
    lines, truncated = bound_rendered_lines(["x" * (ITEMS_BUDGET_BYTES * 2)])
    assert truncated is True
    assert len(json.dumps(lines).encode()) < ITEMS_BUDGET_BYTES
