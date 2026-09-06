#!/bin/bash
# =============================================================================
#  Sand 资格领取器 · 一键打包成 Mac App(.dmg)
#  给【有 Mac 的群友】用：双击本文件，等几分钟，同目录生成「SandClaimer-<版本>.dmg」。
#  之后把 .dmg 发群，其他 Mac 用户下载→打开→拖进“应用程序”→双击使用。
#
#  ⚠️ 只能在 macOS 上运行（.dmg 依赖 hdiutil/codesign，Windows 无法生成）。
#  首次双击若提示“无法打开/未验证的开发者”：右键点本文件 → 打开；
#  或先在“终端”执行一次：  chmod +x *.command
#  版本号统一取自 sand_patch.py 的 TOOL_VERSION，产物名自动带版本，无需手改。
# =============================================================================
set -e
cd "$(dirname "$0")" || exit 1

APP_NAME="Sand资格领取器"
BUNDLE_ID="com.sand.claimer"
PIP_MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"

echo "==================================================="
echo "   打包 $APP_NAME → Mac App(.dmg)"
echo "==================================================="

if ! command -v python3 >/dev/null 2>&1; then
  echo "[X] 没找到 python3。请先安装 Python 3：https://www.python.org/downloads/macos/"
  read -r -p "按回车退出..." _
  exit 1
fi
echo "使用 Python：$(python3 --version 2>&1)"

# [0/6] 读版本号（单一来源：sand_patch.TOOL_VERSION）
VER="$(python3 -c 'import sand_patch; print(sand_patch.TOOL_VERSION)' 2>/dev/null || true)"
if [ -z "$VER" ]; then
  echo "[X] 读不到 TOOL_VERSION（确认 sand_patch.py 在同目录）。"
  read -r -p "按回车退出..." _
  exit 1
fi
echo "版本 = $VER"
DMG="SandClaimer-$VER.dmg"

VENV=".build_venv"
echo "[1/6] 建立打包用虚拟环境 $VENV ..."
python3 -m venv "$VENV"
# shellcheck disable=SC1090
source "$VENV/bin/activate"
python -m pip install -U pip -i "$PIP_MIRROR"

echo "[2/6] 安装依赖 + pyinstaller ..."
python -m pip install -U \
  requests websocket-client pywebview \
  pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-WebKit \
  pyinstaller pyinstaller-hooks-contrib \
  -i "$PIP_MIRROR"

# [3/6] 生成 .icns 图标（有 assets/icon-1024.png 且工具可用时；否则跳过用默认图标）
ICON_ARG=""
if [ -f "assets/icon-1024.png" ] && command -v sips >/dev/null 2>&1 && command -v iconutil >/dev/null 2>&1; then
  echo "[3/6] 生成 .icns 图标 ..."
  rm -rf icon.iconset icon.icns
  mkdir -p icon.iconset
  for s in 16 32 128 256 512; do
    sips -z $s $s        assets/icon-1024.png --out "icon.iconset/icon_${s}x${s}.png"    >/dev/null 2>&1 || true
    d=$((s*2))
    sips -z $d $d        assets/icon-1024.png --out "icon.iconset/icon_${s}x${s}@2x.png" >/dev/null 2>&1 || true
  done
  if iconutil -c icns icon.iconset -o icon.icns >/dev/null 2>&1; then
    ICON_ARG="--icon=icon.icns"
  fi
  rm -rf icon.iconset
else
  echo "[3/6] 跳过图标（无 assets/icon-1024.png 或无 sips/iconutil）"
fi

echo "[4/6] 用 pyinstaller 打包 .app（较慢，请耐心等待）..."
rm -rf build dist
pyinstaller --noconfirm --clean --windowed \
  --name "$APP_NAME" \
  --osx-bundle-identifier "$BUNDLE_ID" \
  $ICON_ARG \
  --add-data "web:web" \
  --collect-all webview \
  --hidden-import sand_api \
  --hidden-import accounts \
  --hidden-import sand_patch \
  --hidden-import resolve \
  --hidden-import local_cursor \
  --hidden-import browser_login \
  --hidden-import websocket \
  --hidden-import webview.platforms.cocoa \
  --hidden-import objc \
  --hidden-import Foundation \
  --hidden-import AppKit \
  --hidden-import WebKit \
  app.py

APP_PATH="dist/$APP_NAME.app"
if [ ! -d "$APP_PATH" ]; then
  echo "[X] 打包失败：没生成 $APP_PATH。请把上面的报错发我。"
  deactivate || true
  read -r -p "按回车退出..." _
  exit 1
fi

echo "[5/6] 写入版本号 + 权限说明 + 临时(ad-hoc)签名 ..."
PLIST="$APP_PATH/Contents/Info.plist"
PB=/usr/libexec/PlistBuddy
# 版本号
$PB -c "Set :CFBundleShortVersionString $VER" "$PLIST" 2>/dev/null || \
  $PB -c "Add :CFBundleShortVersionString string $VER" "$PLIST" 2>/dev/null || true
$PB -c "Set :CFBundleVersion $VER" "$PLIST" 2>/dev/null || \
  $PB -c "Add :CFBundleVersion string $VER" "$PLIST" 2>/dev/null || true
# 切号需要自动退出/重启 Cursor（自动化权限）
$PB -c "Add :NSAppleEventsUsageDescription string '切号时需要自动退出并重启 Cursor'" "$PLIST" 2>/dev/null || \
  $PB -c "Set :NSAppleEventsUsageDescription '切号时需要自动退出并重启 Cursor'" "$PLIST" 2>/dev/null || true
$PB -c "Add :NSHighResolutionCapable bool true" "$PLIST" 2>/dev/null || true
# 改过 plist 会让签名失效 → 重新 ad-hoc 签名（Apple 芯片必须签名才能运行）
codesign --force --deep --sign - "$APP_PATH" 2>/dev/null || true
xattr -cr "$APP_PATH" 2>/dev/null || true

echo "[6/6] 生成 .dmg ..."
rm -f "$DMG"
hdiutil create -volname "$APP_NAME $VER" -srcfolder "$APP_PATH" -ov -format UDZO "$DMG"

deactivate || true

echo
echo "==================================================="
if [ -f "$DMG" ]; then
  echo "   打包完成： $(pwd)/$DMG"
  echo "   把这个 .dmg 发群即可。Mac 用户：下载→打开→拖进“应用程序”→双击运行。"
  echo "   （首次运行请右键图标→打开）"
else
  echo "   [!] 没找到 .dmg，App 已在 $APP_PATH，可用“磁盘工具”手动打包或直接压缩 .app。"
fi
echo "==================================================="
read -r -p "按回车退出..." _
