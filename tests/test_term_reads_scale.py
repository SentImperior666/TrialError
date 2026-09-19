"""The four term-store reads that still asked one question per row, at the
scale the live store actually has: byte-identical output, in a bounded
number of statements.

Backlog item (c). The Lexicon panel's own fix (``tests/
test_dashboard_lexicon_panel_scale.py``) removed this shape from ONE builder
and measured what it had been costing: 48.1 s for that panel alone on the
live 7,260-term store, 25,003 SQL statements against 6 on a synthetic one.
The same shape was still in four places an operator or an agent reaches
directly:

    ``trialerror term list``            senses (1 query) + the preferred
                                       sense (1 more) PER TERM
    ``trialerror term scan``            the term row, its source sets
                                       (1 + one per sense) and its blocking
                                       conflicts, PER TERM
    ``lexicon.scan.source_sets_for_term``  one ``source_keys_for_sense`` per
                                       sense
    MCP ``term_lookup``                one ``evidence_for_sense`` per sense,
                                       plus an anchor query and a source
                                       query PER ANCHORED EVIDENCE ROW

**What "byte-identical" is proved against here.** Every batched read is
compared with the v1 per-row form *itself* -- ``senses_for_term`` /
``source_keys_for_sense`` / ``get_sense`` / ``get_term`` / ``_open_conflicts``
/ ``evidence_for_sense``, still the one definition of each of those reads --
over the whole 2,000-term fixture, serialised with ``json.dumps`` and
compared as strings, so a difference in ORDER or in a key's presence fails
just as loudly as a difference in a value. For ``term_lookup`` the v1 form is
not a re-implementation at all: the batched maps are optional arguments, so
calling the same payload functions without them IS the old code path, and
both are exercised in the same assertion.

``term scan`` is the one site whose entry point writes (it opens conflict
rows), so the equivalence is proved one level in, over the three READS the
rewrite changed -- with the whole-store pass then run for real and its
counts checked against the fixture's generated ground truth. A pure function
of inputs proved identical, over inputs proved identical, is the same
outcome; running the writing pass twice over two stores would only compare
freshly-minted random rel ids.

**The bound.** The batched statement count grows with
``ceil(ids / _ID_CHUNK)`` -- the SQLite parameter ceiling, not the row count
-- so the assertion is against that arithmetic, and against the v1 count it
replaces (thousands). The wall-clock numbers printed by each test are the
measurement the lane's report records; they are not asserted except as a
generous categorical bound, the same posture the panel's own scale test
takes.

The corpus is ``tests/_lexicon_scale_fixtures.py``'s -- invented vocabulary
from no domain -- at 2,000 terms, plus (for the anchored-evidence half) the
two-source retrieval fixture, one ``open`` and one ``commercial_restricted``,
so the fence branch is exercised on both sides.
"""

from __future__ import annotations

import json
import math
import time

import pytest

from trialerror.cli import build_parser
from trialerror.lexicon import api as lexicon_api
from trialerror.lexicon import scan as lexicon_scan
from trialerror.mcp import knowledge as mcp_knowledge
from trialerror.stores.store import open_store

from tests._lexicon_scale_fixtures import build_term_scale_corpus

#: The brief's floor for this test. Three orders of magnitude past the
#: one-term fixtures the term store's other tests use, and close enough to
#: the live store's 7,260 that the measurement means something while the
#: fixture still builds in well under a second.
N_TERMS = 2_000

#: Generous and categorical, like the panel scale test's own: what it rules
#: out is the regime the per-row form was in, not a few ms of drift.
_BOUND_S = 5.0


def _dumps(value) -> str:
    """Serialisation that preserves insertion order and refuses to paper over
    a type change -- what makes the comparisons below byte comparisons."""
    return json.dumps(value, ensure_ascii=False, default=str)


def _chunk_ceiling(n_ids: int, *, statements_per_chunk: int = 1, fixed: int = 1) -> int:
    return fixed + statements_per_chunk * max(1, math.ceil(n_ids / lexicon_scan._ID_CHUNK))


