#!/usr/bin/env bash
set -euo pipefail
CMD=${1:-install}
[ $# -eq 0 ] || shift
ALLOW_DOWNGRADE=false
while [ $# -gt 0 ]; do
  case "$1" in
    --allow-downgrade) ALLOW_DOWNGRADE=true ;;
    *) echo "Unknown option: $1" >&2; echo 'Usage: install.sh {install|update [--allow-downgrade]|ingress|doctor}' >&2; exit 2 ;;
  esac
  shift
done
if [ "$ALLOW_DOWNGRADE" = true ] && [ "$CMD" != update ]; then
  echo '--allow-downgrade is valid only with update' >&2
  exit 2
fi
ROOT=${TERMINAL_MCP_INSTALL_ROOT:-/opt/terminal-mcp}
ENV_DIR=${TERMINAL_MCP_ENV_DIR:-/etc/terminal-mcp}
ENV_FILE=$ENV_DIR/terminal-mcp.env
DATA=${TERMINAL_MCP_DATA_DIR:-/var/lib/terminal-mcp}
CACHE=${TERMINAL_MCP_CACHE_DIR:-/var/cache/terminal-mcp}
BACKUPS=${TERMINAL_MCP_BACKUP_DIR:-/var/backups/terminal-mcp}
UNIT_FILE=${TERMINAL_MCP_UNIT_FILE:-/etc/systemd/system/terminal-mcp.service}
CLI_LINK=${TERMINAL_MCP_CLI_LINK:-/usr/local/bin/terminal-mcp}
# The legacy updater cannot safely chown API stores or roll back one half of a
# split topology. Refuse before any filesystem/service mutation.
if [ -f "${UNIT_FILE}.d/50-local-executor.conf" ] && [ "$CMD" != doctor ]; then
  echo 'Split executor topology is active; use the coordinated topology transition, not the legacy installer' >&2
  exit 46
