#!/bin/bash
# cursor账号管理器 · macOS Nuitka 打包（.app + .dmg）
# 用法：./build_mac.sh
# 产物：SandClaimer-<版本>.dmg（项目根目录与 nuitka-out/ 各一份）
# 版本号取自 sand_patch.TOOL_VERSION。只能在 Mac 上跑。
set -euo pipefail
cd "$(dirname "$0")" || exit 1

APP_NAME="cursor账号管理器"
BUNDLE_ID="com.sand.claimer"
PIP_MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"

if [ -n "${PYTHON:-}" ]; then
  PY="$PYTHON"
elif [ -x /Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11 ]; then
  PY=/Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11
else
  PY="$(command -v python3)"
fi

if [ -z "$PY" ] || ! command -v "$PY" >/dev/null 2>&1 && [ ! -x "$PY" ]; then
  echo "[X] 找不到 Python。请安装 3.11：https://www.python.org/downloads/macos/"
  exit 1
fi

echo "==================================================="
echo "   Nuitka 打包 $APP_NAME → .dmg"
echo "==================================================="
echo "Python：$($PY --version 2>&1)  ($PY)"

VER="$($PY -c 'from app.sand_patch import TOOL_VERSION; print(TOOL_VERSION)')"
if [ -z "$VER" ]; then
  echo "[X] 读不到 TOOL_VERSION（确认 app/sand_patch.py 在仓库里）"
  exit 1
fi
echo "版本 = $VER"
DMG="SandClaimer-$VER.dmg"
VENV=".nuitka_venv"
JOBS="$(sysctl -n hw.ncpu 2>/dev/null || echo 4)"

echo "[1/6] 建立打包虚拟环境 $VENV ..."
"$PY" -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install -U pip -i "$PIP_MIRROR"

echo "[2/6] 安装依赖 + Nuitka ..."
python -m pip install -U \
  -r requirements-mac.txt \
  "nuitka>=2.0" ordered-set zstandard \
  -i "$PIP_MIRROR"

echo "[3/6] 生成 .icns ..."
ICON_ARG=()
if [ -f "assets/icon-1024.png" ] && command -v sips >/dev/null 2>&1 && command -v iconutil >/dev/null 2>&1; then
  rm -rf icon.iconset icon.icns
  mkdir -p icon.iconset
  for s in 16 32 128 256 512; do
    sips -z "$s" "$s" assets/icon-1024.png --out "icon.iconset/icon_${s}x${s}.png" >/dev/null
    d=$((s * 2))
    sips -z "$d" "$d" assets/icon-1024.png --out "icon.iconset/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns icon.iconset -o icon.icns
  rm -rf icon.iconset
  ICON_ARG=(--macos-app-icon=icon.icns)
  echo "    已生成 icon.icns"
else
  echo "    跳过图标"
fi

echo "[4/6] Nuitka 编译 .app（较慢，请耐心等待）..."
rm -rf nuitka-out
mkdir -p nuitka-out
python -m nuitka \
  --standalone \
  --assume-yes-for-downloads \
  --static-libpython=no \
  --macos-create-app-bundle \
  --macos-app-name="$APP_NAME" \
  --macos-app-version="$VER" \
  --macos-signed-app-name="$BUNDLE_ID" \
  --enable-plugin=pywebview \
  --include-data-dir=web=web \
  --include-module=webview.platforms.cocoa \
  --nofollow-import-to=tests,app.preview_server,unittest \
  --company-name="夜雨微寒" \
  --product-name="$APP_NAME" \
  --product-version="$VER" \
  --output-filename="$APP_NAME" \
  --output-dir=nuitka-out \
  --jobs="$JOBS" \
  --lto=no \
  "${ICON_ARG[@]}" \
  --include-package=app \
  app/__main__.py

APP_PATH=""
if [ -d "nuitka-out/${APP_NAME}.app" ]; then
  APP_PATH="nuitka-out/${APP_NAME}.app"
elif [ -d "nuitka-out/app.app" ]; then
  mv "nuitka-out/app.app" "nuitka-out/${APP_NAME}.app"
  APP_PATH="nuitka-out/${APP_NAME}.app"
else
  APP_PATH="$(find nuitka-out -maxdepth 1 -name '*.app' -print | head -n1 || true)"
fi
if [ -z "$APP_PATH" ] || [ ! -d "$APP_PATH" ]; then
  echo "[X] 没生成 .app，请看上面 Nuitka 报错。"
  exit 1
fi

echo "[5/6] 写入权限说明 + ad-hoc 签名 ..."
PLIST="$APP_PATH/Contents/Info.plist"
PB=/usr/libexec/PlistBuddy
$PB -c "Set :CFBundleShortVersionString $VER" "$PLIST" 2>/dev/null || \
  $PB -c "Add :CFBundleShortVersionString string $VER" "$PLIST" 2>/dev/null || true
$PB -c "Set :CFBundleVersion $VER" "$PLIST" 2>/dev/null || \
  $PB -c "Add :CFBundleVersion string $VER" "$PLIST" 2>/dev/null || true
$PB -c "Add :NSAppleEventsUsageDescription string 切号时需要自动退出并重启 Cursor" "$PLIST" 2>/dev/null || \
  $PB -c "Set :NSAppleEventsUsageDescription 切号时需要自动退出并重启 Cursor" "$PLIST" 2>/dev/null || true
$PB -c "Add :NSHighResolutionCapable bool true" "$PLIST" 2>/dev/null || true
codesign --force --deep --sign - "$APP_PATH" || true
xattr -cr "$APP_PATH" 2>/dev/null || true

echo "[6/6] 生成 ${DMG}（含 Applications 拖放安装）..."
DMG_STAGE="nuitka-out/dmg-stage"
rm -rf "$DMG_STAGE"
mkdir -p "$DMG_STAGE"
ditto "$APP_PATH" "$DMG_STAGE/$(basename "$APP_PATH")"
ln -s /Applications "$DMG_STAGE/Applications"
rm -f "nuitka-out/$DMG" "$DMG"
hdiutil create -volname "$APP_NAME $VER" -srcfolder "$DMG_STAGE" -ov -format UDZO "nuitka-out/$DMG"
cp -f "nuitka-out/$DMG" "$DMG"

echo
echo "==================================================="
echo "   完成"
echo "   App: $(pwd)/$APP_PATH"
echo "   DMG: $(pwd)/$DMG"
echo "==================================================="
