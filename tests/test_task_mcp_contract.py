import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError

from terminal_mcp.mcp.server import build_mcp
from terminal_mcp.mcp.task_contract import (
    TaskRequest,
    TaskToolArguments,
    task_request_to_backend,
)

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
    "update": {**_base("update"), "expected_revision": 1},
    "checkpoint": {**_base("checkpoint"), "checkpoint": {}},
    "comment": {**_base("comment"), "comment_text": "Investigated implementation."},
    "relate": {**_base("relate"), "relation_kind": "depends_on", "related_task_id": "TASK-002"},
    "unrelate": {**_base("unrelate"), "relation_kind": "depends_on", "related_task_id": "TASK-002"},
    "state": {**_base("state"), "state": "ready"},
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
        "force": True,
        "force_reason": "External completion state is authoritative",
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
        "description": "Updated description",
        "next_action": "Integrate",
        "resource_context": {"path": "src"},
        "cooperative": True,
        "candidate_ref": "sha:def",
        "input_refs": ["i"],
        "output_refs": ["o"],
        "tags": ["contract"],
        "dependencies": [{"task_id": "TASK-DEP"}],
        # Checkpoint is a distinct append-only action, not a content update.
        "result": {"summary": "updated result"},
        "blocker_reason": "Dependency temporarily unavailable",
        "force": True,
        "force_reason": "External dependency state is authoritative",
        "expected_revision": 5,
    },
    "checkpoint": {**_base("checkpoint"), "checkpoint": ["tested"]},
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
    "state": {**_base("state"), "state": "qa"},
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
    assert len(schema["oneOf"]) == len(ACTIONS) == 11
    for action, ref in schema["discriminator"]["mapping"].items():
        name = ref.rsplit("/", 1)[-1]
        variant = schema["$defs"][name]
        assert variant["additionalProperties"] is False
        assert variant["properties"]["action"]["const"] == action


def test_schema_declares_action_specific_requirements_and_forbidden_fields():
    defs = ADAPTER.json_schema()["$defs"]
    assert defs["TaskCreateRequest"]["required"] == [
        "action",
        "namespace",
        "isolation_hint",
    ]
    assert {"action", "namespace", "task_id", "claim_intent"} <= set(
        defs["TaskClaimRequest"]["required"]
    )
    assert "code" not in defs["TaskClaimRequest"]["required"]
    release = defs["TaskReleaseRequest"]
    assert {"action", "namespace", "task_id"} <= set(release["required"])
    assert "code" not in release["required"]
    assert "release_reason" not in release["required"]
    assert "release_reason" in release["properties"]
    update_props = defs["TaskUpdateRequest"]["properties"]
    assert "isolation_hint" not in update_props
    assert "checkpoint" not in update_props
    assert {"result", "blocker_reason", "force", "force_reason"} <= set(update_props)
    assert {"force", "force_reason"} <= set(defs["TaskCreateRequest"]["properties"])
    assert "TaskDoneRequest" not in defs
    assert set(defs["TaskStateRequest"]["properties"]) == {
        "action",
        "code",
        "namespace",
        "task_id",
        "state",
    }
    assert "request_id" in defs["TaskCreateRequest"]["properties"]
    assert "request_id" not in defs["TaskCreateRequest"]["required"]
    for action in ("TaskStateRequest", "TaskCheckpointRequest", "TaskCommentRequest"):
        assert "request_id" not in defs[action]["properties"]
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


def test_state_context_has_only_minimal_workflow_fields():
    for state in ("ready", "in_progress", "qa", "blocked", "deferred", "done"):
        ADAPTER.validate_python({**_base("state"), "state": state})
    for key, value in (
        ("result", {}),
        ("blocker_reason", "waiting"),
        ("expected_revision", 1),
        ("force", True),
        ("force_reason", "override"),
    ):
        with pytest.raises(ValidationError):
            ADAPTER.validate_python({**_base("state"), "state": "done", key: value})


