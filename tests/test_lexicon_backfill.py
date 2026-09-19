"""``trialerror.lexicon.backfill`` -- the register import, the claim
projection, and the source relink.

The register half is design §9's acceptance criterion B, run against
``tests._lexicon_fixtures``: 60 synthetic records over four register keys,
two lemmas recurring under a second register. Exact counts, exactly two
conflict items, and a second run that inserts nothing at all -- the last of
those being the property that makes a 7,000-row import safe to re-run after
an interruption.

The relink half is ruling L-E6's guarantee, asserted the only way it means
anything: a full-row snapshot before and after, with ``source_key`` the one
column allowed to differ.
"""

from __future__ import annotations

import json

import pytest

from trialerror.ingest.extract import EXTRACT_REGISTER_KEY
from trialerror.lexicon import api, backfill, policy
from trialerror.lexicon.errors import InvalidTermInputError, LaunchRequiredError
from trialerror.stores import insert
from trialerror.stores.errors import ValidationError, XidTargetMissingError
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests._lexicon_fixtures import (
    FAMILY_MAP,
    LEMMAS,
    RECORD_COUNT,
    REGISTER_KEYS,
    SHARED_LEMMAS,
    install_oversized_gloss_records,
    install_records,
)
from tests._store_fixtures import populate_one_of_everything


@pytest.fixture()
def ids(store):
    return populate_one_of_everything(store)


@pytest.fixture()
def records(store, ids):
    return install_records(store)


def _run(store, ids, **overrides):
    kwargs = {"by_launch": ids["launch"], "register_keys": REGISTER_KEYS}
    kwargs.update(overrides)
    return backfill.backfill_records(store, **kwargs)


def _count(store, sql, *params):
    return store.knowledge.execute(sql, params).fetchone()[0]


def _pending_conflicts(store, ids):
    """Pending conflict items MINUS the one ``populate_one_of_everything``
    inserts by hand -- that builder puts a row in every lexicon table, so
    every count here scopes itself past it rather than counting the table."""
    return _count(
        store,
        "SELECT count(*) FROM term_relation WHERE verb = 'conflicts_with' AND status = 'pending' "
        "AND rel_id != ?",
        ids["term_relation"],
    )


# ---------------------------------------------------------------------------
# fix pass F3 -- a raced row is a refusal, not the end of the run
# ---------------------------------------------------------------------------


def _propose_that_loses_one_race(real_propose, losing_lemma: str):
    """``api.propose``, except that one lemma raises the error a lost UNIQUE
    race produces.

    Written as a substitution rather than as a second process on purpose:
    what the fix changes is which exception CLASS the per-row handler
    catches, and a real race reproduces that class non-deterministically
    while costing a subprocess. The message is the one the adversarial pass
    recorded verbatim from four concurrent importers."""

    def _propose(store, **kwargs):
        if kwargs.get("lemma") == losing_lemma:
            raise ValidationError(
                "term: integrity violation on insert: UNIQUE constraint failed: term.lemma_norm"
            )
        return real_propose(store, **kwargs)

    return _propose


def test_a_raced_row_is_counted_under_refused_and_the_run_finishes(store, ids, records, monkeypatch):
    """Finding F3. ``stores.writer.insert`` wraps ``sqlite3.IntegrityError``
    in ``ValidationError`` -- a ``StoreError``, not a ``LexiconError`` -- so
    the per-row handler did not catch it and the losing run died at its first
    collision: nothing further imported, no ``term_backfill_run`` event, and
    no summary to reconcile against. That is exactly the outcome this
    module's docstring says it exists to prevent, by a route it had not
    considered."""
    losing_lemma = LEMMAS[0]
    monkeypatch.setattr(
        backfill.api, "propose", _propose_that_loses_one_race(api.propose, losing_lemma)
    )

    summary = _run(store, ids)

    assert summary["status"] == "ok", "the run finished"
    assert summary["refused_count"] >= 1
    assert any("UNIQUE constraint failed" in r["reason"] for r in summary["refused"])
    assert summary["senses_created"] == summary["records_seen"] - summary["refused_count"]
    events = store.ops.execute(
        "SELECT count(*) FROM event WHERE type = 'term_backfill_run'"
    ).fetchone()[0]
    assert events == 1, "the run reported itself"


