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
SYSTEMCTL=${TERMINAL_MCP_SYSTEMCTL:-systemctl}
HEALTH_URL=${TERMINAL_MCP_HEALTH_URL:-http://127.0.0.1:8080/health/live}
HEALTH_TIMEOUT_SEC=${TERMINAL_MCP_ACTIVATION_HEALTH_TIMEOUT_SEC:-180}
SOURCE=$(cd "$(dirname "$0")/.." && pwd)
[ "$(id -u)" -eq 0 ] || { echo 'Run as root'; exit 1; }
write_env(){
  [ -n "${TERMINAL_MCP_ADMIN_PASSWORD:-}" ] || { echo 'Set TERMINAL_MCP_ADMIN_PASSWORD'; exit 1; }
  mkdir -p "$ENV_DIR"
  cat >"$ENV_FILE" <<ENV
TERMINAL_MCP_HOST="127.0.0.1"
TERMINAL_MCP_PORT="8080"
TERMINAL_MCP_PUBLIC_BASE_URL="${TERMINAL_MCP_PUBLIC_BASE_URL:-https://terminal.example.com}"
TERMINAL_MCP_ENV_FILE_PATH="$ENV_FILE"
TERMINAL_MCP_DATABASE_PATH="$DATA/terminal-mcp.sqlite3"
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
ENV
  chmod 600 "$ENV_FILE"
}
ensure_env_defaults(){
  [ -f "$ENV_FILE" ] || return 0
  ensure_env(){ key=$1; value=$2; grep -q "^${key}=" "$ENV_FILE" || printf '%s="%s"\n' "$key" "$value" >>"$ENV_FILE"; }
  ensure_env TERMINAL_MCP_OUTPUT_CACHE_PATH "$CACHE/output.sqlite3"
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
  chmod 600 "$ENV_FILE"
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
}
backup(){
  [ -f "$DATA/terminal-mcp.sqlite3" ] || return 0
  mkdir -p "$BACKUPS"; stamp=$(date -u +%Y%m%dT%H%M%SZ)
  python3 - "$DATA/terminal-mcp.sqlite3" "$BACKUPS/terminal-mcp-$stamp.sqlite3" <<'PY'
import sqlite3,sys
src=sqlite3.connect(sys.argv[1]); dst=sqlite3.connect(sys.argv[2]); src.backup(dst); dst.close(); src.close()
PY
  chmod 600 "$BACKUPS/terminal-mcp-$stamp.sqlite3"
}
activate(){
  new=$1; old=$(readlink -f "$ROOT/current" 2>/dev/null || true)
  ln -sfn "$new" "$ROOT/current"; $SYSTEMCTL daemon-reload; $SYSTEMCTL restart terminal-mcp
  for _ in $(seq 1 "$HEALTH_TIMEOUT_SEC"); do
    curl -fsS "$HEALTH_URL" >/dev/null && return 0
    sleep 1
  done
  [ -n "$old" ] && ln -sfn "$old" "$ROOT/current"
  $SYSTEMCTL restart terminal-mcp
  echo 'Health check failed; previous release restored' >&2; return 1
}
mkdir -p "$ROOT/releases"
install -d -o root -g root -m 0700 "$DATA" "$CACHE" "$BACKUPS"
find "$BACKUPS" -maxdepth 1 -type f -name 'terminal-mcp-*.sqlite3' -exec chmod 0600 {} +
[ ! -e "$DATA/terminal-mcp.sqlite3" ] || chmod 0600 "$DATA/terminal-mcp.sqlite3"
case "$CMD" in
 install) [ -f "$ENV_FILE" ] || write_env; ensure_env_defaults; write_unit; stage; activate "$STAGED_RELEASE"; $SYSTEMCTL enable terminal-mcp ;;
 update) ensure_env_defaults; backup; stage; activate "$STAGED_RELEASE" ;;
 doctor) $SYSTEMCTL status terminal-mcp --no-pager; curl -fsS "$HEALTH_URL" ;;
 *) echo 'Usage: install.sh {install|update|doctor}'; exit 1 ;;
esac
