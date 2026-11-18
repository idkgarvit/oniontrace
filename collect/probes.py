"""The fixed list of well-known paths a misconfiguration scan needs to read.

Why this is a list and not a crawler
------------------------------------
The front page answers "what does this operator advertise". It does not answer "did they
leave the door open": ``/.git/config``, ``/.env`` and ``/server-status`` are never linked
from navigation, because nobody intends to advertise them. A detector that only sees ``/``
can score well on a farm whose flaws live on named endpoints and still miss every real one.

What keeps this from becoming directory enumeration:

* the list is **fixed in code** (thirteen paths), not generated, mutated or extended at
  runtime, and a caller cannot pass an arbitrary list through this module;
* each path is requested **once**, with a plain GET, no parameters, no cookies and no
  authentication;
* it runs only when live collection is on **and** the operator passes
  ``--probe-known-paths``, and the targets are the case's declared seeds.

Those three properties make it a checklist against services the case already authorised,
which is a different activity from guessing names until something answers. The default is
off, because a tool that silently probes a stranger's service is doing more than the case
said it would.
"""

from __future__ import annotations

#: Paths that a passive front-page read cannot discover, in a stable order so two runs
#: produce comparable snapshots. Every entry is a conventional location for a file an
#: operator either meant to publish or meant to keep private; none is a guess at a private
#: name.
PROBE_PATHS: tuple[str, ...] = (
    "/robots.txt",
    "/.git/config",
    "/.env",
    "/backup.php.bak",
    "/phpinfo.php",
    "/server-status",
    "/files/",
    "/admin/",
    "/staging",
    "/debug/vars",
    "/README.txt",
    "/error",
    "/photo.jpg",
)


def is_probe_path(path: str) -> bool:
    """Whether a path is one of the fixed probes.

    Used by tests and by the collector's own validation. A probe that is not on the list
    would make "fixed list" false, so the check is a membership test rather than a pattern.
    """

    return path in PROBE_PATHS
