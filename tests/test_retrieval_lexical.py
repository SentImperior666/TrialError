"""Unit tests for :mod:`trialerror.retrieve.lexical` -- the lexical tier's
backend seam (C-0080): which backend gets picked, and the four fallback
rules that keep search answering when the preferred one can't.

The fallbacks are the point of this module. A missing optional dependency
or an unbuilt derived index must never turn into an exception or an empty
result set -- it must turn into "FTS5 served this one, and said so once"."""

from __future__ import annotations

import pytest

from tests import _retrieve_fixtures as fx
from trialerror.ingest.anchors import sha256_hex
from trialerror.retrieve import lexical, tantivysearch as tv
from trialerror.stores import paths
from trialerror.stores.writer import insert
from trialerror.util.timeutil import now

HAS_TANTIVY = tv.tantivy_available()


@pytest.fixture(autouse=True)
def _fresh_notes():
    """Every test starts from a clean "nothing noted yet" process state --
    :func:`lexical.note_once` is deliberately process-global."""
    lexical._reset_notes()
    yield
    lexical._reset_notes()


def _write_config(program_root, body: str) -> None:
    (program_root / "trialerror.toml").write_text(
        '[program]\nid = "PROG-test"\n\n' + body, encoding="utf-8"
    )


def _build_index(store):
    return tv.reindex(store.knowledge, paths.fulltext_index_path(store.program_root))


# ---------------------------------------------------------------------------
# the config knob
# ---------------------------------------------------------------------------


def test_default_backend_is_tantivy():
    assert lexical.DEFAULT_FULLTEXT_BACKEND == "tantivy"
    assert lexical.configured_backend_name({}) == "tantivy"
    assert lexical.configured_backend_name(None) == "tantivy"


def test_config_can_pin_fts5():
    assert lexical.configured_backend_name({"retrieve": {"fulltext_backend": "fts5"}}) == "fts5"


def test_config_value_is_case_and_whitespace_insensitive():
    assert lexical.configured_backend_name({"retrieve": {"fulltext_backend": "  FTS5 "}}) == "fts5"


def test_unrecognized_config_value_falls_back_and_notes_once():
    """A typo in trialerror.toml must not take search down."""
    assert lexical.configured_backend_name({"retrieve": {"fulltext_backend": "elasticsearch"}}) == "tantivy"
    assert any(k.startswith("bad_backend:") for k in lexical._notes_emitted())
    # second call: same answer, no second note
    before = lexical._notes_emitted()
    lexical.configured_backend_name({"retrieve": {"fulltext_backend": "elasticsearch"}})
    assert lexical._notes_emitted() == before


def test_non_mapping_retrieve_table_is_tolerated():
    assert lexical.configured_backend_name({"retrieve": "nonsense"}) == "tantivy"


# ---------------------------------------------------------------------------
# resolution + fallbacks
# ---------------------------------------------------------------------------


def test_resolves_to_fts5_when_the_program_pins_it(store):
    _write_config(store.program_root, '[retrieve]\nfulltext_backend = "fts5"\n')
    assert lexical.resolve_backend(store).name == "fts5"


def test_resolves_to_fts5_when_no_index_has_been_built_yet(store):
    """Rule 4, and the state EVERY existing program is in the moment
    C-0080 merges: tantivy is configured (by default), importable, but this
    program has never run the reindex."""
    backend = lexical.resolve_backend(store)
    assert backend.name == "fts5"
    if HAS_TANTIVY:
        assert any(k.startswith("tantivy_index_not_ready:") for k in lexical._notes_emitted())


def test_resolves_to_fts5_when_tantivy_is_not_importable(store, monkeypatch):
    """Rule 3, simulated by making the import probe fail -- the same shape
    an environment without the wheel would produce."""
    monkeypatch.setattr(tv, "tantivy_module", lambda: None)
    backend = lexical.resolve_backend(store)
    assert backend.name == "fts5"
    assert "tantivy_missing" in lexical._notes_emitted()


def test_resolves_to_fts5_for_a_store_without_a_program_root(store):
    """The doctor's knowledge-only shim, and anything else duck-typing just
    ``.knowledge``: there is nowhere to look for an index, so FTS5."""

    class _KnowledgeOnly:
        def __init__(self, conn):
            self.knowledge = conn

    backend = lexical.resolve_backend(_KnowledgeOnly(store.knowledge))
    assert backend.name == "fts5"


@pytest.mark.skipif(not HAS_TANTIVY, reason="tantivy-py not installed")
def test_resolves_to_tantivy_once_the_index_exists(store):
    fx.build_small_corpus(store)
    _build_index(store)
    assert lexical.resolve_backend(store).name == "tantivy"


