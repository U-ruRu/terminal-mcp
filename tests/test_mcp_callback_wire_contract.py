"""Exercise the real HTTPS sender against a loopback-only raw-wire receiver.

Only test-local name/address selection and fixture-CA trust are replaced.
No production serialization, signing, http.client request, or response code is
replaced. This does not verify public DNS, SSRF behavior, or platform acceptance.
"""

from __future__ import annotations

import base64
import json
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from standardwebhooks.webhooks import Webhook, WebhookVerificationError

from terminal_mcp.mcp import event_webhooks as webhooks

HOST = "callback.example.invalid"
# Synthetic public DNS answer, never contacted. The test socket adapter asserts
# this selected destination, then connects exclusively to the loopback listener.
PUBLIC_ANSWER = "8.8.8.8"
SECRET = "whsec_" + base64.b64encode(bytes(range(32))).decode("ascii")
WRONG_SECRET = "whsec_" + base64.b64encode(bytes(range(32, 64))).decode("ascii")
SUBSCRIPTION_ID = "sub_wire_fixture"
EVENT_ID = "evt_wire_fixture"


@dataclass
class CapturedRequest:
    raw: bytes
    request_line: bytes
    headers: dict[str, str]
    body: bytes


def _read_request(connection):
    raw = b""
    while b"\r\n\r\n" not in raw:
        block = connection.recv(4096)
        if not block:
            raise AssertionError("EOF before HTTP headers")
        raw += block
        assert len(raw) <= 32768
    head, body = raw.split(b"\r\n\r\n", 1)
    lines = head.split(b"\r\n")
    headers = {}
    for line in lines[1:]:
        name, value = line.split(b":", 1)
        name = name.decode("ascii").lower()
        assert name not in headers, "duplicate header"
        headers[name] = value.strip().decode("ascii")
    assert "transfer-encoding" not in headers
    length = int(headers["content-length"])
    assert 0 <= length <= 262144
    while len(body) < length:
        block = connection.recv(length - len(body))
        if not block:
            raise AssertionError("EOF before declared body length")
        body += block
    assert len(body) == length
    return CapturedRequest(head + b"\r\n\r\n" + body, lines[0], headers, body)


def _tls_contexts(tmp_path):
    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Ephemeral wire fixture CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False)
        .add_extension(
            x509.KeyUsage(True, False, False, False, False, True, True, None, None), True
        )
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOST)]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
        .add_extension(
            x509.KeyUsage(True, False, False, False, False, False, False, None, None), True
        )
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOST)]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    cert_path = tmp_path / "fixture-cert.pem"
    key_path = tmp_path / "fixture-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    client_context = ssl.create_default_context()
    client_context.load_verify_locations(
        cadata=ca.public_bytes(serialization.Encoding.PEM).decode()
    )
    assert client_context.check_hostname
    assert client_context.verify_mode == ssl.CERT_REQUIRED
    return server_context, client_context


class RawTLSReceiver:
    def __init__(self, server_context, secret=SECRET, *, wrong_challenge=False):
        self.capture = None
        self.verification_error = None
        self.error = None
        self.sni = []
        self.allow_tls_error = False
        self.secret = secret
        self.wrong_challenge = wrong_challenge
        self.context = server_context
        server_context.set_servername_callback(lambda _sock, name, _ctx: self.sni.append(name))
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.listener.settimeout(5)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(
            target=self._serve, name="callback-wire-fixture", daemon=True
        )
        self.thread.start()

    def _serve(self):
        try:
            with self.listener.accept()[0] as plain:
                plain.settimeout(5)
                with self.context.wrap_socket(plain, server_side=True) as connection:
                    self.capture = _read_request(connection)
                    try:
                        payload = Webhook(self.secret).verify(
                            self.capture.body, self.capture.headers
                        )
                    except WebhookVerificationError as exc:
                        self.verification_error = exc
                        status, response = 401, b'{"error":"signature_rejected"}'
                    else:
                        status = 200
                        response_payload = {}
                        if "challenge" in payload:
                            response_payload["challenge"] = (
                                "wrong-fixture-challenge"
                                if self.wrong_challenge
                                else payload["challenge"]
                            )
                        response = json.dumps(response_payload).encode("utf-8")
                    reason = "OK" if status == 200 else "Unauthorized"
                    connection.sendall(
                        f"HTTP/1.1 {status} {reason}\r\n"
                        "Content-Type: application/json\r\n"
                        f"Content-Length: {len(response)}\r\n"
                        "Connection: close\r\n\r\n".encode("ascii")
                        + response
                    )
        except Exception as exc:
            self.error = exc

    def close(self):
        self.listener.close()
        self.thread.join(timeout=6)
        assert not self.thread.is_alive(), "fixture listener did not stop"
        if self.allow_tls_error:
            assert isinstance(self.error, ssl.SSLError)
        else:
            assert self.error is None, repr(self.error)


