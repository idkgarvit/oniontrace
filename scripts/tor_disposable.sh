#!/usr/bin/env bash
#
# A disposable, client-only Tor you can delete with one command.
#
#   scripts/tor_disposable.sh start     # unpack, configure, run in a screen session
#   scripts/tor_disposable.sh status    # is it up, and did it bootstrap
#   scripts/tor_disposable.sh stop      # stop the process
#   scripts/tor_disposable.sh clean     # stop and delete everything it wrote
#   scripts/tor_disposable.sh serve <dir> [port]        # publish one onion
#   scripts/tor_disposable.sh serve-lab <lab-root>      # publish every lab service
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
#: ``notice`` by default. ``info`` is for diagnosing a rendezvous that fails while the
#: service is demonstrably up: it is the only level that names the introduction point.
LOG_LEVEL="${ONIONTRACE_TOR_LOG:-notice}"
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

  cat > "$DIR/torrc" <<TORRC
# Client only: this instance never relays for anyone else.
ClientOnly 1
SocksPort unix:/mnt/tor.sock
DataDirectory /mnt/data
Log ${LOG_LEVEL} file /mnt/tor.log
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
  for _ in $(seq 1 300); do
    if grep -q "Bootstrapped 100" "$DIR/tor.log" 2>/dev/null && [[ -S "$SOCK" ]]; then
      say "ready: $SOCK"
      return 0
    fi
    sleep 1
  done
  say "tor did not bootstrap within 300s; see $DIR/tor.log"
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

