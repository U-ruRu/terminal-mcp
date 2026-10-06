"""Public error value contract; no runtime or transport wiring is required."""

import asyncio
import json
import sqlite3
from dataclasses import FrozenInstanceError

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from terminal_mcp.core.public_errors import (
    ERROR_SPECS,
    LEGACY_PUBLIC_ERROR_ALIASES,
    MAX_COORDINATION_MESSAGES,
    MAX_ERROR_MESSAGE,
    MAX_RETRY_AFTER_MS,
    MAX_VALIDATION_ISSUES,
    ConflictRepair,
    PublicError,
    PublicFailure,
    RetryRepair,
    ValidationIssue,
    ValidationRepair,
    error_from_exception,
    normalize_public_error,
    public_error,
    retry_decision,
    validation_error,
)

SECRET = "PRIVATE_TOKEN_must_never_be_echoed"


@pytest.mark.parametrize("code", sorted(ERROR_SPECS))
def test_registered_codes_are_stable_bounded_and_serializable(code):
    value = public_error(code)
    wire = value.as_dict()
    assert wire == {"ok": False, "code": code, "error": ERROR_SPECS[code].message}
    assert 0 < len(value.error) <= MAX_ERROR_MESSAGE
    assert PublicError.model_validate_json(json.dumps(wire)) == value
    assert len(json.dumps(wire).encode()) < 512


def test_catalog_and_public_values_are_immutable():
    with pytest.raises(TypeError):
        ERROR_SPECS["injected"] = ERROR_SPECS["internal_error"]
    with pytest.raises(FrozenInstanceError):
        ERROR_SPECS["internal_error"].message = SECRET
    with pytest.raises(ValidationError):
        public_error("internal_error").error = SECRET


@pytest.mark.parametrize(
    "raw",
    [
        {"ok": False, "code": SECRET, "error": SECRET, "details": {"trace": SECRET}},
        {"error": SECRET * 10000, "stack": SECRET, "context": {"input": SECRET}},
        {"code": [SECRET], "error": {"private": SECRET}},
        {"code": "nonregistered_but_valid_looking", "details": {"retry_after_ms": 123}},
    ],
)
def test_unknown_codes_and_diagnostics_fail_closed(raw):
    result = normalize_public_error(raw)
    assert result.code == "internal_error"
    assert set(result.as_dict()) == {"ok", "code", "error"}
    assert SECRET not in json.dumps(result.as_dict())


@pytest.mark.parametrize(
    ("legacy_code", "public_code"),
    [
        ("already_claimed", "task_claim_conflict"),
        ("agent_busy", "wip_limit_exceeded"),
    ],
)
def test_legacy_task_conflicts_map_to_registered_public_codes(legacy_code, public_code):
    result = normalize_public_error(
        {"ok": False, "code": legacy_code, "error": f"private {legacy_code} details"}
    )
    assert result == public_error(public_code)


def test_legacy_public_error_aliases_are_immutable():
    with pytest.raises(TypeError):
        LEGACY_PUBLIC_ERROR_ALIASES["already_claimed"] = "internal_error"


def test_task_backend_error_codes_have_public_policy():
    import ast
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "src" / "terminal_mcp" / "core" / "tasks.py"
    tree = ast.parse(path.read_text())
    codes = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "code"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    codes.add(value.value)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "code"
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    codes.add(node.value.value)

    missing = sorted(
        code
        for code in codes
        if code not in ERROR_SPECS and code not in LEGACY_PUBLIC_ERROR_ALIASES
    )
    assert not missing, f"Task backend codes need public policy or alias: {missing}"
    assert all(target in ERROR_SPECS for target in LEGACY_PUBLIC_ERROR_ALIASES.values())


def test_legacy_code_preserved_but_raw_message_and_context_discarded():
    result = normalize_public_error(
        {
            "ok": False,
            "code": "policy_incompatible",
            "error": SECRET,
            "policy": {"authorization": SECRET},
            "details": {"stack": SECRET, "retry_after_ms": 10},
        }
    )
    assert result == public_error("policy_incompatible")
    assert normalize_public_error({"error": "command_not_found"}).code == "command_not_found"
    assert normalize_public_error(result) is result