class _CountedConn:
    """A statement counter around one connection, as a context manager.

    ``sqlite3``'s own trace callback counts every statement the connection
    executes; installed around the call under test only, so the store's
    connect-time pragmas are not in the tally (the panel scale test's own
    technique)."""

    def __init__(self, conn):
        self.conn = conn
        self.statements: list[str] = []
        self.elapsed = 0.0

    def __enter__(self):
        self.conn.set_trace_callback(self.statements.append)
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.elapsed = time.perf_counter() - self._t0
        self.conn.set_trace_callback(None)
        return False

    @property
    def n(self) -> int:
        return len(self.statements)


@pytest.fixture()
def scale_store(program_root, platform_root):
    """2,000 terms, built once, left open for the reads under test."""
    store = open_store(program_root, platform_root=platform_root)
    expected = build_term_scale_corpus(store, n_terms=N_TERMS)
    yield store, expected
    store.close()


class _NoClose:
    """The CLI handlers open and close their own store, which a statement
    counter cannot reach into. This proxy hands them the store the test
    already has -- and swallows only ``close()``, so everything else about
    the handler's path is unchanged."""

    def __init__(self, store):
        self._store = store

    def __getattr__(self, name):
        return getattr(self._store, name)

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# source_sets_for_term / source_sets_for_terms
# ---------------------------------------------------------------------------
def _v1_source_sets(store, term_id: str) -> dict[str, list[str]]:
    """The read as it stood: one ``source_keys_for_sense`` per current sense."""
    return {
        sense["sense_id"]: lexicon_api.source_keys_for_sense(store, sense["sense_id"])
        for sense in lexicon_api.senses_for_term(store, term_id, statuses=("current",))
    }


def test_source_sets_are_byte_identical_before_and_after(scale_store):
    store, expected = scale_store
    term_ids = [r[0] for r in store.knowledge.execute("SELECT term_id FROM term ORDER BY term_id")]
    assert len(term_ids) == N_TERMS

    with _CountedConn(store.knowledge) as v1:
        v1_sets = {tid: _v1_source_sets(store, tid) for tid in term_ids}
    with _CountedConn(store.knowledge) as new:
        batched = lexicon_scan.source_sets_for_terms(store, term_ids)
    with _CountedConn(store.knowledge) as per_term:
        one = lexicon_scan.source_sets_for_term(store, term_ids[0])

    print(
        f"\n[term-scale] source_sets over {N_TERMS} terms: v1 {v1.n} statements / {v1.elapsed:.3f}s "
        f"-> batched {new.n} statements / {new.elapsed:.3f}s (one term: {per_term.n} statements)"
    )

    # the whole map, term by term, in order
    assert _dumps(batched) == _dumps(v1_sets)
    # and the single-term entry point, which is what accept_sense still calls
    assert _dumps(one) == _dumps(v1_sets[term_ids[0]])
    assert _dumps(lexicon_scan.source_sets_for_term(store, term_ids[0], prefetched=batched)) == _dumps(
        v1_sets[term_ids[0]]
    )

    # bounded by the chunk arithmetic, not by the row count: one statement per
    # chunk of term ids for the senses, one per chunk of sense ids for the
    # evidence. v1 issued one per sense plus one per term.
    n_senses = sum(len(s) for s in batched.values())
    ceiling = _chunk_ceiling(len(term_ids), fixed=0) + _chunk_ceiling(n_senses, fixed=0)
    assert new.n <= ceiling, f"{new.n} statements against a ceiling of {ceiling}"
    assert new.n < v1.n / 100
    assert per_term.n == 2  # the senses, then their evidence
    assert new.elapsed < _BOUND_S


def test_a_sense_with_no_live_evidence_still_maps_to_an_empty_list(scale_store):
    """The ungrounded case the conflict rule reports under ``ungrounded``: the
    per-sense form returns ``[]`` for it, so the grouped form must too -- a
    MISSING key would read as "no such sense" one layer up."""
    store, _ = scale_store
    sense_id = "SENSE-SCALE000000-0"
    store.knowledge.execute("UPDATE term_sense_evidence SET retracted_ts = ? WHERE sense_id = ?",
                            ("2026-01-01T00:00:00.000Z", sense_id))
    store.knowledge.commit()
    batched = lexicon_scan.source_sets_for_terms(store, ["TERM-SCALE000000"])
    assert batched["TERM-SCALE000000"][sense_id] == []
    assert _dumps(batched["TERM-SCALE000000"]) == _dumps(_v1_source_sets(store, "TERM-SCALE000000"))


