"""``trialerror.lens`` — ideation/lens tooling (design Section 3.1 subsystem L,
Section 9.6, Section 12 M13 row): lens rosters (vantage axis), corpus-slice
stratification + seeded quota assignment (AMENDMENT-3 generalized), and the
idea content pipeline that feeds M5's full-text feed posting.

TRIALERROR-DEV-NOTE (package path deviation): design Section 3.1's repo layout
and Section 12's M13 row both name this package ``trialerror/ideation/`` (with
``lens.py`` as a single file inside it). This build's explicit lane
isolation (build brief: "LANE ISOLATION: ONLY ``trialerror/lens/`` (new)") names
``trialerror/lens/`` instead — the CLI group table (design Section 5.2) already
independently names the CLI group itself ``lens`` ("``lens`` | roster,
stratify, assign, log"), so this package name matches the CLI surface even
though it departs from Section 3.1/12's stated package path. Flagged here
for whoever does the next design-doc pass; no functionality differs, only
the import path (``trialerror.lens.*`` instead of ``trialerror.ideation.lens``).

Public surface:

- :mod:`trialerror.lens.roster` — ``add_lens``/``list_roster`` (``lens_roster``).
- :mod:`trialerror.lens.vectors` — doc-pooled (mean+L2-renorm) embedding fetch.
- :mod:`trialerror.lens.stratify` — distance scoring + empirical-tercile cut.
- :mod:`trialerror.lens.quota` — weight/floor quota math + seeded draw.
- :mod:`trialerror.lens.assign` — the pure planner + the ``Store``-writing
  wrapper that logs ``lens_assignment`` rows.
- :mod:`trialerror.lens.ideas` — the ``idea`` table content-pipeline writer.
- :mod:`trialerror.lens.export` — launch-bookable rows for ``trialerror.budget.book_launch``.
- :mod:`trialerror.lens.checks` — doctor checks (auto-discovered).
- :mod:`trialerror.lens.errors` — ``LensError`` and its subclasses.
"""

from __future__ import annotations

from trialerror.lens.errors import (
    DuplicateSliceError,
    InsufficientCandidatesError,
    LensError,
    MissingEmbeddingError,
    UnknownRosterError,
)

__all__ = [
    "LensError",
    "InsufficientCandidatesError",
    "MissingEmbeddingError",
    "UnknownRosterError",
    "DuplicateSliceError",
]