# A second tor on the same data directory unlinks the first one's live socket and then
# exits with a lock error. Every connection through the bridge then goes nowhere and looks
# like a network failure minutes later, so publishing always stops first and verifies that
# the process is actually gone.
stop_for_publish() {
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

  stop_for_publish

  mkdir -p "$DIR/hs"
  chmod 700 "$DIR/hs"

  cat > "$DIR/torrc" <<TORRC
# Client only, and now also a publisher: the hidden service is ours, pointed at a
# server that is also inside this jail. Nothing here relays for anyone else.
ClientOnly 1
SocksPort unix:/mnt/tor.sock
DataDirectory /mnt/data
Log ${LOG_LEVEL} file /mnt/tor.log
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
  for _ in $(seq 1 300); do
    if [[ -S "$SOCK" ]] && grep -q "Bootstrapped 100" "$DIR/tor.log" 2>/dev/null; then
      break
    fi
    sleep 1
  done
  if ! grep -q "Bootstrapped 100" "$DIR/tor.log" 2>/dev/null; then
    say "the publishing tor did not bootstrap within 300s; see $DIR/tor.log"
    tail -n 30 "$DIR/tor.log" >&2 2>/dev/null || true
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

# --------------------------------------------------------------------------- #
# serve-lab: publish every service under a lab root from one Tor, one local server
# per service.
#
# One Tor with eight HiddenServiceDir blocks is not eight times the work of one: the
# client circuit, the bootstrap and the data directory are shared, and only the key
# ring entries are new. Starting eight Tor processes would fight over the data
# directory and the socket, which is exactly the failure the single-service path
# already learned to avoid.
#
# The output on stdout is deliberately machine-readable -- "service-id<TAB>address",
# one line per service -- because the caller has to join those addresses to the lab's
# ground truth. Human progress goes to stderr.
# --------------------------------------------------------------------------- #
cmd_serve_lab() {
  local lab_root="${1:-}"
  local addresses_out="${2:-}"
  need_tools
  unpack
  if [[ -z "$lab_root" || ! -d "$lab_root/services" ]]; then
    say "usage: $0 serve-lab <lab-root> [addresses-out]"
    exit 2
  fi
  lab_root="$(cd "$lab_root" && pwd)"
  local script_root lab_py
  script_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  lab_py="$script_root/eval/live_lab.py"
  [[ -f "$lab_py" ]] || { say "missing lab server: $lab_py"; exit 2; }

  local -a ids=() ports=()
  local port_base="${ONIONTRACE_LAB_PORT_BASE:-18080}"
  local service_dir service_id
  for service_dir in "$lab_root"/services/*/; do
    [[ -f "$service_dir/routes.json" ]] || continue
    service_id="$(basename "$service_dir")"
    # The id becomes a directory name and part of the torrc. A slug is the whole
    # validation: anything with a slash, a newline or a space could point a hidden
    # service at a path this script did not choose.
    if [[ ! "$service_id" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
      say "refusing service id '$service_id': expected a simple slug"
      exit 2
    fi
    ids+=("$service_id")
    ports+=("$((port_base + ${#ids[@]} - 1))")
  done
  if [[ ${#ids[@]} -eq 0 ]]; then
    say "no services with routes.json under $lab_root/services"
    exit 2
  fi

  # Stop the previous publisher before checking ports: its servers still hold them until
  # its PID namespace is gone, and checking first made a rerun fail on its own leftovers.
  stop_for_publish
  local session="${ONIONTRACE_HS_SESSION:-onionhs}"
  if command -v screen >/dev/null 2>&1 && screen -ls 2>/dev/null | grep -q "$session"; then
    screen -S "$session" -X quit || true
    sleep 1
  fi

  # A port already in use would start a server that dies while Tor still publishes a
  # descriptor pointing at it. The service would then be "up" and unreachable, which is
  # the least useful failure mode.
  local port attempt busy
  for attempt in $(seq 1 20); do
    busy=0
    for port in "${ports[@]}"; do
      if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
        busy=1
      fi
    done
    (( busy == 0 )) && break
    sleep 0.5
  done
  if (( busy != 0 )); then
    say "a lab port is still in use after the previous publisher stopped; set ONIONTRACE_LAB_PORT_BASE"
    exit 8
  fi

  mkdir -p "$DIR/hs" "$DIR/logs"
  chmod 700 "$DIR/hs"
  rm -f "$DIR"/hs/*/hostname 2>/dev/null || true

  {
    cat <<TORRC
# Client only, and a publisher of our own services: every hidden service here points
# at a server inside this jail. Nothing relays for anyone else.
ClientOnly 1
SocksPort unix:/mnt/tor.sock
DataDirectory /mnt/data
Log ${LOG_LEVEL} file /mnt/tor.log
AvoidDiskWrites 1
TORRC
    local idx
    for idx in "${!ids[@]}"; do
      mkdir -p "$DIR/hs/${ids[$idx]}"
      chmod 700 "$DIR/hs/${ids[$idx]}"
      printf 'HiddenServiceDir /mnt/hs/%s\nHiddenServicePort 80 127.0.0.1:%s\n' \
        "${ids[$idx]}" "${ports[$idx]}"
    done
  } > "$DIR/torrc"

  # One static server per service, each reading its own routes.json. They are started
  # before tor and share its PID namespace, so tor exiting takes them with it.
  {
    echo '#!/usr/bin/env bash'
    echo 'set -euo pipefail'
    local idx
    for idx in "${!ids[@]}"; do
      printf 'python3 /mnt/live_lab.py serve /mnt/lab/services/%s %s >/mnt/logs/%s.log 2>&1 &\n' \
        "${ids[$idx]}" "${ports[$idx]}" "${ids[$idx]}"
    done
    echo 'exec /mnt/root/usr/bin/tor -f /mnt/torrc'
  } > "$DIR/servers.sh"
  chmod +x "$DIR/servers.sh"

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
  --ro-bind "$lab_root" /mnt/lab \\
  --ro-bind "$lab_py" /mnt/live_lab.py \\
  --dev /dev --proc /proc \\
  /mnt/servers.sh
RUNNER
  chmod +x "$DIR/run.sh"

  rm -f "$SOCK" "$DIR/tor.log"
  screen -dmS "$session" "$DIR/run.sh"
  say "started publishing ${#ids[@]} service(s) in screen session '$session'"

  for _ in $(seq 1 300); do
    if [[ -S "$SOCK" ]] && grep -q "Bootstrapped 100" "$DIR/tor.log" 2>/dev/null; then
      break
    fi
    sleep 1
  done
  if ! grep -q "Bootstrapped 100" "$DIR/tor.log" 2>/dev/null; then
    say "the publishing tor did not bootstrap within 300s; see $DIR/tor.log"
    tail -n 30 "$DIR/tor.log" >&2 2>/dev/null || true
    exit 5
  fi

  # Hostname files are written from keys at startup; they say the service exists, not
  # that the descriptor has reached the hash ring. The caller waits for reachability;
  # this loop only refuses to return a partial mapping.
  local waited=0 missing idx
  while :; do
    missing=0
    for idx in "${!ids[@]}"; do
      [[ -s "$DIR/hs/${ids[$idx]}/hostname" ]] || missing=$((missing + 1))
    done
    (( missing == 0 )) && break
    if (( waited >= 60 )); then
      say "$missing of ${#ids[@]} hostname file(s) missing after 60s; see $DIR/tor.log"
      exit 6
    fi
    sleep 1
    waited=$((waited + 1))
  done

  addresses_out="${addresses_out:-$DIR/lab-addresses.tsv}"
  : > "$addresses_out"
  for idx in "${!ids[@]}"; do
    printf '%s\t%s\n' "${ids[$idx]}" "$(cat "$DIR/hs/${ids[$idx]}/hostname")" \
      >> "$addresses_out"
  done
  say "addresses: $addresses_out (descriptors may take minutes to become findable)"
  cat "$addresses_out"
}

case "${1:-}" in
  start)  cmd_start ;;
  status) cmd_status ;;
  stop)   cmd_stop ;;
  clean)  cmd_clean ;;
  serve)  shift; cmd_serve "$@" ;;
  serve-lab) shift; cmd_serve_lab "$@" ;;
  *)      sed -n '2,29p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
