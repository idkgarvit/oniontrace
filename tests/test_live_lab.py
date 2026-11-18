"""The live lab is only useful if its truth is fixed before the live run starts.

Everything here runs offline. The point is not to test Tor -- no test in this repository
can -- but to pin the three things the live experiment depends on:

  * the lab is deterministic, and its fingerprint identifies its content;
  * every rule has exactly one planted positive and a clean control that tempts it;
  * the WARC reader recovers what the collector actually stored, and the scorers give
    the same answer on live-shaped bytes that they give on the farm.

If these hold, a live run that disagrees with them is a finding about the live path.
If they do not hold, the live run would be measuring the lab's bugs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collect.probes import PROBE_PATHS, is_probe_path  # noqa: E402
from collect.transport import FetchResult  # noqa: E402
from collect.warc import build_record, stream  # noqa: E402
from eval import live_lab  # noqa: E402
from eval.detectors import correlation, misconfig  # noqa: E402
from eval.live_lab import (  # noqa: E402
    CLEAN_CONTROLS,
    FLAW_PATHS,
    PLANTED,
    RULE_KINDS,
    Lab,
    Observed,
    build_lab,
    build_lab_farm,
    build_observed_farm,
    extract_artifacts,
    observations_from_warc,
    parse_warc,
    score,
    write_report,
)


def observed_from_routes(lab: Lab) -> tuple[dict[str, str], dict[str, dict[str, Observed]]]:
    """The lab's own responses in the shape the WARC reader produces.

    This is not a substitute for the live run. It is the control: it says what the scorer
    should do when every response arrives intact, so a live diff has something to be
    different from.
    """

    addresses = {service.service_id: f"{service.service_id}.onion" for service in lab.services}
    observed = {
        addresses[service.service_id]: {
            path: Observed(path, route.status, dict(route.headers), route.body)
            for path, route in service.routes.items()
        }
        for service in lab.services
    }
    return addresses, observed


class TestDeterminism:
    def test_two_builds_have_the_same_fingerprint(self):
        assert build_lab().fingerprint == build_lab().fingerprint

    def test_fingerprint_changes_when_content_changes(self):
        from dataclasses import replace

        lab = build_lab()
        before = lab.fingerprint
        front = lab.services[0].routes["/"]
        lab.services[0].routes["/"] = replace(front, body=front.body + " edited")
        assert lab.fingerprint != before

    def test_serialised_lab_carries_a_checkable_fingerprint(self):
        lab = build_lab()
        data = lab.to_dict()
        assert data["fingerprint"] == lab.fingerprint
        # The digest is over the body without the digest field, so a reader can recompute
        # it from the file instead of trusting the field.
        assert Lab.from_dict(data).fingerprint == lab.fingerprint

    def test_routes_round_trip_through_json(self):
        lab = build_lab()
        data = json.loads(json.dumps(lab.to_dict(), sort_keys=True))
        again = Lab.from_dict(data)
        assert again.fingerprint == lab.fingerprint
        assert again.service("beta-2").routes["/.env"].body == lab.service("beta-2").routes[
            "/.env"
        ].body


class TestCoverage:
    def test_the_lab_covers_every_rule_exactly_once(self):
        assert set(RULE_KINDS) == set(misconfig.RULES)
        planted = [kind for kinds in PLANTED.values() for kind in kinds]
        assert sorted(planted) == sorted(RULE_KINDS)
        assert len(planted) == len(set(planted)) == 19

    def test_every_rule_has_a_path(self):
        assert set(FLAW_PATHS) == set(RULE_KINDS)

    def test_every_clean_control_is_a_real_route(self):
        lab = build_lab()
        for service_id, paths in CLEAN_CONTROLS.items():
            service = lab.service(service_id)
            for path in paths:
                assert path in service.routes
                # A clean control must not be the path a flaw is planted at, or it would
                # score as a miss rather than as a trap.
                assert path not in {FLAW_PATHS[kind] for kind in service.flaws}


class TestDetectionDesign:
    def test_careful_rules_have_no_false_positives(self):
        scoring = misconfig.evaluate(build_lab_farm(build_lab()))
        assert (scoring.tp, scoring.fp, scoring.fn) == (19, 0, 0)
        assert scoring.recall == 1.0
        assert scoring.precision == 1.0

    def test_naive_rules_are_actually_defeated(self):
        scoring = misconfig.evaluate(build_lab_farm(build_lab()), misconfig.NAIVE_RULES)
        # The exact numbers are not the point; the gap is. If the naive table ever stops
        # producing false positives, the clean controls have stopped doing their job.
        assert scoring.fp > 0
        assert scoring.fn > 0
        assert scoring.precision is not None and scoring.precision < 0.5


class TestArtifacts:
    def test_cluster_mates_share_a_favicon_hash(self):
        lab = build_lab()
        front = {s.service_id: extract_artifacts(s.routes["/"].body) for s in lab.services}
        assert front["alpha-1"]["favicon"] == front["alpha-2"]["favicon"]
        assert front["alpha-1"]["favicon"] == front["alpha-3"]["favicon"]
        assert front["alpha-1"]["favicon"] != front["beta-1"]["favicon"]
        assert front["gamma-1"]["favicon"] != front["gamma-2"]["favicon"]
        assert front["alpha-1"]["analytics_id"] == "UA-ALPHA-4102"
        assert front["alpha-1"]["contact_handle"] == "ops@alpha-lantern.example"

    def test_stock_vendor_value_is_on_four_services(self):
        lab = build_lab()
        holders = {
            s.service_id
            for s in lab.services
            if extract_artifacts(s.routes["/"].body).get("vendor_analytics")
            == live_lab.VENDOR_ANALYTICS
        }
        assert holders == set(live_lab.STOCK_SERVICES)


class TestCorrelationDesign:
    def test_naive_correlator_links_across_clusters(self):
        lab = build_lab()
        addresses, observed = observed_from_routes(lab)
        farm = build_observed_farm(lab, addresses, observed)
        gamma = correlation.rank_services(
            farm, "gamma-1", farm.correlation_pool("gamma-1"), max_artifact_frequency=None
        )
        assert gamma, "the stock vendor value should link gamma-1 to someone"

    def test_cap_removes_the_vendor_trap(self):
        lab = build_lab()
        addresses, observed = observed_from_routes(lab)
        farm = build_observed_farm(lab, addresses, observed)
        for service_id in live_lab.STOCK_SERVICES - {"alpha-2", "beta-2"}:
            ranked = correlation.rank_services(
                farm, service_id, farm.correlation_pool(service_id), max_artifact_frequency=3
            )
            assert ranked == [], f"{service_id} should abstain once the vendor value is filtered"

    def test_cluster_mates_survive_the_cap(self):
        lab = build_lab()
        addresses, observed = observed_from_routes(lab)
        farm = build_observed_farm(lab, addresses, observed)
        for service_id in ("alpha-1", "beta-3"):
            ranked = correlation.rank_services(
                farm, service_id, farm.correlation_pool(service_id), max_artifact_frequency=3
            )
            assert {candidate for candidate, _ in ranked} == set(farm.cluster_mates(service_id))


class TestLiveShapedScore:
    def test_the_design_scores_as_designed(self):
        lab = build_lab()
        addresses, observed = observed_from_routes(lab)
        result = score(lab, addresses, observed)
        careful = result["detection"]["careful"]
        assert (careful["tp"], careful["fp"], careful["fn"]) == (19, 0, 0)
        assert result["detection"]["path_accuracy"] == {
            "expected": 19,
            "on_planted_path": 19,
            "on_wrong_path": 0,
        }
        assert result["correlation"]["filtered"]["link_scoring"]["fp"] == 0
        assert result["correlation"]["naive"]["link_scoring"]["fp"] > 0
        assert result["correlation"]["singleton_abstention"]["filtered"] == 2
        assert result["correlation"]["singleton_abstention"]["naive"] == 0


class TestWarcReading:
    def _record(self, *, url: str, path: str, status: int, body: str, headers=None):
        fetch = FetchResult(
            url=url + path,
            status=status,
            headers=headers or {"Content-Type": "text/plain", "X-Lab": "yes"},
            body=body.encode(),
            retrieved_at="2026-11-17T00:00:00Z",
            transport="tor-socks",
        )
        return build_record(fetch, case_id="CASE-LAB-1", seed_label=url)

    def test_round_trip_preserves_host_path_status_headers_and_body(self):
        text = stream([
            self._record(url="http://alpha1example.onion", path="/", status=200,
                         body="<html>front</html>"),
            self._record(url="http://alpha1example.onion", path="/server-status", status=200,
                         body="Apache Server Status", headers={"Content-Type": "text/html"}),
            self._record(url="http://beta2example.onion", path="/.env", status=404,
                         body="Not Found"),
        ])
        parsed = parse_warc(text)
        assert set(parsed) == {"alpha1example.onion", "beta2example.onion"}
        assert parsed["alpha1example.onion"]["/"].body == "<html>front</html>"
        assert parsed["alpha1example.onion"]["/server-status"].headers["Content-Type"] == "text/html"
        assert parsed["beta2example.onion"]["/.env"].status == 404
        assert parsed["beta2example.onion"]["/.env"].body == "Not Found"

    def test_a_body_containing_the_boundary_is_not_truncated(self):
        # The reader slices by Content-Length, not by the boundary marker, and this is the
        # case that would catch a delimiter-based implementation.
        marker = "WARC/1.0\r\n"
        text = stream([self._record(
            url="http://gammaexample.onion", path="/", status=200,
            body=f"<html>{marker} in the body</html>",
        )])
        parsed = parse_warc(text)
        assert marker in parsed["gammaexample.onion"]["/"].body

    def test_reads_a_warc_file_with_its_crlf_separators_intact(self, tmp_path):
        # The first live run wrote a valid WARC and the reader returned nothing, because
        # Path.read_text translates CRLF to LF. The file path is the part that matters.
        text = stream([self._record(
            url="http://alpha1example.onion", path="/", status=200, body="front",
        )])
        path = tmp_path / "run.warc"
        path.write_text(text, encoding="utf-8", newline="")
        parsed = observations_from_warc([path])
        assert parsed["alpha1example.onion"]["/"].status == 200
        assert parsed["alpha1example.onion"]["/"].body == "front"

    def test_empty_stream_is_empty_not_an_error(self):
        assert parse_warc("") == {}


class TestLabServer:
    """The server is part of the experiment: a header it loses changes a detector's answer."""

    @pytest.fixture()
    def running(self, tmp_path):
        import socket
        import subprocess
        import time
        import urllib.request

        lab = build_lab()
        service_dir = tmp_path / "service"
        service_dir.mkdir()
        (service_dir / "routes.json").write_text(
            json.dumps({"routes": lab.service("beta-1").to_dict()["routes"]}), encoding="utf-8"
        )
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "eval" / "live_lab.py"), "serve", str(service_dir),
             str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        def get(path: str):
            return urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5)

        try:
            for _ in range(50):
                try:
                    get("/")
                    break
                except OSError:
                    time.sleep(0.05)
            else:
                pytest.fail("the lab server never started")
            yield get
        finally:
            process.terminate()
            process.wait(timeout=5)

    def test_serves_the_front_page(self, running):
        response = running("/")
        assert response.status == 200
        assert b"Atlas Print Works" in response.read()

    def test_serves_a_planted_flaw(self, running):
        response = running("/.git/config")
        assert response.status == 200
        assert b"[core]" in response.read()

    def test_serves_a_per_route_header(self, running):
        response = running("/.git/config")
        assert response.headers["Server"] == "Apache/2.4.49"

    def test_serves_a_clean_control_without_the_flaw(self, running):
        response = running("/error")
        assert response.status == 200
        assert b"10.0.0.5" not in response.read()

    def test_serves_404_for_an_unpublished_path(self, running):
        with pytest.raises(Exception) as caught:
            running("/no-such-path")
        assert "404" in str(caught.value)


