#!/usr/bin/env bash
# ページを更新したら実行: index.html と version.json の版番号を揃えて新しくする
set -euo pipefail
cd "$(dirname "$0")"
V="$(date +%Y%m%d%H%M%S)"
sed -i -E "s/window\.YOBS_VERSION = '[^']*';/window.YOBS_VERSION = '${V}';/" index.html
printf '{"version": "%s"}\n' "$V" > version.json
echo "version: $V"
