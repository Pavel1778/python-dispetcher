#!/usr/bin/env bash
# Build a signed, installable NEXUS Android APK.
#
# Uses the Android SDK build-tools directly (aapt2 -> d8 -> zipalign -> apksigner)
# so no Gradle daemon or network is needed at build time. The result is a real,
# installable APK (a native WebView shell around the same dashboard the web
# serves), signed with a debug keystore generated on the fly if none is given.
#
#   ANDROID_SDK_ROOT=/opt/android-sdk packaging/android/build-apk.sh [output-dir]
#
# Signing: set NEXUS_KEYSTORE / NEXUS_KEY_ALIAS / NEXUS_KEYSTORE_PASS to sign
# with a release key; otherwise a throwaway debug key is generated under
# $OUT. A debug-signed APK is installable but NOT a store release.
set -euo pipefail

SDK="${ANDROID_SDK_ROOT:-/opt/android-sdk}"
BT="${ANDROID_BUILD_TOOLS:-$SDK/build-tools/35.0.0}"
PLATFORM="$SDK/platforms/android-34/android.jar"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP="$ROOT/packaging/android/app"
OUT="${1:-$ROOT/dist}"
VERSION_NAME="${NEXUS_VERSION:-2.0.0}"
VERSION_CODE="${NEXUS_VERSION_CODE:-20000}"

for tool in aapt2 d8 zipalign apksigner; do
    [ -x "$BT/$tool" ] || { echo "missing $BT/$tool (install build-tools;34.0.0)" >&2; exit 1; }
done
[ -f "$PLATFORM" ] || { echo "missing $PLATFORM (install platforms;android-34)" >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$OUT" "$WORK/compiled" "$WORK/classes" "$WORK/dex"

echo "== 1/6 compile resources =="
"$BT/aapt2" compile --dir "$APP/res" -o "$WORK/compiled/res.zip"

echo "== 2/6 link resources + manifest =="
"$BT/aapt2" link \
    -o "$WORK/app-unsigned.apk" \
    -I "$PLATFORM" \
    --manifest "$APP/AndroidManifest.xml" \
    --java "$WORK/gen" \
    --min-sdk-version 24 \
    --target-sdk-version 34 \
    --version-code "$VERSION_CODE" \
    --version-name "$VERSION_NAME" \
    "$WORK/compiled/res.zip"

echo "== 3/6 compile java =="
# JDK 21 rejects -bootclasspath with a modern -target; compile against the
# platform jar on the classpath instead (the Android SDK jar is the bootclasspath).
javac -source 11 -target 11 -classpath "$PLATFORM" -d "$WORK/classes" \
    $(find "$APP/src" "$WORK/gen" -name '*.java')

echo "== 4/6 dex =="
"$BT/d8" --lib "$PLATFORM" --min-api 24 --output "$WORK/dex" \
    $(find "$WORK/classes" -name '*.class')

echo "== 5/6 package + align =="
( cd "$WORK/dex" && zip -q -X "$WORK/app-unsigned.apk" classes.dex )
"$BT/zipalign" -f -p 4 "$WORK/app-unsigned.apk" "$WORK/app-aligned.apk"

echo "== 6/6 sign =="
KS="${NEXUS_KEYSTORE:-$OUT/nexus-debug.keystore}"
if [ ! -f "$KS" ]; then
    keytool -genkeypair -keystore "$KS" -storepass android -keypass android \
        -alias "${NEXUS_KEY_ALIAS:-nexus}" -keyalg RSA -keysize 2048 -validity 10000 \
        -dname "CN=NEXUS Debug, OU=Lab, O=NEXUS, L=, S=, C=US" >/dev/null 2>&1
    echo "   generated debug keystore at $KS"
fi
APK="$OUT/nexus-${VERSION_NAME}.apk"
"$BT/apksigner" sign \
    --ks "$KS" --ks-pass "pass:${NEXUS_KEYSTORE_PASS:-android}" \
    --key-pass "pass:${NEXUS_KEYSTORE_PASS:-android}" \
    --ks-key-alias "${NEXUS_KEY_ALIAS:-nexus}" \
    --out "$APK" "$WORK/app-aligned.apk"

"$BT/apksigner" verify --print-certs "$APK" | head -3
echo "built $APK"
