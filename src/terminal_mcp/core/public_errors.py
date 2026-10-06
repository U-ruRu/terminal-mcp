"""Bounded public error values, independent of application and transports.

Only factories in this module may normalize untrusted/legacy failures. Raw
exception messages and request values are never public messages. Adapters must
log technical causes separately, and must serialize this value into *both*
text and structured channels. OAuth protocol errors remain an adapter concern.

This module deliberately has no runtime wiring. Retry advice is server-side
policy, not a repetitive field in every wire error. An ambiguous failed mutation
must be reconciled unless the operation actually guarantees idempotency.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

MAX_ERROR_MESSAGE = 240
MAX_VALIDATION_ISSUES = 5
MAX_COORDINATION_MESSAGES = 4
MAX_ERROR_PATH = 160
MAX_RETRY_AFTER_MS = 3_600_000
MAX_REVISION = (1 << 63) - 1

ErrorKind = Literal[
    "validation", "access", "conflict", "transient", "missing", "policy", "internal"
]
RecoveryAction = Literal["repair", "reauthenticate", "reconcile", "retry", "stop"]


@dataclass(frozen=True)
class ErrorSpec:
    kind: ErrorKind
    recovery: RecoveryAction
    message: str


def _catalog() -> Mapping[str, ErrorSpec]:
    # Registered codes are literals, never learned from requests or exceptions.
    groups: tuple[tuple[ErrorKind, RecoveryAction, str], ...] = (
        (
            "validation",
            "repair",
            """
            validation_error invalid_request invalid_command invalid_queue invalid_mode
            invalid_message_mode invalid_task_scope invalid_task_target invalid_cursor
            mode_required access_code_required legacy_code_not_allowed invalid_connect_url
            output_item_too_large
        """,
        ),
        (
            "access",
            "reauthenticate",
            """
            unauthorized invalid_access_code invalid_code access_denied access_code_invalid
            access_code_expired session_expired session_inactive session_not_active
            work_session_expired stale_session stale_session_epoch session_epoch_mismatch
            sender_not_authorized message_forbidden capability_not_allowed
            legacy_admission_disabled
        """,
        ),
        (
            "conflict",
            "reconcile",
            """
            conflict revision_conflict task_revision_conflict stale_revision
            candidate_mismatch candidate_ref_frozen output_state_changed output_state_missing
            owner_required claim_conflict task_claim_conflict wip_limit_exceeded
            archived_task command_not_owned command_not_persistent command_not_running
            review_requirements_unsatisfied coordination_alert coordination_ack_required
            session_already_active policy_in_use dependency_open
        """,
        ),
        (
            "transient",
            "retry",
            """
            busy queue_busy storage_busy rate_limited authority_unavailable
            service_unavailable transport_unavailable storage_unavailable
            message_state_unavailable message_unavailable task_unavailable query_v2_unavailable
        """,
        ),
        (
            "missing",
            "repair",
            """
            not_found resource_not_found task_not_found command_not_found message_not_found
            slot_not_found recipient_not_active no_active_recipients unknown_source
        """,
        ),
        ("policy", "repair", "policy_incompatible"),
        (
            "internal",
            "reconcile",
            """
            internal_error operation_failed run_failed recovery_failed execution_failed
            access_registration_failed access_retire_failed
            access_rotation_failed access_update_failed
            fleet_control_invalid_header fleet_control_main_is_wal
        """,
        ),
    )
    # Preserve deployed Access/workflow/Fleet codes, including typed failures whose
    # code is copied into public payloads by adapters. OAuth remains protocol-specific.
    groups += (
        (
            "validation",
            "repair",
            """
            invalid_message invalid_task_context review_task_required
            control_mutation_invalid control_operation_invalid control_snapshot_invalid
            invalid_transfer_transition managed_snapshot_required
            managed_snapshot_revision_invalid mesh_id_required
            policy_update_empty policy_invalid_rearm policy_invalid_duration
            policy_invalid_warning policy_invalid_alert
        """,
        ),
        (
            "access",
            "reauthenticate",
            """
            access_identity_not_found permit_expired persistent_auth_required
            persistent_scope_required selector_not_found session_not_found
            control_rejoin_credential_required invalid_pairing
        """,
        ),
        (
            "policy",
            "repair",
            """
            access_mode_mismatch attachment_not_active slot_not_armed wrong_authority
            control_authority_rehome_requires_standalone control_authority_required
            managed_access_policy_missing managed_control_not_adopted
            managed_node_mesh_missing projection_topology_missing
        """,
        ),
        (
            "conflict",
            "reconcile",
            """
            access_policy_revision_conflict ambiguous_review_parent candidate_conflict
            delete_blocked idempotency_conflict missing_parent_candidate reassign_blocked
            recovery_required session_stopping control_node_mismatch control_release_peer_mismatch
            fleet_id_mismatch managed_snapshot_stale mesh_already_exists node_already_in_other_mesh
            projection_epoch_conflict projection_topology_mismatch stale_authority_epoch
            topology_revision_conflict transfer_source_mismatch trust_revision_conflict
            trust_rotation_peer_mismatch
        """,
        ),
        (
            "transient",
            "retry",
            """
            idempotency_in_progress route_unavailable control_authority_unavailable
        """,
        ),
        (
            "missing",
            "repair",
            """
            claim_not_found recipient_not_found managed_mesh_not_found
            managed_node_not_found transfer_not_found
        """,
        ),
        ("access", "stop", "cors_preflight_rejected origin_not_allowed"),
        ("internal", "reconcile", "access_issue_failed policy_persist_failed"),
    )
    messages = {
        "internal_error": "The operation failed internally. Check current state before retrying.",
        "operation_failed": "The operation did not complete. Check current state before retrying.",
        "validation_error": "Correct the indicated request fields.",
        "invalid_cursor": "Restart the read without a cursor, or use its matching next cursor.",
        "output_item_too_large": "Request a summary or a smaller page.",
        "fleet_control_invalid_header": (
            "Fleet control storage has an invalid header. Restore validated control state."
        ),
        "fleet_control_main_is_wal": (
            "Fleet control storage contains a WAL header. Restore validated control state."
        ),
        "authority_unavailable": (
            "The authoritative node is unavailable. Check state before retrying changes."
        ),
        "capability_not_allowed": "This endpoint does not allow the operation.",
        "coordination_alert": "Read and reply to the pending alert before continuing.",
        "coordination_ack_required": (
            "Read and acknowledge the pending message before running work."
        ),
        "policy_incompatible": "The requested operation is incompatible with the active policy.",
    }
    result = {}
    for kind, recovery, codes in groups:
        for code in codes.split():
            result[code] = ErrorSpec(
                kind, recovery, messages.get(code, code.replace("_", " ").capitalize() + ".")
            )
    for code in ("capability_not_allowed", "legacy_admission_disabled", "access_denied"):
        result[code] = ErrorSpec("access", "stop", result[code].message)
    return MappingProxyType(result)


ERROR_SPECS = _catalog()

LEGACY_PUBLIC_ERROR_ALIASES = MappingProxyType(
    {
        "already_claimed": "task_claim_conflict",
        "agent_busy": "wip_limit_exceeded",
    }
)


class _BoundedValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ValidationIssue(_BoundedValue):
    error_class: str = Field(min_length=1, max_length=48, pattern=r"^[a-z][a-z0-9_]*$")
    path: str = Field(min_length=1, max_length=MAX_ERROR_PATH)
    description: str = Field(min_length=1, max_length=160)
    expected: str | None = Field(default=None, max_length=96)


class ValidationRepair(_BoundedValue):
    validation_errors: tuple[ValidationIssue, ...] = Field(
        min_length=1, max_length=MAX_VALIDATION_ISSUES, strict=False
    )


TaskState = Literal["ready", "in_progress", "blocked", "deferred", "done"]


class ConflictRepair(_BoundedValue):
    current_revision: int | None = Field(default=None, ge=1, le=MAX_REVISION)
    current_state: TaskState | None = None

    @model_validator(mode="after")
    def has_repair(self):
        if self.current_revision is None and self.current_state is None:
            raise ValueError("a conflict repair requires current_revision or current_state")
        return self


class RetryRepair(_BoundedValue):
    retry_after_ms: int = Field(ge=1, le=MAX_RETRY_AFTER_MS)


class CoordinationMessage(_BoundedValue):
    message_hash: str = Field(min_length=1, max_length=128)
    mode: Literal["notify", "ack", "alert"]
    text: str | None = Field(default=None, max_length=500)
    sender: str | None = Field(default=None, max_length=64)


class CoordinationRepair(_BoundedValue):
    pending_messages: tuple[CoordinationMessage, ...] = Field(
        min_length=1,
        max_length=MAX_COORDINATION_MESSAGES,
        strict=False,
    )
    ack_required_pending: bool = False
    alert_pending: bool = False


ErrorRepair = ValidationRepair | ConflictRepair | RetryRepair | CoordinationRepair
_REPAIR_TYPES = {
    "validation": ValidationRepair,
    "conflict": ConflictRepair,
    "transient": RetryRepair,
}


class PublicError(_BoundedValue):
    ok: Literal[False] = False
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    error: str = Field(min_length=1, max_length=MAX_ERROR_MESSAGE)
    details: ErrorRepair | None = None

    @model_validator(mode="after")
    def canonical(self):
        spec = ERROR_SPECS.get(self.code)
        if spec is None or self.error != spec.message:
            raise ValueError("public error code/message must come from the registered catalog")
        expected_type = (
            CoordinationRepair
            if self.code in {"coordination_alert", "coordination_ack_required"}
            else _REPAIR_TYPES.get(spec.kind, type(None))
        )
        if self.details is not None and not isinstance(self.details, expected_type):
            raise ValueError("repair data does not apply to this error class")
        return self

    def as_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude_none=True)


# Schema-owned paths only. Unknown extra-field names may themselves be secrets.
PUBLIC_FIELDS = frozenset(
    """
    request action mode code display_name subject namespace task_id lane state
    operational_status tags detail show_done show_archived limit cursor sender text
    target message_hash require_reply alert history command cmd_hash queue_id task_scope
    context_id id summary content primary title priority description next_action
    resource_context cooperative candidate_ref input_refs output_refs checkpoint result
    blocker_reason force force_reason release_reason claim_intent comment_text relation_kind
    related_namespace related_task_id expected_revision dimensions verdict
    evidence note archive_note
