"""Not a test module (pytest only collects ``test_*.py``) — shared fixture
builders for the M13 (``trialerror.lens``) test suite. Self-contained (own
launch bootstrap, own document/chunk/vector builders) rather than importing
another lane's own private test helpers — same "own glob" rationale
``tests/_retrieve_fixtures.py``'s module docstring states for M8/M14 (2
other builders are concurrently editing their own lanes' files).
"""

from __future__ import annotations

from typing import Any

from trialerror.ingest.anchors import sha256_hex
from trialerror.ingest.backends import DEFAULT_FAKE_EMBED_DIMS, FakeEmbedBackend
from trialerror.stores.store import Store
from trialerror.stores.vecindex import VecBackend, ensure_vec_table, serialize_vector_fallback, vec_table_name
from trialerror.stores.writer import insert
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

DEFAULT_MODEL_DIMS = DEFAULT_FAKE_EMBED_DIMS


def bootstrap_launch(store: Store) -> str:
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
            "purpose": "fixture", "est_tokens": 100, "booked_ts": now(), "state": "PROVISIONAL",
        },
    )
    return launch_id


def _add_doc(
    store: Store, *, source_id: str, rel_path: str, text: str, model_key: str,
    embed_backend: FakeEmbedBackend, backend: VecBackend,
) -> str:
    doc_id = new_id("DOC")
    insert(
        store, "document",
        {
            "doc_id": doc_id, "source_id": source_id, "rel_path": rel_path, "media_type": "md",
            "normalizer_id": "fixture", "normalizer_version": "1", "sha256": "0" * 64, "status": "registered",
        },
    )
    element_id = new_id("ELM")
    insert(store, "element", {"element_id": element_id, "doc_id": doc_id, "seq": 0, "type": "NarrativeText", "text": text})

    chunk_id = new_id("CHK")
    sha = sha256_hex(text)
    insert(
        store, "chunk",
        {
            "chunk_id": chunk_id, "doc_id": doc_id, "seq": 0, "text": text, "token_count": len(text.split()),
            "element_first": element_id, "element_last": element_id, "sha256": sha,
            "chunker_id": "fixture", "chunker_version": "1", "created_ts": now(),
        },
    )
    vector = list(embed_backend.embed_batch([text], kind="document")[0])
    blob = serialize_vector_fallback(vector)
    insert(store, "emb", {"chunk_sha256": sha, "model_key": model_key, "dims": embed_backend.dims, "vector": blob, "created_ts": now()})
    table = vec_table_name(model_key)
    # Explicit `with store.knowledge:` (commits on normal exit) -- a raw
    # execute() left as the LAST write on the connection would otherwise
    # stay in an open, uncommitted transaction and be silently lost on
    # `store.close()` (only visible for the rest of that same connection's
    # life, which is fine for a single-connection test but not for a test
    # that closes and reopens the store, as the CLI tests do).
    with store.knowledge:
        if backend == VecBackend.SQLITE_VEC:
            store.knowledge.execute(f"INSERT INTO {table}(chunk_id, vector) VALUES (?, ?)", (chunk_id, blob))
        else:
            store.knowledge.execute(f"INSERT INTO {table}(chunk_id, model_key, dims, vector) VALUES (?, ?, ?, ?)", (chunk_id, model_key, embed_backend.dims, blob))
    return doc_id


def build_doc_pool(
    store: Store, *, n_docs: int, dims: int = DEFAULT_MODEL_DIMS, launch_id: str | None = None, prefix: str = "doc"
) -> dict[str, Any]:
    """``n_docs`` single-chunk documents under one ``open`` source, each
    with a distinct deterministic (hash-derived) embedding via
    ``FakeEmbedBackend`` — no GPU, no model load, exercises the real
    ``chunk``/``emb``/``vec_chunks__<model_key>`` shape M7 writes."""
    launch_id = launch_id or bootstrap_launch(store)
    embed_backend = FakeEmbedBackend(dims=dims)
    model_key = embed_backend.model_key
    backend = ensure_vec_table(store.knowledge, model_key, dims)

    source_id = new_id("SRC")
    insert(
        store, "source",
        {
            "source_id": source_id, "kind": "paper", "title": "Lens Fixture Corpus",
            "license_tier": "open", "acquisition_route": "web", "request_state": "indexed",
            "registered_ts": now(), "registered_by_launch": launch_id,
        },
    )
    doc_ids = [
        _add_doc(
            store, source_id=source_id, rel_path=f"archive/{prefix}_{i}.md",
            text=f"{prefix} fixture document {i} distinct content marker Q{i}",
            model_key=model_key, embed_backend=embed_backend, backend=backend,
        )
        for i in range(n_docs)
    ]
    return {"launch_id": launch_id, "model_key": model_key, "dims": dims, "source_id": source_id, "doc_ids": doc_ids}
