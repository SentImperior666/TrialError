"""``trialerror.artifacts`` — the typed-artifact registry + gate state machine.
Design Section 2 (subsystem table, row J): "Typed registry + gate state
machine + reproduction hooks." Design Section 12 (M10 row): "registry +
template table (gated flag), gate state machine + transitions + edit-union
verification, template port." Design Section 9.4 (traceability map):
"artifact registry + gate state machine with per-edit verified flags +
reproduction hooks; two-tier validator-then-critic."

Generalizes origin-project's typed-artifact + critic-gate (CR-###) discipline
(REQUIREMENTS Sec 1.4): every research output is a typed, templated
artifact; gated types cannot be registered without passing a critic gate;
the gate's edit union (blocking findings) must be individually verified
before the gate can close.

Public surface
--------------
Registry (:mod:`trialerror.artifacts.registry`):

- :func:`create_artifact` — the one way to create a new artifact row
  (``status='draft'``, unregistered/ungated).
- :func:`get_artifact` / :func:`list_artifacts` — reads.
- :func:`get_template` — read one ``template`` row (the ``gated`` flag
  registration consults).
- :func:`register_artifact` — THE registration entry point. For
  ``template.gated=1`` types, refuses unless the artifact's gate is in
  ``union_applied``, and on success advances that gate
  ``union_applied -> registered`` in the SAME transaction as the artifact's
  own ``status='registered'`` flip ("register-then-close-gate ordering in
  one transaction").

Gates (:mod:`trialerror.artifacts.gates`):

- :func:`open_gate` — create a gate at ``draft`` for an existing artifact.
- :func:`advance_gate` — the generic, low-level state-transition entry
  point (``trialerror gate advance``); every other mutator below shares its
  transition-execution core.
- :func:`submit_gate` — ``draft -> submitted``.
- :func:`record_verdict` — records a critic's verdict/edits/reproduction
  fields AND advances the gate (``submitted -> gated`` or ``-> failed``)
  in one transaction.
- :func:`apply_union` — ``gated -> union_applied``, the F10 terminal-pass
  transition; enforces verdict ∈ {PASS, PASS_WITH_EDITS}, every blocking
  edit verified, and ``reproduction_status != mismatch``.
- :func:`verify_edit` — the applier-verifies layer: marks one ``edits``
  entry applied+verified (NOT a state transition).
- :func:`get_gate` — read.

State machine (:mod:`trialerror.artifacts.state_machine`):

- :data:`~trialerror.artifacts.state_machine.STATES`,
  :data:`~trialerror.artifacts.state_machine.LEGAL_TRANSITIONS`,
  :data:`~trialerror.artifacts.state_machine.TERMINAL_PASS_STATE` — the graph
  itself, importable standalone (e.g. for an exhaustive test over every
  ``(from_state, to_state)`` pair).

``trialerror/artifacts/checks.py`` registers this module's ``trialerror doctor``
checks (``gated_type_without_gate``, ``orphan_gate_transition``,
``gate_illegal_transition_history``) by the same auto-discovery convention
every other subsystem uses.

FX-9 (:mod:`trialerror.artifacts.template_seed`): ``trialerror/artifacts/templates/``
bundles a byte-exact, read-only port of the 12 origin-project ``research/templates/
*.md`` files; :func:`seed_builtin_templates` / :func:`list_builtin_templates`
seed/list them into any program's ``template`` table (``trialerror artifact
templates [--seed]``), independent of whether a origin-project migration ever runs.
"""

from __future__ import annotations

from trialerror.artifacts.errors import (
    ArtifactsError,
    GateEntryConditionError,
    IllegalTransitionError,
    RegistrationRefusedError,
)
from trialerror.artifacts.gates import (
    REPRODUCTION_STATUS_VALUES,
    VERDICT_VALUES,
    advance_gate,
    apply_union,
    get_gate,
    open_gate,
    record_verdict,
    submit_gate,
    verify_edit,
)
from trialerror.artifacts.registry import (
    create_artifact,
    get_artifact,
    get_template,
    list_artifacts,
    register_artifact,
)
from trialerror.artifacts.state_machine import (
    LEGAL_TRANSITIONS,
    STATES,
    TERMINAL_PASS_STATE,
    TERMINAL_STATES,
    assert_legal_transition,
    is_legal_transition,
)
from trialerror.artifacts.template_seed import (
    CANONICAL_PREFIXED_STEMS,
    TEMPLATES_DIR,
    builtin_template_rows,
    list_builtin_templates,
    seed_builtin_templates,
)

__all__ = [
    "ArtifactsError",
    "IllegalTransitionError",
    "GateEntryConditionError",
    "RegistrationRefusedError",
    "STATES",
    "TERMINAL_PASS_STATE",
    "TERMINAL_STATES",
    "LEGAL_TRANSITIONS",
    "is_legal_transition",
    "assert_legal_transition",
    "VERDICT_VALUES",
    "REPRODUCTION_STATUS_VALUES",
    "open_gate",
    "get_gate",
    "advance_gate",
    "submit_gate",
    "record_verdict",
    "apply_union",
    "verify_edit",
    "create_artifact",
    "get_artifact",
    "list_artifacts",
    "get_template",
    "register_artifact",
    "TEMPLATES_DIR",
    "CANONICAL_PREFIXED_STEMS",
    "builtin_template_rows",
    "seed_builtin_templates",
    "list_builtin_templates",
]
