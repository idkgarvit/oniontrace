"""Does a capped directory walk ever get past the pages it has already read?

It used to not, and this script was the reproduction. One directory with five children, a
three-page budget (one of which the robots lookup spends), walked three times, used to print:

    run 1: ['', 'one/']
    run 2: ['']            <- re-reads the index and reaches nothing new
    run 3: ['']            <- and stays there for ever

The cause was that the cap was applied to the listing before completed pages were filtered:
`children = listed[:cap]` selected the same prefix on every run. It now selects the first
`cap` *unread* children, so the same probe advances:

    run 1: ['', 'one/']
    run 2: ['', 'two/']
    run 3: ['', 'three/']

With a budget large enough to hold every page the walk still reads all five in one pass,
which is what the last line of this output shows.

Run it:  python3 scripts/qa/directory_cap_probe.py
"""

from __future__ import annotations

import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path.home() / "oniontrace"))

from collect.clock import FrozenClock  # noqa: E402
from collect.collector import CollectionPolicy, CollectionScope, Collector  # noqa: E402
from collect.schedule import Politeness  # noqa: E402
from collect.transport import FixtureTransport, Page  # noqa: E402
from gates.gate import CollectionGate  # noqa: E402
from gates.roles import Principal, Role  # noqa: E402

HOST = "directory.invalid"
BASE = f"https://{HOST}/directory/"
NAMES = ("one", "two", "three", "four", "five")

pages = {
    BASE: Page(body="<html><body>" + "".join(f'<a href="/directory/{n}/">{n}</a>' for n in NAMES)
               + "</body></html>"),
}
for name in NAMES:
    pages[f"{BASE}{name}/"] = Page(body="<html><body>leaf</body></html>")

clock = FrozenClock()
transport = FixtureTransport(clock=clock, hosts=(HOST,), pages=pages)
gate = CollectionGate(pathlib.Path(tempfile.mkdtemp()) / "casework")
owner = Principal(principal_id="a.sharma", role=Role.CASE_OWNER)
gate.acknowledge(owner)
gate.open_case(owner, case_id="CASE-PREFIX", title="prefix probe", authorisation_ref="AUTH-P",
               retention_days=30)


def walk(pages_allowed: int, case_id: str = "CASE-PREFIX") -> list[str]:
    collector = Collector(
        gate=gate,
        transport=transport,
        clock=clock,
        policy=CollectionPolicy(
            politeness=Politeness(min_host_delay_seconds=0.0, max_pages_per_source=pages_allowed)
        ),
    )
    transport.fetched.clear()
    collector.collect(
        case_id=case_id, principal=owner, scope=CollectionScope(directories=(BASE,))
    )
    return [u.rsplit("/directory/", 1)[-1] for u in transport.fetched if not u.endswith("robots.txt")]


print("five children, a budget of three each time (one of which is the robots lookup):")
for attempt in range(1, 4):
    print(f"  run {attempt}: {walk(3)}")
print()
# A second case, so the pages already read under CASE-PREFIX do not hide the one-pass result.
gate.open_case(owner, case_id="CASE-FULL", title="full probe", authorisation_ref="AUTH-P",
               retention_days=30)
print("with room for every page, in one go:")
print(f"  run 1: {walk(40, case_id='CASE-FULL')}")