def test_a_raced_row_in_the_claim_projection_is_refused_the_same_way(store, ids, monkeypatch):
    claim_id = _definition_claim(store, ids, "a reading another importer claimed a microsecond earlier")
    monkeypatch.setattr(
        backfill.api, "propose", _propose_that_loses_one_race(api.propose, "raced lemma")
    )
    summary = backfill.backfill_claims(
        store, by_launch=ids["launch"], lemma_map={claim_id: "raced lemma"}
    )
    assert summary["status"] == "ok"
    assert summary["refused_count"] == 1
    assert "UNIQUE constraint failed" in summary["refused"][0]["reason"]


# ---------------------------------------------------------------------------
# the register import -- acceptance criterion B
# ---------------------------------------------------------------------------


def test_the_sixty_row_fixture_imports_to_exact_counts(store, ids, records):
    result = _run(store, ids)

    assert result["records_seen"] == records["records"] == 60
    assert result["terms_created"] == len(LEMMAS) == 58
    assert result["senses_created"] == 60
    assert result["evidence_rows"] == 60
    assert result["refused_count"] == 0
    assert result["already_present"] == 0
    assert sorted(result["register_keys"]) == sorted(REGISTER_KEYS)


def test_each_recurring_lemma_becomes_exactly_one_conflict_item(store, ids, records):
    """Two registers using one name for two quantities is the whole reason
    the store exists. Every sense here is single-register, so every pair of
    readings is disjoint by construction."""
    result = _run(store, ids)
    assert result["conflicts_opened"] == len(SHARED_LEMMAS) == 2
    assert _pending_conflicts(store, ids) == 2

    for lemma, _register in SHARED_LEMMAS:
        term = api.find_term(store, lemma)
        senses = api.senses_for_term(store, term["term_id"], statuses=("current",))
        assert len(senses) == 2
        keys = {k for s in senses for k in api.source_keys_for_sense(store, s["sense_id"])}
        assert len(keys) == 2, "two registers, two source identities"


def test_an_imported_sense_lands_current_with_its_decay_stamp(store, ids, records):
    """Ruling L-E2: the denominator was gated once already, so a second
    acceptance pass would be ceremony -- but the sense still carries a
    ``review_after`` so the decay flag brings it back over time."""
    _run(store, ids)
    term = api.find_term(store, LEMMAS[2])
    sense = api.senses_for_term(store, term["term_id"])[0]

    assert sense["status"] == "current"
    assert sense["origin_kind"] == "record_import"
    assert sense["origin_ref"].startswith("REC-SYN-")
    assert sense["procedure_version"] == policy.RECORD_IMPORT_PROCEDURE_VERSION == "register-import-v1"
    assert sense["decided_by_launch"] == ids["launch"]
    assert sense["review_after"] > now()
    assert term["granularity"] == "instance"
    assert json.loads(term["tags"])


def test_the_evidence_row_keeps_the_register_key_and_its_verbatim_citation(store, ids, records):
    """``source_key`` is the register's own id until the document behind it
    is ingested (D31), and ``cite_raw`` is preserved exactly as the register
    wrote it -- neither is reconstructed later."""
    _run(store, ids)
    term = api.find_term(store, LEMMAS[2])
    sense = api.senses_for_term(store, term["term_id"])[0]
    evidence = api.evidence_for_sense(store, sense["sense_id"])
    assert len(evidence) == 1
    row = evidence[0]
    assert row["evidence_kind"] == "record"
    assert row["ref_id"] == sense["origin_ref"]
    assert row["source_key"] in REGISTER_KEYS
    assert row["cite_raw"] == f"[{row['source_key']} p{100 + int(row['ref_id'].split('-')[-1])}]"
    assert row["anchor_id"] is None, "a register row has a citation, not an anchor"


