"""Lane F-1 item E: the resident similarity matrix.

The load-bearing test in this module is
:func:`test_the_matrix_ranking_is_byte_identical_to_the_python_path`: the
cache is only a cache if it answers what the uncached path answers, score
bits included. Everything else here is about the conditions under which it
engages, the fingerprint that keeps it honest, and the two verbs that must
drop it.

The timing test is marked ``acceptance`` and asserts the lane's own target
(``query similar`` under 0.3 s on a 16k x 2048 fixture) rather than a
relative speed-up, because a relative bar passes on a machine where both
paths are slow.
"""

from __future__ import annotations

import json
import time

import pytest

from tests._retrieve_fixtures import bootstrap_launch, build_bulk_corpus, build_small_corpus
from trialerror.retrieve import engine, vecmatrix
from trialerror.retrieve.vecsearch import fetch_vectors, rank_by_query_vector

numpy = pytest.importorskip("numpy", reason="the resident matrix is a numpy-only fast path")

#: Small enough to build in a test, larger than the engagement threshold.
_ROWS = 2_400
_DIMS = 16


@pytest.fixture(autouse=True)
def _clear_cache():
    vecmatrix.clear_process_cache()
    yield
    vecmatrix.clear_process_cache()


@pytest.fixture()
def bulk(store):
    built = build_bulk_corpus(store, n_chunks=_ROWS, n_docs=6, dims=_DIMS)
    return built


def _query_vector(text: str, dims: int = _DIMS) -> list[float]:
    from trialerror.ingest.backends import FakeEmbedBackend

    return FakeEmbedBackend(dims=dims).embed_batch([text], kind="query")[0]


# ---------------------------------------------------------------------------
# the claim: same answer
# ---------------------------------------------------------------------------


def test_the_matrix_ranking_is_byte_identical_to_the_python_path(store, bulk):
    """Not "close", not "same order" -- the same list of ``(chunk_id,
    float)`` tuples, because both lists are produced by
    ``rank_by_query_vector`` on the same inputs."""
    model_key = bulk["model_key"]
    for text in ("topic-7 ALPHA13", "unique reference marker R99", "synthetic fixture chunk"):
        query = _query_vector(text)
        for k in (1, 3, 20, 200):
            matrix = vecmatrix.top_ranked(store, model_key, query, k=k)
            assert matrix is not None, "the matrix should engage on this universe"
            ranked, n_scored = matrix

            universe = [r["chunk_id"] for r in store.knowledge.execute("SELECT chunk_id FROM chunk")]
            vectors = fetch_vectors(store, model_key, universe)
            expected = rank_by_query_vector(query, vectors)[:k]

            assert ranked == expected, f"k={k}, query={text!r}"
            assert n_scored == len(vectors), "the scored count is the count the uncached path reports"


def test_ties_break_on_chunk_id_exactly_as_the_python_path_does(store):
    """Every row carries the SAME vector, so every score ties and the entire
    answer is the id tie-break -- the case where a cache that re-sorted with
    its own comparator would diverge silently."""
    launch_id = bootstrap_launch(store)
    built = build_bulk_corpus(store, n_chunks=_ROWS, n_docs=4, dims=_DIMS, launch_id=launch_id)
    model_key = built["model_key"]
    blob = store.knowledge.execute(
        f"SELECT vector FROM vec_chunks__{model_key.replace('-', '_')} LIMIT 1"
    ).fetchone()["vector"]
    store.knowledge.execute(f"UPDATE vec_chunks__{model_key.replace('-', '_')} SET vector = ?", (blob,))
    store.knowledge.commit()
    vecmatrix.clear_process_cache()
    vecmatrix.invalidate(store.program_root, model_key)

    query = _query_vector("anything at all")
    ranked, _n = vecmatrix.top_ranked(store, model_key, query, k=25)
    universe = [r["chunk_id"] for r in store.knowledge.execute("SELECT chunk_id FROM chunk")]
    expected = rank_by_query_vector(query, fetch_vectors(store, model_key, universe))[:25]
    assert ranked == expected
    assert [cid for cid, _ in ranked] == sorted(cid for cid, _ in ranked), "id order under a total tie"


def test_a_restricted_universe_ranks_only_what_it_names(store, bulk):
    model_key = bulk["model_key"]
    allowed = bulk["chunk_ids"][:2_100]
    query = _query_vector("topic-3")
    ranked, n_scored = vecmatrix.top_ranked(store, model_key, query, k=10, restrict=allowed)
    assert n_scored == len(allowed)
    assert {cid for cid, _ in ranked} <= set(allowed)
    expected = rank_by_query_vector(query, fetch_vectors(store, model_key, allowed))[:10]
    assert ranked == expected


