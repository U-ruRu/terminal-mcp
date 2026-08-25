#!/usr/bin/env bash
set -euo pipefail
CMD=${1:-install}
ROOT=${TERMINAL_MCP_INSTALL_ROOT:-/opt/terminal-mcp}
ENV_DIR=${TERMINAL_MCP_ENV_DIR:-/etc/terminal-mcp}
ENV_FILE=$ENV_DIR/terminal-mcp.env
DATA=${TERMINAL_MCP_DATA_DIR:-/var/lib/terminal-mcp}
BACKUPS=${TERMINAL_MCP_BACKUP_DIR:-/var/backups/terminal-mcp}
UNIT_FILE=${TERMINAL_MCP_UNIT_FILE:-/etc/systemd/system/terminal-mcp.service}
SYSTEMCTL=${TERMINAL_MCP_SYSTEMCTL:-systemctl}
HEALTH_URL=${TERMINAL_MCP_HEALTH_URL:-http://127.0.0.1:8080/health/live}
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
ENV
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
  release=$ROOT/releases/$(date -u +%Y%m%dT%H%M%SZ)
  python3 -m venv "$release"
  "$release/bin/pip" install --upgrade pip >&2
  "$release/bin/pip" install "$SOURCE" >&2
  echo "$release"
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
  for _ in $(seq 1 20); do curl -fsS "$HEALTH_URL" >/dev/null && return 0; sleep 1; done
  [ -n "$old" ] && ln -sfn "$old" "$ROOT/current"
  $SYSTEMCTL restart terminal-mcp
  echo 'Health check failed; previous release restored' >&2; return 1
}
mkdir -p "$ROOT/releases"
install -d -o root -g root -m 0700 "$DATA" "$BACKUPS"
find "$BACKUPS" -maxdepth 1 -type f -name 'terminal-mcp-*.sqlite3' -exec chmod 0600 {} +
[ ! -e "$DATA/terminal-mcp.sqlite3" ] || chmod 0600 "$DATA/terminal-mcp.sqlite3"
case "$CMD" in
 install) [ -f "$ENV_FILE" ] || write_env; write_unit; release=$(stage); activate "$release"; $SYSTEMCTL enable terminal-mcp ;;
 update) backup; release=$(stage); activate "$release" ;;
 doctor) $SYSTEMCTL status terminal-mcp --no-pager; curl -fsS "$HEALTH_URL" ;;
 *) echo 'Usage: install.sh {install|update|doctor}'; exit 1 ;;
esac