class TestRunner:
    def test_without_publish_it_describes_and_exits_clean(self):
        import subprocess

        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "run_live_lab.py")],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0
        assert "Re-run with --publish" in result.stdout
        assert "alpha-1" in result.stdout

    def test_unknown_service_is_refused_before_anything_is_published(self):
        import subprocess

        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "run_live_lab.py"),
             "--only", "alpha-1,nope"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode != 0
        assert "nope" in result.stderr


class TestProbeList:
    def test_probe_paths_are_fixed_absolute_paths(self):
        assert len(PROBE_PATHS) == 13
        assert len(set(PROBE_PATHS)) == 13
        assert all(path.startswith("/") for path in PROBE_PATHS)
        assert all(is_probe_path(path) for path in PROBE_PATHS)
        assert not is_probe_path("/etc/passwd")

    def test_collector_rejects_anything_that_is_not_a_plain_path(self):
        from collect.collector import Collector

        for bad in ("relative", "/", "/../etc/passwd", "/x?y=1", "/x#y", "http://x/"):
            with pytest.raises(ValueError):
                Collector(gate=None, transport=None, clock=None, probe_paths=(bad,))

    def test_collector_accepts_the_fixed_list(self):
        from collect.collector import Collector

        collector = Collector(gate=None, transport=None, clock=None, probe_paths=PROBE_PATHS)
        assert collector._probe_paths == PROBE_PATHS


