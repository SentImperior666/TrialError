"""``trialerror.lexicon.scan`` -- the disjoint-source conflict rule.

The rule is a claim about *evidence*, not about text, so every test here
builds real sources behind the senses it compares. ``_reading`` mints a
source, a document and an anchor under it, and proposes a reading grounded
in that anchor -- which is the only way to get two senses whose
``source_key`` sets genuinely differ.

What is asserted, in the order the module docstring states it:

* two readings on disjoint sources -> exactly one term-scoped item;
* two readings that share a source -> nothing, and the store says why;
* the mixed case -> one item over the senses that take part in a disjoint
  pair, with the overlapping pair recorded rather than dropped;
* nothing the scan writes is ever a decision, and nothing it writes twice.
"""

from __future__ import annotations

import json

import pytest

from trialerror.lexicon import api, policy, scan
from trialerror.stores import insert
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests._store_fixtures import populate_one_of_everything


@pytest.fixture()
def ids(store):
    return populate_one_of_everything(store)


def _source_with_anchor(store, ids, label: str) -> tuple[str, str]:
    """A source, a document under it and an anchor into that document --
    the shortest real path to an evidence row with its own ``source_key``."""
    source_id = new_id("SRC")
    insert(
        store,
        "source",
        {
            "source_id": source_id,
            "kind": "report",
            "title": f"handbook {label}",
            "license_tier": "open",
            "acquisition_route": "web",
            "request_state": "indexed",
            "registered_ts": now(),
            "registered_by_launch": ids["launch"],
        },
    )
    doc_id = new_id("DOC")
    insert(
        store,
        "document",
        {
            "doc_id": doc_id,
            "source_id": source_id,
            "rel_path": f"archive/{label}.md",
            "media_type": "pdf",
            "normalizer_id": "pdf-text",
            "normalizer_version": "1",
            "sha256": (label * 64)[:64],
            "status": "indexed",
        },
    )
    anchor_id = new_id("ANC")
    insert(
        store,
        "quote_anchor",
        {
            "anchor_id": anchor_id,
            "doc_id": doc_id,
            "char_start": 0,
            "char_end": 5,
            "doc_sha256": (label * 64)[:64],
            "quote_sha256": (label * 64)[:64],
            "quote_text": f"text from {label}",
            "created_by_launch": ids["launch"],
            "created_ts": now(),
        },
    )
    return source_id, anchor_id


def _reading(store, ids, lemma, anchor_id, gloss):
    """One CURRENT reading of ``lemma`` grounded in ``anchor_id``."""
    proposed = api.propose(
        store,
        lemma=lemma,
        gloss=gloss,
        origin_kind="manual",
        origin_ref=None,
        evidence=[f"anchor:{anchor_id}"],
        by_launch=ids["launch"],
        procedure_version=policy.MANUAL_PROCEDURE_VERSION,
    )
    api.accept_sense(store, proposed["sense_id"], by_launch=ids["launch"])
    return proposed


def _conflicts(store):
    return [
        dict(r)
        for r in store.knowledge.execute(
            "SELECT * FROM term_relation WHERE verb = 'conflicts_with' ORDER BY marked_ts, rel_id"
        ).fetchall()
    ]


# ---------------------------------------------------------------------------
# the rule
# ---------------------------------------------------------------------------


def test_two_readings_on_disjoint_sources_raise_one_term_scoped_item(store, ids):
    source_a, anchor_a = _source_with_anchor(store, ids, "a")
    source_b, anchor_b = _source_with_anchor(store, ids, "b")
    first = _reading(store, ids, "settling time", anchor_a, "how long the reading takes to stop moving")
    second = _reading(store, ids, "settling time", anchor_b, "the interval between two consecutive passes")

    rows = [r for r in _conflicts(store) if r["src_id"] == first["term_id"]]
    assert len(rows) == 1, "one queue item for the term, not one per pair"
    row = rows[0]
    assert row["src_kind"] == "term" and row["dst_kind"] == "term"
    assert row["src_id"] == row["dst_id"] == first["term_id"]
    assert row["status"] == "pending"
    assert row["marked_by_kind"] == "system" and row["marked_by_launch"] is None
    assert row["marked_by_model"] == policy.SYSTEM_SCAN_MODEL

    evidence = json.loads(row["evidence"])
    assert evidence["sense_ids"] == [first["sense_id"], second["sense_id"]]
    assert evidence["source_sets"] == {
        first["sense_id"]: [source_a],
        second["sense_id"]: [source_b],
    }
    assert evidence["shared_sources"] == []