@pytest.fixture
def wire_receiver(monkeypatch, tmp_path):
    receivers = []

    def make(*, port=443, secret=SECRET, wrong_challenge=False, trust_ca=True, host=HOST):
        server_context, client_context = _tls_contexts(tmp_path)
        if not trust_ca:
            client_context = ssl.create_default_context()
        receiver = RawTLSReceiver(server_context, secret, wrong_challenge=wrong_challenge)
        receiver.allow_tls_error = not trust_ca or host != HOST
        receivers.append(receiver)

        def getaddrinfo(request_host, request_port, *, type):
            assert (request_host, request_port, type) == (host, port, socket.SOCK_STREAM)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_ANSWER, port))]

        def create_connection(address, timeout):
            assert address == (PUBLIC_ANSWER, port)
            return socket.create_connection(("127.0.0.1", receiver.port), timeout=timeout)

        # Module-local facades avoid modifying global socket/ssl functions.
        monkeypatch.setattr(
            webhooks,
            "socket",
            SimpleNamespace(
                SOCK_STREAM=socket.SOCK_STREAM,
                getaddrinfo=getaddrinfo,
                create_connection=create_connection,
            ),
        )
        monkeypatch.setattr(
            webhooks,
            "ssl",
            SimpleNamespace(create_default_context=lambda: client_context, SSLError=ssl.SSLError),
        )
        return receiver

    try:
        yield make
    finally:
        for receiver in receivers:
            receiver.close()


def _expected_wire(target, host, body, message_id, timestamp, signature):
    return (
        f"POST {target} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Accept-Encoding: identity\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Content-Type: application/json\r\n"
        f"webhook-id: {message_id}\r\n"
        f"webhook-timestamp: {timestamp}\r\n"
        f"webhook-signature: {signature}\r\n"
        f"X-MCP-Subscription-Id: {SUBSCRIPTION_ID}\r\n\r\n"
    ).encode("ascii") + body


@pytest.mark.parametrize(
    ("authority", "port", "target", "expected_host"),
    [
        (HOST, 443, "/", HOST),
        (HOST + ":443", 443, "/callbacks/%2Fverify?tag=a%2Bb&mode=wire", HOST),
        (HOST + ":8443", 8443, "/callbacks?mode=wire", HOST + ":8443"),
    ],
)
async def test_full_https_wire_and_reference_verifier(
    monkeypatch, wire_receiver, authority, port, target, expected_host
):
    receiver = wire_receiver(port=port)
    timestamp = int(time.time())
    monkeypatch.setattr(webhooks, "time", SimpleNamespace(time=lambda: timestamp))
    payload = {"type": "command.completed", "message": "Проверка 🧪", "data": [True, None, 7]}
    # Independently specified bytes, not reserialization with the production function.
    body = '{"type":"command.completed","message":"Проверка 🧪","data":[true,null,7]}'.encode()
    signature = Webhook(SECRET).sign(
        EVENT_ID, datetime.fromtimestamp(timestamp, UTC), body.decode()
    )
    assert await webhooks.post_signed(
        "https://" + authority + target, SECRET, SUBSCRIPTION_ID, EVENT_ID, payload
    ) == (200, b"{}")
    receiver.thread.join(timeout=2)
    capture = receiver.capture
    assert capture is not None
    assert capture.raw == _expected_wire(
        target, expected_host, body, EVENT_ID, timestamp, signature
    )
    assert len(body) > len(body.decode("utf-8"))
    assert capture.headers["content-length"] == str(len(body))
    assert receiver.sni == [HOST]
    assert receiver.verification_error is None
    assert Webhook(SECRET).verify(capture.body, capture.headers) == payload

    # Independent verifier rejection of changed captured bytes/identity.
    with pytest.raises(WebhookVerificationError, match="No matching signature"):
        Webhook(WRONG_SECRET).verify(capture.body, capture.headers)
    with pytest.raises(WebhookVerificationError, match="No matching signature"):
        Webhook(SECRET).verify(capture.body + b" ", capture.headers)
    with pytest.raises(WebhookVerificationError, match="No matching signature"):
        Webhook(SECRET).verify(capture.body, {**capture.headers, "webhook-id": "different"})


