"""Not a test module (pytest only collects ``test_*.py``) — shared fixture
builders for the M8 (``trialerror.retrieve`` / ``trialerror.mcp.knowledge``) test
suite. Self-contained (own launch bootstrap, own document/chunk/index
builders) rather than importing another lane's own private test helpers —
this build's lane isolation is ``trialerror/retrieve/`` + ``trialerror/mcp/
knowledge.py`` + this file's own glob, and 2 other builders are
concurrently editing their own lanes' files (``tests/test_mcp_ops_tools.py``
states the identical rationale for M14).

Two corpus builders:

- :func:`build_small_corpus` — a handful of real, chunker-derived documents
  (one ``open``, one ``commercial_restricted``) built through the SAME
  production primitives M7's ``run_normalize``/``run_chunk``/``run_embed``/
  ``run_index`` handlers use (``trialerror.ingest.stream``/``chunker``/
  ``anchors``), so fixture data is byte-consistent with what real ingestion
  produces. Used by correctness tests (fence, citation, resolve_quote, ...).
- :func:`build_bulk_corpus` — a 15k-chunk synthetic corpus built via direct
  bulk ``executemany`` (bypassing the validated write API's per-row
  overhead, and bypassing the job-handler machinery entirely) so the M8
  15k-chunk latency ACCEPTANCE fixture (design Section 12 M8 row: "fixture
  vectors synthetic — no GPU in-lane") builds in a couple of seconds rather
  than minutes. Still FK-correct (real ``document``/``element`` rows) and
  still populates ``chunk_fts``/``emb``/``vec_chunks__<model_key>`` exactly
  as M7's real pipeline would.
"""

from __future__ import annotations

from typing import Any

from trialerror.ingest.anchors import build_chunk_anchor, sha256_hex
from trialerror.ingest.backends import DEFAULT_FAKE_EMBED_DIMS, FakeEmbedBackend
from trialerror.ingest.chunker import CHUNKER_ID, CHUNKER_VERSION, build_chunks
from trialerror.ingest.stream import stream_v1
from trialerror.stores.store import Store
from trialerror.stores.vecindex import VecBackend, ensure_vec_table, serialize_vector_fallback, vec_table_name
from trialerror.stores.writer import insert, update
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

#: matches ``trialerror.ingest.backends.load_embed_backend({})``'s zero-config
#: default (``FakeEmbedBackend(dims=16)``, ``model_key = "fake-16"``) — a
#: fixture built with the DEFAULT ``FakeEmbedBackend`` is automatically
#: found by ``trialerror.retrieve.engine._resolve_embed_backend`` for a Store
#: whose ``program_root`` has no ``trialerror.toml`` (the ``store``/``program_root``
#: conftest fixtures don't write one), with zero extra wiring.
DEFAULT_MODEL_DIMS = DEFAULT_FAKE_EMBED_DIMS


def bootstrap_launch(store: Store) -> str:
    """Insert a minimal account/session/launch chain and return the
    ``launch_id`` — every ``source``/``quote_anchor`` write in this module
    is XID-validated against ``platform.launch`` (same pattern as
    ``tests/_ingest_fixtures.py::bootstrap_launch``, duplicated here per
    this file's own self-containment note)."""
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "test account", "created_ts": now()})
    session_id = new_id("SESS")
    insert(store, "session", {"session_id": session_id, "account_id": account_id, "opened_ts": now(), "status": "open"})
    launch_id = new_id("LNCH")
    insert(
        store,
        "launch",
        {
            "launch_id": launch_id,
            "account_id": account_id,
            "program_id": "PROG-test",
            "session_id": session_id,
            "agent_kind": "tester",
            "model_class": "top",
            "model": "sonnet",
            "purpose": "fixture",
            "est_tokens": 100,
            "booked_ts": now(),
            "state": "PROVISIONAL",
        },
    )
    return launch_id


