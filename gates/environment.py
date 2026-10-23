"""The execution environment verdict (workflow phase 1).

Every published investigation workflow opens with the same three sentences: work in an
isolated environment, send traffic through Tor, and log what you do. It is the phase most
often asserted rather than checked, and the assertion is the part that fails. A launcher
script that *says* ``--unshare-net`` isolates nothing if the operator runs the collector
without it, and a collector that reaches the open internet from a workstation has already
made the mistake the isolation existed to prevent.

This module holds the *verdict*: what a check is, what a report is, and the exception raised
when one fails. The observations that produce the verdict -- socket probes, interface
enumeration, a write -- live in :mod:`collect.preflight`, because the gates package carries
an invariant that it can reach neither the network nor a subprocess, and an environment
check that cannot look at a socket cannot answer its own question.

The split is deliberate and it is the same discipline the rest of the package follows: the
gate decides, the caller observes, and neither one is trusted with the other's job.

The report states what it does not establish, rather than implying completeness. A SOCKS
greeting is not a circuit, a namespace is not a clean host, and a preflight is not a
guarantee about what the process does after it starts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from gates.errors import GateRefused

#: Rule numbers for the refusals this module raises. The gates elsewhere in the package
#: are numbered E2.x; the environment gate continues that series rather than inventing a
#: second scheme.
NAMESPACE_RULE = "E2.6"
EGRESS_RULE = "E2.7"
TOR_RULE = "E2.8"

#: Where Tor listens. A unix socket is preferred because a jail with no network namespace
#: still reaches a unix socket through the filesystem, so the only route out of the jail
#: is Tor itself rather than the whole IP stack.
DEFAULT_TOR_ENDPOINT = "unix:/run/oniontrace/tor.sock"

DEFAULT_LIMITATIONS = (
    "a SOCKS5 greeting proves a listener, not that a circuit will build",
    "a private network namespace says nothing about the cleanliness of the host outside it",
    "this checks the environment the collector starts in, not everything it does afterwards",
)


@dataclass(frozen=True)
class Check:
    """One question, its answer, and what to do about a bad answer."""

    name: str
    ok: bool
    detail: str
    remedy: str = ""

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail, "remedy": self.remedy}


class EnvironmentRefused(GateRefused):
    """The process is not safe to collect from.

    Raised instead of returning False, for the same reason the collection gate raises:
    a check that can be ignored by branching on its return value is decoration.
    """

    def __init__(self, failures: Sequence[Check]) -> None:
        self.failures = tuple(failures)
        lines = "; ".join(f"{c.name}: {c.detail}" for c in self.failures)
        rule = NAMESPACE_RULE
        if any(c.name == "direct egress blocked" for c in self.failures):
            rule = EGRESS_RULE
        elif any(c.name == "tor reachable" for c in self.failures):
            rule = TOR_RULE
        super().__init__(rule, f"environment is not safe for collection -- {lines}")


@dataclass(frozen=True)
class EnvironmentReport:
    """The whole answer, including the parts that passed and the parts not proven."""

    checks: tuple[Check, ...]
    tor_endpoint: str
    limitations: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def failures(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if not c.ok)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "tor_endpoint": self.tor_endpoint,
            "checks": [c.to_dict() for c in self.checks],
            "limitations": list(self.limitations),
        }

    def summary(self) -> str:
        lines = [
            f"{'ok  ' if c.ok else 'FAIL'}  {c.name}: {c.detail}"
            + (f"  -> {c.remedy}" if not c.ok and c.remedy else "")
            for c in self.checks
        ]
        if self.ok:
            lines.append("")
            lines.append("not established by this report:")
            lines.extend(f"  - {text}" for text in self.limitations)
        return "\n".join(lines)


def require_environment(report: EnvironmentReport) -> EnvironmentReport:
    """Return the report, or refuse with every failure named at once.

    Every failure, not the first: an operator who fixes one problem per run learns to
    dread the check.
    """

    if not report.ok:
        raise EnvironmentRefused(report.failures)
    return report