# ---------------------------------------------------------------------------
# term list
# ---------------------------------------------------------------------------
def _v1_list_terms(store) -> list[dict]:
    """``_run_list``'s loop as it stood -- same dict, same key order, with the
    two per-term queries it used to make."""
    rows = [dict(r) for r in store.knowledge.execute("SELECT * FROM term WHERE 1=1 ORDER BY lemma_norm")]
    out = []
    for row in rows:
        senses = lexicon_api.senses_for_term(store, row["term_id"])
        preferred = None
        if row.get("preferred_sense_id"):
            preferred_sense = lexicon_api.get_sense(store, row["preferred_sense_id"])
            preferred = preferred_sense["gloss"] if preferred_sense else None
        out.append({
            "term_id": row["term_id"],
            "lemma": row["lemma"],
            "granularity": row["granularity"],
            "tags": json.loads(row["tags"]) if row.get("tags") else None,
            "status": row["status"],
            "preferred_gloss": preferred,
            "sense_count": len(senses),
            "updated_ts": row["updated_ts"],
        })
    return out


def test_term_list_is_byte_identical_before_and_after(scale_store, monkeypatch, program_root, platform_root):
    store, _ = scale_store
    from trialerror.cli import term as term_cli

    with _CountedConn(store.knowledge) as v1:
        v1_terms = _v1_list_terms(store)

    monkeypatch.setattr(term_cli, "_open", lambda args, cmd: (_NoClose(store), None))
    args = build_parser().parse_args(["term", "list", "--program-root", str(program_root)])
    with _CountedConn(store.knowledge) as new:
        env = args.handler(args)

    print(
        f"\n[term-scale] term list over {N_TERMS} terms: v1 {v1.n} statements / {v1.elapsed:.3f}s "
        f"-> batched {new.n} statements / {new.elapsed:.3f}s"
    )

    assert env["ok"] is True
    assert env["result"]["count"] == N_TERMS
    assert _dumps(env["result"]["terms"]) == _dumps(v1_terms)
    # the term rows, the grouped sense counts, the preferred-gloss join
    assert new.n == 3
    assert new.n < v1.n / 100
    assert new.elapsed < _BOUND_S


def test_term_list_filters_still_read_the_same_rows(scale_store, monkeypatch, program_root):
    """The grouped reads are whole-table; the filtered listing must still
    report each row's own count and gloss, and nobody else's."""
    store, _ = scale_store
    from trialerror.cli import term as term_cli

    monkeypatch.setattr(term_cli, "_open", lambda args, cmd: (_NoClose(store), None))
    args = build_parser().parse_args(
        ["term", "list", "--program-root", str(program_root), "--state", "retired"]
    )
    env = args.handler(args)
    rows = env["result"]["terms"]
    assert rows and all(r["status"] == "retired" for r in rows)
    by_id = {r["term_id"]: r for r in _v1_list_terms(store)}
    assert _dumps(rows) == _dumps([by_id[r["term_id"]] for r in rows])


# ---------------------------------------------------------------------------
# term scan (the conflict half's three reads)
# ---------------------------------------------------------------------------
def test_the_scans_three_prefetched_reads_are_byte_identical(scale_store):
    store, expected = scale_store
    term_ids = [r[0] for r in store.knowledge.execute("SELECT term_id FROM term ORDER BY term_id")]

    with _CountedConn(store.knowledge) as v1:
        v1_terms = {tid: lexicon_api.get_term(store, tid) for tid in term_ids}
        v1_blocking = {tid: lexicon_scan._open_conflicts(store, tid) for tid in term_ids}
        v1_sets = {tid: _v1_source_sets(store, tid) for tid in term_ids}
    with _CountedConn(store.knowledge) as new:
        terms = lexicon_scan.terms_by_id(store, term_ids)
        blocking = lexicon_scan.blocking_conflicts_by_term(store, term_ids)
        sets = lexicon_scan.source_sets_for_terms(store, term_ids)

    print(
        f"\n[term-scale] scan's three reads over {N_TERMS} terms: v1 {v1.n} statements / "
        f"{v1.elapsed:.3f}s -> batched {new.n} statements / {new.elapsed:.3f}s"
    )

    assert _dumps([terms[t] for t in term_ids]) == _dumps([v1_terms[t] for t in term_ids])
    assert _dumps([blocking[t] for t in term_ids]) == _dumps([v1_blocking[t] for t in term_ids])
    assert _dumps(sets) == _dumps(v1_sets)
    # the fixture has blocking rows to compare, not just empty lists
    assert any(v1_blocking[t] for t in term_ids)
    assert new.n < v1.n / 100
    assert new.elapsed < _BOUND_S


