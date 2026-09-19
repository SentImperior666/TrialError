"""The Lexicon panel at term-store scale: a BOUNDED number of SQL statements,
whatever the term count.

Why this test exists, measured rather than asserted from first principles:
``build_lexicon_panel`` used to load every term and then ask three more
questions per term (``senses_for_term``, ``relations_for_term`` -- itself two
queries -- and ``source_keys_for_sense`` per sense). Measured on the
5,000-term corpus below, on this machine: **25,003 statements / 2.36 s**
before, **6 statements / 0.11 s** after, with every number and field the
panel returns identical across the two. The live 7,260-term program measured
48.1 s for this one panel -- every ``GET /dashboard/api/all`` past the
deployment's 5-s status probe, i.e. a dashboard reported DOWN.

The statement-count assertion is the durable half: it is the property that
cannot quietly regress into an O(terms) loop again, and it holds at any
corpus size -- which is why the test builds the corpus at TWO sizes (50
terms and 5,000) and requires the counts to be EQUAL rather than merely
small. The wall-clock assertion is a generous categorical bound (2 s against
a measured 0.11 s), not a micro-benchmark: it is here to fail if the panel
ever returns to the regime that tripped the probe.

Correctness is checked against the v1 per-term primitives themselves
(``senses_for_term`` / ``source_keys_for_sense`` / ``relations_for_term``, still
the one definition of those reads) on a sample of terms, plus against the
fixture's own generated ground truth for every count the index reports.
"""

from __future__ import annotations

import time

import pytest

from trialerror.dashboard import data
from trialerror.dashboard.store_ro import open_store_ro
from trialerror.lexicon import api as lexicon_api
from trialerror.stores.store import open_store

from tests._lexicon_scale_fixtures import N_TERMS_SCALE, SENSES_PER_TERM, build_term_scale_corpus

#: Generous, categorical: what this bound rules out is the 2.36 s (here) /
#: 48.1 s (the live program) regime the per-term loop was in, not a few tens
#: of milliseconds of drift.
_BUILD_BOUND_S = 2.0

#: The batched builder issues six statements for the index (the ``term``
#: table-exists probe, the term rows, the three grouped reads, the
#: unprojected-definition count) and a handful more for one selected term's
#: evidence. Both ceilings are deliberately above the measured counts (6 and
#: 12) and neither may grow with the term count -- that is what
#: :func:`test_statement_count_does_not_grow_with_the_corpus` pins.
_INDEX_STATEMENT_CEILING = 12
_DETAIL_STATEMENT_CEILING = 24

#: Term 0 exists with an identical shape in every corpus size this module
#: builds (two senses, a retracted evidence row on each, a pending
#: term-scoped conflict), so its detail-path cost is comparable across them.
_SHARED_TERM_ID = "TERM-SCALE000000"


def _counted_build(rostore, **kwargs):
    """``(panel, n_statements, elapsed_s)`` for one panel build.

    ``sqlite3``'s own trace callback counts every statement the connection
    executes -- installed around the call only, so the store's own connect-time
    pragmas are not in the tally."""
    statements: list[str] = []
    rostore.knowledge.set_trace_callback(statements.append)
    try:
        t0 = time.perf_counter()
        panel = data.build_lexicon_panel(rostore, **kwargs)
        elapsed = time.perf_counter() - t0
    finally:
        rostore.knowledge.set_trace_callback(None)
    return panel, len(statements), elapsed


@pytest.fixture()
def scale_corpus(program_root, platform_root):
    """The 5,000-term corpus, built once, served read-only."""
    store = open_store(program_root, platform_root=platform_root)
    expected = build_term_scale_corpus(store)
    store.close()
    rostore = open_store_ro(program_root, platform_root=platform_root)
    yield rostore, expected
    rostore.close()


def test_lexicon_panel_index_is_bounded_at_term_scale(scale_corpus):
    rostore, expected = scale_corpus
    assert expected["n_terms"] == N_TERMS_SCALE
    assert expected["n_senses"] == N_TERMS_SCALE * SENSES_PER_TERM

    panel, n_statements, elapsed = _counted_build(rostore)
    print(
        f"\n[lexicon-scale] index over {expected['n_terms']} terms / {expected['n_senses']} senses / "
        f"{expected['n_relations']} relations: {elapsed:.3f}s in {n_statements} SQL statements"
    )

    assert panel["status"] == "ok"
    assert n_statements <= _INDEX_STATEMENT_CEILING, (
        f"{n_statements} statements for {expected['n_terms']} terms -- the panel is back to "
        "asking per-term questions"
    )
    assert elapsed < _BUILD_BOUND_S, f"the index took {elapsed:.2f}s (bound {_BUILD_BOUND_S}s)"

    # --- every count the index reports, against the fixture's ground truth
    counts = panel["counts"]
    assert counts["total"] == expected["n_terms"] == len(panel["terms"])
    assert sum(counts["by_state"].values()) == expected["n_terms"]
    assert counts["by_state"]["split_open"] == expected["split_open"]
    assert counts["by_state"]["retired"] == expected["retired"]
    assert counts["by_state"]["proposed"] == expected["proposed"]
    assert counts["by_granularity"] == expected["by_granularity"]
    assert counts["conflicts_open"] == expected["conflicts_open"]
    assert counts["needs_review"] == expected["needs_review"]
    assert counts["definition_claims_unprojected"] == 0

    # --- ordering: last_revised, descending, end to end
    assert panel["terms"][0]["term_id"] == expected["newest_term_id"]
    assert panel["terms"][-1]["term_id"] == expected["oldest_term_id"]
    stamps = [t["last_revised"] for t in panel["terms"]]
    assert stamps == sorted(stamps, reverse=True)

    # --- per-term fields for the sampled terms
    rows = {t["term_id"]: t for t in panel["terms"]}
    for sample in expected["samples"]:
        row = rows[sample["term_id"]]
        for field in ("lemma", "granularity", "status", "sense_count", "source_count", "gloss", "last_revised"):
            assert row[field] == sample[field], f"{sample['term_id']}.{field}"
        assert row["tags"] == [f"t-{sample['granularity'] or '__null__'}"]


