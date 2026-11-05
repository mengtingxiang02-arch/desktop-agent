#!/bin/bash
# 在 Mac 上一条命令打包：跑测试 → 生成 .app → 做成 .dmg。
# 用法（在项目文件夹里）：bash scripts/package_mac.sh 1.1.0
# 结果：dist/DesktopAgent-v1.1.0-mac-arm64.dmg
set -euo pipefail                      # 任何一步出错就停下，不会带着错误继续往下做
cd "$(dirname "$0")/.."

VERSION="${1:?请写上版本号，例如：bash scripts/package_mac.sh 1.1.0}"
DMG="dist/DesktopAgent-v${VERSION}-mac-arm64.dmg"

echo "1/3 运行自动化测试…"
.venv/bin/python -m unittest -q

echo "2/3 用 PyInstaller 生成 .app（要一两分钟）…"
.venv/bin/pyinstaller --noconfirm --log-level WARN "Desktop Agent.spec"

echo "3/3 做成 .dmg…"
rm -rf dist/dmg && mkdir dist/dmg
cp -R "dist/Desktop Agent.app" dist/dmg/
ln -s /Applications dist/dmg/Applications       # 打开 dmg 后可以直接拖进"应用程序"
hdiutil create -volname "Desktop Agent" -srcfolder dist/dmg -ov -format UDZO "$DMG" > /dev/null
rm -rf dist/dmg

echo "完成：$DMG（$(du -h "$DMG" | cut -f1)）"
