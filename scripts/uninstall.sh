#!/usr/bin/env bash
# RogueLink uninstaller. Stops the daemon, removes the install prefix and
# systemd unit, optionally clears /etc/roguelink, /var/lib/roguelink, and
# /var/log/roguelink when --purge is passed.

set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "RogueLink uninstall must be run as root (sudo)." >&2
  exit 1
fi

PURGE=0
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    *) echo "Unknown arg: $arg" >&2; exit 2 ;;
  esac
done

log() { printf "[roguelink-uninstall] %s\n" "$*"; }

log "Stopping roguelinkd"
systemctl stop roguelinkd.service 2>/dev/null || true
systemctl disable roguelinkd.service 2>/dev/null || true
rm -f /etc/systemd/system/roguelinkd.service
systemctl daemon-reload

log "Removing /opt/roguelink and CLI launchers"
rm -rf /opt/roguelink
rm -f /usr/local/bin/roguelink
rm -f /usr/local/bin/ghostlink

log "Flushing nftables tables (best effort)"
nft delete table inet roguelink 2>/dev/null || true
nft delete table ip roguelink_nat 2>/dev/null || true

if [[ ${PURGE} -eq 1 ]]; then
  log "Removing /etc/roguelink, /var/lib/roguelink, /var/log/roguelink"
  rm -rf /etc/roguelink /var/lib/roguelink /var/log/roguelink /run/roguelink
fi

log "Done."
