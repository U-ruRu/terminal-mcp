import pytest
from pydantic import TypeAdapter, ValidationError

from terminal_mcp.mcp.server import build_mcp
from terminal_mcp.mcp.task_contract import TaskRequest, task_request_to_backend

ADAPTER = TypeAdapter(TaskRequest)
ACTIONS = {
    "create",
    "claim",
    "release",
    "update",
    "checkpoint",
    "comment",
    "relate",
    "unrelate",
    "state",
    "done",
    "archive",
    "review",
}


def _base(action):
    return {"action": action, "code": "1234", "namespace": "terminal-mcp", "task_id": "TASK-001"}


MINIMAL_VALID = {
    "create": {
        "action": "create",
        "code": "1234",
        "namespace": "terminal-mcp",
        "isolation_hint": "none",
    },
    "claim": {**_base("claim"), "claim_intent": "Implement contract"},
    "release": {**_base("release"), "release_reason": "Handing work back"},
    "update": _base("update"),
    "checkpoint": {**_base("checkpoint"), "checkpoint": {}},
    "comment": {**_base("comment"), "comment_text": "Investigated implementation."},
    "relate": {**_base("relate"), "relation_kind": "depends_on", "related_task_id": "TASK-002"},
    "unrelate": {**_base("unrelate"), "relation_kind": "depends_on", "related_task_id": "TASK-002"},
    "state": {**_base("state"), "state": "ready"},
    "done": {**_base("done"), "result": {}},
    "archive": {**_base("archive"), "archive_note": "Superseded"},
    "review": {**_base("review"), "dimensions": ["A"], "verdict": "NON_BLOCKING"},
}

FULL_VALID = {
    "create": {
        "action": "create",
        "code": "1234",
        "namespace": "terminal-mcp",
        "task_id": "TASK-CREATE",
        "isolation_hint": "src/mcp task boundary",
        "title": "Implement contract",
        "lane": "implementation",
        "priority": "P1",
        "state": "ready",
        "description": "Description",
        "next_action": "Run tests",
        "resource_context": {"repo": "terminal-mcp"},
        "cooperative": True,
        "checkpoint": {"phase": 1},
        "candidate_ref": "sha:abc",
        "input_refs": ["input:a"],
        "output_refs": ["output:a"],
        "result": {"status": "prepared"},
        "tags": ["mcp", "contract"],
        "dependencies": [{"task_id": "TASK-DEP"}, {"namespace": "other", "task_id": "TASK-X"}],
    },
    "claim": {
        **_base("claim"),
        "claim_intent": "Implement contract",
        "force": True,
        "force_reason": "Dependency is externally satisfied",
        "expected_revision": 3,
    },
    "release": {**_base("release"), "release_reason": "Handing work back", "expected_revision": 4},
    "update": {
        **_base("update"),
        "title": "Updated",
        "lane": "integration",
        "priority": "P0",
        "state": "in_progress",
        "description": "Updated description",
        "next_action": "Integrate",
        "resource_context": {"path": "src"},
        "cooperative": True,
        "candidate_ref": "sha:def",
        "input_refs": ["i"],
        "output_refs": ["o"],
        "tags": ["contract"],
        "dependencies": [{"task_id": "TASK-DEP"}],
        "expected_revision": 5,
    },
    "checkpoint": {**_base("checkpoint"), "checkpoint": ["tested"], "expected_revision": 5},
    "comment": {**_base("comment"), "comment_text": "Investigated current implementation."},
    "relate": {
        **_base("relate"),
        "relation_kind": "review_of",
        "related_namespace": "other",
        "related_task_id": "TASK-002",
        "expected_revision": 5,
    },
    "unrelate": {
        **_base("unrelate"),
        "relation_kind": "review_of",
        "related_namespace": "other",
        "related_task_id": "TASK-002",
        "expected_revision": 6,
    },
    "state": {
        **_base("state"),
        "state": "blocked",
        "blocker_reason": "Waiting for dependency",
        "expected_revision": 6,
    },
    "done": {
        **_base("done"),
        "result": {"ok": True},
        "output_refs": ["sha:abc"],
        "candidate_ref": "sha:abc",
        "expected_revision": 7,
    },
    "archive": {
        **_base("archive"),
        "archive_note": "Superseded",
        "note": "legacy-compatible",
        "expected_revision": 8,
    },
    "review": {
        **_base("review"),
        "dimensions": ["A", "C", "R"],
        "verdict": "BLOCKING",
        "evidence": {"tests": "failed"},
        "expected_revision": 8,
    },
}


