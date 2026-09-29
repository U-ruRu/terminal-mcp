#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=android-signing-env.sh
source "$ROOT/scripts/android-signing-env.sh"
load_terminal_mcp_android_signing_env
APK="${TERMINAL_MCP_ANDROID_APK_PATH:-$ROOT/android/app/build/outputs/apk/release/app-release.apk}"
EXPECTED_CERT="${TERMINAL_MCP_ANDROID_SIGNING_CERT_SHA256:-}"
ANDROID_HOME="${ANDROID_HOME:-/opt/android-sdk}"

if [[ ! -f "$APK" ]]; then
  echo "release APK not found: $APK" >&2
  exit 1
fi
if [[ -z "$EXPECTED_CERT" ]]; then
  echo "TERMINAL_MCP_ANDROID_SIGNING_CERT_SHA256 is required" >&2
  exit 1
fi

normalize() {
  printf '%s' "$1" | tr -d ':' | tr '[:upper:]' '[:lower:]'
}

APKSIGNER="$(find "$ANDROID_HOME/build-tools" -maxdepth 2 -type f -name apksigner | sort -V | tail -1)"
AAPT="$(find "$ANDROID_HOME/build-tools" -maxdepth 2 -type f -name aapt | sort -V | tail -1)"
if [[ -z "$APKSIGNER" || -z "$AAPT" ]]; then
  echo "Android build tools not found under $ANDROID_HOME" >&2
  exit 1
fi

"$APKSIGNER" verify --verbose "$APK" >/dev/null
ACTUAL_CERT="$("$APKSIGNER" verify --print-certs "$APK" | sed -n 's/^Signer #1 certificate SHA-256 digest: //p')"
if [[ "$(normalize "$ACTUAL_CERT")" != "$(normalize "$EXPECTED_CERT")" ]]; then
  echo "signing certificate mismatch" >&2
  exit 1
fi

VERSION_NAME="$(node -e "const v=require('$ROOT/android/version.json'); process.stdout.write(v.versionName)")"
VERSION_CODE="$(node -e "const v=require('$ROOT/android/version.json'); process.stdout.write(String(v.versionCode))")"
BADGING="$("$AAPT" dump badging "$APK" | sed -n '1p')"
grep -F "name='app.terminalmcp.console'" <<<"$BADGING" >/dev/null
grep -F "versionCode='$VERSION_CODE'" <<<"$BADGING" >/dev/null
grep -F "versionName='$VERSION_NAME'" <<<"$BADGING" >/dev/null

sha256sum "$APK"
printf 'certificate_sha256=%s\n' "$(normalize "$ACTUAL_CERT")"
printf 'version=%s (%s)\n' "$VERSION_NAME" "$VERSION_CODE"
