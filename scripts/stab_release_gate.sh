#!/usr/bin/env bash
# Source-only critical release gate. Never deploys or accesses a live database.
set -Eeuo pipefail

usage() {
  echo "usage: $0 --repo PATH --expected-sha FULL40 --mode prepare|final [--python PYTHON] [--expected-sqlite VERSION] [--out ABS_PATH] [--collect-only]" >&2
  exit 2
}
repo=""
expected_sha=""
mode="prepare"
python_bin="python3"
expected_sqlite=""
out=""
collect=0
while (( $# )); do
  case "$1" in
    --repo) repo="$2"; shift 2 ;;
    --expected-sha) expected_sha="$2"; shift 2 ;;
    --mode) mode="$2"; shift 2 ;;
    --python) python_bin="$2"; shift 2 ;;
    --expected-sqlite) expected_sqlite="$2"; shift 2 ;;
    --out) out="$2"; shift 2 ;;
    --collect-only) collect=1; shift ;;
    *) usage ;;
  esac
done
[[ -n "$repo" && "$expected_sha" =~ ^[0-9a-f]{40}$ && "$mode" =~ ^(prepare|final)$ ]] || usage
if [[ "$mode" == final && -z "$expected_sqlite" ]]; then
  echo "BLOCKED: final mode requires --expected-sqlite (pinned release ABI)" >&2
  exit 2
fi
repo="$(realpath -e "$repo")"
actual_sha="$(git -C "$repo" rev-parse --verify HEAD)"
if [[ "$actual_sha" != "$expected_sha" ]]; then
  echo "BLOCKED: expected SHA $expected_sha, got $actual_sha" >&2
  exit 2
fi
if [[ -n "$(git -C "$repo" status --porcelain=v1 --untracked-files=all)" ]]; then
  echo "BLOCKED: checkout is dirty; freeze exact SHA before gate" >&2
  exit 2
fi
if [[ -z "$out" ]]; then
  out="$(mktemp -d /tmp/tmcp-gate-XXXXXXXX)"
