"""The generic ``knowledge.verdict`` writer. Design Section 4.1 (``verdict``
DDL, verbatim): "``verdict_id PK | subject_kind(hypothesis|claim|citation|
artifact) | subject_id | procedure(citecheck|contracrow|gate|reproduction|
custom) | procedure_version | label ... | evidence JSON [...] | prereg_id?
XID | prereg_compliant? BOOL | reproduction_ref? | ts | issued_by_launch``."

Every M9 procedure (:mod:`trialerror.verify.citecheck`,
:mod:`trialerror.verify.hypothesis`, :mod:`trialerror.verify.reproduce`) records
through :func:`record_verdict` — the ONE place a ``verdict`` row is ever
inserted by this package, mirroring the "one generic write path per table"
discipline ``trialerror.artifacts.gates``/``trialerror.stores.writer`` already
establish. ``trialerror/mcp/ops.py``'s ``record_verdict`` MCP tool (design
Section 5.1, tool #12) is rewired to call this function directly (see that
module's own TRIALERROR-DEV-NOTE, which this build supersedes) — the tool's
NAME, ``inputSchema``, and every observable error code/shape are unchanged;
only the body moved here.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from trialerror.stores import insert as store_insert
from trialerror.stores.store import Store
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now
from trialerror.verify.errors import InvalidProcedureError, InvalidSubjectKindError

__all__ = ["SUBJECT_KINDS", "PROCEDURES", "record_verdict"]

#: Matches ``verdict.subject_kind``'s CHECK constraint
#: (``trialerror/stores/schema/knowledge.py``).
SUBJECT_KINDS: frozenset[str] = frozenset({"hypothesis", "claim", "citation", "artifact"})

#: Matches ``verdict.procedure``'s CHECK constraint.
PROCEDURES: frozenset[str] = frozenset({"citecheck", "contracrow", "gate", "reproduction", "custom"})


def record_verdict(
    store: Store,
    *,
    subject_kind: str,
    subject_id: str,
    procedure: str,
    procedure_version: str,
    label: str,
    evidence: Sequence[Mapping[str, Any]] | None = None,
    prereg_id: str | None = None,
    prereg_compliant: bool | None = None,
    reproduction_ref: str | None = None,
    issued_by_launch: str,
    ts: str | None = None,
) -> dict[str, Any]:
    """Insert one ``verdict`` row. ``evidence`` is a list of
    ``{anchor_id?, chunk_id?, stance?, note?}`` dicts (design's own DDL
    comment shape) — serialized to JSON here (the DDL's ``evidence`` column
    is ``TEXT NOT NULL``, so ``None``/omitted becomes ``"[]"``, never a
    NULL write). Returns the row as written, matching
    ``trialerror.stores.insert``'s own return contract.

    Refuses (before ever reaching ``trialerror.stores.insert``, so the caller
    gets a typed reason rather than a raw ``ValidationError`` string to
    parse):

    - :class:`~trialerror.verify.errors.InvalidSubjectKindError` — ``subject_kind``
      not in :data:`SUBJECT_KINDS`.
    - :class:`~trialerror.verify.errors.InvalidProcedureError` — ``procedure``
      not in :data:`PROCEDURES`.

    XID validation (``prereg_id`` -> ``ops.prereg``, ``issued_by_launch`` ->
    ``platform.launch``) and any NOT NULL/CHECK violation still surface as
    ``trialerror.stores.errors.{ValidationError,XidTargetMissingError}`` from
    the underlying ``trialerror.stores.insert`` call — this function does not
    catch or reshape those; callers that need a structured envelope (e.g.
    ``trialerror/mcp/ops.py``) catch them at their own layer, exactly as every
    other landed write API in this codebase does.
    """
    if subject_kind not in SUBJECT_KINDS:
        raise InvalidSubjectKindError(
            f"record_verdict: subject_kind must be one of {sorted(SUBJECT_KINDS)!r}, got {subject_kind!r}"
        )
    if procedure not in PROCEDURES:
        raise InvalidProcedureError(
            f"record_verdict: procedure must be one of {sorted(PROCEDURES)!r}, got {procedure!r}"
        )

    row = {
        "verdict_id": new_id("VRD"),
        "subject_kind": subject_kind,
        "subject_id": subject_id,
        "procedure": procedure,
        "procedure_version": procedure_version,
        "label": label,
        "evidence": json.dumps(list(evidence) if evidence else [], ensure_ascii=False),
        "prereg_id": prereg_id,
        "prereg_compliant": (1 if prereg_compliant else 0) if prereg_compliant is not None else None,
        "reproduction_ref": reproduction_ref,
        "ts": ts or now(),
        "issued_by_launch": issued_by_launch,
    }
    return store_insert(store, "verdict", row)
