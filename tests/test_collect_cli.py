"""Tests for the acquisition CLI (WP2.2/WP2.3).

The CLI is the only place where an operator meets the guards, so these tests are about
what it *refuses* as much as what it prints: no default transport, no live fetch without an
explicit authorisation, no proposal promoted to a seed, and a sealed case that returns a
non-zero status instead of a quiet success.

Every test here runs the fixture path, so the suite opens no sockets. The live-mode tests
call the real guards and stop at them, which is as far as any test in this repository can
honestly go.
"""

from __future__ import annotations

import argparse
import json

import pytest

from collect.cli import main
from collect.clock import FrozenClock
from collect.fixtures import build_fixtures
from farm.generator import generate_farm
from farm.model import FarmConfig
from gates.audit import AuditAction
from gates.gate import CollectionGate
from gates.roles import Principal, Role

CASE = "CASE-1"
OWNER = Principal(principal_id="a.sharma", role=Role.CASE_OWNER)
ANALYST = Principal(principal_id="b.rao", role=Role.ANALYST)
VIEWER = Principal(principal_id="v.nair", role=Role.VIEWER)

#: The same slice the CLI's defaults build, so a test's expectation and the CLI's addresses
#: cannot drift apart silently.
FIXTURE_ARGS = [
    "--fixtures",
    "--seed",
    "20260925",
    "--services",
    "3",
    "--channels",
    "1",
    "--page-size",
    "3",
    "--ct-limit",
    "3",
]


def cli_fixture_set():
    """The fixture set the CLI's default options build."""
    return build_fixtures(
        generate_farm(FarmConfig(seed=20260925)),
        clock=FrozenClock.at("2026-09-29T12:00:00+00:00"),
        service_limit=3,
        channel_limit=1,
        page_size=3,
        ct_limit=3,
    )


def run_cli(*args: str) -> int:
    return main(list(args))


@pytest.fixture
def root(tmp_path):
    """An acknowledged gate with one open case, built through the gate's own API."""
    root = tmp_path / "casework"
    gate = CollectionGate(root)
    gate.acknowledge(OWNER)
    gate.open_case(
        OWNER,
        case_id=CASE,
        title="Escrow forum operator correlation",
        authorisation_ref="AUTH-2026-0091",
        retention_days=30,
    )
    return root


def gate_at(root) -> CollectionGate:
    return CollectionGate(root)


def run_args(root, *extra: str) -> list[str]:
    return ["--root", str(root), "run", "--case", CASE, "--actor", "a.sharma", "--role",
            "case_owner", *extra]


# --------------------------------------------------------------------------- #
# What the CLI refuses to do on its own
# --------------------------------------------------------------------------- #


def test_run_needs_a_transport_and_has_no_default(root, capsys):
    """Picking one silently means either a network request or pages that were invented."""
    with pytest.raises(SystemExit) as excinfo:
        run_cli(*run_args(root))

    assert excinfo.value.code == 2
    assert "--fixtures" in capsys.readouterr().err


def test_fixtures_and_live_are_mutually_exclusive(root, capsys):
    with pytest.raises(SystemExit) as excinfo:
        run_cli(*run_args(root, "--fixtures", "--live"))

    assert excinfo.value.code == 2
    assert "not allowed with" in capsys.readouterr().err


def test_a_live_run_without_authorisation_is_refused_and_stores_nothing(root, capsys):
    """The transport's own guard, reached through the CLI, with no socket opened."""
    gate = gate_at(root)
    gate.add_seed(CASE, cli_fixture_set().onion_targets[0], OWNER)

    code = run_cli(*run_args(root, "--live"))
    captured = capsys.readouterr()

    assert code == 2
    assert "authorised_mode" in captured.out + captured.err
    assert "guards" in captured.out, "the guards are printed before the refusal"
    assert gate.cases.snapshot_names(CASE) == ()


