#!/usr/bin/env bash
# Sign, notarize, and staple KaroSpace Agent.app for wider distribution.
#
# Prerequisites (yours — Apple Developer Program):
#   * A "Developer ID Application" certificate in your login Keychain.
#   * An App Store Connect API key (Keys tab → .p8), OR an app-specific password.
#
# Configure via env (fill these in — nothing here is baked in):
#   KAROSPACE_CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
#   # Notarization credentials, EITHER a stored notarytool profile:
#   KAROSPACE_NOTARY_PROFILE="karospace-notary"   # see one-time setup below
#   # …OR an API key trio:
#   KAROSPACE_NOTARY_KEY_ID=XXXXXXXXXX
#   KAROSPACE_NOTARY_KEY_ISSUER=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
#   KAROSPACE_NOTARY_KEY_PATH=/path/to/AuthKey_XXXXXXXXXX.p8
#
# One-time: store credentials so you don't pass them each run:
#   xcrun notarytool store-credentials karospace-notary \
#     --key /path/to/AuthKey_XXXX.p8 --key-id XXXX --issuer <issuer-uuid>
#
# Then:  ./packaging/notarize.sh
set -euo pipefail

cd "$(dirname "$0")/.."   # app/
APP="dist/KaroSpace Agent.app"
ENTITLEMENTS="packaging/entitlements.plist"
ZIP="dist/KaroSpaceAgent.zip"

: "${KAROSPACE_CODESIGN_IDENTITY:?set KAROSPACE_CODESIGN_IDENTITY to your Developer ID Application identity}"
[[ -d "$APP" ]] || { echo "error: $APP not found — run ./packaging/build_app.sh first"; exit 1; }

# 1. Sign every nested binary, then the bundle, with the hardened runtime and a
#    secure timestamp. (--deep is deprecated but remains the pragmatic way to
#    cover the many dylibs PyInstaller nests; entitlements apply to the main
#    executable.)
echo "→ signing $APP"
codesign --force --deep --timestamp --options runtime \
  --entitlements "$ENTITLEMENTS" \
  --sign "$KAROSPACE_CODESIGN_IDENTITY" \
  "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"

# 2. Zip for submission (notarytool wants a zip/dmg/pkg, not a raw .app).
echo "→ zipping for notarization"
rm -f "$ZIP"
/usr/bin/ditto -c -k --keepParent "$APP" "$ZIP"

# 3. Submit and wait.
echo "→ submitting to Apple (this can take a few minutes)"
if [[ -n "${KAROSPACE_NOTARY_PROFILE:-}" ]]; then
  xcrun notarytool submit "$ZIP" --keychain-profile "$KAROSPACE_NOTARY_PROFILE" --wait
else
  : "${KAROSPACE_NOTARY_KEY_ID:?set KAROSPACE_NOTARY_KEY_ID or KAROSPACE_NOTARY_PROFILE}"
  : "${KAROSPACE_NOTARY_KEY_ISSUER:?set KAROSPACE_NOTARY_KEY_ISSUER}"
  : "${KAROSPACE_NOTARY_KEY_PATH:?set KAROSPACE_NOTARY_KEY_PATH}"
  xcrun notarytool submit "$ZIP" \
    --key "$KAROSPACE_NOTARY_KEY_PATH" \
    --key-id "$KAROSPACE_NOTARY_KEY_ID" \
    --issuer "$KAROSPACE_NOTARY_KEY_ISSUER" \
    --wait
fi

# 4. Staple the ticket into the app so it verifies offline.
echo "→ stapling"
xcrun stapler staple "$APP"
xcrun stapler validate "$APP"
spctl --assess --type execute --verbose=4 "$APP" || true

echo
echo "✓ Notarized + stapled: $APP"
echo "  Ship it: zip/dmg the .app; it now opens with a normal double-click."
