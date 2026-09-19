"""``trialerror.lexicon.checks`` -- the nine lexicon doctor checks.

Shape follows ``tests/test_feed_translate_checks.py`` exactly: a
``registry`` fixture that clears + re-discovers the global check registry,
a ``_run``/``_ctx`` pair, and the ``store``/``program_root``/``platform_root``
fixtures from ``tests/conftest.py``.

Three states per check (the parenthetical the E3 build brief names): a
bare, never-opened program (``not_initialized``), a program whose
``knowledge.db`` stops at schema v4 (``awaiting_migration`` -- built by
running only ``knowledge.MIGRATIONS`` versions <= 4 against a real file, the
``test_lexicon_api.py::
test_conflicts_for_claim_raises_rather_than_reporting_no_conflicts_on_an_unmigrated_store``
technique, aimed at a real path instead of ``:memory:`` so a fresh
read-only connection can open it), and ``ok`` (the ``store`` fixture, which
``open_store`` always migrates to the latest version).

Several ``fail`` states audit invariants ``trialerror.lexicon.api`` itself
is built to make unreachable (the grounding law, the split-head invariant,
the MINING §5.3 constraint) -- reaching them here means writing directly
through ``trialerror.stores.writer.insert``/raw SQL, bypassing the write
API on purpose, exactly as ``tests/test_lexicon_api.py`` does for its own
adversarial cases.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trialerror.lexicon import api as lexicon_api
from trialerror.lexicon import policy as lexicon_policy
from trialerror.stores import insert, paths
from trialerror.stores.connection import connect
from trialerror.util.doctor import (
    DoctorContext,
    clear_registry,
    discover_and_register_checks,
    registered_checks,
    run_checks,
)
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests._store_fixtures import populate_one_of_everything

ALL_CHECK_NAMES = [
    "term_conflicts_pending",
    "term_duplicates_pending",
    "term_senses_need_review",
    "term_sense_without_evidence",
    "term_split_missing_disambiguator",
    "term_evidence_source_unlinked",
    "term_fts_in_sync",
    "definition_claims_unprojected",
    "term_system_relation_decided",
]

_PAST = "2000-01-01T00:00:00.000Z"


@pytest.fixture()
def registry():
    clear_registry()
    discover_and_register_checks()
    yield
    clear_registry()


@pytest.fixture()
def ids(store):
    return populate_one_of_everything(store)


def _run(ctx: DoctorContext, name: str):
    return {r.name: r for r in run_checks(ctx, only=[name])}[name]


def _ctx(program_root, platform_root) -> DoctorContext:
    return DoctorContext(program_root=program_root, platform_root=platform_root)


def _make_v4_only_knowledge_db(program_root: Path) -> None:
    from trialerror.stores.migrate import apply_migrations
    from trialerror.stores.schema import knowledge as knowledge_schema

    path = paths.knowledge_db_path(program_root)
    conn = connect(path)
    try:
        apply_migrations(conn, tuple(m for m in knowledge_schema.MIGRATIONS if m.version <= 4))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def test_all_nine_checks_are_auto_discovered(registry):
    names = registered_checks()
    for name in ALL_CHECK_NAMES:
        assert name in names, name
        assert names[name][0] == "lexicon"


# ---------------------------------------------------------------------------
# the three states, across the whole set
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ALL_CHECK_NAMES)
def test_every_check_skips_on_an_uninitialized_program(registry, tmp_path, name):
    ctx = _ctx(tmp_path / "nothing-here", tmp_path / "no-platform")
    result = _run(ctx, name)
    assert result.status == "skip"
    assert "not yet initialized" in result.message


@pytest.mark.parametrize("name", ALL_CHECK_NAMES)
def test_every_check_skips_awaiting_migration(registry, tmp_path, name):
    program_root = tmp_path / "program"
    program_root.mkdir()
    _make_v4_only_knowledge_db(program_root)
    ctx = _ctx(program_root, tmp_path / "platform")
    result = _run(ctx, name)
    assert result.status == "skip"
    assert "predates schema v5" in result.message


@pytest.mark.parametrize("name", ALL_CHECK_NAMES)
def test_every_check_passes_on_a_freshly_migrated_empty_store(registry, store, program_root, platform_root, name):
    """No lexicon rows at all -- every check's healthy baseline."""
    store.close()
    result = _run(_ctx(program_root, platform_root), name)
    assert result.status == "pass", (name, result.message)


# ---------------------------------------------------------------------------
# 1. term_conflicts_pending
# ---------------------------------------------------------------------------