def test_a_live_onion_target_without_a_socks_proxy_is_refused(root, capsys):
    """The onion rule protects the analyst's resolver, not just the collection."""
    gate = gate_at(root)
    gate.add_seed(CASE, cli_fixture_set().onion_targets[0], OWNER)

    code = run_cli(*run_args(root, "--live", "--authorised-mode"))
    captured = capsys.readouterr()

    assert code == 2
    assert "SOCKS" in captured.out + captured.err
    assert gate.cases.snapshot_names(CASE) == ()


def test_gates_prints_the_guards_and_what_they_do_not_prove(capsys):
    assert run_cli("gates") == 0
    out = capsys.readouterr().out

    assert "authorised_mode must be True" in out
    assert "never been executed against" in out


# --------------------------------------------------------------------------- #
# What the CLI prints
# --------------------------------------------------------------------------- #


def test_targets_prints_the_addresses_a_case_could_declare(capsys):
    assert run_cli("targets", *FIXTURE_ARGS[1:]) == 0
    out = capsys.readouterr().out

    fixtures = cli_fixture_set()
    for address in fixtures.onion_targets:
        assert address in out
    for channel in fixtures.channels:
        assert channel in out
    assert "None of these is a seed" in out

# --------------------------------------------------------------------------- #
# A fixture run, end to end
# --------------------------------------------------------------------------- #


def test_a_fixture_run_stores_findings_and_leaves_the_seed_list_to_the_analyst(root, capsys):
    """The demo's whole claim: collection fills the case, decisions do not."""
    gate = gate_at(root)
    code = run_cli(*run_args(root, *FIXTURE_ARGS, "--search-query", "marketplace"))
    out = capsys.readouterr().out

    assert code == 0
    assert "BUDGET" in out and "pages=1/" in out
    assert gate.cases.get(CASE).seeds == [], "a search result must never become a seed"
    names = gate.cases.snapshot_names(CASE)
    assert len(names) == 1, "the results page is stored whole, once"

    proposals = json.loads(
        (gate.cases.derived_dir(CASE) / "proposals.json").read_text(encoding="utf-8")
    )
    assert len(proposals["proposals"]) == len(cli_fixture_set().onion_targets)
    assert all(item["status"] == "proposed" for item in proposals["proposals"])

    warc = sorted((gate.cases.derived_dir(CASE) / "warc").glob("*.warc"))
    assert len(warc) == 1, "raw exchanges live inside the case, where the purge reaches them"


def test_the_proposal_list_offers_no_way_to_accept_one(root, capsys):
    """A CLI with an `accept` on it is one refactor from a self-seeding crawler."""
    run_cli(*run_args(root, *FIXTURE_ARGS, "--search-query", "marketplace"))
    capsys.readouterr()
    gate = gate_at(root)

    code = run_cli(
        "--root", str(root), "proposals", "--case", CASE, "--actor", "a.sharma", "--role",
        "case_owner",
    )
    out = capsys.readouterr().out

    assert code == 0
    for address in cli_fixture_set().onion_targets:
        assert address in out
    assert "None of these is a seed" in out
    assert gate.cases.get(CASE).seeds == []


