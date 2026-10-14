#!/bin/sh
# papers.app を作る(macOS)。Finder からダブルクリックで開ける形にする。
#   sh app/bundle.sh        → app/target/release/bundle/macos/papers.app
# 署名は手元用(ad-hoc)だけで、公証はしない(作った Mac で使う。HANDOFF.md「決定済み」)。作った .app は、このリポジトリの gui.py を
# 絶対パスで呼ぶ(リポジトリを動かしたら作り直す)。python3 と poppler は同梱しない(GUI_DESIGN.md「まだ無いもの」)。
# アイコンは icons/icon.png。手元だけの絵を使うなら icons/icon.local.png(追跡しない。1024×1024 の PNG)を置く。
set -eu
here=$(cd "$(dirname "$0")" && pwd)
command -v cargo >/dev/null 2>&1 || PATH="$HOME/.cargo/bin:$PATH"
# custom-protocol: Tauri を開発用でない作りにする(開発用の作りは、起動時に Dock の絵を icons/icon.png で上書きする)
cargo build --release --features custom-protocol --manifest-path "$here/Cargo.toml"

conf() { python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$here/tauri.conf.json" "$1"; }
name=$(conf productName)
out="$here/target/release/bundle/macos/$name.app"
rm -rf "$out"
mkdir -p "$out/Contents/MacOS" "$out/Contents/Resources"
cp "$here/target/release/papers-app" "$out/Contents/MacOS/papers-app"

# アイコン: icons/icon.png(あれば icons/icon.local.png)から、決まった大きさの組を作って .icns にする
icon="$here/icons/icon.png"
[ -f "$here/icons/icon.local.png" ] && icon="$here/icons/icon.local.png"
tmp=$(mktemp -d)
set="$tmp/icon.iconset"
mkdir "$set"
for s in 16 32 128 256 512; do
    sips -z "$s" "$s" "$icon" --out "$set/icon_${s}x${s}.png" >/dev/null
    sips -z "$((s * 2))" "$((s * 2))" "$icon" --out "$set/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$set" -o "$out/Contents/Resources/icon.icns"
rm -rf "$tmp"

cat > "$out/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key><string>papers-app</string>
    <key>CFBundleIdentifier</key><string>$(conf identifier)</string>
    <key>CFBundleName</key><string>$name</string>
    <key>CFBundleDisplayName</key><string>$name</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>$(conf version)</string>
    <key>CFBundleVersion</key><string>$(conf version)</string>
    <key>CFBundleIconFile</key><string>icon</string>
    <key>LSMinimumSystemVersion</key><string>11.0</string>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST
plutil -lint "$out/Contents/Info.plist" >/dev/null
codesign --force --sign - "$out"
echo "$out"
