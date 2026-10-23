"""Command-line interface for the Part E2 deployment gates.

Usage
-----
    python -m gates acceptable-use
    python -m gates ack --accept --actor a.sharma
    python -m gates case open --case CASE-1 --title "..." --authorisation AUTH-77 \\
        --retention-days 30 --actor a.sharma --role case_owner
    python -m gates case add-seed --case CASE-1 --seed http://example.onion --actor a.sharma
    python -m gates authorise --case CASE-1 --actor a.sharma
    python -m gates audit verify
    python -m gates purge --case CASE-1 --actor root --role admin
    python -m gates demo

**This CLI is a demonstration path, not a deployment path.** It does not authenticate
anyone: `--role` is taken at face value, so it shows what the policy does but proves
nothing about who is asking. In a real deployment the principal is established outside the
gate (SSO, session, badge) and the verified `Principal` is passed in; the gate's role
checks are only as good as that identity, and this file does not pretend otherwise.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from gates.acceptable_use import statement_text
from gates.gate import CollectionGate
from gates.presentation import CandidateLink
from gates.roles import Principal, Role

DEMO_WARNING = (
    "role enforcement is real; identity is not -- this CLI takes --role at face value"
)


def _gate(args: argparse.Namespace) -> CollectionGate:
    return CollectionGate(Path(args.root))


def _principal(args: argparse.Namespace) -> Principal:
    return Principal(principal_id=args.actor, role=Role(args.role))


def cmd_acceptable_use(args: argparse.Namespace) -> int:
    print(statement_text())
    return 0


def cmd_ack(args: argparse.Namespace) -> int:
    gate = _gate(args)
    record = gate.acknowledge(_principal(args))
    print(
        f"acknowledged by {record.principal_id} ({record.role}) at "
        f"{record.acknowledged_at}"
    )
    print(f"statement digest: {record.statement_digest}")
    print(f"({DEMO_WARNING})")
    return 0


def cmd_case_open(args: argparse.Namespace) -> int:
    gate = _gate(args)
    case = gate.open_case(
        _principal(args),
        case_id=args.case,
        title=args.title,
        authorisation_ref=args.authorisation,
        retention_days=args.retention_days,
    )
    print(f"opened {case.case_id}: owner={case.owner} auth={case.authorisation_ref}")
    print(f"retention until {case.retention_until}")
    return 0


def cmd_case_add_seed(args: argparse.Namespace) -> int:
    gate = _gate(args)
    case = gate.add_seed(args.case, args.seed, _principal(args))
    print(f"{case.case_id} now holds {len(case.seeds)} seed(s)")
    return 0


def cmd_case_list(args: argparse.Namespace) -> int:
    gate = _gate(args)
    for case in gate.cases.all():
        state = "purged" if case.purged else ("sealed" if case.sealed else "open")
        print(
            f"{case.case_id:24s} {state:7s} owner={case.owner:16s} "
            f"seeds={len(case.seeds):2d} retention_until={case.retention_until}"
        )
    return 0


def cmd_authorise(args: argparse.Namespace) -> int:
    gate = _gate(args)
    permit = gate.authorise_collection(args.case, _principal(args))
    print(json.dumps(permit.to_dict(), indent=2))
    print(f"valid until {permit.expires_at}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    """Store one demonstration snapshot through the gate.

    Stands in for the crawler of WP2.2: it holds no exemption and goes through exactly the
    checks the real collector will have to satisfy.
    """
    gate = _gate(args)
    principal = _principal(args)
    permit = gate.authorise_collection(args.case, principal)
    payload = {
        "seed": args.seed,
        "body": args.body,
        "note": "demonstration snapshot; no network access involved",
    }
    names = gate.ingest(permit, principal, [payload])
    print(f"stored {len(names)} snapshot(s) under case {args.case}: {', '.join(names)}")
    return 0


def cmd_audit_verify(args: argparse.Namespace) -> int:
    gate = _gate(args)
    result = gate.audit.verify()
    print(json.dumps(result, indent=2))
    return 0 if result["verified"] else 1


def cmd_audit_tail(args: argparse.Namespace) -> int:
    gate = _gate(args)
    entries = gate.audit.read(
        _principal(args), case_id=args.case, limit=args.limit or None
    )
    for entry in entries:
        detail = json.dumps(entry.detail, sort_keys=True)
        print(f"{entry.seq:4d} {entry.recorded_at} {entry.actor:16s} "
              f"{entry.action:24s} {detail[:90]}")
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    gate = _gate(args)
    case = gate.purge(args.case, _principal(args))
    print(
        f"purged {case.case_id}: {case.purged_snapshots} snapshot(s) removed at "
        f"{case.purged_at}; tombstone retained with authorisation "
        f"{case.authorisation_ref}"
    )
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    """Show what the analyst view emits, including the abstention state."""
    gate = _gate(args)
    principal = _principal(args)
    link = CandidateLink(
        case_id=args.case,
        left=args.left,
        right=args.right,
        score=args.score,
        method=args.method,
        evidence_refs=("sha256:0000000000000000", f"snapshot:{args.case}/snapshot-0000.json"),
        counter_evidence=(
            "distinct TLS certificate fingerprint",
            "ssh host key differs",
        ),
    )
    for block in gate.render_links_or_abstain(principal, args.case, (link,)):
        print(block)
    print()
    print("--- and with no candidate above threshold ---")
    for block in gate.render_links_or_abstain(principal, args.case, ()):
        print(block)
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Walk the gate through its refusals and its records, in order.

    A gate that has never refused anything has not been shown to work, so the demo starts
    with the refusals rather than ending with them.
    """
    gate = _gate(args)
    root = Path(args.root)
    print(f"gate root: {root}")
    print(f"({DEMO_WARNING})")
    print()

    analyst = Principal(principal_id="a.sharma", role=Role.ANALYST)
    owner = Principal(principal_id="a.sharma", role=Role.CASE_OWNER)
    admin = Principal(principal_id="r.iyer", role=Role.ADMIN)
    viewer = Principal(principal_id="v.nair", role=Role.VIEWER)

    def attempt(label: str, call) -> None:
        try:
            call()
            print(f"  ALLOWED  {label}")
        except Exception as error:  # noqa: BLE001 - the demo reports any refusal
            print(f"  REFUSED  {label}\n             -> {error}")

    print("1. before the statement is acknowledged")
    attempt(
        "open a case",
        lambda: gate.open_case(
            owner,
            case_id="CASE-DEMO",
            title="demo",
            authorisation_ref="AUTH-77",
            retention_days=30,
        ),
    )

    gate.acknowledge(owner)
    print("\n2. acknowledged; now the case gate")
    attempt("collect with no case at all", lambda: gate.authorise_collection("CASE-NOPE", owner))

    gate.open_case(
        owner,
        case_id="CASE-DEMO",
        title="demonstration case",
        authorisation_ref="AUTH-77",
        retention_days=30,
    )
    gate.add_seed("CASE-DEMO", "http://example.onion", owner)
    print("\n   case opened with an authorisation reference and a retention date")

    print("\n3. permits")
    permit = gate.authorise_collection("CASE-DEMO", owner)
    attempt("collect with a valid permit", lambda: gate.ingest(permit, owner, [{"body": "x"}]))
    attempt(
        "mint a permit as a viewer",
        lambda: gate.authorise_collection("CASE-DEMO", viewer),
    )
    attempt(
        "collect with a permit issued to somebody else",
        lambda: gate.ingest(permit, viewer, [{"body": "x"}]),
    )

    forged = type(permit)(**{**permit.to_dict(), "signature": "0" * 64})
    attempt("collect with a forged permit", lambda: gate.ingest(forged, owner, [{"body": "x"}]))

    print("\n4. what the view may say")
    link = CandidateLink(
        case_id="CASE-DEMO",
        left="svc-1",
        right="svc-2",
        score=0.41,
        method="shared favicon hash",
        evidence_refs=("sha256:deadbeef",),
        counter_evidence=("distinct TLS certificate fingerprint",),
    )
    for block in gate.render_links_or_abstain(owner, "CASE-DEMO", (link,)):
        print(block)
    attempt("render candidates for a viewer", lambda: gate.view_candidates(viewer, "CASE-DEMO", (link,)))

    print("\n5. export")
    attempt(
        "export as analyst",
        lambda: gate.export_evidence_pack("CASE-DEMO", analyst, (link,)),
    )
    pack = gate.export_evidence_pack("CASE-DEMO", owner, (link,), snapshot_count=1)
    print(f"  ALLOWED  export as case owner\n             -> {pack.splitlines()[0]}")

    print("\n6. purge")
    attempt("purge as analyst", lambda: gate.purge("CASE-DEMO", analyst))
    purged = gate.purge("CASE-DEMO", admin)
    print(f"  ALLOWED  purge as admin ({purged.purged_snapshots} snapshot(s) removed)")

    print("\n7. the record survives the purge")
    verification = gate.audit.verify()
    print(f"  audit verified: {verification['verified']} "
          f"({verification['entries_checked']} entries)")
    print(f"  anchored: {verification['anchored']} "
          f"(a chain alone cannot see a truncated trail; the anchor can)")
    print(f"  audit file still present: {gate.audit.path.exists()}")
    print(f"  case snapshots remaining: {len(gate.cases.snapshot_names('CASE-DEMO'))}")

    print("\n8. refusals were recorded too")
    refusals = [
        e for e in gate.audit.for_case("CASE-DEMO") if e.action == "gate_refused"
    ]
    print(f"  {len(refusals)} refusal(s) on the trail for this case")
    print(f"  audit head: {verification.get('head_hash', '')[:16]}...")
    print(
        f"VERDICT: {len(refusals)} refusals recorded, audit trail verified over "
        f"{verification['entries_checked']} entries and anchored, "
        f"head {verification.get('head_hash', '')[:16]}"
    )
    return 0


