import base64
import hashlib
import hmac
import time
from urllib.parse import urlencode

import jwt
from jwt import PyJWKClient


class AuthService:
    def __init__(self, s, store, credentials=None):
        self.s = s
        self.store = store
        self.credentials = credentials
        self.external_jwks = PyJWKClient(s.oauth_jwks_url) if s.oauth_jwks_url else None

    def bearer_valid(self, token):
        return (
            self.credentials.bearer_valid(token)
            if self.credentials
            else any(
                hmac.compare_digest(token, x.strip())
                for x in self.s.bearer_tokens.split(",")
                if x.strip()
            )
        )

    def oauth_user_valid(self, username, password):
        if self.credentials:
            return self.credentials.oauth_user_valid(username, password)
        return hmac.compare_digest(username, self.s.oauth_admin_username) and hmac.compare_digest(
            password, self.s.oauth_admin_password
        )

    def issue_access(self, cid, scope):
        now = int(time.time())
        aud = self.s.oauth_audience or f"{self.s.public_base_url}/mcp"
        return jwt.encode(
            {
                "iss": self.s.oauth_issuer or self.s.public_base_url,
                "sub": cid,
                "aud": aud,
                "iat": now,
                "exp": now + self.s.oauth_access_ttl_sec,
                "scope": scope,
            },
            self.s.oauth_signing_secret,
            algorithm="HS256",
        )

    async def verify_access(self, token, required):
        if self.external_jwks:
            key = self.external_jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256", "ES256"],
                audience=self.s.oauth_audience,
                issuer=self.s.oauth_issuer,
            )
        else:
            claims = jwt.decode(
                token,
                self.s.oauth_signing_secret,
                algorithms=["HS256"],
                audience=self.s.oauth_audience or f"{self.s.public_base_url}/mcp",
                issuer=self.s.oauth_issuer or self.s.public_base_url,
            )
        if not set(required).issubset(set(str(claims.get("scope", "")).split())):
            raise PermissionError("insufficient_scope")
        if not self.external_jwks and not await self.store.get_client(str(claims.get("sub", ""))):
            raise PermissionError("revoked_client")
        return claims

    @staticmethod
    def pkce_ok(verifier, challenge):
        actual = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        return hmac.compare_digest(actual, challenge)

    @staticmethod
    def redirect(uri, params):
        return f"{uri}?{urlencode(params)}"
