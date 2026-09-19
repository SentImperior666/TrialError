"""Fix pass V-2: ``--baseline --corpus-mode vector`` reads the vector table
ONCE, not once per record.

As shipped, ``_nearest_corpus_neighbour`` fetched the whole universe inside
``baseline_distribution``'s per-record loop, and fetched it through
``fetch_vectors``, which materialises one Python ``float`` per component --
the exact cost item 1 of this lane exists to remove. Measured at the width
and scale the brief names (2048 dimensions, a store grown to 108k chunks):
107.6 s and 6.8 GB of transient objects PER RECORD, so a 90-record round
asking for its own baseline is hours of wall clock. The number item 2 exists
to make readable still could not be read.

What is asserted here is the SHAPE of the cost, not a wall-clock number: a
timing test on a shared machine measures the machine. The whole-table read
happens at most once per pass and does not grow with the record count, the
matrix route is the one actually taken when numpy is there, and the answer
is identical on both routes -- which is the claim a speed-up is only allowed
to make if it holds.
"""

from __future__ import annotations

import pytest

from tests._novelty_fixtures import IDEAS, build_round
from trialerror.lens import novelty
from trialerror.retrieve import engine as retrieve_engine

pytestmark = pytest.mark.usefixtures("store")


@pytest.fixture()
def counters(monkeypatch):
    """How many WHOLE-TABLE decodes each route made, and which route.

    Three distinctions, each of which a coarser counter gets wrong:

    * **Whole-table vs. bounded.** A pass also reads the inventory's own
      bounded set through ``fetch_vectors`` (``reference_snapshot``), once
      per pass, which is not what this finding is about. A read is
      whole-table when it asks for at least as many ids as the pass's own
      universe (``_all_chunk_ids``) holds.
    * **Asked vs. decoded.** ``fetch_vector_matrix`` returns ``None``
      without touching the table when numpy is absent or the knob is off.
      Counting that as a read makes a numpy-absent program look like two
      reads per pass when it makes one.
    * **Which route decoded.** ``matrix`` is the frombuffer path, ``plain``
      the ``dict[str, list[float]]`` one."""
    seen = {"matrix_asked": 0, "matrix": 0, "plain": 0, "bounded": 0}
    real_vectors = novelty.fetch_vectors
    real_matrix = novelty.fetch_vector_matrix

    def _size(store) -> int:
        return len(retrieve_engine._all_chunk_ids(store))

    def _vectors(store, model_key, chunk_ids, *a, **kw):
        ids = list(chunk_ids)
        seen["plain" if len(ids) >= _size(store) else "bounded"] += 1
        return real_vectors(store, model_key, ids, *a, **kw)

    def _matrix(store, model_key, chunk_ids, *a, **kw):
        ids = list(chunk_ids)
        whole = len(ids) >= _size(store)
        if whole:
            seen["matrix_asked"] += 1
        result = real_matrix(store, model_key, ids, *a, **kw)
        if result is not None:
            seen["matrix" if whole else "bounded"] += 1
        return result

    monkeypatch.setattr(novelty, "fetch_vectors", _vectors)
    monkeypatch.setattr(novelty, "fetch_vector_matrix", _matrix)
    return seen


def _decodes(counters) -> int:
    """Whole-table decodes, whichever route made them."""
    return counters["matrix"] + counters["plain"]


