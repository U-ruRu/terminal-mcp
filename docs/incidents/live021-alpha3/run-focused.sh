#!/bin/sh
set -eu
PYTEST=${PYTEST:-/workspace/terminal-mcp/.venv-test/bin/pytest}
export PYTHONPATH=src
exec "$PYTEST" -q \
 tests/test_live021_attempt_diagnostics.py tests/test_live021_telemetry.py \
 tests/test_public_error_boundaries.py tests/test_command_committed_receipts.py \
 tests/test_command_replay.py tests/test_task_committed_receipts_v2.py \
 tests/test_task_replay_scope_v2.py tests/test_task_qa_public_state.py \
 tests/test_qa_complete_task_mcp_e2e.py tests/test_role_mcp_v1.py \
 tests/test_role_wire_runtime.py tests/test_role_output_planning.py \
 tests/test_role_tool_annotations_review.py tests/test_sqlite_observability.py "$@"