def test_a_second_run_inserts_nothing_at_all(store, ids, records):
    _run(store, ids)
    before = {
        table: _count(store, f"SELECT count(*) FROM {table}")
        for table in ("term", "term_sense", "term_sense_evidence", "term_relation", "prov_edge")
    }

    again = _run(store, ids)

    assert again["senses_created"] == 0
    assert again["terms_created"] == 0
    assert again["evidence_rows"] == 0
    assert again["conflicts_opened"] == 0
    assert again["already_present"] == 60
    assert {
        table: _count(store, f"SELECT count(*) FROM {table}")
        for table in before
    } == before


def test_the_extraction_queue_is_not_a_register(store, ids, records):
    """``record`` is also the merge-review queue's landing zone. Importing
    pending extraction candidates as terms would turn the queue into the
    store it exists to keep things out of."""
    insert(
        store,
        "record",
        {
            "record_id": new_id("RCD"),
            "register_key": EXTRACT_REGISTER_KEY,
            "artifact_id": None,
            "seq": 1,
            "payload": json.dumps({"kind": "entity", "name": "should not be imported", "description": "no"}),
            "anchors": None,
            "created_ts": now(),
        },
    )
    backfill.backfill_records(store, by_launch=ids["launch"])
    assert api.find_term(store, "should not be imported") is None


def test_one_unusable_row_is_counted_and_the_run_continues(store, ids, records):
    """Aborting a 7,000-row import on row 4,000 leaves a half-imported store
    to reason about; refusing per row and reporting the list does not."""
    insert(
        store,
        "record",
        {
            "record_id": "REC-SYN-9999",
            "register_key": REGISTER_KEYS[0],
            "artifact_id": None,
            "seq": 99,
            "payload": json.dumps({"row_id": "REC-SYN-9999", "description": "a reading with no name"}),
            "anchors": None,
            "created_ts": now(),
        },
    )
    result = _run(store, ids)

    assert result["refused_count"] == 1
    assert result["refused"][0]["record_id"] == "REC-SYN-9999"
    assert "name" in result["refused"][0]["reason"]
    assert result["senses_created"] == 60, "every other row still landed"


def test_every_accepted_sense_leaves_a_provenance_edge_home(store, ids, records):
    """Ruling L-E5: the lexicon is the provenance graph's first writer, and
    ``derived_from`` points a sense back at the row it was read out of."""
    _run(store, ids)
    edges = [
        dict(r)
        for r in store.knowledge.execute(
            "SELECT * FROM prov_edge WHERE role = 'derived_from' AND src_kind = 'term_sense'"
        ).fetchall()
    ]
    assert len(edges) == 60
    assert {e["dst_kind"] for e in edges} == {"record"}
    assert all(e["dst_id"].startswith("REC-SYN-") for e in edges)


def test_the_run_leaves_one_event_with_its_counts(store, ids, records):
    _run(store, ids)
    rows = [
        json.loads(r["payload"])
        for r in store.ops.execute("SELECT payload FROM event WHERE type = 'term_backfill_run'").fetchall()
    ]
    assert len(rows) == 1
    assert rows[0]["route"] == "records"
    assert rows[0]["senses_created"] == 60


# ---------------------------------------------------------------------------
# the level-1 family map (ruling L-E3)
# ---------------------------------------------------------------------------


def test_without_a_family_map_no_family_terms_exist_and_the_tags_are_reported(store, ids, records):
    """E1-E4 must never block on a file this repo is not allowed to hold, so
    the absence of the map is a reported state, not a failure."""
    result = _run(store, ids)
    assert result["families"]["created"] == 0
    assert result["families"]["unmapped_tags"] == ["f-handling", "f-signal", "f-stability", "f-timing"]
    assert _count(store, "SELECT count(*) FROM term WHERE granularity = 'family'") == 0
    assert _count(
        store, "SELECT count(*) FROM term WHERE granularity = 'instance' AND term_id != ?", ids["term"]
    ) == 58