def test_term_conflicts_pending_warns_and_names_the_oldest(registry, store, ids, program_root, platform_root):
    store.close()
    result = _run(_ctx(program_root, platform_root), "term_conflicts_pending")
    assert result.status == "warn"
    assert result.details["count"] == 1
    assert result.details["rel_ids"] == [ids["term_relation"]]
    assert result.details["oldest_marked_ts"]


# ---------------------------------------------------------------------------
# 2. term_duplicates_pending
# ---------------------------------------------------------------------------


def test_term_duplicates_pending_warns(registry, store, ids, program_root, platform_root):
    other = lexicon_api.propose(
        store, lemma="a distinct duplicate candidate", gloss="a reading", origin_kind="manual", origin_ref=None,
        evidence=[f"anchor:{ids['quote_anchor']}"], by_launch=ids["launch"], procedure_version="manual-v1",
    )
    lexicon_api.open_relation(
        store, src_kind="term", src_id=other["term_id"], dst_kind="term", dst_id=ids["term"],
        verb="same_as", marked_by_kind="system",
    )
    store.close()

    result = _run(_ctx(program_root, platform_root), "term_duplicates_pending")
    assert result.status == "warn"
    assert result.details["count"] == 1


# ---------------------------------------------------------------------------
# 3. term_senses_need_review
# ---------------------------------------------------------------------------


def test_term_senses_need_review_warns_and_groups_by_origin_kind(registry, store, ids, program_root, platform_root):
    proposed = lexicon_api.propose(
        store, lemma="a stale reading", gloss="a reading", origin_kind="manual", origin_ref=None,
        evidence=[f"anchor:{ids['quote_anchor']}"], by_launch=ids["launch"], procedure_version="manual-v1",
        status="current",
    )
    store.knowledge.execute(
        "UPDATE term_sense SET review_after = ? WHERE sense_id = ?", (_PAST, proposed["sense_id"]),
    )
    store.knowledge.commit()
    store.close()

    result = _run(_ctx(program_root, platform_root), "term_senses_need_review")
    assert result.status == "warn"
    assert proposed["sense_id"] in result.details["sense_ids"]
    assert result.details["by_origin_kind"]["manual"] >= 1


# ---------------------------------------------------------------------------
# 4. term_sense_without_evidence -- FAIL, structurally unreachable via api.py
# ---------------------------------------------------------------------------


def test_term_sense_without_evidence_fails(registry, store, ids, program_root, platform_root):
    orphan_sense_id = new_id("SENSE")
    insert(
        store, "term_sense",
        {
            "sense_id": orphan_sense_id, "term_id": ids["term"], "gloss": "a sense with no evidence row",
            "origin_kind": "manual", "procedure_version": "manual-v1", "status": "proposed",
            "created_at": now(), "proposed_by_launch": ids["launch"],
        },
    )
    store.close()

    result = _run(_ctx(program_root, platform_root), "term_sense_without_evidence")
    assert result.status == "fail"
    assert orphan_sense_id in result.details["sense_ids"]


@pytest.mark.parametrize("status", ["rejected", "superseded", "retired"])
def test_term_sense_without_evidence_ignores_readings_that_are_history(
    registry, store, ids, program_root, platform_root, status
):
    """The population is the two LIVE statuses (fix pass, finding F1). A
    rejected reading was refused, a superseded one has a successor carrying
    its evidence forward, and a retired one stopped being used and says so --
    none of the three is the store claiming a reading is grounded now, and
    counting them would make this fail-level check reachable from an ordinary
    retraction ``retract_evidence`` is right to allow."""
    sense_id = new_id("SENSE")
    insert(
        store, "term_sense",
        {
            "sense_id": sense_id, "term_id": ids["term"], "gloss": f"a {status} reading, no live evidence",
            "origin_kind": "manual", "procedure_version": "manual-v1", "status": status,
            "created_at": now(), "proposed_by_launch": ids["launch"], "decided_by_launch": ids["launch"],
            "decided_ts": now(),
        },
    )
    store.close()

    result = _run(_ctx(program_root, platform_root), "term_sense_without_evidence")
    assert result.status == "pass"


def test_term_sense_without_evidence_is_unreachable_through_the_write_api(
    registry, store, ids, program_root, platform_root
):
    """The other half of finding F1: the check is an audit of an invariant the
    API holds in both directions, so the only way to make it fail is to write
    around the API. Retracting the last live row under the fixture's own
    current sense is refused, and the check stays green."""
    from trialerror.lexicon import api as lexicon_api
    from trialerror.lexicon.errors import SenseWithoutEvidenceError

    with pytest.raises(SenseWithoutEvidenceError):
        lexicon_api.retract_evidence(
            store, ids["term_sense_evidence"], by_launch=ids["launch"], reason="wrong page"
        )
    store.close()

    result = _run(_ctx(program_root, platform_root), "term_sense_without_evidence")
    assert result.status == "pass"
    assert result.details["count"] == 0


