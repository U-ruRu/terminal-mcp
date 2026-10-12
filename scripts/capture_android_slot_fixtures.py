"""Regenerate Android contract fixtures using isolated HTTP routes, never live servers.

Run with the repository test dependencies installed. Only GET projections are
serialized; credentials and mutation receipts remain inside temporary test apps.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]


def capture(destination: Path) -> None:
    # Import app/test helpers only after main() has isolated every storage path.
    from fastapi.testclient import TestClient
    from test_access_mesh_operator_http import HEADERS, internal_persistent_start
    from test_access_slot_inheritance_http import (
        change_defaults,
        create,
        defaults,
        fleet_policy,
        mesh_config,
        mutate,
        settings,
    )
    from test_android_slot_http_window_qa import T0, read_slot
    from test_android_slot_policy_continuation import set_clock

    from terminal_mcp.app import create_app

    captured = {}
    with TemporaryDirectory(prefix="tmcp-android-parity-") as root:
        app = create_app(settings(Path(root) / "standalone"))
        with TestClient(app, base_url="https://terminal.example") as client:
            set_clock(app, T0)
            issued = create(client, "contract-fixture-create")
            assert internal_persistent_start(client, app, issued["access_code"]).get("ok") is True
            captured["standalone_initial"] = {
                "ok": True,
                "slot": read_slot(client, issued["slot_id"]),
            }
            set_clock(app, T0 + timedelta(seconds=60))
            change_defaults(client, "contract-fixture-defaults", policy={"duration_seconds": 10800})
            captured["standalone_defaults"] = defaults(client)
            captured["future_slot"] = {"ok": True, "slot": read_slot(client, issued["slot_id"])}
            mutate(
                client,
                "policy",
                "contract-fixture-override",
                slot_id=issued["slot_id"],
                expected_revision=captured["future_slot"]["slot"]["revision"],
                policy={"duration_seconds": 7200},
            )
            captured["override_slot"] = {"ok": True, "slot": read_slot(client, issued["slot_id"])}
            mutate(
                client,
                "policy",
                "contract-fixture-reset",
                slot_id=issued["slot_id"],
                expected_revision=captured["override_slot"]["slot"]["revision"],
                inherit_policy=True,
                expected_parent_revision=defaults(client)["revision"],
            )
            captured["reset_slot"] = {"ok": True, "slot": read_slot(client, issued["slot_id"])}
        app = create_app(mesh_config(Path(root) / "mesh"))
        with TestClient(app, base_url="https://terminal.example") as client:
            set_clock(app, T0)
            assert (
                client.post(
                    "/actions/fleet/control/adopt",
                    headers=HEADERS,
                    json={"mesh_id": "fixture-mesh", "display_name": "Fixture"},
                )
                .json()
                .get("ok")
                is True
            )
            assert fleet_policy(client, 10800).get("ok") is True
            captured["mesh_defaults"] = defaults(client)
            issued = create(client, "contract-fixture-mesh")
            captured["mesh_slot"] = {"ok": True, "slot": read_slot(client, issued["slot_id"])}

    def check(value):
        if isinstance(value, dict):
            assert not (
                {"access_code", "code_tag", "token", "auth_token", "proof_key"} & set(value)
            )
            for item in value.values():
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)

    check(captured)
    for payload in captured.values():
        assert payload.get("slot", payload)["management_contract_version"] == 2
    dest = destination
    dest.write_text(
        "// Generated from isolated candidate HTTP GET routes, 2026-10-12. "
        "No credentials or code receipts.\n"
        + "export const nativeInheritanceFixtures = "
        + json.dumps(captured, indent=2)
        + " as const\n"
    )
    print("Captured", len(captured), "safe HTTP projections:", dest)


def main() -> None:
    with TemporaryDirectory(prefix="tmcp-fixture-env-") as directory:
        base = Path(directory)
        for key in tuple(os.environ):
            if key.startswith("TERMINAL_MCP_"):
                del os.environ[key]
        paths = {
            "DATABASE_PATH": "db.sqlite3",
            "AUTH_DATABASE_PATH": "auth.sqlite3",
            "OUTPUT_CACHE_PATH": "output.sqlite3",
            "ENV_FILE_PATH": "test.env",
            "EXECUTOR_SOCKET_PATH": "e.sock",
            "FLEET_PEER_TRANSPORTS_PATH": "peers.json",
            "MESH_VPN_STATE_DIR": "mesh-vpn",
            "CWD": "cwd",
            "LOG_PATH": "test.log",
            "RUNTIME_CONFIG_PATH": "runtime.env",
            "FLEET_CONTROL_PATH": "fleet.sqlite3",
            "FLEET_PROJECTION_PATH": "projection.sqlite3",
            "FLEET_NODE_META_PATH": "node.json",
        }
        (base / "cwd").mkdir()
        os.environ.update(
            {"TERMINAL_MCP_" + key: str(base / value) for key, value in paths.items()}
        )
        sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
        capture(ROOT / "apps/console/src/api/fixtures/androidSlotInheritance.ts")


if __name__ == "__main__":
    main()