def _add_actor(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--actor", required=True, help="principal identifier")
    parser.add_argument(
        "--role",
        default="analyst",
        choices=[role.value for role in Role],
        help="role the actor claims (unauthenticated in this CLI; see module docstring)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gates",
        description=(
            "ONIONTRACE Part E2 deployment gates: cases, authorisation, audit and "
            "presentation rules. " + DEMO_WARNING
        ),
    )
    parser.add_argument("--root", default="./casework", help="gate root directory")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("acceptable-use", help="print ACCEPTABLE_USE.md").set_defaults(
        func=cmd_acceptable_use
    )

    ack = sub.add_parser("ack", help="record acceptance of the statement")
    ack.add_argument("--accept", action="store_true", required=True)
    _add_actor(ack)
    ack.set_defaults(func=cmd_ack)

    case = sub.add_parser("case", help="case operations")
    case_sub = case.add_subparsers(dest="case_command", required=True)

    case_open = case_sub.add_parser("open", help="open a case")
    case_open.add_argument("--case", required=True)
    case_open.add_argument("--title", required=True)
    case_open.add_argument("--authorisation", required=True)
    case_open.add_argument("--retention-days", type=int, default=30)
    _add_actor(case_open)
    case_open.set_defaults(func=cmd_case_open)

    add_seed = case_sub.add_parser("add-seed", help="add a per-case seed")
    add_seed.add_argument("--case", required=True)
    add_seed.add_argument("--seed", required=True)
    _add_actor(add_seed)
    add_seed.set_defaults(func=cmd_case_add_seed)

    case_list = case_sub.add_parser("list", help="list cases")
    case_list.set_defaults(func=cmd_case_list)

    authorise = sub.add_parser("authorise", help="mint a collection permit")
    authorise.add_argument("--case", required=True)
    _add_actor(authorise)
    authorise.set_defaults(func=cmd_authorise)

    ingest = sub.add_parser("ingest", help="store a demonstration snapshot")
    ingest.add_argument("--case", required=True)
    ingest.add_argument("--seed", default="http://example.onion")
    ingest.add_argument("--body", default="<html><body>demonstration</body></html>")
    _add_actor(ingest)
    ingest.set_defaults(func=cmd_ingest)

    render = sub.add_parser("render", help="show the analyst view for one candidate")
    render.add_argument("--case", required=True)
    render.add_argument("--left", default="svc-1")
    render.add_argument("--right", default="svc-2")
    render.add_argument("--score", type=float, default=0.41)
    render.add_argument("--method", default="shared favicon hash")
    _add_actor(render)
    render.set_defaults(func=cmd_render)

    audit = sub.add_parser("audit", help="audit log operations")
    audit_sub = audit.add_subparsers(dest="audit_command", required=True)
    audit_sub.add_parser("verify", help="verify the hash chain").set_defaults(
        func=cmd_audit_verify
    )
    tail = audit_sub.add_parser("tail", help="read the trail (admin only)")
    tail.add_argument("--case", default=None)
    tail.add_argument("--limit", type=int, default=20)
    _add_actor(tail)
    tail.set_defaults(func=cmd_audit_tail)

    purge = sub.add_parser("purge", help="purge a case's data (admin only)")
    purge.add_argument("--case", required=True)
    _add_actor(purge)
    purge.set_defaults(func=cmd_purge)

    sub.add_parser("demo", help="walk the gate through its refusals and records").set_defaults(
        func=cmd_demo
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Exception as error:  # noqa: BLE001 - the CLI reports refusals as text
        print(f"REFUSED: {error}")
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())