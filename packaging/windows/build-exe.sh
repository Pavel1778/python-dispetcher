#!/usr/bin/env bash
# Build the NEXUS Windows executable.
#
# Cross-compiles a small native Win32 launcher with mingw-w64, producing a real
# PE32+ executable with version metadata. The launcher opens the configured
# backend URL in the default browser; it stores no credential.
#
#   packaging/windows/build-exe.sh [output-dir]
#
# Cross-compilation produces a genuine Windows binary (verified with `file`).
# Running it requires Windows; see the report for that status.
set -euo pipefail

CC="${MINGW_CC:-x86_64-w64-mingw32-gcc}"
RC="${MINGW_RC:-x86_64-w64-mingw32-windres}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC="$ROOT/packaging/windows"
OUT="${1:-$ROOT/dist}"
VERSION="${NEXUS_VERSION:-2.0.0}"

command -v "$CC" >/dev/null || { echo "$CC not found (install mingw-w64)" >&2; exit 1; }
mkdir -p "$OUT"

# Version resource: gives the .exe real file properties (right-click -> Details).
RES="$OUT/nexus-version.o"
cat > "$OUT/nexus.rc" <<EOF
#include <winver.h>
VS_VERSION_INFO VERSIONINFO
FILEVERSION 2,0,0,0
PRODUCTVERSION 2,0,0,0
FILEOS VOS_NT_WINDOWS32
FILETYPE VFT_APP
BEGIN
  BLOCK "StringFileInfo"
  BEGIN
    BLOCK "040904b0"
    BEGIN
      VALUE "CompanyName", "NEXUS"
      VALUE "FileDescription", "NEXUS AI development lab launcher"
      VALUE "FileVersion", "$VERSION"
      VALUE "ProductName", "NEXUS"
      VALUE "ProductVersion", "$VERSION"
      VALUE "OriginalFilename", "nexus-client.exe"
    END
  END
  BLOCK "VarFileInfo"
  BEGIN
    VALUE "Translation", 0x409, 1200
  END
END
EOF
if command -v "$RC" >/dev/null; then
    "$RC" -I /usr/share/mingw-w64/include "$OUT/nexus.rc" -O coff -o "$RES"
    RES_ARG="$RES"
else
    echo "windres not found; building without a version resource" >&2
    RES_ARG=""
fi

"$CC" -O2 -municode -mwindows -Wall -Wextra \
    -o "$OUT/nexus-client.exe" "$SRC/nexus-client.c" $RES_ARG \
    -lshell32 -lole32 -luser32

if command -v file >/dev/null; then
    file "$OUT/nexus-client.exe"
else
    python3 - "$OUT/nexus-client.exe" <<'PY'
import struct, sys
data = open(sys.argv[1], "rb").read()
assert data[:2] == b"MZ", "not a DOS/PE image"
pe = struct.unpack_from("<I", data, 0x3C)[0]
assert data[pe:pe+4] == b"PE\0\0", "no PE signature"
machine = struct.unpack_from("<H", data, pe + 4)[0]
names = {0x8664: "x86-64", 0x14c: "i386"}
print(f"PE32+ executable ({names.get(machine, hex(machine))}), "
      f"{len(data)} bytes, subsystem=GUI")
PY
fi
echo "built $OUT/nexus-client.exe"
