"""Probes for the execution environment, and the preflight that runs them.

This module sits outside ``gates/`` on purpose. The gates package is required to have no
network or subprocess capability at all -- an invariant another test enforces -- because a
gate that can reach the network is a gate that can leak. An environment check, though, has
to touch sockets to answer its question honestly: "is there a route out of this process?"
cannot be answered by reading a configuration file.

So the split is: ``gates.environment`` holds the verdict -- what a check is, what a report
is, and the exception raised when one fails -- and this module holds the observations that
produce it. The gate decides; this code looks.

The probes, and why each one asks the question it asks:

* **interfaces** rather than the namespace inode, because inside a PID namespace
  ``/proc/1`` is the jail's own init and the inode comparison silently passes for a process
  that shares the host's network. A fresh namespace has loopback and nothing else.
* **a direct connection attempt** rather than a firewall listing, because the listing can be
  right about the rules and still be the wrong machine. Success is the failure.
* **a SOCKS5 greeting** rather than a configured port number, because a port being open says
  nothing about what is listening on it, and an HTTP proxy on the SOCKS port would collect
  clearnet traffic while reporting itself as Tor.
* **a write** rather than a permission bit, because a run that cannot record itself is not
  evidence, and the filesystem is the only authority on that.
"""

from __future__ import annotations

import os
import socket
import struct
from typing import Callable, Iterable

from gates.environment import (
    DEFAULT_LIMITATIONS,
    DEFAULT_TOR_ENDPOINT,
    Check,
    EnvironmentReport,
)

#: A routable address used only to answer "do I have a route of my own?". Nothing is sent
#: beyond a SYN: the check fails on connect success, which is the outcome we do not want.
EGRESS_PROBE_ADDRESS = ("1.1.1.1", 443)

#: Where the deep check asks Tor to connect. A clearnet host belonging to the Tor Project,
#: chosen because the request is a CONNECT and nothing else -- no page is fetched, and the
#: service exists to be reached by people checking their Tor setup.
CIRCUIT_CHECK_HOST = ("check.torproject.org", 443)

#: Long enough for a local socket, short enough that a blackholed route does not stall a
#: preflight check. A jail that drops packets rather than refusing them is the slow case.
PROBE_TIMEOUT = 1.0

#: A circuit needs longer than a local socket: Tor has to pick and build a path.
CIRCUIT_TIMEOUT = 30.0

#: SOCKS5 greeting: version 5, one method offered, "no authentication required".
_SOCKS_GREETING = b"\x05\x01\x00"


def _namespace_inode(path: str) -> str | None:
    try:
        return os.readlink(path)
    except OSError:
        return None


def probe_network_namespace() -> Check:
    """Ask the kernel which interfaces this process can see.

    The obvious probe -- compare ``/proc/self/ns/net`` with ``/proc/1/ns/net`` -- is
    wrong inside a PID namespace, which is exactly where an isolated collector runs:
    ``/proc/1`` becomes the jail's own init, so the two inodes match and a correctly
    jailed process would be reported as sharing the host's network. Enumerating
    interfaces does not have that failure mode. A fresh network namespace has loopback
    and nothing else, whatever namespaces surround it.
    """

    mine = _namespace_inode("/proc/self/ns/net")
    init = _namespace_inode("/proc/1/ns/net")
    supporting = ""
    if mine and init and mine != init:
        supporting = f"; netns inode also differs from PID 1 ({mine} vs {init})"
    elif mine and init:
        supporting = f"; netns inode {mine} matches PID 1, which a PID namespace also produces"

    try:
        interfaces = sorted(name for index, name in socket.if_nameindex() if name != "lo")
    except OSError:
        interfaces = []
        supporting += "; interface list unavailable"

    if interfaces:
        return Check(
            "own network namespace",
            False,
            f"non-loopback interfaces present: {', '.join(interfaces)}",
            "run under scripts/run_isolated.sh, which uses bwrap --unshare-all",
        )
    return Check(
        "own network namespace",
        True,
        f"loopback only, no routable interface{supporting}",
    )


def probe_direct_egress() -> Check:
    """Try to reach a routable address directly. Success is the failure."""

    host, port = EGRESS_PROBE_ADDRESS
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(PROBE_TIMEOUT)
    try:
        sock.connect((host, port))
    except OSError as exc:
        return Check(
            "direct egress blocked",
            True,
            f"{host}:{port} unreachable from here ({exc.__class__.__name__})",
        )
    finally:
        sock.close()
    return Check(
        "direct egress blocked",
        False,
        f"connected to {host}:{port} without Tor -- this process has its own route out",
        "run under scripts/run_isolated.sh; a network namespace is what makes this fail",
    )


def _connect_endpoint(endpoint: str) -> socket.socket:
    if endpoint.startswith("unix:"):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(PROBE_TIMEOUT)
        sock.connect(endpoint[len("unix:") :])
        return sock
    host, _, port = endpoint.rpartition(":")
    return socket.create_connection((host or "127.0.0.1", int(port)), timeout=PROBE_TIMEOUT)


