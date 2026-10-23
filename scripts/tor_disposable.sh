#!/usr/bin/env bash
#
# A disposable, client-only Tor you can delete with one command.
#
#   scripts/tor_disposable.sh start     # unpack, configure, run in a screen session
#   scripts/tor_disposable.sh status    # is it up, and did it bootstrap
#   scripts/tor_disposable.sh stop      # stop the process
#   scripts/tor_disposable.sh clean     # stop and delete everything it wrote
#
# Why it is built this way:
#
# * **no root, no package installed.** `apt-get download` fetches the distro's signed .deb
#   and `dpkg-deb -x` unpacks it into the disposable directory. Nothing is written to /usr,
#   no service is enabled, and `clean` leaves no trace. Installing system-wide needs sudo,
#   and a tool that asks for sudo to start a client-only daemon is asking for more than it
#   needs.
# * **client only.** ClientOnly 1 means this instance never relays for anyone else, so it
#   cannot become part of the network being observed.
# * **jailed, with home hidden.** It needs the network, so it cannot have the collector's
#   no-network jail. It does not need to read the operator's home directory, so /home and
#   /root are empty tmpfs mounts. A client-only daemon that can read ~/.hermes/.env is a
#   liability.
# * **a unix socket, not 9050.** A unix socket crosses a network namespace boundary, which
#   is what lets the collector run with no interfaces at all and still reach Tor.
#
# The unix socket path is capped near 108 bytes by the kernel, so the directory must stay
# short. The default is under ~/.cache for that reason.
set -euo pipefail

DIR="${ONIONTRACE_TOR_DIR:-$HOME/.cache/oniontrace-tor}"
SOCK="$DIR/tor.sock"
SESSION="${ONIONTRACE_TOR_SESSION:-oniontor}"

say() { printf '%s\n' "$*" >&2; }

need_tools() {
  for tool in bwrap apt-get dpkg-deb; do
    command -v "$tool" >/dev/null 2>&1 || { say "missing required tool: $tool"; exit 3; }
  done
}

unpack() {
  mkdir -p "$DIR" && chmod 700 "$DIR"
  if [[ ! -x "$DIR/root/usr/bin/tor" ]]; then
    say "fetching tor from the distribution (no root required, nothing installed)"
    ( cd "$DIR" && apt-get download tor >/dev/null 2>&1 )
    local deb
    deb="$(ls -1t "$DIR"/tor_*.deb 2>/dev/null | head -1)"
    [[ -n "$deb" ]] || { say "apt-get download produced no .deb"; exit 4; }
    dpkg-deb -x "$deb" "$DIR/root"
  fi

  cat > "$DIR/torrc" <<'TORRC'
# Client only: this instance never relays for anyone else.
ClientOnly 1
SocksPort unix:/mnt/tor.sock
DataDirectory /mnt/data
Log notice file /mnt/tor.log
AvoidDiskWrites 1
TORRC
  mkdir -p "$DIR/data"

  cat > "$DIR/run.sh" <<RUNNER
#!/usr/bin/env bash
set -euo pipefail
exec bwrap \\
  --die-with-parent --new-session \\
  --unshare-pid --unshare-ipc --unshare-uts --unshare-cgroup \\
  --ro-bind / / \\
  --tmpfs /home \\
  --tmpfs /root \\
  --bind "$DIR" /mnt \\
  --dev /dev --proc /proc \\
  /mnt/root/usr/bin/tor -f /mnt/torrc
RUNNER
  chmod +x "$DIR/run.sh"
}

cmd_start() {
  need_tools
  unpack
  if [[ -S "$SOCK" ]]; then
    say "already running: $SOCK"
    return 0
  fi
  rm -f "$DIR/tor.log"

  if command -v screen >/dev/null 2>&1; then
    screen -dmS "$SESSION" "$DIR/run.sh"
    say "started in screen session '$SESSION' (attach with: screen -r $SESSION)"
  else
    say "screen not found; starting detached with nohup instead"
    nohup "$DIR/run.sh" >/dev/null 2>&1 &
  fi

  say "waiting for bootstrap..."
  for _ in $(seq 1 60); do
    if grep -q "Bootstrapped 100" "$DIR/tor.log" 2>/dev/null && [[ -S "$SOCK" ]]; then
      say "ready: $SOCK"
      return 0
    fi
    sleep 1
  done
  say "tor did not bootstrap within 60s; see $DIR/tor.log"
  exit 5
}

cmd_status() {
  if [[ -S "$SOCK" ]]; then
    echo "socket:     $SOCK"
  else
    echo "socket:     absent"
  fi
  if grep -q "Bootstrapped 100" "$DIR/tor.log" 2>/dev/null; then
    echo "bootstrap:  100%"
  else
    echo "bootstrap:  not complete"
  fi
  if command -v screen >/dev/null 2>&1; then
    screen -ls 2>/dev/null | grep -q "$SESSION" && echo "session:    $SESSION (detached)" || echo "session:    none"
  fi
}

cmd_stop() {
  if command -v screen >/dev/null 2>&1 && screen -ls 2>/dev/null | grep -q "$SESSION"; then
    screen -S "$SESSION" -X quit || true
  fi
  # -x matches the process name exactly: a -f pattern can match the shell that
  # invoked this script, which is how a cleanup step kills its own caller.
  pkill -x tor 2>/dev/null || true
  sleep 1
  rm -f "$SOCK"
  say "stopped"
}

cmd_clean() {
  cmd_stop
  rm -rf "$DIR"
  say "removed $DIR"
}

case "${1:-}" in
  start)  cmd_start ;;
  status) cmd_status ;;
  stop)   cmd_stop ;;
  clean)  cmd_clean ;;
  *)      sed -n '2,28p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
