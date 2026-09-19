"""The vector index: the doctor check that sees it, the verb that rebuilds
it, and the hand-off defect that emptied it.

THE LIVE DEFECT (measured 2026-09-10). One program's ``knowledge.db`` held
16,173 chunks and 16,088 ``emb`` rows under its real embedding key, and
2,173 entries in that key's ``vec_chunks__*`` table. Semantic retrieval was
answering from 2,173 of 16,088 vectors; ``doctor`` said nothing, because
``embedding_missing`` (35 chunks, warn) and ``embedding_stale`` (none) both
read ``emb`` rows and NOTHING compared the index against them.

The cause was an id, not an exception. The ``index`` stage's job id was
``JOB-ingest-<doc_id>-index`` -- deterministic in the document and blind to
the model key -- and ``_enqueue_next_stage`` skips the create when a job
with that id already exists (crash-resume idempotency). So when the program
was re-pointed at a real model and every document re-embedded, each embed
stage handed off to an ``index`` job that had settled ``complete`` during
the PLACEHOLDER era: the emb rows landed, the hand-off evaporated, and
nothing raised. The live arithmetic is exact -- 14,000 chunks had been
indexed under the placeholder key, and 16,173 - 14,000 = 2,173 is precisely
what the real key's table held, those 2,173 being the chunks of documents
whose FIRST ingest happened after the switch (a fresh id, so a fresh job).

``test_a_key_blind_hand_off_id_*`` pins the mechanism, the two
``test_adopting_*`` tests pin the fix where the live rows actually came from
(a published offload embed result, adopted by the sandbox), and the rest
covers the check and the repair verb.

Everything is synthetic and domain-neutral: html/markdown fixtures, neutral
model-key names, no SSH and no GPU (the DEV side is a hand-built published
result, the sandbox side is the real ``run_one`` claim-run-settle loop).
"""

from __future__ import annotations

import json

import pytest

from trialerror.cli import ingest as cli_ingest
from trialerror.ingest import pipeline
from trialerror.ingest.backends import DEFAULT_FAKE_EMBED_DIMS
from trialerror.ingest.checks import check_embedding_stale, check_vector_index_stale
from trialerror.ingest.errors import (
    UnknownEmbedModelKeyError,
    VectorIndexDimsConflictError,
)
from trialerror.ingest.handlers import _enqueue_next_stage, index_job_id
from trialerror.ingest.reindex import REINDEX_EVENT_TYPE, reindex_vectors
from trialerror.jobs import ledger
from trialerror.jobs.worker import run_one
from trialerror.offload import protocol
from trialerror.stores.errors import XidTargetMissingError
from trialerror.stores.vecindex import (
    ensure_vec_table,
    safe_model_key,
    serialize_vector_fallback,
    vec_table_name,
)
from trialerror.util.doctor import DoctorContext
from trialerror.util.timeutil import now
from tests._ingest_fixtures import bootstrap_launch, write_html_fixture, write_markdown_fixture
from tests._offload_fixtures import publish_stub_result, write_offload_toml

#: What the default (placeholder) embed backend stamps on its rows -- the
#: "before" key in every key-switch scenario here.
PLACEHOLDER_KEY = f"fake-{DEFAULT_FAKE_EMBED_DIMS}"

#: A neutral stand-in for "the real model this program was re-pointed at".
SECOND_KEY = "mk-second-8"
SECOND_DIMS = 8


def _sqlite_vec_available() -> bool:
    import sqlite3

    from trialerror.stores.vecindex import try_load_sqlite_vec

    conn = sqlite3.connect(":memory:")
    try:
        return try_load_sqlite_vec(conn)
    finally:
        conn.close()


class _Args:
    """``argparse.Namespace`` stand-in for the CLI handlers (the shape
    ``tests/test_ingest_purge_embeddings.py`` uses)."""

    def __init__(self, **kw):
        self.program_root = None
        self.platform_root = None
        self.dry_run = False
        for k, v in kw.items():
            setattr(self, k, v)


# ---------------------------------------------------------------------------
# fixtures and small helpers
# ---------------------------------------------------------------------------
def _drain(store, max_steps=16):
    for i in range(max_steps):
        if run_one(store, worker_id=f"w{i}")["status"] == "idle":
            return


