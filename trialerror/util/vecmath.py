"""One optional-numpy fast path for every brute-force cosine scan in the
tree -- and one plain-Python path that is still the definition of the answer.

**The problem this lane measured.** Four separate modules score vectors by
hand, each with its own copy of the same loop:
:func:`trialerror.retrieve.vecsearch.cosine_similarity` /
``rank_by_query_vector``, :func:`trialerror.lens.stratify.cosine_distance` /
``score_candidates``, :func:`trialerror.lens.vectors.mean_pool_l2` and the
novelty screen's own corpus / archive / inventory nearest-neighbour passes.
Every one of them is a Python loop over 2048-dimension vectors, and every one
of them is fine on the few hundred rows a full-text prefilter hands it and
hopeless on the hundred thousand an unbounded pass does: a 36-subject
calibration batch over a 108k-chunk corpus ran for more than ten minutes,
almost all of it per-float Python objects.

**numpy is OPTIONAL and stays optional.** It is imported lazily, never at
module import, and every entry point here works without it. ``pyproject``
carries it as the extra ``fast``; it is not a dependency of the package and
nothing in the tree may start assuming it. The knob is
``trialerror.toml``::

    [retrieve]
    numpy_fastpath = "auto"   # default: numpy when importable, else plain
    numpy_fastpath = "off"    # never numpy, whatever is installed

``off`` exists so a program that sees a difference it cannot explain has one
line to turn the whole thing off with, and so this module's own agreement
tests have something to compare against. ``TRIALERROR_NUMPY_FASTPATH`` sets the
same thing process-wide for a caller that has no program config in hand.

**What "agreement" means here, precisely, because the two paths are not the
same arithmetic.** CPython's ``sum()`` compensates its rounding since 3.12 and
numpy's reductions do not, so the last bits of a 2048-term dot product differ
between them; bit-identity is not available and this module does not pretend
it is. Instead:

* :func:`top_k` -- where identity actually matters, because a different
  order is a different answer -- uses numpy only to NARROW. It scores every
  row with numpy, takes a superset of the ``k`` best (everything within
  :data:`_SELECTION_MARGIN` of the ``k``-th, twelve orders of magnitude wider
  than the float64 error above, so the true top ``k`` and anything that
  exactly ties it cannot be outside it), and then computes the returned
  answer with :func:`cosine_one` and the plain ``(-score, id)`` sort. The
  returned ids, their order AND their scores are therefore produced by the
  plain path on both routes -- byte-identical, and independent of how numpy
  happened to block the scan. This is the same argument
  :mod:`trialerror.retrieve.vecmatrix` makes for the resident matrix, reused
  rather than re-derived.
* :func:`cosine_many`, :func:`score_candidates` and :func:`mean_pool_l2`
  return numpy's own float64 result on the fast path. It agrees with the
  plain path to ~1e-15 relative, far inside the 1e-6 this lane's brief asks
  for. The numpy route computes the SAME algebraic form as the plain one
  (``1 - cos`` per home vector, then the mean -- not ``1 - mean cos``), so
  two rows that genuinely tie in one path genuinely tie in the other.

**The tie-break is ``(-score, id)``, ascending on the id.** That was already
:func:`trialerror.retrieve.vecsearch.rank_by_query_vector`'s rule and
:func:`trialerror.lens.stratify.stratify`'s rule; it is written down here once
so a third caller cannot invent a fourth.

**Degenerate inputs never raise.** An empty vector, a length mismatch, or a
zero norm scores ``0.0`` similarity on both paths -- the "never crash a
ranking pass" posture ``cosine_similarity`` already had, applied uniformly.
(:func:`trialerror.lens.stratify.cosine_distance`'s own convention, distance
``1.0`` for the same cases, is exactly ``1 - 0.0`` and so is preserved by
construction.) ``NaN`` is not degenerate and is not special-cased: it
propagates on both paths, as it did before.

**Memory is bounded by construction.** Every scan here is blocked, and the
block is sized from the vector width, never from the table's row count. A
block holds at most :data:`MAX_BLOCK_ROWS` rows AND at most
:data:`_BLOCK_BYTES` of packed float32 source (``block x dims x 4`` bytes),
whichever is smaller; the working copy the arithmetic runs on is the same
block in float64, so peak scratch is ``block x dims x 8`` bytes plus two
row-length vectors. At 2048 dimensions that is a 4,096-row block and 64 MB
of scratch; at 256 dimensions it is the 20,000-row ceiling and 41 MB. A
three-million-row table is SCANNED in exactly the same 64 MB a hundred-row
one would need a fraction of: the bound is O(1) in the table's row count.

That figure is the scan's own scratch and nothing else. Every reduction
here is written to reduce in place (``einsum`` rather than
``(block * block).sum(axis=1)``, which would make a second full-size copy
and double the peak -- lane FB-7 fix pass, V-5), and
``tests/test_util_vecmath_memory.py`` measures it rather than trusting the
sentence. What a CALLER then holds is its own: :func:`decode_float32_blobs`
and :func:`trialerror.retrieve.vecsearch.fetch_vector_matrix` return the
whole matrix, which is ``rows x dims x 4`` bytes resident by definition --
that is what a matrix is, and it is still an order of magnitude below the
per-float Python objects it replaces.
"""

