#!/usr/bin/env bash

load_terminal_mcp_android_signing_env() {
  local env_file="${TERMINAL_MCP_ANDROID_SIGNING_ENV_FILE:-/root/secrets/terminal-mcp/android-console/release.env}"
  local required=(
    TERMINAL_MCP_ANDROID_KEYSTORE
    TERMINAL_MCP_ANDROID_STORE_PASSWORD
    TERMINAL_MCP_ANDROID_KEY_ALIAS
    TERMINAL_MCP_ANDROID_KEY_PASSWORD
  )
  local present=0
  local name

  for name in "${required[@]}"; do
    if [[ -n "${!name:-}" ]]; then
      present=$((present + 1))
    fi
  done

  if (( present == 0 )) && [[ -r "$env_file" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$env_file"
    set +a
    present=0
    for name in "${required[@]}"; do
      if [[ -n "${!name:-}" ]]; then
        present=$((present + 1))
      fi
    done
  fi

  if (( present != ${#required[@]} )); then
    echo "Android release signing is incomplete." >&2
    echo "Use a complete TERMINAL_MCP_ANDROID_* environment or provide a readable signing env file at:" >&2
    echo "  $env_file" >&2
    return 2
  fi

  if [[ ! -r "$TERMINAL_MCP_ANDROID_KEYSTORE" ]]; then
    echo "Android release keystore is not readable: $TERMINAL_MCP_ANDROID_KEYSTORE" >&2
    return 2
  fi
}
