#!/usr/bin/env bash
set -euo pipefail
CMD=${1:-install}
ROOT=${TERMINAL_MCP_INSTALL_ROOT:-/opt/terminal-mcp}
ENV_DIR=${TERMINAL_MCP_ENV_DIR:-/etc/terminal-mcp}
ENV_FILE=$ENV_DIR/terminal-mcp.env
DATA=${TERMINAL_MCP_DATA_DIR:-/var/lib/terminal-mcp}
CACHE=${TERMINAL_MCP_CACHE_DIR:-/var/cache/terminal-mcp}
BACKUPS=${TERMINAL_MCP_BACKUP_DIR:-/var/backups/terminal-mcp}
UNIT_FILE=${TERMINAL_MCP_UNIT_FILE:-/etc/systemd/system/terminal-mcp.service}
CLI_LINK=${TERMINAL_MCP_CLI_LINK:-/usr/local/bin/terminal-mcp}
SYSTEMCTL=${TERMINAL_MCP_SYSTEMCTL:-systemctl}
HEALTH_URL=${TERMINAL_MCP_HEALTH_URL:-http://127.0.0.1:8080/health/live}
HEALTH_TIMEOUT_SEC=${TERMINAL_MCP_ACTIVATION_HEALTH_TIMEOUT_SEC:-600}
PUBLIC_INGRESS_TIMEOUT_SEC=${TERMINAL_MCP_PUBLIC_INGRESS_TIMEOUT_SEC:-10}
SOURCE=$(cd "$(dirname "$0")/.." && pwd)
SQLITE_VERSION=3.53.4
SQLITE_SOURCE_ID=3530400
SQLITE_SOURCE_URL=https://www.sqlite.org/2026/sqlite-autoconf-3530400.tar.gz
SQLITE_SOURCE_SHA3_256=454e45f61c6bd75b7420e7190732dea03ce6639c63ada47bbc592f67fc340338
[ "$(id -u)" -eq 0 ] || { echo 'Run as root'; exit 1; }
write_env(){
  [ -n "${TERMINAL_MCP_ADMIN_PASSWORD:-}" ] || { echo 'Set TERMINAL_MCP_ADMIN_PASSWORD'; exit 1; }
  mkdir -p "$ENV_DIR"
  cat >"$ENV_FILE" <<ENV
TERMINAL_MCP_HOST="127.0.0.1"
TERMINAL_MCP_PORT="8080"
TERMINAL_MCP_PUBLIC_BASE_URL="${TERMINAL_MCP_PUBLIC_BASE_URL:-https://server-a.example.invalid}"
TERMINAL_MCP_CONSOLE_ALLOWED_ORIGINS="${TERMINAL_MCP_CONSOLE_ALLOWED_ORIGINS:-}"
TERMINAL_MCP_ENV_FILE_PATH="$ENV_FILE"
TERMINAL_MCP_DATABASE_PATH="$DATA/terminal-mcp.sqlite3"
TERMINAL_MCP_AUTH_DATABASE_PATH="$DATA/auth.sqlite3"
TERMINAL_MCP_OUTPUT_CACHE_PATH="$CACHE/output.sqlite3"
TERMINAL_MCP_OUTPUT_LINE_MAX_BYTES="${TERMINAL_MCP_OUTPUT_LINE_MAX_BYTES:-4194304}"
TERMINAL_MCP_OUTPUT_COMMAND_MAX_BYTES="${TERMINAL_MCP_OUTPUT_COMMAND_MAX_BYTES:-8388608}"
TERMINAL_MCP_OUTPUT_RETENTION_TARGET_BYTES="${TERMINAL_MCP_OUTPUT_RETENTION_TARGET_BYTES:-201326592}"
TERMINAL_MCP_OUTPUT_RETENTION_MAX_BYTES="${TERMINAL_MCP_OUTPUT_RETENTION_MAX_BYTES:-268435456}"
TERMINAL_MCP_OUTPUT_RETENTION_MAX_ROWS="${TERMINAL_MCP_OUTPUT_RETENTION_MAX_ROWS:-1000000}"
TERMINAL_MCP_CWD="/"
TERMINAL_MCP_TERMINAL_USER="root"
TERMINAL_MCP_HEALTH_COMMAND=""
TERMINAL_MCP_MCP_AUTH_MODE="oauth"
TERMINAL_MCP_ACTIONS_AUTH_MODE="bearer"
TERMINAL_MCP_ADMIN_USERNAME="${TERMINAL_MCP_ADMIN_USERNAME:-operator}"
TERMINAL_MCP_ADMIN_PASSWORD="${TERMINAL_MCP_ADMIN_PASSWORD}"
TERMINAL_MCP_ADMIN_SESSION_SECRET="$(openssl rand -hex 32)"
TERMINAL_MCP_OAUTH_SIGNING_SECRET="$(openssl rand -hex 32)"
TERMINAL_MCP_OAUTH_REQUIRED_SCOPES="${TERMINAL_MCP_OAUTH_REQUIRED_SCOPES:-terminal:read terminal:execute}"
TERMINAL_MCP_OAUTH_ACCESS_TTL_SEC="${TERMINAL_MCP_OAUTH_ACCESS_TTL_SEC:-2592000}"
TERMINAL_MCP_BEARER_CREDENTIALS_JSON="[]"
TERMINAL_MCP_OAUTH_USERS_JSON="[]"
TERMINAL_MCP_AGENT_IDLE_TTL_SEC="${TERMINAL_MCP_AGENT_IDLE_TTL_SEC:-300}"
TERMINAL_MCP_AGENT_INTENT_TTL_SEC="${TERMINAL_MCP_AGENT_INTENT_TTL_SEC:-180}"
TERMINAL_MCP_AGENT_MAX_SESSION_SEC="${TERMINAL_MCP_AGENT_MAX_SESSION_SEC:-1500}"
TERMINAL_MCP_AGENT_SESSION_WARNING_AFTER_SEC="${TERMINAL_MCP_AGENT_SESSION_WARNING_AFTER_SEC:-1200}"
TERMINAL_MCP_AGENT_SESSION_ALERT_ENABLED="${TERMINAL_MCP_AGENT_SESSION_ALERT_ENABLED:-true}"
TERMINAL_MCP_AGENT_SESSION_ALERT_AFTER_SEC="${TERMINAL_MCP_AGENT_SESSION_ALERT_AFTER_SEC:-1380}"
TERMINAL_MCP_AGENT_SESSION_ALERT_REPEAT_SEC="${TERMINAL_MCP_AGENT_SESSION_ALERT_REPEAT_SEC:-60}"
TERMINAL_MCP_AGENT_SESSION_ALERT_MESSAGE="${TERMINAL_MCP_AGENT_SESSION_ALERT_MESSAGE:-Ваша сессия закончилась. У пользователя для вас новая задача. Завершите сессию и немедленно вернитесь в чат к пользователю, чтобы дать ему промежуточный отчёт, получить дальнейшие указания и новую задачу.}"
TERMINAL_MCP_AGENT_EVENT_WINDOW_SEC="${TERMINAL_MCP_AGENT_EVENT_WINDOW_SEC:-180}"
TERMINAL_MCP_AGENT_COMMAND_PREVIEW_CHARS="${TERMINAL_MCP_AGENT_COMMAND_PREVIEW_CHARS:-160}"
TERMINAL_MCP_AGENT_HISTORY_DEFAULT_MINUTES="${TERMINAL_MCP_AGENT_HISTORY_DEFAULT_MINUTES:-60}"
TERMINAL_MCP_MESSAGE_REMINDER_SEC="${TERMINAL_MCP_MESSAGE_REMINDER_SEC:-180}"
TERMINAL_MCP_MESSAGE_REMINDER_CALLS="${TERMINAL_MCP_MESSAGE_REMINDER_CALLS:-5}"
TERMINAL_MCP_MAX_ACTIVE_AGENTS="${TERMINAL_MCP_MAX_ACTIVE_AGENTS:-8}"
TERMINAL_MCP_QUEUE_WORKERS="${TERMINAL_MCP_QUEUE_WORKERS:-4}"
TERMINAL_MCP_QUEUE_RECONCILE_SEC="${TERMINAL_MCP_QUEUE_RECONCILE_SEC:-1.0}"
TERMINAL_MCP_PERSISTENT_AGENTS_ENABLED="${TERMINAL_MCP_PERSISTENT_AGENTS_ENABLED:-true}"
TERMINAL_MCP_PERSISTENT_SESSION_DURATION_SEC="${TERMINAL_MCP_PERSISTENT_SESSION_DURATION_SEC:-1380}"
TERMINAL_MCP_PERSISTENT_SESSION_WARNING_AFTER_SEC="${TERMINAL_MCP_PERSISTENT_SESSION_WARNING_AFTER_SEC:-1200}"
TERMINAL_MCP_PERSISTENT_SESSION_ALERT_AFTER_SEC="${TERMINAL_MCP_PERSISTENT_SESSION_ALERT_AFTER_SEC:-1320}"
TERMINAL_MCP_LEGACY_AGENT_ADMISSION_ENABLED="${TERMINAL_MCP_LEGACY_AGENT_ADMISSION_ENABLED:-false}"
ENV
  chmod 600 "$ENV_FILE"
}
ensure_env_defaults(){
  [ -f "$ENV_FILE" ] || return 0
  ensure_env(){ key=$1; value=$2; grep -q "^${key}=" "$ENV_FILE" || printf '%s="%s"\n' "$key" "$value" >>"$ENV_FILE"; }
  ensure_env TERMINAL_MCP_CONSOLE_ALLOWED_ORIGINS "${TERMINAL_MCP_CONSOLE_ALLOWED_ORIGINS:-}"
  ensure_env TERMINAL_MCP_OUTPUT_CACHE_PATH "$CACHE/output.sqlite3"
  ensure_env TERMINAL_MCP_AUTH_DATABASE_PATH "$DATA/auth.sqlite3"
  ensure_env TERMINAL_MCP_OAUTH_REQUIRED_SCOPES "${TERMINAL_MCP_OAUTH_REQUIRED_SCOPES:-terminal:read terminal:execute}"
  ensure_env TERMINAL_MCP_OAUTH_ACCESS_TTL_SEC "${TERMINAL_MCP_OAUTH_ACCESS_TTL_SEC:-2592000}"
  ensure_env TERMINAL_MCP_OUTPUT_LINE_MAX_BYTES "${TERMINAL_MCP_OUTPUT_LINE_MAX_BYTES:-4194304}"
  ensure_env TERMINAL_MCP_OUTPUT_COMMAND_MAX_BYTES "${TERMINAL_MCP_OUTPUT_COMMAND_MAX_BYTES:-8388608}"
  ensure_env TERMINAL_MCP_OUTPUT_RETENTION_TARGET_BYTES "${TERMINAL_MCP_OUTPUT_RETENTION_TARGET_BYTES:-201326592}"
  ensure_env TERMINAL_MCP_OUTPUT_RETENTION_MAX_BYTES "${TERMINAL_MCP_OUTPUT_RETENTION_MAX_BYTES:-268435456}"
  ensure_env TERMINAL_MCP_OUTPUT_RETENTION_MAX_ROWS "${TERMINAL_MCP_OUTPUT_RETENTION_MAX_ROWS:-1000000}"
  ensure_env TERMINAL_MCP_AGENT_SESSION_WARNING_AFTER_SEC "${TERMINAL_MCP_AGENT_SESSION_WARNING_AFTER_SEC:-1200}"
  ensure_env TERMINAL_MCP_AGENT_SESSION_ALERT_ENABLED "${TERMINAL_MCP_AGENT_SESSION_ALERT_ENABLED:-true}"
  ensure_env TERMINAL_MCP_AGENT_SESSION_ALERT_AFTER_SEC "${TERMINAL_MCP_AGENT_SESSION_ALERT_AFTER_SEC:-1380}"
  ensure_env TERMINAL_MCP_AGENT_SESSION_ALERT_REPEAT_SEC "${TERMINAL_MCP_AGENT_SESSION_ALERT_REPEAT_SEC:-60}"
  ensure_env TERMINAL_MCP_AGENT_SESSION_ALERT_MESSAGE "${TERMINAL_MCP_AGENT_SESSION_ALERT_MESSAGE:-Ваша сессия закончилась. У пользователя для вас новая задача. Завершите сессию и немедленно вернитесь в чат к пользователю, чтобы дать ему промежуточный отчёт, получить дальнейшие указания и новую задачу.}"
  ensure_env TERMINAL_MCP_PERSISTENT_AGENTS_ENABLED "${TERMINAL_MCP_PERSISTENT_AGENTS_ENABLED:-true}"
  ensure_env TERMINAL_MCP_PERSISTENT_SESSION_DURATION_SEC "${TERMINAL_MCP_PERSISTENT_SESSION_DURATION_SEC:-1380}"
  ensure_env TERMINAL_MCP_PERSISTENT_SESSION_WARNING_AFTER_SEC "${TERMINAL_MCP_PERSISTENT_SESSION_WARNING_AFTER_SEC:-1200}"
  ensure_env TERMINAL_MCP_PERSISTENT_SESSION_ALERT_AFTER_SEC "${TERMINAL_MCP_PERSISTENT_SESSION_ALERT_AFTER_SEC:-1320}"
  ensure_env TERMINAL_MCP_LEGACY_AGENT_ADMISSION_ENABLED "${TERMINAL_MCP_LEGACY_AGENT_ADMISSION_ENABLED:-false}"
  chmod 600 "$ENV_FILE"
}

check_public_fleet_ingress(){
  [ -f "$ENV_FILE" ] || { echo "Missing env file: $ENV_FILE" >&2; return 1; }
  public_base_url=$(
    sed -n 's/^TERMINAL_MCP_PUBLIC_BASE_URL="\(.*\)"$/\1/p' "$ENV_FILE" | tail -n 1
  )
  [ -n "$public_base_url" ] || { echo "TERMINAL_MCP_PUBLIC_BASE_URL is not configured" >&2; return 1; }
  ingress_url="${public_base_url%/}/internal/fleet/identities"
  ingress_status=$(
    curl --max-time "$PUBLIC_INGRESS_TIMEOUT_SEC" -sS -o /dev/null -w '%{http_code}' "$ingress_url" || true
  )
  if [ "$ingress_status" != "401" ]; then
    echo "Public fleet ingress check failed: $ingress_url returned ${ingress_status:-request_error}, expected 401" >&2
    echo "Ensure the reverse proxy forwards /internal/fleet/* to Terminal MCP without bypassing fleet authentication." >&2
    return 1
  fi
}

write_unit(){ mkdir -p "$(dirname "$UNIT_FILE")"; cat >"$UNIT_FILE" <<UNIT
[Unit]
Description=terminal-mcp
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=root
Group=root
WorkingDirectory=/
EnvironmentFile=$ENV_FILE
ExecStart=$ROOT/current/bin/terminal-mcp
Restart=always
RestartSec=2
[Install]
WantedBy=multi-user.target
UNIT
}
verify_sqlite_source(){
  local archive=$1
  local got
  got=$(
    python3 - "$archive" <<'PYHASH'
import hashlib
import sys

with open(sys.argv[1], "rb") as source:
    print(hashlib.sha3_256(source.read()).hexdigest())
PYHASH
  )
  [ "$got" = "$SQLITE_SOURCE_SHA3_256" ]
}

runtime_python(){
  local release=$1
  local loader
  shift
  loader=$(cat "$release/lib/terminal-mcp-native/loader.path")
  "$loader" --library-path "$release/lib/terminal-mcp-native" "$release/bin/python" "$@"
}

build_sqlite_runtime(){
  local release=$1
  local tool cache_dir archive tmp_archive native build_dir python_real loader actual src
  for tool in cc curl readelf tar; do
    command -v "$tool" >/dev/null 2>&1 || {
      echo "SQLite runtime build requires $tool" >&2
      return 1
    }
  done

  cache_dir=$ROOT/build-cache/sqlite
  archive=$cache_dir/sqlite-autoconf-$SQLITE_SOURCE_ID.tar.gz
  mkdir -p "$cache_dir"
  if [ -f "$archive" ] && ! verify_sqlite_source "$archive"; then
    echo "Cached SQLite source checksum mismatch; redownloading" >&2
    rm -f "$archive"
  fi
  if [ ! -f "$archive" ]; then
    tmp_archive=$archive.tmp.$$
    rm -f "$tmp_archive"
    if ! curl --proto '=https' --tlsv1.2 -fsSL --retry 3 "$SQLITE_SOURCE_URL" -o "$tmp_archive"; then
      rm -f "$tmp_archive"
      return 1
    fi
    if ! verify_sqlite_source "$tmp_archive"; then
      echo "SQLite source checksum mismatch" >&2
      rm -f "$tmp_archive"
      return 1
    fi
    mv "$tmp_archive" "$archive"
  fi

  native=$release/lib/terminal-mcp-native
  mkdir -p "$native"
  build_dir=$(mktemp -d)
  (
    trap 'rm -rf "$build_dir"' EXIT
    tar -xzf "$archive" -C "$build_dir"
    src=$build_dir/sqlite-autoconf-$SQLITE_SOURCE_ID/sqlite3.c
    [ -f "$src" ] || { echo "SQLite amalgamation missing sqlite3.c" >&2; exit 1; }
    cc -O2 -fPIC \
      -DSQLITE_THREADSAFE=1 \
      -DSQLITE_ENABLE_COLUMN_METADATA \
      -DSQLITE_ENABLE_DBPAGE_VTAB \
      -DSQLITE_ENABLE_DBSTAT_VTAB \
      -DSQLITE_ENABLE_FTS3 \
      -DSQLITE_ENABLE_FTS3_PARENTHESIS \
      -DSQLITE_ENABLE_FTS3_TOKENIZER \
      -DSQLITE_ENABLE_FTS4 \
      -DSQLITE_ENABLE_FTS5 \
      -DSQLITE_ENABLE_MATH_FUNCTIONS \
      -DSQLITE_ENABLE_PREUPDATE_HOOK \
      -DSQLITE_ENABLE_RTREE \
      -DSQLITE_ENABLE_SESSION \
      -DSQLITE_ENABLE_STMTVTAB \
      -DSQLITE_ENABLE_UNLOCK_NOTIFY \
      -DSQLITE_MAX_VARIABLE_NUMBER=250000 \
      -DSQLITE_SECURE_DELETE \
      -DSQLITE_USE_URI \
      -shared -Wl,-soname,libsqlite3.so.0 \
      -o "$native/libsqlite3.so.0" "$src" -ldl -lpthread -lm
  )

  python_real=$(readlink -f "$release/bin/python")
  loader=$(
    LC_ALL=C readelf -l "$python_real" |
      sed -n 's/.*Requesting program interpreter: \(.*\)]/\1/p' |
      head -n 1
  )
  [ -n "$loader" ] && [ -x "$loader" ] || {
    echo "Could not determine executable loader for $python_real" >&2
    return 1
  }
  printf '%s\n' "$loader" >"$native/loader.path"

  mv "$release/bin/terminal-mcp" "$release/bin/terminal-mcp-python"
  cat >"$release/bin/terminal-mcp" <<EOF
#!/usr/bin/env bash
set -euo pipefail
SELF_DIR=\$(cd "\$(dirname "\$0")" && pwd)
RELEASE=\$(cd "\$SELF_DIR/.." && pwd)
exec "$loader" --library-path "\$RELEASE/lib/terminal-mcp-native" \
  "\$SELF_DIR/python" "\$SELF_DIR/terminal-mcp-python" "\$@"
EOF
  chmod 0755 "$release/bin/terminal-mcp"

  actual=$(runtime_python "$release" -c 'import sqlite3; print(sqlite3.sqlite_version)')
  if [ "$actual" != "$SQLITE_VERSION" ]; then
    echo "Staged SQLite runtime mismatch: expected $SQLITE_VERSION, got $actual" >&2
    return 1
  fi
  echo "Staged Terminal MCP SQLite runtime: $actual" >&2
}

stage(){
  STAGED_RELEASE=$ROOT/releases/$(date -u +%Y%m%dT%H%M%SZ)
  if ! python3 -m venv "$STAGED_RELEASE"; then
    rm -rf "$STAGED_RELEASE"
    return 1
  fi
  if ! "$STAGED_RELEASE/bin/pip" install --upgrade pip >&2; then
    rm -rf "$STAGED_RELEASE"
    return 1
  fi
  if ! "$STAGED_RELEASE/bin/pip" install "$SOURCE" >&2; then
    rm -rf "$STAGED_RELEASE"
    return 1
  fi
  if [ ! -x "$STAGED_RELEASE/bin/terminal-mcp" ]; then
    echo "Staged release is missing bin/terminal-mcp" >&2
    rm -rf "$STAGED_RELEASE"
    return 1
  fi
  if ! build_sqlite_runtime "$STAGED_RELEASE"; then
    echo "Staged SQLite runtime build failed" >&2
    rm -rf "$STAGED_RELEASE"
    return 1
  fi
  if ! runtime_python "$STAGED_RELEASE" -c \
    'from mcp.server.transport_security import TransportSecuritySettings; import terminal_mcp.app' \
    >&2; then
    echo "Staged release runtime import check failed" >&2
    rm -rf "$STAGED_RELEASE"
    return 1
  fi
  if ! "$STAGED_RELEASE/bin/terminal-mcp" --help >/dev/null; then
    echo "Staged Terminal MCP launcher check failed" >&2
    rm -rf "$STAGED_RELEASE"
    return 1
  fi
}
backup(){
  local release=$1
  local stamp
  [ -f "$DATA/terminal-mcp.sqlite3" ] || return 0
  mkdir -p "$BACKUPS"; stamp=$(date -u +%Y%m%dT%H%M%SZ)
  runtime_python "$release" - "$DATA/terminal-mcp.sqlite3" "$BACKUPS/terminal-mcp-$stamp.sqlite3" <<'PYBACKUP'
import sqlite3,sys
src=sqlite3.connect(sys.argv[1]); dst=sqlite3.connect(sys.argv[2]); src.backup(dst); dst.close(); src.close()
PYBACKUP
  chmod 600 "$BACKUPS/terminal-mcp-$stamp.sqlite3"
}
schema_rollback_safe(){
  local release=$1
  [ -f "$DATA/terminal-mcp.sqlite3" ] || return 0
  runtime_python "$release" - "$DATA/terminal-mcp.sqlite3" <<'PYSCHEMA'
import sqlite3, sys

try:
    from terminal_mcp.storage import sqlite as target_sqlite
    target_version = int(getattr(target_sqlite, "SCHEMA_VERSION", 14))
except Exception:
    target_version = 14

with sqlite3.connect(sys.argv[1]) as db:
    current_version = int(db.execute("PRAGMA user_version").fetchone()[0])
    if current_version <= target_version:
        raise SystemExit(0)

    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    persistent = False
    if "logical_agents" in tables:
        persistent = db.execute("SELECT 1 FROM logical_agents LIMIT 1").fetchone() is not None
    if not persistent and "work_claims" in tables:
        columns = {row[1] for row in db.execute("PRAGMA table_info(work_claims)")}
        if "owner_kind" in columns:
            persistent = db.execute(
                "SELECT 1 FROM work_claims WHERE owner_kind='logical_agent' LIMIT 1"
            ).fetchone() is not None

    if persistent:
        print(
            f"Refusing schema downgrade {current_version}->{target_version}: "
            "Persistent Slot ownership exists",
            file=sys.stderr,
        )
        raise SystemExit(42)
PYSCHEMA
}

install_cli_link(){
  mkdir -p "$(dirname "$CLI_LINK")"
  ln -sfn "$ROOT/current/bin/terminal-mcp" "$CLI_LINK"
}
activate(){
  new=$1; old=$(readlink -f "$ROOT/current" 2>/dev/null || true)
  ln -sfn "$new" "$ROOT/current"; $SYSTEMCTL daemon-reload; $SYSTEMCTL restart terminal-mcp
  for _ in $(seq 1 "$HEALTH_TIMEOUT_SEC"); do
    if curl -fsS "$HEALTH_URL" >/dev/null; then
      install_cli_link
      return 0
    fi
    sleep 1
  done
  if [ -n "$old" ]; then
    if schema_rollback_safe "$old"; then
      ln -sfn "$old" "$ROOT/current"
      $SYSTEMCTL restart terminal-mcp
      echo 'Health check failed; previous release restored' >&2
    else
      echo 'Health check failed; automatic rollback blocked by durable schema state' >&2
    fi
  fi
  return 1
}
mkdir -p "$ROOT/releases"
install -d -o root -g root -m 0700 "$DATA" "$CACHE" "$BACKUPS"
find "$BACKUPS" -maxdepth 1 -type f -name 'terminal-mcp-*.sqlite3' -exec chmod 0600 {} +
[ ! -e "$DATA/terminal-mcp.sqlite3" ] || chmod 0600 "$DATA/terminal-mcp.sqlite3"
[ ! -e "$DATA/auth.sqlite3" ] || chmod 0600 "$DATA/auth.sqlite3"
case "$CMD" in
 install) [ -f "$ENV_FILE" ] || write_env; ensure_env_defaults; write_unit; stage; activate "$STAGED_RELEASE"; $SYSTEMCTL enable terminal-mcp ;;
 update) ensure_env_defaults; stage; schema_rollback_safe "$STAGED_RELEASE"; backup "$STAGED_RELEASE"; activate "$STAGED_RELEASE" ;;
 doctor) $SYSTEMCTL status terminal-mcp --no-pager; curl -fsS "$HEALTH_URL"; check_public_fleet_ingress ;;
 *) echo 'Usage: install.sh {install|update|doctor}'; exit 1 ;;
esac
