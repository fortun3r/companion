#!/bin/sh
# Builds Companion.app into ~/Applications, so Spotlight and Login Items find it.
# Signed with a "Companion Local Signing" certificate when the keychain has one (see README), so
# macOS keeps the mic and system audio permissions across rebuilds. Without it, it's ad-hoc signed
# and each rebuild asks again. Sharing the app would need a Developer ID signature and notarization.
set -eu
cd "$(dirname "$0")"

swift build -c release
app="$HOME/Applications/Companion.app"
rm -rf "$app"
mkdir -p "$app/Contents/MacOS"
cp .build/release/Companion "$app/Contents/MacOS/"

cat > "$app/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key><string>io.github.fortun3r.companion</string>
    <key>CFBundleName</key><string>Companion</string>
    <key>CFBundleExecutable</key><string>Companion</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>0.1</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>LSMinimumSystemVersion</key><string>14.2</string>
    <key>LSUIElement</key><true/>
    <key>NSMicrophoneUsageDescription</key>
    <string>Companion transcribes the meeting on this Mac to answer questions as they come up.</string>
    <key>NSAudioCaptureUsageDescription</key>
    <string>Companion hears the other people on a call, even on headphones, by capturing what the Mac plays.</string>
</dict>
</plist>
EOF

identity=-
security find-certificate -c "Companion Local Signing" >/dev/null 2>&1 && identity="Companion Local Signing"
codesign --force --sign "$identity" "$app"
echo "built $app"