fi
SYSTEMCTL=${TERMINAL_MCP_SYSTEMCTL:-systemctl}
HEALTH_URL=${TERMINAL_MCP_HEALTH_URL:-http://127.0.0.1:8080/health/live}
HEALTH_TIMEOUT_SEC=${TERMINAL_MCP_ACTIVATION_HEALTH_TIMEOUT_SEC:-600}
PUBLIC_INGRESS_TIMEOUT_SEC=${TERMINAL_MCP_PUBLIC_INGRESS_TIMEOUT_SEC:-10}
CANONICAL_CONSOLE_ORIGINS=${TERMINAL_MCP_CANONICAL_CONSOLE_ORIGINS:-https://localhost}
CADDYFILE=${TERMINAL_MCP_CADDYFILE:-/etc/caddy/Caddyfile}
CADDY_BIN=${TERMINAL_MCP_CADDY_BIN:-caddy}
CADDY_SERVICE=${TERMINAL_MCP_CADDY_SERVICE:-caddy}
CADDY_MANAGE_MODE=${TERMINAL_MCP_CADDY_MANAGE_MODE:-auto}
CADDY_UPSTREAM=${TERMINAL_MCP_CADDY_UPSTREAM:-127.0.0.1:8080}
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
TERMINAL_MCP_CONSOLE_ALLOWED_ORIGINS="${TERMINAL_MCP_CONSOLE_ALLOWED_ORIGINS:-$CANONICAL_CONSOLE_ORIGINS}"
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
TERMINAL_MCP_PERSISTENT_SESSION_REARM_AFTER_SEC="${TERMINAL_MCP_PERSISTENT_SESSION_REARM_AFTER_SEC:-180}"
ENV
  if [ -n "${TERMINAL_MCP_LEGACY_AGENT_ADMISSION_ENABLED:-}" ]; then
    printf 'TERMINAL_MCP_LEGACY_AGENT_ADMISSION_ENABLED="%s"\\n' "${TERMINAL_MCP_LEGACY_AGENT_ADMISSION_ENABLED}" >>"$ENV_FILE"
  fi
  chmod 600 "$ENV_FILE"
}
ensure_console_origins(){
  python3 - "$ENV_FILE" "$CANONICAL_CONSOLE_ORIGINS" <<'PYORIGINS'
from pathlib import Path
import sys

path = Path(sys.argv[1])
required = [item.strip() for item in sys.argv[2].split(",") if item.strip()]
lines = path.read_text().splitlines()
prefix = "TERMINAL_MCP_CONSOLE_ALLOWED_ORIGINS="
found = False

for index, line in enumerate(lines):
    if not line.startswith(prefix):
        continue
    found = True
    raw = line[len(prefix):].strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        raw = raw[1:-1]
    existing = [item.strip() for item in raw.split(",") if item.strip()]
    merged = list(dict.fromkeys(existing + required))
    lines[index] = prefix + '"' + ",".join(merged) + '"'
    break

if not found:
    lines.append(prefix + '"' + ",".join(required) + '"')

path.write_text("\n".join(lines) + "\n")
PYORIGINS
}

ensure_env_defaults(){
  [ -f "$ENV_FILE" ] || return 0
  ensure_env(){ key=$1; value=$2; grep -q "^${key}=" "$ENV_FILE" || printf '%s="%s"\n' "$key" "$value" >>"$ENV_FILE"; }
  ensure_console_origins
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
  ensure_env TERMINAL_MCP_FLEET_LEGACY_REPLICATION_ENABLED "${TERMINAL_MCP_FLEET_LEGACY_REPLICATION_ENABLED:-true}"
  ensure_env TERMINAL_MCP_FLEET_V1_PUBLIC_ENABLED "${TERMINAL_MCP_FLEET_V1_PUBLIC_ENABLED:-false}"
  ensure_env TERMINAL_MCP_PERSISTENT_AGENTS_ENABLED "${TERMINAL_MCP_PERSISTENT_AGENTS_ENABLED:-true}"
  ensure_env TERMINAL_MCP_PERSISTENT_SESSION_DURATION_SEC "${TERMINAL_MCP_PERSISTENT_SESSION_DURATION_SEC:-1380}"
  ensure_env TERMINAL_MCP_PERSISTENT_SESSION_WARNING_AFTER_SEC "${TERMINAL_MCP_PERSISTENT_SESSION_WARNING_AFTER_SEC:-1200}"
  ensure_env TERMINAL_MCP_PERSISTENT_SESSION_ALERT_AFTER_SEC "${TERMINAL_MCP_PERSISTENT_SESSION_ALERT_AFTER_SEC:-1320}"
  ensure_env TERMINAL_MCP_PERSISTENT_SESSION_REARM_AFTER_SEC "${TERMINAL_MCP_PERSISTENT_SESSION_REARM_AFTER_SEC:-180}"
  if [ -n "${TERMINAL_MCP_LEGACY_AGENT_ADMISSION_ENABLED:-}" ]; then
    ensure_env TERMINAL_MCP_LEGACY_AGENT_ADMISSION_ENABLED "${TERMINAL_MCP_LEGACY_AGENT_ADMISSION_ENABLED}"
  fi
  chmod 600 "$ENV_FILE"
}

read_env_value(){
  local key=$1
  sed -n "s/^${key}=\"\(.*\)\"$/\1/p" "$ENV_FILE" | tail -n 1
}

first_console_origin(){
  local origins
  origins=$(read_env_value TERMINAL_MCP_CONSOLE_ALLOWED_ORIGINS)
  printf '%s' "${origins%%,*}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//'
}

public_status(){
  local method=$1
  local url=$2
  shift 2
  curl --max-time "$PUBLIC_INGRESS_TIMEOUT_SEC" -sS -o /dev/null -w '%{http_code}' -X "$method" "$@" "$url" || true
}

check_public_console_ingress(){
  local public_base_url console_origin connect_status ticket_status
  local headers preflight_status allow_origin route method status

  [ -f "$ENV_FILE" ] || { echo "Missing env file: $ENV_FILE" >&2; return 1; }
  public_base_url=$(read_env_value TERMINAL_MCP_PUBLIC_BASE_URL)
  console_origin=$(first_console_origin)
  [ -n "$public_base_url" ] || { echo "TERMINAL_MCP_PUBLIC_BASE_URL is not configured" >&2; return 1; }
  [ -n "$console_origin" ] || { echo "TERMINAL_MCP_CONSOLE_ALLOWED_ORIGINS is empty" >&2; return 1; }

  connect_status=$(public_status GET "${public_base_url%/}/connect")
  if [ "$connect_status" != "200" ]; then
    echo "Public Console ingress check failed: /connect returned ${connect_status:-request_error}, expected 200" >&2
    return 1
  fi

  headers=$(mktemp)
  preflight_status=$(
    curl --max-time "$PUBLIC_INGRESS_TIMEOUT_SEC" -sS -D "$headers" -o /dev/null -w '%{http_code}' \
      -X OPTIONS \
      -H "Origin: $console_origin" \
      -H 'Access-Control-Request-Method: GET' \
      -H 'Access-Control-Request-Headers: authorization,content-type' \
      "${public_base_url%/}/actions/console/snapshot" || true
  )
  allow_origin=$(
    awk 'tolower($1) == "access-control-allow-origin:" {
      $1=""; sub(/^[[:space:]]+/, ""); sub(/\r$/, ""); print; exit
    }' "$headers"
  )
  rm -f "$headers"
  if { [ "$preflight_status" != "200" ] && [ "$preflight_status" != "204" ]; } ||
     [ "$allow_origin" != "$console_origin" ]; then
    echo "Public Console CORS preflight failed: status=${preflight_status:-request_error} allow-origin=${allow_origin:-missing} expected=$console_origin" >&2
    return 1
  fi

  ticket_status=$(public_status POST "${public_base_url%/}/console/ws-ticket" -H "Origin: $console_origin")
  if [ "$ticket_status" != "401" ]; then
    echo "Public Console route check failed: /console/ws-ticket returned ${ticket_status:-request_error}, expected 401" >&2
    return 1
  fi

  fleet_v1_public_enabled=$(read_env_value TERMINAL_MCP_FLEET_V1_PUBLIC_ENABLED)
  case "${fleet_v1_public_enabled,,}" in
    true|1|yes|on)
      while IFS='|' read -r method route json_body; do
        [ -n "$route" ] || continue
        if [ -n "$json_body" ]; then
          status=$(
            public_status "$method" "${public_base_url%/}${route}" \
              -H "Origin: $console_origin" \
              -H 'Content-Type: application/json' \
              --data "$json_body"
          )
        else
          status=$(public_status "$method" "${public_base_url%/}${route}" -H "Origin: $console_origin")
        fi
        if [ "$status" != "401" ]; then
          echo "Public Fleet v1 Console route check failed: $method $route returned ${status:-request_error}, expected 401" >&2
          return 1
        fi
      done <<'ROUTES'
