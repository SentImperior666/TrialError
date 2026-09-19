"""The typed-artifact registry. Design Section 12 (M10 row): "registry +
template table (gated flag)"; Section 4.2: "``trialerror artifact register``
consults the ``template`` registry — for types with ``gated=1`` it
requires the artifact's gate in ``union_applied``, and on success advances
that gate ``union_applied -> registered`` in the same transaction.
Registration closes a gate; never the reverse."

TRIALERROR-DEV-NOTE (``artifact create`` vs. the design's abridged CLI table):
Design Section 5.2's ``artifact`` row lists "register, list, show" and is
explicitly headed "Commands (abridged)" — not exhaustive. Some function
must create the initial ``artifact`` row (status ``draft``) BEFORE a gate
can be opened against it at all (``gate.artifact_id`` is ``NOT NULL``, and
``tests/_store_fixtures.py``'s own dependency order inserts ``artifact``
before ``gate``) — the design's own DDL prose reads "``trialerror artifact
register`` consults the template registry ... for types with ``gated=1``"
as an operation on an artifact that ALREADY EXISTS, not one that also
creates it. :func:`create_artifact` is that one missing step, faithfully
filling the abridged table's gap rather than overloading ``register`` to
mean two different things for gated vs. ungated types.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from trialerror.artifacts._txn import raw_insert, raw_update
from trialerror.artifacts.errors import RegistrationRefusedError
from trialerror.stores import get as store_get
from trialerror.stores.errors import ValidationError, XidTargetMissingError
from trialerror.stores.store import Store
from trialerror.stores.writer import insert as store_insert
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

__all__ = ["create_artifact", "get_artifact", "list_artifacts", "get_template", "register_artifact"]


def create_artifact(
    store: Store,
    *,
    type_key: str,
    title: str,
    path: str,
    sha256: str,
    by_launch: str,
    purpose: str | None = None,
    domains: list[str] | None = None,
    attrs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a new artifact row at ``status='draft'``, unregistered and
    ungated. ``by_launch`` lands in ``registered_by_launch`` — a ``NOT
    NULL`` column in the M1-built schema despite the name (see module
    docstring): it is the authoring launch at creation time, confirmed (or
    overwritten by a DIFFERENT registering launch) when
    :func:`register_artifact` later finalizes the row.

    TRIALERROR-DEV-NOTE: no ``ts`` parameter — the ``artifact`` DDL has no
    "created_ts" column (only ``registered_ts``, which stays ``NULL`` until
    :func:`register_artifact` stamps it), so there is nothing to accept a
    caller-supplied timestamp for.

    Uses ``trialerror.stores.writer.insert`` directly (single row, single
    table) — its automatic XID validation covers ``registered_by_launch``,
    and its unknown-table/unknown-column/CHECK-violation refusals cover
    an unknown ``type_key`` (the same-file FK to ``template.type_key``
    raises a clean :class:`~trialerror.stores.errors.ValidationError`, not a
    raw ``sqlite3.IntegrityError``)."""
    row = {
        "artifact_id": new_id("ART"),
        "type": type_key,
        "title": title,
        "path": path,
        "sha256": sha256,
        "status": "draft",
        "purpose": purpose,
        "domains": json.dumps(list(domains), ensure_ascii=False) if domains is not None else None,
        "attrs": json.dumps(attrs, ensure_ascii=False) if attrs is not None else None,
        "gate_id": None,
        "registered_ts": None,
        "registered_by_launch": by_launch,
        "supersedes": None,
    }
    return store_insert(store, "artifact", row)


def get_artifact(store: Store, artifact_id: str) -> dict[str, Any] | None:
    return store_get(store, "artifact", pk_column="artifact_id", pk_value=artifact_id)


def get_template(store: Store, type_key: str) -> dict[str, Any] | None:
    return store_get(store, "template", pk_column="type_key", pk_value=type_key)


