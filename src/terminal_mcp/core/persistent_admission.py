from __future__ import annotations

from collections.abc import Iterable
from contextvars import ContextVar, Token
from dataclasses import dataclass

PERSISTENT_ADMISSION_SCOPE = "terminal:execute"


class PersistentAdmissionError(PermissionError):
    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True, slots=True)
class VerifiedAdmissionContext:
    """Verified transport identity for Persistent session admission."""

    principal_id: str
    credential_id: str
    scopes: frozenset[str]
    auth_generation: int
    transport: str
    auth_mode: str

    def __post_init__(self) -> None:
        if self.auth_mode == "none":
            raise PersistentAdmissionError("persistent_auth_required")
        if not self.principal_id or not self.credential_id:
            raise PersistentAdmissionError("persistent_auth_required")
        if self.auth_generation < 1:
            raise ValueError("auth_generation must be positive")

    def require(self, scope: str = PERSISTENT_ADMISSION_SCOPE) -> VerifiedAdmissionContext:
        if scope not in self.scopes:
            raise PersistentAdmissionError("persistent_scope_required")
        return self


_current_admission: ContextVar[VerifiedAdmissionContext | None] = ContextVar(
    "terminal_mcp_persistent_admission", default=None
)


def bind_admission_context(context: VerifiedAdmissionContext | None) -> Token:
    return _current_admission.set(context)


def reset_admission_context(token: Token) -> None:
    _current_admission.reset(token)


def current_admission_context(*, required: bool = False) -> VerifiedAdmissionContext | None:
    context = _current_admission.get()
    if required and context is None:
        raise PersistentAdmissionError("persistent_auth_required")
    return context


def normalize_scopes(value: str | Iterable[str] | None) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset(part for part in value.split() if part)
    return frozenset(str(part) for part in value if str(part))
