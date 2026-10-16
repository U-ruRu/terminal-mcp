"""Agent identity, public references and bounded command previews."""

import re

from terminal_mcp.core.orchestration import (
    CROCKFORD,
    NATO_WORDS,
    generate_agent_id,
    normalize_preview,
    public_session_ref,
)


def test_public_session_ref_uses_full_identity_without_exposing_suffix():
    started = "2026-09-28T10:00:00Z"
    first = public_session_ref("Alpha-00000000", "server-a", started)
    second = public_session_ref("Alpha-00000001", "server-a", started)

    assert first.startswith("session-")
    assert second.startswith("session-")
    assert first != second
    assert "00000000" not in first
    assert "00000001" not in second


def test_generated_agent_id_has_current_call_sign_and_suffix():
    agent_id = generate_agent_id()
    word, suffix = agent_id.rsplit("-", 1)
    assert word in NATO_WORDS
    assert len(suffix) == 8 and all(ch in CROCKFORD for ch in suffix)
    assert re.fullmatch("[A-Za-z-]+-[0-9A-HJKMNP-TV-Z]{8}", agent_id)


def test_command_preview_normalizes_whitespace():
    assert normalize_preview("  printf   hello\n world  ") == "printf hello world"


def test_command_preview_bounds_length():
    assert len(normalize_preview("x" * 150)) == 100