# ---------------------------------------------------------------------------
# 5. term_split_missing_disambiguator -- FAIL
# ---------------------------------------------------------------------------


def test_term_split_missing_disambiguator_fails(registry, store, ids, program_root, platform_root):
    # the fixture's own term_sense is 'current' with disambiguator=NULL;
    # decide_relation's 'scoped' branch is the only real path to term.status
    # = 'split', and it refuses to leave a member without one -- reached
    # here by writing the head layer directly instead.
    store.knowledge.execute("UPDATE term SET status = 'split' WHERE term_id = ?", (ids["term"],))
    store.knowledge.commit()
    store.close()

    result = _run(_ctx(program_root, platform_root), "term_split_missing_disambiguator")
    assert result.status == "fail"
    assert ids["term_sense"] in result.details["sense_ids"]


# ---------------------------------------------------------------------------
# 6. term_evidence_source_unlinked
# ---------------------------------------------------------------------------


def test_term_evidence_source_unlinked_warns_and_groups_by_key(registry, store, ids, program_root, platform_root):
    # ids['record']'s register_key ("test-register") matches no source row
    # -- exactly the D31 pre-ingest state.
    lexicon_api.propose(
        store, lemma="a register-only term", gloss="a reading", origin_kind="record_import", origin_ref=ids["record"],
        evidence=[f"record:{ids['record']}"], by_launch=ids["launch"], procedure_version=lexicon_policy.RECORD_IMPORT_PROCEDURE_VERSION,
    )
    store.close()

    result = _run(_ctx(program_root, platform_root), "term_evidence_source_unlinked")
    assert result.status == "warn"
    assert result.details["by_source_key"]["test-register"] == 1


# ---------------------------------------------------------------------------
# 7. term_fts_in_sync
# ---------------------------------------------------------------------------


def test_term_fts_in_sync_fails_when_a_direct_write_never_reindexed(registry, store, ids, program_root, platform_root):
    # populate_one_of_everything inserts its term/alias/sense rows directly
    # through trialerror.stores.writer.insert, never through lexicon.api --
    # so term_fts (API-maintained only) has zero rows for them.
    store.close()
    result = _run(_ctx(program_root, platform_root), "term_fts_in_sync")
    assert result.status == "fail"
    assert result.details["fts_count"] == 0
    assert result.details["expected"] == result.details["term_count"] + result.details["alias_count"] > 0


def test_term_fts_in_sync_passes_after_reindex_all(registry, store, ids, program_root, platform_root):
    lexicon_api.reindex_all(store)
    store.close()
    result = _run(_ctx(program_root, platform_root), "term_fts_in_sync")
    assert result.status == "pass"
    assert result.details["fts_count"] == result.details["expected"]


# ---------------------------------------------------------------------------
# 8. definition_claims_unprojected
# ---------------------------------------------------------------------------


def test_definition_claims_unprojected_warns(registry, store, ids, program_root, platform_root):
    claim_id = new_id("CLM")
    insert(
        store, "claim",
        {
            "claim_id": claim_id, "text": "a definition nobody has projected yet", "kind": "definition",
            "anchor_id": ids["quote_anchor"], "created_at": now(), "created_by_launch": ids["launch"],
        },
    )
    store.close()

    result = _run(_ctx(program_root, platform_root), "definition_claims_unprojected")
    assert result.status == "warn"
    assert claim_id in result.details["claim_ids"]


def test_definition_claims_unprojected_passes_once_the_lexicon_has_a_sense_for_it(registry, store, ids, program_root, platform_root):
    claim_id = new_id("CLM")
    insert(
        store, "claim",
        {
            "claim_id": claim_id, "text": "a definition the lexicon already has", "kind": "definition",
            "anchor_id": ids["quote_anchor"], "created_at": now(), "created_by_launch": ids["launch"],
        },
    )
    lexicon_api.propose(
        store, lemma="a projected definition", gloss="a reading", origin_kind="extract", origin_ref=claim_id,
        evidence=[f"claim:{claim_id}"], by_launch=ids["launch"], procedure_version="extract-term-v1",
    )
    store.close()

    result = _run(_ctx(program_root, platform_root), "definition_claims_unprojected")
    assert result.status == "pass"
    assert claim_id not in result.details["claim_ids"]