""".split()
)

_VALIDATION_MESSAGES = MappingProxyType(
    {
        "missing": "Provide this required field.",
        "extra_forbidden": "Remove this unsupported field.",
        "string_type": "Provide a string.",
        "string_too_short": "Provide a longer string.",
        "string_too_long": "Provide a shorter string.",
        "string_pattern_mismatch": "Use the field format declared in the schema.",
        "int_type": "Provide an integer.",
        "int_parsing": "Provide an integer.",
        "int_from_float": "Provide a whole number.",
        "bool_type": "Provide a boolean.",
        "bool_parsing": "Provide a boolean.",
        "list_type": "Provide an array.",
        "tuple_type": "Provide an array.",
        "dict_type": "Provide an object.",
        "model_type": "Provide an object matching the schema.",
        "model_attributes_type": "Provide an object matching the schema.",
        "literal_error": "Use one of the values declared in the schema.",
        "enum": "Use one of the values declared in the schema.",
        "union_tag_invalid": "Use a supported action declared in the schema.",
        "union_tag_not_found": "Provide the action discriminator.",
        "too_long": "Provide fewer items.",
        "too_short": "Provide more items.",
        "greater_than": "Increase this value to the schema minimum.",
        "greater_than_equal": "Increase this value to the schema minimum.",
        "less_than": "Reduce this value to the schema maximum.",
        "less_than_equal": "Reduce this value to the schema maximum.",
        "value_error": "Correct this field according to the schema.",
        "invalid_value": "Correct this field according to the schema.",
    }
)


def _path(parts: object, allowed_fields: frozenset[str]) -> str:
    if not isinstance(parts, (list, tuple)):
        return "$"
    output = []
    for part in parts[:12]:
        if type(part) is int:
            output.append(f"[{part}]" if 0 <= part <= 9999 else "[*]")
        else:
            name = part if isinstance(part, str) and part in allowed_fields else "*"
            output.append(("." if output else "") + name)
    return ("".join(output) or "$")[:MAX_ERROR_PATH]


def _expectation(context: object) -> str | None:
    if isinstance(context, Mapping):
        for key in ("max_length", "min_length", "le", "lt", "ge", "gt"):
            value = context.get(key)
            if type(value) is int and abs(value) <= 1_000_000_000:
                return f"{key}={value}"
    return None


def validation_issue(item: Mapping[str, object], *, path: str) -> ValidationIssue:
    """Build one schema-owned validation issue without exposing Pydantic's raw message."""
    kind = item.get("type")
    kind = kind if kind in _VALIDATION_MESSAGES else "invalid_value"
    return ValidationIssue(
        error_class=kind,
        path=path,
        description=_VALIDATION_MESSAGES[kind],
        expected=_expectation(item.get("ctx")),
    )