def test_done_action_is_not_public():
    for result in ("complete", {"ok": True}, ["artifact"]):
        with pytest.raises(ValidationError):
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
    ADAPTER.validate_python({**MINIMAL_VALID["update"], "title": "Changed"})
    ADAPTER.validate_python({**MINIMAL_VALID["create"], "input_refs": ["r"] * 64})
    ADAPTER.validate_python({**MINIMAL_VALID["create"], "tags": [f"t{i}" for i in range(50)]})
    ADAPTER.validate_python(
        {**MINIMAL_VALID["create"], "dependencies": [{"task_id": f"T{i}"} for i in range(100)]}
    )

    invalid = [
        {**_base("claim"), "claim_intent": "x" * 161},
        {**_base("claim"), "claim_intent": "work", "expected_revision": 0},
        {**_base("release"), "release_reason": "x" * 4001},
        {**MINIMAL_VALID["update"], "expected_revision": 0},
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
        "checkpoint": "checkpoint",
        "comment": "comment_text",
        "relate": "related_task_id",
        "unrelate": "relation_kind",
        "state": "state",
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


def test_backend_contract_accepts_server_resolved_access_code():
    payload = dict(FULL_VALID["claim"])
    payload.pop("code")
    request = ADAPTER.validate_python(payload)
    code, namespace, task_id, backend = task_request_to_backend(request)
    assert code is None
    assert namespace == "terminal-mcp"
    assert task_id == "TASK-001"
    assert backend["action"] == "claim"


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
        task = {
            "namespace": kwargs["namespace"],
            "task_id": kwargs["task_id"],
            "title": "Task",
            "lane": "implementation",
            "priority": "P1",
            "state": "in_progress",
            "operational_status": "in_progress",
            "revision": 2,
            "isolation_hint": "task/TASK-001",
        }
        if kwargs["action"] == "claim":
            task = {
                **{
                    key: task[key]
                    for key in (
                        "namespace",
                        "task_id",
                        "title",
                        "lane",
                        "priority",
                        "state",
                        "operational_status",
                        "revision",
                    )
                },
                "claim": None,
                "next_action": "",
                "description_preview": "",
                "description_truncated": False,
                "latest_checkpoint": None,
                "blocking_dependencies": [],
            }
        return {"ok": True, "task": task, "warnings": []}


class _Service:
    def __init__(self, backend):
        self.persistent = backend


@pytest.mark.asyncio
async def test_schema_invalid_task_request_is_rejected_before_backend():
    backend = _RecordingBackend()
    tool = {tool.name: tool for tool in build_mcp(_Service(backend))._tool_manager.list_tools()}[
        "task"
    ]
    result = await tool.run(
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
    assert result.structuredContent["code"] == "validation_error"
    errors = result.structuredContent["details"]["validation_errors"]
    assert errors == [
        {
            "error_class": "missing",
            "path": "request.claim_intent",
            "description": "Provide this required field.",
        },
        {
            "error_class": "extra_forbidden",
            "path": "request.*",
            "description": "Remove this unsupported field.",
        },
    ]


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_request", "expected"),
    [
        (
            {
                "action": "create",
                "code": "1234",
                "namespace": "terminal-mcp",
                "task_id": "CREATE-DONE-FORCED",
                "isolation_hint": "none",
                "state": "done",
                "result": {"summary": "forced creation"},
                "dependencies": [{"task_id": "OPEN"}],
                "force": True,
                "force_reason": "External completion must be represented immediately",
            },
            {
                "action": "create",
                "state": "done",
                "result": {"summary": "forced creation"},
                "force": True,
                "force_reason": "External completion must be represented immediately",
            },
        ),
        (
            {**_base("state"), "state": "done"},
            {"action": "state", "state": "done"},
        ),
        (
            {**_base("state"), "state": "qa"},
            {"action": "state", "state": "qa"},
        ),
        (
            {**_base("update"), "title": "updated", "expected_revision": 5},
            {"action": "update", "title": "updated", "expected_revision": 5},
        ),
        (
            {**_base("checkpoint"), "checkpoint": {"phase": "validated"}},
            {"action": "checkpoint", "checkpoint": {"phase": "validated"}},
        ),
        (
            {**_base("state"), "state": "blocked"},
            {"action": "state", "state": "blocked"},
        ),
    ],
)
async def test_public_boundary_preserves_backend_supported_completion_fields(
    task_request, expected
):
    backend = _RecordingBackend()
    mcp = build_mcp(_Service(backend))
    tool = {tool.name: tool for tool in mcp._tool_manager.list_tools()}["task"]
    validator = Draft202012Validator(tool.parameters["x-runtime-schema"])
    arguments = {"request": task_request}
    assert not list(validator.iter_errors(arguments))

    await tool.run(arguments, convert_result=True)
    assert backend.identity_calls == 1
    assert len(backend.task_calls) == 1
    call = backend.task_calls[0]
    for key, value in expected.items():
        assert call[key] == value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "task_request",
    [
        {**_base("state"), "state": "done"},
        {**_base("state"), "state": "blocked"},
        _base("release"),
    ],
)
async def test_public_boundary_defers_dynamic_state_and_claim_requirements_to_backend(task_request):
    backend = _RecordingBackend()
    mcp = build_mcp(_Service(backend))
    tool = {tool.name: tool for tool in mcp._tool_manager.list_tools()}["task"]
    validator = Draft202012Validator(tool.parameters["x-runtime-schema"])
    arguments = {"request": task_request}
    assert not list(validator.iter_errors(arguments))

    await tool.run(arguments, convert_result=True)
    assert backend.identity_calls == 1
    assert len(backend.task_calls) == 1
    call = backend.task_calls[0]
    assert call["action"] == task_request["action"]
    if task_request["action"] == "state":
        assert call["state"] == task_request["state"]
        assert "result" not in call
        assert "blocker_reason" not in call
    else:
        assert "release_reason" not in call


