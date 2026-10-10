"""Standard Webhooks signatures and address-pinned HTTPS callbacks."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import secrets
import socket
import ssl
import time
from urllib.parse import urlsplit


class CallbackEndpointError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def signing_key(secret: str) -> bytes:
    if not secret.startswith("whsec_"):
        raise ValueError("invalid_signing_secret")
    try:
        key = base64.b64decode(secret[6:], validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("invalid_signing_secret") from exc
    if len(key) < 24 or len(key) > 64:
        raise ValueError("invalid_signing_secret")
    return key


def signed_headers(
    secret,
    webhook_id,
    body,
    subscription_id,
    *,
    previous_secret=None,
):
    timestamp = str(int(time.time()))
    signed = webhook_id.encode() + b"." + timestamp.encode() + b"." + body
    signatures = []
    for key in (secret, previous_secret):
        if key:
            value = base64.b64encode(
                hmac.new(signing_key(key), signed, hashlib.sha256).digest()
            ).decode("ascii")
            signatures.append("v1," + value)
    return {
        "Content-Type": "application/json",
        "webhook-id": webhook_id,
        "webhook-timestamp": timestamp,
        "webhook-signature": " ".join(signatures),
        "X-MCP-Subscription-Id": subscription_id,
    }


def validate_callback_url(url):
    if not isinstance(url, str) or len(url) > 2048:
        raise CallbackEndpointError("invalid_url")
    try:
        parsed = urlsplit(url)
        port = parsed.port or 443
    except ValueError as exc:
        raise CallbackEndpointError("invalid_url") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port <= 0
        or port > 65535
    ):
        raise CallbackEndpointError("invalid_url")
    return parsed, port


def _pinned_https_post(url, body, headers):
    """Never connect to an address other than an inspected public DNS answer."""
    parsed, port = validate_callback_url(url)
    host = parsed.hostname
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, ValueError) as exc:
        raise CallbackEndpointError("dns_failed") from exc
    ip_addresses = sorted({item[4][0] for item in addresses})
    if not ip_addresses or any(not ipaddress.ip_address(ip).is_global for ip in ip_addresses):
        raise CallbackEndpointError("unsafe_address")
    # Pin the connection to the validated address. TLS still checks the original host.
    connection = http.client.HTTPSConnection(
        host, port, timeout=8, context=ssl.create_default_context()
    )
    try:
        connection.sock = socket.create_connection((ip_addresses[0], port), timeout=8)
        connection.sock = connection._context.wrap_socket(connection.sock, server_hostname=host)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        if (
            response.getheader("content-length")
            and int(response.getheader("content-length")) > 65536
        ):
            raise CallbackEndpointError("response_too_large")
        data = response.read(65537)
        if len(data) > 65536:
            raise CallbackEndpointError("response_too_large")
        return response.status, data
    except (OSError, ssl.SSLError, TimeoutError) as exc:
        raise CallbackEndpointError("transport_error") from exc
    finally:
        connection.close()


async def post_signed(
    url,
    secret,
    subscription_id,
    webhook_id,
    payload,
    *,
    sender=None,
    previous_secret=None,
):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > 262144:
        raise CallbackEndpointError("payload_too_large")
    headers = signed_headers(
        secret,
        webhook_id,
        body,
        subscription_id,
        previous_secret=previous_secret,
    )
    if sender is not None:
        return await sender(url, body, headers)
    return await asyncio.to_thread(_pinned_https_post, url, body, headers)


async def verify_callback(url, secret, subscription_id, *, sender=None):
    # Fresh, single-use challenge, bound to its signed request.
    challenge = secrets.token_urlsafe(36)
    verification_id = "msg_verification_" + secrets.token_hex(16)
    status, response_body = await post_signed(
        url,
        secret,
        subscription_id,
        verification_id,
        {"type": "verification", "challenge": challenge},
        sender=sender,
    )
    if status < 200 or status >= 300:
        raise CallbackEndpointError("challenge_failed")
    try:
        echoed = json.loads(response_body)
        got = echoed["challenge"] if isinstance(echoed, dict) else ""
    except (ValueError, KeyError, TypeError) as exc:
        raise CallbackEndpointError("challenge_failed") from exc
    if not isinstance(got, str) or not hmac.compare_digest(got, challenge):
        raise CallbackEndpointError("challenge_failed")