def _ingest(store, program_root, *, launch_id, name="doc.html", writer=write_html_fixture):
    raw_dir = program_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = writer(raw_dir / name)
    source_id = pipeline.register_source(
        store, kind="paper", title=f"Fixture {name}", license_tier="open",
        acquisition_route="web", registered_by_launch=launch_id,
    )["source_id"]
    return pipeline.add_document(
        store, program_root=program_root, source_id=source_id, raw_path=path,
        created_by_launch=launch_id,
    )["document"]["doc_id"]


def _emb_count(store, model_key):
    return int(
        store.knowledge.execute(
            "SELECT COUNT(*) FROM emb WHERE model_key = ?", (model_key,)
        ).fetchone()[0]
    )


def _vec_count(store, model_key):
    """Entry count, or ``None`` when the key has no table at all."""
    table = vec_table_name(model_key)
    exists = store.knowledge.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (table,)
    ).fetchone()
    if exists is None:
        return None
    return int(store.knowledge.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _chunk_ids(store, doc_id=None):
    sql = "SELECT chunk_id FROM chunk"
    params: tuple = ()
    if doc_id is not None:
        sql += " WHERE doc_id = ?"
        params = (doc_id,)
    return [r["chunk_id"] for r in store.knowledge.execute(sql + " ORDER BY chunk_id", params)]


def _write_embeddings(store, *, model_key, dims, index=True, doc_id=None):
    """The state a re-embed under a second key leaves: one ``emb`` row per
    distinct chunk hash, and (when ``index``) that key's own vector table
    filled. ``index=False`` is the live hole."""
    conn = store.knowledge
    sql = "SELECT chunk_id, sha256 FROM chunk"
    params: tuple = ()
    if doc_id is not None:
        sql += " WHERE doc_id = ?"
        params = (doc_id,)
    chunks = [dict(r) for r in conn.execute(sql, params).fetchall()]
    blob = serialize_vector_fallback([0.125] * dims)
    if index:
        ensure_vec_table(conn, model_key, dims)
        table = vec_table_name(model_key)
    with conn:
        for c in chunks:
            conn.execute(
                "INSERT OR REPLACE INTO emb(chunk_sha256, model_key, dims, vector, created_ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (c["sha256"], model_key, dims, blob, now()),
            )
            if index:
                conn.execute(
                    f"INSERT OR REPLACE INTO {table}(chunk_id, model_key, dims, vector) "
                    "VALUES (?, ?, ?, ?)",
                    (c["chunk_id"], model_key, dims, blob),
                )
    return len(chunks)


def _write_toml(program_root, *, model_key, dims):
    """A program whose ACTIVE embed key is ``model_key`` -- the offload
    branch, the one branch where the loader reads ``model_key`` from the
    config (see ``ingest.checks._embed_model_key``)."""
    (program_root / "trialerror.toml").write_text(
        '[program]\nid = "PROG-test"\n\n'
        f'[ingest.embed]\nbackend = "offload"\nmodel_key = "{model_key}"\ndims = {dims}\n',
        encoding="utf-8",
    )


@pytest.fixture()
def one_doc(store, program_root):
    """One document through the REAL pipeline on the placeholder backend:
    chunks, ``emb`` rows under ``fake-16``, and that key's vector table
    filled by the ``index`` stage."""
    launch_id = bootstrap_launch(store)
    doc_id = _ingest(store, program_root, launch_id=launch_id)
    _drain(store)
    assert _vec_count(store, PLACEHOLDER_KEY), "the index stage should have filled the first key"
    return launch_id, doc_id


def _ctx(program_root):
    return DoctorContext(program_root=program_root)


def _key_row(result, model_key):
    return next(k for k in result.details["keys"] if k["model_key"] == model_key)


# ---------------------------------------------------------------------------
# the root cause, pinned
# ---------------------------------------------------------------------------
def test_the_hand_off_id_names_the_model_key_the_index_stage_works_on():
    doc_id = "DOC-01JQZ8X4A7N6MCTVB9KDWF2HRY"
    assert index_job_id(doc_id, "qwen3-4b") == f"JOB-ingest-{doc_id}-index-qwen3_4b"
    # One sanitizer, so the job id and the table it fills spell the key the
    # same way -- two answers to "which key is this?" is how the first one
    # went wrong.
    assert vec_table_name("qwen3-4b").endswith(safe_model_key("qwen3-4b"))
    assert index_job_id(doc_id, "a/b c") == f"JOB-ingest-{doc_id}-index-a_b_c"


def test_a_key_blind_hand_off_id_is_silently_dropped(store):
    """The MECHANISM, in three lines: ``_enqueue_next_stage`` is a no-op when
    a job with that id exists, whatever state it settled in. That is correct
    for a resumed hand-off and catastrophic for a hand-off whose id does not
    say what changed."""
    launch_id = bootstrap_launch(store)
    payload = {"doc_id": "DOC-x", "created_by_launch": launch_id}
    legacy_id = "JOB-ingest-DOC-x-index"

    ledger.enqueue(store, kind="index", payload=payload, job_id=legacy_id)
    assert ledger.claim_specific(store, legacy_id, worker_id="w0") is not None
    ledger.complete(store, legacy_id, worker_id="w0")
    assert ledger.get_job(store, legacy_id)["state"] == "complete"

    # the old shape: a second stage, a different model key, the same id
    _enqueue_next_stage(store, stage="index", payload=payload, job_id=legacy_id)
    assert [r[0] for r in store.jobs.execute("SELECT job_id FROM job").fetchall()] == [legacy_id]

    # the shipped shape: the key is in the id, so the hand-off lands
    _enqueue_next_stage(
        store, stage="index", payload=payload, job_id=index_job_id("DOC-x", SECOND_KEY)
    )
    assert ledger.get_job(store, index_job_id("DOC-x", SECOND_KEY))["state"] == "pending"


# ---------------------------------------------------------------------------
# the regression: adopting a published offload embed result fills the index
# ---------------------------------------------------------------------------
def _publish_embed_vectors(root, job_id, *, model_key, dims):
    """Hand-build what the DEV GPU worker publishes for an ``embed`` job:
    one JSON vector per line, in the manifest's own ``chunk_ids`` order,
    plus the ``model_key``/``dims``/``chunk_ids`` block
    ``protocol.verify_published`` checks the result against."""
    manifest = protocol.read_json(protocol.pending_dir(root) / f"{job_id}.json")
    chunk_ids = manifest["expect"]["chunk_ids"]
    assert manifest["expect"]["model_key"] == model_key
    assert manifest["expect"]["dims"] == dims
    payload = "\n".join(json.dumps([0.125] * dims) for _ in chunk_ids).encode() + b"\n"
    publish_stub_result(
        root,
        job_id,
        outputs={"vectors.jsonl": payload},
        result_overrides={"model_key": model_key, "dims": dims, "chunk_ids": chunk_ids},
    )
    return chunk_ids


def _kick(program_root, platform_root):
    """``trialerror offload kick`` -- what un-delays a parked job once its
    result lands (the sandbox's jobs window runs it every cycle)."""
    from trialerror.cli import build_parser

    args = build_parser().parse_args(
        ["offload", "kick", "--program-root", str(program_root), "--platform-root", str(platform_root)]
    )
    return args.handler(args)


def test_adopting_an_offload_embed_result_fills_the_vector_index(
    store, program_root, platform_root
):
    """The path the live rows came in on: the sandbox parks the embed stage,
    DEV publishes vectors, the sandbox adopts them -- and the vector index
    has an entry for every chunk when the dust settles."""
    write_offload_toml(program_root, model_key=SECOND_KEY, dims=SECOND_DIMS)
    launch_id = bootstrap_launch(store)
    doc_id = _ingest(store, program_root, launch_id=launch_id)
    root = protocol.offload_root(program_root)

    assert run_one(store, worker_id="w0")["status"] == "complete"  # normalize (local)
    assert run_one(store, worker_id="w1")["status"] == "complete"  # chunk (local)
    # Lane FB-6 item 8: the full-text-only index job, enqueued by `chunk`
    # and run before `embed` -- this is the very shape the item is about,
    # a program whose embed stage is parked on another machine.
    assert run_one(store, worker_id="w1b")["status"] == "complete"  # index (full text)
    assert store.knowledge.execute(
        "SELECT COUNT(*) AS n FROM chunk_fts f JOIN chunk c ON c.chunk_id = f.chunk_id "
        "WHERE c.doc_id = ?",
        (doc_id,),
    ).fetchone()["n"] > 0, "the document is searchable while its embed sits on another machine"
    assert run_one(store, worker_id="w2")["status"] == "deferred"  # embed -> offload
    embed_job = f"JOB-ingest-{doc_id}-embed"
    assert protocol.list_pending(root) == [embed_job]

    chunk_ids = _publish_embed_vectors(root, embed_job, model_key=SECOND_KEY, dims=SECOND_DIMS)
    _kick(program_root, platform_root)
    assert run_one(store, worker_id="w3")["status"] == "complete"  # embed: ADOPTION
    assert run_one(store, worker_id="w4")["status"] == "complete"  # index

    assert _emb_count(store, SECOND_KEY) == len(chunk_ids) > 0
    indexed = [
        r[0]
        for r in store.knowledge.execute(
            f"SELECT chunk_id FROM {vec_table_name(SECOND_KEY)}"
        ).fetchall()
    ]
    assert sorted(indexed) == sorted(chunk_ids), "every adopted vector reached the index"
    assert check_vector_index_stale(_ctx(program_root)).status == "pass"


def test_adopting_under_a_second_model_key_still_fills_the_index(
    store, program_root, platform_root, one_doc
):
    """THE REGRESSION. The document has already been indexed once, under the
    placeholder key -- which is exactly the state 14,000 live chunks were in
    when the real model's vectors were adopted and silently went nowhere."""
    launch_id, doc_id = one_doc
    legacy_index_job = f"JOB-ingest-{doc_id}-index"
    assert ledger.get_job(store, index_job_id(doc_id, PLACEHOLDER_KEY))["state"] == "complete"
    assert ledger.get_job(store, legacy_index_job) is None, "the id now carries the key"

    write_offload_toml(program_root, model_key=SECOND_KEY, dims=SECOND_DIMS)
    requeued = pipeline.requeue_stage(
        store, doc_id=doc_id, kind="embed", created_by_launch=launch_id
    )
    root = protocol.offload_root(program_root)

    assert run_one(store, worker_id="r0")["status"] == "deferred"  # embed -> offload
    chunk_ids = _publish_embed_vectors(
        root, requeued["job_id"], model_key=SECOND_KEY, dims=SECOND_DIMS
    )
    _kick(program_root, platform_root)
    assert run_one(store, worker_id="r1")["status"] == "complete"  # embed: ADOPTION
    assert run_one(store, worker_id="r2")["status"] == "complete"  # index under the NEW key

    assert _emb_count(store, SECOND_KEY) == len(chunk_ids)
    assert _vec_count(store, SECOND_KEY) == len(chunk_ids), (
        "the second key's index is filled -- before the fix this was 0 and nothing said so"
    )
    # and the first key's index is untouched: both are registered, both complete
    assert _vec_count(store, PLACEHOLDER_KEY) == len(chunk_ids)
    assert check_vector_index_stale(_ctx(program_root)).status == "pass"


# ---------------------------------------------------------------------------
# the doctor check
# ---------------------------------------------------------------------------
def test_the_check_passes_when_every_registered_key_is_complete(store, program_root, one_doc):
    result = check_vector_index_stale(_ctx(program_root))
    assert result.status == "pass"
    assert result.category == "ingest"
    assert result.details["count"] == 0
    row = _key_row(result, PLACEHOLDER_KEY)
    assert row["emb_rows"] == row["vec_entries"] == row["chunks_with_embedding"]
    assert "complete" in result.message


def test_the_check_fails_for_the_configured_key_with_a_hole(store, program_root, one_doc):
    """The live finding: emb rows for the active key, an index that holds
    only some of them. FAIL, not warn -- this is the live search surface."""
    _write_toml(program_root, model_key=SECOND_KEY, dims=SECOND_DIMS)
    total = _write_embeddings(store, model_key=SECOND_KEY, dims=SECOND_DIMS, index=True)
    keep = _chunk_ids(store)[0]
    with store.knowledge:
        store.knowledge.execute(
            f"DELETE FROM {vec_table_name(SECOND_KEY)} WHERE chunk_id != ?", (keep,)
        )

    result = check_vector_index_stale(_ctx(program_root))
    assert result.status == "fail"
    assert result.details["active_model_key"] == SECOND_KEY
    row = _key_row(result, SECOND_KEY)
    assert row["vec_entries"] == 1
    assert row["chunks_missing_entry"] == total - 1
    assert row["missing_sample"] and keep not in row["missing_sample"]
    assert f"{SECOND_KEY} (active)" in result.message
    assert "not in the index" in result.message
    # the same hole is invisible to the neighbour that only reads emb rows
    assert check_embedding_stale(_ctx(program_root)).status == "pass"


def test_the_check_only_warns_for_a_superseded_keys_hole(store, program_root, one_doc):
    """A retired key's index is stale by definition -- reported, not failed;
    ``purge-embeddings`` is what retires it."""
    _write_toml(program_root, model_key=SECOND_KEY, dims=SECOND_DIMS)
    _write_embeddings(store, model_key=SECOND_KEY, dims=SECOND_DIMS, index=True)
    with store.knowledge:
        store.knowledge.execute(f"DELETE FROM {vec_table_name(PLACEHOLDER_KEY)}")

    result = check_vector_index_stale(_ctx(program_root))
    assert result.status == "warn"
    assert _key_row(result, PLACEHOLDER_KEY)["chunks_missing_entry"] > 0
    assert _key_row(result, SECOND_KEY)["chunks_missing_entry"] == 0


def test_the_check_counts_chunks_not_the_two_row_totals(store, program_root, one_doc):
    """The arithmetic trap this check refuses to fall into. ``emb`` is
    hash-addressed and the index is chunk-addressed, so a corpus with two
    chunks of identical text has MORE index entries than embedding rows and
    is nonetheless complete -- on the live program, 14,000 entries for
    13,950 rows. Subtracting totals would have read that as a surplus."""
    conn = store.knowledge
    twin = dict(conn.execute("SELECT * FROM chunk LIMIT 1").fetchone())
    twin["chunk_id"] = twin["chunk_id"] + "-TWIN"
    twin["seq"] = int(twin["seq"]) + 1000
    with conn:
        conn.execute(
            "INSERT INTO chunk(chunk_id, doc_id, seq, text, token_count, element_first, "
            "element_last, page_start, page_end, sha256, chunker_id, chunker_version, created_ts) "
            "VALUES (:chunk_id, :doc_id, :seq, :text, :token_count, :element_first, :element_last, "
            ":page_start, :page_end, :sha256, :chunker_id, :chunker_version, :created_ts)",
            twin,
        )
        blob = serialize_vector_fallback([0.125] * DEFAULT_FAKE_EMBED_DIMS)
        conn.execute(
            f"INSERT INTO {vec_table_name(PLACEHOLDER_KEY)}(chunk_id, model_key, dims, vector) "
            "VALUES (?, ?, ?, ?)",
            (twin["chunk_id"], PLACEHOLDER_KEY, DEFAULT_FAKE_EMBED_DIMS, blob),
        )

    result = check_vector_index_stale(_ctx(program_root))
    row = _key_row(result, PLACEHOLDER_KEY)
    assert row["vec_entries"] > row["emb_rows"], "the duplicate text shares one emb row"
    assert row["chunks_missing_entry"] == 0
    assert result.status == "pass"


def test_the_check_passes_on_a_program_that_has_never_indexed(store, program_root):
    result = check_vector_index_stale(_ctx(program_root))
    assert result.status == "pass"
    assert result.details["keys"] == []
    assert "no vector index registry" in result.message


def test_a_registered_key_whose_table_is_gone_reports_every_embedded_chunk(
    store, program_root, one_doc
):
    """Registered and missing is the same finding as registered and empty --
    every embedded chunk is unreachable -- and must not be skipped just
    because there is no table to count."""
    with store.knowledge:
        store.knowledge.execute(f"DROP TABLE {vec_table_name(PLACEHOLDER_KEY)}")
    result = check_vector_index_stale(_ctx(program_root))
    assert result.status == "fail"  # fake-16 is this program's configured key
    row = _key_row(result, PLACEHOLDER_KEY)
    assert row["vec_entries"] == 0
    assert row["chunks_missing_entry"] == row["chunks_with_embedding"] > 0


def test_the_check_skips_without_a_program_root():
    assert check_vector_index_stale(DoctorContext(program_root=None)).status == "skip"


def test_the_ingest_doctor_subcommand_runs_the_new_check(store, program_root, one_doc):
    env = cli_ingest._cmd_doctor(_Args(program_root=str(program_root)))
    names = [c["name"] for c in (env.get("result") or env["error"]["details"])["checks"]]
    assert "vector_index_stale" in names


# ---------------------------------------------------------------------------
# the rebuild
# ---------------------------------------------------------------------------
def test_the_rebuild_fills_a_half_empty_index(store, program_root, one_doc):
    launch_id, _doc_id = one_doc
    _write_toml(program_root, model_key=SECOND_KEY, dims=SECOND_DIMS)
    total = _write_embeddings(store, model_key=SECOND_KEY, dims=SECOND_DIMS, index=True)
    assert total >= 2
    keep = _chunk_ids(store)[0]
    with store.knowledge:
        store.knowledge.execute(
            f"DELETE FROM {vec_table_name(SECOND_KEY)} WHERE chunk_id != ?", (keep,)
        )
    assert check_vector_index_stale(_ctx(program_root)).status == "fail"

    result = reindex_vectors(store, model_key=SECOND_KEY, launch_id=launch_id)

    assert result["vec_rows_before"] == 1
    assert result["vec_rows_after"] == total
    assert _vec_count(store, SECOND_KEY) == total
    assert check_vector_index_stale(_ctx(program_root)).status == "pass"
    # the vectors themselves come from the emb rows, byte for byte
    pairs = store.knowledge.execute(
        f"SELECT v.vector AS indexed, e.vector AS embedded "
        f"FROM {vec_table_name(SECOND_KEY)} v JOIN chunk c ON c.chunk_id = v.chunk_id "
        "JOIN emb e ON e.chunk_sha256 = c.sha256 AND e.model_key = ?",
        (SECOND_KEY,),
    ).fetchall()
    assert len(pairs) == total
    assert all(bytes(r["indexed"]) == bytes(r["embedded"]) for r in pairs)


def test_the_rebuild_reports_the_documented_envelope_shape(store, program_root, one_doc):
    launch_id, _doc_id = one_doc
    result = reindex_vectors(store, model_key=PLACEHOLDER_KEY, launch_id=launch_id)
    assert set(result) == {"model_key", "emb_rows", "vec_rows_before", "vec_rows_after", "dry_run"}
    assert result["model_key"] == PLACEHOLDER_KEY
    assert result["dry_run"] is False


def test_the_rebuild_creates_the_table_for_a_key_that_was_never_indexed(
    store, program_root, one_doc
):
    """Exactly the live shape: emb rows adopted, no index at all. The verb
    creates the table, fills it, and registers it."""
    launch_id, _doc_id = one_doc
    total = _write_embeddings(store, model_key=SECOND_KEY, dims=SECOND_DIMS, index=False)
    assert _vec_count(store, SECOND_KEY) is None

    result = reindex_vectors(store, model_key=SECOND_KEY, launch_id=launch_id)

    assert result["vec_rows_before"] == 0
    assert result["vec_rows_after"] == total
    registry = dict(
        store.knowledge.execute(
            "SELECT * FROM vec_index_registry WHERE model_key = ?", (SECOND_KEY,)
        ).fetchone()
    )
    assert registry["table_name"] == vec_table_name(SECOND_KEY)
    assert registry["dims"] == SECOND_DIMS
    assert registry["backend"] == "fallback"


def test_the_rebuild_is_idempotent(store, program_root, one_doc):
    launch_id, _doc_id = one_doc
    first = reindex_vectors(store, model_key=PLACEHOLDER_KEY, launch_id=launch_id)
    second = reindex_vectors(store, model_key=PLACEHOLDER_KEY, launch_id=launch_id)
    assert second["vec_rows_before"] == second["vec_rows_after"] == first["vec_rows_after"]
    assert check_vector_index_stale(_ctx(program_root)).status == "pass"


def test_the_rebuild_writes_one_vectors_reindexed_event(store, program_root, one_doc):
    launch_id, _doc_id = one_doc
    reindex_vectors(store, model_key=PLACEHOLDER_KEY, launch_id=launch_id)
    rows = store.ops.execute(
        "SELECT * FROM event WHERE type = ?", (REINDEX_EVENT_TYPE,)
    ).fetchall()
    assert len(rows) == 1
    event = dict(rows[0])
    assert event["launch_id"] == launch_id
    payload = json.loads(event["payload"])
    assert payload["model_key"] == PLACEHOLDER_KEY
    assert payload["vec_table"] == vec_table_name(PLACEHOLDER_KEY)
    assert payload["dims"] == DEFAULT_FAKE_EMBED_DIMS
    assert payload["backend"] == "fallback"
    assert payload["vec_rows_after"] == _vec_count(store, PLACEHOLDER_KEY)


def test_a_dry_run_touches_nothing(store, program_root, one_doc):
    launch_id, _doc_id = one_doc
    total = _write_embeddings(store, model_key=SECOND_KEY, dims=SECOND_DIMS, index=False)

    result = reindex_vectors(store, model_key=SECOND_KEY, launch_id=launch_id, dry_run=True)

    assert result["dry_run"] is True
    assert result["vec_rows_before"] == 0
    assert result["vec_rows_after"] == total, "the projection the real run then produces"
    assert _vec_count(store, SECOND_KEY) is None, "no table created"
    assert (
        store.knowledge.execute(
            "SELECT COUNT(*) FROM vec_index_registry WHERE model_key = ?", (SECOND_KEY,)
        ).fetchone()[0]
        == 0
    ), "no registry row either"
    assert (
        store.ops.execute("SELECT COUNT(*) FROM event WHERE type = ?", (REINDEX_EVENT_TYPE,)).fetchone()[0]
        == 0
    )


def test_the_rebuild_refuses_an_unregistered_launch(store, program_root, one_doc):
    with pytest.raises(XidTargetMissingError):
        reindex_vectors(store, model_key=PLACEHOLDER_KEY, launch_id="LNCH-nope")
    with pytest.raises(XidTargetMissingError):
        reindex_vectors(store, model_key=PLACEHOLDER_KEY, launch_id="LNCH-nope", dry_run=True)


def test_the_rebuild_refuses_an_unknown_model_key(store, program_root, one_doc):
    launch_id, _doc_id = one_doc
    with pytest.raises(UnknownEmbedModelKeyError) as exc:
        reindex_vectors(store, model_key="mk-typo", launch_id=launch_id)
    assert PLACEHOLDER_KEY in str(exc.value), "the message names the keys the record carries"
    assert _vec_count(store, "mk-typo") is None


def test_the_rebuild_refuses_a_key_whose_rows_disagree_about_dims(store, program_root, one_doc):
    launch_id, _doc_id = one_doc
    _write_embeddings(store, model_key=SECOND_KEY, dims=SECOND_DIMS, index=False)
    sha = store.knowledge.execute("SELECT sha256 FROM chunk LIMIT 1").fetchone()[0]
    with store.knowledge:
        store.knowledge.execute(
            "UPDATE emb SET dims = ?, vector = ? WHERE model_key = ? AND chunk_sha256 = ?",
            (SECOND_DIMS * 2, serialize_vector_fallback([0.1] * (SECOND_DIMS * 2)), SECOND_KEY, sha),
        )
    with pytest.raises(VectorIndexDimsConflictError):
        reindex_vectors(store, model_key=SECOND_KEY, launch_id=launch_id)
    assert _vec_count(store, SECOND_KEY) is None, "refused before anything was created"


def test_the_rebuild_survives_an_emptied_key(store, program_root, one_doc):
    """A key whose ``emb`` rows have been purged but whose table is still
    registered: the rebuild empties the index rather than refusing, because
    the index is derived and the emb rows are the truth."""
    launch_id, _doc_id = one_doc
    with store.knowledge:
        store.knowledge.execute("DELETE FROM emb WHERE model_key = ?", (PLACEHOLDER_KEY,))
    result = reindex_vectors(store, model_key=PLACEHOLDER_KEY, launch_id=launch_id)
    assert result["emb_rows"] == 0
    assert result["vec_rows_after"] == 0
    assert _vec_count(store, PLACEHOLDER_KEY) == 0


@pytest.mark.skipif(
    not _sqlite_vec_available(), reason="sqlite-vec extension not installed in this environment"
)
def test_the_rebuild_fills_a_real_vec0_table(store, program_root, monkeypatch):
    """The other backend, on its own merits: a ``vec0`` virtual table has no
    ``model_key``/``dims`` columns, so the insert shape follows the TABLE
    rather than the configuration."""
    monkeypatch.setenv("TRIALERROR_VEC_BACKEND", "sqlite_vec")
    launch_id = bootstrap_launch(store)
    _ingest(store, program_root, launch_id=launch_id, name="vec0.md", writer=write_markdown_fixture)
    _drain(store)
    table = vec_table_name(PLACEHOLDER_KEY)
    columns = {r[1] for r in store.knowledge.execute(f"PRAGMA table_info({table})").fetchall()}
    assert "model_key" not in columns, "this program's table really is a vec0 one"
    with store.knowledge:
        store.knowledge.execute(f"DELETE FROM {table}")

    result = reindex_vectors(store, model_key=PLACEHOLDER_KEY, launch_id=launch_id)

    assert result["vec_rows_before"] == 0
    assert result["vec_rows_after"] == len(_chunk_ids(store))
    assert (
        dict(
            store.knowledge.execute(
                "SELECT * FROM vec_index_registry WHERE model_key = ?", (PLACEHOLDER_KEY,)
            ).fetchone()
        )["backend"]
        == "sqlite_vec"
    )


# ---------------------------------------------------------------------------
# the CLI surface
# ---------------------------------------------------------------------------
def test_the_cli_rebuilds_and_points_at_the_check(store, program_root, platform_root, one_doc):
    launch_id, _doc_id = one_doc
    _write_embeddings(store, model_key=SECOND_KEY, dims=SECOND_DIMS, index=False)
    store.close()

    env = cli_ingest._cmd_reindex_vectors(
        _Args(program_root=str(program_root), model_key=SECOND_KEY, launch_id=launch_id)
    )

    assert env["ok"], env
    assert env["result"]["vec_rows_after"] > 0
    assert env["nextActions"][0]["argv"][-1] == "vector_index_stale"


def test_the_cli_dry_run_offers_the_real_run(store, program_root, one_doc):
    launch_id, _doc_id = one_doc
    store.close()
    env = cli_ingest._cmd_reindex_vectors(
        _Args(
            program_root=str(program_root),
            model_key=PLACEHOLDER_KEY,
            launch_id=launch_id,
            dry_run=True,
        )
    )
    assert env["ok"] and env["result"]["dry_run"] is True
    assert env["nextActions"][0]["argv"][:3] == ["trialerror", "ingest", "reindex-vectors"]


def test_the_cli_refusals_carry_their_own_codes(store, program_root, one_doc):
    launch_id, _doc_id = one_doc
    store.close()
    unknown = cli_ingest._cmd_reindex_vectors(
        _Args(program_root=str(program_root), model_key="mk-typo", launch_id=launch_id)
    )
    assert unknown["ok"] is False
    assert unknown["error"]["code"] == "unknown_embed_model_key"

    unbooked = cli_ingest._cmd_reindex_vectors(
        _Args(program_root=str(program_root), model_key=PLACEHOLDER_KEY, launch_id="LNCH-nope")
    )
    assert unbooked["ok"] is False
    assert unbooked["error"]["code"] == "XidTargetMissingError"


def test_the_verb_is_registered_on_the_parser():
    from trialerror.cli import build_parser

    args = build_parser().parse_args(
        ["ingest", "reindex-vectors", "--model-key", "k", "--launch-id", "LNCH-1", "--dry-run"]
    )
    assert args.handler is cli_ingest._cmd_reindex_vectors
    assert (args.model_key, args.launch_id, args.dry_run) == ("k", "LNCH-1", True)