else
  [[ "$out" = /* ]] || usage
  mkdir -p "$out"
  out="$(realpath -e "$out")"
fi
case "$out/" in "$repo/"*) echo "BLOCKED: gate artifacts must stay outside source checkout" >&2; exit 2 ;; esac
# Use a private short-lived path: long release artifact names can break Unix sockets.
gate_tmp="$(mktemp -d /tmp/tmcp-gate-data-XXXXXXXX)"
# Preserve release receipts/logs; discard all test temp files, including tempfile roots.
# To diagnose a specific failed gate, explicitly set TMCP_GATE_KEEP_TEST_STATE=1.
cleanup_gate_tmp() {
  if [[ "${TMCP_GATE_KEEP_TEST_STATE:-0}" != "1" ]]; then
    rm -rf --one-file-system -- "$gate_tmp"
  else
    echo "preserved_test_tmp=$gate_tmp"
  fi
}
trap cleanup_gate_tmp EXIT
exec > >(tee "$out/driver.log") 2>&1

echo "mode=$mode collect_only=$collect"
echo "source_repo=$repo"
echo "source_sha=$actual_sha"
echo "started_utc=$(date -u +%FT%TZ)"
echo "output=$out"
echo "test_tmp=$gate_tmp"
echo "python=$python_bin"
env PYTHONNOUSERSITE=1 "$python_bin" -c 'import sys,sqlite3,pytest; print("python_version="+sys.version.split()[0]); print("sqlite_abi="+sqlite3.sqlite_version); print("pytest_version="+pytest.__version__)' | tee "$out/runtime.txt"
sqlite_abi="$(env PYTHONNOUSERSITE=1 "$python_bin" -c 'import sqlite3; print(sqlite3.sqlite_version)')"
if [[ -n "$expected_sqlite" && "$sqlite_abi" != "$expected_sqlite" ]]; then
  echo "BLOCKED: SQLite ABI $sqlite_abi != requested $expected_sqlite" | tee "$out/RESULT.txt"
  exit 2
fi
# Existing committed tests only: each tests its own temporary SQLite/process/peer context.
targets=(
  tests/test_sqlite_main_health.py
  tests/test_sqlite_posix_lock_preservation.py
  tests/test_backup_manifest.py
  tests/test_session_restart.py
  tests/test_persistent_lifecycle.py
  tests/test_access_mesh_session_restart_contract.py
  tests/test_access_mesh_session_recovery_qa.py
  tests/test_command_committed_receipts.py
  tests/test_command_replay.py
  tests/test_cancel_receipt_provenance.py
  tests/test_cancel_receipt_observed_status.py
  tests/test_cancel_reconciler_real_process.py
  tests/test_command_run_fast_path.py
  tests/test_task_mcp_contract.py
  tests/test_task_create_request_receipt.py
  tests/test_task_revision_side_effects.py
  tests/test_task_checkpoint_append_boundary.py
  tests/test_task_qa_public_state.py
  tests/test_persistent_standalone_messaging.py
  tests/test_access_mesh_outbox.py
  tests/test_qa_mesh_start_authority.py
  tests/test_fleet_control_hardening.py
  tests/test_qa_mesh_real_tcp_partition.py
)
if [[ "$mode" == final ]]; then
  # Missing integrated tests are a release BLOCKER, never an implicit skip.
  targets+=(
    tests/test_local_messages_24h_victor.py
    tests/test_mcp_events.py
    tests/test_console_events.py
  )
else
  echo "PREPARE only: local 24h messaging and MCP Events source gates NOT INCLUDED; no release PASS."
fi
printf '%s\n' "${targets[@]}" > "$out/targets.txt"
sha256sum "$out/targets.txt" | tee "$out/targets.sha256"
for f in "${targets[@]}"; do
  if [[ ! -f "$repo/$f" ]]; then
    echo "BLOCKED: mandatory test missing on exact SHA: $f" | tee "$out/RESULT.txt"
    exit 2
  fi
done
export PYTHONPATH="$repo/src"
export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1
export TMPDIR="$out/tmp"
export PYTEST_ADDOPTS=""
cd "$repo"
pytest_flags=(-p no:cacheprovider -q -ra --tb=short "--basetemp=$gate_tmp/pytest")
if (( collect )); then pytest_flags+=(--collect-only); fi
echo "run=timeout 420s $python_bin -m pytest [${#targets[@]} existing test modules]"
set +e
timeout --signal=TERM --kill-after=10s 420s env TMPDIR="$gate_tmp" TMP="$gate_tmp" TEMP="$gate_tmp" "$python_bin" -m pytest "${pytest_flags[@]}" "${targets[@]}" 2>&1 | tee "$out/pytest.log"
rc=${PIPESTATUS[0]}
set -e
after_sha="$(git -C "$repo" rev-parse HEAD)"
git_dirty="$(git -C "$repo" status --porcelain=v1 --untracked-files=all)"
{
  echo "mode=$mode"
  echo "collect_only=$collect"
  echo "started_sha=$actual_sha"
  echo "finished_sha=$after_sha"
  echo "python=$python_bin"
  echo "sqlite_abi=$sqlite_abi"
  echo "pytest_exit=$rc"
  echo "finished_utc=$(date -u +%FT%TZ)"
  echo "checkout_clean_after=$([[ -z "$git_dirty" ]] && echo yes || echo no)"
  if (( rc == 0 )) && [[ "$after_sha" == "$expected_sha" && -z "$git_dirty" ]]; then
    echo "source_gate=PASS"
    if [[ "$mode" == prepare ]]; then echo "release_verdict=NOT_ASSESSED (local messages/events, real OAuth/dot, staged hosts, soak/full suite pending)"; fi
    if [[ "$mode" == final ]]; then echo "release_verdict=SOURCE_ONLY (live gates, real OAuth/dot, soak/full suite still required)"; fi
  else
    echo "source_gate=FAIL"
  fi
} | tee "$out/RESULT.txt"
[[ "$rc" -eq 0 && "$after_sha" == "$expected_sha" && -z "$git_dirty" ]] || exit 1