def validation_error(
    exc: ValidationError, *, allowed_fields: frozenset[str] = PUBLIC_FIELDS
) -> PublicError:
    issues = []
    # Pydantic's msg/ctx.error may contain custom exceptions with secrets.
    for item in exc.errors(include_input=False, include_url=False)[:MAX_VALIDATION_ISSUES]:
        issues.append(
            validation_issue(
                item,
                path=_path(item.get("loc"), allowed_fields),
            )
        )
    details = ValidationRepair(validation_errors=tuple(issues)) if issues else None
    return public_error("validation_error", details=details)


def _repair(code: str, kind: ErrorKind, raw: object) -> ErrorRepair | None:
    if code in {"coordination_alert", "coordination_ack_required"}:
        if isinstance(raw, CoordinationRepair):
            return raw
        if not isinstance(raw, Mapping):
            return None
        source = raw.get("pending_messages") or raw.get("messages")
        if not isinstance(source, (list, tuple)):
            return None
        messages = []
        for item in source[:MAX_COORDINATION_MESSAGES]:
            if not isinstance(item, Mapping):
                continue
            message_hash = item.get("message_hash")
            mode = item.get("mode")
            if not isinstance(message_hash, str) or mode not in {"notify", "ack", "alert"}:
                continue
            text = item.get("text")
            sender = item.get("sender")
            messages.append(
                CoordinationMessage(
                    message_hash=message_hash,
                    mode=mode,
                    text=text if isinstance(text, str) else None,
                    sender=sender if isinstance(sender, str) else None,
                )
            )
        if not messages:
            return None
        return CoordinationRepair(
            pending_messages=tuple(messages),
            ack_required_pending=bool(raw.get("ack_required_pending")),
            alert_pending=bool(raw.get("alert_pending")),
        )
    expected_type = _REPAIR_TYPES.get(kind)
    if expected_type and isinstance(raw, expected_type):
        return raw
    if not isinstance(raw, Mapping):
        return None
    if kind == "conflict":
        data = {}
        revision = raw.get("current_revision")
        state = raw.get("current_state")
        if type(revision) is int and 1 <= revision <= MAX_REVISION:
            data["current_revision"] = revision
        if isinstance(state, str) and state in {
            "ready",
            "in_progress",
            "blocked",
            "deferred",
            "done",
        }:
            data["current_state"] = state
        return ConflictRepair(**data) if data else None
    if kind == "transient":
        delay = raw.get("retry_after_ms")
        if type(delay) is int and 1 <= delay <= MAX_RETRY_AFTER_MS:
            return RetryRepair(retry_after_ms=delay)
    # Arbitrary legacy validation descriptions/paths are intentionally not echoed.
    # Call validation_error() with the actual ValidationError at the adapter.
    return None


