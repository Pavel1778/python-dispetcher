#!/usr/bin/env bash
# Build the NEXUS Debian package from the repository tree.
#
# Produces a real .deb with the standard layout, using only dpkg-deb (no
# network, no build daemon). The package ships a thin desktop client that opens
# the configured backend URL; it contains no server address and no secret.
#
#   packaging/debian/build-deb.sh [output-dir]
set -euo pipefail

VERSION="${NEXUS_VERSION:-2.0.0}"
ARCH="all"
PKG="nexus-client"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${1:-$ROOT/dist}"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

APP="$STAGE/${PKG}_${VERSION}_${ARCH}"
mkdir -p "$APP/DEBIAN" \
         "$APP/usr/bin" \
         "$APP/usr/share/applications" \
         "$APP/usr/share/icons/hicolor/192x192/apps" \
         "$APP/usr/share/icons/hicolor/512x512/apps" \
         "$APP/usr/share/doc/$PKG"

# --- payload ---------------------------------------------------------------
install -m 0755 "$ROOT/packaging/debian/nexus-client.py" "$APP/usr/bin/nexus-client"
install -m 0644 "$ROOT/packaging/debian/nexus.desktop" \
    "$APP/usr/share/applications/nexus.desktop"
install -m 0644 "$ROOT/orchestration/pwa/icon-192.png" \
    "$APP/usr/share/icons/hicolor/192x192/apps/nexus.png"
install -m 0644 "$ROOT/orchestration/pwa/icon-512.png" \
    "$APP/usr/share/icons/hicolor/512x512/apps/nexus.png"
install -m 0644 "$ROOT/LICENSE" "$APP/usr/share/doc/$PKG/copyright"
gzip -9n -c "$ROOT/README.md" > "$APP/usr/share/doc/$PKG/README.md.gz"

# --- control ---------------------------------------------------------------
INSTALLED_SIZE="$(du -sk "$APP/usr" | cut -f1)"
cat > "$APP/DEBIAN/control" <<EOF
Package: $PKG
Version: $VERSION
Section: devel
Priority: optional
Architecture: $ARCH
Depends: python3 (>= 3.10), xdg-utils
Installed-Size: $INSTALLED_SIZE
Maintainer: NEXUS <noreply@example.invalid>
Description: NEXUS AI development lab desktop client
 Opens the NEXUS autonomous development lab dashboard in the default browser.
 The client stores no credentials; the web UI's own login authenticates the
 session. Point it at a backend with \$NEXUS_URL or ~/.config/nexus/client.conf.
EOF

cat > "$APP/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ -x /usr/bin/update-desktop-database ]; then
    update-desktop-database -q /usr/share/applications || true
fi
exit 0
EOF
cat > "$APP/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if [ -x /usr/bin/update-desktop-database ]; then
    update-desktop-database -q /usr/share/applications || true
fi
exit 0
EOF
chmod 0755 "$APP/DEBIAN/postinst" "$APP/DEBIAN/postrm"

mkdir -p "$OUT"
dpkg-deb --build --root-owner-group "$APP" "$OUT/${PKG}_${VERSION}_${ARCH}.deb" >/dev/null
echo "built $OUT/${PKG}_${VERSION}_${ARCH}.deb"
