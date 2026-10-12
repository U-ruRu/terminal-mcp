#!/usr/bin/env python3
"""Check two reviewed project signer functions against a public Standard Webhooks vector.

No callbacks, application startup, databases, external dependencies or downloaded
code are executed. This is not a live-delivery test. Review a changed subject file
before using this script with another version.
"""
from __future__ import annotations

import ast
import base64
import hashlib
import hmac
import json
from pathlib import Path
from types import SimpleNamespace

EXPECTED_SOURCE_SHA256 = "43720d32a3bbb3059c8668f727efa30130dd000ae30f31511fb90e19d2db3659"
EXPECTED_SIGNATURE = "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="


def main() -> None:
    root = next((p for p in Path(__file__).resolve().parents
                 if (p / "src/terminal_mcp/mcp/event_webhooks.py").is_file()), None)
    if root is None:
        raise SystemExit("Run from a Terminal MCP worktree containing the reviewed signer.")
    source = root / "src/terminal_mcp/mcp/event_webhooks.py"
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != EXPECTED_SOURCE_SHA256:
        raise SystemExit("Source changed: review it and record a new baseline before executing.")
    tree = ast.parse(raw, filename=str(source))
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name in {"signing_key", "signed_headers"}]
    if len(functions) != 2:
        raise SystemExit("Expected exactly two reviewed signer functions.")
    namespace = {"base64": base64, "hashlib": hashlib, "hmac": hmac,
                 "time": SimpleNamespace(time=lambda: 1614265330)}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)
    # Public test fixture, not a credential used by any running service.
    headers = namespace["signed_headers"](
        "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw",
        "msg_p5jXN8AQM9LWM0D4loKWxJek", b'{"test": 2432232314}', "sub_synthetic_only")
    expected = {"Content-Type": "application/json",
                "webhook-id": "msg_p5jXN8AQM9LWM0D4loKWxJek",
                "webhook-timestamp": "1614265330",
                "webhook-signature": EXPECTED_SIGNATURE,
                "X-MCP-Subscription-Id": "sub_synthetic_only"}
    if headers != expected:
        raise SystemExit("FAIL: signer output differs from the published fixed vector/profile.")
    print(json.dumps({"result": "PASS", "source_sha256": digest,
                      "scope": "two signer functions; fixed public vector",
                      "network_requests": 0, "live_dot_payload_proven": False}))


if __name__ == "__main__":
    main()
