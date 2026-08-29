#!/usr/bin/env bash
set -euo pipefail
src="$(cd "$(dirname "$0")" && pwd)/skills/umami-health"
if [ ! -f "$src/SKILL.md" ]; then echo "未找到 $src/SKILL.md" >&2; exit 1; fi
dst="$HOME/.agents/skills/umami-health"
mkdir -p "$(dirname "$dst")"
rm -rf "$dst"
cp -R "$src" "$dst"
echo "✅ 已安装到 $dst"
echo "重启 ZCode / Codex 后即可识别技能 umami-health"