@pytest.mark.parametrize("wrong_challenge", [False, True])
async def test_verification_challenge_round_trip(monkeypatch, wire_receiver, wrong_challenge):
    receiver = wire_receiver(wrong_challenge=wrong_challenge)
    challenge = "dummy_fixture_challenge"
    message_id = "msg_verification_" + "a" * 32
    timestamp = int(time.time())
    monkeypatch.setattr(
        webhooks, "time", SimpleNamespace(time=lambda: timestamp, monotonic=time.monotonic)
    )
    monkeypatch.setattr(
        webhooks,
        "secrets",
        SimpleNamespace(token_urlsafe=lambda _n: challenge, token_hex=lambda _n: "a" * 32),
    )
    observations = []
    if wrong_challenge:
        with pytest.raises(webhooks.CallbackEndpointError, match="challenge_failed"):
            await webhooks.verify_callback(
                "https://" + HOST + ":443/verify?fixture=1",
                SECRET,
                SUBSCRIPTION_ID,
                observer=observations.append,
            )
    else:
        await webhooks.verify_callback(
            "https://" + HOST + ":443/verify?fixture=1",
            SECRET,
            SUBSCRIPTION_ID,
            observer=observations.append,
        )
    receiver.thread.join(timeout=2)
    capture = receiver.capture
    body = ('{"type":"verification","challenge":"' + challenge + '"}').encode()
    signature = Webhook(SECRET).sign(
        message_id, datetime.fromtimestamp(timestamp, UTC), body.decode()
    )
    assert capture.raw == _expected_wire(
        "/verify?fixture=1", HOST, body, message_id, timestamp, signature
    )
    assert receiver.verification_error is None
    assert observations[-1]["result"] == ("challenge_mismatch" if wrong_challenge else "verified")
    assert observations[-1]["challenge_matches"] is not wrong_challenge
    assert observations[-1]["callback_explicit_default_port"] is True


@pytest.mark.parametrize("case", ["wrong_key", "stale_timestamp", "future_timestamp"])
async def test_reference_receiver_rejects_invalid_signed_request(monkeypatch, wire_receiver, case):
    receiver = wire_receiver(secret=WRONG_SECRET if case == "wrong_key" else SECRET)
    offset = {"wrong_key": 0, "stale_timestamp": -600, "future_timestamp": 600}[case]
    monkeypatch.setattr(webhooks, "time", SimpleNamespace(time=lambda: int(time.time()) + offset))
    status, _body = await webhooks.post_signed(
        "https://" + HOST + "/negative", SECRET, SUBSCRIPTION_ID, EVENT_ID, {"fixture": True}
    )
    assert status == 401
    receiver.thread.join(timeout=2)
    assert receiver.capture is not None
    assert isinstance(receiver.verification_error, WebhookVerificationError)
    expected_error = {
        "wrong_key": "No matching signature",
        "stale_timestamp": "timestamp too old",
        "future_timestamp": "timestamp too new",
    }[case]
    assert expected_error in str(receiver.verification_error)


@pytest.mark.parametrize("case", ["untrusted_ca", "wrong_hostname"])
async def test_fixture_tls_still_verifies_ca_and_hostname(wire_receiver, case):
    host = "other.example.invalid" if case == "wrong_hostname" else HOST
    receiver = wire_receiver(trust_ca=case != "untrusted_ca", host=host)
    with pytest.raises(webhooks.CallbackEndpointError, match="transport_error"):
        await webhooks.post_signed(
            "https://" + host + "/tls-negative", SECRET, SUBSCRIPTION_ID, EVENT_ID, {}
        )
    assert receiver.capture is None


def test_reference_known_vector_and_production_signature(monkeypatch):
    # Published upstream Python test_sign_function vector (all values are dummy):
    # https://github.com/standard-webhooks/standard-webhooks/blob/main/libraries/python/tests/test_webhooks.py
    secret = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
    message_id = "msg_p5jXN8AQM9LWM0D4loKWxJek"
    timestamp = 1614265330
    body = b'{"test": 2432232314}'
    expected = "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="
    monkeypatch.setattr(webhooks, "time", SimpleNamespace(time=lambda: timestamp))
    assert (
        Webhook(secret).sign(message_id, datetime.fromtimestamp(timestamp, UTC), body.decode())
        == expected
    )
    assert (
        webhooks.signed_headers(secret, message_id, body, SUBSCRIPTION_ID)["webhook-signature"]
        == expected
    )
