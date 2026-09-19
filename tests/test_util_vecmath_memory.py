"""Fix pass V-5: the stated memory bound is measured, not asserted in prose.

``vecmath``'s docstring and ``USER_SETUP`` §3i both promise that a scan's
scratch is ``block × dims × 8`` bytes -- 41 MB at 256 dimensions, 64 MB at
2048 -- and that it does not grow with the table's row count. The second
half held. The first was out by about 2× on both widths, for two separate
reasons, neither visible from reading the sentence:

* ``norms = np.sqrt((block * block).sum(axis=1))`` materialised a second
  full-size float64 copy of the block before reducing it;
* the loop allocated the NEXT block while the previous one was still
  referenced, so two blocks overlapped for the duration of the ``asarray``.

Both are fixed; this module is what keeps them fixed. A documented memory
bound that nothing measures is a sentence, and a reader sizing a scan on a
machine with a hard limit is entitled to the number being true.

numpy allocates through its own tracemalloc domain, which
``tracemalloc.get_traced_memory()`` includes, so the peak below is the real
one and not just the Python-object overhead around it.
"""

from __future__ import annotations

import tracemalloc

import pytest

from trialerror.util import vecmath

numpy = pytest.importorskip("numpy")

#: How far above the documented bound a measurement may land before it is a
#: broken promise rather than measurement noise. Generous on purpose: the
#: failure this guards against is a factor of two, and the returned score
#: list and the two row-length vectors are real costs the bound does not
#: name. Before the fix, both widths measured above this.
_TOLERANCE = 1.35


def _peak_above_base(fn) -> int:
    tracemalloc.start()
    try:
        base = tracemalloc.get_traced_memory()[0]
        fn()
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak - base


def _matrix(rows: int, dims: int):
    rng = numpy.random.default_rng(20260919)
    return rng.random((rows, dims), dtype=numpy.float32)


@pytest.mark.parametrize("dims,rows", [(256, 20_000), (2048, 8_192)])
def test_a_scan_stays_inside_the_documented_block(dims, rows):
    matrix = _matrix(rows, dims)
    query = [float(x) for x in matrix[0]]
    vecmath.cosine_many(query, matrix[:300])  # warm the import and the BLAS

    documented = vecmath._block_rows(dims) * dims * 8
    peak = _peak_above_base(lambda: vecmath.cosine_many(query, matrix))
    assert peak <= documented * _TOLERANCE, (
        f"dims={dims}: peak {peak / 1e6:.1f} MB against a documented "
        f"{documented / 1e6:.1f} MB"
    )


def test_the_scratch_does_not_grow_with_the_table():
    """The claim that matters, kept separate from the number: 50k rows and
    200k rows at the same width scan inside the same block."""
    dims = 256
    query = [float(x) for x in _matrix(1, dims)[0]]
    peaks = {}
    for rows in (50_000, 200_000):
        matrix = _matrix(rows, dims)
        vecmath.cosine_many(query, matrix[:300])
        peaks[rows] = _peak_above_base(lambda m=matrix: vecmath.cosine_many(query, m))
        del matrix

    # The only part that legitimately grows is the returned score list, one
    # Python float per row (24 bytes plus a pointer); everything else is the
    # fixed block. 48 bytes a row leaves room for list over-allocation and
    # nothing like room for a second per-row structure.
    growth = peaks[200_000] - peaks[50_000]
    assert growth < 150_000 * 48, peaks


def test_mean_pool_holds_one_block_not_two():
    dims = 2048
    rows = vecmath._block_rows(dims) * 2 + 7
    matrix = _matrix(rows, dims)
    vecmath.mean_pool_l2(matrix[:300])  # warm

    documented = vecmath._block_rows(dims) * dims * 8
    peak = _peak_above_base(lambda: vecmath.mean_pool_l2(matrix))
    assert peak <= documented * _TOLERANCE, (
        f"mean_pool_l2: peak {peak / 1e6:.1f} MB against a documented "
        f"{documented / 1e6:.1f} MB"
    )


def test_the_docstring_states_what_the_returned_matrix_costs():
    """``decode_float32_blobs`` returns the WHOLE matrix, which the old
    "never materialises" sentence read against. The docstring has to say
    which figure is the scan's scratch and which is the caller's own."""
    doc = vecmath.__doc__ or ""
    assert "O(1) in the table's row count" in doc
    assert "rows x dims x 4" in doc