@pytest.mark.parametrize("action", sorted(ACTIONS))
def test_each_task_action_accepts_minimal_and_full_request(action):
    minimal = ADAPTER.validate_python(MINIMAL_VALID[action])
    full = ADAPTER.validate_python(FULL_VALID[action])
    assert minimal.action == action
    assert full.action == action


def test_task_schema_is_action_discriminated_and_strict():
    schema = ADAPTER.json_schema()
    assert schema["discriminator"]["propertyName"] == "action"
    assert set(schema["discriminator"]["mapping"]) == ACTIONS
    assert len(schema["oneOf"]) == 12
    for action, ref in schema["discriminator"]["mapping"].items():
        name = ref.rsplit("/", 1)[-1]
        variant = schema["$defs"][name]
        assert variant["additionalProperties"] is False
        assert variant["properties"]["action"]["const"] == action


def test_schema_declares_action_specific_requirements_and_forbidden_fields():
    defs = ADAPTER.json_schema()["$defs"]
    assert defs["TaskCreateRequest"]["required"] == [
        "action",
        "code",
        "namespace",
        "isolation_hint",
    ]
    assert {"action", "code", "namespace", "task_id", "claim_intent"} <= set(
        defs["TaskClaimRequest"]["required"]
    )
    assert {"action", "code", "namespace", "task_id", "release_reason"} <= set(
        defs["TaskReleaseRequest"]["required"]
    )
    assert "isolation_hint" not in defs["TaskUpdateRequest"]["properties"]
    assert "candidate_ref" not in defs["TaskReviewRequest"]["properties"]
    assert defs["TaskReviewRequest"]["properties"]["dimensions"]["maxItems"] == 3
    assert defs["TaskReviewRequest"]["properties"]["dimensions"]["uniqueItems"] is True
    assert defs["TaskClaimRequest"]["properties"]["expected_revision"]["anyOf"][0]["minimum"] == 1


def test_refs_tags_dependencies_and_relation_bounds_are_formalized():
    defs = ADAPTER.json_schema()["$defs"]
    create = defs["TaskCreateRequest"]["properties"]
    assert create["input_refs"]["anyOf"][0]["maxItems"] == 64
    assert create["input_refs"]["anyOf"][0]["items"]["maxLength"] == 512
    assert create["output_refs"]["anyOf"][0]["maxItems"] == 64
    assert create["tags"]["anyOf"][0]["maxItems"] == 50
    assert create["tags"]["anyOf"][0]["items"]["maxLength"] == 64
    assert create["dependencies"]["anyOf"][0]["maxItems"] == 100
    dep = defs["TaskDependency"]
    assert dep["additionalProperties"] is False
    assert dep["required"] == ["task_id"]


def test_create_defaults_and_generated_id_contract():
    request = ADAPTER.validate_python(MINIMAL_VALID["create"])
    assert request.task_id is None
    assert request.lane == "general"
    assert request.priority == "P2"
    assert request.state == "ready"
    explicit = ADAPTER.validate_python({**MINIMAL_VALID["create"], "task_id": "explicit-id"})
    assert explicit.task_id == "explicit-id"


@pytest.mark.parametrize("checkpoint", ["phase-1", {"phase": 1}, ["phase-1"]])
def test_checkpoint_accepts_supported_shapes(checkpoint):
    ADAPTER.validate_python({**_base("checkpoint"), "checkpoint": checkpoint})


@pytest.mark.parametrize("checkpoint", [1, 1.5, True])
def test_checkpoint_rejects_unsupported_scalar(checkpoint):
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({**_base("checkpoint"), "checkpoint": checkpoint})


@pytest.mark.parametrize("state", ["ready", "in_progress", "deferred"])
def test_state_accepts_plain_states(state):
    ADAPTER.validate_python({**_base("state"), "state": state})


