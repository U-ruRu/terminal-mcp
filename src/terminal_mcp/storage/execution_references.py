"""Schema 21: execution receipts/audit may refer to a verified remote identity.

The authority keeps slots and sessions. The execution node keeps its own durable
receipts. Every other authority-table FK remains enforced.
"""

_DEFINITIONS = {
    "persistent_idempotency": """logical_agent_id TEXT NOT NULL, operation TEXT NOT NULL,
        idempotency_key TEXT NOT NULL, request_fingerprint TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('pending','complete')),
        result_json TEXT, created_at TEXT NOT NULL,
        PRIMARY KEY(logical_agent_id,operation,idempotency_key)""",
    "persistent_agent_audit": """id INTEGER PRIMARY KEY AUTOINCREMENT,
        logical_agent_id TEXT NOT NULL, event_type TEXT NOT NULL,
        principal_id TEXT NOT NULL, work_session_id TEXT, session_epoch INTEGER,
        payload_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL""",
}


async def migrate_execution_references(db) -> None:
    for table, definition in _DEFINITIONS.items():
        constraints = await (await db.execute(f"PRAGMA foreign_key_list({table})")).fetchall()
        if not constraints:
            continue
        if len(constraints) != 1 or constraints[0][2:5] != (
            "logical_agents",
            "logical_agent_id",
            "logical_agent_id",
        ):
            raise RuntimeError("unexpected execution-reference schema")
        indexes = await (
            await db.execute(
                "SELECT sql FROM sqlite_master WHERE tbl_name=? "
                "AND type='index' AND sql IS NOT NULL",
                (table,),
            )
        ).fetchall()
        sequence = await (
            await db.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,))
        ).fetchone()
        temporary = table + "_v21"
        await db.execute("SAVEPOINT execution_reference_migration")
        try:
            columns = [
                r[1] for r in await (await db.execute(f"PRAGMA table_info({table})")).fetchall()
            ]
            await db.execute(f"CREATE TABLE {temporary}({definition})")
            new_columns = [
                r[1] for r in await (await db.execute(f"PRAGMA table_info({temporary})")).fetchall()
            ]
            if columns != new_columns:
                raise RuntimeError("execution-reference migration column mismatch")
            names = ",".join(columns)
            await db.execute(f"INSERT INTO {temporary}({names}) SELECT {names} FROM {table}")
            await db.execute(f"DROP TABLE {table}")
            await db.execute(f"ALTER TABLE {temporary} RENAME TO {table}")
            for (sql,) in indexes:
                await db.execute(sql)
            if sequence is not None:
                await db.execute(
                    "UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name=?", (sequence[0], table)
                )
            await db.execute("RELEASE execution_reference_migration")
        except Exception:
            await db.execute("ROLLBACK TO execution_reference_migration")
            await db.execute("RELEASE execution_reference_migration")
            raise
