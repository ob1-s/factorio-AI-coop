#!/usr/bin/env bash
# Launch a Factorio instance detached from the calling shell (survives tool timeouts).
# usage: factorio-detach.sh <logfile> [args...]
LOG="$1"; shift
export WINEPREFIX="/home/ob1/Games/umu/umu-default"
export WINEDEBUG=-all
cd "/home/ob1/Games/umu/umu-default/drive_c/users/steamuser/AppData/Roaming/Factorio"
setsid nohup wine "C:\\Games\\Factorio\\bin\\x64\\factorio.exe" "$@" >"$LOG" 2>&1 </dev/null &
disown
echo "launched pid=$!"