def test_a_family_map_mints_one_term_per_mapped_tag_that_was_used(store, ids, records):
    result = _run(store, ids, family_map=FAMILY_MAP)

    assert result["families"]["created"] == 2
    assert result["families"]["unmapped_tags"] == ["f-handling", "f-signal"]
    family = api.find_term(store, "stability behaviours")
    assert family["granularity"] == "family"
    sense = api.senses_for_term(store, family["term_id"])[0]
    assert sense["status"] == "current"
    assert sense["procedure_version"] == backfill.FAMILY_PROCEDURE_VERSION
    evidence = api.evidence_for_sense(store, sense["sense_id"])
    assert 1 <= len(evidence) <= backfill.MAX_FAMILY_EVIDENCE
    assert all(row["evidence_kind"] == "record" for row in evidence)


def test_a_second_run_with_the_same_map_mints_nothing_new(store, ids, records):
    """A family sense has no origin row, so the partial unique index cannot
    make it idempotent -- the module checks instead, and this is the check."""
    _run(store, ids, family_map=FAMILY_MAP)
    before = _count(store, "SELECT count(*) FROM term_sense")

    again = _run(store, ids, family_map=FAMILY_MAP)
    assert again["families"] == {"created": 0, "existing": 2, "unmapped_tags": ["f-handling", "f-signal"]}
    assert _count(store, "SELECT count(*) FROM term_sense") == before


# ---------------------------------------------------------------------------
# attribution
# ---------------------------------------------------------------------------


def test_the_import_refuses_without_a_launch(store, ids, records):
    with pytest.raises(LaunchRequiredError):
        backfill.backfill_records(store, by_launch="")
    assert _count(store, "SELECT count(*) FROM term_sense WHERE origin_kind = 'record_import'") == 0


def test_the_import_refuses_a_launch_that_names_no_row(store, ids, records):
    with pytest.raises(XidTargetMissingError):
        backfill.backfill_records(store, by_launch="LNCH-does-not-exist")
    assert _count(store, "SELECT count(*) FROM term_sense WHERE origin_kind = 'record_import'") == 0


# ---------------------------------------------------------------------------
# the claim projection
# ---------------------------------------------------------------------------


def _definition_claim(store, ids, text: str) -> str:
    claim_id = new_id("CLM")
    insert(
        store,
        "claim",
        {
            "claim_id": claim_id,
            "text": text,
            "kind": "definition",
            "anchor_id": ids["quote_anchor"],
            "created_at": now(),
            "created_by_launch": ids["launch"],
        },
    )
    return claim_id


def test_a_mapped_definition_claim_is_projected_and_an_unmapped_one_is_counted(store, ids):
    mapped = _definition_claim(store, ids, "settling time is how long a reading takes to stop moving")
    _definition_claim(store, ids, "a definition nobody has mapped onto a lemma")

    result = backfill.backfill_claims(
        store, by_launch=ids["launch"], lemma_map={mapped: {"lemma": "settling time"}}
    )

    assert result["claims_seen"] == 2
    assert result["projected"] == 1
    assert result["unmapped"] == 1
    term = api.find_term(store, "settling time")
    sense = api.senses_for_term(store, term["term_id"])[0]
    assert sense["status"] == "current"
    assert sense["origin_kind"] == "extract"
    assert sense["origin_ref"] == mapped
    assert sense["procedure_version"] == backfill.CLAIM_BACKFILL_PROCEDURE_VERSION
    assert api.evidence_for_sense(store, sense["sense_id"])[0]["evidence_kind"] == "claim"


def test_an_already_projected_claim_is_not_seen_again(store, ids):
    """The same population the ``definition_claims_unprojected`` doctor check
    counts, so the backlog number and the work left cannot disagree."""
    mapped = _definition_claim(store, ids, "settling time is how long a reading takes to stop moving")
    backfill.backfill_claims(store, by_launch=ids["launch"], lemma_map={mapped: "settling time"})

    again = backfill.backfill_claims(store, by_launch=ids["launch"], lemma_map={mapped: "settling time"})
    assert again["claims_seen"] == 0
    assert again["projected"] == 0


