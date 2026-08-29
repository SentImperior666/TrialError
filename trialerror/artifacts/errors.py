"""Artifacts/gates-subsystem exceptions.

Mirrors ``trialerror.stores.errors``'s and ``trialerror.budget.errors``'s split: a
caller that only cares "did the artifact/gate operation fail" catches
:class:`ArtifactsError`; a caller that needs to branch on *why* catches the
specific subclass. Reserved for STRUCTURAL refusals this subsystem itself
enforces on top of what ``trialerror.stores`` (FK/CHECK/XID) already catches —
an illegal state-machine edge, a failed transition-in entry condition, or a
registration attempted before its gate is in the one terminal-pass state.
"""

from __future__ import annotations

__all__ = [
    "ArtifactsError",
    "IllegalTransitionError",
    "GateEntryConditionError",
    "RegistrationRefusedError",
]


class ArtifactsError(Exception):
    """Base class for every error :mod:`trialerror.artifacts` raises."""


class IllegalTransitionError(ArtifactsError):
    """``trialerror gate advance`` (or any of its convenience wrappers —
    ``submit``/``verdict``/``apply-union``) was asked to move a gate along
    an edge the state machine does not allow (design Section 4.2: "``trialerror
    gate advance`` is the ONLY mutation path and refuses illegal
    transitions"). Covers both an unknown ``to_state`` and a structurally
    disallowed ``from_state -> to_state`` pair (e.g. ``draft ->
    registered``, or advancing a terminal state at all)."""


class GateEntryConditionError(ArtifactsError):
    """A transition was structurally legal (a real edge in the state
    graph) but refused because the entry conditions for ``to_state`` were
    not met. The only ``to_state`` with entry conditions today is
    ``union_applied`` (design Section 4.2, F10): verdict must be ``PASS``
    or ``PASS_WITH_EDITS``, every ``blocking`` edit must carry
    ``verified=true``, and ``reproduction_status`` must not be
    ``'mismatch'``."""


class RegistrationRefusedError(ArtifactsError):
    """``trialerror artifact register`` was refused: either the artifact's
    ``template.gated`` is true and its gate is not (yet) in
    ``union_applied`` — including the case of no gate at all — or the
    artifact has already been registered/superseded (design Section 4.2:
    "Registration closes a gate; never the reverse")."""