# ---------------------------------------------------------------------------
# 9. term_system_relation_decided -- FAIL, MINING §5.3 audited from outside
# ---------------------------------------------------------------------------


def test_term_system_relation_decided_fails(registry, store, ids, program_root, platform_root):
    # decide_relation refuses to confirm a system-marked relation with no
    # deciding launch (unreachable by construction, per its own docstring);
    # reached here with a direct UPDATE instead.
    store.knowledge.execute(
        "UPDATE term_relation SET status = 'confirmed' WHERE rel_id = ?", (ids["term_relation"],),
    )
    store.knowledge.commit()
    store.close()

    result = _run(_ctx(program_root, platform_root), "term_system_relation_decided")
    assert result.status == "fail"
    assert ids["term_relation"] in result.details["rel_ids"]


def test_term_system_relation_decided_passes_for_a_properly_decided_relation(registry, store, ids, program_root, platform_root):
    other = lexicon_api.propose(
        store, lemma="a properly decided duplicate", gloss="a reading", origin_kind="manual", origin_ref=None,
        evidence=[f"anchor:{ids['quote_anchor']}"], by_launch=ids["launch"], procedure_version="manual-v1",
    )
    opened = lexicon_api.open_relation(
        store, src_kind="term", src_id=other["term_id"], dst_kind="term", dst_id=ids["term"],
        verb="same_as", marked_by_kind="system",
    )
    lexicon_api.decide_relation(store, opened["rel_id"], decision="unrelated", by_launch=ids["launch"])
    store.close()

    result = _run(_ctx(program_root, platform_root), "term_system_relation_decided")
    assert result.status == "pass"


# ---------------------------------------------------------------------------
# 2b. term_duplicates_pending -- which half of the queue a re-scan can move
# (build step 1c, decision D4)
# ---------------------------------------------------------------------------


def _bare_term(store, ids, lemma: str) -> str:
    """A term with no name in common with any other, so proposing it opens
    no candidate of its own and the counts below are only what each test
    put there."""
    return lexicon_api.propose(
        store, lemma=lemma, gloss=f"a reading of {lemma}", origin_kind="manual", origin_ref=None,
        evidence=[f"anchor:{ids['quote_anchor']}"], by_launch=ids["launch"], procedure_version="manual-v1",
    )["term_id"]


def test_term_duplicates_pending_splits_system_opened_from_human_touched(
    registry, store, ids, program_root, platform_root
):
    """An operator facing a five-figure queue needs to know which half a
    ``term scan --rescan`` can withdraw before deciding whether it is a
    crisis or a knob. The system-opened half is machine guesses; the rest is
    somebody's attention, and stays exactly the size it is."""
    left = _bare_term(store, ids, "alpha widget")
    right = _bare_term(store, ids, "beta gadget")
    third = _bare_term(store, ids, "gamma sprocket")

    lexicon_api.open_relation(
        store, src_kind="term", src_id=left, dst_kind="term", dst_id=right,
        verb="same_as", marked_by_kind="system",
    )
    lexicon_api.open_relation(
        store, src_kind="term", src_id=left, dst_kind="term", dst_id=third,
        verb="same_as", marked_by_kind="launch", marked_by_launch=ids["launch"],
        marked_by_model="a-person",
    )
    store.close()

    result = _run(_ctx(program_root, platform_root), "term_duplicates_pending")
    assert result.status == "warn"
    assert result.details["count"] == 2
    assert result.details["system_opened"] == 1
    assert result.details["human_touched"] == 1
    assert "1 system-opened, 1 human-touched" in result.message


def test_a_pending_row_a_launch_has_reached_into_counts_as_human_touched(
    registry, store, ids, program_root, platform_root
):
    """The second half of the rule the withdrawal obeys: a decided timestamp
    on a still-pending row means somebody is mid-decision, and neither the
    re-scan nor this count treats it as the machine's any more."""
    from trialerror.stores.writer import update

    left = _bare_term(store, ids, "alpha widget")
    right = _bare_term(store, ids, "beta gadget")
    opened = lexicon_api.open_relation(
        store, src_kind="term", src_id=left, dst_kind="term", dst_id=right,
        verb="same_as", marked_by_kind="system",
    )
    update(
        store, "term_relation", pk_column="rel_id", pk_value=opened["rel_id"],
        changes={"decided_ts": now(), "decided_by_launch": ids["launch"]},
    )
    store.close()

    result = _run(_ctx(program_root, platform_root), "term_duplicates_pending")
    assert result.details["count"] == 1
    assert result.details["system_opened"] == 0
    assert result.details["human_touched"] == 1