def test_an_excluded_id_is_never_returned(store, bulk):
    model_key = bulk["model_key"]
    query_chunk = bulk["chunk_ids"][0]
    query = _query_vector("topic-3")
    ranked, n_scored = vecmatrix.top_ranked(store, model_key, query, k=10, exclude=query_chunk)
    assert all(cid != query_chunk for cid, _ in ranked)
    assert n_scored == _ROWS - 1


# ---------------------------------------------------------------------------
# when it engages
# ---------------------------------------------------------------------------


def test_a_small_universe_declines_so_the_old_path_runs(store):
    built = build_small_corpus(store)
    query = _query_vector("distributed schedulers")
    assert vecmatrix.top_ranked(store, built["model_key"], query, k=5) is None


def test_a_query_vector_of_the_wrong_width_declines(store, bulk):
    assert vecmatrix.top_ranked(store, bulk["model_key"], [0.1] * (_DIMS + 1), k=5) is None


def test_an_unknown_model_key_declines(store, bulk):
    assert vecmatrix.top_ranked(store, "no-such-key", _query_vector("x"), k=5) is None


def test_the_threshold_is_a_parameter_not_a_law(store):
    built = build_small_corpus(store)
    query = _query_vector("distributed schedulers")
    result = vecmatrix.top_ranked(store, built["model_key"], query, k=2, min_universe=1)
    assert result is not None and len(result[0]) == 2


# ---------------------------------------------------------------------------
# the cache on disk
# ---------------------------------------------------------------------------


def test_building_writes_a_matrix_and_a_sidecar_under_the_index_dir(store, bulk):
    vecmatrix.top_ranked(store, bulk["model_key"], _query_vector("x"), k=5)
    npy_path, ids_path = vecmatrix.matrix_paths(store.program_root, bulk["model_key"])
    assert npy_path.is_file() and ids_path.is_file()
    assert npy_path.parent.name == vecmatrix.MATRIX_DIRNAME
    sidecar = json.loads(ids_path.read_text(encoding="utf-8"))
    assert len(sidecar["chunk_ids"]) == _ROWS
    assert sidecar["dims"] == _DIMS
    assert sidecar["fingerprint"]["rows"] == _ROWS


def test_a_second_process_loads_the_cache_instead_of_rebuilding(store, bulk):
    vecmatrix.top_ranked(store, bulk["model_key"], _query_vector("x"), k=5)
    npy_path, _ids = vecmatrix.matrix_paths(store.program_root, bulk["model_key"])
    mtime = npy_path.stat().st_mtime_ns
    vecmatrix.clear_process_cache()  # a fresh process, same files
    assert vecmatrix.top_ranked(store, bulk["model_key"], _query_vector("x"), k=5) is not None
    assert npy_path.stat().st_mtime_ns == mtime, "the cache was rewritten when it should have been loaded"


def test_a_changed_table_rebuilds_by_fingerprint(store, bulk):
    model_key = bulk["model_key"]
    vecmatrix.top_ranked(store, model_key, _query_vector("x"), k=5)
    npy_path, ids_path = vecmatrix.matrix_paths(store.program_root, model_key)
    before = json.loads(ids_path.read_text(encoding="utf-8"))["fingerprint"]

    table = f"vec_chunks__{model_key.replace('-', '_')}"
    store.knowledge.execute(f"DELETE FROM {table} WHERE chunk_id IN (SELECT chunk_id FROM {table} LIMIT 5)")
    store.knowledge.commit()
    vecmatrix.clear_process_cache()

    result = vecmatrix.top_ranked(store, model_key, _query_vector("x"), k=5)
    assert result is not None
    after = json.loads(ids_path.read_text(encoding="utf-8"))["fingerprint"]
    assert after != before and after["rows"] == before["rows"] - 5
    assert result[1] == _ROWS - 5, "the rebuilt matrix scores the rows that are actually there"


def test_a_stale_cache_is_reported_as_stale(store, bulk):
    model_key = bulk["model_key"]
    vecmatrix.top_ranked(store, model_key, _query_vector("x"), k=5)
    assert vecmatrix.matrix_status(store, model_key)["stale"] is False
    table = f"vec_chunks__{model_key.replace('-', '_')}"
    store.knowledge.execute(f"DELETE FROM {table} WHERE chunk_id = (SELECT chunk_id FROM {table} LIMIT 1)")
    store.knowledge.commit()
    status = vecmatrix.matrix_status(store, model_key)
    assert status["stale"] is True
    assert status["present"] is True
    assert status["fingerprint"]["rows"] != status["table_fingerprint"]["rows"]


