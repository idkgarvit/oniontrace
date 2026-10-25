"""Tests for the execution environment gate.

Two kinds of test live here, and the split is deliberate.

The first kind substitutes probes. Every refusal path is produced by a probe that lies,
so "the gate refuses a process with a route out" can be tested on a machine that has one,
and "the gate refuses when Tor is missing" can be tested where Tor is installed.

The second kind does not substitute anything: it starts a real bubblewrap jail and asks
the kernel whether the process inside can reach the network. That test is the one that
matters, because everything else here tests a function's opinion, and this tests the
isolation itself. It is skipped rather than failed where bubblewrap is absent, since a
missing tool is not a passing gate -- the preflight reports it as a failure, and the
launcher refuses to run without it.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from collect import preflight
from collect.preflight import (
    check_environment,
    probe_circuit,
    probe_case_writable,
    probe_direct_egress,
    probe_network_namespace,
    probe_tor,
)
from gates.environment import Check, EnvironmentRefused, require_environment

OK = Check("stub", True, "fine")
BAD = Check("stub", False, "broken", "fix it")


def _probes(*checks):
    """Build a probe set that returns the given checks, in order."""

    return tuple((lambda c=c: c) for c in checks)


# -- the report --------------------------------------------------------- #


def test_a_clean_environment_passes_and_still_states_what_it_cannot_prove():
    report = check_environment(probes=_probes(OK, OK))

    assert report.ok
    assert report.failures == ()
    # A passing report that reads as a guarantee is the failure mode this guards against.
    assert report.limitations
    assert any("circuit" in line for line in report.limitations)
    assert "not established by this report" in report.summary()


def test_every_failure_is_reported_at_once_with_a_remedy():
    report = check_environment(
        probes=_probes(OK, Check("direct egress blocked", False, "route out", "use the jail"))
    )

    assert not report.ok
    assert [c.name for c in report.failures] == ["direct egress blocked"]
    assert "use the jail" in report.summary()


def test_require_environment_names_every_failure_in_one_refusal():
    report = check_environment(
        probes=_probes(
            Check("own network namespace", False, "shares the host namespace", "jail it"),
            Check("tor reachable", False, "no listener", "start tor"),
        )
    )

    with pytest.raises(EnvironmentRefused) as caught:
        require_environment(report)

    message = str(caught.value)
    assert "shares the host namespace" in message and "no listener" in message
    # One fix per run is how a preflight becomes something operators route around.
    assert caught.value.failures == report.failures


def test_the_refusal_carries_the_rule_that_fired():
    report = check_environment(
        probes=_probes(Check("direct egress blocked", False, "route out", ""))
    )
    with pytest.raises(EnvironmentRefused) as caught:
        require_environment(report)
    assert caught.value.rule == "E2.7"


def test_require_environment_returns_the_report_when_it_passes():
    report = check_environment(probes=_probes(OK))
    assert require_environment(report) is report


# -- the namespace probe ------------------------------------------------ #
# The inode comparison that looks like the obvious probe is wrong inside a PID namespace,
# so these pin the interface enumeration that replaced it.


def test_a_process_with_a_routable_interface_is_not_isolated(monkeypatch):
    monkeypatch.setattr(socket, "if_nameindex", lambda: [(1, "lo"), (2, "eth0")])

    check = probe_network_namespace()

    assert not check.ok
    assert "eth0" in check.detail
    assert "run_isolated.sh" in check.remedy


def test_loopback_only_counts_as_isolated(monkeypatch):
    monkeypatch.setattr(socket, "if_nameindex", lambda: [(1, "lo")])

    check = probe_network_namespace()

    assert check.ok
    assert "loopback only" in check.detail


def test_an_unreadable_interface_list_does_not_crash_the_preflight(monkeypatch):
    def explode():
        raise OSError("no netlink")

    monkeypatch.setattr(socket, "if_nameindex", explode)

    check = probe_network_namespace()

    # No interfaces visible means no route, and the check says the list was unavailable
    # rather than silently presenting that as proof the kernel has none.
    assert check.ok
    assert "unavailable" in check.detail


# -- the egress probe --------------------------------------------------- #


def test_a_successful_direct_connection_is_reported_as_a_leak(monkeypatch):
    class ConnectingSocket:
        def __init__(self, *args, **kwargs):
            pass

        def settimeout(self, timeout):
            pass

        def connect(self, address):
            return None  # a route exists -- the outcome the check treats as failure

        def close(self):
            pass

    monkeypatch.setattr(preflight.socket, "socket", ConnectingSocket)

    check = probe_direct_egress()

    assert not check.ok
    assert "without Tor" in check.detail


def test_a_refused_connection_is_reported_as_blocked(monkeypatch):
    class RefusingSocket:
        def __init__(self, *args, **kwargs):
            pass

        def settimeout(self, timeout):
            pass

        def connect(self, address):
            raise OSError("network is unreachable")

        def close(self):
            pass

    monkeypatch.setattr(preflight.socket, "socket", RefusingSocket)

    check = probe_direct_egress()

    assert check.ok
    assert "unreachable" in check.detail


# -- the Tor probe ------------------------------------------------------ #


def _listener(path: Path, reply: bytes) -> tuple[threading.Thread, Path]:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(4)

    def serve():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            try:
                conn.recv(3)
                conn.sendall(reply)
            finally:
                conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return thread, path


@pytest.fixture
def short_socket_dir():
    """A socket directory short enough for AF_UNIX.

    A unix socket path is capped near 108 bytes, and pytest's tmp_path under a deep home
    directory exceeds it. The fixture is here rather than in each test so the reason is
    written down once.
    """

    import shutil
    import tempfile

    path = Path(tempfile.mkdtemp(prefix="ot-", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_a_socks5_listener_satisfies_the_tor_check(short_socket_dir):
    _listener(short_socket_dir / "tor.sock", b"\x05\x00")

    check = probe_tor(f"unix:{short_socket_dir / 'tor.sock'}")

    assert check.ok
    assert "SOCKS5" in check.detail


def test_a_listener_that_speaks_something_else_is_refused(short_socket_dir):
    """An HTTP proxy on the SOCKS port would collect clearnet traffic and call it Tor."""

    _listener(short_socket_dir / "tor.sock", b"HTTP/1.1 400")

    check = probe_tor(f"unix:{short_socket_dir / 'tor.sock'}")

    assert not check.ok
    assert "not a SOCKS5" in check.detail


def test_a_missing_socket_names_the_path_it_wanted(tmp_path):
    check = probe_tor(f"unix:{tmp_path / 'absent.sock'}")

    assert not check.ok
    assert "absent.sock" in check.detail
    assert "SocksPort" in check.remedy


# -- the circuit probe (--deep) ----------------------------------------- #


def _socks_server(path: Path, *, reply_code: int, bind_atyp: int, truncate: bool = False):
    """A SOCKS5 server that answers the greeting and one CONNECT.

    ``bind_atyp`` selects the reply's address type, because the bind address length varies
    and a client that reads a fixed number of bytes desynchronises the stream -- the bug
    this probe had before the reply was parsed by type.
    """

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(4)

    if bind_atyp == 1:
        bind = b"\x7f\x00\x00\x01" + b"\x00\x50"
    elif bind_atyp == 4:
        bind = b"\x00" * 16 + b"\x00\x50"
    else:
        bind = bytes([9]) + b"localhost" + b"\x00\x50"

    def serve():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            try:
                conn.recv(3)
                conn.sendall(b"\x05\x00")
                conn.recv(64)  # the CONNECT request
                if truncate:
                    conn.sendall(b"\x05\x00\x00")
                    continue
                conn.sendall(b"\x05" + bytes([reply_code]) + b"\x00" + bytes([bind_atyp]) + bind)
            except OSError:
                pass
            finally:
                conn.close()

    threading.Thread(target=serve, daemon=True).start()


def test_a_built_circuit_is_reported_as_one(short_socket_dir):
    path = short_socket_dir / "tor.sock"
    _socks_server(path, reply_code=0, bind_atyp=1)

    check = probe_circuit(f"unix:{path}")

    assert check.ok
    assert "succeeded through Tor" in check.detail


def test_an_ipv6_bind_address_in_the_reply_does_not_desynchronise_the_stream(short_socket_dir):
    """The reply's length depends on its address type; a fixed read leaves bytes behind."""

    path = short_socket_dir / "tor.sock"
    _socks_server(path, reply_code=0, bind_atyp=4)

    check = probe_circuit(f"unix:{path}")

    assert check.ok, check.detail


