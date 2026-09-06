"""Mining adoption engram-F4: save-time conflict-candidate surfacing with
a locked verb taxonomy (``docs/reviews/MINING_2026-09_OPERATOR_LINKS.md``
section 3, orchestrator verdict "adopt-now:memory WITH CONSTRAINT --
advisory candidates only, never auto-applied verdicts").

The constraint gets more test weight than the feature does, on purpose.
Section 5.3 is the reason this adoption is half of what the source ships,
and a port that quietly regained ``JudgeBySemantic`` would look like a
harmless refactor to a future reader -- so "a machine cannot write a
verdict" and "an advisory cannot fail a save" are each pinned here.
"""

from __future__ import annotations

import pytest

from trialerror.memory import conflicts
from trialerror.memory.api import put_item
from trialerror.memory.conflicts import (
    RELATION_VERBS,
    candidates_for,
    judge,
    list_candidates,
    record_candidates,
    render_note,
)

from tests._memory_fixtures import make_account


def _seed(store, account_id):
    put_item(
        store,
        key="spawn-booking-rule",
        tier="L0",
        kind="rule",
        body="Every agent run is booked in the spend ledger before spawn.",
        account_id=account_id,
        l0_abstract="book every launch before spawn",
    )
    put_item(
        store,
        key="ocr-hardware-note",
        tier="L1",
        kind="fact",
        body="Marker OCR runs on the GPU only; CPU fallback is disabled.",
        account_id=account_id,
        l0_abstract="OCR is GPU-only",
    )


# ---------------------------------------------------------------------------
# ranking
# ---------------------------------------------------------------------------


def test_candidates_surface_a_topically_close_item(store):
    account_id = make_account(store)
    _seed(store, account_id)
    found = candidates_for(
        store,
        key="launch-booking-discipline",
        body="A launch that was not booked before spawn is a dangling launch.",
        l0_abstract="booking before spawn is mandatory",
        account_id=account_id,
    )
    assert [c["key"] for c in found][:1] == ["spawn-booking-rule"]


def test_candidates_are_empty_when_nothing_shares_vocabulary(store):
    account_id = make_account(store)
    _seed(store, account_id)
    found = candidates_for(
        store, key="zzz-unrelated", body="quartz sundial", l0_abstract="quartz sundial", account_id=account_id
    )
    assert found == []


def test_candidates_never_include_the_item_being_saved(store):
    account_id = make_account(store)
    row = put_item(store, key="self", tier="L0", kind="rule", body="a rule about spawning", account_id=account_id)
    found = candidates_for(
        store, key="self", body="a rule about spawning", account_id=account_id, exclude_id=row["memory_item_id"]
    )
    assert all(c["target_id"] != row["memory_item_id"] for c in found)


def test_candidates_skip_the_same_key_because_that_is_an_ordinary_edit(store):
    """A same-key save is the upsert's business. Reporting every ordinary
    edit as a possible contradiction would train the operator to ignore
    the advisory entirely."""
    account_id = make_account(store)
    put_item(store, key="topic", tier="L0", kind="rule", body="booking before spawn", account_id=account_id)
    found = candidates_for(store, key="topic", body="booking before spawn, revised", account_id=account_id)
    assert found == []


def test_candidates_are_scoped_to_one_account(store):
    a = make_account(store, label="a")
    b = make_account(store, label="b")
    put_item(store, key="a-rule", tier="L0", kind="rule", body="booking before spawn", account_id=a)
    found = candidates_for(store, key="b-rule", body="booking before spawn", account_id=b)
    assert found == []


def test_candidate_ranking_is_deterministic(store):
    account_id = make_account(store)
    _seed(store, account_id)
    first = candidates_for(store, key="k", body="spawn ledger booking", l0_abstract="spawn booking", account_id=account_id)
    second = candidates_for(store, key="k", body="spawn ledger booking", l0_abstract="spawn booking", account_id=account_id)
    assert first == second