def test_two_readings_that_share_a_source_are_nuance_and_raise_nothing(store, ids):
    _source_a, anchor_a = _source_with_anchor(store, ids, "a")
    first = _reading(store, ids, "settling time", anchor_a, "how long the reading takes to stop moving")
    second = _reading(store, ids, "settling time", anchor_a, "the same idea, said again in other words")

    outcome = scan.conflicts_for_term(store, first["term_id"])
    assert outcome["opened"] is None
    assert "nuance" in outcome["reason"]
    assert outcome["shared_sources"] == [
        {"sense_ids": [first["sense_id"], second["sense_id"]], "shared": [outcome["source_sets"][first["sense_id"]][0]]}
    ]
    assert _conflicts(store) == [
        r for r in _conflicts(store) if r["src_id"] != first["term_id"]
    ], "nothing was opened for this term"


def test_the_mixed_case_raises_the_disagreement_and_records_the_overlap(store, ids):
    """Two readings out of one register and a third out of another is a real
    cross-source disagreement. The strict "every pair must be disjoint"
    reading would have dropped it on the floor; the overlapping pair is
    recorded in the item instead."""
    source_a, anchor_a = _source_with_anchor(store, ids, "a")
    source_b, anchor_b = _source_with_anchor(store, ids, "b")
    first = _reading(store, ids, "settling time", anchor_a, "how long the reading takes to stop moving")
    second = _reading(store, ids, "settling time", anchor_a, "the same idea, said again in other words")
    third = _reading(store, ids, "settling time", anchor_b, "the interval between two consecutive passes")

    rows = [r for r in _conflicts(store) if r["src_id"] == first["term_id"]]
    assert len(rows) == 1
    evidence = json.loads(rows[0]["evidence"])
    assert evidence["sense_ids"] == [first["sense_id"], second["sense_id"], third["sense_id"]]
    assert evidence["shared_sources"] == [
        {"sense_ids": [first["sense_id"], second["sense_id"]], "shared": [source_a]}
    ]
    assert evidence["source_sets"][third["sense_id"]] == [source_b]


def test_one_reading_is_not_a_disagreement(store, ids):
    _source_a, anchor_a = _source_with_anchor(store, ids, "a")
    first = _reading(store, ids, "settling time", anchor_a, "how long the reading takes to stop moving")
    outcome = scan.conflicts_for_term(store, first["term_id"])
    assert outcome["opened"] is None
    assert "fewer than two" in outcome["reason"]


def test_a_reading_that_is_not_current_does_not_count(store, ids):
    """A proposal is not yet one of the program's readings, so it cannot
    disagree with one."""
    _source_a, anchor_a = _source_with_anchor(store, ids, "a")
    _source_b, anchor_b = _source_with_anchor(store, ids, "b")
    first = _reading(store, ids, "settling time", anchor_a, "how long the reading takes to stop moving")
    api.propose(
        store,
        lemma="settling time",
        gloss="a reading nobody has accepted yet",
        origin_kind="manual",
        origin_ref=None,
        evidence=[f"anchor:{anchor_b}"],
        by_launch=ids["launch"],
        procedure_version=policy.MANUAL_PROCEDURE_VERSION,
    )
    outcome = scan.conflicts_for_term(store, first["term_id"])
    assert outcome["opened"] is None
    assert [r for r in _conflicts(store) if r["src_id"] == first["term_id"]] == []


