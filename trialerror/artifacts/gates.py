"""The gate state machine's mutation service. Design Section 12 (M10 row):
"gate state machine + transitions + edit-union verification"; Section 4.2:
"``trialerror gate advance`` is the ONLY mutation path and refuses illegal
transitions... the transition INTO ``union_applied`` is what enforces
verdict in {PASS, PASS_WITH_EDITS} with every blocking edit
``verified=true`` and ``reproduction_status != mismatch``".

Every state-changing operation in this module — :func:`advance_gate`,
:func:`submit_gate`, :func:`record_verdict`, :func:`apply_union` — funnels
through the single private :func:`_execute_transition`, which is what makes
"the ONLY mutation path" true in substance, not just at the CLI's naming
level: there is exactly one place a ``gate.state`` column is ever written
by this subsystem, exactly one place a ``gate_transition`` row is ever
inserted, and exactly one place the ``union_applied`` entry conditions are
checked. :func:`verify_edit` is deliberately NOT a state transition (it
mutates one entry of the ``edits`` JSON array in place, without touching
``gate.state``) — see its own docstring.

TRIALERROR-DEV-NOTE (reproduction acceptance, per the build brief / design F4
resolution): ``gate.reproduction_status`` is read here exactly as stored —
this module never runs a reproduction script itself (that machinery is
M9's ``trialerror verify reproduce``, per Design Section 8.3: "``gate advance ->
registered`` consults this"). Tests in this build plant
``reproduction_status`` rows directly (fixture rows), which is the
CONTRACT M9 inherits: whatever writes that column for real (M9's runner)
only has to call ``trialerror.stores.update(store, "gate", ...,
changes={"reproduction_status": "match"|"mismatch"|"unrun", ...})`` and
this module's enforcement applies unchanged.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Sequence

from trialerror.artifacts._txn import raw_insert, raw_update
from trialerror.artifacts.errors import GateEntryConditionError, IllegalTransitionError
from trialerror.artifacts.state_machine import assert_legal_transition
from trialerror.stores import get as store_get
from trialerror.stores.errors import ValidationError, XidTargetMissingError
from trialerror.stores.store import Store
from trialerror.stores.writer import update as store_update
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

__all__ = [
    "VERDICT_VALUES",
    "REPRODUCTION_STATUS_VALUES",
    "open_gate",
    "get_gate",
    "advance_gate",
    "submit_gate",
    "record_verdict",
    "apply_union",
    "verify_edit",
]

#: Matches ``gate.verdict``'s CHECK constraint (``trialerror/stores/schema/ops.py``).
VERDICT_VALUES = frozenset({"PASS", "PASS_WITH_EDITS", "FAIL"})

#: Matches ``gate.reproduction_status``'s CHECK constraint.
REPRODUCTION_STATUS_VALUES = frozenset({"match", "mismatch", "unrun"})

_GATE_ID_RE_PREFIX = "CR"


def _next_gate_id(conn: sqlite3.Connection) -> str:
    """``'CR-###'`` style (design Section 4.2 DDL comment: ``gate_id PK
    ('CR-###')``) — sequential, derived as max-existing-suffix + 1 exactly
    like ``trialerror.law.service._next_ruling_id`` derives ``'C-####'``, for the
    same reason (correct even if rows were seeded out of band, e.g. a origin-project
    migration import of real ``CR-090``..``CR-096``-style ids)."""
    pat = re.compile(rf"^{_GATE_ID_RE_PREFIX}-(\d+)$")
    rows = conn.execute("SELECT gate_id FROM gate").fetchall()
    max_n = 0
    for r in rows:
        m = pat.match(r["gate_id"])
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"{_GATE_ID_RE_PREFIX}-{max_n + 1:03d}"


def _require_launch_exists(store: Store, launch_id: str, *, field_name: str) -> None:
    """Manual XID check for a launch id written inside this module's raw
    (non-``trialerror.stores.writer``) transactions — see ``_txn.py`` module
    docstring for why the generic writer's automatic XID validation is
    bypassed on these paths, and why it must be reproduced by hand here."""
    row = store.platform.execute("SELECT 1 FROM launch WHERE launch_id = ? LIMIT 1", (launch_id,)).fetchone()
    if row is None:
        raise XidTargetMissingError(
            f"{field_name} = {launch_id!r} has no matching row in platform.launch (XID refused)"
        )


def get_gate(store: Store, gate_id: str) -> dict[str, Any] | None:
    """Fetch one gate row by id, or ``None``."""
    return store_get(store, "gate", pk_column="gate_id", pk_value=gate_id)


def _require_gate(store: Store, gate_id: str) -> dict[str, Any]:
    gate = get_gate(store, gate_id)
    if gate is None:
        raise ValueError(f"no such gate: {gate_id!r}")
    return gate


def _parse_edits(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    return json.loads(raw)


def _normalize_edits(edits: Sequence[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Fill in the full ``edits`` entry shape (design Section 4.2 DDL:
    ``edits JSON [ {edit_id, text, blocking BOOL, applied BOOL,
    applied_by_launch, verified BOOL, verified_note} ]``) for whatever
    subset a critic actually supplies — every entry gets a stable
    ``edit_id`` (generated if the caller didn't supply one) and the
    applied/verified fields start ``False``/``None`` until
    :func:`verify_edit` touches them."""
    normalized: list[dict[str, Any]] = []
    for e in edits or []:
        normalized.append(
            {
                "edit_id": e.get("edit_id") or new_id("EDIT"),
                "text": e["text"],
                "blocking": bool(e.get("blocking", False)),
                "applied": bool(e.get("applied", False)),
                "applied_by_launch": e.get("applied_by_launch"),
                "verified": bool(e.get("verified", False)),
                "verified_note": e.get("verified_note"),
            }
        )
    return normalized