def test_a_map_entry_with_no_lemma_is_refused_by_name(store, ids):
    mapped = _definition_claim(store, ids, "a definition whose map entry is empty")
    result = backfill.backfill_claims(store, by_launch=ids["launch"], lemma_map={mapped: "   "})
    assert result["refused_count"] == 1
    assert "names no lemma" in result["refused"][0]["reason"]
    assert result["projected"] == 0


# ---------------------------------------------------------------------------
# relink (ruling L-E6)
# ---------------------------------------------------------------------------


def _evidence_snapshot(store) -> list[dict]:
    return [
        dict(r)
        for r in store.knowledge.execute(
            "SELECT * FROM term_sense_evidence ORDER BY evidence_id"
        ).fetchall()
    ]


def _ingested_source(store, ids, title: str) -> str:
    source_id = new_id("SRC")
    insert(
        store,
        "source",
        {
            "source_id": source_id,
            "kind": "book",
            "title": title,
            "license_tier": "open",
            "acquisition_route": "web",
            "request_state": "indexed",
            "registered_ts": now(),
            "registered_by_launch": ids["launch"],
        },
    )
    return source_id


def test_relink_rewrites_the_source_key_and_touches_nothing_else(store, ids, records):
    _run(store, ids)
    source_id = _ingested_source(store, ids, "the ingested handbook")
    before = _evidence_snapshot(store)
    anchors_before = _count(store, "SELECT count(*) FROM quote_anchor")

    result = backfill.relink_evidence_sources(
        store, {REGISTER_KEYS[0]: source_id}, by_launch=ids["launch"]
    )

    assert result["rows_relinked"] == result["per_key"][REGISTER_KEYS[0]] == 15
    after = {row["evidence_id"]: row for row in _evidence_snapshot(store)}
    for row in before:
        moved = after[row["evidence_id"]]
        expected = dict(row)
        if row["source_key"] == REGISTER_KEYS[0]:
            expected["source_key"] = source_id
        assert moved == expected, "source_key is the only column a relink may change"
    assert _count(store, "SELECT count(*) FROM quote_anchor") == anchors_before


def test_relink_is_idempotent(store, ids, records):
    _run(store, ids)
    source_id = _ingested_source(store, ids, "the ingested handbook")
    backfill.relink_evidence_sources(store, {REGISTER_KEYS[0]: source_id}, by_launch=ids["launch"])
    snapshot = _evidence_snapshot(store)

    again = backfill.relink_evidence_sources(
        store, {REGISTER_KEYS[0]: source_id}, by_launch=ids["launch"]
    )
    assert again["rows_relinked"] == 0
    assert again["unmatched_keys"] == [REGISTER_KEYS[0]]
    assert _evidence_snapshot(store) == snapshot


def test_relink_refuses_a_target_with_no_source_row_before_writing_anything(store, ids, records):
    """Relinking evidence to a source that does not exist would break the
    disjointness rule in the direction that HIDES conflicts -- two senses
    silently agreeing on a source neither of them has."""
    _run(store, ids)
    snapshot = _evidence_snapshot(store)
    with pytest.raises(InvalidTermInputError, match="no source row"):
        backfill.relink_evidence_sources(
            store, {REGISTER_KEYS[0]: "SRC-not-ingested"}, by_launch=ids["launch"]
        )
    assert _evidence_snapshot(store) == snapshot


def test_relink_dry_run_reports_and_writes_nothing(store, ids, records):
    _run(store, ids)
    source_id = _ingested_source(store, ids, "the ingested handbook")
    snapshot = _evidence_snapshot(store)
    events_before = _count_events(store)

    result = backfill.relink_evidence_sources(
        store, {REGISTER_KEYS[0]: source_id}, by_launch=ids["launch"], dry_run=True
    )
    assert result["rows_matched"] == 15
    assert result["rows_relinked"] == 0
    assert _evidence_snapshot(store) == snapshot
    assert _count_events(store) == events_before


