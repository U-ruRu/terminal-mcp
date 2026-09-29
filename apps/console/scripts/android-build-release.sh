#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=android-signing-env.sh
source "$ROOT/scripts/android-signing-env.sh"
load_terminal_mcp_android_signing_env
export ANDROID_HOME="${ANDROID_HOME:-/opt/android-sdk}"
export ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-$ANDROID_HOME}"
if [[ ! -d "$ANDROID_HOME" ]]; then
  echo "Android SDK is not available at $ANDROID_HOME" >&2
  exit 2
fi
cd "$ROOT"
npm run android:sync
cd android
exec ./gradlew assembleRelease