def _check_union_entry(gate: dict[str, Any]) -> None:
    """The F10-resolution enforcement: everything the transition INTO
    ``union_applied`` must verify before it is allowed to land. Collects
    every violation (rather than failing on the first) so a caller sees the
    whole picture in one refusal — the same "combine every reason" style
    ``trialerror.law.service.verify_pin`` uses for its own multi-check refusal."""
    problems: list[str] = []

    verdict = gate.get("verdict")
    if verdict not in ("PASS", "PASS_WITH_EDITS"):
        problems.append(f"verdict must be PASS or PASS_WITH_EDITS to apply union, got {verdict!r}")

    edits = _parse_edits(gate.get("edits"))
    unverified_blocking = [e["edit_id"] for e in edits if e.get("blocking") and not e.get("verified")]
    if unverified_blocking:
        problems.append(f"blocking edit(s) not yet verified: {unverified_blocking}")

    reproduction_status = gate.get("reproduction_status")
    if reproduction_status == "mismatch":
        problems.append("reproduction_status is 'mismatch'")

    if problems:
        raise GateEntryConditionError(
            f"gate {gate['gate_id']!r}: cannot enter union_applied — " + "; ".join(problems)
        )


def _execute_transition(
    conn: sqlite3.Connection,
    *,
    gate: dict[str, Any],
    to_state: str,
    by_launch: str,
    evidence: Any,
    ts: str,
    extra_gate_changes: dict[str, Any] | None = None,
) -> None:
    """THE single place ``gate.state`` is ever written and a
    ``gate_transition`` row is ever inserted. Called only from inside an
    already-open ``BEGIN IMMEDIATE`` transaction on ``conn`` (``store.ops``)
    — see each public function below for the transaction boundary."""
    assert_legal_transition(gate["state"], to_state)
    if to_state == "union_applied":
        merged = dict(gate)
        merged.update(extra_gate_changes or {})
        _check_union_entry(merged)

    changes = dict(extra_gate_changes or {})
    changes["state"] = to_state
    raw_update(conn, "gate", pk_column="gate_id", pk_value=gate["gate_id"], changes=changes)
    raw_insert(
        conn,
        "gate_transition",
        {
            "gate_id": gate["gate_id"],
            "from_state": gate["state"],
            "to_state": to_state,
            "ts": ts,
            "by_launch": by_launch,
            "evidence": json.dumps(evidence, ensure_ascii=False) if evidence is not None else None,
        },
    )