def test_a_truncated_sidecar_is_a_cache_miss_not_an_error(store, bulk):
    model_key = bulk["model_key"]
    vecmatrix.top_ranked(store, model_key, _query_vector("x"), k=5)
    _npy, ids_path = vecmatrix.matrix_paths(store.program_root, model_key)
    ids_path.write_text("{not json", encoding="utf-8")
    vecmatrix.clear_process_cache()
    assert vecmatrix.top_ranked(store, model_key, _query_vector("x"), k=5) is not None


# ---------------------------------------------------------------------------
# V-1: a cache that cannot be WRITTEN must not break a search that worked
# ---------------------------------------------------------------------------


@pytest.fixture()
def unwritable_index_dir(store):
    """The program's index dir, made unwritable for the duration of one test
    and restored afterwards (an unwritable dir pytest could not clean up
    would fail the session, not the test).

    Two blockers, because neither is portable on its own. ``chmod(0o555)``
    is the real thing on POSIX; on Windows a directory's read-only attribute
    does not stop its OWNER creating children, so the cache write simply
    succeeded there and the test measured nothing. A plain FILE standing
    where the matrix cache directory belongs stops the write's first
    statement -- ``mkdir(parents=True, exist_ok=True)`` -- with an
    ``OSError`` on both platforms, which is the same exception from the same
    line as a read-only volume or a full one. It blocks the matrix cache
    only; the lexical index lives elsewhere under this directory."""
    index_dir = store.program_root / "index"
    index_dir.mkdir(parents=True, exist_ok=True)
    (index_dir / vecmatrix.MATRIX_DIRNAME).write_bytes(b"")
    index_dir.chmod(0o555)
    try:
        yield index_dir
    finally:
        index_dir.chmod(0o755)


def test_an_unwritable_index_dir_still_ranks_from_the_matrix_in_memory(store, bulk, unwritable_index_dir):
    """A read-only volume, a full one, an index dir owned by another account:
    all of them answered a search before this lane and all of them must
    still answer one. The cache write is best-effort; the ranking is not."""
    model_key = bulk["model_key"]
    query = _query_vector("topic-7 ALPHA13")
    result = vecmatrix.top_ranked(store, model_key, query, k=20)
    assert result is not None, "the matrix declined instead of serving from memory"
    ranked, n_scored = result
    assert n_scored == _ROWS

    universe = [r["chunk_id"] for r in store.knowledge.execute("SELECT chunk_id FROM chunk")]
    expected = rank_by_query_vector(query, fetch_vectors(store, model_key, universe))[:20]
    assert ranked == expected, "the in-memory answer must be the same answer"

    npy_path, ids_path = vecmatrix.matrix_paths(store.program_root, model_key)
    assert not npy_path.exists() and not ids_path.exists(), "nothing should have been written"


def test_an_unwritable_index_dir_does_not_break_search_or_similar(store, bulk, unwritable_index_dir):
    """Through the engine, where the traceback would have reached the CLI:
    ``search(mode="vector")`` has nothing to fall back to and ``similar``
    ranks the whole corpus, so these are the two callers the write sits in
    front of."""
    searched = engine.search(store, query="topic-3", k=5, mode="vector")
    assert len(searched["results"]) == 5
    assert searched["stats"]["vector_scored"] == _ROWS

    similar = engine.similar(store, bulk["chunk_ids"][3], k=5)
    assert len(similar["results"]) == 5