def test_tor_refusing_the_connect_is_reported_with_its_reply_code(short_socket_dir):
    path = short_socket_dir / "tor.sock"
    _socks_server(path, reply_code=4, bind_atyp=1)  # 0x04 = host unreachable

    check = probe_circuit(f"unix:{path}")

    assert not check.ok
    assert "0x04" in check.detail


def test_a_truncated_reply_does_not_pass(short_socket_dir):
    path = short_socket_dir / "tor.sock"
    _socks_server(path, reply_code=0, bind_atyp=1, truncate=True)

    check = probe_circuit(f"unix:{path}")

    assert not check.ok


def test_deep_adds_the_circuit_check_and_retires_its_limitation(monkeypatch):
    called = []

    def fake_circuit(endpoint):
        called.append(endpoint)
        return Check("tor circuit", True, "connected")

    monkeypatch.setattr(preflight, "probe_circuit", fake_circuit)

    shallow = check_environment()
    deep = check_environment(deep=True)

    assert called, "deep did not run the circuit probe"
    assert any("circuit" in line for line in shallow.limitations)
    assert not any("circuit" in line for line in deep.limitations)
    assert any(c.name == "tor circuit" for c in deep.checks)


# -- the writability probe ---------------------------------------------- #


def test_the_case_directory_probe_writes_and_cleans_up(tmp_path):
    check = probe_case_writable(tmp_path / "casework")

    assert check.ok
    assert list((tmp_path / "casework").iterdir()) == []