def open_gate(store: Store, *, artifact_id: str) -> dict[str, Any]:
    """Open a new gate for ``artifact_id`` at state ``draft`` and link it
    back (``artifact.gate_id``), moving the artifact to ``in_gate`` status.

    TRIALERROR-DEV-NOTE: takes no ``by_launch``/``ts`` — the design's ``gate``
    DDL has no column to record who opened a gate or when (unlike every
    state TRANSITION, which logs ``gate_transition.by_launch``/``ts``);
    this function does not invent columns the schema doesn't have. No
    ``gate_transition`` row is written either — there is no ``from_state``
    for a gate's very first row (the M1 fixture ``tests/_store_fixtures.py``
    follows the same convention: the gate row lands at ``draft`` with zero
    prior ``gate_transition`` history).

    Refuses (:class:`ValueError`) if the artifact does not exist, is
    already ``registered``/``superseded``, or already has an open
    (non-terminal, non-``failed``) gate — re-opening after a ``failed``
    gate is allowed (a fresh review attempt)."""
    artifact = store_get(store, "artifact", pk_column="artifact_id", pk_value=artifact_id)
    if artifact is None:
        raise ValueError(f"no such artifact: {artifact_id!r}")
    if artifact["status"] in ("registered", "superseded"):
        raise ValueError(f"artifact {artifact_id!r} is already {artifact['status']!r}; cannot open a gate")
    if artifact["status"] == "in_gate":
        current = get_gate(store, artifact["gate_id"]) if artifact["gate_id"] else None
        current_state = current["state"] if current is not None else "?"
        if current is None or current_state != "failed":
            raise ValueError(
                f"artifact {artifact_id!r} already has an open gate "
                f"({artifact.get('gate_id')!r}, state={current_state!r}); "
                "close or abandon it (advance it to 'failed') before opening a new one"
            )

    conn = store.ops
    conn.execute("BEGIN IMMEDIATE")
    try:
        gate_id = _next_gate_id(conn)
        gate_row: dict[str, Any] = {
            "gate_id": gate_id,
            "artifact_id": artifact_id,
            "state": "draft",
            "verdict": None,
            "critic_launch": None,
            "verdict_ts": None,
            "edits": None,
            "reproduction_ref": None,
            "reproduction_status": None,
        }
        raw_insert(conn, "gate", gate_row)
        raw_update(
            conn, "artifact", pk_column="artifact_id", pk_value=artifact_id,
            changes={"status": "in_gate", "gate_id": gate_id},
        )
        conn.execute("COMMIT")
    except sqlite3.IntegrityError as exc:
        conn.execute("ROLLBACK")
        raise ValidationError(f"open_gate: integrity violation: {exc}") from exc
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return gate_row


def advance_gate(
    store: Store,
    *,
    gate_id: str,
    to_state: str,
    by_launch: str,
    evidence: Any = None,
    ts: str | None = None,
) -> dict[str, Any]:
    """The generic, low-level entry point (``trialerror gate advance``): move
    ``gate_id`` to ``to_state`` if the edge is legal (and, for
    ``union_applied``, if its entry conditions are met). Every named
    convenience below (:func:`submit_gate`, :func:`apply_union`) is a thin
    argument-shape wrapper over this same function; :func:`record_verdict`
    shares its transition-execution core (see module docstring)."""
    if not by_launch:
        raise ValueError("advance_gate: by_launch is required (gate_transition.by_launch is NOT NULL)")
    _require_launch_exists(store, by_launch, field_name="by_launch")
    gate = _require_gate(store, gate_id)
    ts = ts or now()

    conn = store.ops
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Re-fetch under the write lock so the legality/entry check sees a
        # consistent snapshot even if another writer landed a transition
        # between the pre-transaction fetch above and this point.
        fresh = conn.execute("SELECT * FROM gate WHERE gate_id = ?", (gate_id,)).fetchone()
        if fresh is None:
            raise ValueError(f"no such gate: {gate_id!r}")
        gate = dict(fresh)
        _execute_transition(conn, gate=gate, to_state=to_state, by_launch=by_launch, evidence=evidence, ts=ts)
        conn.execute("COMMIT")
    except (IllegalTransitionError, GateEntryConditionError, ValueError):
        conn.execute("ROLLBACK")
        raise
    except sqlite3.IntegrityError as exc:
        conn.execute("ROLLBACK")
        raise ValidationError(f"advance_gate: integrity violation: {exc}") from exc
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _require_gate(store, gate_id)