def test_strict_code_and_message_cannot_be_overridden():
    with pytest.raises(ValidationError):
        PublicError(code="internal_error", error=SECRET)
    with pytest.raises(ValidationError):
        PublicError(code="unregistered", error="Unregistered.")
    with pytest.raises(ValidationError):
        PublicError(**public_error("internal_error").as_dict(), retryable=True)


def test_conflict_has_only_current_revision_and_state():
    result = normalize_public_error(
        {
            "code": "revision_conflict",
            "error": SECRET,
            "current_revision": 17,
            "current_state": "in_progress",
            "task": {"body": SECRET},
        }
    )
    assert result.as_dict()["details"] == {"current_revision": 17, "current_state": "in_progress"}
    nested = normalize_public_error(
        {
            "code": "revision_conflict",
            "details": {
                "current_revision": 17,
                "current_state": "in_progress",
                "trace": SECRET,
            },
        }
    )
    assert result == nested
    assert PublicError.model_validate(result.as_dict()) == result


@pytest.mark.parametrize("revision", [True, False, 0, -1, 1.5, "17", 1 << 64, None])
def test_invalid_revision_does_not_escape(revision):
    result = public_error(
        "revision_conflict",
        details={
            "current_revision": revision,
            "current_state": SECRET,
        },
    )
    assert result.details is None


@pytest.mark.parametrize("delay", [True, 0, -1, "20", 2.5, MAX_RETRY_AFTER_MS + 1, None])
def test_retry_delay_is_strict_and_bounded(delay):
    assert public_error("rate_limited", details={"retry_after_ms": delay}).details is None


def test_repair_is_class_specific():
    assert public_error("unauthorized", details={"current_revision": 12}).details is None
    assert public_error("revision_conflict", details={"retry_after_ms": 5}).details is None
    assert public_error("rate_limited", details={"current_revision": 12}).details is None
    value = public_error("rate_limited", details={"retry_after_ms": 1500, "trace": SECRET})
    assert value.as_dict()["details"] == {"retry_after_ms": 1500}
    with pytest.raises(ValidationError):
        PublicError(
            code="unauthorized",
            error=ERROR_SPECS["unauthorized"].message,
            details=RetryRepair(retry_after_ms=10),
        )
    with pytest.raises(ValidationError):
        ConflictRepair()


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(max_length=3)
    count: int

    @field_validator("count", mode="before")
    @classmethod
    def private_validator(cls, value):
        raise ValueError(SECRET)


def make_validation_error():
    with pytest.raises(ValidationError) as caught:
        Input.model_validate({"title": SECRET, "count": SECRET, SECRET: SECRET})
    return caught.value


def test_validation_never_echoes_values_custom_errors_or_extra_field_names():
    result = validation_error(make_validation_error())
    wire = result.as_dict()
    assert SECRET not in json.dumps(wire)
    assert result.code == "validation_error"
    issues = wire["details"]["validation_errors"]
    assert issues[0]["path"] == "title"
    assert issues[0]["expected"] == "max_length=3"
    assert issues[1]["error_class"] == "value_error"
    assert issues[1]["path"] == "*"  # count is not a canonical public schema field
    assert issues[2]["path"] == "*"  # caller-controlled extra key
    assert PublicError.model_validate(wire) == result
    assert error_from_exception(make_validation_error()).code == "validation_error"


def test_validation_can_use_trusted_schema_paths():
    result = validation_error(make_validation_error(), allowed_fields=frozenset({"title", "count"}))
    assert result.details.validation_errors[1].path == "count"
    assert result.details.validation_errors[2].path == "*"


def test_validation_issue_count_is_bounded():
    class ExtraInput(BaseModel):
        model_config = ConfigDict(extra="forbid")

    with pytest.raises(ValidationError) as caught:
        ExtraInput.model_validate({f"{SECRET}{n}": SECRET for n in range(100)})
    result = validation_error(caught.value)
    assert len(result.details.validation_errors) == MAX_VALIDATION_ISSUES
    assert SECRET not in json.dumps(result.as_dict())
    assert len(json.dumps(result.as_dict()).encode()) < 4096
    assert isinstance(result.details.validation_errors, tuple)


def test_untrusted_legacy_validation_details_are_not_echoed():
    value = normalize_public_error(
        {
            "code": "validation_error",
            "error": SECRET,
            "details": {"validation_errors": [{"path": SECRET, "description": SECRET}]},
        }
    )
    assert value.details is None


