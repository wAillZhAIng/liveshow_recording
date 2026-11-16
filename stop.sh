#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ -x .venv/bin/python ]; then
  exec .venv/bin/python whatnot_launcher.py stop
fi
echo '没有找到已安装的 Whatnot 监控环境。'
