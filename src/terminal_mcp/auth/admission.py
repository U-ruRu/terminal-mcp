from __future__ import annotations

import hashlib
import hmac

from terminal_mcp.core.persistent_admission import VerifiedAdmissionContext, normalize_scopes


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:20]


def verified_admission_context(
    *,
    auth_mode: str,
    token: str,
    claims: dict | None = None,
    credentials=None,
    transport: str = "http",
) -> VerifiedAdmissionContext | None:
    """Adapt an already-verified credential into a safe, non-secret audit identity."""

    if auth_mode == "none":
        return None
    if auth_mode == "oauth":
        claims = claims or {}
        subject = str(claims.get("sub") or "").strip()
        if not subject:
            return None
        principal = str(claims.get("principal_id") or subject)
        generation = int(claims.get("auth_generation") or 1)
        return VerifiedAdmissionContext(
            principal_id=principal,
            credential_id=f"oauth:{subject}",
            scopes=normalize_scopes(claims.get("scope")),
            auth_generation=generation,
            transport=transport,
            auth_mode="oauth",
        )
    if auth_mode == "bearer":
        credential_id = None
        principal_id = None
        if credentials is not None:
            for item in credentials.bearer_items():
                stored = str(item.get("token") or "")
                if stored and hmac.compare_digest(token, stored) and credentials._active(item):
                    identifier = str(item.get("id") or _fingerprint(stored))
                    credential_id = f"bearer:{identifier}"
                    principal_id = str(item.get("name") or identifier)
                    break
        if credential_id is None:
            fingerprint = _fingerprint(token)
            credential_id = f"bearer:{fingerprint}"
            principal_id = credential_id
        return VerifiedAdmissionContext(
            principal_id=principal_id,
            credential_id=credential_id,
            scopes=frozenset({"terminal:read", "terminal:execute"}),
            auth_generation=1,
            transport=transport,
            auth_mode="bearer",
        )
    return None
