#!/usr/bin/env bash
# Build (and optionally sign) KaroSpace Agent.app with PyInstaller.
#
#   ./packaging/build_app.sh            # unsigned local build → dist/
#   KAROSPACE_CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" \
#     ./packaging/build_app.sh          # signed build (still needs notarize.sh)
#
# Run from the `app/` directory, in a venv with the app + its extras installed:
#   pip install -e '.[app]'
set -euo pipefail

cd "$(dirname "$0")/.."   # app/
HERE="packaging"

# 1. Icon: generate a .icns from the KaroSpace logo if we can find one and the
#    output doesn't exist yet. Non-fatal — the app just ships without a custom
#    icon otherwise.
LOGO="../../KaroSpace/assets/logo.png"
ICNS="$HERE/KaroSpaceAgent.icns"
if [[ ! -f "$ICNS" && -f "$LOGO" ]]; then
  echo "→ generating icon from $LOGO"
  ICONSET="$(mktemp -d)/icon.iconset"; mkdir -p "$ICONSET"
  for size in 16 32 64 128 256 512; do
    sips -z $size $size     "$LOGO" --out "$ICONSET/icon_${size}x${size}.png"     >/dev/null
    sips -z $((size*2)) $((size*2)) "$LOGO" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
  done
  iconutil -c icns "$ICONSET" -o "$ICNS" || echo "  (iconutil failed; continuing without an icon)"
fi
[[ -f "$ICNS" ]] && export KAROSPACE_ICNS="$(pwd)/$ICNS"

# 2. Wire signing into the PyInstaller build if an identity is provided.
if [[ -n "${KAROSPACE_CODESIGN_IDENTITY:-}" ]]; then
  export KAROSPACE_ENTITLEMENTS="$(pwd)/$HERE/entitlements.plist"
  echo "→ building SIGNED with: $KAROSPACE_CODESIGN_IDENTITY"
else
  echo "→ building UNSIGNED (set KAROSPACE_CODESIGN_IDENTITY to sign)"
fi

# 3. Build.
rm -rf build "dist/KaroSpace Agent.app"
pyinstaller "$HERE/KaroSpaceAgent.spec" --noconfirm --distpath dist --workpath build

echo
echo "✓ Built: dist/KaroSpace Agent.app"
if [[ -n "${KAROSPACE_CODESIGN_IDENTITY:-}" ]]; then
  echo "  Next: ./packaging/notarize.sh   (notarize + staple with Apple)"
else
  echo "  This is an UNSIGNED build — for wider distribution, re-run with"
  echo "  KAROSPACE_CODESIGN_IDENTITY set, then ./packaging/notarize.sh"
fi
