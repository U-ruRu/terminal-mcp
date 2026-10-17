"""Issuance failure rolls back its current slot and number claim together."""

import pytest
from test_access_mesh_native_lifecycle import actor
from test_access_mesh_native_lifecycle import fixture as fixture


@pytest.mark.asyncio
async def test_failed_number_commit_rolls_back_slot_and_claim(fixture, monkeypatch):
    original = fixture.store._after_apply

    def fail(db, event, **kwargs):
        original(db, event, **kwargs)
        raise RuntimeError("failure before issuance commit")

    monkeypatch.setattr(fixture.store, "_after_apply", fail)
    with pytest.raises(RuntimeError, match="before issuance commit"):
        await fixture.app.issue(actor("access"), kind="persistent", code="1234")
    assert fixture.store.snapshot_page(issuer_id="firstbyte") == []
    assert fixture.store.numbers.snapshot() == []