def test_state_requires_context_for_blocked_and_done():
    ADAPTER.validate_python({**_base("state"), "state": "blocked", "blocker_reason": "blocked"})
    ADAPTER.validate_python({**_base("state"), "state": "done", "result": {"ok": True}})
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({**_base("state"), "state": "blocked"})
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({**_base("state"), "state": "done"})


@pytest.mark.parametrize("result", ["complete", {"ok": True}, ["artifact"]])
def test_done_accepts_supported_result_shapes(result):
    ADAPTER.validate_python({**_base("done"), "result": result})


def test_archive_accepts_either_note_and_rejects_neither():
    ADAPTER.validate_python({**_base("archive"), "archive_note": "superseded"})
    ADAPTER.validate_python({**_base("archive"), "note": "legacy"})
    ADAPTER.validate_python({**_base("archive"), "archive_note": "a", "note": "b"})
    with pytest.raises(ValidationError):
        ADAPTER.validate_python(_base("archive"))


def test_review_dimensions_are_bounded_unique_and_typed():
    ADAPTER.validate_python({**_base("review"), "dimensions": ["A"], "verdict": "NON_BLOCKING"})
    ADAPTER.validate_python(
        {**_base("review"), "dimensions": ["A", "C", "R"], "verdict": "BLOCKING"}
    )
    for dimensions in (["A", "A"], ["A", "C", "R", "A"], ["X"]):
        with pytest.raises(ValidationError):
            ADAPTER.validate_python(
                {**_base("review"), "dimensions": dimensions, "verdict": "NON_BLOCKING"}
            )
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({**_base("review"), "dimensions": ["A"], "verdict": "UNKNOWN"})


def test_boundary_lengths_and_collection_limits():
    ADAPTER.validate_python({**_base("claim"), "claim_intent": "x" * 160, "expected_revision": 1})
    ADAPTER.validate_python({**_base("release"), "release_reason": "x" * 4000})
    ADAPTER.validate_python({**MINIMAL_VALID["create"], "input_refs": ["r"] * 64})
    ADAPTER.validate_python({**MINIMAL_VALID["create"], "tags": [f"t{i}" for i in range(50)]})
    ADAPTER.validate_python(
        {**MINIMAL_VALID["create"], "dependencies": [{"task_id": f"T{i}"} for i in range(100)]}
    )

    invalid = [
        {**_base("claim"), "claim_intent": "x" * 161},
        {**_base("claim"), "claim_intent": "work", "expected_revision": 0},
        {**_base("release"), "release_reason": "x" * 4001},
        {**MINIMAL_VALID["create"], "input_refs": ["r"] * 65},
        {**MINIMAL_VALID["create"], "input_refs": ["x" * 513]},
        {**MINIMAL_VALID["create"], "tags": [f"t{i}" for i in range(51)]},
        {**MINIMAL_VALID["create"], "tags": ["x" * 65]},
        {**MINIMAL_VALID["create"], "dependencies": [{"task_id": f"T{i}"} for i in range(101)]},
        {**_base("relate"), "relation_kind": "x" * 65, "related_task_id": "TASK-2"},
    ]
    for request in invalid:
        with pytest.raises(ValidationError):
            ADAPTER.validate_python(request)


@pytest.mark.parametrize("action", sorted(ACTIONS))
def test_every_variant_rejects_unknown_fields(action):
    request = {**MINIMAL_VALID[action], "unknown_field": "nope"}
    with pytest.raises(ValidationError):
        ADAPTER.validate_python(request)


def test_wrong_json_types_and_invalid_enums_are_rejected_without_coercion():
    invalid = [
        {**MINIMAL_VALID["claim"], "code": 1234},
        {**MINIMAL_VALID["claim"], "force": "true"},
        {**MINIMAL_VALID["claim"], "expected_revision": "3"},
        {**MINIMAL_VALID["create"], "lane": "unknown"},
        {**MINIMAL_VALID["create"], "priority": "P9"},
        {**_base("state"), "state": "archived"},
    ]
    for request in invalid:
        with pytest.raises(ValidationError):
            ADAPTER.validate_python(request)


