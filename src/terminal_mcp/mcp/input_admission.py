"""Coarse JSON transport admission, before authoritative domain Pydantic validation.

This is a circuit breaker for pathological MCP inputs, not a second definition
of field semantics. Normal valid arguments remain governed by existing models.
"""

from __future__ import annotations

from terminal_mcp.core.public_errors import ValidationIssue, ValidationRepair, public_error

MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_INPUT_NESTING = 128
MAX_INPUT_ELEMENTS = 100_000


def _rejected(reason: str, expected: str) -> dict:
    issue = ValidationIssue(
        error_class="input_admission",
        path="$",
        description="Reduce the input size or structural complexity.",
        expected=expected,
    )
    return public_error(
        "input_admission_failed",
        reason=reason,
        path="$",
        details=ValidationRepair(validation_errors=(issue,)),
    ).as_dict()


def admit_arguments(arguments: dict) -> dict | None:
    """Reject excessive transport size/complexity in bounded linear time.

    No untrusted field names, values or exception strings are reflected publicly.
    MCP's object shape is protocol-owned; strict runtime models decide all other
    types, requiredness and nested structures.
    """
    seen = 0
    approx_bytes = 2
    stack = [(arguments, 0)]
    while stack:
        value, depth = stack.pop()
        seen += 1
        if seen > MAX_INPUT_ELEMENTS:
            return _rejected("too_many_elements", f"elements<={MAX_INPUT_ELEMENTS}")
        if depth > MAX_INPUT_NESTING:
            return _rejected("nesting_too_deep", f"depth<={MAX_INPUT_NESTING}")
        if isinstance(value, dict):
            approx_bytes += 2 + len(value) * 4
            for key, child in value.items():
                stack.append((child, depth + 1))
                if isinstance(key, str):
                    approx_bytes += len(key.encode("utf-8", errors="replace"))
        elif isinstance(value, (list, tuple)):
            approx_bytes += 2 + len(value)
            stack.extend((item, depth + 1) for item in value)
        elif isinstance(value, str):
            approx_bytes += len(value.encode("utf-8", errors="replace")) + 2
        else:
            approx_bytes += 32
        if approx_bytes > MAX_INPUT_BYTES:
            return _rejected("payload_too_large", f"bytes<={MAX_INPUT_BYTES}")
    return None
