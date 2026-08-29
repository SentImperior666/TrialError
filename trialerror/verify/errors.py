"""Verification-subsystem exceptions. Mirrors ``trialerror.stores.errors``/
``trialerror.artifacts.errors``/``trialerror.retrieve.errors``'s split: a caller that
only cares "did the verify operation fail" catches :class:`VerifyError`; a
caller that needs to branch on *why* catches the specific subclass.
"""

from __future__ import annotations

__all__ = [
    "VerifyError",
    "InvalidProcedureError",
    "InvalidSubjectKindError",
    "PreregNotFoundError",
    "PreregTamperedError",
    "PreregVoidedError",
    "VerdictNotFoundError",
    "ReproductionRefError",
    "CitecheckError",
]


class VerifyError(Exception):
    """Base class for every error :mod:`trialerror.verify` raises."""


class InvalidProcedureError(VerifyError):
    """A ``verdict.procedure`` or ``prereg`` procedure value failed a
    structural check this subsystem enforces on top of SQLite's own CHECK
    constraint (e.g. an empty/blank procedure text at ``prereg commit``, or
    a ``verdict.procedure`` outside the DDL's fixed enum)."""


class InvalidSubjectKindError(VerifyError):
    """A ``verdict.subject_kind`` value was not one of ``hypothesis|claim|
    citation|artifact`` (the DDL's CHECK constraint, re-verified here so
    :mod:`trialerror.verify.verdicts` returns a clean, typed refusal before
    ever reaching ``trialerror.stores.insert``)."""


class PreregNotFoundError(VerifyError):
    """No ``prereg`` row exists with the given ``prereg_id``."""


class PreregTamperedError(VerifyError):
    """``prereg reveal`` recomputed the escrowed content's sha256 and it
    does NOT match the sha stamped at commit time — design Section 4.2:
    "reveal w/ tampered escrow refused". The prereg row is marked
    ``voided`` as a side effect (never silently accepted)."""


class PreregVoidedError(VerifyError):
    """An operation was attempted against a ``prereg`` row already in
    ``voided`` status (a previously-tampered or explicitly-voided
    commitment) — reveal/compliance checks against a voided prereg are
    refused rather than silently reporting non-compliance."""


class VerdictNotFoundError(VerifyError):
    """No ``verdict`` row exists with the given ``verdict_id`` (e.g. the
    target of ``trialerror verify reproduce``)."""


class ReproductionRefError(VerifyError):
    """A verdict's ``reproduction_ref`` is missing, malformed (not the
    ``{"script", "args"?, "expected_sha256"}`` shape this runner requires),
    or names a script that could not be found/executed."""


class CitecheckError(VerifyError):
    """A structural refusal in the citecheck pipeline (e.g. an
    unrecognized citation-marker syntax when a caller demands strict
    parsing)."""
