import pytest

from terminal_mcp.core.read_contract import (
    ITEMS_BUDGET_BYTES,
    InvalidCursor,
    OutputItemTooLarge,
    bounded_page,
    decode_cursor,
    encode_cursor,
)


def test_cursor_round_trip_preserves_the_offset():
    scope = {"kind": "tasks", "filters": {"state": "ready"}}

    assert decode_cursor(encode_cursor(20, scope), scope) == 20


def test_cursor_is_bound_to_the_requested_projection():
    scope = {"kind": "tasks", "detail": "summary"}
    cursor = encode_cursor(20, scope)

    with pytest.raises(InvalidCursor):
        decode_cursor(cursor, {**scope, "detail": "full"})


def test_bounded_page_reads_the_final_page_at_the_cursor_offset():
    rows = [{"id": i} for i in range(4)]
    scope = {"kind": "collection"}
    cursor = encode_cursor(2, scope)

    page, next_cursor = bounded_page(rows, limit=2, cursor=cursor, scope=scope)

    assert page == [{"id": 2}, {"id": 3}]
    assert next_cursor is None


def test_bounded_page_rejects_oversized_structured_item():
    with pytest.raises(OutputItemTooLarge):
        bounded_page(
            [{"payload": "x" * ITEMS_BUDGET_BYTES}],
            limit=1,
            cursor=None,
            scope={"kind": "large"},
        )