def test_a_failed_sidecar_write_leaves_no_orphan_matrix_behind(store, bulk, monkeypatch):
    """Half a cache is worse than none: the sidecar is what ``load_or_build``
    trusts, so a ``.npy`` whose sidecar never landed is dead weight."""
    import trialerror.util.atomic as atomic

    def _refuse_text(*_args, **_kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(atomic, "atomic_write_text", _refuse_text)
    model_key = bulk["model_key"]
    assert vecmatrix.top_ranked(store, model_key, _query_vector("x"), k=5) is not None
    npy_path, ids_path = vecmatrix.matrix_paths(store.program_root, model_key)
    assert not ids_path.exists()
    assert not npy_path.exists(), "the matrix was left without the sidecar that validates it"


def test_a_build_that_fails_any_other_way_declines_rather_than_raising(store, bulk, monkeypatch):
    """The last line of the contract: whatever ``_build`` did not anticipate
    (a MemoryError on a matrix too big for this process, a numpy build that
    raises on save) comes out as ``None`` and the uncached path runs."""

    def _explode(*_args, **_kwargs):
        raise MemoryError("not this process")

    monkeypatch.setattr(vecmatrix, "_build", _explode)
    assert vecmatrix.load_or_build(store, bulk["model_key"]) is None
    assert vecmatrix.top_ranked(store, bulk["model_key"], _query_vector("x"), k=5) is None
    # and the engine still answers, on the path it took before this module
    assert len(engine.search(store, query="topic-3", k=5, mode="vector")["results"]) == 5


# ---------------------------------------------------------------------------
# invalidation by the two verbs that rewrite a key's vectors
# ---------------------------------------------------------------------------


def test_reindex_vectors_invalidates_the_matrix(store, bulk):
    from trialerror.ingest.reindex import reindex_vectors

    model_key = bulk["model_key"]
    vecmatrix.top_ranked(store, model_key, _query_vector("x"), k=5)
    npy_path, _ids = vecmatrix.matrix_paths(store.program_root, model_key)
    assert npy_path.is_file()

    reindex_vectors(store, model_key=model_key, launch_id=bulk["launch_id"])
    assert not npy_path.is_file(), "a rebuilt index must not leave a matrix of the old one"


def test_purge_embeddings_invalidates_the_matrix(store, bulk):
    from trialerror.ingest.purge import purge_embeddings

    model_key = bulk["model_key"]
    vecmatrix.top_ranked(store, model_key, _query_vector("x"), k=5)
    npy_path, _ids = vecmatrix.matrix_paths(store.program_root, model_key)
    assert npy_path.is_file()

    # the purge refuses the CONFIGURED key, so this call declares another
    purge_embeddings(
        store,
        model_key=model_key,
        launch_id=bulk["launch_id"],
        config={"ingest": {"embed": {"backend": "fake", "dims": 32}}},
    )
    assert not npy_path.is_file()


def test_a_relocated_index_dir_is_honoured_by_stats_and_by_reindex(store, bulk):
    """V-2: every other call site threads the program config so
    ``matrix_paths`` can resolve ``[paths] index_dir``. These two did not, so
    on a program that relocates its index dir the explicit invalidation
    cleared a path nothing had written and ``query stats`` reported
    ``present: false`` for a cache that was on disk and being served."""
    from trialerror.ingest.reindex import reindex_vectors
    from trialerror.util.config import CONFIG_FILENAME, load_config

    (store.program_root / CONFIG_FILENAME).write_text(
        '[program]\nid = "fx"\n\n[paths]\nindex_dir = "derived_index"\n', encoding="utf-8"
    )
    config = load_config(store.program_root / CONFIG_FILENAME).raw
    model_key = bulk["model_key"]

    npy_path, _ids = vecmatrix.matrix_paths(store.program_root, model_key, config)
    assert npy_path.parent.parent.name == "derived_index"
    assert vecmatrix.top_ranked(store, model_key, _query_vector("x"), k=5, config=config) is not None
    assert npy_path.is_file()

    # corpus_stats reads the config off disk itself -- it takes no config
    # parameter, because `query stats` and the MCP tool both call it bare.
    status = engine.corpus_stats(store)["vecmatrix_by_model_key"][model_key]
    assert status["present"] is True
    assert status["path"] == str(npy_path)

    reindex_vectors(store, model_key=model_key, launch_id=bulk["launch_id"], config=config)
    assert not npy_path.is_file(), "the rebuild cleared a cache path nothing had written"


def test_the_reindex_cli_loads_the_config_the_invalidation_needs(store, bulk, program_root, platform_root):
    """The library call above only helps if its one caller passes the config;
    ``ingest purge-embeddings``' handler is the template."""
    from trialerror.cli import ingest as cli_ingest
    from trialerror.util.config import CONFIG_FILENAME, load_config

    (program_root / CONFIG_FILENAME).write_text(
        '[program]\nid = "fx"\n\n[paths]\nindex_dir = "derived_index"\n', encoding="utf-8"
    )
    config = load_config(program_root / CONFIG_FILENAME).raw
    model_key = bulk["model_key"]
    vecmatrix.top_ranked(store, model_key, _query_vector("x"), k=5, config=config)
    npy_path, _ids = vecmatrix.matrix_paths(program_root, model_key, config)
    assert npy_path.is_file()
    store.close()

    class _Args:
        program_root = None
        platform_root = None
        dry_run = False

    args = _Args()
    args.program_root = str(program_root)
    args.platform_root = platform_root
    args.model_key = model_key
    args.launch_id = bulk["launch_id"]

    env = cli_ingest._cmd_reindex_vectors(args)
    assert env["ok"], env
    assert not npy_path.is_file()


def test_invalidate_is_idempotent_and_says_whether_it_removed_anything(store, bulk):
    model_key = bulk["model_key"]
    vecmatrix.top_ranked(store, model_key, _query_vector("x"), k=5)
    assert vecmatrix.invalidate(store.program_root, model_key) is True
    assert vecmatrix.invalidate(store.program_root, model_key) is False


# ---------------------------------------------------------------------------
# through the engine
# ---------------------------------------------------------------------------


def test_similar_returns_the_same_rows_with_and_without_the_matrix(store, bulk, monkeypatch):
    ref = bulk["chunk_ids"][3]
    with_matrix = engine.similar(store, ref, k=10)

    monkeypatch.setattr(vecmatrix, "top_ranked", lambda *a, **kw: None)
    vecmatrix.clear_process_cache()
    without = engine.similar(store, ref, k=10)

    assert [r["chunk_id"] for r in with_matrix["results"]] == [r["chunk_id"] for r in without["results"]]
    assert [r["score"] for r in with_matrix["results"]] == [r["score"] for r in without["results"]]


def test_vector_mode_search_returns_the_same_rows_with_and_without_the_matrix(store, bulk, monkeypatch):
    with_matrix = engine.search(store, query="unique reference marker R7", mode="vector", k=10)
    assert with_matrix["stats"].get("vector_matrix") is True

    monkeypatch.setattr(vecmatrix, "top_ranked", lambda *a, **kw: None)
    vecmatrix.clear_process_cache()
    without = engine.search(store, query="unique reference marker R7", mode="vector", k=10)

    assert [r["chunk_id"] for r in with_matrix["results"]] == [r["chunk_id"] for r in without["results"]]
    assert [r["score"] for r in with_matrix["results"]] == [r["score"] for r in without["results"]]
    assert with_matrix["stats"]["vector_scored"] == without["stats"]["vector_scored"]


def test_a_two_stage_search_never_engages_the_matrix(store, bulk):
    """The FTS prefilter caps the universe at 500 rows, below the threshold:
    ``auto``/``hybrid`` stay byte-for-byte on the path they were on."""
    result = engine.search(store, query="ALPHA13 topic-7", mode="auto", k=10)
    assert "vector" in result["tiers_used"]
    assert "vector_matrix" not in result["stats"]


def test_corpus_stats_reports_the_matrix_and_its_fingerprint(store, bulk):
    engine.similar(store, bulk["chunk_ids"][0], k=5)
    stats = engine.corpus_stats(store)
    status = stats["vecmatrix_by_model_key"][bulk["model_key"]]
    assert status["present"] is True and status["stale"] is False
    assert status["fingerprint"]["rows"] == _ROWS
    assert status["numpy_available"] is True


# ---------------------------------------------------------------------------
# the measurement
# ---------------------------------------------------------------------------


@pytest.mark.acceptance
def test_query_similar_on_a_16k_by_2048_corpus_is_under_the_target(store, program_root, capsys):
    """Lane F-1's own bar, on the live corpus's shape: 16,000 chunks at 2,048
    dimensions, ``query similar --k 20`` under 0.3 s.

    Both numbers are printed, because the interesting one is not only the
    warm query: the COLD query pays for the build, and an operator who has
    just re-indexed wants to know what that costs once."""
    built = build_bulk_corpus(store, n_chunks=16_000, n_docs=30, dims=2_048)
    (program_root / "trialerror.toml").write_text(
        '[program]\nid = "PROG-test"\n\n[ingest.embed]\nbackend = "fake"\ndims = 2048\n', encoding="utf-8"
    )
    ref = built["chunk_ids"][0]

    t0 = time.perf_counter()
    cold = engine.similar(store, ref, k=20)
    cold_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    result = engine.similar(store, ref, k=20)
    warm = time.perf_counter() - t0

    with capsys.disabled():
        print(
            f"\n[F-1 item E] query similar --k 20 over 16,000 x 2,048: "
            f"cold (build+query) {cold_s:.3f}s, warm {warm:.3f}s"
        )

    assert len(cold["results"]) == 20 and len(result["results"]) == 20
    assert warm < 0.3, f"query similar --k 20 took {warm:.3f}s on 16k x 2048"
