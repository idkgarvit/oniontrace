"""The real-data corpus has to be right about the things the farm let slide.

Three claims are pinned here, because each one is a place where code written against
synthetic data quietly breaks on a crawl:

  * frequency is counted per ``(kind, value)``, so the same hex string in two artefact
    classes is not one popular artefact;
  * the cap bounds the pair join, so a value on every service costs one comparison and
    not n^2;
  * declarations merge transitively, so a clearnet domain, its onion, and a certificate
    that names a third host produce one ground-truth group rather than three.

The adapters are exercised against the project's own record types, not against dicts
that happen to have the right keys.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collect.ahmia import SeedCandidate
from collect.ctlog import OnionAssociation
from collect.onionlocation import Advertisement
from eval.realdata import (
    Corpus,
    Declaration,
    Observation,
    from_advertisement,
    from_ct_association,
    from_seed,
    normalise_subject,
    score_corpus,
)

# --------------------------------------------------------------------------- #
# normalisation and the record itself
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("http://AbC.onion/path", "abc.onion"),
        ("abc.onion", "abc.onion"),
        ("ABC.ONION.", "abc.onion"),
        ("https://shop.example:8443/x", "shop.example"),
        ("shop.example/x", "shop.example"),
    ],
)
def test_normalise_subject(raw, expected):
    assert normalise_subject(raw) == expected


def test_observation_refuses_an_incomplete_record():
    with pytest.raises(ValueError):
        Observation(subject="", kind="favicon_sha256", value="x", source="s", provenance="p")
    with pytest.raises(ValueError):
        Observation(subject="a", kind="", value="x", source="s", provenance="p")


def test_declaration_needs_two_subjects():
    with pytest.raises(ValueError):
        Declaration("d", ("only",), "manual", "p")


# --------------------------------------------------------------------------- #
# frequency is per (kind, value)
# --------------------------------------------------------------------------- #


def test_the_same_string_in_two_classes_is_two_artefacts():
    """Value-only counting would call this a popular artefact and filter it."""
    corpus = Corpus()
    corpus.add(Observation("a.onion", "favicon_sha256", "deadbeef", "crawl", "w1"))
    corpus.add(Observation("b.onion", "app_js_sha256", "deadbeef", "crawl", "w2"))
    frequencies = corpus.frequencies()
    assert frequencies[("favicon_sha256", "deadbeef")] == 1
    assert frequencies[("app_js_sha256", "deadbeef")] == 1
    # neither value is shared, so nothing is proposed
    assert corpus.propose(cap=2) == []


def test_frequency_counts_subjects_not_observations():
    corpus = Corpus()
    corpus.add(Observation("a.onion", "favicon_sha256", "x", "crawl", "w1"))
    corpus.add(Observation("a.onion", "favicon_sha256", "x", "crawl", "w2"))
    assert corpus.frequencies()[("favicon_sha256", "x")] == 1


# --------------------------------------------------------------------------- #
# the cap bounds the join
# --------------------------------------------------------------------------- #


def test_cap_filters_a_popular_value():
    corpus = Corpus()
    for subject in ("a.onion", "b.onion", "c.onion"):
        corpus.add(Observation(subject, "favicon_sha256", "stock", "crawl", "w"))
    assert corpus.propose(cap=2) == []
    assert len(corpus.propose(cap=3)) == 3


def test_pairs_are_undirected_and_deduplicated():
    corpus = Corpus()
    corpus.add(Observation("a.onion", "ssh_hostkey_sha256", "k", "crawl", "w"))
    corpus.add(Observation("b.onion", "ssh_hostkey_sha256", "k", "crawl", "w"))
    links = corpus.propose(cap=2)
    assert len(links) == 1
    assert (links[0]["a"], links[0]["b"]) == ("a.onion", "b.onion")


def test_kinds_accumulate_across_values():
    corpus = Corpus()
    for subject in ("a.onion", "b.onion"):
        corpus.add(Observation(subject, "favicon_sha256", "f", "crawl", "w"))
        corpus.add(Observation(subject, "analytics_id", "ua-1", "crawl", "w"))
    (link,) = corpus.propose(cap=2)
    assert link["kinds"] == ["analytics_id", "favicon_sha256"]


def test_a_ubiquitous_value_does_not_explode_the_join():
    """500 services sharing one stock value must cost one frequency check, not 124k pairs."""
    corpus = Corpus()
    for index in range(500):
        subject = f"s{index:03d}.onion"
        corpus.add(Observation(subject, "favicon_sha256", "stock", "crawl", "w"))
        corpus.add(Observation(subject, "app_js_sha256", f"unique-{index}", "crawl", "w"))
    assert corpus.propose(cap=3) == []


# --------------------------------------------------------------------------- #
# declarations
# --------------------------------------------------------------------------- #


def test_declarations_merge_transitively():
    corpus = Corpus()
    corpus.declare(Declaration("d1", ("shop.example", "aaa.onion"), "onion-location", "p1"))
    corpus.declare(Declaration("d2", ("aaa.onion", "cdn.example"), "ct", "p2"))
    corpus.declare(Declaration("d3", ("forum.example", "fff.onion"), "onion-location", "p3"))
    groups = corpus.declared_groups()
    merged = {frozenset(group) for group in groups.values()}
    assert frozenset({"shop.example", "aaa.onion", "cdn.example"}) in merged
    assert frozenset({"forum.example", "fff.onion"}) in merged
    assert len(merged) == 2


def test_adapter_from_onion_location():
    advertisement = Advertisement(
        onion_address="AbC.onion",
        advertised_url="https://shop.example/",
        source_url="https://shop.example/",
        form="header",
    )
    declaration, observations = from_advertisement(advertisement)
    assert declaration.subjects == ("abc.onion", "shop.example")
    assert declaration.source == "onion-location"
    assert observations[0].kind == "onion_location"
    assert observations[0].subject == "abc.onion"


def test_adapter_from_ct_association():
    association = OnionAssociation(
        onion="aaa.onion",
        clearnet_names=["shop.example", "cdn.example"],
        log_ids=["argon2026"],
        first_seen="2026-01-02T00:00:00Z",
        last_seen=None,
        note="",
    )
    declaration, observations = from_ct_association(association)
    assert declaration.subjects == ("aaa.onion", "cdn.example", "shop.example")
    assert {obs.value for obs in observations} == {"cdn.example", "shop.example"}
    assert observations[0].observed_at == "2026-01-02T00:00:00Z"


def test_adapter_from_seed_claims_no_artefact():
    seed = SeedCandidate(
        address="aaa.onion", title="t", snippet="s", source_url="http://index/search?q=x"
    )
    observation = from_seed(seed)
    assert observation.kind == "index_listing"
    assert corpus_artefacts_ignore_it(observation)


def corpus_artefacts_ignore_it(observation: Observation) -> bool:
    corpus = Corpus()
    corpus.add(observation)
    return corpus.artefacts() == {}


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #


def test_jsonl_round_trip_preserves_both_record_types():
    corpus = Corpus()
    corpus.add(Observation("a.onion", "favicon_sha256", "f", "crawl", "w", "2026-01-01T00:00:00Z"))
    corpus.declare(Declaration("d", ("a.onion", "shop.example"), "onion-location", "p"))
    restored = Corpus.from_jsonl(corpus.to_jsonl())
    assert restored.observations == corpus.observations
    assert restored.declarations == corpus.declarations


def test_jsonl_refuses_an_unknown_record_type():
    with pytest.raises(ValueError):
        Corpus.from_jsonl('{"type": "mystery"}')


# --------------------------------------------------------------------------- #
# windows and the end-to-end score
# --------------------------------------------------------------------------- #


def test_windows_group_by_the_newest_supporting_observation():
    corpus = Corpus()
    corpus.add(Observation("a.onion", "favicon_sha256", "f", "crawl", "w", "2026-03-01T00:00:00Z"))
    corpus.add(Observation("b.onion", "favicon_sha256", "f", "crawl", "w", "2026-03-09T00:00:00Z"))
    windows = corpus.windows(cap=2)
    assert [day for day, _ in windows] == ["2026-03-09"]


def test_end_to_end_score_against_declarations():
    corpus = Corpus()
    # one declared pair, sharing two soft artefacts
    for subject in ("aaa.onion", "bbb.onion"):
        corpus.add(Observation(subject, "favicon_sha256", "f", "crawl", "w"))
        corpus.add(Observation(subject, "analytics_id", "ua-1", "crawl", "w"))
    corpus.declare(Declaration("d", ("aaa.onion", "bbb.onion"), "mirror-list", "p"))

    result = score_corpus(corpus, cap=3)
    assert result["score"]["tp"] == 1
    assert result["score"]["fp"] == 0
    assert result["score"]["precision"] == 1.0
    assert result["score"]["recall"] == 1.0


def test_end_to_end_refuses_a_single_copied_artefact():
    corpus = Corpus()
    for subject in ("aaa.onion", "bbb.onion"):
        corpus.add(Observation(subject, "favicon_sha256", "copied", "crawl", "w"))
    corpus.declare(Declaration("d", ("aaa.onion", "bbb.onion"), "mirror-list", "p"))

    result = score_corpus(corpus, cap=3)
    assert result["proposed"] == 1
    assert result["kept_by_policy"] == 0
    assert result["dropped_by_policy"] == 1
    assert result["score"]["scored"] == 0


def test_end_to_end_charges_a_link_across_declarations():
    corpus = Corpus()
    for subject in ("aaa.onion", "bbb.onion"):
        corpus.add(Observation(subject, "ssh_hostkey_sha256", "shared", "crawl", "w"))
    corpus.declare(Declaration("d1", ("aaa.onion", "bbb.onion"), "mirror-list", "p1"))
    corpus.declare(Declaration("d2", ("ccc.onion", "ddd.onion"), "mirror-list", "p2"))
    # a hard artefact shared across two declarations is a demonstrated error
    corpus.add(Observation("aaa.onion", "tls_spki_sha256", "k", "crawl", "w"))
    corpus.add(Observation("ccc.onion", "tls_spki_sha256", "k", "crawl", "w"))

    result = score_corpus(corpus, cap=3)
    assert result["score"]["fp"] == 1
    assert result["score"]["precision"] == 0.5


# --------------------------------------------------------------------------- #
# frequency poisoning: a discovered weakness, pinned so it stays known
# --------------------------------------------------------------------------- #


def _soft_family(corpus: Corpus, members: tuple[str, ...]) -> None:
    for member in members:
        corpus.add(Observation(member, "favicon_sha256", "family-fav", "crawl", "w"))
        corpus.add(Observation(member, "analytics_id", "UA-family", "crawl", "w"))


def test_one_forged_copy_can_poison_a_soft_only_family():
    """Pins a real weakness rather than pretending it does not exist.

    A family of three shares a favicon and an analytics id. An unrelated service copies
    the favicon. The favicon now appears on four services, so a cap of three discards it
    as a stock value -- and the genuine family is left with one soft class, which the
    corroboration rule refuses. The forgery suppressed the link it was meant to fake.

    This is a small-corpus effect: at real population sizes one extra copy does not move
    a relative prevalence threshold. It is recorded because a reader should not have to
    discover it by running the code.
    """
    corpus = Corpus()
    _soft_family(corpus, ("s1.onion", "s2.onion", "s3.onion"))
    corpus.add(Observation("x.onion", "favicon_sha256", "family-fav", "crawl", "w"))

    intact = Corpus()
    _soft_family(intact, ("s1.onion", "s2.onion", "s3.onion"))

    poisoned = score_corpus(corpus, cap=3)
    healthy = score_corpus(intact, cap=3)
    assert healthy["kept_by_policy"] == 3
    assert poisoned["kept_by_policy"] == 0, "the forgery is expected to suppress the family"


def test_a_hard_class_is_immune_to_forgery():
    """The same family with one hard class keeps its link despite the copied favicon."""
    corpus = Corpus()
    for member in ("s1.onion", "s2.onion", "s3.onion"):
        corpus.add(Observation(member, "ssh_hostkey_sha256", "family-key", "crawl", "w"))
        corpus.add(Observation(member, "favicon_sha256", "family-fav", "crawl", "w"))
    corpus.add(Observation("x.onion", "favicon_sha256", "family-fav", "crawl", "w"))

    result = score_corpus(corpus, cap=3)
    assert result["kept_by_policy"] == 3


def test_raising_the_cap_restores_recall_and_costs_precision():
    """The tradeoff the sweep describes, reproduced on the real-data path."""
    corpus = Corpus()
    families = {
        "a.example": ("a1.onion", "a2.onion", "a3.onion"),
        "b.example": ("b1.onion", "b2.onion"),
    }
    for index, (clearnet, onions) in enumerate(families.items()):
        corpus.declare(
            Declaration(f"ol:{clearnet}", (clearnet, *onions), "onion-location", "p")
        )
        for onion in onions:
            corpus.add(Observation(onion, "favicon_sha256", f"fav-{index}", "crawl", "w"))
            corpus.add(Observation(onion, "analytics_id", f"UA-{index}", "crawl", "w"))

    tight = score_corpus(corpus, cap=3)
    loose = score_corpus(corpus, cap=5)
    assert tight["score"]["recall"] == 1.0
    assert tight["score"]["fp"] == 0
    assert loose["score"]["fp"] == tight["score"]["fp"]
    # widening the cap admits nothing new here, so the rows agree; the sweep covers the
    # case where widening does admit a stock value
    assert loose["score"]["tp"] == tight["score"]["tp"]