def test_an_ungrounded_reading_takes_part_in_nothing(store, ids):
    """The empty set is disjoint from everything, so a sense whose evidence
    was all retracted would otherwise manufacture a conflict with every
    sibling it has."""
    _source_a, anchor_a = _source_with_anchor(store, ids, "a")
    first = _reading(store, ids, "settling time", anchor_a, "how long the reading takes to stop moving")
    _source_b, anchor_b = _source_with_anchor(store, ids, "b")
    second = _reading(store, ids, "settling time", anchor_b, "the interval between two consecutive passes")
    rel_id = [r for r in _conflicts(store) if r["src_id"] == first["term_id"]][0]["rel_id"]
    api.decide_relation(store, rel_id, decision="rejected", by_launch=ids["launch"])

    # Written around the API on purpose: ``retract_evidence`` refuses to
    # leave a live reading ungrounded (fix pass, finding F1), so the only way
    # into this state is a row somebody wrote directly -- which is exactly the
    # case this branch of the scan is a backstop for.
    with store.knowledge:
        store.knowledge.execute(
            "UPDATE term_sense_evidence SET retracted_ts = ?, retracted_reason = ? WHERE sense_id = ?",
            (now(), "wrong page", second["sense_id"]),
        )

    outcome = scan.conflicts_for_term(store, first["term_id"])
    assert outcome["ungrounded"] == [second["sense_id"]]
    assert outcome["opened"] is None
    assert "fewer than two" in outcome["reason"]


# ---------------------------------------------------------------------------
# idempotence, and not re-asking an answered question
# ---------------------------------------------------------------------------


def test_a_second_scan_of_the_same_member_set_writes_nothing(store, ids):
    _source_a, anchor_a = _source_with_anchor(store, ids, "a")
    _source_b, anchor_b = _source_with_anchor(store, ids, "b")
    first = _reading(store, ids, "settling time", anchor_a, "how long the reading takes to stop moving")
    _reading(store, ids, "settling time", anchor_b, "the interval between two consecutive passes")
    before = len(_conflicts(store))

    outcome = scan.conflicts_for_term(store, first["term_id"])
    assert outcome["opened"] is None
    assert outcome["existing"]["status"] == "pending"
    assert len(_conflicts(store)) == before


def test_a_rejected_conflict_is_not_reopened(store, ids):
    _source_a, anchor_a = _source_with_anchor(store, ids, "a")
    _source_b, anchor_b = _source_with_anchor(store, ids, "b")
    first = _reading(store, ids, "settling time", anchor_a, "how long the reading takes to stop moving")
    _reading(store, ids, "settling time", anchor_b, "the interval between two consecutive passes")
    rel_id = [r for r in _conflicts(store) if r["src_id"] == first["term_id"]][0]["rel_id"]
    api.decide_relation(store, rel_id, decision="rejected", by_launch=ids["launch"])

    outcome = scan.conflicts_for_term(store, first["term_id"])
    assert outcome["opened"] is None
    assert outcome["existing"]["rel_id"] == rel_id
    assert outcome["existing"]["status"] == "rejected"


def test_a_third_reading_is_a_new_member_set_and_does_get_raised(store, ids):
    """Idempotence is per member set, not per term: a decided item covers
    the readings it covered, and a reading that arrives afterwards has not
    been adjudicated against anything."""
    _source_a, anchor_a = _source_with_anchor(store, ids, "a")
    _source_b, anchor_b = _source_with_anchor(store, ids, "b")
    _source_c, anchor_c = _source_with_anchor(store, ids, "c")
    first = _reading(store, ids, "settling time", anchor_a, "how long the reading takes to stop moving")
    _reading(store, ids, "settling time", anchor_b, "the interval between two consecutive passes")
    rel_id = [r for r in _conflicts(store) if r["src_id"] == first["term_id"]][0]["rel_id"]
    api.decide_relation(store, rel_id, decision="rejected", by_launch=ids["launch"])

    _reading(store, ids, "settling time", anchor_c, "a third handbook's reading of the same name")
    rows = [r for r in _conflicts(store) if r["src_id"] == first["term_id"]]
    assert len(rows) == 2
    assert [r["status"] for r in rows] == ["rejected", "pending"]


def test_an_unknown_term_is_reported_not_raised(store, ids):
    outcome = scan.conflicts_for_term(store, "TERM-does-not-exist")
    assert outcome["status"] == "unknown_term"
    assert outcome["opened"] is None


# ---------------------------------------------------------------------------
# the whole-store pass
# ---------------------------------------------------------------------------


