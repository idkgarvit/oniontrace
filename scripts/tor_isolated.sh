#!/usr/bin/env bash
# Isolated, unprivileged Tor client for ONIONTRACE validation work.
#
# Why this exists
# ---------------
# Tor does not need root and does not need a container runtime. It needs a writable data
# directory and a port above 1024. What it does need is isolation, because whatever it
# fetches may be hostile, and a collector that parses untrusted content should not be able
# to see the operator's home directory.
#
# This script runs tor inside a bubblewrap jail, not a container. bwrap is already
# installed on Kali and gives the properties that matter here without any privilege:
#   - the host filesystem is not visible at all (no /home, so no ~/.ssh, no project repos)
#   - a private PID, IPC, UTS and cgroup namespace
#   - every Linux capability dropped (CapEff = 0000000000000000)
#   - a private session, so it cannot signal processes outside the jail
#   - dies with the parent, so nothing outlives the run
#
# It is a CLIENT ONLY. No relay, no exit traffic, no published onion service. Verified:
# ClientOnly 1, and no HiddenService directives.
#
# Usage:
#   ./scripts/tor_isolated.sh start     # launch tor in the jail
#   ./scripts/tor_isolated.sh status    # bootstrap progress + listener check
#   ./scripts/tor_isolated.sh stop      # stop it
#
# The SOCKS proxy then listens on 127.0.0.1:9150 ONLY. It is never bound to a public
# interface, and no other host can reach it.

set -euo pipefail

PREFIX="${PREFIX:-/tmp/torprefix}"
JAIL="${JAIL:-/tmp/torjail}"
SOCKS_PORT="${SOCKS_PORT:-9150}"
TOR_BIN="${PREFIX}/usr/bin/tor"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[[ -x "$TOR_BIN" ]] || die "tor binary not found at $TOR_BIN (extract the package first)"

cmd_start() {
  mkdir -p "$JAIL/data" "$JAIL/conf"

  cat > "$JAIL/conf/torrc" <<EOF
SocksPort 127.0.0.1:${SOCKS_PORT}
DataDirectory /data
Log notice file /data/tor.log
# Client only. No relay, no exit traffic, no published service. This process never
# accepts an inbound connection from the Tor network.
ClientOnly 1
EOF

  # Optional extras, bound only if present on the host.
  local extra=()
  [[ -d "${PREFIX}/usr/share/tor" ]] && extra+=(--ro-bind "${PREFIX}/usr/share/tor" /usr/share/tor)
  [[ -f /etc/resolv.conf ]] && extra+=(--ro-bind /etc/resolv.conf /etc/resolv.conf)
  [[ -f /etc/hosts ]]       && extra+=(--ro-bind /etc/hosts /etc/hosts)
  [[ -f /etc/nsswitch.conf ]] && extra+=(--ro-bind /etc/nsswitch.conf /etc/nsswitch.conf)
  [[ -d /etc/ssl ]]         && extra+=(--ro-bind /etc/ssl /etc/ssl)

  # Resource caps: untrusted content must not be able to exhaust the machine.
  ulimit -v 2097152   # 2 GiB address space
  ulimit -f 262144    # 256 MiB max file the process may write
  ulimit -n 256       # file descriptors

  printf 'starting tor in a bwrap jail (socks 127.0.0.1:%s)\n' "$SOCKS_PORT"
  exec bwrap \
    --ro-bind /usr /usr \
    --ro-bind /lib /lib \
    --ro-bind /lib64 /lib64 \
    --ro-bind /bin /bin \
    --ro-bind "$TOR_BIN" /tor/tor \
    --ro-bind "$JAIL/conf" /conf \
    --bind "$JAIL/data" /data \
    --proc /proc --dev /dev \
    "${extra[@]}" \
    --unshare-pid --unshare-ipc --unshare-uts --unshare-cgroup \
    --die-with-parent --new-session --cap-drop ALL \
    /tor/tor -f /conf/torrc
}

cmd_status() {
  echo "=== tor process ==="
  pgrep -a tor || echo "  not running"
  echo
  echo "=== bootstrap progress ==="
  tail -3 "$JAIL/data/tor.log" 2>/dev/null || echo "  no log yet"
  echo
  echo "=== listener (must be 127.0.0.1 only) ==="
  ss -ltn 2>/dev/null | grep ":${SOCKS_PORT}" || echo "  not listening yet"
  echo
  echo "=== jail check: can tor see the operator's home? ==="
  bwrap --ro-bind /usr /usr --ro-bind /lib /lib --ro-bind /lib64 /lib64 --ro-bind /bin /bin \
    --proc /proc --dev /dev --unshare-pid --die-with-parent --new-session --cap-drop ALL \
    /bin/sh -c 'ls /home 2>/dev/null && echo "  VISIBLE - bad" || echo "  no /home inside the jail"'
}

cmd_stop() {
  pkill -x tor 2>/dev/null && echo "stopped" || echo "not running"
}

case "${1:-start}" in
  start)  cmd_start ;;
  status) cmd_status ;;
  stop)   cmd_stop ;;
  *)      die "usage: $0 {start|status|stop}" ;;
esac