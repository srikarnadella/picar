#!/bin/sh
# Builds ~/Applications/Picar.app (Spotlight: type "picar"). Double-click it or keep it in the Dock.
# It runs app/picar_app.py from this folder with this folder's .venv, so keep the project where it is
# (rebuild after moving it).
set -e
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
APP="$HOME/Applications/Picar.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" dist

# icon: the dashboard's car mark, blue on the app's dark ground
.venv/bin/python - <<'PY'
from PIL import Image, ImageDraw
S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0)); d = ImageDraw.Draw(img)
d.rounded_rectangle([64, 64, S - 64, S - 64], radius=200, fill=(19, 18, 23, 255))
blue, w = (74, 138, 214, 255), 34
d.rounded_rectangle([392, 232, 632, 792], radius=60, outline=blue, width=w)
for x in (282, 742):
    for y in (300, 580):
        d.rounded_rectangle([x - 26, y, x + 26, y + 150], radius=18, fill=blue)
img.save("app/icon.png")
PY
mkdir -p dist/icon.iconset
for s in 16 32 64 128 256 512; do
  sips -z $s $s app/icon.png --out dist/icon.iconset/icon_${s}x${s}.png >/dev/null
  sips -z $((s * 2)) $((s * 2)) app/icon.png --out dist/icon.iconset/icon_${s}x${s}@2x.png >/dev/null
done
iconutil -c icns dist/icon.iconset -o "$APP/Contents/Resources/icon.icns"
rm -rf dist/icon.iconset

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Picar</string>
  <key>CFBundleDisplayName</key><string>Picar</string>
  <key>CFBundleIdentifier</key><string>local.picar.app</string>
  <key>CFBundleExecutable</key><string>Picar</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSLocalNetworkUsageDescription</key><string>Picar talks to your car on the local network.</string>
</dict></plist>
PLIST

cat > "$APP/Contents/MacOS/Picar" <<LAUNCH
#!/bin/sh
cd "$ROOT"
exec "$ROOT/.venv/bin/python" "$ROOT/app/picar_app.py" >> "\$HOME/.picar-app/app.log" 2>&1
LAUNCH
chmod +x "$APP/Contents/MacOS/Picar"
mkdir -p "$HOME/.picar-app"
mdimport "$APP" 2>/dev/null || true   # so Spotlight finds it straight away
echo "built $APP"
