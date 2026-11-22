#!/bin/bash
# 打包飞牛 FPK：app.tgz（程序）+ 元信息（manifest/cmd/config/wizard/图标）
#
# 两个不能错的地方（错了安装时会报「解压 app.tgz 失败」）：
#   1. 程序压缩包必须叫 app.tgz，飞牛系统按这个名字找；
#   2. tgz 内部路径必须是裸相对路径（fnosiptv.py），不能带 ./ 前缀。
set -eu
cd "$(dirname "$0")/pack"

VER="$(grep -E '^version *=' manifest | head -1 | cut -d= -f2 | tr -d ' ')"
OUT_DIR="$(cd .. && pwd)/releases"
mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/fnosiptv_${VER}.fpk"

# 运行期数据与字节码不进包
rm -rf app/__pycache__ app/data 2>/dev/null || true

# staging：程序文件铺在根，默认配置放 config/（安装后落到 $TRIM_PKGETC）
STG=/tmp/fnos_stg
rm -rf "$STG"; mkdir -p "$STG"
cp -a app/. "$STG"/
rm -rf "$STG/__pycache__"
mkdir -p "$STG/config"
cp -a config/. "$STG/config"/

# 在 staging 目录里就地打包，条目名才是不带 ./ 的裸路径
rm -f /tmp/app.tgz
( cd "$STG" && tar czf /tmp/app.tgz --owner=root --group=root --numeric-owner * )
rm -rf "$STG"

tar czf "$OUT" manifest cmd config wizard ICON.PNG ICON_256.PNG -C /tmp app.tgz
rm -f /tmp/app.tgz

echo "生成 $OUT  $(du -h "$OUT" | cut -f1)"
echo "--- 包内条目 ---"
tar tzf "$OUT" | sed 's/^/  /'
md5sum "$OUT"