def submit_gate(store: Store, *, gate_id: str, by_launch: str, evidence: Any = None, ts: str | None = None) -> dict[str, Any]:
    """``trialerror gate submit``: ``draft -> submitted`` — where the two-tier
    structural-validator-then-critic flow (design Section 5.3) begins."""
    return advance_gate(store, gate_id=gate_id, to_state="submitted", by_launch=by_launch, evidence=evidence, ts=ts)


def record_verdict(
    store: Store,
    *,
    gate_id: str,
    verdict: str,
    critic_launch: str | None = None,
    by_launch: str | None = None,
    edits: Sequence[dict[str, Any]] | None = None,
    reproduction_ref: str | None = None,
    reproduction_status: str | None = None,
    evidence: Any = None,
    ts: str | None = None,
) -> dict[str, Any]:
    """``trialerror gate verdict``: records the critic's verdict/edits/
    reproduction fields AND advances the gate's state — in ONE transaction,
    the same "one API, two effects land together or not at all" shape
    ``trialerror.law.service.append_ruling`` uses for ruling+digest.

    Requires the gate to currently be ``submitted`` (:class:`ValueError`
    otherwise) — a verdict is only meaningful once a review was actually
    submitted; this is a business precondition ``record_verdict`` itself
    enforces, narrower than (but consistent with) the raw state graph,
    which also allows ``draft -> failed`` as a separate ABANDON action via
    plain :func:`advance_gate` (no verdict fields touched).

    Destination state: ``gated`` for ``PASS``/``PASS_WITH_EDITS``,
    ``failed`` for ``FAIL`` — a FAIL verdict is itself a real verdict
    (recorded on the gate) that lands the gate in its fail-terminal state
    in the same step, rather than requiring a second call.
    """
    if verdict not in VERDICT_VALUES:
        raise ValueError(f"record_verdict: verdict must be one of {sorted(VERDICT_VALUES)}, got {verdict!r}")
    if reproduction_status is not None and reproduction_status not in REPRODUCTION_STATUS_VALUES:
        raise ValueError(
            f"record_verdict: reproduction_status must be one of {sorted(REPRODUCTION_STATUS_VALUES)} "
            f"or None, got {reproduction_status!r}"
        )
    by_launch = by_launch or critic_launch
    if not by_launch:
        raise ValueError("record_verdict: by_launch (or critic_launch) is required")
    if critic_launch:
        _require_launch_exists(store, critic_launch, field_name="critic_launch")
    _require_launch_exists(store, by_launch, field_name="by_launch")

    gate = _require_gate(store, gate_id)
    if gate["state"] != "submitted":
        raise ValueError(
            f"record_verdict: gate {gate_id!r} must be 'submitted' to record a verdict, is {gate['state']!r}"
        )

    ts = ts or now()
    normalized_edits = _normalize_edits(edits)
    to_state = "gated" if verdict in ("PASS", "PASS_WITH_EDITS") else "failed"
    extra_changes = {
        "verdict": verdict,
        "critic_launch": critic_launch,
        "verdict_ts": ts,
        "edits": json.dumps(normalized_edits, ensure_ascii=False) if normalized_edits else None,
        "reproduction_ref": reproduction_ref,
        "reproduction_status": reproduction_status,
    }

    conn = store.ops
    conn.execute("BEGIN IMMEDIATE")
    try:
        fresh = conn.execute("SELECT * FROM gate WHERE gate_id = ?", (gate_id,)).fetchone()
        if fresh is None:
            raise ValueError(f"no such gate: {gate_id!r}")
        fresh_gate = dict(fresh)
        if fresh_gate["state"] != "submitted":
            raise ValueError(
                f"record_verdict: gate {gate_id!r} must be 'submitted' to record a verdict, "
                f"is {fresh_gate['state']!r}"
            )
        _execute_transition(
            conn, gate=fresh_gate, to_state=to_state, by_launch=by_launch, evidence=evidence, ts=ts,
            extra_gate_changes=extra_changes,
        )
        conn.execute("COMMIT")
    except (IllegalTransitionError, GateEntryConditionError, ValueError):
        conn.execute("ROLLBACK")
        raise
    except sqlite3.IntegrityError as exc:
        conn.execute("ROLLBACK")
        raise ValidationError(f"record_verdict: integrity violation: {exc}") from exc
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _require_gate(store, gate_id)