def test_the_whole_store_conflict_pass_opens_what_the_fixture_says_it_should(scale_store):
    """The batched pass run for real. The fixture's bucket-0 terms already
    carry a pending term-scoped conflict over both senses, and its bucket-3
    terms carry a sense-to-sense one that is NOT the term-scoped member set --
    so the pass is expected to open exactly the items whose member set nothing
    has raised, and to report the rest as existing."""
    store, expected = scale_store
    with _CountedConn(store.knowledge) as counted:
        result = lexicon_scan.scan_terms(store, duplicates=False)
    print(
        f"\n[term-scale] scan_terms(duplicates=False) over {N_TERMS} terms: {counted.n} statements "
        f"/ {counted.elapsed:.3f}s, {result['conflicts_opened']} opened"
    )
    assert result["status"] == "ok"
    # merged/retired are skipped; the fixture retires one term in _RETIRED_STRIDE
    assert result["terms_scanned"] == N_TERMS - expected["retired"]

    # every opened item is a term whose two senses are current and stand on
    # disjoint sources, and which had no term-scoped row for that member set
    opened_terms = {c["term_id"] for c in result["conflicts"]}
    for term_id in sorted(opened_terms):
        sets = lexicon_scan.source_sets_for_term(store, term_id)
        grounded = [k for k, v in sets.items() if v]
        assert len(grounded) >= 2
        assert set(sets[grounded[0]]).isdisjoint(sets[grounded[1]])

    # re-running opens nothing: the second pass sees its own rows
    again = lexicon_scan.scan_terms(store, duplicates=False)
    assert again["conflicts_opened"] == 0