def test_scan_terms_finds_what_the_per_term_scan_would(store, ids):
    _source_a, anchor_a = _source_with_anchor(store, ids, "a")
    _source_b, anchor_b = _source_with_anchor(store, ids, "b")
    first = _reading(store, ids, "settling time", anchor_a, "how long the reading takes to stop moving")
    _reading(store, ids, "settling time", anchor_b, "the interval between two consecutive passes")
    # wipe the item the accept-time scan already raised, to prove the
    # whole-store pass finds it on its own rather than only reporting it
    store.knowledge.execute("DELETE FROM term_relation WHERE verb = 'conflicts_with'")
    store.knowledge.commit()

    out = scan.scan_terms(store, duplicates=False)
    assert out["conflicts_opened"] == 1
    assert out["conflicts"][0]["term_id"] == first["term_id"]
    assert out["terms_scanned"] >= 1


def test_scan_terms_is_idempotent(store, ids):
    _source_a, anchor_a = _source_with_anchor(store, ids, "a")
    _source_b, anchor_b = _source_with_anchor(store, ids, "b")
    _reading(store, ids, "settling time", anchor_a, "how long the reading takes to stop moving")
    _reading(store, ids, "settling time", anchor_b, "the interval between two consecutive passes")
    scan.scan_terms(store)
    before = store.knowledge.execute("SELECT count(*) FROM term_relation").fetchone()[0]

    again = scan.scan_terms(store)
    assert again["conflicts_opened"] == 0
    assert again["duplicates_opened"] == 0
    assert store.knowledge.execute("SELECT count(*) FROM term_relation").fetchone()[0] == before


def test_scan_terms_skips_a_merged_term(store, ids):
    """Its readings belong to the term it was folded into now, and an item
    raised under the old id would name a lemma that no longer resolves to
    it."""
    _source_a, anchor_a = _source_with_anchor(store, ids, "a")
    _source_b, anchor_b = _source_with_anchor(store, ids, "b")
    keep = _reading(store, ids, "settling time", anchor_a, "how long the reading takes to stop moving")
    other = _reading(store, ids, "settling period", anchor_b, "the interval between two consecutive passes")
    api.merge_terms(store, keep["term_id"], other["term_id"], by_launch=ids["launch"])
    store.knowledge.execute("DELETE FROM term_relation")
    store.knowledge.commit()

    out = scan.scan_terms(store, duplicates=False)
    assert all(c["term_id"] != other["term_id"] for c in out["conflicts"])


def test_source_sets_for_term_reports_the_sets_without_the_verdict(store, ids):
    source_a, anchor_a = _source_with_anchor(store, ids, "a")
    source_b, anchor_b = _source_with_anchor(store, ids, "b")
    first = _reading(store, ids, "settling time", anchor_a, "how long the reading takes to stop moving")
    second = _reading(store, ids, "settling time", anchor_b, "the interval between two consecutive passes")
    assert scan.source_sets_for_term(store, first["term_id"]) == {
        first["sense_id"]: [source_a],
        second["sense_id"]: [source_b],
    }


def test_the_grouped_reads_seed_from_the_ids_they_are_GIVEN(store, ids):
    """Verify V-6: the empty-case promise ("no current sense -> {}", "no
    blocking row -> []") is a property of the NAMED-subset form, which
    pre-seeds from ``term_ids``. The whole-store form has no such list and
    carries no key for a term that contributed no row -- which is why every
    caller in the module passes ids and every consumer reads with a
    default."""
    _source, anchor = _source_with_anchor(store, ids, "d")
    proposed = api.propose(
        store,
        lemma="dwell angle",
        gloss="the span over which the reading is held",
        origin_kind="manual",
        origin_ref=None,
        evidence=[f"anchor:{anchor}"],
        by_launch=ids["launch"],
        procedure_version=policy.MANUAL_PROCEDURE_VERSION,
    )
    term_id = proposed["term_id"]  # a term whose only sense is not current

    assert scan.source_sets_for_terms(store, [term_id])[term_id] == {}
    assert term_id not in scan.source_sets_for_terms(store)

    assert scan.blocking_conflicts_by_term(store, [term_id])[term_id] == []
    assert term_id not in scan.blocking_conflicts_by_term(store)