def test_bm25_idf_is_never_negative_on_a_tiny_corpus(store):
    """The unsmoothed classic IDF goes negative for a term in more than
    half the corpus -- on eight memory items that is the common case, and
    a negative score would silently drop the most obviously related item.
    Every returned score must be positive."""
    account_id = make_account(store)
    for i in range(3):
        put_item(store, key=f"shared-{i}", tier="L0", kind="rule", body="booking spawn ledger", account_id=account_id)
    found = candidates_for(store, key="another-booking-spawn-note", body="booking spawn ledger", account_id=account_id)
    assert found and all(c["score"] > 0 for c in found)


# ---------------------------------------------------------------------------
# persistence: pending, and only pending
# ---------------------------------------------------------------------------


def test_put_item_records_candidates_as_pending_rows_with_no_verb(store):
    account_id = make_account(store)
    _seed(store, account_id)
    row = put_item(
        store,
        key="launch-booking-discipline",
        tier="L0",
        kind="rule",
        body="A launch not booked before spawn is a dangling launch.",
        account_id=account_id,
    )
    assert row["conflict_candidates"]
    rows = list_candidates(store, source_id=row["memory_item_id"])
    assert rows
    for r in rows:
        assert r["judgment_status"] == "pending"
        assert r["relation"] is None
        assert r["marked_by_actor"] is None
        assert r["marked_by_kind"] is None


def test_put_item_returns_a_rendered_note(store):
    account_id = make_account(store)
    _seed(store, account_id)
    row = put_item(
        store, key="spawn-booking-again", tier="L0", kind="rule",
        body="booked in the spend ledger before spawn", account_id=account_id,
    )
    assert "ADVISORY (unjudged)" in row["conflict_note"]
    assert "spawn-booking-rule" in row["conflict_note"]


def test_no_candidates_means_no_note(store):
    account_id = make_account(store)
    row = put_item(store, key="lonely", tier="L0", kind="rule", body="the only item", account_id=account_id)
    assert row["conflict_candidates"] == []
    assert row["conflict_note"] is None


def test_re_saving_does_not_duplicate_a_pending_candidate(store):
    account_id = make_account(store)
    _seed(store, account_id)
    for body in ("booked before spawn", "booked before spawn, v2", "booked before spawn, v3"):
        row = put_item(store, key="spawn-booking-repeat", tier="L0", kind="rule", body=body, account_id=account_id)
    rows = list_candidates(store, source_id=row["memory_item_id"], status=None)
    targets = [r["target_id"] for r in rows]
    assert len(targets) == len(set(targets))


def test_an_idempotent_no_op_save_still_reports_the_standing_advisory(store):
    account_id = make_account(store)
    _seed(store, account_id)
    first = put_item(store, key="spawn-booking-repeat", tier="L0", kind="rule", body="booked before spawn", account_id=account_id)
    second = put_item(store, key="spawn-booking-repeat", tier="L0", kind="rule", body="booked before spawn", account_id=account_id)
    assert second["updated_ts"] == first["updated_ts"]  # still a true no-op
    assert [c["target_id"] for c in second["conflict_candidates"]] == [
        c["target_id"] for c in first["conflict_candidates"]
    ]


def test_a_judged_pair_is_never_resurrected_as_a_new_candidate(store):
    """A human who ruled ``not_conflict`` must not be asked again on the
    next save."""
    account_id = make_account(store)
    _seed(store, account_id)
    row = put_item(store, key="spawn-booking-repeat", tier="L0", kind="rule", body="booked before spawn", account_id=account_id)
    rel = list_candidates(store, source_id=row["memory_item_id"])[0]
    judge(store, relation_id=rel["relation_id"], relation="not_conflict", actor="operator", actor_kind="human")

    put_item(store, key="spawn-booking-repeat", tier="L0", kind="rule", body="booked before spawn, edited", account_id=account_id)
    still = list_candidates(store, source_id=row["memory_item_id"], status=None)
    assert [r["relation_id"] for r in still].count(rel["relation_id"]) == 1
    assert len({r["target_id"] for r in still}) == len(still)