def apply_union(store: Store, *, gate_id: str, by_launch: str, evidence: Any = None, ts: str | None = None) -> dict[str, Any]:
    """``trialerror gate apply-union``: ``gated -> union_applied``, the F10
    terminal-pass transition. Refuses (:class:`~trialerror.artifacts.errors.
    GateEntryConditionError`) unless verdict is a pass value, every
    blocking edit is verified, and reproduction did not mismatch — see
    :func:`_check_union_entry`. "A PASS with zero edits still passes
    through ``union_applied`` as a no-op transition" (design Section 4.2):
    no edits means the ``unverified_blocking`` check is vacuously empty."""
    return advance_gate(store, gate_id=gate_id, to_state="union_applied", by_launch=by_launch, evidence=evidence, ts=ts)


def verify_edit(
    store: Store,
    *,
    gate_id: str,
    edit_id: str,
    by_launch: str,
    verified_note: str | None = None,
    ts: str | None = None,
) -> dict[str, Any]:
    """``trialerror gate verify-edit``: the applier-verifies layer (design
    Section 4.2 comment: "BLOCKING edits need verified=true"). NOT a state
    transition — mutates one entry of the ``edits`` JSON array in place,
    marking it ``applied=true, verified=true`` (attributed to
    ``by_launch``) and writes NO ``gate_transition`` row (nothing about
    ``gate.state`` changed).

    TRIALERROR-DEV-NOTE: the ``trialerror gate`` CLI surface names exactly six verbs
    (design Section 5.2) with no separate "mark this edit applied" verb —
    the applier calling ``verify-edit`` after making the file change is
    the single write path for both ``applied`` and ``verified``, which is
    also the honest reading of "applier-VERIFIES" as one combined act, not
    two.

    Requires the gate to be in ``gated`` state (post-verdict, pre-union) —
    editing after ``union_applied`` would silently invalidate a check that
    already passed."""
    if not by_launch:
        raise ValueError("verify_edit: by_launch is required")
    _require_launch_exists(store, by_launch, field_name="by_launch")
    gate = _require_gate(store, gate_id)
    if gate["state"] != "gated":
        raise ValueError(f"verify_edit: gate {gate_id!r} must be 'gated' to verify an edit, is {gate['state']!r}")

    edits = _parse_edits(gate.get("edits"))
    match = next((e for e in edits if e["edit_id"] == edit_id), None)
    if match is None:
        raise ValueError(f"verify_edit: no edit {edit_id!r} on gate {gate_id!r}")
    match["applied"] = True
    match["applied_by_launch"] = by_launch
    match["verified"] = True
    match["verified_note"] = verified_note

    store_update(
        store, "gate", pk_column="gate_id", pk_value=gate_id,
        changes={"edits": json.dumps(edits, ensure_ascii=False)},
    )
    return _require_gate(store, gate_id)
