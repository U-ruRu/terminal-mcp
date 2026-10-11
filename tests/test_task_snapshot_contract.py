import pytest
from pydantic import ValidationError

from terminal_mcp.mcp.output_contracts import ObserveOutput, TaskOutput, observe_result, task_result
from terminal_mcp.mcp.task_contract import TaskCheckpointRequest

SNAPSHOT = {
    "namespace": "project",
    "task_id": "T-1",
    "title": "Task",
    "lane": "implementation",
    "priority": "P0",
    "state": "ready",
    "operational_status": "ready",
    "revision": 3,
    "claim": None,
    "next_action": "continue",
    "description_preview": "x" * 1500,
    "description_truncated": True,
    "latest_checkpoint": {
        "text": "checkpoint",
        "author": "Alpha",
        "created_at": "2026-10-05T21:00:00Z",
        "revision": 3,
    },
    "blocking_dependencies": [],
}


def test_claim_receipt_is_minimal_and_snapshot_stays_in_observe():
    observed = observe_result(
        {"ok": True, "task": SNAPSHOT, "tasks": [], "next_cursor": None},
        "tasks",
        detail="summary",
        task_id="T-1",
    )
    claimed = task_result({"ok": True, "task": SNAPSHOT, "warnings": []}, "claim")
    assert claimed.structuredContent == {"ok": True}
    assert observed.structuredContent["task"] == SNAPSHOT
    ObserveOutput.model_validate(observed.structuredContent)
    TaskOutput.model_validate(claimed.structuredContent)


def test_checkpoint_text_contract_is_bounded_to_4000_characters():
    TaskCheckpointRequest(
        action="checkpoint",
        code="1234",
        namespace="project",
        task_id="T-1",
        checkpoint="x" * 4000,
    )
    with pytest.raises(ValidationError):
        TaskCheckpointRequest(
            action="checkpoint",
            code="1234",
            namespace="project",
            task_id="T-1",
            checkpoint="x" * 4001,
        )