def test_surface_conflicts_false_skips_the_scan_entirely(store):
    account_id = make_account(store)
    _seed(store, account_id)
    row = put_item(
        store, key="spawn-booking-quiet", tier="L0", kind="rule", body="booked before spawn",
        account_id=account_id, surface_conflicts=False,
    )
    assert row["conflict_candidates"] == []
    assert list_candidates(store, source_id=row["memory_item_id"]) == []


def test_an_advisory_failure_never_fails_the_save(store, monkeypatch):
    """The write has already happened when the scan runs. A session must
    not lose the lesson it was recording because a ranking query tripped."""
    account_id = make_account(store)

    def _boom(*a, **kw):
        raise RuntimeError("ranking exploded")

    monkeypatch.setattr(conflicts, "scan_and_record", _boom)
    row = put_item(store, key="resilient", tier="L0", kind="rule", body="still saved", account_id=account_id)
    assert row["conflict_scan_error"].startswith("RuntimeError:")
    stored = store.ops.execute(
        "SELECT body FROM memory_item WHERE memory_item_id = ?", (row["memory_item_id"],)
    ).fetchone()
    assert stored["body"] == "still saved"


# ---------------------------------------------------------------------------
# the locked taxonomy and section 5.3's constraint
# ---------------------------------------------------------------------------


def test_the_verb_taxonomy_is_exactly_the_sources_six(store):
    assert RELATION_VERBS == (
        "related", "compatible", "scoped", "conflicts_with", "supersedes", "not_conflict",
    )


def test_judge_records_a_verdict_with_provenance(store):
    account_id = make_account(store)
    _seed(store, account_id)
    row = put_item(store, key="j", tier="L0", kind="rule", body="booked before spawn", account_id=account_id)
    rel = list_candidates(store, source_id=row["memory_item_id"])[0]
    judged = judge(
        store, relation_id=rel["relation_id"], relation="conflicts_with",
        actor="lens-3", actor_kind="agent", model="opus-5", confidence=0.8, reason="opposite claims",
    )
    assert judged["judgment_status"] == "judged"
    assert judged["relation"] == "conflicts_with"
    assert (judged["marked_by_actor"], judged["marked_by_kind"], judged["marked_by_model"]) == (
        "lens-3", "agent", "opus-5",
    )
    assert judged["judged_ts"]


def test_judge_refuses_a_system_actor(store):
    """review section 5.3: the source's ``JudgeBySemantic`` pre-populates
    verdicts with ``marked_by_kind='system'``. That is a silent auto-merge
    by another name, and this port refuses it structurally -- a machine
    that wants to rule does so as a NAMED agent, leaving a trail."""
    account_id = make_account(store)
    _seed(store, account_id)
    row = put_item(store, key="j", tier="L0", kind="rule", body="booked before spawn", account_id=account_id)
    rel = list_candidates(store, source_id=row["memory_item_id"])[0]
    with pytest.raises(ValueError, match="actor_kind='system' is refused"):
        judge(store, relation_id=rel["relation_id"], relation="not_conflict", actor="semantic-pass", actor_kind="system")
    assert list_candidates(store, source_id=row["memory_item_id"])[0]["judgment_status"] == "pending"


def test_judge_refuses_a_verb_outside_the_taxonomy(store):
    account_id = make_account(store)
    _seed(store, account_id)
    row = put_item(store, key="j", tier="L0", kind="rule", body="booked before spawn", account_id=account_id)
    rel = list_candidates(store, source_id=row["memory_item_id"])[0]
    with pytest.raises(ValueError, match="relation must be one of"):
        judge(store, relation_id=rel["relation_id"], relation="probably_fine", actor="op", actor_kind="human")


