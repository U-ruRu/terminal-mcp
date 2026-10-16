"""Canonical task DTOs remain transport-independent and bounded by read purpose."""

import ast
from pathlib import Path

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


def test_core_has_no_transport_or_application_dependency():
    tree = ast.parse(Path(core.__file__).read_text())
    imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)] + [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ]
    forbidden = ("mcp", "fastapi", "starlette", "terminal_mcp.mcp", "terminal_mcp.application")
    assert not any(name and name.startswith(forbidden) for name in imports)


def test_detail_contains_only_current_state_fields():
    assert HISTORY.isdisjoint(core.TaskDetail.model_fields)
    assert HISTORY.issubset(core.TaskRecord.model_fields)
    assert set(core.TaskDetail.model_fields) == set(core.TaskRecord.model_fields) - HISTORY
    with pytest.raises(ValidationError):
        core.TaskDetail.model_validate({**BASE, "events": []})
    detail = core.TaskDetail.model_validate(BASE)
    assert detail.model_json_schema()["additionalProperties"] is False


class UnreadableHistory:
    def __iter__(self):
        raise AssertionError("current-state projections must not touch history")


@pytest.mark.parametrize("project", [core.project_task_detail, core.project_task_receipt])
def test_current_state_projection_does_not_access_history(project):
    record = core.TaskRecord.model_construct(
        **BASE,
        **dict.fromkeys(HISTORY, UnreadableHistory()),
    )
    result = project(record)
    assert result.revision == 7
    assert result.state == "ready"
    assert result.operational_status == "blocked"
    assert HISTORY.isdisjoint(result.model_dump(mode="json"))


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
    assert core.project_task_receipt(record, output_state_changed=True).output_state_id == 31


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


def test_receipt_rejects_warning_count_overflow():
    record = core.TaskRecord.model_validate(BASE)
    warning = core.WorkflowWarning(code="attention", message="Check result")
    with pytest.raises(ValidationError):
        core.project_task_receipt(record, warnings=(warning for _ in range(101)))


@pytest.mark.parametrize(
    ("kind", "item"),
    [
        ("comments", EVENT),
        ("checkpoints", EVENT),
        ("events", EVENT),
        ("reviews", REVIEW),
        ("output_states", OUTPUT),
    ],
)
def test_history_has_bounded_kind_specific_items_and_opaque_cursor(kind, item):
    data = {
        "namespace": "demo",
        "task_id": "T-1",
        "kind": kind,
        "items": [item] * 100,
        "next_cursor": "opaque-query-bound-cursor",
    }
    page = core.TaskHistory.model_validate(data).model_dump(mode="json")
    assert len(page["items"]) == 100
    assert page["next_cursor"] == data["next_cursor"]
    with pytest.raises(ValidationError):
        core.TaskHistory.model_validate({**data, "items": [item] * 101})
    with pytest.raises(ValidationError):
        core.TaskHistory.model_validate({**data, "backend_only": True})


def test_history_kind_rejects_wrong_record_model():
    with pytest.raises(ValidationError):
        core.TaskHistory.model_validate(
            {
                "namespace": "demo",
                "task_id": "T-1",
                "kind": "reviews",
                "items": [EVENT],
            }
        )


def test_working_set_default_reads_only_one_recent_item():
    def newest_first():
        yield core.TaskEvent.model_validate(EVENT)
        raise AssertionError("default working set may read only one comment")

    result = core.project_task_working_set(
        core.TaskSnapshot.model_validate(SNAPSHOT),
        comments=newest_first(),
    )
    assert [event.id for event in result.recent_comments] == [1]
    assert result.description_preview == SNAPSHOT["description_preview"]
    assert result.operational_status == "blocked"


def test_zero_working_set_limits_do_not_access_history():
    result = core.project_task_working_set(
        core.TaskSnapshot.model_validate(SNAPSHOT),
        comments=UnreadableHistory(),
        checkpoints=UnreadableHistory(),
        comments_limit=0,
        checkpoints_limit=0,
    )
    assert result.recent_comments == result.recent_checkpoints == []


@pytest.mark.parametrize("limit", [-1, 11, True, "1"])
def test_working_set_rejects_invalid_limits(limit):
    with pytest.raises(ValueError):
        core.project_task_working_set(
            core.TaskSnapshot.model_validate(SNAPSHOT), comments_limit=limit
        )


def test_working_set_schema_rejects_more_than_ten_recent_comments():
    with pytest.raises(ValidationError):
        core.TaskWorkingSet.model_validate({**SNAPSHOT, "recent_comments": [EVENT] * 11})


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


def test_receipt_does_not_serialize_huge_body():
    record = core.TaskRecord.model_validate(
        {
            **BASE,
            "description": "x" * (core.MAX_TASK_PROJECTION_BYTES * 2),
        }
    )
    assert core.project_task_receipt(record).revision == 7


def test_receipt_rejects_warning_byte_overflow():
    record = core.TaskRecord.model_validate(BASE)
    warning = core.WorkflowWarning(code="large", message="x" * core.MAX_TASK_PROJECTION_BYTES)
    with pytest.raises(ValidationError, match="serialized-byte budget"):
        core.project_task_receipt(record, warnings=[warning])


def test_working_set_bounds_aggregate_comment_payload_bytes():
    event = core.TaskEvent.model_validate(
        {
            **EVENT,
            "payload": {"serialized": "x" * core.MAX_TASK_PROJECTION_BYTES},
        }
    )
    with pytest.raises(ValidationError, match="serialized-byte budget"):
        core.project_task_working_set(core.TaskSnapshot.model_validate(SNAPSHOT), comments=[event])