from __future__ import annotations

import math
import os
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "NUMPY_FASTPATH_MODES",
    "DEFAULT_NUMPY_FASTPATH",
    "MAX_BLOCK_ROWS",
    "numpy_fastpath_mode",
    "numpy_module",
    "numpy_enabled",
    "cosine_one",
    "cosine_many",
    "top_k",
    "mean_pool_l2",
    "score_candidates",
    "decode_float32_blobs",
]

#: The two values ``[retrieve] numpy_fastpath`` accepts.
NUMPY_FASTPATH_MODES: tuple[str, ...] = ("auto", "off")

#: What an unset (or unreadable) knob means: use numpy when it imports.
DEFAULT_NUMPY_FASTPATH = "auto"

#: Process-wide override for a caller with no program config in hand.
NUMPY_FASTPATH_ENV = "TRIALERROR_NUMPY_FASTPATH"

#: Hard ceiling on a scan block, in rows. The brief's bound.
MAX_BLOCK_ROWS = 20_000

#: Byte ceiling on the float32 source of one block. The block actually used
#: is ``min(MAX_BLOCK_ROWS, _BLOCK_BYTES // (dims * 4))``, floored at 1 -- so
#: a wide corpus blocks by bytes and a narrow one by rows, and neither ever
#: sizes a block from the table's row count. See the module docstring for the
#: resulting peak.
_BLOCK_BYTES = 32 * 1024 * 1024

#: Below this many rows the plain path wins outright: importing numpy,
#: building an array and calling BLAS costs more than a few dozen Python
#: cosines, and every bounded (full-text-prefiltered) caller in the tree is
#: under it. Deliberately the same posture as
#: :data:`trialerror.retrieve.vecmatrix.DEFAULT_MIN_UNIVERSE`, an order of
#: magnitude lower because there is no matrix to load here.
MIN_FASTPATH_ROWS = 256

#: How far below the ``k``-th numpy score a row may be and still be re-scored
#: exactly by :func:`top_k`. Twelve orders of magnitude above the float64
#: error between the two paths, and still tight enough that a normal query
#: re-scores a handful of rows rather than the table.
_SELECTION_MARGIN = 1e-9

_NOTED: set[str] = set()


def _note_once(key: str, message: str) -> None:
    """One line on stderr the first time a bad knob value is seen -- the same
    posture :func:`trialerror.retrieve.lexical.note_once` takes for
    ``fulltext_backend``, so a typo is visible without being fatal."""
    if key in _NOTED:
        return
    _NOTED.add(key)
    import sys

    print(f"[trialerror.util.vecmath] {message}", file=sys.stderr)