def test_generated_discovery_schema_matches_runtime_conditionals():
    backend = _RecordingBackend()
    tool = {tool.name: tool for tool in build_mcp(_Service(backend))._tool_manager.list_tools()}[
        "task"
    ]
    schema = tool.parameters["x-runtime-schema"]
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    invalid = [
        {
            "request": {
                "action": "create",
                "code": "1234",
                "namespace": "example",
                "isolation_hint": "none",
                "state": "done",
                "result": None,
            }
        },
        {"request": {**_base("archive"), "archive_note": None}},
    ]
    for payload in invalid:
        assert list(validator.iter_errors(payload)), payload
        with pytest.raises(ValidationError):
            ADAPTER.validate_python(payload["request"])

    valid = [
        {
            "request": {
                "action": "create",
                "code": "1234",
                "namespace": "example",
                "isolation_hint": "none",
                "state": "done",
                "result": {"ok": True},
            }
        },
        {"request": {**_base("state"), "state": "blocked"}},
        {"request": {**_base("state"), "state": "qa"}},
        {"request": {**_base("state"), "state": "done"}},
        {"request": _base("release")},
        {"request": {**_base("archive"), "note": "legacy-compatible"}},
    ]
    for payload in valid:
        assert not list(validator.iter_errors(payload)), payload
        ADAPTER.validate_python(payload["request"])


def test_generated_discovery_accepts_all_minimal_and_full_variants():
    backend = _RecordingBackend()
    tool = {tool.name: tool for tool in build_mcp(_Service(backend))._tool_manager.list_tools()}[
        "task"
    ]
    validator = Draft202012Validator(tool.parameters["x-runtime-schema"])
    for action in ACTIONS:
        assert not list(validator.iter_errors({"request": MINIMAL_VALID[action]})), action
        assert not list(validator.iter_errors({"request": FULL_VALID[action]})), action


def test_generated_checkpoint_schema_uses_one_of():
    backend = _RecordingBackend()
    tool = {tool.name: tool for tool in build_mcp(_Service(backend))._tool_manager.list_tools()}[
        "task"
    ]
    checkpoint = tool.parameters["x-runtime-schema"]["$defs"]["TaskCheckpointRequest"][
        "properties"
    ]["checkpoint"]
    assert "anyOf" not in checkpoint
    assert checkpoint["oneOf"] == [
        {"type": "string", "maxLength": 4000},
        {"type": "object"},
        {"type": "array"},
    ]


def test_generated_done_schema_is_absent():
    tool = {
        tool.name: tool
        for tool in build_mcp(_Service(_RecordingBackend()))._tool_manager.list_tools()
    }["task"]
    schema = tool.parameters["x-runtime-schema"]
    assert "TaskDoneRequest" not in schema["$defs"]
    validator = Draft202012Validator(schema)
    assert list(validator.iter_errors({"request": _base("done")}))