def _off(store) -> None:
    """The program's own config file. ``[program]`` is not decoration:
    :func:`trialerror.util.config.load_config` wants a whole config, and a
    fragment it refuses is read as no config at all."""
    (store.program_root / "trialerror.toml").write_text(
        '[program]\nid = "PROG-cost"\n\n[retrieve]\nnumpy_fastpath = "off"\n',
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# the shape of the cost
# ---------------------------------------------------------------------------


def test_the_whole_table_is_read_at_most_once_per_baseline_pass(store, counters):
    built = build_round(store)
    result = novelty.baseline_distribution(
        store, round_id=built["round_id"], corpus_mode="vector"
    )
    assert result["n_records"] > 1
    assert _decodes(counters) == 1, counters


def test_the_read_count_does_not_grow_with_the_record_count(store, counters):
    """The regression itself: before the fix this was ``n_records`` reads,
    so six records and eighteen differed by twelve. One round, one corpus,
    two populations -- ``--where`` selects a third of the records, and the
    table is still read exactly once for each pass."""
    built = build_round(store)

    novelty.baseline_distribution(
        store, round_id=built["round_id"], corpus_mode="vector",
        where={"set_id": "lens-1"},
    )
    six = dict(counters)

    whole = novelty.baseline_distribution(
        store, round_id=built["round_id"], corpus_mode="vector"
    )
    eighteen = {k: counters[k] - six[k] for k in counters}

    assert whole["n_records"] == len(IDEAS)
    assert _decodes(six) == _decodes(eighteen) == 1, (six, eighteen)


def test_the_matrix_route_is_the_one_taken_when_numpy_is_there(store, counters):
    """``fetch_vector_matrix`` had no production caller at all -- the 17x
    figure in the docs described a path nothing in the tree took (V-4).
    This pass is that caller."""
    pytest.importorskip("numpy")
    built = build_round(store)
    novelty.baseline_distribution(store, round_id=built["round_id"], corpus_mode="vector")
    assert counters["matrix"] == 1
    assert counters["plain"] == 0


def test_the_plain_route_still_reads_once_when_the_knob_is_off(store, counters):
    """The route a numpy-absent or knob-off program takes. ``asked`` is not
    ``decoded``: ``fetch_vector_matrix`` declines without touching the table,
    so the pass still makes exactly ONE whole-table decode -- which is the
    claim, on every program rather than only on one with numpy."""
    built = build_round(store)
    _off(store)
    novelty.baseline_distribution(store, round_id=built["round_id"], corpus_mode="vector")
    assert counters["matrix_asked"] == 1  # asked, and declined: numpy is off
    assert counters["matrix"] == 0
    assert counters["plain"] == 1
    assert _decodes(counters) == 1


# ---------------------------------------------------------------------------
# and the answer is the same one
# ---------------------------------------------------------------------------


def test_both_routes_return_the_identical_per_record_answer(store):
    """Byte-identical, not approximately equal: the matrix route uses numpy
    only to narrow and hands the superset to the same
    ``rank_by_query_vector`` the plain route uses."""
    pytest.importorskip("numpy")
    built = build_round(store)
    fast = novelty.baseline_distribution(
        store, round_id=built["round_id"], corpus_mode="vector"
    )
    _off(store)
    plain = novelty.baseline_distribution(
        store, round_id=built["round_id"], corpus_mode="vector"
    )
    assert fast["per_record"] == plain["per_record"]
    for key in ("min", "p50", "p90", "p95", "max", "n_with_neighbour"):
        assert fast[key] == plain[key]


def test_the_table_object_answers_every_record_from_one_decode(store):
    """The holder itself, directly: one load, many questions, and the same
    answer the one-shot helper gives."""
    built = build_round(store)
    # One baseline pass fills the per-model idea-vector cache; the point
    # here is the corpus side, so the record vectors come off that cache.
    novelty.baseline_distribution(store, round_id=built["round_id"], corpus_mode="vector")
    table = novelty.CorpusVectorTable(store, model_key=built["model_key"])
    assert table.n_rows > 0

    idea_vectors = novelty.fetch_idea_vectors(
        store, model_key=built["model_key"], idea_ids=built["idea_ids"]
    )
    asked = 0
    for idea_id, cached in sorted(idea_vectors.items()):
        vector = cached.get("vector")
        if not vector:
            continue
        asked += 1
        best = table.nearest(vector)
        one_shot = novelty._nearest_corpus_neighbour(
            store, vector=vector, statement="", model_key=built["model_key"],
            mode="vector", k=5,
        )
        assert best is not None and one_shot is not None
        assert best[0] == one_shot["nearest_chunk_id"]
        assert round(float(best[1]), 6) == one_shot["cosine"]
    assert asked > 1


def test_an_empty_corpus_is_none_rather_than_a_crash(store):
    table = novelty.CorpusVectorTable(store, model_key="no-such-model-key")
    assert table.n_rows == 0
    assert table.nearest([1.0, 0.0, 0.0]) is None
