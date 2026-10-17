"""Public error value contract; no runtime or transport wiring is required."""

import asyncio
import json
import sqlite3

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from terminal_mcp.core.public_errors import (
    ERROR_SPECS,
    MAX_COORDINATION_MESSAGES,
    MAX_ERROR_MESSAGE,
    PublicError,
    PublicFailure,
    error_from_exception,
    normalize_public_error,
    public_error,
    retry_decision,
    validation_error,
)

SECRET = "PRIVATE_TOKEN_must_never_be_echoed"


@pytest.mark.parametrize(
    "code,kind,recovery,outcome",
    [
        ("validation_error", "validation", "repair", "not_committed"),
        ("unauthorized", "access", "reauthenticate", "not_committed"),
        ("access_denied", "access", "stop", "not_committed"),
        ("revision_conflict", "conflict", "reconcile", "not_committed"),
        ("busy", "transient", "retry", "unknown"),
        ("internal_error", "internal", "reconcile", "unknown"),
        ("session_expired", "access", "stop", "not_committed"),
    ],
)
def test_representative_registered_classes_are_bounded_and_serializable(
    code, kind, recovery, outcome
):
    spec = ERROR_SPECS[code]
    assert (spec.kind, spec.recovery) == (kind, recovery)
    value = public_error(code)
    wire = value.as_dict()
    assert wire == {
        "ok": False,
        "code": code,
        "message": spec.message,
        "error": spec.message,
        "outcome": outcome,
        "retry": recovery,
        **({"return_to_chat": True} if code == "session_expired" else {}),
    }
    assert 0 < len(value.error) <= MAX_ERROR_MESSAGE
    assert PublicError.model_validate_json(json.dumps(wire)) == value
    assert len(json.dumps(wire, ensure_ascii=False).encode()) < 512


@pytest.mark.parametrize(
    "raw",
    [
        {"ok": False, "code": SECRET, "error": SECRET, "details": {"trace": SECRET}},
        {"code": [SECRET], "error": {"private": SECRET}},
    ],
)
def test_unknown_codes_and_diagnostics_fail_closed(raw):
    result = normalize_public_error(raw)
    assert result.code == "internal_error"
    assert set(result.as_dict()) == {"ok", "code", "message", "error", "outcome", "retry"}
    assert SECRET not in json.dumps(result.as_dict())


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


@pytest.mark.parametrize(
    "exc,code",
    [
        (RuntimeError(SECRET), "internal_error"),
        (TimeoutError(SECRET), "service_unavailable"),
        (sqlite3.OperationalError(SECRET), "storage_unavailable"),
        (PublicFailure("command_not_found"), "command_not_found"),
        (PublicFailure(SECRET), "internal_error"),
    ],
)
def test_exception_mapping_does_not_classify_from_or_echo_text(exc, code):
    result = error_from_exception(exc)
    assert result.code == code
    assert SECRET not in json.dumps(result.as_dict())


@pytest.mark.parametrize("exc", [asyncio.CancelledError(), KeyboardInterrupt(), SystemExit()])
def test_cancellation_and_process_control_are_not_swallowed(exc):
    with pytest.raises(type(exc)) as caught:
        error_from_exception(exc)
    assert caught.value is exc


def test_execution_request_timeout_requires_reconciliation_before_retry():
    value = public_error("execution_request_timeout")
    assert ERROR_SPECS["execution_request_timeout"].recovery == "reconcile"
    decision = retry_decision(value, operation="cmd.run")
    assert decision.action == "reconcile"
    assert decision.retry_after_ms is None


def test_ambiguous_command_acceptance_requires_reconciliation():
    value = public_error("authority_unavailable", details={"retry_after_ms": 1500})

    decision = retry_decision(value, operation="cmd.run")

    assert decision.action == "reconcile"
    assert decision.retry_after_ms is None


def test_coordination_error_keeps_only_bounded_next_action_messages():
    raw = {
        "ok": False,
        "code": "coordination_alert",
        "error": "private backend wording",
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


def test_missing_session_directs_the_agent_to_start_one():
    required = public_error("session_required")

    assert required.retry == "start_session"
    assert required.message == "Сначала запусти сессию"


def test_unconfirmed_control_mutation_requires_reconciliation_even_with_an_idempotency_key():
    failure = public_error("control_mutation_unconfirmed")

    decision = retry_decision(
        failure, operation="fleet.control.policy", idempotency_guaranteed=True
    )

    assert decision.action == "reconcile"
    assert decision.retry_after_ms is None
