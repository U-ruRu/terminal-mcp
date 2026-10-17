"""Current runtime state requires the protected identity database at startup."""

import pytest

from terminal_mcp.auth.identity_migration import IdentityMigrationError, assert_identity_isolated
from terminal_mcp.storage.sqlite import SqliteRepository


@pytest.mark.asyncio
async def test_existing_runtime_refuses_missing_protected_auth(tmp_path):
    repository = SqliteRepository(tmp_path / "runtime.sqlite3", tmp_path / "output.sqlite3")
    await repository.initialize()
    await repository.create("printf pending", queue_id=1)
    with pytest.raises(IdentityMigrationError, match="protected auth database is missing"):
        assert_identity_isolated(repository.path, tmp_path / "auth.sqlite3")