@pytest.mark.asyncio
async def test_done_action_is_rejected_before_backend():
    backend = _RecordingBackend()
    tool = {tool.name: tool for tool in build_mcp(_Service(backend))._tool_manager.list_tools()}[
        "task"
    ]
    await tool.run({"request": _base("done")}, convert_result=True)
    assert backend.task_calls == []


def test_generated_discovery_schema_rejects_regression_inputs():
    backend = _RecordingBackend()
    tool = {tool.name: tool for tool in build_mcp(_Service(backend))._tool_manager.list_tools()}[
        "task"
    ]
    validator = Draft202012Validator(tool.parameters["x-runtime-schema"])
    invalid = [
        {
            "code": "1234",
            "action": "claim",
            "namespace": "example",
            "task_id": "TASK-001",
            "payload": {"anything": "accepted"},
        },
        {
            "request": {
                "action": "claim",
                "code": "1234",
                "namespace": "example",
                "task_id": "TASK-001",
                "claim_intnet": "work",
            }
        },
        {
            "request": {
                "action": "review",
                "code": "1234",
                "namespace": "example",
                "task_id": "TASK-001",
                "dimensions": ["A"],
                "verdict": "NON_BLOCKING",
                "candidate_ref": "sha:abc",
            }
        },
    ]
    for payload in invalid:
        assert list(validator.iter_errors(payload)), payload


def test_fastmcp_runtime_arg_model_is_the_discovery_schema_source():
    backend = _RecordingBackend()
    tool = {tool.name: tool for tool in build_mcp(_Service(backend))._tool_manager.list_tools()}[
        "task"
    ]
    assert tool.fn_metadata.arg_model is TaskToolArguments
    assert tool.parameters["x-runtime-schema"] == TaskToolArguments.model_json_schema()
    assert tool.parameters["x-runtime-schema"]["required"] == ["request"]
    assert "required" not in tool.parameters
    assert tool.parameters["x-runtime-schema"]["additionalProperties"] is False
    assert "additionalProperties" not in tool.parameters


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "expected_paths"),
    [
        (
            {
                "request": {
                    "action": "claim",
                    "code": "1234",
                    "namespace": "example",
                    "task_id": "TASK-001",
                    "claim_intent": "work",
                },
                "unexpected": 1,
            },
            ["*"],
        ),
        ({"request": "bad"}, ["request"]),
        ({"request": []}, ["request"]),
        ({}, ["request"]),
    ],
)
async def test_tool_run_returns_structured_errors_for_full_public_boundary(
    arguments, expected_paths
):
    backend = _RecordingBackend()
    mcp = build_mcp(_Service(backend))
    tool = {tool.name: tool for tool in mcp._tool_manager.list_tools()}["task"]
    validator = Draft202012Validator(tool.parameters["x-runtime-schema"])
    assert list(validator.iter_errors(arguments))

    result = await tool.run(arguments, convert_result=True)
    assert backend.identity_calls == 0
    assert backend.task_calls == []
    assert result.structuredContent["code"] == "validation_error"
    errors = result.structuredContent["details"]["validation_errors"]
    assert [item["path"] for item in errors] == expected_paths
    assert all(item["error_class"] for item in errors)
    assert all(item["description"] for item in errors)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {
            "request": {
                "action": "claim",
                "code": "1234",
                "namespace": "example",
                "task_id": "TASK-001",
                "claim_intent": "work",
            },
            "unexpected": 1,
        },
        {"request": "bad"},
        {"request": []},
        {},
    ],
)
async def test_tool_manager_call_tool_uses_same_task_boundary(arguments):
    backend = _RecordingBackend()
    mcp = build_mcp(_Service(backend))
    result = await mcp._tool_manager.call_tool("task", arguments, convert_result=True)
    assert backend.identity_calls == 0
    assert backend.task_calls == []
    assert result.structuredContent["code"] == "validation_error"
    errors = result.structuredContent["details"]["validation_errors"]
    assert errors
    assert all({"error_class", "path", "description"} <= set(item) for item in errors)