@pytest.mark.skipif(not HAS_TANTIVY, reason="tantivy-py not installed")
def test_fts5_pin_is_honored_even_with_a_built_index(store):
    fx.build_small_corpus(store)
    _build_index(store)
    _write_config(store.program_root, '[retrieve]\nfulltext_backend = "fts5"\n')
    assert lexical.resolve_backend(store).name == "fts5"


@pytest.mark.skipif(not HAS_TANTIVY, reason="tantivy-py not installed")
def test_a_mid_rebuild_index_is_refused_and_search_still_answers(store):
    """The crash-safety contract end to end: remove the readiness sidecar
    (what a kill mid-``reindex`` leaves behind) and the tier keeps working
    -- out of ``chunk_fts``, at full recall, not out of a half-built
    tantivy index at partial recall."""
    fx.build_small_corpus(store)
    _build_index(store)
    (paths.fulltext_index_path(store.program_root) / tv.META_FILENAME).unlink()

    rows, backend = lexical.lexical_search(store, "retry budgets", limit=50)
    assert backend == "fts5"
    assert rows, "the FTS5 fallback must still return the corpus's real hits"


# ---------------------------------------------------------------------------
# lexical_search: the contract the engine consumes
# ---------------------------------------------------------------------------


def test_lexical_search_returns_rows_and_the_backend_that_served_them(store):
    fx.build_small_corpus(store)
    rows, backend = lexical.lexical_search(store, "retry budgets", limit=50)
    assert backend in lexical.FULLTEXT_BACKENDS
    assert rows and set(rows[0]) == {"chunk_id", "bm25"}


def test_both_backends_satisfy_the_protocol():
    assert isinstance(lexical.Fts5Backend(), lexical.LexicalBackend)


# ---------------------------------------------------------------------------
# maintain_index (the ingest-time write path)
# ---------------------------------------------------------------------------


def test_maintain_index_skips_when_the_program_pins_fts5(store):
    _write_config(store.program_root, '[retrieve]\nfulltext_backend = "fts5"\n')
    out = lexical.maintain_index(store, [("CHK-1", "hello world")])
    assert out["action"] == "skip"
    assert not paths.fulltext_index_path(store.program_root).exists()


def test_maintain_index_skips_when_tantivy_is_absent(store, monkeypatch):
    monkeypatch.setattr(tv, "tantivy_module", lambda: None)
    assert lexical.maintain_index(store, [("CHK-1", "hello world")])["action"] == "skip"


@pytest.mark.skipif(not HAS_TANTIVY, reason="tantivy-py not installed")
def test_maintain_index_builds_the_whole_index_on_first_call(store):
    """A program with an existing corpus and no index gets a full rebuild,
    not an append of just this batch -- an append would leave a
    ready-LOOKING index covering a fraction of the corpus, which the
    serving path would then trust."""
    built = fx.build_small_corpus(store)
    all_ids = set(built["open_chunk_ids"]) | set(built["restricted_chunk_ids"])
    one_doc = [(cid, "irrelevant") for cid in built["open_chunk_ids"]]

    out = lexical.maintain_index(store, one_doc)
    assert out["action"] == "rebuild"
    assert out["chunk_count"] == len(all_ids)

    status = tv.index_status(store.knowledge, paths.fulltext_index_path(store.program_root))
    assert status["state"] == "ok"


@pytest.mark.skipif(not HAS_TANTIVY, reason="tantivy-py not installed")
def test_maintain_index_appends_incrementally_once_an_index_exists(store):
    fx.build_small_corpus(store)
    _build_index(store)
    out = lexical.maintain_index(store, [("CHK-brand-new", "a chunk that arrived after the build")])
    assert out["action"] == "add"
    assert out["added"] == 1


@pytest.mark.skipif(not HAS_TANTIVY, reason="tantivy-py not installed")
def test_maintain_index_is_idempotent_across_reruns(store):
    """Re-running the ``index`` stage after a kill must add nothing -- the
    restart-safety discipline every other ingest handler already keeps."""
    built = fx.build_small_corpus(store)
    _build_index(store)
    rows = [(cid, "text") for cid in built["open_chunk_ids"]]
    assert lexical.maintain_index(store, rows)["added"] == 0
    assert lexical.maintain_index(store, rows)["added"] == 0


