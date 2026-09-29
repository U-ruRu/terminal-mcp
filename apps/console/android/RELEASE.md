# Android release and direct update contract

android/version.json is the canonical Android version source. Increment versionCode for every
installable build; keep versionName user-facing.

Release signing material is never committed. A release build requires these environment
variables: TERMINAL_MCP_ANDROID_KEYSTORE, TERMINAL_MCP_ANDROID_STORE_PASSWORD,
TERMINAL_MCP_ANDROID_KEY_ALIAS and TERMINAL_MCP_ANDROID_KEY_PASSWORD.

Build with npm run android:build:release. The Gradle build fails closed when any release
signing input is missing.

Before publishing, set TERMINAL_MCP_ANDROID_SIGNING_CERT_SHA256 to the pinned production
certificate fingerprint and run npm run android:verify:release. Verification checks the APK
signature, package id and canonical version. The same fingerprint is published in latest.json
and the app refuses an update signed by another certificate.

Generate update metadata with npm run android:manifest. Required inputs are:
TERMINAL_MCP_ANDROID_APK_URL (HTTPS immutable APK URL),
TERMINAL_MCP_ANDROID_SIGNING_CERT_SHA256 and TERMINAL_MCP_SOURCE_COMMIT_SHA.
TERMINAL_MCP_ANDROID_MINIMUM_VERSION_CODE and TERMINAL_MCP_ANDROID_RELEASE_NOTES are optional.

dist/android/latest.json contains version, minimum supported version, APK SHA-256, byte size,
signing certificate fingerprint, source commit and publication timestamp.

The native TerminalApkInstaller downloads only HTTPS APKs, verifies exact size and SHA-256,
requires package id app.terminalmcp.console, requires the APK certificate to match both the
manifest and the currently installed app, and refuses same-version/downgrade installs before
opening the Android package installer.

No keystore, store password, key password, or private signing material belongs in git or the
update manifest.
