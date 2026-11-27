#!/bin/sh
# Usage: deploy/sync.sh [--install]   Shows what will change, asks, syncs, restarts the car service.
# --install also installs and enables the systemd unit (first deploy only).
set -e
cd "$(dirname "$0")/.."
H=pi@raspberrypi.local
export RSYNC_RSH="ssh -i $HOME/.ssh/picar_ed25519 -o IdentitiesOnly=yes"
RS="rsync -ai --delete --exclude __pycache__ --exclude .token --exclude .viewtoken --exclude settings.json --exclude calib.json --exclude test_*.py"
$RS --dry-run car dashboard "$H:picar/" | grep -v '/$' || true
printf "Deploy these changes? [y/N] "; read a; [ "$a" = y ] || exit 1
$RS car dashboard "$H:picar/"
if [ "$1" = "--install" ]; then
  $RSYNC_RSH $H 'sudo tee /etc/systemd/system/picar.service >/dev/null && sudo systemctl daemon-reload && sudo systemctl enable picar' < deploy/picar.service
fi
$RSYNC_RSH $H 'sudo systemctl restart picar && sleep 2 && systemctl is-active picar && journalctl -u picar -n 50 --no-pager | grep "open http" | tail -1'