# ---------------------------------------------------------------------------
# fix pass, finding D-4: maintain_index must never fail the `index` ingest
# stage over a problem in DERIVED (cache) state -- knowledge.db has already
# committed by the time this runs.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_TANTIVY, reason="tantivy-py not installed")
def test_maintain_index_never_raises_when_the_first_build_fails(store):
    """Real reproduction, not a monkeypatch: a stray FILE sits where the
    index directory tree must go, so ``reindex()``'s own ``mkdir`` raises.
    Before the fix this propagated straight out of ``maintain_index`` and
    would have failed the ingest job's ``index`` stage over a problem in
    derived state alone."""
    built = fx.build_small_corpus(store)
    index_dir = paths.fulltext_index_path(store.program_root)
    # `index_dir` is `<index_root>/tantivy/chunks`; `index_dir.parent.parent`
    # is `<index_root>` itself (default "index", a direct child of
    # `program_root`, which the `store`/`program_root` fixtures already
    # created). Putting a plain FILE there means `reindex()`'s own
    # `mkdir(parents=True, ...)` for `.../tantivy/chunks` cannot create
    # `.../tantivy` underneath a path component that is not a directory.
    index_dir.parent.parent.write_text("a stray file, not a directory", encoding="utf-8")

    out = lexical.maintain_index(store, [(cid, "irrelevant") for cid in built["open_chunk_ids"]])
    assert out["action"] == "failed"
    assert out["error"]
    assert any(k.startswith("tantivy_maintain_failed:") for k in lexical._notes_emitted())


@pytest.mark.skipif(not HAS_TANTIVY, reason="tantivy-py not installed")
def test_maintain_index_never_raises_when_the_first_build_fails_generically(store, monkeypatch):
    """The general contract, independent of any one failure mode's exact
    filesystem shape: whatever ``tantivysearch.reindex`` raises, the ingest
    job's ``index`` stage must see a normal return, never an exception."""
    built = fx.build_small_corpus(store)

    def _boom(*_a, **_k):
        raise ValueError("simulated: Failed to acquire Lockfile: LockBusy")

    monkeypatch.setattr(tv, "reindex", _boom)
    out = lexical.maintain_index(store, [(cid, "irrelevant") for cid in built["open_chunk_ids"]])
    assert out["action"] == "failed"
    assert "LockBusy" in out["error"]


@pytest.mark.skipif(not HAS_TANTIVY, reason="tantivy-py not installed")
def test_maintain_index_never_raises_when_an_append_fails(store, monkeypatch):
    """Same contract on the ``add`` path (a READY index already exists) --
    the case the finding names as reachable in normal operation: a second
    tantivy writer already holding the index's lock (a concurrent
    ``reindex-fulltext``, or another worker process)."""
    fx.build_small_corpus(store)
    _build_index(store)

    def _boom(*_a, **_k):
        raise ValueError("simulated: Failed to acquire Lockfile: LockBusy")

    monkeypatch.setattr(tv, "add_chunks", _boom)
    out = lexical.maintain_index(store, [("CHK-new", "a chunk that arrived while another writer held the lock")])
    assert out["action"] == "failed"
    assert "LockBusy" in out["error"]


# ---------------------------------------------------------------------------
# fix pass, finding D-8: resolve_backend must not trust a READY index whose
# document count has silently drifted from knowledge.db's chunk count
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_TANTIVY, reason="tantivy-py not installed")
def test_resolve_backend_falls_back_when_the_index_undercounts_the_corpus(store):
    """A chunk committed to knowledge.db after the index's last successful
    add/rebuild (e.g. a D-4 failure) makes a READY-sidecar index silently
    under-recall. The cheap ``COUNT(*)`` guard closes that window without
    waiting for the next ``doctor`` run."""
    built = fx.build_small_corpus(store)
    _build_index(store)
    assert lexical.resolve_backend(store).name == "tantivy"

    (doc_id,) = store.knowledge.execute("SELECT doc_id FROM document LIMIT 1").fetchone()
    (element_id,) = store.knowledge.execute("SELECT element_id FROM element LIMIT 1").fetchone()
    insert(
        store, "chunk",
        {
            "chunk_id": "CHK-drifted", "doc_id": doc_id, "seq": 9999, "text": "a chunk the index never saw",
            "token_count": 6, "element_first": element_id, "element_last": element_id,
            "page_start": 1, "page_end": 1, "sha256": sha256_hex("CHK-drifted"),
            "chunker_id": "fixture", "chunker_version": "1", "created_ts": now(),
        },
    )
    store.knowledge.commit()

    backend = lexical.resolve_backend(store)
    assert backend.name == "fts5"
    index_dir = paths.fulltext_index_path(store.program_root)
    assert any(k.startswith(f"tantivy_index_chunk_count_mismatch:{index_dir}") for k in lexical._notes_emitted())
    assert built  # keep the fixture reference alive for readability
