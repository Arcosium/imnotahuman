#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NATIVE="${HOME}/android-build/native"
OUT="${HERE}/ImNotAHuman.apk"

export JAVA_HOME="${NATIVE}/jdk17"
export ANDROID_HOME="${NATIVE}/sdk"
export PATH="${JAVA_HOME}/bin:${PATH}"
printf 'sdk.dir=%s\n' "${ANDROID_HOME}" > "${HERE}/local.properties"
cd "${HERE}"
./gradlew --no-daemon assembleRelease \
  -Pandroid.aapt2FromMavenOverride="${NATIVE}/aapt2-wrap/aapt2"
"${JAVA_HOME}/bin/java" -jar "${ANDROID_HOME}/build-tools/34.0.0/lib/apksigner.jar" \
  verify app/build/outputs/apk/release/app-release.apk
cp app/build/outputs/apk/release/app-release.apk "${OUT}"
printf 'APK: %s\n' "${OUT}"
