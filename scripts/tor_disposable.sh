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
  # A socket file is not a running tor. When the process dies the file stays behind, and a
  # start that trusted it would answer "already running" to a bridge that connects to
  # nothing. The failure then surfaces minutes later as a collector that cannot reach an
  # onion, which reads like a network problem and is not one.
  if [[ -S "$SOCK" ]] && pgrep -x tor >/dev/null 2>&1; then
    say "already running: $SOCK"
    return 0
  fi
  if [[ -S "$SOCK" ]]; then
    say "removing a socket with no tor behind it: $SOCK"
    rm -f "$SOCK"
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

# --------------------------------------------------------------------------- #
# serve: publish an onion of our own, so the collector can be tested against a
# live hidden service without touching anyone else's.
#
# The alternative is to point the acquisition path at a stranger's onion service,
# which is a third party who did not ask to be measured. Publishing our own costs
# a torrc and a python http.server, keeps the target inside the authorisation we
# hold, and exercises the same code path: real Tor, real rendezvous, real HTTP over
# a .onion. It also makes the test repeatable, because the address is ours.
# --------------------------------------------------------------------------- #
cmd_serve() {
  local webroot="${1:-}"
  local port="${2:-8080}"
  need_tools
  if [[ -z "$webroot" || ! -d "$webroot" ]]; then
    say "usage: $0 serve <directory-to-publish> [port]"
    exit 2
  fi
  webroot="$(cd "$webroot" && pwd)"
  unpack

  # A tor started by `start` holds this data directory's lock and owns the socket path.
  # Starting a publisher on top of it produced a tor that exited with "another Tor process is
  # running with the same data directory" after unlinking the live socket, so every connection
  # through the bridge went nowhere and looked like a network failure minutes later. Stop it
  # first, and refuse to continue if it will not stop.
  if command -v screen >/dev/null 2>&1; then
    screen -S "$SESSION" -X quit 2>/dev/null || true
  fi
  pkill -x tor 2>/dev/null || true
  for _ in $(seq 1 20); do
    pgrep -x tor >/dev/null 2>&1 || break
    sleep 0.5
  done
  if pgrep -x tor >/dev/null 2>&1; then
    say "a tor process survived the stop; refusing to start a second one on the same data directory"
    exit 7
  fi
  rm -f "$SOCK"

  mkdir -p "$DIR/hs"
  chmod 700 "$DIR/hs"

  cat > "$DIR/torrc" <<TORRC
# Client only, and now also a publisher: the hidden service is ours, pointed at a
# server that is also inside this jail. Nothing here relays for anyone else.
ClientOnly 1
SocksPort unix:/mnt/tor.sock
DataDirectory /mnt/data
Log notice file /mnt/tor.log
AvoidDiskWrites 1
HiddenServiceDir /mnt/hs
HiddenServicePort 80 127.0.0.1:$port
TORRC

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
  --ro-bind "$webroot" /mnt/www \\
  --dev /dev --proc /proc \\
  /bin/bash -c 'python3 -m http.server $port --bind 127.0.0.1 --directory /mnt/www >/mnt/www.log 2>&1 &
exec /mnt/root/usr/bin/tor -f /mnt/torrc'
RUNNER
  chmod +x "$DIR/run.sh"

  if [[ -n "${ONIONTRACE_HS_SESSION:-}" ]] && screen -ls 2>/dev/null | grep -q "$ONIONTRACE_HS_SESSION"; then
    screen -S "$ONIONTRACE_HS_SESSION" -X quit || true
    sleep 1
  fi
  rm -f "$SOCK" "$DIR/tor.log"

  local session="${ONIONTRACE_HS_SESSION:-onionhs}"
  screen -dmS "$session" "$DIR/run.sh"
  say "started publishing in screen session '$session'"

  # The hostname file appears immediately, because it is derived from keys written at start.
  # The service is not reachable until the descriptor is published, which cannot happen before
  # the client has a circuit. Returning the address before this point hands the caller an
  # address that does not work yet, so wait for the socket and the bootstrap first.
  for _ in $(seq 1 120); do
    if [[ -S "$SOCK" ]] && grep -q "Bootstrapped 100" "$DIR/tor.log" 2>/dev/null; then
      break
    fi
    sleep 1
  done
  if ! grep -q "Bootstrapped 100" "$DIR/tor.log" 2>/dev/null; then
    say "the publishing tor did not bootstrap within 120s; see $DIR/tor.log"
    exit 5
  fi

  for _ in $(seq 1 30); do
    if [[ -s "$DIR/hs/hostname" ]]; then
      say "address: $(cat "$DIR/hs/hostname")"
      cat "$DIR/hs/hostname"
      return 0
    fi
    sleep 1
  done
  say "no onion address after 30s; see $DIR/tor.log"
  exit 6
}

case "${1:-}" in
  start)  cmd_start ;;
  status) cmd_status ;;
  stop)   cmd_stop ;;
  clean)  cmd_clean ;;
  serve)  shift; cmd_serve "$@" ;;
  *)      sed -n '2,28p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