# ---------------------------------------------------------------------------
# MCP term_lookup
# ---------------------------------------------------------------------------
@pytest.fixture()
def anchored_scale_store(scale_store):
    """The scale corpus plus real anchors: one ``open``-licensed and one
    ``commercial_restricted``, attached to the first term's two senses, so the
    fence branch is exercised on both sides and the non-anchored and retracted
    rows beside them stay in the payload."""
    from tests._retrieve_fixtures import build_small_corpus

    store, expected = scale_store
    corpus = build_small_corpus(store)
    anchors = {
        "open": store.knowledge.execute(
            "SELECT anchor_id FROM quote_anchor WHERE chunk_id = ?", (corpus["open_chunk_ids"][0],)
        ).fetchone()[0],
        "restricted": store.knowledge.execute(
            "SELECT anchor_id FROM quote_anchor WHERE chunk_id = ?",
            (corpus["restricted_chunk_ids"][0],),
        ).fetchone()[0],
    }
    rows = [
        (
            f"TSE-ANCHORED-{i}", f"SENSE-SCALE000000-{i}", "quote_anchor", anchor_id, None,
            source_id, f"cite {i}", None, expected["launch_id"], "2025-01-01T00:00:00.000Z", None, None,
        )
        for i, (anchor_id, source_id) in enumerate(
            [(anchors["open"], corpus["open_source_id"]), (anchors["restricted"], corpus["restricted_source_id"])]
        )
    ]
    with store.knowledge:
        store.knowledge.executemany(
            "INSERT INTO term_sense_evidence (evidence_id,sense_id,evidence_kind,anchor_id,ref_id,"
            "source_key,cite_raw,excerpt,created_by_launch,created_ts,retracted_ts,retracted_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return store, expected


def _v1_senses_payload(store, term_id: str, statuses) -> list[dict]:
    """The payload as it stood: one ``evidence_for_sense`` per sense, and the
    anchor + source queries per anchored row. Not a re-implementation -- these
    are the same two functions with their batched arguments omitted, which is
    exactly the code path they took before this change."""
    return [
        mcp_knowledge._term_sense_payload(store, s)
        for s in lexicon_api.senses_for_term(store, term_id, statuses=statuses)
    ]


@pytest.mark.parametrize("include_all", [False, True])
def test_term_lookup_is_byte_identical_before_and_after(
    anchored_scale_store, program_root, platform_root, include_all
):
    store, _ = anchored_scale_store
    term_id = "TERM-SCALE000000"
    statuses = None if include_all else ("current",)

    with _CountedConn(store.knowledge) as v1:
        v1_senses = _v1_senses_payload(store, term_id, statuses)

    tools = mcp_knowledge.build_tools(program_root=program_root, platform_root=platform_root)
    env = tools["term_lookup"].handler({"term_id": term_id, "include_all_senses": include_all})

    assert env["ok"] is True
    assert _dumps(env["result"]["senses"]) == _dumps(v1_senses)
    # the fence really was exercised on both sides, and the unresolvable
    # anchor fenced rather than opened
    fenced = {
        e["evidence_id"]: e["fenced"]
        for sense in env["result"]["senses"]
        for e in sense["evidence"]
        if e["anchored"]
    }
    assert fenced
    assert fenced["TSE-ANCHORED-0"] is False
    if include_all:
        assert fenced["TSE-ANCHORED-1"] is True

    # the batched read, counted on the test's own connection (the handler opens
    # its own store, so this measures the same functions against the same rows)
    with _CountedConn(store.knowledge) as new:
        sense_rows = lexicon_api.senses_for_term(store, term_id, statuses=statuses)
        by_sense = mcp_knowledge._live_evidence_by_sense(store, [s["sense_id"] for s in sense_rows])
        context = mcp_knowledge._term_evidence_context(
            store, [row for rows in by_sense.values() for row in rows]
        )
        batched = [
            mcp_knowledge._term_sense_payload(
                store, s, evidence=by_sense[s["sense_id"]], context=context
            )
            for s in sense_rows
        ]
    print(
        f"\n[term-scale] term_lookup (include_all={include_all}): v1 {v1.n} statements / "
        f"{v1.elapsed:.4f}s -> batched {new.n} statements / {new.elapsed:.4f}s"
    )
    assert _dumps(batched) == _dumps(v1_senses)
    # senses, their evidence, the anchors, the sources: four, and it does not
    # grow with the number of anchored rows
    assert new.n == 4
    assert new.n < v1.n


def test_term_lookup_with_no_anchored_evidence_asks_no_anchor_questions(scale_store, program_root, platform_root):
    """A term whose evidence is all ``record`` rows: the anchor and source
    reads must not happen at all, not merely return nothing."""
    store, _ = scale_store
    sense_rows = lexicon_api.senses_for_term(store, "TERM-SCALE000001", statuses=("current",))
    by_sense = mcp_knowledge._live_evidence_by_sense(store, [s["sense_id"] for s in sense_rows])
    with _CountedConn(store.knowledge) as counted:
        context = mcp_knowledge._term_evidence_context(
            store, [row for rows in by_sense.values() for row in rows]
        )
    assert counted.n == 0
    assert context == {"anchors": {}, "licenses": {}}


def test_an_unresolvable_anchor_fences_on_both_paths(scale_store):
    """The one branch the schema will not let a fixture INSERT (the anchor_id
    foreign key sees to that): an evidence row whose anchor cannot be
    resolved. The batched form looks it up in a map and the per-row form in a
    query, and a MISSING entry has to mean the same restrictive thing as a
    query that returned nothing -- "unknown" must fail toward the more
    restrictive reading, never the less (design Section 7)."""
    store, _ = scale_store
    row = {
        "evidence_id": "TSE-synthetic",
        "evidence_kind": "quote_anchor",
        "anchor_id": "QA-does-not-exist",
        "source_key": "SRC-gone",
        "cite_raw": None,
        "excerpt": "text that must never be served",
    }
    per_row = mcp_knowledge._term_evidence_payload(store, row)
    batched = mcp_knowledge._term_evidence_payload(
        store, row, context={"anchors": {}, "licenses": {}}
    )
    assert per_row["fenced"] is True
    assert _dumps(batched) == _dumps(per_row)
