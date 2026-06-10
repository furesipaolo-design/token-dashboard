#!/bin/zsh
# Build TokenDashboard.app — a self-contained, relocatable macOS bundle.
#
#   ./macos/build.sh             build into dist/TokenDashboard.app
#   ./macos/build.sh --install   build and install into /Applications
#
# The bundle embeds the whole Python backend under Contents/Resources/backend,
# so the .app works from any location and on any Mac with python3 available.
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
REPO_DIR="${SCRIPT_DIR:h}"
APP_NAME="TokenDashboard"
VERSION="2.0.0"
DIST_DIR="$REPO_DIR/dist"
APP_DIR="$DIST_DIR/$APP_NAME.app"

echo "==> Compiling Swift shell"
mkdir -p "$DIST_DIR"
BIN="$DIST_DIR/$APP_NAME.bin"
if swiftc -O -target arm64-apple-macos12.0 "$SCRIPT_DIR/main.swift" -o "$DIST_DIR/.arm64" 2>/dev/null \
   && swiftc -O -target x86_64-apple-macos12.0 "$SCRIPT_DIR/main.swift" -o "$DIST_DIR/.x86_64" 2>/dev/null; then
  lipo -create "$DIST_DIR/.arm64" "$DIST_DIR/.x86_64" -output "$BIN"
  rm -f "$DIST_DIR/.arm64" "$DIST_DIR/.x86_64"
  echo "    universal binary (arm64 + x86_64)"
else
  swiftc -O "$SCRIPT_DIR/main.swift" -o "$BIN"
  echo "    native binary ($(uname -m) only)"
fi

echo "==> Assembling $APP_DIR"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources/backend"
mv "$BIN" "$APP_DIR/Contents/MacOS/$APP_NAME"

cat > "$APP_DIR/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDisplayName</key>      <string>Token Dashboard</string>
  <key>CFBundleExecutable</key>       <string>$APP_NAME</string>
  <key>CFBundleIdentifier</key>       <string>studio.pollo.token-dashboard</string>
  <key>CFBundleIconFile</key>         <string>AppIcon</string>
  <key>CFBundleName</key>             <string>$APP_NAME</string>
  <key>CFBundlePackageType</key>      <string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key>          <string>$VERSION</string>
  <key>LSMinimumSystemVersion</key>   <string>12.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.developer-tools</string>
  <key>NSHighResolutionCapable</key>  <true/>
  <key>NSAppTransportSecurity</key>
  <dict>
    <key>NSAllowsLocalNetworking</key><true/>
  </dict>
</dict>
</plist>
PLIST

cp "$SCRIPT_DIR/AppIcon.icns" "$APP_DIR/Contents/Resources/AppIcon.icns"

echo "==> Embedding Python backend"
BACKEND="$APP_DIR/Contents/Resources/backend"
cp "$REPO_DIR/cli.py" "$REPO_DIR/pricing.json" "$BACKEND/"
rsync -a --exclude '__pycache__' "$REPO_DIR/token_dashboard" "$BACKEND/"
rsync -a "$REPO_DIR/web" "$BACKEND/"
mkdir -p "$BACKEND/docs"
cp "$REPO_DIR/docs/logo.png" "$BACKEND/docs/logo.png"

echo "==> Signing (ad-hoc)"
codesign --force --sign - "$APP_DIR"

if [[ "${1:-}" == "--install" ]]; then
  # Installing over a running instance leaves the OLD app + server alive:
  # relaunching just activates the stale process and the old backend serves
  # the new frontend files — quit it first so the next launch is the new build.
  if pgrep -fq "/Applications/$APP_NAME.app/Contents/MacOS/$APP_NAME"; then
    echo "==> Quitting running $APP_NAME (so the new build actually loads)"
    osascript -e "quit app \"$APP_NAME\"" >/dev/null 2>&1 || true
    for _ in {1..20}; do
      pgrep -fq "/Applications/$APP_NAME.app/Contents/MacOS/$APP_NAME" || break
      sleep 0.25
    done
  fi
  echo "==> Installing into /Applications"
  rm -rf "/Applications/$APP_NAME.app"
  ditto "$APP_DIR" "/Applications/$APP_NAME.app"
  echo "    installed: /Applications/$APP_NAME.app"
fi

echo "==> Done: $APP_DIR"
