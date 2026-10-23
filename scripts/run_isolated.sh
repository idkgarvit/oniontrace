#!/usr/bin/env bash
#
# Run a command inside the collection jail.
#
#   scripts/run_isolated.sh -- python -m oniontrace env
#   scripts/run_isolated.sh --tor-sock /run/oniontrace/tor.sock -- python -m oniontrace run ...
#
# The design decision worth stating: the jail gets **no network namespace at all**, not a
# firewall rule and not a proxy environment variable. Its only path to the outside is a
# unix socket bind-mounted from the host, where Tor listens. A unix socket is a filesystem
# object, so it crosses a network namespace boundary where an IP socket cannot. That turns
# "the collector is configured to use Tor" into "the collector has no other option": there
# is no interface to route through, so a bug, a dependency or a malicious payload inside
# the jail cannot reach the open internet even deliberately.
#
# The alternative -- isolate the process and let it keep the host's network -- is what most
# tooling does, and it is the reason isolated workstations still leak.
#
# Exit codes: 0 ran, 2 usage error, 3 bwrap missing, 4 no Tor socket.
set -euo pipefail

TOR_SOCK="${ONIONTRACE_TOR_SOCK:-/run/oniontrace/tor.sock}"
CASE_DIR=""
ALLOW_NO_TOR=0
SELF_TEST=0

usage() {
  sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tor-sock)   TOR_SOCK="${2:?--tor-sock needs a path}"; shift 2 ;;
    --case-dir)   CASE_DIR="${2:?--case-dir needs a path}"; shift 2 ;;
    --allow-no-tor) ALLOW_NO_TOR=1; shift ;;
    --self-test)  SELF_TEST=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    --)           shift; break ;;
    *)            break ;;
  esac
done

if ! command -v bwrap >/dev/null 2>&1; then
  echo "refusing to run: bwrap (bubblewrap) is not installed." >&2
  echo "  Without it there is no jail, and running the collector unsandboxed is the" >&2
  echo "  exact mistake this wrapper exists to prevent. Install bubblewrap first." >&2
  exit 3
fi

TOR_DIR="$(dirname "$TOR_SOCK")"
if [[ -S "$TOR_SOCK" ]]; then
  :
elif [[ "$ALLOW_NO_TOR" -eq 1 ]]; then
  echo "warning: no Tor socket at $TOR_SOCK; continuing because --allow-no-tor was given." >&2
  echo "         The preflight will fail, which is the intended outcome." >&2
else
  echo "refusing to run: no Tor socket at $TOR_SOCK" >&2
  echo "  Start Tor with:  SocksPort $TOR_SOCK" >&2
  echo "  Without it the jail has no route out, and a collector that cannot reach" >&2
  echo "  anything is indistinguishable from one that silently uses the host's network." >&2
  exit 4
fi

CASE_DIR="${CASE_DIR:-$PWD/casework}"
mkdir -p "$CASE_DIR"
CASE_DIR="$(cd "$CASE_DIR" && pwd)"

# --self-test runs the preflight inside the jail and exits with its verdict, so the
# isolation can be demonstrated rather than described.
if [[ "$SELF_TEST" -eq 1 ]]; then
  set -- python3 -m oniontrace env --case-root "$CASE_DIR" --tor-socks "unix:$TOR_SOCK"
fi

if [[ $# -eq 0 ]]; then
  echo "nothing to run. Pass a command after --, or use --self-test." >&2
  exit 2
fi

# --unshare-all: new user, mount, pid, net, ipc, uts and cgroup namespaces. The net one is
# the point. --ro-bind / / gives the jail a readable copy of the root, then the binds below
# punch narrow holes: the Tor socket's directory (writable, because connecting to a unix
# socket needs write permission on it) and the case directory.
#
# The Tor bind is conditional because bwrap refuses to bind a path that does not exist,
# and with --allow-no-tor there may be no socket directory yet. Binding it unconditionally
# made --allow-no-tor unusable, which the launcher test caught.
BWRAP_ARGS=(
  --unshare-all
  --die-with-parent
  --new-session
  --cap-drop ALL
  --ro-bind / /
  --dev /dev
  --proc /proc
  --tmpfs /tmp
  --dir /tmp/home
)
if [[ -d "$TOR_DIR" ]]; then
  BWRAP_ARGS+=(--bind "$TOR_DIR" "$TOR_DIR")
fi
BWRAP_ARGS+=(
  --bind "$CASE_DIR" "$CASE_DIR"
  --setenv HOME /tmp/home
  --setenv PATH /usr/local/bin:/usr/bin:/bin
  --setenv ONIONTRACE_TOR_SOCK "$TOR_SOCK"
  --unsetenv ONIONTRACE_ALLOW_UNSANDBOXED
  --chdir "$PWD"
  --
)
exec bwrap "${BWRAP_ARGS[@]}" "$@"