@pytest.mark.parametrize(
    "exc,code",
    [
        (RuntimeError(SECRET), "internal_error"),
        (ValueError(SECRET), "internal_error"),
        (KeyError(SECRET), "internal_error"),
        (PermissionError(SECRET), "internal_error"),
        (TimeoutError(SECRET), "service_unavailable"),
        (ConnectionError(SECRET), "service_unavailable"),
        (sqlite3.OperationalError(SECRET), "storage_unavailable"),
        (PublicFailure("command_not_found"), "command_not_found"),
        (PublicFailure(SECRET), "internal_error"),
    ],
)
def test_exception_mapping_does_not_classify_from_or_echo_text(exc, code):
    result = error_from_exception(exc)
    assert result.code == code
    assert SECRET not in json.dumps(result.as_dict())


def test_sqlite_busy_uses_numeric_code_not_message():
    exc = sqlite3.OperationalError(SECRET)
    exc.sqlite_errorcode = sqlite3.SQLITE_BUSY | (2 << 8)
    assert error_from_exception(exc).code == "storage_busy"
    exc.sqlite_errorcode = sqlite3.SQLITE_LOCKED
    assert error_from_exception(exc).code == "storage_busy"
    assert (
        error_from_exception(sqlite3.OperationalError("database is locked")).code
        == "storage_unavailable"
    )


@pytest.mark.parametrize("exc", [asyncio.CancelledError(), KeyboardInterrupt(), SystemExit()])
def test_cancellation_and_process_control_are_not_swallowed(exc):
    with pytest.raises(type(exc)) as caught:
        error_from_exception(exc)
    assert caught.value is exc


def test_ambiguous_mutation_is_never_blindly_retried():
    value = public_error("authority_unavailable", details={"retry_after_ms": 1500})
    for operation in (
        "cmd.run",
        "session.start",
        "message.send",
        "task.create",
        "cmd.recovery",
        "unknown",
    ):
        decision = retry_decision(value, operation=operation)
        assert decision.action == "reconcile"
        assert decision.retry_after_ms is None
    assert retry_decision(value, operation="cmd.read").action == "reconcile"
    assert retry_decision(value, operation="observe").retry_after_ms == 1500
    assert retry_decision(value, operation="cmd.run", outcome="rejected").action == "retry"
    assert (
        retry_decision(value, operation="task.create", idempotency_guaranteed=True).action
        == "retry"
    )


@pytest.mark.parametrize(
    "code,action",
    [
        ("validation_error", "repair"),
        ("unauthorized", "reauthenticate"),
        ("revision_conflict", "reconcile"),
        ("capability_not_allowed", "stop"),
        ("internal_error", "reconcile"),
    ],
)
def test_nontransient_error_policy_is_not_overridden_by_retry_safety(code, action):
    assert (
        retry_decision(public_error(code), operation="observe", outcome="rejected").action == action
    )


def test_retry_policy_requires_explicit_guarantee():
    with pytest.raises(ValueError):
        retry_decision(public_error("busy"), operation="cmd.run", idempotency_guaranteed="yes")
    with pytest.raises(ValueError):
        retry_decision(public_error("busy"), operation="cmd.run", outcome="maybe")


def test_schema_is_closed_and_has_visible_bounds():
    schema = PublicError.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["code"]["maxLength"] == 64
    assert schema["properties"]["error"]["maxLength"] == MAX_ERROR_MESSAGE
    for definition in schema["$defs"].values():
        if definition.get("type") == "object":
            assert definition["additionalProperties"] is False
    repair_schema = schema["$defs"]["ValidationRepair"]["properties"]["validation_errors"]
    assert repair_schema["maxItems"] == MAX_VALIDATION_ISSUES
    assert (
        schema["$defs"]["RetryRepair"]["properties"]["retry_after_ms"]["maximum"]
        == MAX_RETRY_AFTER_MS
    )
    issue = ValidationIssue(error_class="missing", path="title", description="Provide the title.")
    with pytest.raises(ValidationError):
        ValidationRepair(validation_errors=(issue,) * (MAX_VALIDATION_ISSUES + 1))


