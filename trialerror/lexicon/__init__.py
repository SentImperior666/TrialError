"""``trialerror.lexicon`` -- the term store. Design of record:
``docs/reviews/LANE_E_TERM_STORE_DESIGN.md`` (schema §2, lifecycle §3,
proposal routes §4, consumers §5, doctor checks §6, CLI §7); orchestrator
rulings in that document's §12.

What this package is for. A **term** is a lemma the program uses as a name.
A **sense** is one own-words reading of that term, grounded in
**evidence**. Polysemy is a property of the term, not a defect: one lemma
legitimately reads differently in two source systems. A **conflict** is the
specific case where two *current* senses of one term are grounded in
**disjoint source sets** -- if the two readings share a source, that is
nuance and the store says so rather than raising a flag.

Before this package, the Lexicon view was an honest proxy assembled from
``entity`` rows, definition ``claim`` rows and draft ``merge_proposal``
rows, with a contradiction read against a ``prov_edge`` table that had no
writers anywhere in the codebase. This is the real thing behind it.

The modules, in the order it is worth reading them (the CLI, the doctor
checks and the dashboard builder arrive with E3 and E4):

- :mod:`~trialerror.lexicon.errors` -- one named class per refusal.
- :mod:`~trialerror.lexicon.normalize` -- the lemma key function, and why
  it does not stem.
- :mod:`~trialerror.lexicon.policy` -- the closed vocabularies mirrored
  from the DDL, the decay/gloss/duplicate knobs, and the ``[lexicon]``
  config table.
- :mod:`~trialerror.lexicon.api` -- the whole write lifecycle plus the
  reads its consumers need.
- :mod:`~trialerror.lexicon.candidates` (E2) -- save-time duplicate
  surfacing: what the write API calls to ask "is there already a term for
  this?", answered with pending candidates and never with a merge. Its
  second stage -- and the verb that withdraws candidates opened before that
  stage existed -- arrived at build step 1c, once the first real register
  import measured what the first stage does at corpus scale.
- :mod:`~trialerror.lexicon.scan` (E2) -- the disjoint-source conflict
  rule, and the idempotent whole-store pass over it.
- :mod:`~trialerror.lexicon.backfill` (E2) -- the bulk routes in: register
  records, definition claims, and the relink that repoints their evidence
  at real sources once those are ingested.

**Three invariants this package will not break**, stated once here because
each is enforced in more than one place and a reader should know they are
meant to agree:

1. *A live reading always stands on evidence.* No code path writes
   ``status='current'`` without a live, non-retracted evidence row under it
   -- ``propose(status='current')`` runs the identical accept path a later
   ``accept_sense`` would, rather than short-cutting to the same column
   value -- **and no code path takes the last one away afterwards**:
   ``retract_evidence`` refuses when the row it was handed is the only live
   one under a ``proposed`` or ``current`` sense, and names the verb that
   ends a reading instead (fix pass, finding F1; before it, one retraction
   could strand a ``current`` sense while its term went on rendering that
   gloss with nothing underneath). The ``term_sense_without_evidence``
   doctor check audits the same population from outside -- between the two
   guards it is unreachable through this package, so a positive count means
   something wrote around the API.

2. *A machine judgment can only ever open a pending row.* A relation marked
   ``marked_by_kind='system'`` is created ``pending`` and can only be
   confirmed by a call that carries a real ``by_launch`` (MINING §5.3;
   ruling L-E4). Staleness never decides anything either -- ``review_after``
   sets a computed ``needs_review`` flag at read time and mutates no status
   (MINING §5.7).

3. *Nothing is deleted.* A wrong evidence row is retracted in place, a
   corrected gloss supersedes rather than overwrites, a merged term keeps
   every row it had and gains a ``merged_into`` pointer, and its old lemma
   survives as an alias. The head layer (``term``, ``term_alias``) is the
   only thing that updates in place, and every such update appends one
   type-keyed ``event``, so the head can be rebuilt from the layers below
   it.

**One environment requirement, asserted at import.** ``term_fts`` uses
FTS5's ``trigram`` tokenizer, which needs SQLite >= 3.34 (design §11).
Python 3.12's bundled build is far past that, so this is not expected to
fire -- but a CREATE VIRTUAL TABLE failing inside a migration would refuse
to open *every* store on that machine, with a message about a tokenizer,
so the check is made here where it can say what is actually wrong.
"""

from __future__ import annotations

import sqlite3

from trialerror.lexicon.errors import (
    GlossTooLongError,
    InvalidDecisionError,
    InvalidEvidenceError,
    InvalidMergeError,
    InvalidTermInputError,
    LaunchRequiredError,
    LexiconError,
    MissingDisambiguatorError,
    RelationNotFoundError,
    RelationNotPendingError,
    SenseNotDecidableError,
    SenseNotFoundError,
    SenseWithoutEvidenceError,
    TermNotFoundError,
    UnsupportedSqliteError,
)

__all__ = [
    "MIN_SQLITE_VERSION",
    "LexiconError",
    "UnsupportedSqliteError",
    "LaunchRequiredError",
    "InvalidTermInputError",
    "GlossTooLongError",
    "SenseWithoutEvidenceError",
    "InvalidEvidenceError",
    "TermNotFoundError",
    "SenseNotFoundError",
    "RelationNotFoundError",
    "SenseNotDecidableError",
    "RelationNotPendingError",
    "InvalidDecisionError",
    "MissingDisambiguatorError",
    "InvalidMergeError",
]

#: The SQLite release that added FTS5's ``trigram`` tokenizer.
MIN_SQLITE_VERSION: tuple[int, int, int] = (3, 34, 0)


def _require_trigram_fts5() -> None:
    """Refuse at import on a SQLite too old for ``term_fts``.

    Checked against ``sqlite3.sqlite_version_info`` rather than by trying a
    CREATE on a scratch connection: the version test is free, total, and
    cannot leave a temp file behind, while the empirical test would run on
    every import of every consumer of this package.
    """
    if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
        want = ".".join(str(p) for p in MIN_SQLITE_VERSION)
        raise UnsupportedSqliteError(
            f"trialerror.lexicon needs SQLite >= {want} for the term_fts trigram "
            f"tokenizer; this interpreter is linked against {sqlite3.sqlite_version}. "
            "The term store cannot be created or read on this build."
        )


_require_trigram_fts5()