def public_error(code: str, *, details: object = None) -> PublicError:
    canonical_code = code if isinstance(code, str) and code in ERROR_SPECS else "internal_error"
    spec = ERROR_SPECS[canonical_code]
    return PublicError(
        code=canonical_code,
        error=spec.message,
        details=_repair(canonical_code, spec.kind, details),
    )


def normalize_public_error(raw: Mapping[str, object] | PublicError) -> PublicError:
    """Whitelist legacy data; never stringify raw errors or copy diagnostics."""
    if isinstance(raw, PublicError):
        return raw
    # Preserve already-canonical bounded repair data, while extra legacy fields
    # fail closed into the whitelist path below.
    try:
        return PublicError.model_validate(raw)
    except ValidationError:
        pass
    code = raw.get("code")
    if isinstance(code, str):
        code = LEGACY_PUBLIC_ERROR_ALIASES.get(code, code)
    if not isinstance(code, str) or code not in ERROR_SPECS:
        old_error = raw.get("error")
        if isinstance(old_error, str):
            old_error = LEGACY_PUBLIC_ERROR_ALIASES.get(old_error, old_error)
        code = (
            old_error
            if isinstance(old_error, str) and old_error in ERROR_SPECS
            else "internal_error"
        )
    # Accept one old envelope layer, never recursively expand diagnostics.
    details = raw.get("details")
    return public_error(code, details=details if isinstance(details, Mapping) else raw)