def test_index_rows_match_the_per_term_primitives(scale_corpus):
    """The batched reads answer what ``senses_for_term`` /
    ``source_keys_for_sense`` / ``relations_for_term`` answer -- checked
    against those functions themselves on the sampled terms, which is the
    equivalence the rewrite has to keep (they remain the one definition of
    those three reads; the panel just stopped calling them once per term)."""
    rostore, expected = scale_corpus
    panel = data.build_lexicon_panel(rostore)
    rows = {t["term_id"]: t for t in panel["terms"]}

    for sample in expected["samples"]:
        term_id = sample["term_id"]
        row = rows[term_id]
        senses = lexicon_api.senses_for_term(rostore, term_id)
        assert row["sense_count"] == len(senses)

        source_keys: set[str] = set()
        for sense in senses:
            source_keys.update(lexicon_api.source_keys_for_sense(rostore, sense["sense_id"]))
        assert row["source_count"] == len(source_keys)
        assert "REG-retracted" not in source_keys  # the retracted rows stay out of both sides

        pending = lexicon_api.relations_for_term(rostore, term_id, statuses=("pending",))
        has_open_conflict = any(r["verb"] == "conflicts_with" for r in pending)
        assert (row["state"] == "split_open") == (has_open_conflict and row["status"] == "active")


def test_detail_half_still_selects_one_term_and_stays_bounded(scale_corpus):
    rostore, expected = scale_corpus
    panel, n_statements, elapsed = _counted_build(rostore, term_id=_SHARED_TERM_ID)
    print(f"[lexicon-scale] detail for one term: {elapsed:.3f}s in {n_statements} SQL statements")

    assert n_statements <= _DETAIL_STATEMENT_CEILING
    assert elapsed < _BUILD_BOUND_S
    assert panel["counts"]["total"] == expected["n_terms"]  # the index is still served alongside

    detail = panel["term"]
    assert detail is not None
    assert detail["term"]["term_id"] == _SHARED_TERM_ID

    senses = lexicon_api.senses_for_term(rostore, _SHARED_TERM_ID)
    assert [v["sense_id"] for v in detail["senses"]] == [s["sense_id"] for s in senses]
    for view, sense in zip(detail["senses"], senses):
        assert view["source_keys"] == lexicon_api.source_keys_for_sense(rostore, sense["sense_id"])
        assert view["gloss"] == sense["gloss"]
        assert len(view["evidence"]) == 1  # the retracted row is not served
        assert view["evidence"][0]["source_key"] == view["source_keys"][0]

    relations = lexicon_api.relations_for_term(rostore, _SHARED_TERM_ID)
    assert [v["rel_id"] for v in detail["relations"]] == [r["rel_id"] for r in relations]
    conflict = detail["conflict"]
    assert conflict is not None
    assert conflict["member_sense_ids"] == [s["sense_id"] for s in senses]

    missing = data.build_lexicon_panel(rostore, term_id="TERM-SCALE-does-not-exist")
    assert missing["term"] is None
    assert missing["not_found"] == {"kind": "term_id", "id": "TERM-SCALE-does-not-exist"}


def test_statement_count_does_not_grow_with_the_corpus(scale_corpus, tmp_path, platform_root):
    """The same panel over 50 terms and over 5,000 issues the SAME number of
    statements -- the property an O(terms) loop cannot have, and the one a
    future edit would break first."""
    big_rostore, expected = scale_corpus
    _, big_index, _ = _counted_build(big_rostore)
    _, big_detail, _ = _counted_build(big_rostore, term_id=_SHARED_TERM_ID)

    small_root = tmp_path / "program-small"
    small_root.mkdir()
    store = open_store(small_root, platform_root=platform_root)
    small_expected = build_term_scale_corpus(store, n_terms=50)
    store.close()
    small_rostore = open_store_ro(small_root, platform_root=platform_root)
    try:
        small_panel, small_index, _ = _counted_build(small_rostore)
        _, small_detail, _ = _counted_build(small_rostore, term_id=_SHARED_TERM_ID)
    finally:
        small_rostore.close()

    assert small_panel["counts"]["total"] == small_expected["n_terms"] == 50
    assert expected["n_terms"] == N_TERMS_SCALE
    print(
        f"[lexicon-scale] statements: 50 terms -> index {small_index} / detail {small_detail}; "
        f"{N_TERMS_SCALE} terms -> index {big_index} / detail {big_detail}"
    )
    assert big_index == small_index
    assert big_detail == small_detail