def test_an_unwritable_case_directory_is_refused(tmp_path):
    check = probe_case_writable(tmp_path / "missing" / "\0bad")

    assert not check.ok
    assert check.remedy


# -- the gated transport ------------------------------------------------ #


def _failing_inner():
    from collect.errors import LiveCollectionRefused

    class Inner:
        def __init__(self):
            self.checked = 0

        def check(self, url, headers=None):
            self.checked += 1
            raise LiveCollectionRefused("SOCKS proxy required for .onion")

        def fetch(self, url, headers=None):  # pragma: no cover - never reached
            raise AssertionError("fetch must not run")

    return Inner()


def test_the_transports_own_refusal_wins_over_the_environment_check():
    """A run with no authorisation should not be told its problem is the jail."""

    from collect.cli import _EnvironmentGatedTransport
    from collect.errors import LiveCollectionRefused

    called = []

    def factory():  # pragma: no cover - asserted not to run
        called.append(True)
        return check_environment(probes=_probes(OK))

    gated = _EnvironmentGatedTransport(_failing_inner(), report_factory=factory)

    with pytest.raises(LiveCollectionRefused, match="SOCKS"):
        gated.check("http://example.onion/")

    assert called == [], "the environment was checked before the URL was allowed"


def test_a_bad_environment_stops_the_fetch():
    from collect.cli import _EnvironmentGatedTransport
    from collect.errors import LiveCollectionRefused

    class Permissive:
        def check(self, url, headers=None):
            return None

        def fetch(self, url, headers=None):  # pragma: no cover - never reached
            raise AssertionError("nothing may leave the process")

    gated = _EnvironmentGatedTransport(
        Permissive(),
        report_factory=lambda: check_environment(
            probes=_probes(Check("direct egress blocked", False, "has a route out", ""))
        ),
    )

    with pytest.raises(LiveCollectionRefused, match="environment is not safe"):
        gated.fetch("http://example.onion/")


