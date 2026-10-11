"""Bounded message text slices; offsets are Unicode code points, never bytes."""

from __future__ import annotations

DEFAULT_MESSAGE_TEXT_LIMIT = 2048
MAX_MESSAGE_TEXT_LIMIT = 2048
MAX_MESSAGE_TEXT_OFFSET = 2**31 - 1


def text_slice_arguments(offset: int | None, limit: int | None) -> tuple[int, int]:
    offset = 0 if offset is None else offset
    limit = DEFAULT_MESSAGE_TEXT_LIMIT if limit is None else limit
    if type(offset) is not int or not 0 <= offset <= MAX_MESSAGE_TEXT_OFFSET:
        raise ValueError("text_offset must be a nonnegative integer code-point offset")
    if type(limit) is not int or not 1 <= limit <= MAX_MESSAGE_TEXT_LIMIT:
        raise ValueError("text_limit must be an integer from 1 through 2048")
    return offset, limit


def message_text_slice(item: dict, *, offset: int | None, limit: int | None) -> dict:
    start, size = text_slice_arguments(offset, limit)
    text = item["text"]
    if not isinstance(text, str):
        raise ValueError("stored message text must be a string")
    if start > len(text):
        raise ValueError("text_offset exceeds text_length")
    stop = min(len(text), start + size)
    result = {**item, "text": text[start:stop], "text_offset": start, "text_length": len(text)}
    # Text reads do not embed the sender's potentially unbounded delivery table.
    result.pop("deliveries", None)
    result.pop("next_text_offset", None)
    if stop < len(text):
        result["next_text_offset"] = stop
    return result
