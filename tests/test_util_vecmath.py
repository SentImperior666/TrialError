"""The two paths of :mod:`trialerror.util.vecmath` must agree.

The module's whole claim is that numpy changes the wall clock and nothing
else, so every test here is written as a COMPARISON: run the same inputs
through ``mode="off"`` and through ``mode="auto"`` and assert on the
relationship between the two answers, rather than on a number one of them
happens to produce. A test that pinned a literal cosine would pass on a
machine with no numpy while the fast path was silently wrong.

Timing is deliberately absent. A speed-up is not a unit test (it fails on a
loaded machine and proves nothing on a fast one); the measured figure lives
in the lane's IMPL report.
"""

from __future__ import annotations

import builtins
import random
import struct
import sys

import pytest

from trialerror.util import vecmath

pytestmark = pytest.mark.skipif(
    vecmath.numpy_module(mode="auto") is None,
    reason="numpy is not installed here; there is no fast path to compare the plain one against",
)

# Comfortably above vecmath.MIN_FASTPATH_ROWS, so every fixture below
# actually engages the fast path rather than silently testing plain-vs-plain.
N_ROWS = 400
DIMS = 24


def _rows(seed: int, n: int = N_ROWS, dims: int = DIMS) -> list[list[float]]:
    rng = random.Random(seed)
    return [[rng.uniform(-1.0, 1.0) for _ in range(dims)] for _ in range(n)]


def _keyed(rows):
    return {f"id-{i:04d}": vec for i, vec in enumerate(rows)}


# ---------------------------------------------------------------------------
# the knob
# ---------------------------------------------------------------------------


def test_mode_defaults_to_auto_and_reads_the_retrieve_table():
    assert vecmath.numpy_fastpath_mode(None) == "auto"
    assert vecmath.numpy_fastpath_mode({}) == "auto"
    assert vecmath.numpy_fastpath_mode({"retrieve": {"numpy_fastpath": "off"}}) == "off"
    assert vecmath.numpy_fastpath_mode({"retrieve": {"numpy_fastpath": " OFF "}}) == "off"


def test_unrecognized_mode_is_noted_once_and_treated_as_unset(capsys):
    vecmath._NOTED.clear()
    assert vecmath.numpy_fastpath_mode({"retrieve": {"numpy_fastpath": "fast!"}}) == "auto"
    first = capsys.readouterr().err
    assert "numpy_fastpath" in first and "fast!" in first
    assert vecmath.numpy_fastpath_mode({"retrieve": {"numpy_fastpath": "fast!"}}) == "auto"
    assert capsys.readouterr().err == ""


def test_env_override_when_no_config_is_in_hand(monkeypatch):
    monkeypatch.setenv(vecmath.NUMPY_FASTPATH_ENV, "off")
    assert vecmath.numpy_fastpath_mode(None) == "off"
    assert vecmath.numpy_enabled(None) is False
    # An explicit config still wins over the process-wide lever.
    assert vecmath.numpy_fastpath_mode({"retrieve": {"numpy_fastpath": "auto"}}) == "auto"


def test_off_really_means_off():
    assert vecmath.numpy_module({"retrieve": {"numpy_fastpath": "off"}}) is None
    assert vecmath.numpy_enabled({"retrieve": {"numpy_fastpath": "off"}}) is False
    assert vecmath.numpy_enabled({"retrieve": {"numpy_fastpath": "auto"}}) is True


def test_package_does_not_import_numpy_at_module_import():
    """numpy must stay OPTIONAL: importing the module may not pull it in."""
    source = (vecmath.__file__ or "")
    assert source
    with open(source, encoding="utf-8") as handle:
        head = handle.read().split("def numpy_module", 1)[0]
    assert "\nimport numpy" not in head