def test_relink_reports_the_backlog_the_doctor_check_counts(store, ids, records):
    _run(store, ids)
    source_id = _ingested_source(store, ids, "the ingested handbook")
    result = backfill.relink_evidence_sources(
        store, {REGISTER_KEYS[0]: source_id}, by_launch=ids["launch"]
    )
    remaining = result["unlinked_remaining"]
    assert REGISTER_KEYS[0] not in remaining
    assert set(REGISTER_KEYS[1:]) <= set(remaining)
    assert backfill.unlinked_source_keys(store) == remaining


def test_relink_needs_a_map(store, ids, records):
    with pytest.raises(InvalidTermInputError, match="non-empty"):
        backfill.relink_evidence_sources(store, {}, by_launch=ids["launch"])


def _count_events(store) -> int:
    return store.ops.execute("SELECT count(*) FROM event").fetchone()[0]


# ---------------------------------------------------------------------------
# the import route's own gloss cap (build step 1c, decision D5)
# ---------------------------------------------------------------------------


@pytest.fixture()
def oversized(store, ids, records):
    """The 60-row fixture plus two rows whose imported gloss is longer than
    a hand-written gloss may be: 100 words (the shape the real import
    refused) and 200 (the shape the import cap still refuses)."""
    return install_oversized_gloss_records(store)


def test_an_imported_gloss_over_the_hand_written_cap_is_imported(store, ids, records, oversized):
    """The 100-word row lands. Under the old single cap it was one of the
    271 rows the real import dropped -- for being a faithful copy of what
    the register said, which is the one thing an imported gloss is supposed
    to be."""
    out = _run(store, ids)
    assert out["senses_created"] == RECORD_COUNT + 1
    assert out["refused_count"] == 1
    assert "cap is 160" in out["refused"][0]["reason"]
    assert out["refused"][0]["record_id"] == oversized["record_ids"][1]


def test_re_running_the_backfill_imports_the_rows_the_old_cap_refused(store, ids, records, oversized):
    """The migration path, asserted end to end: a run under the old cap, a
    run under the new one, and the difference is EXACTLY the rows the first
    run refused. Everything else is idempotent -- 60 rows already present,
    nothing re-imported, nothing duplicated. That is what makes raising a
    cap a safe operation on a store that already holds 7,000 senses."""
    strict = {"lexicon": {"gloss_max_words_import": policy.GLOSS_MAX_WORDS}}

    first = _run(store, ids, config=strict)
    assert first["senses_created"] == RECORD_COUNT
    assert first["refused_count"] == 2
    assert sorted(r["record_id"] for r in first["refused"]) == sorted(oversized["record_ids"])

    second = _run(store, ids)
    assert second["senses_created"] == 1, "the 100-word row, and only it"
    assert second["terms_created"] == 1
    assert second["already_present"] == RECORD_COUNT
    assert second["refused_count"] == 1, "the 200-word row is still not a gloss"
    assert second["refused"][0]["record_id"] == oversized["record_ids"][1]

    third = _run(store, ids)
    assert third["senses_created"] == 0
    assert third["already_present"] == RECORD_COUNT + 1
    assert third["refused_count"] == 1


def test_the_family_route_keeps_the_hand_written_cap(store, ids, records):
    """A family gloss is hand-authored by the orchestrator, not lifted from
    a register, so it is measured against the strict cap like any other
    thing this program writes. Refused rows are counted, not fatal."""
    long_map = {
        "f-stability": {
            "lemma": "stability behaviours",
            "gloss": " ".join(["word"] * (policy.GLOSS_MAX_WORDS + 1)),
        }
    }
    out = _run(store, ids, family_map=long_map)
    assert out["families"]["created"] == 0
    assert api.find_term(store, "stability behaviours") is None
