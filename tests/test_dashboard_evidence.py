"""``build_evidence_panel`` -- lane C step C6, spec section 1.4.

The Evidence tab had no backing route at all (the operator's own 2026-09-05
walkthrough: "Evidence page: no backing route yet"), and the page said so in
a ``gap-notice``. This file is the contract for what replaced it.

Two halves, deliberately:

1. **A hand-built fixture** (:func:`traced`) rather than
   ``populate_one_of_everything`` alone, because the readings this panel
   exists for are all about DISAGREEMENT between rows -- an anchor whose
   document has been re-ingested since, a claim with more anchors than the
   one the schema calls primary, a fenced source next to an open one, a
   verdict that argues with the claim. A one-of-everything store has one row
   per table and can express none of that.
2. **The shared fixtures** for the ``ok`` / ``not_initialized`` pair every
   builder owes (spec section 6), plus the bundle and the HTTP route.

The Node/DOM half of C6 lives in ``tests/test_dashboard_evidence_render.py``.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from trialerror.dashboard import data
from trialerror.dashboard.store_ro import open_store_ro
from trialerror.ingest.anchors import sha256_hex
from trialerror.retrieve.wrap import UNTRUSTED_CLOSE, UNTRUSTED_OPEN
from trialerror.stores.store import open_store
from trialerror.stores.writer import insert as store_insert
from trialerror.stores.writer import update as store_update
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now
from tests._store_fixtures import populate_one_of_everything


@pytest.fixture()
def seeded(program_root, platform_root):
    store = open_store(program_root, platform_root=platform_root)
    ids = populate_one_of_everything(store)
    store.close()
    rostore = open_store_ro(program_root, platform_root=platform_root)
    yield rostore, ids
    rostore.close()


@pytest.fixture()
def empty_rostore(tmp_path):
    rostore = open_store_ro(tmp_path / "no-program", platform_root=tmp_path / "no-platform")
    yield rostore
    rostore.close()


def _unwrap(text: str) -> str:
    """The body an ``untrusted_wrap``\\ ed field carries, which is what the
    renderer shows and therefore what a word-count assertion must count."""
    assert text.startswith(UNTRUSTED_OPEN) and text.endswith(UNTRUSTED_CLOSE), text[:80]
    return text[len(UNTRUSTED_OPEN):-len(UNTRUSTED_CLOSE)].strip()


# ---------------------------------------------------------------------------
# the traced fixture -- one claim with something to say about every region
# ---------------------------------------------------------------------------

#: >20 words, so the fence's D-COC-1 cap is observable rather than a no-op.
LONG_QUOTE = " ".join(f"word{i:02d}" for i in range(40))


@pytest.fixture()
def traced(program_root, platform_root):
    """One open source and one ``commercial_restricted`` one; a claim with a
    primary anchor, an extra anchor on a SECOND chunk, and a third anchor
    whose document has since been re-ingested (``doc_sha256`` no longer
    matches); a ``contracrow`` verdict against the claim; two relations
    anchored on the claim's own evidence; a second claim sharing the primary
    anchor and a third sharing only the document; and a superseded ancestor.
    """
    store = open_store(program_root, platform_root=platform_root)
    base = populate_one_of_everything(store)
    launch = base["launch"]
    ids: dict[str, str] = {}

    def src(tier: str) -> str:
        sid = new_id("SRC")
        store_insert(store, "source", {
            "source_id": sid, "kind": "paper", "title": f"{tier} source", "license_tier": tier,
            "acquisition_route": "web", "request_state": "indexed", "registered_ts": now(),
            "registered_by_launch": launch,
        })
        return sid

    def doc(source_id: str, sha: str) -> str:
        did = new_id("DOC")
        store_insert(store, "document", {
            "doc_id": did, "source_id": source_id, "rel_path": f"archive/{did}.md", "media_type": "pdf",
            "normalizer_id": "pdf-text", "normalizer_version": "1", "sha256": sha, "status": "indexed",
        })
        return did

    def chunk(doc_id: str, seq: int, text: str) -> str:
        eid = new_id("ELM")
        store_insert(store, "element", {"element_id": eid, "doc_id": doc_id, "seq": seq, "type": "NarrativeText", "text": text})
        cid = new_id("CHK")
        store_insert(store, "chunk", {
            "chunk_id": cid, "doc_id": doc_id, "seq": seq, "text": text, "token_count": len(text.split()),
            "element_first": eid, "element_last": eid, "sha256": sha256_hex(text), "chunker_id": "two-pass",
            "chunker_version": "1", "created_ts": now(),
        })
        return cid

    def anchor(doc_id: str, chunk_id: str, quote: str, *, doc_sha: str, page: int = 1) -> str:
        aid = new_id("ANC")
        store_insert(store, "quote_anchor", {
            "anchor_id": aid, "doc_id": doc_id, "chunk_id": chunk_id, "page_number": page,
            "char_start": 0, "char_end": len(quote), "doc_sha256": doc_sha,
            "quote_sha256": sha256_hex(quote), "quote_text": quote,
            "created_by_launch": launch, "created_ts": now(),
        })
        return aid

    def claim(text: str, anchor_id: str, *, extra: list[str] | None = None, kind: str = "finding", ts: str | None = None) -> str:
        cid = new_id("CLM")
        store_insert(store, "claim", {
            "claim_id": cid, "text": text, "kind": kind, "confidence": 0.8, "anchor_id": anchor_id,
            "extra_anchors": json.dumps(extra) if extra else None,
            "created_at": ts or now(), "created_by_launch": launch,
        })
        return cid

    ids["source_open"] = src("open")
    ids["source_fenced"] = src("commercial_restricted")

    live_sha, stale_now_sha = "a" * 64, "b" * 64
    ids["doc_main"] = doc(ids["source_open"], live_sha)
    ids["doc_stale"] = doc(ids["source_open"], stale_now_sha)
    ids["doc_fenced"] = doc(ids["source_fenced"], live_sha)

    ids["chunk_main"] = chunk(ids["doc_main"], 1, "the main chunk body")
    ids["chunk_second"] = chunk(ids["doc_main"], 2, "the second chunk body")
    ids["chunk_stale"] = chunk(ids["doc_stale"], 1, "the stale chunk body")
    ids["chunk_fenced"] = chunk(ids["doc_fenced"], 1, LONG_QUOTE)

    ids["anchor_primary"] = anchor(ids["doc_main"], ids["chunk_main"], "grounding quote one", doc_sha=live_sha)
    ids["anchor_extra"] = anchor(ids["doc_main"], ids["chunk_second"], "grounding quote two", doc_sha=live_sha, page=2)
    # The stale one: recorded against the sha the document had at ingest, and
    # the document has moved on since.
    ids["anchor_stale"] = anchor(ids["doc_stale"], ids["chunk_stale"], "grounding quote three", doc_sha="c" * 64)
    ids["anchor_fenced"] = anchor(ids["doc_fenced"], ids["chunk_fenced"], LONG_QUOTE, doc_sha=live_sha)
    # An anchor with no quote_text at all -- the "not re-checkable" third state.
    ids["anchor_no_quote"] = new_id("ANC")
    store_insert(store, "quote_anchor", {
        "anchor_id": ids["anchor_no_quote"], "doc_id": ids["doc_main"], "chunk_id": ids["chunk_main"],
        "page_number": 3, "char_start": 5, "char_end": 9, "doc_sha256": live_sha,
        "quote_sha256": sha256_hex("nope"), "quote_text": None,
        "created_by_launch": launch, "created_ts": now(),
    })

    ids["claim_old"] = claim("the ancestor claim", ids["anchor_primary"], ts="2026-01-01T00:00:00.000Z")
    ids["claim"] = claim(
        "the traced claim, which stands on three anchors",
        ids["anchor_primary"],
        extra=[ids["anchor_extra"], ids["anchor_stale"], ids["anchor_no_quote"]],
        ts="2026-02-01T00:00:00.000Z",
    )
    store_update(store, "claim", pk_column="claim_id", pk_value=ids["claim_old"],
                 changes={"superseded_by": ids["claim"]})
    # shares the PRIMARY anchor
    ids["claim_same_anchor"] = claim("a sibling claim on the same anchor", ids["anchor_primary"],
                                     ts="2026-01-15T00:00:00.000Z")
    # shares only the DOCUMENT (its own anchor is a third one on doc_main)
    ids["anchor_doc_only"] = anchor(ids["doc_main"], chunk(ids["doc_main"], 3, "a third chunk body"),
                                    "grounding quote four", doc_sha=live_sha)
    ids["claim_same_doc"] = claim("a claim elsewhere in the same document", ids["anchor_doc_only"],
                                  ts="2026-01-20T00:00:00.000Z")
    # a fenced claim, for the <=20-word assertion
    ids["claim_fenced"] = claim(LONG_QUOTE, ids["anchor_fenced"], ts="2026-01-10T00:00:00.000Z")

    ids["verdict_contra"] = new_id("VRD")
    store_insert(store, "verdict", {
        "verdict_id": ids["verdict_contra"], "subject_kind": "claim", "subject_id": ids["claim"],
        "procedure": "contracrow", "procedure_version": "2", "label": "CONTRADICTED",
        "evidence": "[]", "prereg_compliant": 1, "ts": now(), "issued_by_launch": launch,
    })

    def entity(name: str) -> str:
        eid = new_id("ENT")
        store_insert(store, "entity", {
            "entity_id": eid, "name": name, "entity_type": "concept", "resolution": "confirmed",
            "created_by_launch": launch, "created_at": now(),
        })
        return eid

    ids["entity_a"], ids["entity_b"], ids["entity_c"] = entity("Alpha"), entity("Beta"), entity("Gamma")
    for key, srce, dst, anch in (
        ("rel_1", ids["entity_a"], ids["entity_b"], ids["anchor_primary"]),
        ("rel_2", ids["entity_b"], ids["entity_c"], ids["anchor_extra"]),
    ):
        rid = new_id("REL")
        ids[key] = rid
        store_insert(store, "relation", {
            "rel_id": rid, "src_entity": srce, "dst_entity": dst, "rel_type": "relates_to",
            "fact_text": f"{srce} relates_to {dst}", "evidence_anchor": anch,
            "created_at": now(),
        })

    store.close()
    rostore = open_store_ro(program_root, platform_root=platform_root)
    yield rostore, ids
    rostore.close()


# ---------------------------------------------------------------------------
# the ok / not_initialized pair every builder owes (spec section 6)
# ---------------------------------------------------------------------------


def test_evidence_panel_ok_on_one_of_everything(seeded):
    rostore, ids = seeded
    panel = data.build_evidence_panel(rostore)
    assert panel["status"] == "ok"
    assert panel["active_claim_id"] == ids["claim"]
    assert [c["claim_id"] for c in panel["index"]] == [ids["claim"]]
    assert panel["index_total"] == 1
    assert panel["index_truncated"] is False
    assert [a["anchor_id"] for a in panel["anchors"]] == [ids["quote_anchor"]]


def test_evidence_panel_not_initialized(empty_rostore):
    panel = data.build_evidence_panel(empty_rostore)
    assert panel["status"] == "not_initialized"
    assert "knowledge.db" in panel["message"]


def test_build_all_panels_includes_evidence_with_the_newest_claim(seeded):
    rostore, ids = seeded
    panels = data.build_all_panels(rostore)
    assert "evidence" in panels
    assert panels["evidence"]["status"] == "ok"
    assert panels["evidence"]["active_claim_id"] == ids["claim"]


# ---------------------------------------------------------------------------
# WHAT IT STANDS ON
# ---------------------------------------------------------------------------


def test_anchors_carry_role_provenance_and_both_hash_chips(traced):
    rostore, ids = traced
    panel = data.build_evidence_panel(rostore, claim_id=ids["claim"])
    assert panel["active_claim_id"] == ids["claim"]
    by_id = {a["anchor_id"]: a for a in panel["anchors"]}
    assert [a["anchor_id"] for a in panel["anchors"]] == [
        ids["anchor_primary"], ids["anchor_extra"], ids["anchor_stale"], ids["anchor_no_quote"]
    ], "primary first, then extra_anchors in their stored order"
    assert by_id[ids["anchor_primary"]]["role"] == "primary"
    assert by_id[ids["anchor_extra"]]["role"] == "extra"

    primary = by_id[ids["anchor_primary"]]
    assert primary["doc_sha_matches"] is True
    assert primary["quote_sha_matches"] is True
    assert primary["source_id"] == ids["source_open"]
    assert primary["source_title"] == "open source"
    assert primary["license_tier"] == "open"
    assert primary["page"] == 1
    assert primary["char_start"] == 0 and primary["char_end"] == len("grounding quote one")
    assert primary["quote"] == "grounding quote one"
    assert primary["fenced"] is False

    # the document moved under this anchor after it was written
    assert by_id[ids["anchor_stale"]]["doc_sha_matches"] is False
    assert by_id[ids["anchor_stale"]]["quote_sha_matches"] is True

    # no quote_text stored -> the THIRD reading, not a failed check
    assert by_id[ids["anchor_no_quote"]]["quote_sha_matches"] is None
    assert by_id[ids["anchor_no_quote"]]["doc_sha_matches"] is True


def test_a_claim_naming_a_missing_anchor_says_which_one(traced, program_root, platform_root):
    """A broken FK is a reading on the row, not a blank panel."""
    rostore, ids = traced
    store = open_store(program_root, platform_root=platform_root)
    ghost = "ANC-0000000000000000000000000"
    cid = new_id("CLM")
    store_insert(store, "claim", {
        "claim_id": cid, "text": "a claim with a dangling extra anchor", "kind": "finding",
        "anchor_id": ids["anchor_primary"], "extra_anchors": json.dumps([ghost]),
        "created_at": now(), "created_by_launch": ids.get("launch") or
        store.platform.execute("SELECT launch_id FROM launch LIMIT 1").fetchone()[0],
    })
    store.close()
    rostore.close()
    rostore = open_store_ro(program_root, platform_root=platform_root)
    try:
        panel = data.build_evidence_panel(rostore, claim_id=cid)
        rows = {a["anchor_id"]: a for a in panel["anchors"]}
        assert rows[ghost]["missing"] is True
        assert rows[ghost]["doc_sha_matches"] is False
        assert rows[ids["anchor_primary"]]["missing"] is False
    finally:
        rostore.close()


def test_a_fenced_source_caps_the_quote_and_the_claim_body_at_twenty_words(traced):
    """D-COC-1 through the engine's own ``citation_quote``: the fence is not
    re-implemented here, it is CALLED, so the cap cannot drift from the one
    every other serving path applies."""
    rostore, ids = traced
    panel = data.build_evidence_panel(rostore, claim_id=ids["claim_fenced"])
    assert panel["claim"]["fenced"] is True
    body = _unwrap(panel["claim"]["text"])
    assert len(body.split()) <= 20
    assert body.split()[0] == "word00"

    anchor = panel["anchors"][0]
    assert anchor["fenced"] is True
    assert anchor["license_tier"] == "commercial_restricted"
    assert len(anchor["quote"].split()) <= 20
    # and the index label is capped by the same rule
    row = next(c for c in panel["index"] if c["claim_id"] == ids["claim_fenced"])
    assert len(row["text_short"].split()) <= 20


def test_an_open_claim_body_is_untrusted_wrapped_and_not_word_capped(traced):
    rostore, ids = traced
    panel = data.build_evidence_panel(rostore, claim_id=ids["claim"])
    assert panel["claim"]["fenced"] is False
    assert _unwrap(panel["claim"]["text"]) == "the traced claim, which stands on three anchors"


# ---------------------------------------------------------------------------
# WHAT ARGUES WITH IT
# ---------------------------------------------------------------------------


def test_argues_reports_the_contracrow_verdict_and_an_honestly_empty_prov_edge(traced):
    rostore, ids = traced
    argues = data.build_evidence_panel(rostore, claim_id=ids["claim"])["argues"]
    assert [v["verdict_id"] for v in argues["verdicts"]] == [ids["verdict_contra"]]
    v = argues["verdicts"][0]
    assert v["procedure"] == "contracrow"
    assert v["procedure_version"] == "2"
    assert v["label"] == "CONTRADICTED", "the label is carried verbatim, never re-worded"
    assert v["prereg_compliant"] == 1
    assert argues["contradicts"] == [] and argues["supports"] == []
    assert "prov_edge has zero writers" in argues["note"]


def test_argues_reads_prov_edge_when_a_row_is_there(traced, program_root, platform_root):
    """The table has no writer in this codebase -- which is a fact about
    today, not a licence to skip the read. Written directly here so the read
    itself is covered rather than only its emptiness."""
    rostore, ids = traced
    store = open_store(program_root, platform_root=platform_root)
    edge_id = new_id("PVE")
    store_insert(store, "prov_edge", {
        "edge_id": edge_id, "src_kind": "claim", "src_id": ids["claim_same_anchor"],
        "dst_kind": "claim", "dst_id": ids["claim"], "role": "contradicts", "ts": now(),
    })
    store.close()
    rostore.close()
    rostore = open_store_ro(program_root, platform_root=platform_root)
    try:
        argues = data.build_evidence_panel(rostore, claim_id=ids["claim"])["argues"]
        assert [e["edge_id"] for e in argues["contradicts"]] == [edge_id]
        assert argues["supports"] == []
    finally:
        rostore.close()


# ---------------------------------------------------------------------------
# co-anchored claims, lineage, neighbourhood
# ---------------------------------------------------------------------------


def test_co_anchored_claims_rank_anchor_over_chunk_over_document(traced):
    rostore, ids = traced
    panel = data.build_evidence_panel(rostore, claim_id=ids["claim"])
    shared = {c["claim_id"]: c["shared"] for c in panel["co_anchored_claims"]}
    assert shared[ids["claim_same_anchor"]] == "anchor"
    assert shared[ids["claim_old"]] == "anchor"
    assert shared[ids["claim_same_doc"]] == "document"
    assert ids["claim"] not in shared, "a claim is never co-anchored with itself"
    assert ids["claim_fenced"] not in shared, "a different document is not shared evidence"
    ranks = [{"anchor": 0, "chunk": 1, "document": 2}[c["shared"]] for c in panel["co_anchored_claims"]]
    assert ranks == sorted(ranks)


def test_lineage_reports_both_directions_of_supersession(traced):
    rostore, ids = traced
    panel = data.build_evidence_panel(rostore, claim_id=ids["claim"])
    assert panel["lineage"] == {"superseded_by": None, "supersedes": [ids["claim_old"]]}
    older = data.build_evidence_panel(rostore, claim_id=ids["claim_old"])
    assert older["lineage"] == {"superseded_by": ids["claim"], "supersedes": []}
    assert older["claim"]["superseded_by"] == ids["claim"]
    row = next(c for c in panel["index"] if c["claim_id"] == ids["claim_old"])
    assert row["superseded"] is True


def test_neighbourhood_seeds_from_the_claims_own_anchors_and_fences_every_fact(traced):
    rostore, ids = traced
    n = data.build_evidence_panel(rostore, claim_id=ids["claim"])["neighbourhood"]
    seeds = {s["entity_id"] for s in n["seed_entities"]}
    assert seeds == {ids["entity_a"], ids["entity_b"], ids["entity_c"]}
    assert all(s["via_anchor"] in (ids["anchor_primary"], ids["anchor_extra"]) for s in n["seed_entities"])
    assert n["seeds_dropped"] == 0
    assert {e["rel_id"] for e in n["edges"]} == {ids["rel_1"], ids["rel_2"]}
    assert n["edge_count"] == 2 and n["edges_listed"] == 2
    assert n["max_hops"] == 2 and n["hops_reached"] >= 1
    assert n["hop_limit"] >= 1 and n["truncated"] is False
    # the engine's fence, inherited rather than re-implemented
    for e in n["edges"]:
        assert e["fact_text"].startswith(UNTRUSTED_OPEN)
        assert e["fenced"] is False
    kinds = {node["kind"] for node in n["nodes"]}
    assert kinds == {"claim", "entity"}
    assert n["nodes"][0] == {"id": ids["claim"], "kind": "claim", "label": ids["claim"]}
    assert n["node_count"] == len(n["nodes"])
    labels = {node["label"] for node in n["nodes"] if node["kind"] == "entity"}
    assert labels == {"Alpha", "Beta", "Gamma"}


def test_neighbourhood_is_empty_and_says_so_when_no_relation_touches_the_evidence(traced):
    rostore, ids = traced
    n = data.build_evidence_panel(rostore, claim_id=ids["claim_fenced"])["neighbourhood"]
    assert n["seed_entities"] == [] and n["edges"] == []
    assert n["edge_count"] == 0 and n["seeds_dropped"] == 0
    assert [node["kind"] for node in n["nodes"]] == ["claim"]


def test_neighbourhood_caps_its_seeds_and_reports_the_ones_it_dropped(traced, program_root, platform_root):
    rostore, ids = traced
    store = open_store(program_root, platform_root=platform_root)
    launch = store.platform.execute("SELECT launch_id FROM launch LIMIT 1").fetchone()[0]
    extra_entities = []
    for i in range(6):
        eid = new_id("ENT")
        extra_entities.append(eid)
        store_insert(store, "entity", {
            "entity_id": eid, "name": f"Extra {i}", "entity_type": "concept", "resolution": "confirmed",
            "created_by_launch": launch, "created_at": now(),
        })
        store_insert(store, "relation", {
            "rel_id": new_id("REL"), "src_entity": eid, "dst_entity": eid, "rel_type": "relates_to",
            "fact_text": f"Extra {i} relates_to itself", "evidence_anchor": ids["anchor_primary"],
            "created_at": now(),
        })
    store.close()
    rostore.close()
    rostore = open_store_ro(program_root, platform_root=platform_root)
    try:
        n = data.build_evidence_panel(rostore, claim_id=ids["claim"])["neighbourhood"]
        assert len(n["seed_entities"]) == data.EVIDENCE_MAX_SEEDS
        assert n["seeds_dropped"] == 9 - data.EVIDENCE_MAX_SEEDS
    finally:
        rostore.close()


# ---------------------------------------------------------------------------
# the IN-list bound (lane C, finding F3)
# ---------------------------------------------------------------------------


def _crowd_the_chunk(program_root, platform_root, chunk_id, doc_id, launch, n):
    """``n`` further anchors on one chunk, written straight through the
    connection: this is a SCALE fixture, and 1,100 rows through the validated
    write API would pay its XID check 1,100 times for no extra coverage."""
    store = open_store(program_root, platform_root=platform_root)
    try:
        rows = [
            (f"ANC-crowd{i:05d}", doc_id, chunk_id, 1, 0, 4, "a" * 64, sha256_hex("q"), "q", launch, now())
            for i in range(n)
        ]
        store.knowledge.executemany(
            "INSERT INTO quote_anchor (anchor_id, doc_id, chunk_id, page_number, char_start, char_end, "
            "doc_sha256, quote_sha256, quote_text, created_by_launch, created_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        store.knowledge.commit()
    finally:
        store.close()


def test_a_chunk_with_more_anchors_than_sqlite_takes_parameters_still_renders(
    traced, program_root, platform_root, monkeypatch
):
    """F3. Every anchor sharing a chunk with the claim joins the
    neighbourhood's anchor pool, and the pool used to bind ONE SQL PARAMETER
    PER ANCHOR. Past ``SQLITE_LIMIT_VARIABLE_NUMBER`` that raises ``too many
    SQL variables``, which ``isolated_panel`` turns into an Evidence panel
    permanently reading ``{"status": "error"}``: visible on the page, and
    undiagnosable from it.

    The real limit is 32,766 on this host's SQLite and 999 on builds before
    3.32, so a test that reproduced by row count alone would either be slow
    or would pass on the wrong build for the wrong reason. Instead the
    CONNECTION'S OWN limit is lowered (``Connection.setlimit``, 3.11+), which
    is the same knob SQLite compiles in -- and asserted to bite, so this
    cannot quietly stop testing anything."""
    rostore, ids = traced
    launch = rostore.platform.execute("SELECT launch_id FROM launch LIMIT 1").fetchone()[0]
    rostore.close()
    _crowd_the_chunk(program_root, platform_root, ids["chunk_main"], ids["doc_main"], launch, 300)

    monkeypatch.setattr(data, "_SQL_MAX_IN_PARAMS", 50)
    rostore = open_store_ro(program_root, platform_root=platform_root)
    try:
        rostore.knowledge.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 60)
        # the limit really is in force: the pre-fix query shape raises here
        with pytest.raises(sqlite3.OperationalError, match="too many SQL variables"):
            rostore.knowledge.execute(
                "SELECT anchor_id FROM quote_anchor WHERE anchor_id IN (%s)" % ",".join(["?"] * 300),
                [f"ANC-crowd{i:05d}" for i in range(300)],
            )

        panel = data.build_evidence_panel(rostore, claim_id=ids["claim"])
        assert panel["status"] == "ok"
        # the region is still the real one -- the two seeded relations are
        # anchored on this claim's own anchors and must survive the batching
        assert {e["rel_id"] for e in panel["neighbourhood"]["edges"]} >= {ids["rel_1"], ids["rel_2"]}
        # and the chunk_id entry point, whose own IN list is the same shape
        by_chunk = data.build_evidence_panel(rostore, chunk_id=ids["chunk_main"])
        assert by_chunk["status"] == "ok"
        assert by_chunk["active_claim_id"] is not None
    finally:
        rostore.close()


def test_batching_the_in_lists_does_not_change_the_answer(traced, monkeypatch):
    """Splitting an ``IN`` list means several queries, so any ORDER BY inside
    one orders within a batch and not across them -- which is why the callers
    re-sort. Proven by running each entry point at a batch size of 1 and
    comparing against the unbatched answer, field for field."""
    rostore, ids = traced
    unbatched = {
        "claim": data.build_evidence_panel(rostore, claim_id=ids["claim"]),
        "chunk": data.build_evidence_panel(rostore, chunk_id=ids["chunk_main"]),
        "anchor": data.build_evidence_panel(rostore, anchor_id=ids["anchor_primary"]),
    }
    monkeypatch.setattr(data, "_SQL_MAX_IN_PARAMS", 1)
    assert data.build_evidence_panel(rostore, claim_id=ids["claim"]) == unbatched["claim"]
    assert data.build_evidence_panel(rostore, chunk_id=ids["chunk_main"]) == unbatched["chunk"]
    assert data.build_evidence_panel(rostore, anchor_id=ids["anchor_primary"]) == unbatched["anchor"]


def test_rows_in_batches_splits_and_returns_every_row(traced):
    """The helper itself: N ids at batch size B is ceil(N/B) queries, and the
    union is exactly what one query would have returned."""
    rostore, ids = traced
    conn = rostore.knowledge
    anchor_ids = [r["anchor_id"] for r in conn.execute("SELECT anchor_id FROM quote_anchor").fetchall()]
    assert len(anchor_ids) >= 5, "the fixture must have enough anchors for this to mean anything"

    sql = "SELECT anchor_id FROM quote_anchor WHERE anchor_id IN ({placeholders})"
    one_shot = {r["anchor_id"] for r in data._rows_in_batches(conn, sql, anchor_ids, batch_size=len(anchor_ids))}
    split = {r["anchor_id"] for r in data._rows_in_batches(conn, sql, anchor_ids, batch_size=2)}
    assert split == one_shot == set(anchor_ids)
    assert data._rows_in_batches(conn, sql, [], batch_size=2) == []


# ---------------------------------------------------------------------------
# the three entry points, and the not_found reading
# ---------------------------------------------------------------------------


def test_anchor_id_entry_point_lands_on_the_anchored_claim(traced):
    """The TRACE from a search-result row: results carry
    ``citation.anchor.anchor_id`` and nothing else about a claim."""
    rostore, ids = traced
    panel = data.build_evidence_panel(rostore, anchor_id=ids["anchor_primary"])
    assert panel["active_claim_id"] == ids["claim"], "newest live claim on that anchor"
    assert "not_found" not in panel


def test_anchor_id_matches_a_claim_that_holds_it_only_as_an_extra(traced):
    rostore, ids = traced
    panel = data.build_evidence_panel(rostore, anchor_id=ids["anchor_extra"])
    assert panel["active_claim_id"] == ids["claim"]


def test_anchor_id_membership_is_the_decoded_list_not_a_substring(traced, program_root, platform_root):
    """``extra_anchors LIKE '%id%'`` is a prefilter. A shorter id that happens
    to be a substring of a stored one must not claim the row."""
    rostore, ids = traced
    store = open_store(program_root, platform_root=platform_root)
    launch = store.platform.execute("SELECT launch_id FROM launch LIMIT 1").fetchone()[0]
    prefix = ids["anchor_extra"][:-3]
    store_insert(store, "quote_anchor", {
        "anchor_id": prefix, "doc_id": ids["doc_main"], "chunk_id": ids["chunk_main"], "page_number": 9,
        "char_start": 0, "char_end": 4, "doc_sha256": "a" * 64, "quote_sha256": sha256_hex("frag"),
        "quote_text": "frag", "created_by_launch": launch, "created_ts": now(),
    })
    store.close()
    rostore.close()
    rostore = open_store_ro(program_root, platform_root=platform_root)
    try:
        panel = data.build_evidence_panel(rostore, anchor_id=prefix)
        assert panel["active_claim_id"] is None
        assert panel["not_found"] == {"kind": "anchor_id", "id": prefix}
    finally:
        rostore.close()


def test_chunk_id_entry_point(traced):
    rostore, ids = traced
    panel = data.build_evidence_panel(rostore, chunk_id=ids["chunk_second"])
    assert panel["active_claim_id"] == ids["claim"], "reached through an extra anchor on that chunk"
    assert data.build_evidence_panel(rostore, chunk_id=ids["chunk_main"])["active_claim_id"] == ids["claim"]


def test_selector_precedence_is_claim_then_anchor_then_chunk(traced):
    rostore, ids = traced
    panel = data.build_evidence_panel(
        rostore, claim_id=ids["claim_fenced"], anchor_id=ids["anchor_primary"], chunk_id=ids["chunk_main"]
    )
    assert panel["active_claim_id"] == ids["claim_fenced"]


@pytest.mark.parametrize(
    "kwargs,kind,bad",
    [
        ({"claim_id": "CLM-nope"}, "claim_id", "CLM-nope"),
        ({"anchor_id": "ANC-nope"}, "anchor_id", "ANC-nope"),
        ({"chunk_id": "CHK-nope"}, "chunk_id", "CHK-nope"),
    ],
)
def test_an_unknown_id_is_a_reading_never_a_refusal(traced, kwargs, kind, bad):
    rostore, _ids = traced
    panel = data.build_evidence_panel(rostore, **kwargs)
    assert panel["status"] == "ok"
    assert panel["active_claim_id"] is None
    assert panel["claim"] is None
    assert panel["not_found"] == {"kind": kind, "id": bad}
    assert panel["index"], "the rail still renders -- only the selection failed"


def test_an_explicitly_named_expired_claim_is_still_shown(traced, program_root, platform_root):
    rostore, ids = traced
    store = open_store(program_root, platform_root=platform_root)
    store_update(store, "claim", pk_column="claim_id", pk_value=ids["claim_same_anchor"],
                 changes={"expired_at": now()})
    store.close()
    rostore.close()
    rostore = open_store_ro(program_root, platform_root=platform_root)
    try:
        panel = data.build_evidence_panel(rostore, claim_id=ids["claim_same_anchor"])
        assert panel["active_claim_id"] == ids["claim_same_anchor"]
        assert panel["claim"]["expired_at"] is not None
        assert ids["claim_same_anchor"] not in {c["claim_id"] for c in panel["index"]}, \
            "the rail is the live view; the detail is what you asked for"
    finally:
        rostore.close()


# ---------------------------------------------------------------------------
# the rail
# ---------------------------------------------------------------------------


def test_index_is_capped_and_says_how_much_it_is_not_showing(traced, program_root, platform_root):
    rostore, ids = traced
    store = open_store(program_root, platform_root=platform_root)
    launch = store.platform.execute("SELECT launch_id FROM launch LIMIT 1").fetchone()[0]
    before = data.build_evidence_panel(rostore, claim_id=ids["claim"])["index_total"]
    for i in range(data.EVIDENCE_INDEX_LIMIT + 1 - before):
        store_insert(store, "claim", {
            "claim_id": new_id("CLM"), "text": f"bulk claim {i}", "kind": "finding",
            "anchor_id": ids["anchor_primary"], "created_at": now(), "created_by_launch": launch,
        })
    store.close()
    rostore.close()
    rostore = open_store_ro(program_root, platform_root=platform_root)
    try:
        panel = data.build_evidence_panel(rostore)
        assert panel["index_total"] == data.EVIDENCE_INDEX_LIMIT + 1
        assert len(panel["index"]) == data.EVIDENCE_INDEX_LIMIT
        assert panel["index_truncated"] is True
    finally:
        rostore.close()


def test_index_rows_carry_their_source_and_anchor_count(traced):
    rostore, ids = traced
    panel = data.build_evidence_panel(rostore, claim_id=ids["claim"])
    row = next(c for c in panel["index"] if c["claim_id"] == ids["claim"])
    assert row["source_id"] == ids["source_open"]
    assert row["source_title"] == "open source"
    assert row["anchor_count"] == 4
    assert row["kind"] == "finding"
    assert row["confidence"] == 0.8
    stamps = [c["created_at"] for c in panel["index"]]
    assert stamps == sorted(stamps, reverse=True), "newest live claim first"
    order = [c["claim_id"] for c in panel["index"]]
    assert order.index(ids["claim"]) < order.index(ids["claim_same_doc"]) < order.index(ids["claim_old"])


# ---------------------------------------------------------------------------
# L-C5: the term-conflict region is a hook, not an implementation
# ---------------------------------------------------------------------------


def test_the_term_conflict_region_is_omitted_with_its_reason_stated(traced):
    """Ruling L-C5: C6 ships the builder WITHOUT the lexicon region, and lane
    e wires it in behind an import guard. Absent means omitted-and-said-so,
    never an empty box that reads as "no conflicts"."""
    rostore, ids = traced
    panel = data.build_evidence_panel(rostore, claim_id=ids["claim"])
    assert "term_conflicts" not in panel
    assert panel["term_conflicts_omitted"]["reason"] == "awaiting_migration"
    assert "conflicts_for_claim" in panel["term_conflicts_omitted"]["message"]


def test_the_lexicon_hook_returns_none_while_the_module_is_absent(traced):
    rostore, ids = traced
    assert data._evidence_lexicon_conflicts(rostore, ids["claim"]) is None