def test_the_environment_wrapper_keeps_the_transports_timeout_visible():
    """The wrapper is transparent apart from the preflight, timeout included.

    The collector's permit reserve is the transport's own timeout, because a request that
    starts inside the permit can finish outside it. When this wrapper hid the setting, the
    reserve fell back to a 30-second default while a live request was allowed ten minutes,
    so the guard fired after the deadline it was meant to protect -- or never at all.
    """
    from collect.cli import _EnvironmentGatedTransport
    from collect.live import LiveSettings, LiveTransport

    inner = LiveTransport(settings=LiveSettings(timeout_seconds=600))
    gated = _EnvironmentGatedTransport(inner, report_factory=lambda: None)

    assert gated.settings is inner.settings
    assert gated.settings.timeout_seconds == 600


def test_a_good_environment_is_checked_once_for_the_whole_run():
    from collect.cli import _EnvironmentGatedTransport

    class Permissive:
        def check(self, url, headers=None):
            return None

        def fetch(self, url, headers=None):
            return "page"

    calls = []

    def factory():
        calls.append(True)
        return check_environment(probes=_probes(OK))

    gated = _EnvironmentGatedTransport(Permissive(), report_factory=factory)

    for _ in range(5):
        assert gated.fetch("http://example.onion/") == "page"

    assert len(calls) == 1


# -- the real thing ----------------------------------------------------- #


def _bwrap() -> str | None:
    import shutil

    return shutil.which("bwrap")


PROBE = """
import socket, sys
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(1.0)
try:
    sock.connect(("1.1.1.1", 443))
except OSError:
    print("BLOCKED")
else:
    print("CONNECTED")
finally:
    sock.close()
print("IFACES", sorted(n for _, n in socket.if_nameindex()))
"""


@pytest.mark.skipif(_bwrap() is None, reason="bubblewrap is not installed")
def test_a_bubblewrap_jail_actually_has_no_route_out():
    """The claim the launcher makes, tested against the kernel rather than the script.

    Everything above this test verifies that the checker reports what it is told. This one
    starts a namespace and asks whether a socket can reach the internet from inside it, so
    it would fail if `--unshare-all` were dropped from the launcher's bwrap invocation.
    """

    result = subprocess.run(
        [
            _bwrap(),
            "--unshare-all",
            "--die-with-parent",
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--tmpfs",
            "/tmp",
            sys.executable,
            "-c",
            PROBE,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "BLOCKED" in result.stdout, "a socket inside the jail reached the network"
    assert "IFACES ['lo']" in result.stdout, "the jail kept an interface it should not have"


@pytest.mark.skipif(_bwrap() is None, reason="bubblewrap is not installed")
def test_the_shipped_launcher_is_the_thing_that_isolates():
    """The test above proves a jail works. This one proves *this* launcher makes one.

    Without it, deleting `--unshare-all` from scripts/run_isolated.sh would leave every
    test green while the shipped wrapper stopped isolating anything -- the exact class of
    drift the wrapper exists to prevent.
    """

    script = Path(__file__).resolve().parent.parent / "scripts" / "run_isolated.sh"
    assert script.exists()

    result = subprocess.run(
        [str(script), "--allow-no-tor", "--", sys.executable, "-c", PROBE],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(script.parent.parent),
    )

    assert result.returncode == 0, result.stderr
    assert "BLOCKED" in result.stdout, "the shipped launcher let a socket out"
    assert "IFACES ['lo']" in result.stdout
