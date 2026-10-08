#!/bin/bash
# Supervisor poller Muse Backend — pastikan tetap jalan.
# Aturan scan: /proc, PID numerik, skip diri sendiri & parent, string terpisah.
ME=$(basename "$0")
VIC="agent-poll""er.py"
FOUND=0
SELF=$$; PAR=$PPID
for p in $(ls /proc 2>/dev/null | grep -E '^[0-9]+$'); do
  [ "$p" = "$SELF" ] && continue
  [ "$p" = "$PAR" ] && continue
  cl=$(tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null)
  case "$cl" in
    *"$VIC"*)
      en=$(tr '\0' '\n' < /proc/$p/environ 2>/dev/null | grep '^AGENT_NAME=Muse Backend$')
      [ -n "$en" ] && FOUND=1 ;;
  esac
done
if [ "$FOUND" = "0" ]; then
  echo "[supervisor] poller mati, restart..."
  cd ~/workspace/command-center-3d
  CC_URL="https://command-center-3d-production-7440.up.railway.app" \
  CC_TOKEN="$(cat ~/.cc3d-token)" \
  AGENT_NAME="Muse Backend" CC_AUTO_REPLY=0 \
  nohup python3 -u agent-poller.py > /tmp/cc3d-poller.log 2>&1 &
else
  echo "[supervisor] poller hidup"
fi
