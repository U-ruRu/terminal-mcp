"""The release gate permits exact native conversions and protects other values."""

import base64
import hashlib
import hmac
import json
import sqlite3

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from terminal_mcp.deployment._release_common import ReleaseError
from terminal_mcp.deployment.migration_preservation import receipt_cipher
from terminal_mcp.deployment.update import preserved_rows


def seal(cipher, payload, nonce):
    clear = json.dumps(payload).encode()
    return base64.b64encode(nonce + cipher.encrypt(nonce, clear, b"request-1")).decode()


@pytest.fixture
def converted(tmp_path):
    before, after = tmp_path / "before.db", tmp_path / "after.db"
    cipher = AESGCM(bytes(range(32)))
    old = {"ok": True, "mode": "legacy", "session_number": "0427", "deadline": "keep"}
    current = dict(old, mode="temporary")
    with sqlite3.connect(before) as db:
        db.executescript("""
            CREATE TABLE work_items(id INTEGER PRIMARY KEY,title TEXT);
            INSERT INTO work_items VALUES(1,'Keep the application result');
            CREATE TABLE access_mesh_slot_replicas(
                slot_id TEXT PRIMARY KEY,kind TEXT CHECK(kind IN ('legacy','persistent')),
                policy_json TEXT,namespace_id TEXT);
            INSERT INTO access_mesh_slot_replicas
                VALUES('slot-1','legacy','{"duration_seconds":300}','mesh-x');
            CREATE TABLE access_mesh_operator_defaults(
                singleton INTEGER PRIMARY KEY,revision INTEGER,legacy_enabled INTEGER,
                policy_json TEXT);
            INSERT INTO access_mesh_operator_defaults VALUES(1,7,1,'{"duration_seconds":300}');
            CREATE TABLE access_mesh_issuer_receipts(
                request_key TEXT PRIMARY KEY,fingerprint TEXT,created_at TEXT,result_json TEXT);
            CREATE TABLE access_mesh_event_outbox(id INTEGER PRIMARY KEY,payload_json TEXT);
            INSERT INTO access_mesh_event_outbox
                VALUES(1,'{"issued_kind":"legacy","slot_id":"slot-1","revision":9}');
            CREATE TABLE control_schema(singleton INTEGER PRIMARY KEY,version INTEGER);
            INSERT INTO control_schema VALUES(1,3);
            CREATE TABLE access_policy(singleton INTEGER PRIMARY KEY,
                revision INTEGER,legacy_admission_enabled INTEGER,policy_json TEXT);
            INSERT INTO access_policy VALUES(1,3,1,'{"duration_seconds":300}');
        """)
        db.execute(
            "INSERT INTO access_mesh_issuer_receipts VALUES('request-1','fingerprint','stamp',?)",
            (seal(cipher, old, b"a" * 12),),
        )
        db.commit()
        with sqlite3.connect(after) as target:
            db.backup(target)
    with sqlite3.connect(after) as db:
        db.executescript("""
            CREATE TABLE native_slots(
                slot_id TEXT PRIMARY KEY,kind TEXT CHECK(kind IN ('temporary','persistent')),
                policy_json TEXT,namespace_id TEXT);
            INSERT INTO native_slots SELECT slot_id,
                CASE kind WHEN 'legacy' THEN 'temporary' ELSE kind END,
                policy_json,namespace_id FROM access_mesh_slot_replicas;
            DROP TABLE access_mesh_slot_replicas;
            ALTER TABLE native_slots RENAME TO access_mesh_slot_replicas;
            ALTER TABLE access_mesh_operator_defaults DROP COLUMN legacy_enabled;
            ALTER TABLE access_policy DROP COLUMN legacy_admission_enabled;
            UPDATE control_schema SET version=6;
            UPDATE access_mesh_event_outbox SET payload_json=
                '{"revision":9,"slot_id":"slot-1","issued_kind":"temporary"}';
        """)
        db.execute(
            "UPDATE access_mesh_issuer_receipts SET result_json=?",
            (seal(cipher, current, b"b" * 12),),
        )
    return before, after, cipher, current


def test_exact_enum_column_and_authenticated_receipt_conversions_pass(converted):
    before, after, cipher, _ = converted
    preserved_rows(before, after, cipher=cipher)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE work_items SET title='Lost application data'",
        "UPDATE access_mesh_slot_replicas SET policy_json='{}'",
        "UPDATE access_mesh_slot_replicas SET namespace_id='another-mesh'",
        "UPDATE access_mesh_issuer_receipts SET fingerprint='changed'",
    ],
)
def test_conversion_does_not_exempt_other_row_fields(converted, statement):
    before, after, cipher, _ = converted
    with sqlite3.connect(after) as db:
        db.execute(statement)
    with pytest.raises(ReleaseError, match="protected_state_rows_changed"):
        preserved_rows(before, after, cipher=cipher)


def test_reencrypted_receipt_cannot_change_the_issued_number(converted):
    before, after, cipher, payload = converted
    with sqlite3.connect(after) as db:
        db.execute(
            "UPDATE access_mesh_issuer_receipts SET result_json=?",
            (seal(cipher, dict(payload, session_number="9999"), b"c" * 12),),
        )
    with pytest.raises(ReleaseError, match="protected_state_rows_changed"):
        preserved_rows(before, after, cipher=cipher)


def test_only_named_obsolete_columns_can_be_removed(converted):
    before, after, cipher, _ = converted
    with sqlite3.connect(after) as db:
        db.execute("ALTER TABLE work_items DROP COLUMN title")
    with pytest.raises(ReleaseError, match="protected_state_columns_removed"):
        preserved_rows(before, after, cipher=cipher)