GET|/console/fleet/v1/probe|
GET|/console/fleet/v1/snapshot|
POST|/console/fleet/v1/activity|{}
POST|/console/fleet/v1/query/tasks|{}
GET|/console/fleet/v1/detail/tasks?entity_id=ingress-probe|
POST|/console/fleet/v1/namespaces|{}
GET|/console/fleet/v1/task-graph?namespace=ingress-probe&task_id=ingress-probe|
POST|/console/fleet/v1/ws-ticket|
ROUTES
      ;;
  esac
}

configure_console_caddy(){
  local public_base_url candidate stamp backup

  case "$CADDY_MANAGE_MODE" in
    off) return 0 ;;
    auto|required) ;;
    *) echo "TERMINAL_MCP_CADDY_MANAGE_MODE must be auto|required|off" >&2; return 1 ;;
  esac

  if [ ! -f "$CADDYFILE" ] || ! command -v "$CADDY_BIN" >/dev/null 2>&1; then
    if [ "$CADDY_MANAGE_MODE" = "required" ]; then
      echo "Caddy management required but Caddyfile/binary is unavailable" >&2
      return 1
    fi
    return 0
  fi

  public_base_url=$(read_env_value TERMINAL_MCP_PUBLIC_BASE_URL)
  [ -n "$public_base_url" ] || { echo "TERMINAL_MCP_PUBLIC_BASE_URL is not configured" >&2; return 1; }
  candidate=$(mktemp "$(dirname "$CADDYFILE")/.terminal-mcp-caddy.XXXXXX")
  if ! python3 "$SOURCE/deploy/console_ingress.py" \
      --input "$CADDYFILE" \
      --output "$candidate" \
      --public-base-url "$public_base_url" \
      --upstream "$CADDY_UPSTREAM"; then
    rm -f "$candidate"
    return 1
  fi

  if cmp -s "$CADDYFILE" "$candidate"; then
    rm -f "$candidate"
    return 0
  fi

  if ! "$CADDY_BIN" validate --config "$candidate" --adapter caddyfile >/dev/null; then
    echo "Candidate Caddy configuration failed validation; current config left unchanged" >&2
    rm -f "$candidate"
    return 1
  fi

  mkdir -p "$BACKUPS"
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  backup="$BACKUPS/caddy-$stamp.Caddyfile"
  cp -p "$CADDYFILE" "$backup"
  chmod 600 "$backup"
  cat "$candidate" >"$CADDYFILE"
  rm -f "$candidate"

  if ! "$SYSTEMCTL" reload "$CADDY_SERVICE"; then
    echo "Caddy reload failed; restoring $backup" >&2
    cat "$backup" >"$CADDYFILE"
    "$CADDY_BIN" validate --config "$CADDYFILE" --adapter caddyfile >/dev/null || true
    "$SYSTEMCTL" reload "$CADDY_SERVICE" || true
    return 1
  fi
}