class TestReport:
    def test_link_precision_is_a_rate_not_a_metric_record(self, tmp_path):
        """The first full live run died here, after collecting all 112 responses.

        Detector precision is a metric record with a ``value``; link precision is a
        plain float. The writer assumed both were the same and only a run that reached
        the correlation table ever executed the line.
        """
        result = {
            "live": {
                "lab_fingerprint": "deadbeef",
                "services": 8,
                "clusters": 2,
                "addresses": 8,
                "routes_observed": 112,
            },
            "detection": {
                "careful": {
                    "recall": {"value": 1.0},
                    "precision": {"value": 1.0},
                    "tp": 19, "fp": 0, "fn": 0,
                },
                "naive": {
                    "recall": {"value": 0.895},
                    "precision": {"value": 0.224},
                    "tp": 17, "fp": 59, "fn": 2,
                },
                "path_accuracy": {
                    "on_planted_path": 19, "expected": 19, "on_wrong_path": 0,
                },
            },
            "correlation": {
                "cap": 3,
                "naive": {
                    "ranking": {"precision@1": {"value": 1.0}},
                    "link_scoring": {
                        "precision": 0.545455, "proposed": 11, "tp": 6, "fp": 5,
                    },
                },
                "filtered": {
                    "ranking": {"precision@1": {"value": 1.0}},
                    "link_scoring": {
                        "precision": 1.0, "proposed": 6, "tp": 6, "fp": 0,
                    },
                },
                "singleton_abstention": {"filtered": 2, "naive": 0, "queries": 2},
            },
            "scope_limit": "live bytes, constructed truth",
        }

        _, md_path = write_report(result, tmp_path, case_id="CASE-REPORT")
        markdown = md_path.read_text(encoding="utf-8")
        assert "| naive | 100.0% | 54.5% | 11 | 6 | 5 |" in markdown
        assert "| filtered | 100.0% | 100.0% | 6 | 6 | 0 |" in markdown
        assert "Singleton abstention: filtered 2/2, naive 0/2." in markdown
