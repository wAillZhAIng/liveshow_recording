#!/bin/zsh
cd -- "${0:A:h}" || exit 1
if [[ ! -x .venv/bin/python ]]; then
  print -u2 '请先运行「启动 Whatnot」完成首次安装。'
  exit 1
fi
PYTHONPATH="$PWD/src" exec .venv/bin/python -m whatnot_monitor.accounts_ui
