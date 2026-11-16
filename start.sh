#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
mode=start
if [ "${1:-}" = "--check" ]; then
  mode=check
fi
if [ -x .venv/bin/python ]; then
  exec .venv/bin/python whatnot_launcher.py "$mode"
fi
if [ -n "${WHATNOT_PYTHON:-}" ]; then
  set -- "$WHATNOT_PYTHON"
else
  set -- python3.12 python3.11 python3
fi
for candidate; do
  if command -v "$candidate" >/dev/null 2>&1 &&
    "$candidate" -c 'import sys; sys.exit(sys.version_info[:2] not in ((3, 11), (3, 12)))'
  then
    exec "$candidate" whatnot_launcher.py "$mode"
  fi
done
echo 'Python 3.11 or 3.12 is required. Install it or set WHATNOT_PYTHON to its path.' >&2
exit 1