def probe_tor(endpoint: str) -> Check:
    """Send a SOCKS5 greeting and read the method-selection reply."""

    try:
        sock = _connect_endpoint(endpoint)
    except OSError as exc:
        return Check(
            "tor reachable",
            False,
            f"no listener at {endpoint} ({exc.__class__.__name__})",
            "start tor with 'SocksPort " + endpoint + "' and bind the socket into the jail",
        )
    try:
        sock.sendall(_SOCKS_GREETING)
        reply = sock.recv(2)
    except OSError as exc:
        return Check(
            "tor reachable",
            False,
            f"greeting to {endpoint} failed ({exc.__class__.__name__})",
            "check that the endpoint is a SOCKS5 listener and not an HTTP proxy",
        )
    finally:
        sock.close()

    if reply[:2] != b"\x05\x00":
        return Check(
            "tor reachable",
            False,
            f"{endpoint} answered {reply.hex() or 'nothing'}, not a SOCKS5 no-auth handshake",
            "point the collector at Tor's SocksPort; Privoxy and HTTP proxies are not Tor",
        )
    return Check("tor reachable", True, f"SOCKS5 no-auth handshake completed at {endpoint}")


def probe_case_writable(case_root: str | os.PathLike[str] | None) -> Check:
    """The run has to be able to write down what it did."""

    if case_root is None:
        return Check("case directory writable", True, "no case root required for this run")
    path = os.fspath(case_root)
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".oniontrace-write-probe")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.unlink(probe)
    except (OSError, ValueError) as exc:
        # ValueError as well as OSError: a path carrying a null byte raises ValueError,
        # and a preflight that crashes on a bad argument is a preflight nobody runs.
        return Check(
            "case directory writable",
            False,
            f"{path} is not writable ({exc.__class__.__name__})",
            "bind the case directory into the jail; a run that cannot record itself is "
            "not evidence",
        )
    return Check("case directory writable", True, f"{path} accepts writes")


def _recv_exactly(sock: socket.socket, count: int) -> bytes:
    buf = b""
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        if not chunk:
            raise OSError("connection closed mid-reply")
        buf += chunk
    return buf


def probe_circuit(endpoint: str) -> Check:
    """Ask Tor to actually connect somewhere, which a greeting never does.

    This is the deep check, off by default for two reasons: it needs a working Tor network,
    and it sends one CONNECT to a third party. Both are reasonable things to refuse on a
    preflight that runs before every collection, so it is opt-in.

    A successful CONNECT proves three things a greeting cannot: the exit can reach the
    network, a circuit was built, and the proxy is speaking SOCKS5 rather than being a
    listener that happens to answer two bytes.
    """

    host, port = CIRCUIT_CHECK_HOST
    try:
        sock = _connect_endpoint(endpoint)
        sock.settimeout(CIRCUIT_TIMEOUT)
    except OSError as exc:
        return Check(
            "tor circuit",
            False,
            f"no listener at {endpoint} ({exc.__class__.__name__})",
            "start tor and bind its SocksPort into the jail",
        )

    try:
        sock.sendall(_SOCKS_GREETING)
        if _recv_exactly(sock, 2) != b"\x05\x00":
            return Check("tor circuit", False, "greeting refused", "check the SocksPort")

        sock.sendall(
            b"\x05\x01\x00\x03"
            + bytes([len(host)])
            + host.encode("idna")
            + struct.pack(">H", port)
        )
        head = _recv_exactly(sock, 4)
        if head[0] != 5 or head[1] != 0:
            return Check(
                "tor circuit",
                False,
                f"Tor refused CONNECT to {host}:{port} (SOCKS5 reply {head[1]:#04x})",
                "Tor is answering but cannot build a circuit; check the network and the log",
            )

        # Consume the bind address by type. Reading a fixed length here is what leaves
        # stray bytes in a stream and breaks whatever protocol speaks next.
        atyp = head[3]
        if atyp == 1:
            _recv_exactly(sock, 6)
        elif atyp == 4:
            _recv_exactly(sock, 18)
        elif atyp == 3:
            _recv_exactly(sock, _recv_exactly(sock, 1)[0] + 2)
        else:
            return Check("tor circuit", False, f"unexpected bind address type {atyp}")
    except OSError as exc:
        return Check(
            "tor circuit",
            False,
            f"circuit to {host}:{port} failed ({exc.__class__.__name__})",
            "Tor may still be bootstrapping; check its log for 'Bootstrapped 100%'",
        )
    finally:
        sock.close()

    return Check("tor circuit", True, f"CONNECT to {host}:{port} succeeded through Tor")


def check_environment(
    *,
    tor_endpoint: str = DEFAULT_TOR_ENDPOINT,
    case_root: str | os.PathLike[str] | None = None,
    deep: bool = False,
    probes: Iterable[Callable[[], Check]] | None = None,
) -> EnvironmentReport:
    """Run the preflight and return every answer, passing or not.

    ``probes`` replaces the whole set, which is what the tests use to produce each failure
    without needing a broken machine to produce it. ``deep`` adds the circuit check, and
    drops the limitation it retires.
    """

    if probes is None:
        chosen: list[Callable[[], Check]] = [
            probe_network_namespace,
            probe_direct_egress,
            lambda: probe_tor(tor_endpoint),
            lambda: probe_case_writable(case_root),
        ]
        if deep:
            chosen.append(lambda: probe_circuit(tor_endpoint))
    else:
        chosen = list(probes)

    limitations = DEFAULT_LIMITATIONS
    if deep:
        limitations = tuple(
            line for line in DEFAULT_LIMITATIONS if "circuit" not in line
        )

    return EnvironmentReport(
        checks=tuple(probe() for probe in chosen),
        tor_endpoint=tor_endpoint,
        limitations=limitations,
    )