def list_artifacts(
    store: Store, *, type_key: str | None = None, status: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """``trialerror artifact list`` — filtered read, newest-first by
    ``rowid`` (true insertion order; ``artifact_id`` has no natural sort
    tied to a legacy convention the way ``ruling_id``/``gate_id`` do)."""
    clauses: list[str] = []
    params: list[Any] = []
    if type_key is not None:
        clauses.append("type = ?")
        params.append(type_key)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = store.ops.execute(
        f"SELECT *, rowid AS _rowid FROM artifact {where} ORDER BY _rowid DESC LIMIT ?", params + [limit]
    ).fetchall()
    return [{k: v for k, v in dict(r).items() if k != "_rowid"} for r in rows]


def _require_launch_exists(store: Store, launch_id: str, *, field_name: str) -> None:
    row = store.platform.execute("SELECT 1 FROM launch WHERE launch_id = ? LIMIT 1", (launch_id,)).fetchone()
    if row is None:
        raise XidTargetMissingError(
            f"{field_name} = {launch_id!r} has no matching row in platform.launch (XID refused)"
        )


def register_artifact(
    store: Store,
    *,
    artifact_id: str,
    by_launch: str,
    supersedes: str | None = None,
    ts: str | None = None,
) -> dict[str, Any]:
    """``trialerror artifact register`` — THE registration entry point (design
    Section 4.2). Consults ``template.gated`` for the artifact's type:

    - ``gated=1``: refuses (:class:`~trialerror.artifacts.errors.
      RegistrationRefusedError`) unless the artifact has a gate AND that
      gate's ``state == 'union_applied'`` — "register-before-gate refused
      for gated template types". On success, in ONE transaction: the gate
      advances ``union_applied -> registered`` (with its own
      ``gate_transition`` row) AND the artifact flips to ``status =
      'registered'``. Registration closes a gate; never the reverse — this
      function contains the ONLY code path that ever writes
      ``gate.state = 'registered'``.
    - ``gated=0``: registers the artifact directly; no gate is required or
      touched, even if one happens to exist (an ungated type may still go
      through a voluntary review — ``open_gate`` never checks
      ``template.gated`` either, see its own docstring).

    ``supersedes``, if given, must name an existing ``registered``
    artifact — flipped to ``superseded`` in the SAME transaction (mirrors
    ``trialerror.law.service.append_ruling``'s ``supersedes`` handling).
    Refuses (:class:`ValueError`) if the artifact is already
    ``registered``/``superseded`` (idempotency: registration happens once).
    """
    if not by_launch:
        raise ValueError("register_artifact: by_launch is required")
    _require_launch_exists(store, by_launch, field_name="by_launch")

    artifact = get_artifact(store, artifact_id)
    if artifact is None:
        raise ValueError(f"no such artifact: {artifact_id!r}")
    if artifact["status"] in ("registered", "superseded"):
        raise ValueError(f"artifact {artifact_id!r} is already {artifact['status']!r}")

    template = get_template(store, artifact["type"])
    if template is None:
        raise ValueError(f"artifact {artifact_id!r} names unknown template type {artifact['type']!r}")
    gated = bool(template["gated"])

    gate = None
    if gated:
        gate_id = artifact.get("gate_id")
        gate = store_get(store, "gate", pk_column="gate_id", pk_value=gate_id) if gate_id else None
        if gate is None or gate["state"] != "union_applied":
            raise RegistrationRefusedError(
                f"artifact {artifact_id!r}: type {artifact['type']!r} is gated and requires its gate "
                f"in 'union_applied' before registration — "
                + (f"gate {gate_id!r} is at {gate['state']!r}" if gate is not None else "no gate has been opened")
            )

    ts = ts or now()
    conn = store.ops
    conn.execute("BEGIN IMMEDIATE")
    try:
        if gated:
            # OB-2 (C-0064 fix-tier3): the pre-transaction check above ran
            # BEFORE this write lock was taken, so it can only prove
            # "union_applied a moment ago" -- not "union_applied right
            # now". Re-fetch under the lock and re-verify, same pattern
            # trialerror.artifacts.gates.advance_gate itself uses at its own
            # "fresh = conn.execute(...)" re-fetch (gates.py:289-295): a
            # concurrent register_artifact racing the SAME gate through
            # this same BEGIN IMMEDIATE would otherwise write a second,
            # illegitimate union_applied -> registered gate_transition row
            # (or flip an already-superseded artifact back to registered)
            # once the first writer's commit has already moved the gate
            # past union_applied.
            fresh_gate = conn.execute("SELECT * FROM gate WHERE gate_id = ?", (gate["gate_id"],)).fetchone()
            if fresh_gate is None or fresh_gate["state"] != "union_applied":
                raise RegistrationRefusedError(
                    f"artifact {artifact_id!r}: type {artifact['type']!r} is gated and requires its gate "
                    f"in 'union_applied' before registration — "
                    + (
                        f"gate {gate['gate_id']!r} is at {fresh_gate['state']!r}"
                        if fresh_gate is not None
                        else f"gate {gate['gate_id']!r} no longer exists"
                    )
                )
            gate = dict(fresh_gate)

        if supersedes:
            prior = conn.execute(
                "SELECT status FROM artifact WHERE artifact_id = ?", (supersedes,)
            ).fetchone()
            if prior is None or prior["status"] != "registered":
                raise ValidationError(
                    f"register_artifact: supersedes={supersedes!r} does not name an existing "
                    "'registered' artifact"
                )
            raw_update(conn, "artifact", pk_column="artifact_id", pk_value=supersedes, changes={"status": "superseded"})

        if gated:
            raw_update(conn, "gate", pk_column="gate_id", pk_value=gate["gate_id"], changes={"state": "registered"})
            raw_insert(
                conn,
                "gate_transition",
                {
                    "gate_id": gate["gate_id"],
                    "from_state": "union_applied",
                    "to_state": "registered",
                    "ts": ts,
                    "by_launch": by_launch,
                    "evidence": None,
                },
            )

        raw_update(
            conn, "artifact", pk_column="artifact_id", pk_value=artifact_id,
            changes={
                "status": "registered",
                "registered_ts": ts,
                "registered_by_launch": by_launch,
                "supersedes": supersedes,
            },
        )
        conn.execute("COMMIT")
    except (ValidationError, RegistrationRefusedError, ValueError):
        conn.execute("ROLLBACK")
        raise
    except sqlite3.IntegrityError as exc:
        conn.execute("ROLLBACK")
        raise ValidationError(f"register_artifact: integrity violation: {exc}") from exc
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_artifact(store, artifact_id)
