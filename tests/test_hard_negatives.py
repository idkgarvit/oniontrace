"""A hard negative is only worth having if it could have tripped the rule.

The farm's careful rule set scores 19 true positives and no false positives. That number is
only evidence if something was present that a careless rule would have got wrong, so every
fixture has to do two things at once: the careful rule must stay silent on it, and the careless
one must fire. A fixture that neither fires nor is tempting is decoration, and a fixture that
fires the careful rule is a bug in the fixture.

These tests hold both ends. They are cheap because they need no farm: a fixture is a path, a
body and some headers, and a rule is a function over one endpoint.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval.detectors import misconfig  # noqa: E402
from farm.model import Endpoint  # noqa: E402
from farm.catalogue import rules_without_hard_negative  # noqa: E402
from farm.model import Service  # noqa: E402
from farm.services import (  # noqa: E402
    HARD_NEGATIVE_FIXTURES,
    inject_rule_hard_negative,
    rules_with_hard_negative_fixture,
)

KINDS = sorted(misconfig.RULES)


def endpoint_for(kind: str) -> Endpoint:
    path, body, headers = HARD_NEGATIVE_FIXTURES[kind]
    return Endpoint(path=path, body=body, headers=dict(headers))


class TestCoverage:
    def test_every_rule_has_a_fixture(self):
        assert set(HARD_NEGATIVE_FIXTURES) >= set(misconfig.RULES)
        assert rules_without_hard_negative() == []
        assert set(rules_with_hard_negative_fixture()) == set(misconfig.RULES)

    def test_there_are_nineteen_rules(self):
        # If the rule set grows, the fixtures have to grow with it, and this test is where
        # that shows up rather than in a report that quietly claims full coverage.
        assert len(misconfig.RULES) == 19


class TestTheFixtureDoesNotTripTheCarefulRule:
    @pytest.mark.parametrize("kind", KINDS)
    def test_careful_rule_stays_silent(self, kind):
        hit = misconfig.RULES[kind](endpoint_for(kind), None)
        assert hit is None, (
            f"the negative fixture for {kind} trips the careful rule, which makes it a "
            f"false positive planted by the farm itself: {hit}"
        )


class TestTheFixtureWouldCatchACarelessRule:
    @pytest.mark.parametrize("kind", KINDS)
    def test_careless_rule_fires(self, kind):
        if kind not in misconfig.NAIVE_RULES:
            pytest.skip(f"no careless variant exists for {kind}")
        hit = misconfig.NAIVE_RULES[kind](endpoint_for(kind), None)
        assert hit is not None, (
            f"the fixture for {kind} does not tempt the careless rule, so it cannot show "
            "that the careful rule is doing anything"
        )


class TestFixtureShape:
    def test_no_two_fixtures_share_a_path(self):
        # Two fixtures on one path would overwrite each other on a service and silently
        # reduce coverage without failing anything.
        paths = [spec[0] for spec in HARD_NEGATIVE_FIXTURES.values()]
        assert len(paths) == len(set(paths))

    def test_every_fixture_has_a_body(self):
        for kind, (path, body, _headers) in HARD_NEGATIVE_FIXTURES.items():
            assert path.startswith("/"), kind
            assert body.strip(), kind

    def test_injection_places_the_fixture_at_its_path(self):
        service = Service(
            service_id="svc-probe",
            cluster_id="C0",
            address="probe" + "a" * 51 + ".onion",
            title="Probe",
            topic="probe",
            endpoints=[],
        )
        assert inject_rule_hard_negative(service, "timezone_leak") is True
        placed = service.endpoint("/contact")
        assert placed is not None
        assert placed.status == 200
        assert placed.headers["Date"].endswith("GMT")

    def test_injection_refuses_a_kind_it_has_no_fixture_for(self):
        service = Service(
            service_id="svc-probe",
            cluster_id="C0",
            address="probe" + "a" * 51 + ".onion",
            title="Probe",
            topic="probe",
            endpoints=[],
        )
        assert inject_rule_hard_negative(service, "not_a_rule") is False
