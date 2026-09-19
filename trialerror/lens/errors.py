"""Lens-subsystem exceptions.

Mirrors ``trialerror.budget.errors``'s split (see that module's docstring): a
caller that only cares "did the ideation operation fail" catches
:class:`LensError`; a caller that needs to branch on *why* catches the
specific subclass. These are reserved for STRUCTURAL refusals — an
unsatisfiable quota, a missing embedding, an unknown roster/round — not
soft "this round happens to have fewer candidates than usual" cases, which
callers are expected to size their candidate pool to avoid (design Section
12, M13 row: "weights/floor honored" is an acceptance bar, not a best-effort
one — a shortfall against it must be loud, never silently under-filled).
"""

from __future__ import annotations

__all__ = [
    "LensError",
    "InsufficientCandidatesError",
    "MissingEmbeddingError",
    "UnknownRosterError",
    "DuplicateSliceError",
    "ArmAllocationError",
]


class LensError(Exception):
    """Base class for every error :mod:`trialerror.lens` raises."""


class InsufficientCandidatesError(LensError):
    """The candidate pool (after any ``inter_cluster_mandate`` filtering)
    does not have enough members in some arm to satisfy that arm's quota —
    including the far-arm floor. Raised rather than silently under-filling
    or silently violating the mandate (design Section 12, M13 row:
    "weights/floor honored" — a refuse-loud bar, matching
    ``trialerror.budget.book_launch``'s "over-cap book refused" precedent)."""


class MissingEmbeddingError(LensError):
    """A candidate or home document has no doc-pooled vector available
    (the design's "vector tier absent" case — see
    ``trialerror.retrieve.vecsearch.fetch_vectors``'s own docstring: "no vector"
    is ordinarily silent-absent at the chunk-search layer, but stratification
    cannot score a candidate it cannot place, so this module raises instead
    of silently dropping it — an unscoreable candidate is a data problem the
    caller needs to see, not a decision this module should make for them)."""


class UnknownRosterError(LensError):
    """A ``roster_id`` (or ``round_id``) named by a caller does not exist in
    ``ops.lens_roster``."""


class ArmAllocationError(LensError):
    """``arm_mode="per_lens"``: the roster-level arm quota cannot seat every
    lens. Two ways to get here, both structural: more pre-placed seats want
    one arm than that arm's quota has room for (e.g. three
    assumption-busters against a far quota of two), or the quota itself does
    not sum to the roster size because the far-lens floor exceeds it. Raised
    rather than quietly demoting a buster out of the far arm or a control
    out of the modal arm — either silent fix would make the round report an
    arm balance it did not run."""


class DuplicateSliceError(LensError):
    """A direct-write (or a caller-supplied ``allow_reuse=False`` violation)
    would assign the same candidate slice to more than one lens within a
    round — the "no duplicate sets" coverage invariant (build brief)."""
