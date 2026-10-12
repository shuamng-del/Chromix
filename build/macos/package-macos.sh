#!/usr/bin/env bash
# Package a native macOS Chromix build into the SDK-compatible ZIP bundle.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP="${1:?usage: package-macos.sh /path/to/Chromium.app [dest] [arch]}"
DEST="${2:-$REPO/dist}"
ARCH="${3:-$(uname -m)}"
case "$ARCH" in
  x86_64|amd64) ARCH=x64 ;;
  aarch64) ARCH=arm64 ;;
  x64|arm64) ;;
  *) echo "unsupported macOS package architecture: $ARCH" >&2; exit 2 ;;
esac
STAGE="$DEST/chromix"
FONTS_SRC="${CHROMIX_FONTS_DIR:-$REPO/assets/fonts}"

if [ ! -d "$APP" ] || [ "$(basename "$APP")" != "Chromium.app" ]; then
  echo "Chromium.app is missing: $APP" >&2
  exit 1
fi
BINARY="$APP/Contents/MacOS/Chromium"
if [ ! -x "$BINARY" ]; then
  echo "Chromium executable is missing: $BINARY" >&2
  exit 1
fi
CHROMIUM_LICENSE="${CHROMIX_CHROMIUM_LICENSE:-$(cd "$APP/../../.." && pwd)/LICENSE}"
if [ ! -f "$CHROMIUM_LICENSE" ]; then
  echo "Chromium license is missing: $CHROMIUM_LICENSE" >&2
  exit 1
fi
for required in fonts.conf.template NOTICE SOURCE.md; do
  if [ ! -f "$FONTS_SRC/$required" ]; then
    echo "font bundle is incomplete: $FONTS_SRC/$required" >&2
    exit 1
  fi
done
if ! find "$FONTS_SRC" -maxdepth 1 -type f \( -iname '*.ttf' -o -iname '*.ttc' \) -print -quit | grep -q .; then
  echo "font bundle has no TrueType/OpenType assets: $FONTS_SRC" >&2
  exit 1
fi

python3 "$REPO/tools/verify_macos_bundle.py" --app "$APP" --arch "$ARCH"

rm -rf "$STAGE"
mkdir -p "$STAGE/fonts" "$DEST"
cp -a "$REPO/LICENSE" "$STAGE/LICENSE.chromix"
cp -a "$CHROMIUM_LICENSE" "$STAGE/LICENSE.chromium"
cp -a "$APP" "$STAGE/Chromium.app"
find "$FONTS_SRC" -maxdepth 1 -type f \( -iname '*.ttf' -o -iname '*.ttc' \) -exec cp -a {} "$STAGE/fonts/" \;
cp -a "$FONTS_SRC/fonts.conf.template" "$FONTS_SRC/NOTICE" \
  "$FONTS_SRC/SOURCE.md" "$STAGE/fonts/"

cat > "$STAGE/chromix" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$HERE/Chromium.app/Contents/MacOS/Chromium" "$@"
EOF
chmod 0755 "$STAGE/chromix" "$STAGE/Chromium.app/Contents/MacOS/Chromium"
find "$STAGE/fonts" -type f -exec chmod 0644 {} +

ASSET="$DEST/chromix-mac-$ARCH.zip"
rm -f "$ASSET"
(
  cd "$DEST"
  zip -X -q -r -y "$(basename "$ASSET")" chromix
)
python3 "$REPO/tools/verify_macos_bundle.py" --zip "$ASSET" --arch "$ARCH"
HASH="$(shasum -a 256 "$ASSET" | awk '{print $1}')"
(cd "$DEST" && shasum -a 256 chromix-*.zip > SHA256SUMS)
printf '==> %s  sha256=%s\n' "$ASSET" "$HASH"