def test_receipt_key_preserves_utf8_proof_material(tmp_path):
    proof, node = "я" * 16, "node-a"
    (tmp_path / "node-identity.json").write_text(
        json.dumps({"node_id": node, "access_proof_key": proof, "signing_private_key": "test"})
    )
    cipher = receipt_cipher(tmp_path / "runtime.db")
    key = hmac.new(
        proof.encode(), f"access-mesh-issuer-receipt-v2:{node}".encode(), hashlib.sha256
    ).digest()
    nonce = b"n" * 12
    encrypted = AESGCM(key).encrypt(nonce, b"preserved", b"request")
    assert cipher.decrypt(nonce, encrypted, b"request") == b"preserved"


@pytest.fixture
def session_split(tmp_path):
    before, after = tmp_path / "access25.db", tmp_path / "access27.db"
    policy = json.dumps({"duration_seconds": 300})
    with sqlite3.connect(before) as db:
        db.executescript("""
            PRAGMA user_version=25;
            CREATE TABLE access_mesh_slot_replicas(
                issuer_id TEXT,slot_id TEXT,logical_agent_id TEXT,
                kind TEXT CHECK(kind IN ('legacy','persistent')),state TEXT,
                policy_json TEXT,code_tag TEXT,anchor TEXT,deadline_at TEXT,policy_window_json TEXT);
            CREATE TABLE access_mesh_event_log(
                issuer_id TEXT,slot_id TEXT,revision INTEGER,event_json TEXT);
            CREATE TABLE access_mesh_number_claims(
                issuer_id TEXT,slot_id TEXT,logical_agent_id TEXT,number TEXT);
            INSERT INTO access_mesh_number_claims VALUES('node','slot','old-agent','1234');
        """)
        db.execute(
            "INSERT INTO access_mesh_slot_replicas VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "node",
                "slot",
                "old-agent",
                "legacy",
                "active",
                policy,
                "retained-code-tag",
                "2026-10-19T11:01:00+00:00",
                None,
                '{"hard_expires_at":"2026-10-19T11:05:00+00:00"}',
            ),
        )
        for revision, kind, stamp in [
            (1, "SessionStarted", "2026-10-19T11:00:00+00:00"),
            (2, "SessionEnded", "2026-10-19T11:01:00+00:00"),
        ]:
            db.execute(
                "INSERT INTO access_mesh_event_log VALUES('node','slot',?,?)",
                (revision, json.dumps({"kind": kind, "revision": revision, "effective_at": stamp})),
            )
        db.commit()
        with sqlite3.connect(after) as copied:
            db.backup(copied)
    with sqlite3.connect(after) as db:
        db.executescript("""
            ALTER TABLE access_mesh_slot_replicas DROP COLUMN logical_agent_id;
            ALTER TABLE access_mesh_number_claims DROP COLUMN logical_agent_id;
            CREATE TABLE access_mesh_sessions(
                issuer_id TEXT,slot_id TEXT,logical_agent_id TEXT,
                active INTEGER,ended_at TEXT,active_from TEXT);
            INSERT INTO access_mesh_sessions VALUES('node','slot','old-agent',0,
                '2026-10-19T11:01:00+00:00','2026-10-19T11:00:00+00:00');
            PRAGMA ignore_check_constraints=ON;
            UPDATE access_mesh_slot_replicas SET kind='temporary',
                anchor='2026-10-19T11:00:00+00:00',deadline_at='2026-10-19T11:05:00+00:00';
            PRAGMA user_version=27;
        """)
    return before, after


def test_session_split_preserves_identity_code_and_the_original_window(session_split):
    preserved_rows(*session_split)


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE access_mesh_sessions SET logical_agent_id='different-agent'",
        "UPDATE access_mesh_sessions SET active=1",
        "UPDATE access_mesh_sessions SET ended_at=NULL",
        "UPDATE access_mesh_sessions SET active_from='2026-10-19T11:00:01+00:00'",
        "UPDATE access_mesh_slot_replicas SET deadline_at='2026-10-19T11:06:00+00:00'",
        "UPDATE access_mesh_slot_replicas SET code_tag='different-code'",
    ],
)
def test_session_split_rejects_changed_identity_code_or_window(session_split, change):
    before, after = session_split
    with sqlite3.connect(after) as db:
        db.execute(change)
    with pytest.raises(ReleaseError):
        preserved_rows(before, after)


def test_offline_v25_bridge_changes_only_the_named_enum(session_split):
    from terminal_mcp.deployment.access_bridge import prepare_access_v26

    before, _ = session_split
    with sqlite3.connect(before) as db:
        db.execute("PRAGMA user_version=27")
    prepare_access_v26(before, source_version=25)
    with sqlite3.connect(before) as db:
        assert db.execute(
            "SELECT kind,logical_agent_id,code_tag FROM access_mesh_slot_replicas"
        ).fetchone() == ("temporary", "old-agent", "retained-code-tag")
        assert (
            db.execute(
                "SELECT value FROM schema_migration_state WHERE name='repository_schema_from'"
            ).fetchone()[0]
            == 26
        )


def test_offline_v25_bridge_requires_review_for_retired_deleted_rows(session_split):
    from terminal_mcp.deployment.access_bridge import prepare_access_v26

    before, _ = session_split
    with sqlite3.connect(before) as db:
        db.execute("PRAGMA user_version=27")
        db.execute("UPDATE access_mesh_slot_replicas SET state='deleted'")
    with pytest.raises(ReleaseError, match="access_v25_deleted_registration_requires_review"):
        prepare_access_v26(before, source_version=25)
    with sqlite3.connect(before) as db:
        assert db.execute("SELECT kind FROM access_mesh_slot_replicas").fetchone()[0] == "legacy"
