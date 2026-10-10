"""TMCP-05 RUN-01 acceptance: generated descriptions vs untouched canonical runtime.

No external services or shared DBs. Independent Bravo-3 scripts and evidence remain
unchanged at docs/compact-contracts/qa-tmcp01-bravo3-run01.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from terminal_mcp.mcp import compact_contracts as c
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore
from terminal_mcp.core.tasks import TaskCoordinator

CAT = c.ARTIFACTS
DESCRIPTIONS = {
    role + "." + entry["name"]: entry["description"]
    for role in c.ROLE_NAMES
    for entry in json.loads((CAT / f"catalog-{role}.json").read_text())["tools"]
}


@pytest.mark.parametrize(
    ("tool", "terms"),
    [
        ("coordinator.task_manage", (
            "either non-null archive_note or non-null note",
            "Runtime selects raw archive_note if truthy",
            'archive_note="   " masks valid note fallback',
            "comment_text with at least one non-whitespace character",
            "safe participant fields title, description, next_action, tags",
            "owner_required",
            "archive, relate and unrelate require the live owner",
            "candidate_ref_frozen",
            "incoming review_of",
            "action=update:",
            "action=state:",
            "no content revision CAS check/increment",
        )),
        ("executor.task_claim", (
            "action=claim:", "claim_intent", "non-whitespace",
            "action=release:", "release_reason is optional even when an existing live claim",
            "released_by_owner", "idempotent unclaimed release bypasses reason validation",
        )),
        ("executor.task_comment", (
            "non-whitespace comment_text", "rejects checkpoint",
            "rejects comment_text", "checkpoint does not accept expected_revision",
        )),
        ("executor.task_state", (
            "any admitted agent", "including on a task claimed by someone else",
            "expected_revision does not guard state writes via content CAS",
            "content revision is unchanged", "live claims persist",
        )),
    ],
)
def test_generated_descriptions_cover_independent_qa(tool, terms):
    for term in terms:
        assert term in DESCRIPTIONS[tool], (tool, term)


def test_action_scopes_are_canonical_and_source_anchored():
    notes = c._annotations()
    for roletool, entries in notes.items():
        for rule in entries:
            assert rule["anchor"] in (c.ROOT / rule["source"]).read_text()
            if "action" in rule:
                role, tool = roletool.split(".", 1)
                assert rule["action"] in c.action_models(role, tool)
                assert "action=" + rule["action"] + ":" in DESCRIPTIONS[roletool]
    assert "candidate_ref_frozen" in DESCRIPTIONS["coordinator.task_manage"]


def test_invalid_action_scoped_annotation_fails_before_generation(tmp_path, monkeypatch):
    src = tmp_path / "bad.json"
    src.write_text(json.dumps({"coordinator.task_manage": [{
        "action": "invented_action", "source": "src/terminal_mcp/core/tasks.py",
        "anchor": "async def _action_update(", "text": "invented action",
    }]}))
    monkeypatch.setattr(c, "ANNOTATIONS", src)
    with pytest.raises(ValueError, match="Invalid action-scoped annotation"):
        c._annotations()


@pytest.mark.asyncio
async def test_runtime_nonblank_and_archive_precedence(tmp_path):
    repo = SqliteRepository(tmp_path / "data.sqlite", tmp_path / "output.sqlite")
    await repo.initialize()
    store = TaskStore(repo.path)
    core = TaskCoordinator(store)
    await store.create_task("qa", "archive", "Archive example")
    await store.claim("qa", "archive", "owner", claim_intent="real intent")

    invalid = await core._action_archive("owner", "qa", "archive", note="   ")
    masked = await core._action_archive(
        "owner", "qa", "archive", archive_note="   ", note="valid fallback"
    )
    assert not invalid.get("ok")
    assert not masked.get("ok")
    valid = await core._action_archive("owner", "qa", "archive", note="meaningful")
    assert valid.get("ok") is True

    await store.create_task("qa", "claim", "Claim example")
    bad_claim = await core._action_claim("owner", "qa", "claim", claim_intent="  \t  ")
    assert bad_claim.get("ok") is False
    await store.claim("qa", "claim", "owner", claim_intent="real intent")
    bad_release = await core._action_release("owner", "qa", "claim", release_reason="   ")
    assert bad_release.get("ok") is False
    good_release = await core._action_release("owner", "qa", "claim")
    assert good_release.get("ok") is True

    await store.create_task("qa", "comment", "Comment example")
    bad_comment = await core._action_comment("participant", "qa", "comment", comment_text="   ")
    assert bad_comment.get("ok") is False
    good_comment = await core._action_comment(
        "participant", "qa", "comment", comment_text="meaningful"
    )
    assert good_comment.get("ok") is True


@pytest.mark.asyncio
async def test_runtime_owner_safe_fields_and_state_revision(tmp_path):
    repo = SqliteRepository(tmp_path / "data.sqlite", tmp_path / "output.sqlite")
    await repo.initialize()
    store = TaskStore(repo.path)
    core = TaskCoordinator(store)
    await store.create_task("qa", "source", "Owned example", lane="implementation")
    await store.create_task("qa", "other", "Relation target")
    await store.claim("qa", "source", "owner", claim_intent="owned")
    rev = (await store.get_task("qa", "source"))["revision"]

    safe = await core._action_update(
        "nonowner", "qa", "source", expected_revision=rev, title="Safe participant edit"
    )
    assert safe.get("ok") is True
    rev = (await store.get_task("qa", "source"))["revision"]
    unsafe = await core._action_update(
        "nonowner", "qa", "source", expected_revision=rev, lane="review"
    )
    assert unsafe.get("code") == "owner_required"

    for name, kwargs in [
        ("archive", {"note": "reason"}),
        ("relate", {"relation_kind": "blocks", "related_task_id": "other"}),
        ("unrelate", {"relation_kind": "blocks", "related_task_id": "other"}),
    ]:
        denied = await getattr(core, "_action_" + name)(
            "nonowner", "qa", "source", **kwargs
        )
        assert denied.get("code") == "owner_required", name

    claims_before = await store.active_claims("qa", "source")
    state = await core._action_state(
        "nonowner", "qa", "source", state="blocked", expected_revision=999999
    )
    assert state.get("ok") is True
    after = await store.get_task("qa", "source")
    assert after["state"] == "blocked"
    assert after["revision"] == rev
    assert await store.active_claims("qa", "source") == claims_before


@pytest.mark.asyncio
async def test_runtime_candidate_freeze_after_done_and_incoming_review(tmp_path):
    repo = SqliteRepository(tmp_path / "data.sqlite", tmp_path / "output.sqlite")
    await repo.initialize()
    store = TaskStore(repo.path)
    core = TaskCoordinator(store)
    await store.create_task(
        "qa", "done", "Completed", state="done", result={"evidence": "ok"},
        candidate_ref="candidate-done"
    )
    done_rev = (await store.get_task("qa", "done"))["revision"]
    after_done = await core._action_update(
        "agent", "qa", "done", expected_revision=done_rev, candidate_ref="candidate-done"
    )
    assert after_done.get("code") == "candidate_ref_frozen"

    await store.create_task("qa", "parent", "Parent", candidate_ref="candidate-a")
    await store.create_task(
        "qa", "review", "Review", lane="review", candidate_ref="candidate-a"
    )
    relation = await core._action_relate(
        "reviewer", "qa", "review",
        relation_kind="review_of", related_task_id="parent",
    )
    assert relation.get("ok") is True
    parent_rev = (await store.get_task("qa", "parent"))["revision"]
    with_review = await core._action_update(
        "agent", "qa", "parent", expected_revision=parent_rev,
        candidate_ref="candidate-a",
    )
    assert with_review.get("code") == "candidate_ref_frozen"
