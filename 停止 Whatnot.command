#!/bin/zsh
cd -- "${0:A:h}" || exit 1
exec sh ./stop.sh
