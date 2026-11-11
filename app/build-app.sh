#!/bin/sh
# Builds Companion.app into /Applications.
# Signed with a "Companion Local Signing" certificate when the keychain has one (see README), so
# macOS keeps the mic and system audio permissions across rebuilds. Without it, it's ad-hoc signed
# and each rebuild asks again. Sharing the app would need a Developer ID signature and notarization.
set -eu
cd "$(dirname "$0")"

swift build -c release
app=/Applications/Companion.app
rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
cp .build/release/Companion "$app/Contents/MacOS/"
cp Companion.icns "$app/Contents/Resources/"  # the menu bar's waveform symbol
# Its own copy of companion and uv, so the app needs neither the repo nor anything installed.
# Rebuild to pick up changes to companion.
cp ../companion.py "$app/Contents/Resources/companion"
cp start.sh "$app/Contents/Resources/"
cp "$(command -v uv)" "$app/Contents/Resources/"
# Built now, after its source so companion sees it's current, since it can't write into the signed app later
cp ../system_audio.swift "$app/Contents/Resources/"
swiftc -O ../system_audio.swift -o "$app/Contents/Resources/.system_audio"

cat > "$app/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key><string>io.github.fortun3r.companion</string>
    <key>CFBundleName</key><string>Companion</string>
    <key>CFBundleExecutable</key><string>Companion</string>
    <key>CFBundleIconFile</key><string>Companion</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>0.2.2</string>
    <key>CFBundleVersion</key><string>4</string>
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
