"""The CT ground-truth evaluator has to be wrong in only the ways it admits to.

These tests use tiny hand-built certificate records, because the point is the arithmetic and
the split, not the corpus. The corpus result lives in out/paper-drop/real_world/ and moves when
the cache moves; a test that pinned it would fail the day someone re-ran collection for an
unrelated reason.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval import ct_ground_truth as ct  # noqa: E402

V3_A = "a" * 56 + ".onion"
V3_B = "b" * 56 + ".onion"
V3_C = "c" * 56 + ".onion"
V3_D = "d" * 56 + ".onion"


def cert(names: str, issuer: str = "Example CA", issued: str = "2026-01-01T00:00:00") -> dict:
    return {"name_value": names, "issuer_name": issuer, "not_before": issued}


class TestParsing:
    def test_names_split_on_newlines_and_commas(self):
        parsed = ct.parse_sans(cert(f"{V3_A}\n{V3_B}, {V3_C}"))
        assert parsed == [V3_A, V3_B, V3_C]

    def test_a_wildcard_folds_into_the_name_it_covers(self):
        # '*.x.onion' and 'x.onion' are one holder. Counting them as two services would split
        # one operator across two nodes and corrupt the ground truth it is scored against.
        assert ct.parse_sans(cert("*.x7y" + "a" * 53 + ".onion")) == ["x7y" + "a" * 53 + ".onion"]

    def test_only_onion_names_count(self):
        assert ct.is_onion(V3_A)
        assert ct.is_onion("a" * 16 + ".onion")
        assert not ct.is_onion("example.com")
        assert not ct.is_onion("short.onion")
        # Base32 has no 0, 1, 8 or 9, so a v3 label containing one is not an address.
        assert not ct.is_onion("0" * 56 + ".onion")

    def test_a_clearnet_certificate_is_dropped_entirely(self):
        assert ct.records([cert("example.com\nwww.example.com")]) == []


class TestGroundTruth:
    def test_two_certificates_sharing_a_name_form_one_cluster(self):
        rows = ct.records([cert(f"{V3_A}\n{V3_B}"), cert(f"{V3_B}\n{V3_C}")])
        found = ct.clusters(rows)
        assert list(found.values()) == [{V3_A, V3_B, V3_C}]

    def test_pairs_are_the_full_choose_two(self):
        assert len(ct.pairs_of({V3_A, V3_B, V3_C, V3_D})) == 6

    def test_pairs_are_order_independent(self):
        assert ct.pairs_of([V3_B, V3_A]) == ct.pairs_of([V3_A, V3_B]) == {(V3_A, V3_B)}


class TestLinkers:
    def test_issuer_alone_links_across_unrelated_holders(self):
        rows = ct.records(
            [cert(f"{V3_A}\n{V3_B}", issuer="Mass CA"), cert(f"{V3_C}\n{V3_D}", issuer="Mass CA")]
        )
        proposed = ct.propose(ct.group_by(rows, "issuer"))
        assert len(proposed) == 6  # every pair, including the four false ones

    def test_the_issue_date_separates_two_customers_of_one_issuer(self):
        rows = ct.records(
            [
                cert(f"{V3_A}\n{V3_B}", issuer="Mass CA", issued="2026-01-01"),
                cert(f"{V3_C}\n{V3_D}", issuer="Mass CA", issued="2026-02-02"),
            ]
        )
        assert ct.propose(ct.group_by(rows, "batch")) == {(V3_A, V3_B), (V3_C, V3_D)}

    def test_a_cap_drops_a_value_seen_on_more_services_than_allowed(self):
        grouped = {("Mass CA", "2026-01-01"): {V3_A, V3_B, V3_C, V3_D}}
        assert ct.propose(grouped, cap=3) == set()
        assert len(ct.propose(grouped, cap=4)) == 6


class TestArithmetic:
    def test_precision_and_recall_on_a_hand_computed_case(self):
        truth = {(V3_A, V3_B), (V3_B, V3_C)}
        score = ct.evaluate({(V3_A, V3_B), (V3_A, V3_C)}, truth)
        assert (score["tp"], score["fp"], score["fn"]) == (1, 1, 1)
        assert score["precision"] == 0.5
        assert score["recall"] == 0.5
        assert score["f1"] == 0.5

    def test_an_empty_linker_scores_zero_rather_than_dividing_by_zero(self):
        score = ct.evaluate(set(), {(V3_A, V3_B)})
        assert (score["precision"], score["recall"], score["f1"]) == (0.0, 0.0, 0.0)


class TestHeldOut:
    def test_the_two_halves_are_disjoint_and_cover_every_service(self):
        rows = ct.records([cert(f"{V3_A}\n{V3_B}"), cert(f"{V3_C}\n{V3_D}")])
        select, report = ct.split_services(rows)
        assert not select & report
        assert select | report == {V3_A, V3_B, V3_C, V3_D}

    def test_a_cluster_never_straddles_the_split(self):
        # Scored on single onions, a cluster split across halves would score a link against
        # ground truth that had half its own evidence removed.
        rows = ct.records([cert(f"{V3_A}\n{V3_B}\n{V3_C}")])
        select, report = ct.split_services(rows)
        assert ({V3_A, V3_B, V3_C} <= select) or ({V3_A, V3_B, V3_C} <= report)

    def test_the_chosen_cap_comes_from_the_list_it_was_given(self):
        rows = ct.records([cert(f"{V3_A}\n{V3_B}"), cert(f"{V3_C}\n{V3_D}", issued="2026-02-02")])
        held = ct.held_out_cap(rows, [2, 5])
        assert held["chosen_cap"] in (2, 5)

    def test_the_report_half_is_never_the_selection_half(self):
        rows = ct.records([cert(f"{V3_A}\n{V3_B}"), cert(f"{V3_C}\n{V3_D}")])
        held = ct.held_out_cap(rows, [2, 5])
        assert held["selection_services"] + held["report_services"] == 4


class TestRun:
    def test_a_corpus_with_one_holder_reports_one_cluster(self):
        result = ct.run([cert(f"{V3_A}\n{V3_B}")])
        assert result["services"] == 2
        assert result["clusters"] == 1
        assert result["true_pairs"] == 1
        assert result["largest_cluster"] == 2

    def test_the_result_explains_its_own_service_count(self):
        # 688 unique onions is the other convention, and a reader who compares the two numbers
        # without an explanation would conclude one of them is broken.
        result = ct.run([cert(f"{V3_A}\n{V3_B}")])
        assert "688" in result["service_count_note"]

    def test_issuer_only_never_beats_issuer_and_date_on_precision(self):
        rows = [
            cert(f"{V3_A}\n{V3_B}", issuer="Mass CA", issued="2026-01-01"),
            cert(f"{V3_C}\n{V3_D}", issuer="Mass CA", issued="2026-02-02"),
        ]
        result = ct.run(rows)
        assert result["issuer_and_date"]["precision"] >= result["issuer_only"]["precision"]

    @pytest.mark.parametrize("caps", [(2, 3), (5,)])
    def test_every_cap_appears_in_the_sweep(self, caps):
        result = ct.run([cert(f"{V3_A}\n{V3_B}")], caps=caps)
        assert [row["cap"] for row in result["sweep"]] == [None, *caps]