def numpy_fastpath_mode(config: Mapping[str, Any] | None = None) -> str:
    """``[retrieve] numpy_fastpath`` normalized, else the
    :data:`NUMPY_FASTPATH_ENV` process override, else
    :data:`DEFAULT_NUMPY_FASTPATH`.

    An unrecognized value is noted once and treated as unset -- a typo in a
    performance knob must not stop a program from answering."""
    raw: Any = None
    section = (config or {}).get("retrieve") if isinstance(config, Mapping) else None
    if isinstance(section, Mapping):
        raw = section.get("numpy_fastpath")
    if raw is None:
        raw = os.environ.get(NUMPY_FASTPATH_ENV)
    if raw is None:
        return DEFAULT_NUMPY_FASTPATH
    name = str(raw).strip().lower()
    if name not in NUMPY_FASTPATH_MODES:
        _note_once(
            f"bad_mode:{name}",
            f"trialerror.toml [retrieve] numpy_fastpath={raw!r} is not one of "
            f"{NUMPY_FASTPATH_MODES!r}; using {DEFAULT_NUMPY_FASTPATH!r}",
        )
        return DEFAULT_NUMPY_FASTPATH
    return name


def numpy_module(config: Mapping[str, Any] | None = None, *, mode: str | None = None):
    """The numpy module, or ``None`` when the fast path is not available
    here: the knob says ``off``, numpy is not installed, or its import
    raises (a broken build is "no fast path", never an error).

    Deliberately NOT cached across calls. ``sys.modules`` already makes the
    repeat import a dict lookup, and a cache here would make the "numpy is
    missing" test unwritable -- a test that cannot simulate an absent
    optional dependency is not testing that it is optional."""
    if (mode or numpy_fastpath_mode(config)) == "off":
        return None
    try:
        import numpy  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - absent, or a broken build: both mean "no fast path"
        return None
    return numpy


def numpy_enabled(config: Mapping[str, Any] | None = None, *, mode: str | None = None) -> bool:
    """Whether a scan here would actually use numpy. Public because the
    doctor and the IMPL report both have to distinguish "off by knob" from
    "numpy not installed"."""
    return numpy_module(config, mode=mode) is not None


