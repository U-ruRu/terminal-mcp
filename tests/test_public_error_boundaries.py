from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from terminal_mcp.application.base import canonical_application_result
from terminal_mcp.core.public_errors import public_error
from terminal_mcp.telemetry import observed

SECRET = "secret technical exception text"


def test_application_failure_normalization_drops_legacy_diagnostics():
    result = canonical_application_result(
        {
            "ok": False,
            "code": "policy_incompatible",
            "error": SECRET,
            "diagnostics": {"exception": SECRET},
        }
    )
    assert result == public_error("policy_incompatible").as_dict()
    assert SECRET not in repr(result)


@pytest.mark.asyncio
async def test_http_observed_boundary_maps_unexpected_exception_without_leaking_text():
    async def fail():
        raise RuntimeError(SECRET)

    result = await observed(SimpleNamespace(metrics=None, events=None), "rest", "test", fail())
    assert result == public_error("internal_error").as_dict()
    assert SECRET not in repr(result)


@pytest.mark.asyncio
async def test_http_observed_boundary_does_not_swallow_cancellation():
    async def cancel():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await observed(SimpleNamespace(metrics=None, events=None), "rest", "test", cancel())