def test_judge_refuses_an_anonymous_actor(store):
    account_id = make_account(store)
    _seed(store, account_id)
    row = put_item(store, key="j", tier="L0", kind="rule", body="booked before spawn", account_id=account_id)
    rel = list_candidates(store, source_id=row["memory_item_id"])[0]
    with pytest.raises(ValueError, match="actor is required"):
        judge(store, relation_id=rel["relation_id"], relation="related", actor="  ", actor_kind="human")


def test_judge_refuses_to_edit_a_settled_verdict(store):
    account_id = make_account(store)
    _seed(store, account_id)
    row = put_item(store, key="j", tier="L0", kind="rule", body="booked before spawn", account_id=account_id)
    rel = list_candidates(store, source_id=row["memory_item_id"])[0]
    judge(store, relation_id=rel["relation_id"], relation="related", actor="op", actor_kind="human")
    with pytest.raises(ValueError, match="already 'judged'"):
        judge(store, relation_id=rel["relation_id"], relation="not_conflict", actor="op2", actor_kind="human")


def test_judge_refuses_an_unknown_relation_id(store):
    with pytest.raises(ValueError, match="no memory_relation"):
        judge(store, relation_id="MREL-nope", relation="related", actor="op", actor_kind="human")


def test_the_schema_allows_two_actors_to_disagree_about_one_pair(store):
    """The source's own schema comment: "multi-actor disagreement allowed"
    -- no UNIQUE(source_id, target_id). A second judge's opposite verdict
    is a second row, not an overwrite of the first."""
    account_id = make_account(store)
    _seed(store, account_id)
    row = put_item(store, key="j", tier="L0", kind="rule", body="booked before spawn", account_id=account_id)
    rel = list_candidates(store, source_id=row["memory_item_id"])[0]
    judge(store, relation_id=rel["relation_id"], relation="conflicts_with", actor="op", actor_kind="human")

    second = record_candidates(
        store,
        source_id=row["memory_item_id"],
        candidates=[{"target_id": rel["target_id"], "score": 1.0}],
    )
    assert second == []  # the ordinary scan path will not re-raise a judged pair

    # ... but the row model itself holds a second, contradicting verdict.
    import sqlite3

    from trialerror.stores import insert
    from trialerror.util.ids import new_id
    from trialerror.util.timeutil import now

    try:
        insert(
            store,
            "memory_relation",
            {
                "relation_id": new_id("MREL"),
                "source_id": row["memory_item_id"],
                "target_id": rel["target_id"],
                "relation": "not_conflict",
                "judgment_status": "judged",
                "marked_by_actor": "second-opinion",
                "marked_by_kind": "human",
                "created_ts": now(),
                "judged_ts": now(),
            },
        )
    except sqlite3.IntegrityError:  # pragma: no cover - would mean a UNIQUE crept in
        pytest.fail("memory_relation must permit two verdicts about the same pair")

    both = list_candidates(store, source_id=row["memory_item_id"], status="judged")
    assert {r["relation"] for r in both} == {"conflicts_with", "not_conflict"}


def test_render_note_is_none_for_no_candidates():
    assert render_note("k", []) is None


def test_render_note_says_nothing_was_blocked():
    note = render_note("k", [{"key": "other", "tier": "L0", "kind": "rule", "score": 1.0, "l0_abstract": "x"}])
    assert "Nothing was blocked, changed, or decided" in note


def test_an_empty_query_falls_back_to_the_body(store):
    """A slug-only key with no abstract would otherwise produce an EMPTY
    BM25 query and silently surface nothing at all -- the one place this
    port needed a fallback the source does not (engram's titles are
    sentences; this schema's keys are slugs)."""
    account_id = make_account(store)
    _seed(store, account_id)
    found = candidates_for(store, key="x", body="booked in the spend ledger before spawn", account_id=account_id)
    assert [c["key"] for c in found][:1] == ["spawn-booking-rule"]
