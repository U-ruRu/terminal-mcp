"""Canonical task DTOs remain transport-independent and bounded by read purpose."""

import pytest
from pydantic import ValidationError

from terminal_mcp.core import task_projections as core

BASE = {
    "namespace": "demo",
    "task_id": "T-1",
    "title": "Task",
    "lane": "implementation",
    "priority": "P1",
    "state": "ready",
    "operational_status": "blocked",
    "revision": 7,
}
SNAPSHOT = {
    **BASE,
    "claim": None,
    "next_action": "Proceed",
    "description_preview": "Current work",
    "description_truncated": False,
    "latest_checkpoint": None,
    "blocking_dependencies": [],
}
EVENT = {
    "id": 1,
    "event_type": "comment",
    "payload": {},
    "created_at": "2026-10-06T00:00:00Z",
}
REVIEW = {
    "dimension": "C",
    "verdict": "NON_BLOCKING",
    "evidence": {},
    "warnings": [],
    "reviewed_at": "2026-10-06T00:00:00Z",
}
OUTPUT = {"output_state_id": 1, "created_at": "2026-10-06T00:00:00Z"}
HISTORY = {"comments", "reviews", "events", "output_states"}


class UnreadableHistory:
    def __iter__(self):
        raise AssertionError("current-state projections must not touch history")


def test_receipt_only_includes_outcome_relevant_fields():
    record = core.TaskRecord.model_validate(
        {
            **BASE,
            "description": "large current body",
            "output_state_id": 31,
            "owner": "logical-owner",
            "archived_at": "2026-10-06T00:00:00Z",
        }
    )
    receipt = core.project_task_receipt(record).model_dump(mode="json", exclude_none=True)
    assert receipt == {
        "namespace": "demo",
        "task_id": "T-1",
        "revision": 7,
        "state": "ready",
        "operational_status": "blocked",
        "owner": "logical-owner",
        "archived": True,
    }


def test_receipt_projects_claim_owner():
    record = core.TaskRecord.model_validate(
        {
            **BASE,
            "owner": {
                "agent_name": "Alpha",
                "claimed_at": "2026-10-06T00:00:00Z",
                "claim_age_seconds": 0,
                "claim_intent": "work",
                "role": "owner",
            },
        }
    )
    assert core.project_task_receipt(record).owner == "Alpha"


def test_working_set_default_reads_only_one_recent_item():
    def newest_first():
        yield core.TaskEvent.model_validate(EVENT)
        raise AssertionError("default working set may read only one comment")

    result = core.project_task_working_set(
        core.TaskSnapshot.model_validate(SNAPSHOT),
        comments=newest_first(),
    )
    assert [event.id for event in result.recent_comments] == [1]


def test_detail_rejects_excessive_byte_size():
    oversized = {**BASE, "description": "Ж" * core.MAX_TASK_PROJECTION_BYTES}
    record = core.TaskRecord.model_construct(**oversized)
    with pytest.raises(ValidationError, match="serialized-byte budget"):
        core.project_task_detail(record)


def test_history_page_bytes_are_bounded_even_with_one_large_event():
    with pytest.raises(ValidationError, match="serialized-byte budget"):
        core.TaskHistory.model_validate(
            {
                "namespace": "demo",
                "task_id": "T-1",
                "kind": "comments",
                "items": [
                    {**EVENT, "payload": {"serialized": "x" * core.MAX_TASK_PROJECTION_BYTES}}
                ],
            }
        )


def test_working_set_bounds_aggregate_comment_payload_bytes():
    event = core.TaskEvent.model_validate(
        {
            **EVENT,
            "payload": {"serialized": "x" * core.MAX_TASK_PROJECTION_BYTES},
        }
    )
    with pytest.raises(ValidationError, match="serialized-byte budget"):
        core.project_task_working_set(core.TaskSnapshot.model_validate(SNAPSHOT), comments=[event])