def _index_chunk(store: Store, *, chunk_id: str, text: str, sha256: str, model_key: str, dims: int, vector: list[float], backend: VecBackend) -> None:
    """Mirrors ``trialerror.ingest.handlers.run_index``'s own per-chunk indexing
    exactly (both branches), reused here so fixtures land in the SAME
    ``chunk_fts``/``vec_chunks__<model_key>`` shape real ingestion does."""
    store.knowledge.execute("INSERT INTO chunk_fts(chunk_id, text) VALUES (?, ?)", (chunk_id, text))
    insert(store, "emb", {"chunk_sha256": sha256, "model_key": model_key, "dims": dims, "vector": serialize_vector_fallback(vector), "created_ts": now()})
    table = vec_table_name(model_key)
    if backend == VecBackend.SQLITE_VEC:
        store.knowledge.execute(f"INSERT INTO {table}(chunk_id, vector) VALUES (?, ?)", (chunk_id, serialize_vector_fallback(vector)))
    else:
        store.knowledge.execute(f"INSERT INTO {table}(chunk_id, model_key, dims, vector) VALUES (?, ?, ?, ?)", (chunk_id, model_key, dims, serialize_vector_fallback(vector)))


def _add_document(
    store: Store,
    *,
    source_id: str,
    rel_path: str,
    paragraphs: list[str],
    launch_id: str,
    model_key: str,
    embed_backend: FakeEmbedBackend,
    backend: VecBackend,
) -> dict[str, Any]:
    doc_id = new_id("DOC")
    insert(
        store,
        "document",
        {
            "doc_id": doc_id, "source_id": source_id, "rel_path": rel_path, "media_type": "md",
            "normalizer_id": "fixture", "normalizer_version": "1", "sha256": "0" * 64, "status": "registered",
        },
    )
    element_rows: list[dict[str, Any]] = []
    for i, text in enumerate(paragraphs):
        row = {"element_id": new_id("ELM"), "doc_id": doc_id, "seq": i, "type": "NarrativeText", "text": text, "page_number": (i // 2) + 1}
        insert(store, "element", row)
        element_rows.append(row)

    stream_text = stream_v1(element_rows)
    doc_sha256 = sha256_hex(stream_text)
    update(store, "document", pk_column="doc_id", pk_value=doc_id, changes={"sha256": doc_sha256, "status": "chunked"})

    chunk_ids: list[str] = []
    for draft in build_chunks(element_rows):
        chunk_id = new_id("CHK")
        chunk_sha256 = sha256_hex(draft["text"])
        row = {
            "chunk_id": chunk_id, "doc_id": doc_id, "seq": draft["seq"], "text": draft["text"],
            "token_count": draft["token_count"], "element_first": draft["element_first"], "element_last": draft["element_last"],
            "page_start": draft["page_start"], "page_end": draft["page_end"], "sha256": chunk_sha256,
            "chunker_id": draft["chunker_id"], "chunker_version": draft["chunker_version"], "created_ts": now(),
        }
        insert(store, "chunk", row)

        anchor_draft = build_chunk_anchor(
            doc_id=doc_id, doc_sha256=doc_sha256, elements=element_rows, chunk_id=chunk_id,
            element_first=row["element_first"], element_last=row["element_last"], page_number=row["page_start"],
        )
        insert(store, "quote_anchor", {"anchor_id": new_id("ANC"), **anchor_draft, "created_by_launch": launch_id, "created_ts": now()})

        vector = list(embed_backend.embed_batch([draft["text"]], kind="document")[0])
        _index_chunk(store, chunk_id=chunk_id, text=draft["text"], sha256=chunk_sha256, model_key=model_key, dims=embed_backend.dims, vector=vector, backend=backend)
        chunk_ids.append(chunk_id)

    update(store, "document", pk_column="doc_id", pk_value=doc_id, changes={"status": "indexed"})
    return {"doc_id": doc_id, "chunk_ids": chunk_ids, "doc_sha256": doc_sha256}


_RESTRICTED_PARAGRAPH = (
    "This paragraph belongs to a commercial rulebook and is intentionally long so that fencing it "
    "down to twenty words is a meaningful, observable transformation rather than a no-op: it deals "
    "with combat resolution, initiative order, and the precise wording of a proprietary special "
    "ability that a licensed publisher would not want reproduced verbatim in any external system, "
    "database, or agent context window under any circumstances whatsoever."
)


def build_small_corpus(store: Store, *, launch_id: str | None = None, dims: int = DEFAULT_MODEL_DIMS) -> dict[str, Any]:
    """A tiny two-source corpus (one ``open``, one ``commercial_restricted``)
    built through real chunker/anchor primitives — used by fence,
    citation-shape, resolve_quote, similar, get_chunk/get_source/
    get_document_outline correctness tests."""
    launch_id = launch_id or bootstrap_launch(store)
    embed_backend = FakeEmbedBackend(dims=dims)
    model_key = embed_backend.model_key
    backend = ensure_vec_table(store.knowledge, model_key, dims)

    open_source_id = new_id("SRC")
    insert(
        store, "source",
        {
            "source_id": open_source_id, "kind": "paper", "title": "An Open Paper About Tabletop Systems",
            "license_tier": "open", "acquisition_route": "web", "request_state": "indexed",
            "registered_ts": now(), "registered_by_launch": launch_id,
        },
    )
    open_doc = _add_document(
        store, source_id=open_source_id, rel_path="archive/open.md",
        paragraphs=[
            "Tabletop role-playing games use dice pools to resolve uncertain outcomes during play.",
            "A game master adjudicates rules disputes and narrates the consequences of player actions.",
        ],
        launch_id=launch_id, model_key=model_key, embed_backend=embed_backend, backend=backend,
    )

    restricted_source_id = new_id("SRC")
    insert(
        store, "source",
        {
            "source_id": restricted_source_id, "kind": "rulebook", "title": "Proprietary Combat Rulebook",
            "license_tier": "commercial_restricted", "acquisition_route": "user_scan", "request_state": "indexed",
            "registered_ts": now(), "registered_by_launch": launch_id,
        },
    )
    restricted_doc = _add_document(
        store, source_id=restricted_source_id, rel_path="archive/restricted.md",
        paragraphs=[_RESTRICTED_PARAGRAPH, "A second, shorter paragraph about spell components and casting time."],
        launch_id=launch_id, model_key=model_key, embed_backend=embed_backend, backend=backend,
    )

    return {
        "launch_id": launch_id,
        "model_key": model_key,
        "dims": dims,
        "open_source_id": open_source_id,
        "open_doc_id": open_doc["doc_id"],
        "open_chunk_ids": open_doc["chunk_ids"],
        "restricted_source_id": restricted_source_id,
        "restricted_doc_id": restricted_doc["doc_id"],
        "restricted_chunk_ids": restricted_doc["chunk_ids"],
    }


def _bulk_chunk_text(i: int) -> str:
    topic = i % 37
    keyword = f"ALPHA{i % 53}"
    return (
        f"Synthetic fixture chunk {i} discusses topic-{topic} in some detail, mentioning keyword "
        f"{keyword} and unique reference marker R{i} for latency-fixture retrieval testing."
    )


def build_bulk_corpus(
    store: Store, *, n_chunks: int = 15_000, n_docs: int = 30, dims: int = DEFAULT_MODEL_DIMS, launch_id: str | None = None
) -> dict[str, Any]:
    """A single ``open`` source with ``n_chunks`` synthetic chunks spread
    over ``n_docs`` documents, built via direct bulk ``executemany`` — the
    M8 15k-chunk latency acceptance fixture (design Section 12 M8 row:
    "fixture vectors synthetic — no GPU in-lane"). Deliberately bypasses
    the validated ``trialerror.stores.writer.insert`` per-row API (and the M2
    jobs-ledger/handler machinery entirely) purely for fixture-build speed
    — this is a throwaway perf-test corpus, not a correctness fixture."""
    launch_id = launch_id or bootstrap_launch(store)
    embed_backend = FakeEmbedBackend(dims=dims)
    model_key = embed_backend.model_key
    backend = ensure_vec_table(store.knowledge, model_key, dims)
    table = vec_table_name(model_key)

    source_id = new_id("SRC")
    with store.knowledge:
        store.knowledge.execute(
            "INSERT INTO source (source_id, kind, title, license_tier, acquisition_route, request_state, registered_ts, registered_by_launch) "
            "VALUES (?, 'report', 'Bulk Synthetic Latency Fixture', 'open', 'web', 'indexed', ?, ?)",
            (source_id, now(), launch_id),
        )

        doc_ids = [new_id("DOC") for _ in range(n_docs)]
        doc_rows = [
            (doc_id, source_id, f"archive/bulk_{i}.md", "md", "fixture", "1", "0" * 64, "indexed")
            for i, doc_id in enumerate(doc_ids)
        ]
        store.knowledge.executemany(
            "INSERT INTO document (doc_id, source_id, rel_path, media_type, normalizer_id, normalizer_version, sha256, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            doc_rows,
        )

        element_ids = [new_id("ELM") for _ in range(n_docs)]
        element_rows = [
            (element_ids[i], doc_ids[i], 0, "NarrativeText", f"bulk fixture document {i} root element", 1)
            for i in range(n_docs)
        ]
        store.knowledge.executemany(
            "INSERT INTO element (element_id, doc_id, seq, type, text, page_number) VALUES (?, ?, ?, ?, ?, ?)",
            element_rows,
        )

    chunk_ids: list[str] = [new_id("CHK") for _ in range(n_chunks)]
    texts = [_bulk_chunk_text(i) for i in range(n_chunks)]
    shas = [sha256_hex(t) for t in texts]

    chunk_rows = []
    fts_rows = []
    anchor_rows = []
    ts = now()
    for i in range(n_chunks):
        doc_idx = i % n_docs
        chunk_rows.append(
            (
                chunk_ids[i], doc_ids[doc_idx], i, texts[i], len(texts[i].split()),
                element_ids[doc_idx], element_ids[doc_idx], 1, 1, shas[i], CHUNKER_ID, CHUNKER_VERSION, ts,
            )
        )
        fts_rows.append((chunk_ids[i], texts[i]))
        anchor_rows.append(
            (new_id("ANC"), doc_ids[doc_idx], chunk_ids[i], 1, 0, len(texts[i]), "0" * 64, shas[i], launch_id, ts)
        )

    with store.knowledge:
        store.knowledge.executemany(
            "INSERT INTO chunk (chunk_id, doc_id, seq, text, token_count, element_first, element_last, "
            "page_start, page_end, sha256, chunker_id, chunker_version, created_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            chunk_rows,
        )
        store.knowledge.executemany("INSERT INTO chunk_fts (chunk_id, text) VALUES (?, ?)", fts_rows)
        store.knowledge.executemany(
            "INSERT INTO quote_anchor (anchor_id, doc_id, chunk_id, page_number, char_start, char_end, "
            "doc_sha256, quote_sha256, created_by_launch, created_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            anchor_rows,
        )

    # embeddings: batch through FakeEmbedBackend (fast, deterministic, synthetic -- F18)
    emb_rows = []
    vec_rows = []
    batch = 500
    for start in range(0, n_chunks, batch):
        chunk_texts = texts[start : start + batch]
        vectors = embed_backend.embed_batch(chunk_texts, kind="document")
        for j, vector in enumerate(vectors):
            idx = start + j
            blob = serialize_vector_fallback(list(vector))
            emb_rows.append((shas[idx], model_key, dims, blob, ts))
            if backend == VecBackend.SQLITE_VEC:
                vec_rows.append((chunk_ids[idx], blob))
            else:
                vec_rows.append((chunk_ids[idx], model_key, dims, blob))

    with store.knowledge:
        store.knowledge.executemany(
            "INSERT INTO emb (chunk_sha256, model_key, dims, vector, created_ts) VALUES (?, ?, ?, ?, ?)", emb_rows
        )
        if backend == VecBackend.SQLITE_VEC:
            store.knowledge.executemany(f"INSERT INTO {table}(chunk_id, vector) VALUES (?, ?)", vec_rows)
        else:
            store.knowledge.executemany(f"INSERT INTO {table}(chunk_id, model_key, dims, vector) VALUES (?, ?, ?, ?)", vec_rows)

    return {"launch_id": launch_id, "model_key": model_key, "dims": dims, "source_id": source_id, "doc_ids": doc_ids, "chunk_ids": chunk_ids, "n_chunks": n_chunks}
