"""Lexicon exceptions. Same shape as ``trialerror.ingest.errors`` and
``trialerror.stores.errors``: one base class a caller that only cares "did
this refuse" can catch, plus a specific subclass per refusal so a caller
that must branch on *why* never has to match on message text.

The design's rule for this package is stronger than the house default and
is stated here because these classes are what make it checkable: **every
refusal is a named error**. There is no path through
:mod:`trialerror.lexicon.api` that returns a falsy value or a ``None`` to
mean "no". A sense with no evidence, a decision without a launch, a gloss
that quotes instead of paraphrasing, a second decision on an already-decided
relation, a decision whose member senses have gone stale -- each raises the
class below that says which invariant refused, before anything is written.

**How far "before anything is written" reaches, precisely.** Every check a
caller's own arguments can fail runs ahead of the first write: the closed
vocabularies, the gloss cap, the lemma key, the evidence specifications and
their ``source_key`` resolution, the alias vocabulary, the idempotency
lookup, and the member senses a relation decision names. A refusal from any
of them leaves the store byte-identical, and that is the guarantee these
classes carry. (Fix pass, finding F2: the alias vocabulary used to be
checked three writes in, so this paragraph used to claim more than the code
delivered.)

What is NOT claimed is transactional rollback.
``trialerror.stores.writer`` commits each ``insert``/``update`` in its own
transaction (``with conn:`` per statement) and an outer ``BEGIN`` cannot
wrap them -- the first nested ``with conn:`` commits it -- so making a
multi-row write atomic would mean changing the writer every subsystem
shares, not this package. The residue is bounded and worth naming: once the
argument checks pass, a lost race for a UNIQUE key, a disk error or a killed
process can still leave a partly-written proposal. The store stays
*consistent* under that -- the adversarial pass measured zero duplicate
lemmas, zero duplicate origins and zero orphan terms across four concurrent
importers, and a crashed ``propose``'s orphan term is re-adopted by
``find_term`` on the next pass -- but it is a real state, not an impossible
one.
"""

from __future__ import annotations

__all__ = [
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


class LexiconError(Exception):
    """Base class for every error the ``trialerror.lexicon`` package raises."""


class UnsupportedSqliteError(LexiconError, ImportError):
    """The running SQLite build cannot host the term store -- specifically,
    it is older than the 3.34 that FTS5's ``trigram`` tokenizer needs, so
    ``term_fts`` could not be created (design §11, risks).

    Deliberately also an :class:`ImportError`. An environment that cannot
    host this package is, from a consumer's point of view, indistinguishable
    from the package not being installed, and the consumers written against
    that possibility guard on ``ImportError`` -- ruling L-C5's Evidence hook
    (``trialerror.dashboard.data._evidence_lexicon_conflicts``) is the one
    already in the tree. Making this a plain ``LexiconError`` would turn a
    graceful "the region is omitted with a stated reason" into a dashboard
    traceback on the one class of machine that cannot do anything about it.
    """


class LaunchRequiredError(LexiconError):
    """A decision was attempted with no ``by_launch``, or a relation was
    being marked ``marked_by_kind='launch'`` with no launch id.

    Distinct from :class:`~trialerror.stores.errors.XidTargetMissingError`,
    which is what a launch id that names *no row* raises: this one is the
    absence of the field itself. Both are refusals before any write; ruling
    L-E4 is the rule they enforce between them ("a term decision needs an
    existing ``platform.launch`` row -- the XID guard, never a fallback").
    """


class InvalidTermInputError(LexiconError):
    """A field the caller supplied cannot be stored as given: an empty lemma
    or gloss, or a value outside one of the closed vocabularies in
    :mod:`trialerror.lexicon.policy`.

    Raised BEFORE SQLite would raise its own CHECK violation, on purpose.
    The DDL constraint is the backstop and stays; this class is what makes
    the refusal say which field, what was given, and what the legal set is
    -- an integrity-violation message names the constraint, not the fix.
    """


class GlossTooLongError(LexiconError):
    """A sense gloss exceeded :data:`trialerror.lexicon.policy.GLOSS_MAX_WORDS`.

    The cap is not a formatting preference. A gloss is the program's OWN
    words for a term; a long one is, in practice, a transcription of the
    source rather than a reading of it, which is the thing D-COC-1 and the
    fence exist to prevent. The quote lives in the evidence row, where it
    is anchored and fenced; the gloss is what the program says about it.
    """


class SenseWithoutEvidenceError(LexiconError):
    """A sense was proposed with no evidence, or one was being moved to
    ``current`` with no non-retracted evidence left under it -- the
    quote-grounding law applied to the lexicon. Checked twice on purpose:
    once at propose time, once again at accept time, because evidence can
    be retracted in between."""


class InvalidEvidenceError(LexiconError):
    """An evidence specification could not be turned into a row: an unknown
    ``evidence_kind``, a missing anchor/ref id, or a ``source_key`` that
    could neither be supplied nor derived from the referenced row. The
    disjoint-source conflict rule counts ``source_key``, so evidence whose
    source cannot be named would silently distort every conflict verdict
    the store later reaches -- refused instead."""


class TermNotFoundError(LexiconError):
    """No ``term`` row exists with the given ``term_id``."""


class SenseNotFoundError(LexiconError):
    """No ``term_sense`` row exists with the given ``sense_id``."""


class RelationNotFoundError(LexiconError):
    """No ``term_relation`` row exists with the given ``rel_id``."""


class SenseNotDecidableError(LexiconError):
    """A sense lifecycle call was made against a sense in a status that does
    not permit it -- accepting one that is not ``proposed``, superseding one
    that is not ``current``. The compounding layer's "new rows only, never a
    silent redo" discipline: a decision, once made, is superseded in the
    open, not overwritten."""


class RelationNotPendingError(LexiconError):
    """``decide_relation`` was called against a relation that is no longer
    ``pending`` (already confirmed, rejected, or superseded). Same posture
    as ``trialerror.ingest.errors.CandidateNotPendingError`` for the
    merge-review queue, and the reason a later system scan seeing a
    ``rejected`` row leaves it alone rather than reopening it."""


class InvalidDecisionError(LexiconError):
    """A decision verb outside the closed set
    :data:`trialerror.lexicon.policy.RELATION_DECISIONS`, or one that does
    not apply to the relation being decided (``not_conflict`` without an
    ``into`` sense, ``scoped`` on a duplicate candidate, a bare
    ``conflicts_with`` offered as a resolution when it is the question)."""


class MissingDisambiguatorError(LexiconError):
    """A ``scoped`` decision left one of the member senses without a
    disambiguator. Splitting a term is the decision to keep both readings,
    and a kept reading with no way to name it apart from its sibling is the
    one state the head layer is not allowed to be in (the
    ``term_split_missing_disambiguator`` doctor check audits the same
    invariant from the outside)."""


class InvalidMergeError(LexiconError):
    """A merge that cannot mean anything: a term into itself, into a term
    that is itself already merged away, or of a term that has already been
    merged. Nothing is deleted by a merge, so a second one would leave two
    live ``merged_into`` chains disagreeing about where a lemma went."""