def test_required_fields_are_rejected_per_variant():
    required = {
        "create": "isolation_hint",
        "claim": "claim_intent",
        "release": "release_reason",
        "checkpoint": "checkpoint",
        "comment": "comment_text",
        "relate": "related_task_id",
        "unrelate": "relation_kind",
        "state": "state",
        "done": "result",
        "review": "verdict",
    }
    for action, field in required.items():
        request = dict(MINIMAL_VALID[action])
        request.pop(field)
        with pytest.raises(ValidationError):
            ADAPTER.validate_python(request)


def test_regression_free_form_payload_typo_and_review_candidate_are_invalid():
    legacy = {
        "code": "1234",
        "action": "claim",
        "namespace": "example",
        "task_id": "TASK-001",
        "payload": {"anything": "accepted"},
    }
    typo = {
        "action": "claim",
        "code": "1234",
        "namespace": "example",
        "task_id": "TASK-001",
        "claim_intnet": "work",
    }
    review_candidate = {
        "action": "review",
        "code": "1234",
        "namespace": "example",
        "task_id": "TASK-001",
        "dimensions": ["A"],
        "verdict": "NON_BLOCKING",
        "candidate_ref": "sha:abc",
    }
    for request in (legacy, typo, review_candidate):
        with pytest.raises(ValidationError):
            ADAPTER.validate_python(request)


def test_public_request_adapter_preserves_backend_contract():
    request = ADAPTER.validate_python(FULL_VALID["claim"])
    code, namespace, task_id, backend = task_request_to_backend(request)
    assert code == "1234"
    assert namespace == "terminal-mcp"
    assert task_id == "TASK-001"
    assert backend == {
        "action": "claim",
        "claim_intent": "Implement contract",
        "force": True,
        "force_reason": "Dependency is externally satisfied",
        "expected_revision": 3,
    }


class _RecordingBackend:
    def __init__(self):
        self.identity_calls = 0
        self.task_calls = []

    async def access_identity(self, code):
        self.identity_calls += 1
        return {
            "ok": True,
            "logical_agent_id": "logical-1",
            "work_session_id": "session-1",
            "session_epoch": 1,
        }

    async def task(self, **kwargs):
        self.task_calls.append(kwargs)
        return {
            "ok": True,
            "task": {
                "namespace": kwargs["namespace"],
                "task_id": kwargs["task_id"],
                "title": "Task",
                "lane": "implementation",
                "priority": "P1",
                "state": "in_progress",
                "operational_status": "in_progress",
                "revision": 2,
                "isolation_hint": "task/TASK-001",
            },
            "warnings": [],
        }


class _Service:
    def __init__(self, backend):
        self.persistent = backend


@pytest.mark.asyncio
async def test_schema_invalid_task_request_is_rejected_before_backend():
    backend = _RecordingBackend()
    tool = {tool.name: tool for tool in build_mcp(_Service(backend))._tool_manager.list_tools()}[
        "task"
    ]
    with pytest.raises(Exception) as exc_info:
        await tool.run(
            {
                "request": {
                    "action": "claim",
                    "code": "1234",
                    "namespace": "example",
                    "task_id": "TASK-001",
                    "claim_intnet": "work",
                }
            },
            convert_result=True,
        )
    assert backend.identity_calls == 0
    assert backend.task_calls == []
    assert "claim_intent" in str(exc_info.value)
    assert "claim_intnet" in str(exc_info.value)


@pytest.mark.asyncio
async def test_valid_task_request_reaches_backend_through_strict_adapter():
    backend = _RecordingBackend()
    tool = {tool.name: tool for tool in build_mcp(_Service(backend))._tool_manager.list_tools()}[
        "task"
    ]
    await tool.run({"request": MINIMAL_VALID["claim"]}, convert_result=True)
    assert backend.identity_calls == 1
    assert len(backend.task_calls) == 1
    call = backend.task_calls[0]
    assert call["action"] == "claim"
    assert call["namespace"] == "terminal-mcp"
    assert call["task_id"] == "TASK-001"
    assert call["claim_intent"] == "Implement contract"
    assert "payload" not in call