check_public_fleet_ingress(){
  [ -f "$ENV_FILE" ] || { echo "Missing env file: $ENV_FILE" >&2; return 1; }
  public_base_url=$(
    sed -n 's/^TERMINAL_MCP_PUBLIC_BASE_URL="\(.*\)"$/\1/p' "$ENV_FILE" | tail -n 1
  )
  [ -n "$public_base_url" ] || { echo "TERMINAL_MCP_PUBLIC_BASE_URL is not configured" >&2; return 1; }
  legacy_replication_enabled=$(read_env_value TERMINAL_MCP_FLEET_LEGACY_REPLICATION_ENABLED)
  case "${legacy_replication_enabled,,}" in
    false|0|no|off) ingress_path=/internal/fleet/v1/source/manifest ;;
    *) ingress_path=/internal/fleet/identities ;;
  esac
  ingress_url="${public_base_url%/}${ingress_path}"
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
  # Version calculation works on a disposable copy, leaving Git sources unchanged.
  # The installed wheel and PEP 440 metadata keep the numeric version; a short
  # commit SHA is exposed independently as the release identifier.
  BUILD_SOURCE="$STAGED_RELEASE/_build_source"
  mkdir -p "$BUILD_SOURCE"
  if ! (cd "$SOURCE" && tar --exclude=.git --exclude=.venv --exclude=__pycache__ -cf - .) \
      | tar -xf - -C "$BUILD_SOURCE"; then
    rm -rf "$STAGED_RELEASE"
    echo "Unable to stage isolated build source" >&2
    return 1
  fi
  version_args=(--source "$BUILD_SOURCE" --stamp --output "$STAGED_RELEASE/RELEASE_META.json")
  # An existing QA deployment is not the released schema baseline.
  # Anchor its automatic bump to the last confirmed canonical release.
  prior_release="$ROOT/current"
  stable_found=false
  use_current_qa=false
  # A QA release created by our versioning system is a legitimate
  # prerelease baseline. Compare it first: schema remains 15, and new
  # code on that schema becomes 0.15.1, 0.15.2, etc.
  if [ -f "$ROOT/current/QA_RELEASE.json" ] &&
     [ -f "$ROOT/current/RELEASE_META.json" ]; then
    use_current_qa=true
    version_args+=(--prior-metadata "$ROOT/current/RELEASE_META.json")
  fi
  if [ -f "$ROOT/current/QA_RELEASE.json" ] && [ "$use_current_qa" = false ]; then
    while IFS= read -r prior_candidate; do
      if [ -f "$prior_candidate/CANONICAL_RELEASE.json" ] &&
         [ ! -f "$prior_candidate/QA_RELEASE.json" ] &&
         [ -x "$prior_candidate/bin/python" ]; then
        prior_release="$prior_candidate"
        stable_found=true
        break
      fi
    done < <(find "$ROOT/releases" -mindepth 1 -maxdepth 1 -type d | sort -r)
    if [ "$stable_found" = false ]; then
      # Release retention may remove all old canonical wheels.
      # Pin the last confirmed baseline outside the pruned release tree.
      baseline_file="$ROOT/CANONICAL_BASELINE.json"
      if [ ! -f "$baseline_file" ]; then
        baseline_file="$SOURCE/release/canonical_baseline.json"
      fi
      if [ ! -f "$baseline_file" ]; then
        rm -rf "$STAGED_RELEASE"
        echo "No canonical version baseline for QA update" >&2
        return 1
      fi
      version_args+=(--prior-baseline "$baseline_file")
    fi
  fi
  if [ "$stable_found" = true ] ||
     [ "$use_current_qa" = true ] ||
     [ ! -f "$ROOT/current/QA_RELEASE.json" ]; then
    if [ -x "$prior_release/bin/python" ]; then
      prior_pkg=$("$prior_release/bin/python" -c \
        'from pathlib import Path; import terminal_mcp; print(Path(terminal_mcp.__file__).resolve().parent)') || {
        rm -rf "$STAGED_RELEASE"; echo "Cannot read prior package" >&2; return 1;
      }
      version_args+=(--prior-package "$prior_pkg")
    fi
  fi
  if [ -n "${TERMINAL_MCP_SOURCE_SHA:-}" ]; then
    version_args+=(--sha "$TERMINAL_MCP_SOURCE_SHA")
  elif [ -e "$SOURCE/.git" ]; then
    version_args+=(--source-repo "$SOURCE")
  elif [[ "${SOURCE##*/}" =~ ^[0-9a-fA-F]{7,40}$ ]]; then
    # SHA-named git-archive staging directories need no .git metadata.
    version_args+=(--sha "${SOURCE##*/}")
  fi
  if ! python3 "$SOURCE/scripts/release_version.py" "${version_args[@]}" >&2; then
    rm -rf "$STAGED_RELEASE"
    echo "Automatic release version calculation failed" >&2
    return 1
  fi
  if ! "$STAGED_RELEASE/bin/pip" install "$BUILD_SOURCE" >&2; then
    rm -rf "$STAGED_RELEASE"
    return 1
  fi
  rm -rf "$BUILD_SOURCE"
  # Keep QA builds distinct from canonical releases for the next comparison.
  # A final promotion opts in with TERMINAL_MCP_DEPLOY_CHANNEL=canonical.
  release_channel=${TERMINAL_MCP_DEPLOY_CHANNEL:-}
  if [ -z "$release_channel" ] && [ -f "$ROOT/current/QA_RELEASE.json" ]; then
    release_channel=qa
  fi
  if [ "$release_channel" = qa ]; then
    cp "$STAGED_RELEASE/RELEASE_META.json" "$STAGED_RELEASE/QA_RELEASE.json"
  elif [ "$release_channel" = canonical ]; then
    cp "$STAGED_RELEASE/RELEASE_META.json" "$STAGED_RELEASE/CANONICAL_RELEASE.json"
  elif [ -n "$release_channel" ]; then
    rm -rf "$STAGED_RELEASE"
    echo "Invalid TERMINAL_MCP_DEPLOY_CHANNEL (expected qa or canonical)" >&2
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
release_version(){
  release=$1
  "$release/bin/python" -c 'import importlib.metadata as metadata; print(metadata.version("terminal-mcp"))'
}
version_relation(){
  current=$1
  candidate=$2
  python3 - "$current" "$candidate" <<'PY_VERSION'
import re
import sys


def parse(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", value):
        raise ValueError(value)
    return tuple(int(part) for part in value.split("."))


try:
    current = parse(sys.argv[1])
    candidate = parse(sys.argv[2])
except ValueError as exc:
    print(f"uncomparable:{exc.args[0]}")
    raise SystemExit(0)

width = max(len(current), len(candidate))
current += (0,) * (width - len(current))
candidate += (0,) * (width - len(candidate))
print("downgrade" if candidate < current else "ok")
PY_VERSION
}
guard_downgrade(){
  staged=$1
  current=$ROOT/current
  [ -x "$current/bin/python" ] || return 0

  current_version=$(release_version "$current") || {
    echo "Unable to determine current Terminal MCP version; refusing update" >&2
    rm -rf "$staged"
    return 1
  }
  candidate_version=$(release_version "$staged") || {
    echo "Unable to determine staged Terminal MCP version; refusing update" >&2
    rm -rf "$staged"
    return 1
  }
  relation=$(version_relation "$current_version" "$candidate_version")
  case "$relation" in
    downgrade)
      if [ "$ALLOW_DOWNGRADE" = true ]; then
        echo "WARNING: explicit downgrade allowed: $current_version -> $candidate_version" >&2
        return 0
      fi
      echo "Downgrade blocked: current=$current_version candidate=$candidate_version" >&2
      echo "Re-run with: install.sh update --allow-downgrade" >&2
      rm -rf "$staged"
      return 1
      ;;
    ok) return 0 ;;
    uncomparable:*)
      echo "Unable to compare Terminal MCP versions safely: current=$current_version candidate=$candidate_version" >&2
      rm -rf "$staged"
      return 1
      ;;
    *)
      echo "Unexpected version comparison result: $relation" >&2
      rm -rf "$staged"
      return 1
      ;;
  esac
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
  runtime_python "$release" -m terminal_mcp.deployment.retention backups "$BACKUPS" --keep 2
}
schema_rollback_safe(){
  local release=$1
  local runtime_db="$DATA/terminal-mcp.sqlite3"
  local auth_db="$DATA/auth.sqlite3"
  local fleet_control_db="$DATA/fleet-control.sqlite3"
  if [ -f "$ENV_FILE" ]; then
    local configured_fleet_control
    configured_fleet_control=$(sed -n 's/^TERMINAL_MCP_FLEET_CONTROL_PATH=//p' "$ENV_FILE" | tail -n 1 | tr -d '"' || true)
    [ -z "$configured_fleet_control" ] || fleet_control_db="$configured_fleet_control"
  fi
  [ -f "$runtime_db" ] || [ -f "$auth_db" ] || [ -f "$fleet_control_db" ] || return 0
  runtime_python "$release" - "$runtime_db" "$auth_db" "$fleet_control_db" <<'PYSCHEMA'
import sqlite3
import sys
from pathlib import Path

runtime_path = Path(sys.argv[1])
auth_path = Path(sys.argv[2])
fleet_control_path = Path(sys.argv[3])

if runtime_path.is_file():
    try:
        from terminal_mcp.storage import sqlite as target_sqlite
        target_version = int(getattr(target_sqlite, "SCHEMA_VERSION", 14))
    except Exception:
        target_version = 14

    with sqlite3.connect(runtime_path) as db:
        current_version = int(db.execute("PRAGMA user_version").fetchone()[0])
    if current_version > target_version:
        print(
            f"Refusing runtime schema downgrade {current_version}->{target_version}: "
            "automatic binary rollback cannot restore a compatible runtime database",
            file=sys.stderr,
        )
        raise SystemExit(42)

if fleet_control_path.is_file():
    try:
        from terminal_mcp.fleet.control_storage import FleetControlStore
        target_control_version = int(FleetControlStore.SCHEMA_VERSION)
    except Exception:
        target_control_version = 1

    with sqlite3.connect(fleet_control_path) as db:
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "control_schema" in tables:
            row = db.execute(
                "SELECT version FROM control_schema WHERE singleton=1"
            ).fetchone()
            current_control_version = int(row[0]) if row else 0
        else:
            current_control_version = 0
    if current_control_version > target_control_version:
        print(
            f"Refusing fleet-control schema downgrade "
            f"{current_control_version}->{target_control_version}: "
            "durable managed topology/trust/AccessPolicy state requires a compatible binary",
            file=sys.stderr,
        )
        raise SystemExit(45)

if auth_path.is_file():
    try:
        from terminal_mcp.auth import foundation as target_auth
        target_auth_version = int(getattr(target_auth, "AUTH_SCHEMA_VERSION", 2))
    except Exception:
        target_auth_version = 2

    with sqlite3.connect(auth_path) as db:
        current_auth_version = int(db.execute("PRAGMA user_version").fetchone()[0])
        if current_auth_version < 3 <= target_auth_version:
            tables = {
                row[0]
                for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            access_tables = (
                "auth_access_slots",
                "auth_access_codes",
                "auth_access_code_tombstones",
            )
            access_rows = sum(
                int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in access_tables
                if table in tables
            )
            if access_rows:
                print(
                    f"Refusing auth schema upgrade {current_auth_version}->{target_auth_version}: "
                    "pre-v3 Access verifier state exists and cannot be safely re-keyed automatically; "
                    "resolve the legacy Access state before activating this release",
                    file=sys.stderr,
                )
                raise SystemExit(44)
    if current_auth_version > target_auth_version:
        print(
            f"Refusing auth schema downgrade {current_auth_version}->{target_auth_version}: "
            "rollback-excluded Access security state requires a compatible binary",
            file=sys.stderr,
        )
        raise SystemExit(43)
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
      if check_public_fleet_ingress && check_public_console_ingress; then
        install_cli_link
        if [ -f "$new/CANONICAL_RELEASE.json" ]; then
          cp "$new/RELEASE_META.json" "$ROOT/CANONICAL_BASELINE.json"
        fi
        if [ -n "$old" ]; then
          runtime_python "$new" -m terminal_mcp.deployment.retention releases "$ROOT/releases" "$new" "$old"
        else
          runtime_python "$new" -m terminal_mcp.deployment.retention releases "$ROOT/releases" "$new"
        fi
        return 0
      fi
      break
    fi
    sleep 1
  done
  if [ -n "$old" ]; then
    if schema_rollback_safe "$old"; then
      ln -sfn "$old" "$ROOT/current"
      $SYSTEMCTL restart terminal-mcp
      rm -rf -- "$new"
      echo 'Health check failed; previous release restored' >&2
    else
      echo 'Health check failed; automatic rollback blocked by durable schema state' >&2
    fi
  fi
  return 1
}
if [ "$CMD" = install ] || [ "$CMD" = update ]; then
  mkdir -p "$ROOT/releases"
  install -d -o root -g root -m 0700 "$DATA" "$CACHE" "$BACKUPS"
  find "$BACKUPS" -maxdepth 1 -type f -name 'terminal-mcp-*.sqlite3' -exec chmod 0600 {} +
  [ ! -e "$DATA/terminal-mcp.sqlite3" ] || chmod 0600 "$DATA/terminal-mcp.sqlite3"
  [ ! -e "$DATA/auth.sqlite3" ] || chmod 0600 "$DATA/auth.sqlite3"
fi
case "$CMD" in
 install) [ -f "$ENV_FILE" ] || write_env; ensure_env_defaults; write_unit; stage; configure_console_caddy; activate "$STAGED_RELEASE"; $SYSTEMCTL enable terminal-mcp ;;
 update) ensure_env_defaults; stage; guard_downgrade "$STAGED_RELEASE"; schema_rollback_safe "$STAGED_RELEASE"; backup "$STAGED_RELEASE"; configure_console_caddy; activate "$STAGED_RELEASE" ;;
 ingress) ensure_env_defaults; configure_console_caddy ;;
 doctor) $SYSTEMCTL status terminal-mcp --no-pager; curl -fsS "$HEALTH_URL"; check_public_fleet_ingress; check_public_console_ingress ;;
 *) echo 'Usage: install.sh {install|update [--allow-downgrade]|ingress|doctor}'; exit 1 ;;
esac