@pytest.mark.parametrize("operation", ["cmd.read", "command_read"])
def test_read_named_operation_requires_actual_retry_safety(operation):
    failure = public_error("authority_unavailable", details={"retry_after_ms": 25})
    unknown = retry_decision(failure, operation=operation)
    assert unknown.action == "reconcile"
    assert unknown.retry_after_ms is None
    # Proof comes from server-side adapter semantics, never an agent input flag.
    proven = retry_decision(failure, operation=operation, idempotency_guaranteed=True)
    assert proven.action == "retry"
    assert proven.retry_after_ms == 25
    rejected = retry_decision(failure, operation=operation, outcome="rejected")
    assert rejected.action == "retry"


@pytest.mark.parametrize(
    "code",
    [
        "not_found",
        "session_not_found",
        "wrong_authority",
        "recipient_not_found",
        "idempotency_conflict",
        "missing_parent_candidate",
        "candidate_conflict",
        "persistent_auth_required",
        "persistent_scope_required",
        "policy_update_empty",
        "policy_invalid_rearm",
        "policy_invalid_duration",
        "policy_invalid_warning",
        "policy_invalid_alert",
        "policy_persist_failed",
        "access_policy_revision_conflict",
        "topology_revision_conflict",
        "control_authority_unavailable",
    ],
)
def test_deployed_public_code_survives_legacy_normalization(code):
    error = normalize_public_error(
        {
            "ok": False,
            "code": code,
            "error": "PRIVATE DATABASE OR REQUEST VALUE",
            "traceback": "PRIVATE TRACE",
            "details": {"secret": "PRIVATE"},
        }
    )
    assert error.code == code
    assert "PRIVATE" not in json.dumps(error.as_dict())
    assert normalize_public_error({"ok": False, "error": code}).code == code


def test_public_source_error_codes_have_explicit_catalog_policy():
    """Guard direct public literals AND domain exception codes copied by adapters.

    This is not an inference of arbitrary runtime strings. New code expressions
    need explicit review; fixed protocol OAuth errors remain outside this DTO.
    """
    import ast
    import re
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "src" / "terminal_mcp"
    constructors = {
        "PersistentStoreError",
        "PersistentLifecycleError",
        "PersistentAdmissionError",
        "PersistentPolicyError",
        "TaskRelationConflict",
        "FleetControlError",
        "MeshApplicationError",
        "PublicFailure",
        "failure",
        "_read_error",
    }
    found = {}
    for path in source.rglob("*.py"):
        relative = path.relative_to(source)
        if path.name == "public_errors.py" or relative.as_posix() == "auth/routes.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            values = []
            if isinstance(node, ast.Dict):
                values = [
                    value
                    for key, value in zip(node.keys, node.values, strict=True)
                    if isinstance(key, ast.Constant) and key.value in {"code", "error"}
                ]
            elif isinstance(node, ast.Call):
                name = getattr(node.func, "id", getattr(node.func, "attr", ""))
                if name in constructors:
                    values.extend(node.args[:1])
                values.extend(item.value for item in node.keywords if item.arg == "code")
            for value in values:
                if (
                    isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                    and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value.value)
                ):
                    found.setdefault(value.value, []).append(
                        f"{path.relative_to(source)}:{node.lineno}"
                    )
    assert {
        "session_not_found",
        "wrong_authority",
        "idempotency_conflict",
        "persistent_scope_required",
        "policy_update_empty",
        "policy_invalid_rearm",
        "policy_invalid_duration",
        "policy_invalid_warning",
        "policy_invalid_alert",
        "policy_persist_failed",
    } <= found.keys()
    missing = {code: origins for code, origins in found.items() if code not in ERROR_SPECS}
    assert not missing, f"New public codes need explicit bounded message/retry policy: {missing}"


def test_coordination_error_keeps_only_bounded_next_action_messages():
    raw = {
        "ok": False,
        "code": "coordination_alert",
        "error": "secret legacy wording",
        "messages": [
            {"message_hash": f"m-{index}", "mode": "alert", "text": "reply", "secret": "drop"}
            for index in range(10)
        ],
        "pending_messages": [
            {"message_hash": f"p-{index}", "mode": "alert", "text": "reply"} for index in range(10)
        ],
        "ack_required_pending": True,
        "alert_pending": True,
        "diagnostics": {"password": "drop"},
    }
    result = normalize_public_error(raw).as_dict()
    assert result["code"] == "coordination_alert"
    assert len(result["details"]["pending_messages"]) == MAX_COORDINATION_MESSAGES
    assert result["details"]["pending_messages"][0]["message_hash"] == "p-0"
    assert "diagnostics" not in repr(result)
    assert "secret" not in repr(result)