class PublicFailure(Exception):
    """Explicit domain failure; exception chaining retains private technical causes."""

    def __init__(self, code: str, *, details: object = None):
        self.public = public_error(code, details=details)
        super().__init__(self.public.code)


def error_from_exception(exc: BaseException) -> PublicError:
    """Map known exception classes without inspecting their text; propagate cancellation."""
    if not isinstance(exc, Exception):
        raise exc
    if isinstance(exc, PublicFailure):
        return exc.public
    if isinstance(exc, ValidationError):
        return validation_error(exc)
    if isinstance(exc, sqlite3.Error):
        sqlite_code = getattr(exc, "sqlite_errorcode", None)
        if type(sqlite_code) is int and sqlite_code & 0xFF in {
            sqlite3.SQLITE_BUSY,
            sqlite3.SQLITE_LOCKED,
        }:
            return public_error("storage_busy")
        return public_error("storage_unavailable")
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return public_error("service_unavailable")
    return public_error("internal_error")


# Conservative: do not label side-effecting reads (message inbox marks seen) pure.
# An authenticated command read surfaces inbox state and is not automatically
# pure. Adapters may prove an anonymous read retry-safe per call using the
# existing idempotency_guaranteed flag; a read-shaped operation name is not proof.
_READ_ONLY_OPERATIONS = frozenset(
    {
        "health",
        "observe",
        "task_list",
        "tasks",
        "context.list",
    }
)


@dataclass(frozen=True)
class RetryDecision:
    action: RecoveryAction
    retry_after_ms: int | None = None


def retry_decision(
    failure: PublicError,
    *,
    operation: str,
    outcome: Literal["rejected", "unknown"] = "unknown",
    idempotency_guaranteed: bool = False,
) -> RetryDecision:
    """Never invent deduplication: `rejected` must mean known pre-commit rejection.

    For transport loss, timeouts or failed forwarding the default `unknown` is
    mandatory. A future adapter may set idempotency_guaranteed only when the
    operation implements that guarantee, not because a caller sent a key.
    This includes proven anonymous command reads, never authenticated inbox
    surfacing merely because the operation is named cmd.read/command_read.
    """
    if outcome not in {"rejected", "unknown"} or type(idempotency_guaranteed) is not bool:
        raise ValueError("retry outcome and idempotency guarantee must be explicit")
    action = ERROR_SPECS[failure.code].recovery
    if action != "retry":
        return RetryDecision(action)
    if (
        outcome != "rejected"
        and operation not in _READ_ONLY_OPERATIONS
        and not idempotency_guaranteed
    ):
        return RetryDecision("reconcile")
    delay = failure.details.retry_after_ms if isinstance(failure.details, RetryRepair) else None
    return RetryDecision("retry", delay)