def test_simulated_missing_numpy_still_answers(monkeypatch):
    """The import hook the module docstring's "optional" claim rests on."""
    real_import = builtins.__import__

    def _no_numpy(name, *args, **kwargs):
        if name == "numpy" or name.startswith("numpy."):
            raise ImportError("simulated: numpy is not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "numpy", raising=False)
    monkeypatch.setattr(builtins, "__import__", _no_numpy)

    assert vecmath.numpy_module(mode="auto") is None
    assert vecmath.numpy_enabled(mode="auto") is False

    rows = _rows(seed=11)
    query = _rows(seed=12, n=1)[0]
    # With numpy unimportable, "auto" and "off" are the same path and must
    # produce the same answer as the plain path always did.
    assert vecmath.cosine_many(query, rows, mode="auto") == vecmath.cosine_many(query, rows, mode="off")
    assert vecmath.top_k(query, _keyed(rows), 5, mode="auto") == vecmath.top_k(query, _keyed(rows), 5, mode="off")
    assert vecmath.decode_float32_blobs([struct.pack("<4f", 1.0, 2.0, 3.0, 4.0)], mode="auto") == [
        [1.0, 2.0, 3.0, 4.0]
    ]


# ---------------------------------------------------------------------------
# cosine_many
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_cosine_many_agrees_with_the_plain_path(seed):
    rows = _rows(seed)
    query = _rows(seed + 100, n=1)[0]
    plain = vecmath.cosine_many(query, rows, mode="off")
    fast = vecmath.cosine_many(query, rows, mode="auto")
    assert len(fast) == len(plain) == len(rows)
    for a, b in zip(plain, fast):
        assert abs(a - b) <= 1e-6


def test_cosine_many_degenerate_rows_score_zero_on_both_paths():
    rows = _rows(seed=7)
    rows[3] = [0.0] * DIMS          # zero norm
    rows[9] = [0.5] * (DIMS - 1)    # narrower than the query
    rows[17] = [0.5] * (DIMS + 3)   # wider than the query
    rows[23] = []                   # empty
    query = _rows(seed=8, n=1)[0]
    plain = vecmath.cosine_many(query, rows, mode="off")
    fast = vecmath.cosine_many(query, rows, mode="auto")
    for index in (3, 9, 17, 23):
        assert plain[index] == 0.0
        assert fast[index] == 0.0
    for a, b in zip(plain, fast):
        assert abs(a - b) <= 1e-6


def test_cosine_many_propagates_nan_rather_than_zeroing_it():
    """A NaN norm is not a zero norm. Both paths return NaN for a NaN row --
    if the fast one returned 0.0 instead, top_k's superset would be built
    from a score the plain path never produces."""
    rows = _rows(seed=9)
    rows[11] = [float("nan")] * DIMS
    query = _rows(seed=10, n=1)[0]
    plain = vecmath.cosine_many(query, rows, mode="off")
    fast = vecmath.cosine_many(query, rows, mode="auto")
    assert plain[11] != plain[11]
    assert fast[11] != fast[11]
    for index, (a, b) in enumerate(zip(plain, fast)):
        if index != 11:
            assert abs(a - b) <= 1e-6


def test_cosine_many_degenerate_query_scores_zero_on_both_paths():
    rows = _rows(seed=13)
    for query in ([0.0] * DIMS, []):
        assert vecmath.cosine_many(query, rows, mode="off") == [0.0] * len(rows)
        assert vecmath.cosine_many(query, rows, mode="auto") == [0.0] * len(rows)


def test_cosine_many_blocks_a_scan_wider_than_one_block(monkeypatch):
    """Crossing the block boundary may not change the answer."""
    monkeypatch.setattr(vecmath, "_BLOCK_BYTES", DIMS * 4 * 7)  # 7 rows a block
    rows = _rows(seed=21)
    query = _rows(seed=22, n=1)[0]
    assert vecmath._block_rows(DIMS) == 7
    plain = vecmath.cosine_many(query, rows, mode="off")
    fast = vecmath.cosine_many(query, rows, mode="auto")
    for a, b in zip(plain, fast):
        assert abs(a - b) <= 1e-6


def test_cosine_many_accepts_a_numpy_matrix_and_a_list_alike():
    np = vecmath.numpy_module(mode="auto")
    rows = _rows(seed=31)
    query = _rows(seed=32, n=1)[0]
    from_list = vecmath.cosine_many(query, rows, mode="auto")
    from_matrix = vecmath.cosine_many(query, np.asarray(rows, dtype=np.float32), mode="auto")
    for a, b in zip(from_list, from_matrix):
        assert abs(a - b) <= 1e-6
    # A matrix of the wrong width is a length mismatch for every row.
    narrow = np.asarray(_rows(seed=33, dims=DIMS - 2), dtype=np.float32)
    assert vecmath.cosine_many(query, narrow, mode="auto") == [0.0] * N_ROWS


# ---------------------------------------------------------------------------
# top_k -- the identity claim
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("k", [1, 3, 10, 50, N_ROWS, N_ROWS + 5])
def test_top_k_is_byte_identical_to_the_plain_path(k):
    rows = _keyed(_rows(seed=41))
    query = _rows(seed=42, n=1)[0]
    assert vecmath.top_k(query, rows, k, mode="auto") == vecmath.top_k(query, rows, k, mode="off")


def test_top_k_ties_break_on_the_id_ascending_on_both_paths():
    """The tie case the brief names. Twenty rows share one vector, so their
    cosines are bit-identical and ONLY the id tie-break orders them."""
    rows = _rows(seed=51)
    shared = [0.3, -0.2] * (DIMS // 2)
    for index in range(0, 200, 10):
        rows[index] = list(shared)
    keyed = _keyed(rows)
    query = list(shared)  # every tied row is the best possible match
    plain = vecmath.top_k(query, keyed, 12, mode="off")
    fast = vecmath.top_k(query, keyed, 12, mode="auto")
    assert fast == plain
    tied_ids = [cid for cid, _score in plain]
    assert tied_ids == sorted(tied_ids)
    assert len({score for _cid, score in plain}) == 1


def test_top_k_none_ranks_everything_identically():
    rows = _keyed(_rows(seed=61))
    query = _rows(seed=62, n=1)[0]
    ranked = vecmath.top_k(query, rows, None, mode="auto")
    assert ranked == vecmath.top_k(query, rows, None, mode="off")
    assert len(ranked) == N_ROWS
    assert ranked == sorted(ranked, key=lambda pair: (-pair[1], pair[0]))


def test_top_k_zero_and_empty():
    rows = _keyed(_rows(seed=71))
    query = _rows(seed=72, n=1)[0]
    assert vecmath.top_k(query, rows, 0, mode="auto") == []
    assert vecmath.top_k(query, {}, 5, mode="auto") == []
    assert vecmath.top_k(query, {}, None, mode="auto") == []


def test_top_k_accepts_pairs_as_well_as_a_mapping():
    rows = _rows(seed=81)
    keyed = _keyed(rows)
    query = _rows(seed=82, n=1)[0]
    assert vecmath.top_k(query, list(keyed.items()), 7, mode="auto") == vecmath.top_k(query, keyed, 7, mode="off")


def test_top_k_with_a_nan_row_agrees_with_the_plain_path():
    """NaN defeats the narrowing pass; the module hands the whole set to the
    exact one instead, and the two paths still agree."""
    rows = _rows(seed=91)
    rows[5] = [float("nan")] * DIMS
    keyed = _keyed(rows)
    query = _rows(seed=92, n=1)[0]
    assert vecmath.top_k(query, keyed, 6, mode="auto") == vecmath.top_k(query, keyed, 6, mode="off")


def test_top_k_with_degenerate_rows_agrees_with_the_plain_path():
    rows = _rows(seed=101)
    rows[2] = [0.0] * DIMS
    rows[4] = [1.0] * (DIMS + 1)
    rows[6] = []
    keyed = _keyed(rows)
    query = _rows(seed=102, n=1)[0]
    for k in (1, 5, 40, None):
        assert vecmath.top_k(query, keyed, k, mode="auto") == vecmath.top_k(query, keyed, k, mode="off")


# ---------------------------------------------------------------------------
# mean_pool_l2
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", [111, 112, 113])
def test_mean_pool_l2_agrees_with_the_plain_path(seed):
    rows = _rows(seed)
    plain = vecmath.mean_pool_l2(rows, mode="off")
    fast = vecmath.mean_pool_l2(rows, mode="auto")
    assert plain is not None and fast is not None
    assert len(plain) == len(fast) == DIMS
    for a, b in zip(plain, fast):
        assert abs(a - b) <= 1e-6
    assert abs(sum(x * x for x in fast) - 1.0) <= 1e-9


def test_mean_pool_l2_empty_and_zero_mean_are_none_on_both_paths():
    for mode in ("off", "auto"):
        assert vecmath.mean_pool_l2([], mode=mode) is None
        assert vecmath.mean_pool_l2([[0.0] * DIMS] * N_ROWS, mode=mode) is None
        # Opposing vectors that cancel exactly.
        rows = [[1.0] * DIMS, [-1.0] * DIMS] * (N_ROWS // 2)
        assert vecmath.mean_pool_l2(rows, mode=mode) is None


def test_mean_pool_l2_mixed_dims_raise_on_both_paths():
    rows = _rows(seed=121)
    rows[300] = [0.1] * (DIMS + 1)
    for mode in ("off", "auto"):
        with pytest.raises(ValueError, match="inconsistent vector dims"):
            vecmath.mean_pool_l2(rows, mode=mode)


def test_mean_pool_l2_blocks_a_pool_wider_than_one_block(monkeypatch):
    monkeypatch.setattr(vecmath, "_BLOCK_BYTES", DIMS * 4 * 5)
    rows = _rows(seed=131)
    plain = vecmath.mean_pool_l2(rows, mode="off")
    fast = vecmath.mean_pool_l2(rows, mode="auto")
    for a, b in zip(plain, fast):
        assert abs(a - b) <= 1e-6


# ---------------------------------------------------------------------------
# score_candidates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n_home", [1, 2, 5])
def test_score_candidates_agrees_with_the_plain_path(n_home):
    candidates = _keyed(_rows(seed=141))
    home = {f"home-{i}": vec for i, vec in enumerate(_rows(seed=142, n=n_home))}
    plain = vecmath.score_candidates(candidates, home, mode="off")
    fast = vecmath.score_candidates(candidates, home, mode="auto")
    assert set(plain) == set(fast) == set(candidates)
    for cid in plain:
        assert abs(plain[cid] - fast[cid]) <= 1e-6


def test_score_candidates_degenerate_pairs_are_distance_one_on_both_paths():
    candidates = _keyed(_rows(seed=151))
    candidates["id-0003"] = [0.0] * DIMS
    candidates["id-0009"] = [0.4] * (DIMS - 1)
    home = {"home-0": _rows(seed=152, n=1)[0]}
    for mode in ("off", "auto"):
        scores = vecmath.score_candidates(candidates, home, mode=mode)
        assert scores["id-0003"] == pytest.approx(1.0)
        assert scores["id-0009"] == pytest.approx(1.0)


def test_score_candidates_keeps_genuine_ties_tied_on_both_paths():
    """The stratify cut reads this order positionally, so two identical
    candidates must score identically -- bit for bit -- on both paths."""
    rows = _rows(seed=161)
    rows[10] = list(rows[0])
    rows[20] = list(rows[0])
    candidates = _keyed(rows)
    home = {f"home-{i}": vec for i, vec in enumerate(_rows(seed=162, n=3))}
    for mode in ("off", "auto"):
        scores = vecmath.score_candidates(candidates, home, mode=mode)
        assert scores["id-0000"] == scores["id-0010"] == scores["id-0020"]


def test_score_candidates_empty_inputs():
    home = {"home-0": _rows(seed=171, n=1)[0]}
    for mode in ("off", "auto"):
        assert vecmath.score_candidates({}, home, mode=mode) == {}
        assert vecmath.score_candidates(_keyed(_rows(seed=172)), {}, mode=mode) == {}


# ---------------------------------------------------------------------------
# decode_float32_blobs
# ---------------------------------------------------------------------------


def test_decode_float32_blobs_round_trips_the_on_disk_shape():
    from trialerror.stores.vecindex import deserialize_vector_fallback, serialize_vector_fallback

    rows = _rows(seed=181, n=50)
    blobs = [serialize_vector_fallback(vec) for vec in rows]
    expected = [deserialize_vector_fallback(b) for b in blobs]

    plain = vecmath.decode_float32_blobs(blobs, mode="off")
    assert plain == expected

    matrix = vecmath.decode_float32_blobs(blobs, mode="auto")
    assert matrix.shape == (50, DIMS)
    assert [[float(v) for v in row] for row in matrix] == expected


def test_decode_float32_blobs_refuses_a_ragged_set():
    from trialerror.stores.vecindex import serialize_vector_fallback

    blobs = [serialize_vector_fallback([1.0, 2.0]), serialize_vector_fallback([1.0, 2.0, 3.0])]
    for mode in ("off", "auto"):
        assert vecmath.decode_float32_blobs(blobs, mode=mode) is None
        assert vecmath.decode_float32_blobs([], mode=mode) is None
        assert vecmath.decode_float32_blobs([b""], mode=mode) is None
        assert vecmath.decode_float32_blobs([b"\x00\x00\x00"], mode=mode) is None


def test_decode_float32_blobs_blocks_a_long_set(monkeypatch):
    from trialerror.stores.vecindex import serialize_vector_fallback

    monkeypatch.setattr(vecmath, "_BLOCK_BYTES", DIMS * 4 * 3)
    rows = _rows(seed=191, n=25)
    blobs = [serialize_vector_fallback(vec) for vec in rows]
    matrix = vecmath.decode_float32_blobs(blobs, mode="auto")
    assert matrix.shape == (25, DIMS)
    assert [[float(v) for v in row] for row in matrix] == vecmath.decode_float32_blobs(blobs, mode="off")


def test_block_rows_is_bounded_by_rows_and_by_bytes():
    assert vecmath._block_rows(2048) == vecmath._BLOCK_BYTES // (2048 * 4)
    assert vecmath._block_rows(4) == vecmath.MAX_BLOCK_ROWS
    assert vecmath._block_rows(0) == vecmath.MAX_BLOCK_ROWS
    assert vecmath._block_rows(vecmath._BLOCK_BYTES) == 1


def test_the_documented_lever_does_not_decide_this_suite(monkeypatch):
    """Fix pass V-10. ``TRIALERROR_NUMPY_FASTPATH`` is documented in
    ``USER_SETUP`` §3i as the process-wide lever, and setting it used to
    turn this very suite red -- the default-mode test above read the
    ambient environment, and several tests elsewhere take the fast path as
    a given because numpy is installed here. ``tests/conftest.py`` now
    clears the variable for every test, so the suite answers about the code
    rather than about the shell it was run from; a test that is ABOUT the
    lever sets it itself and still wins."""
    import os

    assert os.environ.get(vecmath.NUMPY_FASTPATH_ENV) is None
    assert vecmath.numpy_fastpath_mode(None) == "auto"

    monkeypatch.setenv(vecmath.NUMPY_FASTPATH_ENV, "off")
    assert vecmath.numpy_fastpath_mode(None) == "off"