def _block_rows(dims: int) -> int:
    """Rows per block for a ``dims``-wide scan -- the module docstring's
    bound, in one place so every scan here uses the same one."""
    if dims <= 0:
        return MAX_BLOCK_ROWS
    return max(1, min(MAX_BLOCK_ROWS, _BLOCK_BYTES // (dims * 4)))


# ---------------------------------------------------------------------------
# the plain path -- the definition of the answer
# ---------------------------------------------------------------------------


def cosine_one(a: Sequence[float], b: Sequence[float]) -> float:
    """Plain-Python cosine similarity of two vectors, no numpy, ever.

    This is the function the fast path is checked against and the function
    :func:`top_k` computes its returned scores with, so it is never routed
    anywhere: a single pair is far too small for numpy to pay for itself,
    and a "fast" variant of the reference answer would have nothing left to
    be the reference for.

    ``0.0`` for an empty vector, a length mismatch or a zero norm."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _as_pairs(rows: Mapping[str, Sequence[float]] | Iterable[tuple[str, Sequence[float]]]) -> list[tuple[str, Sequence[float]]]:
    if isinstance(rows, Mapping):
        return [(str(cid), vec) for cid, vec in rows.items()]
    return [(str(cid), vec) for cid, vec in rows]


# ---------------------------------------------------------------------------
# scans
# ---------------------------------------------------------------------------


def _cosine_many_plain(query: Sequence[float], matrix_rows: Any) -> list[float]:
    """The plain scan. A numpy row is converted to a list of Python floats
    first: ``cosine_one`` over a float32 ndarray would do float32 arithmetic
    and return an answer neither path should ever produce. The fast path is
    allowed to be faster, never less precise."""
    if hasattr(matrix_rows, "shape") and getattr(matrix_rows, "ndim", 0) == 2:
        return [cosine_one(query, [float(v) for v in row]) for row in matrix_rows]
    return [cosine_one(query, row) for row in matrix_rows]


def cosine_many(
    query: Sequence[float],
    matrix_rows: Any,
    *,
    config: Mapping[str, Any] | None = None,
    mode: str | None = None,
) -> list[float]:
    """Cosine similarity of ``query`` against every row of ``matrix_rows``,
    in row order.

    ``matrix_rows`` is a sequence of vectors or a 2-D numpy array (what
    :func:`decode_float32_blobs` returns on the fast path, handed straight
    back in without a round trip through Python floats).

    Rows whose width differs from ``query``'s score ``0.0``, as do zero-norm
    rows and every row when the query itself is empty or zero-norm -- the
    plain path's convention, preserved on both routes. On the numpy route
    the scan is blocked (module docstring's bound) and the result agrees
    with the plain path to ~1e-15 relative."""
    np = numpy_module(config, mode=mode)
    n_rows = len(matrix_rows)
    dims = len(query)
    if np is None or n_rows < MIN_FASTPATH_ROWS or dims == 0:
        return _cosine_many_plain(query, matrix_rows)

    query64 = np.asarray(query, dtype=np.float64)
    query_norm = float(np.sqrt(query64 @ query64))
    if not query_norm:
        return [0.0] * n_rows

    is_array = hasattr(matrix_rows, "shape") and getattr(matrix_rows, "ndim", 0) == 2
    if is_array and int(matrix_rows.shape[1]) != dims:
        # A whole matrix of the wrong width: every row is a length mismatch,
        # which the plain path scores 0.0.
        return [0.0] * n_rows
    if is_array:
        # A ``range``, not a list: a matrix has no ragged rows to filter
        # out, so the indices are known by arithmetic and materialising a
        # hundred thousand Python ints to hold them would cost more than the
        # scratch this module bounds (lane FB-7 fix pass, V-5).
        index_list: Sequence[int] = range(n_rows)
    else:
        # A ragged sequence is split rather than padded: only the rows that
        # can be scored go into a block, the rest keep the plain path's 0.0.
        index_list = [i for i, row in enumerate(matrix_rows) if len(row) == dims]
        if not index_list:
            return [0.0] * n_rows

    out = [0.0] * n_rows
    step = _block_rows(dims)
    block = None
    for start in range(0, len(index_list), step):
        window = index_list[start : start + step]
        # Release the PREVIOUS block before allocating the next one. Without
        # this the two overlap for the duration of the ``asarray`` call and
        # the peak is two blocks, not the one the module docstring states
        # (lane FB-7 fix pass, V-5).
        block = None
        if is_array:
            block = np.asarray(matrix_rows[window[0] : window[-1] + 1], dtype=np.float64)
        else:
            block = np.asarray([matrix_rows[i] for i in window], dtype=np.float64)
        dots = block @ query64
        # ``einsum``, not ``(block * block).sum(axis=1)``: the latter
        # materialises a SECOND full-size float64 copy of the block, which
        # doubled the peak this module's docstring states (lane FB-7 fix
        # pass, V-5). einsum reduces in place, so the scratch really is one
        # block. ``np.linalg.norm(block, axis=1)`` would not have helped --
        # it builds the same temporary internally.
        norms = np.sqrt(np.einsum("ij,ij->i", block, block))
        with np.errstate(divide="ignore", invalid="ignore"):
            # ``== 0.0``, NOT ``> 0``: a NaN norm is not a zero norm. The
            # plain path's zero-check is ``norm == 0.0`` too, so a NaN row
            # divides and propagates NaN on both routes; ``> 0`` would quietly
            # turn it into a 0.0 the plain path never produces, and top_k's
            # identity claim rests on the two agreeing about it.
            scores = np.where(norms == 0.0, 0.0, dots / (norms * query_norm))
        for offset, index in enumerate(window):
            out[index] = float(scores[offset])
    return out


def top_k(
    query: Sequence[float],
    rows: Mapping[str, Sequence[float]] | Iterable[tuple[str, Sequence[float]]],
    k: int | None = None,
    *,
    config: Mapping[str, Any] | None = None,
    mode: str | None = None,
) -> list[tuple[str, float]]:
    """The ``k`` rows of ``rows`` most similar to ``query``, best first,
    ties broken on the id ascending. ``k`` of ``None`` ranks everything.

    **Byte-identical on both routes, by construction.** numpy is used only
    to narrow to a superset of the answer; the scores returned and the sort
    that orders them are always :func:`cosine_one` and ``(-score, id)``.
    See the module docstring for why that is the only honest way to have
    both a fast path and an unchanged answer.

    ``k=None`` takes the plain path outright: a full ranking needs every
    exact score anyway, so there is nothing for a narrowing pass to save,
    and scoring it with numpy would put float64 noise into an ordering that
    callers (RRF fusion, tercile cuts) read positionally."""
    pairs = _as_pairs(rows)
    k_wanted = None if k is None else max(int(k), 0)
    if k_wanted == 0:
        return []

    np = None if k_wanted is None else numpy_module(config, mode=mode)
    if np is not None and len(pairs) >= MIN_FASTPATH_ROWS:
        scores = cosine_many(query, [vec for _cid, vec in pairs], config=config, mode=mode)
        array = np.asarray(scores, dtype=np.float64)
        # A NaN anywhere in the scores makes both the partition and the
        # threshold meaningless (NaN compares false against every bound, so
        # a superset built from one could silently drop the very rows the
        # plain path would sort). Narrowing is an optimisation; the answer
        # is not. Hand the whole set to the exact pass instead.
        if bool(np.isfinite(array).all()):
            take = min(k_wanted, array.shape[0])
            partition = np.argpartition(-array, take - 1)[:take]
            threshold = float(array[partition].min()) - _SELECTION_MARGIN
            # ``>=`` and the margin together: everything that could beat or
            # exactly tie the k-th once the exact cosine is recomputed.
            keep = np.nonzero(array >= threshold)[0]
            pairs = [pairs[int(i)] for i in keep]

    scored = [(cid, cosine_one(query, vec)) for cid, vec in pairs]
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return scored if k_wanted is None else scored[:k_wanted]


def mean_pool_l2(
    vectors: Any,
    *,
    config: Mapping[str, Any] | None = None,
    mode: str | None = None,
) -> list[float] | None:
    """Element-wise mean of ``vectors``, L2-renormalized to unit length.

    ``None`` for an empty input (a document with zero embedded chunks) or
    when the mean is the zero vector. Raises ``ValueError`` when the rows
    disagree about width -- chunks embedded under two model keys mixed into
    one pool, which is a caller data problem and not something to average
    over.

    The numpy route sums float64 in blocks (module docstring's bound) and
    agrees with the plain one to ~1e-15 relative."""
    n_rows = len(vectors)
    if n_rows == 0:
        return None
    is_array = hasattr(vectors, "shape") and getattr(vectors, "ndim", 0) == 2
    dims = int(vectors.shape[1]) if is_array else len(vectors[0])

    np = numpy_module(config, mode=mode)
    if np is None or n_rows < MIN_FASTPATH_ROWS:
        sums = [0.0] * dims
        for vec in vectors:
            if len(vec) != dims:
                raise ValueError(
                    f"mean_pool_l2: inconsistent vector dims ({len(vec)} != {dims}) — "
                    "chunks embedded under different model_keys were mixed"
                )
            for i, value in enumerate(vec):
                sums[i] += value
        mean = [s / float(n_rows) for s in sums]
        norm = math.sqrt(sum(x * x for x in mean))
        return None if norm == 0.0 else [x / norm for x in mean]

    if not is_array:
        for vec in vectors:
            if len(vec) != dims:
                raise ValueError(
                    f"mean_pool_l2: inconsistent vector dims ({len(vec)} != {dims}) — "
                    "chunks embedded under different model_keys were mixed"
                )
    total = np.zeros(dims, dtype=np.float64)
    step = _block_rows(dims)
    block = None
    for start in range(0, n_rows, step):
        block = None  # see cosine_many: one block resident, never two
        block = np.asarray(vectors[start : start + step], dtype=np.float64)
        total += block.sum(axis=0)
    mean_a = total / float(n_rows)
    norm = float(np.sqrt(mean_a @ mean_a))
    if norm == 0.0:
        return None
    return [float(x) for x in (mean_a / norm)]


def score_candidates(
    candidates: Mapping[str, Sequence[float]],
    home: Mapping[str, Sequence[float]],
    *,
    config: Mapping[str, Any] | None = None,
    mode: str | None = None,
) -> dict[str, float]:
    """``candidate_id -> mean cosine DISTANCE (1 - similarity) to every
    vector in ``home``.

    Degenerate pairs (empty, width mismatch, zero norm) contribute a
    distance of ``1.0``, which is ``1 - 0.0`` and so needs no branch of its
    own on either route.

    The numpy route computes ``1 - cos`` per home vector and then the mean,
    the same algebraic form the plain loop uses rather than the cheaper
    ``1 - mean(cos)`` -- so two candidates that genuinely tie still tie, and
    :func:`trialerror.lens.stratify.stratify`'s ``(score, id)`` cut lands in
    the same place on both routes."""
    if not candidates or not home:
        return {}
    home_vectors = list(home.values())
    np = numpy_module(config, mode=mode)
    if np is None or len(candidates) < MIN_FASTPATH_ROWS:
        return {
            cid: sum(1.0 - cosine_one(vec, h) for h in home_vectors) / len(home_vectors)
            for cid, vec in candidates.items()
        }
    ids = list(candidates)
    rows = [candidates[cid] for cid in ids]
    per_home = [cosine_many(h, rows, config=config, mode=mode) for h in home_vectors]
    n_home = float(len(home_vectors))
    return {
        cid: sum(1.0 - per_home[j][i] for j in range(len(home_vectors))) / n_home
        for i, cid in enumerate(ids)
    }


def decode_float32_blobs(
    blobs: Sequence[bytes],
    *,
    config: Mapping[str, Any] | None = None,
    mode: str | None = None,
) -> Any:
    """Decode packed little-endian float32 BLOBs (the on-disk shape
    :func:`trialerror.stores.vecindex.serialize_vector_fallback` writes) into
    a 2-D numpy float32 array on the fast path, or a list of lists on the
    plain one.

    The point of the fast path is that ``numpy.frombuffer`` over the joined
    bytes creates no per-float Python object at all -- which is where an
    unbounded scan's wall clock actually goes. The join is blocked
    (module docstring's bound) so a 100k-row table is never one 800 MB
    bytes object.

    Returns ``None`` when the blobs disagree about width: a ragged set
    cannot become a matrix, and padding or truncating it would be a
    different corpus. The caller falls back to decoding row by row, exactly
    as it did before this module existed."""
    if not blobs:
        return None
    width = len(blobs[0])
    if width == 0 or width % 4 or any(len(b) != width for b in blobs):
        return None
    dims = width // 4
    np = numpy_module(config, mode=mode)
    if np is None:
        import struct

        return [list(struct.unpack(f"<{dims}f", b)) for b in blobs]
    step = _block_rows(dims)
    out = np.empty((len(blobs), dims), dtype=np.float32)
    for start in range(0, len(blobs), step):
        window = blobs[start : start + step]
        out[start : start + len(window)] = np.frombuffer(
            b"".join(window), dtype="<f4", count=len(window) * dims
        ).reshape(len(window), dims)
    return out
