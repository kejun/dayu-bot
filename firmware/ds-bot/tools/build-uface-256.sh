#!/usr/bin/env bash
# 从 U-Face 素材包生成 256×256 的 LVGL9 C 资源。
#
# 为什么要自己生成：官方只提供 128/192px，而 128px 画布里精灵只占 82×70，
# 在 480×480 屏幕上偏小。源帧是 480×480（精灵原生约 300px），下采样到
# 256 画布后精灵约 160px——正好是 128px 版的两倍，且是下采样、不模糊。
#
# 依赖：ImageMagick(magick) + Node.js + 素材包 tools/（含 pngjs）
#
# 用法：
#   ./tools/build-uface-256.sh <素材包目录> [输出目录]
# 默认输出到 components/uface/

set -euo pipefail

SRC_PACK="${1:?用法: $0 <U-Face-LVGL-Sprites-Final 目录> [输出目录]}"
OUT_DIR="${2:-$(cd "$(dirname "$0")/.." && pwd)/components/uface}"
SIZE=256

command -v magick >/dev/null || { echo "❌ 需要 ImageMagick(magick)"; exit 1; }
command -v node   >/dev/null || { echo "❌ 需要 Node.js"; exit 1; }
[ -d "$SRC_PACK/source/frames" ] || { echo "❌ 找不到 $SRC_PACK/source/frames"; exit 1; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

echo "① 源帧 -> ${SIZE}x${SIZE}"
for f in "$SRC_PACK"/source/frames/*.png; do
    magick "$f" -resize "${SIZE}x${SIZE}" "$TMP/$(basename "$f")"
done
ls "$TMP" | wc -l | xargs echo "   帧数:"

echo "② 导出 LVGL9 C 资源"
( cd "$SRC_PACK/tools" && [ -d node_modules ] || npm install --silent )
mkdir -p "$TMP/out"
( cd "$SRC_PACK/tools" && node export-lvgl.cjs --input "$TMP" --out "$TMP/out" --lvgl 9 )

echo "③ 安装到 $OUT_DIR"
mkdir -p "$OUT_DIR"
cp "$TMP/out/bluebot_assets.c" "$TMP/out/bluebot_assets.h" "$OUT_DIR/"
cp "$TMP/out/resource-stats.json" "$OUT_DIR/${SIZE}/" 2>/dev/null || {
    mkdir -p "$OUT_DIR/${SIZE}"; cp "$TMP/out/resource-stats.json" "$OUT_DIR/${SIZE}/"; }
echo "✅ 完成：$(grep -E 'BLUEBOT_WIDTH' "$OUT_DIR/bluebot_assets.h")"