def test_the_cli_offers_no_command_that_turns_a_proposal_into_a_seed():
    """Structural, not stylistic: the route must not exist to be forgotten about."""
    from collect.cli import build_parser

    subparsers = next(
        action
        for action in build_parser()._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    commands = set(subparsers.choices)

    assert commands == {"targets", "run", "status", "proposals", "gates"}
    assert not any("seed" in command or "accept" in command for command in commands)



def test_a_seed_an_analyst_added_is_what_the_next_run_fetches(root, capsys):
    """The seam the demo exists to show: the case decides, the collector executes."""
    gate = gate_at(root)
    run_cli(*run_args(root, *FIXTURE_ARGS, "--search-query", "marketplace"))
    capsys.readouterr()

    address = cli_fixture_set().onion_targets[0]
    gate.add_seed(CASE, address, OWNER)
    code = run_cli(*run_args(root, *FIXTURE_ARGS, "--search-query", "marketplace"))
    out = capsys.readouterr().out

    assert code == 0
    stored = gate.collected_snapshots(OWNER, CASE)
    origins = [snapshot["origin"] for snapshot in stored]
    assert origins.count("onion_service") == 1, "the declared seed was fetched"
    assert origins.count("public_index") == 1, "the settled search was not repeated"

    onion_row = next(line for line in out.splitlines() if line.startswith("onion_service"))
    assert "pages=1" in onion_row and "stored=1" in onion_row
    assert "not searched again" in out


def test_a_seed_the_fixture_set_does_not_serve_is_reported_not_invented(root, capsys):
    """A fixture run cannot conjure a page: it says which seeds it cannot reach."""
    from farm.model import deterministic_onion_address

    gate = gate_at(root)
    stale = deterministic_onion_address(20260929, "stale")
    gate.add_seed(CASE, stale, OWNER)

    code = run_cli(*run_args(root, *FIXTURE_ARGS))
    out = capsys.readouterr().out

    assert code == 0, "an unreachable seed is a finding, not a crash"
    assert f"http://{stale}/" in out
    assert "not served by this transport" in out
    assert "not in this fixture set" in out
    assert gate.cases.get(CASE).seeds == [stale]


def test_the_scope_is_what_the_case_declared_and_is_printed(root, capsys):
    gate = gate_at(root)
    gate.add_seed(CASE, cli_fixture_set().onion_targets[0], OWNER)

    run_cli(*run_args(root, *FIXTURE_ARGS, "--search-query", "marketplace"))
    out = capsys.readouterr().out

    assert "onion targets: 1" in out
    assert "search queries: 1" in out
    assert "ct queries: 0" in out
    assert "telegram channels: 0" in out


# --------------------------------------------------------------------------- #
# Refusals reach the exit status
# --------------------------------------------------------------------------- #


def test_a_sealed_case_makes_run_exit_non_zero_with_the_reason(root, capsys):
    gate = gate_at(root)
    gate.cases.seal(CASE)

    code = run_cli(*run_args(root, *FIXTURE_ARGS, "--search-query", "marketplace"))
    out = capsys.readouterr().out

    assert code == 1
    assert "sealed" in out
    assert "BLOCKED" in out, "the row says the path was blocked rather than merely empty"
    assert "refused: 4 of 4" in out
    assert gate.cases.snapshot_names(CASE) == ()


def test_status_shows_where_each_walk_stopped_and_records_the_read(root, capsys):
    gate = gate_at(root)
    run_cli(*run_args(root, *FIXTURE_ARGS, "--channel", cli_fixture_set().channels[0]))
    capsys.readouterr()

    code = run_cli(
        "--root", str(root), "status", "--case", CASE, "--actor", "a.sharma", "--role",
        "case_owner",
    )
    out = capsys.readouterr().out

    assert code == 0
    assert "cursors   : 1" in out
    assert "telegram_preview:" in out
    assert "complete" in out
    assert "WARC      : 1" in out

    reads = gate.audit.entries()
    viewed = [entry for entry in reads if entry.action == AuditAction.SNAPSHOT_VIEWED]
    assert viewed, "reading the case's contents is a disclosure event, and is recorded"


def test_a_viewer_cannot_read_case_status_or_proposals(root, capsys):
    run_cli(*run_args(root, *FIXTURE_ARGS, "--search-query", "marketplace"))
    capsys.readouterr()

    for command in ("status", "proposals"):
        code = run_cli(
            "--root", str(root), command, "--case", CASE, "--actor", "v.nair", "--role",
            "viewer",
        )
        out = capsys.readouterr().out
        assert code == 2, command
        assert "REFUSED" in out
        assert "viewer" in out


def test_an_unknown_case_is_refused_by_every_command_that_names_one(root, capsys):
    unknown = "CASE-NOPE"
    invocations = (
        run_args(root, *FIXTURE_ARGS),
        ["--root", str(root), "status", "--case", unknown, "--actor", "a.sharma", "--role",
         "case_owner"],
        ["--root", str(root), "proposals", "--case", unknown, "--actor", "a.sharma",
         "--role", "case_owner"],
    )

    for argv in invocations:
        code = run_cli(*[unknown if argument == CASE else argument for argument in argv])
        out = capsys.readouterr().out
        assert code == 2, argv
        assert "REFUSED" in out

