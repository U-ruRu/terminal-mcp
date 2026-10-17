"""Bounded read-only probes using the runtime's supported HTTPX client."""

from __future__ import annotations

from urllib.parse import urlsplit

from terminal_mcp.deployment._release_common import ReleaseError, require


def checked_url(value: str) -> str:
    url = urlsplit(value)
    require(
        url.scheme in ("https", "http")
        and url.hostname
        and not url.username
        and not url.password
        and not url.query
        and not url.fragment,
        "unsafe_probe_url",
    )
    require(
        url.scheme == "https" or url.hostname in ("127.0.0.1", "localhost", "::1"),
        "https_required_for_remote_probe",
    )
    return value.rstrip("/")


class ProbeClient:
    def __init__(self, timeout: float = 5):
        import httpx

        self.client = httpx.Client(
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
            headers={"User-Agent": "Terminal-MCP-deployment/1"},
        )

    def close(self) -> None:
        self.client.close()

    def request(self, method: str, url: str, **kwargs):
        import httpx

        checked_url(url)
        try:
            response = self.client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise ReleaseError("probe_transport_failure") from exc
        text = response.content[:4096].lower()
        if response.headers.get("cf-mitigated", "").lower() == "challenge" or (
            response.status_code in (403, 429, 503)
            and (b"/cdn-cgi/challenge" in text or b"challenge-platform" in text)
        ):
            raise ReleaseError("probe_client_blocked_by_cdn")
        return response

    def health(
        self, url: str, expected_release: str | None = None, *, readiness: bool = False
    ) -> dict:
        url = checked_url(url)
        if readiness:
            parts = urlsplit(url)
            require(parts.path in {"/health/live", "/health/ready"}, "invalid_readiness_url")
            url = parts._replace(path="/health/ready").geturl()
        response = self.request("GET", url)
        require(response.status_code == 200, "health_http_failure")
        try:
            value = response.json()
        except ValueError as exc:
            raise ReleaseError("health_not_application_json") from exc
        require(
            value.get("ok") is True and isinstance(value.get("release"), str),
            "health_application_failure",
        )
        if readiness:
            require(value.get("ready") is True, "application_not_ready")
        if expected_release is not None:
            require(value["release"] == expected_release, "live_release_mismatch")
        return {"ok": True, "release": value["release"], "version": value.get("version")}

    def ingress(self, base: str) -> list[dict]:
        base = checked_url(base)
        # These calls carry no credentials and cannot mutate application state.
        routes = [
            ("GET", "/.well-known/oauth-authorization-server", 200),
            ("GET", "/.well-known/oauth-protected-resource", 200),
            ("GET", "/terminal-mcp/executor/v1/mcp", 401),
            ("GET", "/terminal-mcp/coordinator/v1/mcp", 401),
            ("GET", "/connect", 200),
        ]
        roles = ["access", "executor", "coordinator"]
        routes += [
            ("GET", "/terminal-mcp/access/v1/mcp", 401),
            ("GET", "/actions/access/slots", 401),
            ("GET", "/actions/access/defaults", 401),
        ]
        for role in roles:
            routes.append(
                (
                    "GET",
                    "/.well-known/oauth-protected-resource/terminal-mcp/" + role + "/v1/mcp",
                    200,
                )
            )
        results = []
        for method, path, expected in routes:
            response = self.request(method, base + path)
            require(response.status_code == expected, "ingress_route_failure:" + path)
            if path.startswith("/.well-known/"):
                try:
                    data = response.json()
                except ValueError as exc:
                    raise ReleaseError("oauth_discovery_not_json") from exc
                if "authorization-server" in path:
                    require(data.get("issuer", "").rstrip("/") == base, "oauth_issuer_mismatch")
                else:
                    require(
                        str(data.get("resource", "")).startswith(base + "/"),
                        "oauth_resource_mismatch",
                    )
            results.append({"method": method, "path": path, "status": expected})
        return results
