"""Not a test module (pytest only collects ``test_*.py``) — the shared
builder for a corpus that holds an ``inventory`` source alongside ordinary
ones, plus a booked lens launch that declares a slice.

Self-contained in the same sense ``tests/_retrieve_fixtures.py`` states for
itself: it builds its own documents through the same primitives real
ingestion uses (``build_row_chunks`` for the inventory, the real chunker for
prose), so what the exclusion, the scope and the screen are tested against
is shaped like what the pipeline actually writes.
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from trialerror.ingest.anchors import build_chunk_anchor, sha256_hex
from trialerror.ingest.backends import DEFAULT_FAKE_EMBED_DIMS, FakeEmbedBackend
from trialerror.ingest.chunker import build_chunks, build_row_chunks
from trialerror.ingest.stream import stream_v1
from trialerror.stores.store import Store
from trialerror.stores.vecindex import VecBackend, ensure_vec_table, serialize_vector_fallback, vec_table_name
from trialerror.stores.writer import insert, update
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

DEFAULT_MODEL_DIMS = DEFAULT_FAKE_EMBED_DIMS

#: Two registers ("families"), four MECHANIC rows each, written as the
#: register table rows a real register states them as: ``| <source>-M<nnn> |
#: name | description |``. The id pattern is what
#: ``trialerror.lens.novelty.parse_mechanic_rows`` filters plant candidates
#: on, so a fixture whose rows were bare prose would exercise the plant
#: battery against a shape the register never has. The family label is the
#: document's rel_path stem, which is the convention
#: ``trialerror.lens.novelty`` documents and reads.
INVENTORY_DESCRIPTIONS: dict[str, list[tuple[str, str, str]]] = {
    "family-a": [
        ("REGA-M001", "track step", "a spent resource advances a shared track by exactly one step"),
        ("REGA-M002", "low-marker initiative", "initiative passes to whoever holds the fewest open markers"),
        ("REGA-M003", "simultaneous bid", "a contested check compares two simultaneously revealed hidden bids"),
        ("REGA-M004", "phase refill", "a depleted common pool refills at the start of every third phase"),
    ],
    "family-b": [
        ("REGB-M001", "cancel discard", "a player may discard a held card to cancel one revealed effect"),
        ("REGB-M002", "boundary scoring", "territory control is scored once per boundary rather than per region"),
        ("REGB-M003", "inaction timer", "a timer advances whenever any participant declines to act"),
        ("REGB-M004", "silent seat", "one seat sees the deck order and cannot speak, so information is asymmetric"),
    ],
}

#: The same rows as the register text a chunk actually carries.
INVENTORY_ROWS: dict[str, list[str]] = {
    family: [f"| {row_id} | {name} | {description} |" for row_id, name, description in rows]
    for family, rows in INVENTORY_DESCRIPTIONS.items()
}

#: The corpus documents deliberately share exact tokens with the inventory
#: rows above -- "shared track", "coordinator" -- because the FTS tier ANDs
#: quoted tokens (``trialerror.retrieve.ftssearch.fts_query_string``), so an
#: exclusion test whose query matches ONLY the inventory would pass on an
#: empty result set and prove nothing.
CORPUS_PARAGRAPHS: list[list[str]] = [
    [
        "Scheduling literature treats contention between concurrent writers as a queueing problem.",
        "A coordinator keeps a shared track and advances it whenever a resource is consumed.",
    ],
    [
        "Studies of turn allocation describe how the right to act is distributed among contributors.",
        "A coordinator summary shown before the next turn changes what that turn produces.",
    ],
    [
        "Measurement theory distinguishes ordinal from interval scales and warns against arithmetic on the former.",
        "An instrument that is never validated reports a number whose meaning nobody has established.",
    ],
]


def bootstrap_launch(store: Store, *, attrs: dict[str, Any] | None = None, purpose: str = "fixture") -> str:
    """One account/session/launch chain, returning the ``launch_id``.
    ``attrs`` rides on the launch row exactly as ``book_launch`` would write
    it -- which is where the retrieval scope reads its slice from."""
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "test account", "created_ts": now()})
    session_id = new_id("SESS")
    insert(store, "session", {"session_id": session_id, "account_id": account_id, "opened_ts": now(), "status": "open"})
    launch_id = new_id("LNCH")
    insert(
        store,
        "launch",
        {
            "launch_id": launch_id, "account_id": account_id, "program_id": "PROG-test",
            "session_id": session_id, "agent_kind": "tester", "model_class": "top", "model": "sonnet",
            "purpose": purpose, "est_tokens": 100, "booked_ts": now(), "state": "PROVISIONAL",
            "attrs": json.dumps(attrs, ensure_ascii=False) if attrs else None,
        },
    )
    return launch_id


def _index_chunk(store: Store, *, chunk_id: str, text: str, sha256: str, model_key: str, dims: int, vector, backend) -> None:
    store.knowledge.execute("INSERT INTO chunk_fts(chunk_id, text) VALUES (?, ?)", (chunk_id, text))
    insert(
        store, "emb",
        {"chunk_sha256": sha256, "model_key": model_key, "dims": dims,
         "vector": serialize_vector_fallback(vector), "created_ts": now()},
    )
    table = vec_table_name(model_key)
    with store.knowledge:
        if backend == VecBackend.SQLITE_VEC:
            store.knowledge.execute(f"INSERT INTO {table}(chunk_id, vector) VALUES (?, ?)", (chunk_id, serialize_vector_fallback(vector)))
        else:
            store.knowledge.execute(
                f"INSERT INTO {table}(chunk_id, model_key, dims, vector) VALUES (?, ?, ?, ?)",
                (chunk_id, model_key, dims, serialize_vector_fallback(vector)),
            )


def add_document(
    store: Store,
    *,
    source_id: str,
    rel_path: str,
    paragraphs: Sequence[str],
    launch_id: str,
    model_key: str,
    embed_backend: FakeEmbedBackend,
    backend,
    row_per_element: bool = False,
) -> dict[str, Any]:
    doc_id = new_id("DOC")
    insert(
        store, "document",
        {"doc_id": doc_id, "source_id": source_id, "rel_path": rel_path, "media_type": "md",
         "normalizer_id": "fixture", "normalizer_version": "1", "sha256": "0" * 64, "status": "registered"},
    )
    element_rows: list[dict[str, Any]] = []
    for i, text in enumerate(paragraphs):
        row = {"element_id": new_id("ELM"), "doc_id": doc_id, "seq": i, "type": "NarrativeText", "text": text, "page_number": 1}
        insert(store, "element", row)
        element_rows.append(row)

    doc_sha256 = sha256_hex(stream_v1(element_rows))
    update(store, "document", pk_column="doc_id", pk_value=doc_id, changes={"sha256": doc_sha256, "status": "chunked"})

    drafts = build_row_chunks(element_rows) if row_per_element else build_chunks(element_rows)
    chunk_ids: list[str] = []
    for draft in drafts:
        chunk_id = new_id("CHK")
        chunk_sha256 = sha256_hex(draft["text"])
        insert(
            store, "chunk",
            {"chunk_id": chunk_id, "doc_id": doc_id, "seq": draft["seq"], "text": draft["text"],
             "token_count": draft["token_count"], "element_first": draft["element_first"],
             "element_last": draft["element_last"], "page_start": draft["page_start"],
             "page_end": draft["page_end"], "sha256": chunk_sha256,
             "chunker_id": draft["chunker_id"], "chunker_version": draft["chunker_version"], "created_ts": now()},
        )
        anchor_draft = build_chunk_anchor(
            doc_id=doc_id, doc_sha256=doc_sha256, elements=element_rows, chunk_id=chunk_id,
            element_first=draft["element_first"], element_last=draft["element_last"], page_number=draft["page_start"],
        )
        insert(store, "quote_anchor", {"anchor_id": new_id("ANC"), **anchor_draft, "created_by_launch": launch_id, "created_ts": now()})
        vector = list(embed_backend.embed_batch([draft["text"]], kind="document")[0])
        _index_chunk(
            store, chunk_id=chunk_id, text=draft["text"], sha256=chunk_sha256,
            model_key=model_key, dims=embed_backend.dims, vector=vector, backend=backend,
        )
        chunk_ids.append(chunk_id)

    update(store, "document", pk_column="doc_id", pk_value=doc_id, changes={"status": "indexed"})
    return {"doc_id": doc_id, "chunk_ids": chunk_ids}


def build_corpus_with_inventory(
    store: Store, *, launch_id: str | None = None, dims: int = DEFAULT_MODEL_DIMS
) -> dict[str, Any]:
    """One ``paper`` source with three prose documents and one ``inventory``
    source with two row-per-element register documents."""
    launch_id = launch_id or bootstrap_launch(store)
    embed_backend = FakeEmbedBackend(dims=dims)
    model_key = embed_backend.model_key
    backend = ensure_vec_table(store.knowledge, model_key, dims)

    corpus_source_id = new_id("SRC")
    insert(
        store, "source",
        {"source_id": corpus_source_id, "kind": "paper", "title": "Corpus Fixture",
         "license_tier": "open", "acquisition_route": "web", "request_state": "indexed",
         "registered_ts": now(), "registered_by_launch": launch_id},
    )
    corpus_docs = [
        add_document(
            store, source_id=corpus_source_id, rel_path=f"archive/corpus_{i}.md", paragraphs=paragraphs,
            launch_id=launch_id, model_key=model_key, embed_backend=embed_backend, backend=backend,
        )
        for i, paragraphs in enumerate(CORPUS_PARAGRAPHS)
    ]

    inventory_source_id = new_id("SRC")
    insert(
        store, "source",
        {"source_id": inventory_source_id, "kind": "inventory", "title": "Reference Rows",
         "license_tier": "open", "acquisition_route": "web", "request_state": "indexed",
         "registered_ts": now(), "registered_by_launch": launch_id},
    )
    inventory_docs = {
        family: add_document(
            store, source_id=inventory_source_id, rel_path=f"archive/{family}.md", paragraphs=rows,
            launch_id=launch_id, model_key=model_key, embed_backend=embed_backend, backend=backend,
            row_per_element=True,
        )
        for family, rows in INVENTORY_ROWS.items()
    }

    return {
        "launch_id": launch_id,
        "model_key": model_key,
        "dims": dims,
        "embed_backend": embed_backend,
        "corpus_source_id": corpus_source_id,
        "corpus_doc_ids": [d["doc_id"] for d in corpus_docs],
        "corpus_chunk_ids": [cid for d in corpus_docs for cid in d["chunk_ids"]],
        "inventory_source_id": inventory_source_id,
        "inventory_doc_ids": {family: d["doc_id"] for family, d in inventory_docs.items()},
        "inventory_chunk_ids": [cid for d in inventory_docs.values() for cid in d["chunk_ids"]],
    }
