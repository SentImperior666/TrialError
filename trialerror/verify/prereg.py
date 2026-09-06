"""Blind pre-registration. Design Section 4.2 (``prereg`` DDL) + Section
1.6 + Section 8.2 step 1: "(Optional but default for keystones) `prereg
commit` the procedure + parameters blind." Design Section 12 (M9 row):
"prereg commit/reveal CLI (platform-tree escrow)."

**The blind is physical, not conventional (design Section 4.2 verbatim):**
:func:`commit_prereg` writes the raw ``{title, procedure, params}`` content
to ``escrow_path`` under ``store.platform_root / "escrow" / <program_id>``
— the PLATFORM tree, a directory this program's own repo/scaffold has no
path into — and stores only the two sha256 hashes (never the raw content)
in ``ops.prereg``. :func:`reveal_prereg` is the only function that ever
reads the escrowed content back out and copies it INTO the program tree,
and only after recomputing both hashes and confirming they still match what
was committed (design: "reveal w/ tampered escrow refused").

**Compliance, stated precisely (design Section 4.2 last line):** "verdict
tooling stamps `prereg_compliant` by recomputing the sha of the procedure
it actually executed. Any mismatch -> verdict is recorded with
`prereg_compliant=false`." :func:`check_prereg_compliance` is that
recomputation — it takes whatever procedure text/params the caller ACTUALLY
ran (not the escrowed content, which may not even be revealed yet) and
compares its hash against the committed one. ``trialerror.verify.hypothesis``
calls this when a hypothesis carries a ``prereg_id``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from trialerror.stores import get as store_get
from trialerror.stores import insert as store_insert
from trialerror.stores import update as store_update
from trialerror.stores.errors import ValidationError
from trialerror.stores.store import Store
from trialerror.util.atomic import atomic_write_text
from trialerror.util.config import ConfigError, load_config
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now
from trialerror.verify.errors import (
    InvalidProcedureError,
    PreregAlreadyRevealedError,
    PreregNotFoundError,
    PreregTamperedError,
    PreregVoidedError,
)

__all__ = [
    "sha256_hex",
    "canonical_json",
    "commit_prereg",
    "reveal_prereg",
    "prereg_status",
    "check_prereg_compliance",
]


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    """Deterministic JSON encoding used for every ``params`` hash in this
    module — sorted keys, stable separators, so the same logical params
    dict always hashes identically regardless of insertion order (the same
    encoding ``trialerror/mcp/ops.py``'s original thin adapter used, preserved
    here as the one canonical form)."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _resolve_program_id(store: Store) -> str:
    """``trialerror.toml``'s ``[program] id`` if configured, else the program
    root's own directory name — same "read generically, tolerate absence"
    fallback every other module's own small config helper uses (e.g.
    ``trialerror/mcp/ops.py::_resolve_program_id``, duplicated here per that
    module's own stated convention: cross-lane private helpers are
    re-derived, not imported)."""
    try:
        config = load_config(store.program_root / "trialerror.toml")
        return config.program_id
    except ConfigError:
        return store.program_root.name or "default"


def _require_prereg(store: Store, prereg_id: str) -> dict[str, Any]:
    row = store_get(store, "prereg", pk_column="prereg_id", pk_value=prereg_id)
    if row is None:
        raise PreregNotFoundError(f"no such prereg: {prereg_id!r}")
    return row


def commit_prereg(
    store: Store,
    *,
    title: str,
    procedure: str,
    params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Hash-commit ``{title, procedure, params}`` blind, escrowing the raw
    content under the platform tree. Returns the ``prereg`` row as written
    (``status='committed'``, ``revealed_ts=None``).

    Refuses (:class:`~trialerror.verify.errors.InvalidProcedureError`) an empty
    or whitespace-only ``procedure`` — a blind commitment to nothing is not
    a commitment.
    """
    if not isinstance(procedure, str) or not procedure.strip():
        raise InvalidProcedureError("commit_prereg: 'procedure' must be a non-empty string")

    params = dict(params) if params is not None else {}
    ts = now()
    prereg_id = new_id("PREG")
    procedure_sha256 = sha256_hex(procedure)
    params_sha256 = sha256_hex(canonical_json(params))

    program_id = _resolve_program_id(store)
    escrow_dir = store.platform_root / "escrow" / program_id
    escrow_path = escrow_dir / f"{prereg_id}.json"
    atomic_write_text(
        escrow_path,
        json.dumps(
            {"prereg_id": prereg_id, "title": title, "procedure": procedure, "params": params, "committed_ts": ts},
            ensure_ascii=False,
            indent=2,
        ),
    )

    row = {
        "prereg_id": prereg_id,
        "title": title,
        "procedure_sha256": procedure_sha256,
        "params_sha256": params_sha256,
        "committed_ts": ts,
        "escrow_path": str(escrow_path),
        "revealed_ts": None,
        "status": "committed",
    }
    return store_insert(store, "prereg", row)


def prereg_status(store: Store, *, prereg_id: str) -> dict[str, Any]:
    """Fetch one ``prereg`` row. Raises
    :class:`~trialerror.verify.errors.PreregNotFoundError` if it does not
    exist."""
    return _require_prereg(store, prereg_id)


def _resolve_event_session(store: Store, session_id: str | None) -> str | None:
    """Which session the ``prereg_revealed`` event is attributed to.

    An explicit id must name a REAL ``ops.session`` row -- ``event.session_id``
    is a same-file FK, so an invented one would fail the insert, and refusing
    up front is the difference between "your session id is wrong" and "the
    blind is broken and the audit row is missing". Any status is accepted, not
    just ``open``: a reveal during a session that has just closed is a real
    thing to do, and this is attribution, not authorship.

    With no id given, the newest open session -- and ``None`` when nothing is
    open, because a CLI reveal outside a session is legitimate and must not be
    refused over an attribution field. The event still records the reveal."""
    conn = store.ops
    if session_id is not None:
        found = conn.execute("SELECT session_id FROM session WHERE session_id = ?", (session_id,)).fetchone()
        if found is None:
            raise ValidationError(
                f"session_id={session_id!r} has no row in ops.session "
                "(a prereg reveal is attributed to a real session, never an arbitrary string)"
            )
        return session_id
    row = conn.execute(
        "SELECT session_id FROM session WHERE status = 'open' ORDER BY opened_ts DESC LIMIT 1"
    ).fetchone()
    return row[0] if row is not None else None


def reveal_prereg(
    store: Store,
    *,
    prereg_id: str,
    dest_dir: str | Path | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Read the escrowed content back, verify it still hashes to what was
    committed, copy it INTO the program tree, and mark the ``prereg`` row
    ``revealed`` (design: "reveal copies the content into the program tree
    only after sha comparison").

    Refuses:

    - :class:`~trialerror.verify.errors.PreregVoidedError` if the row is already
      ``voided`` (a prior tamper detection, or an explicit void).
    - :class:`~trialerror.verify.errors.PreregAlreadyRevealedError` if the row
      is already ``revealed``. A reveal is the one irreversible act here, so
      it happens exactly once: a second call used to re-copy the escrow,
      overwrite ``revealed_ts`` with the later time and append a second
      ``prereg_revealed`` event, losing the moment the blind was actually
      broken. Spec section 4 named three refusals and not this one; the
      Determinations queue drops the item after the first reveal, so only a
      direct CLI or HTTP call could reach it (lane C, finding F2).
    - :class:`~trialerror.verify.errors.PreregTamperedError` if the escrow
      file's content no longer hashes to ``procedure_sha256``/
      ``params_sha256`` — design: "reveal w/ tampered escrow refused". The
      row is marked ``voided`` as a side effect of this refusal (a tamper
      is a permanent finding, not a retryable error).

    **Emits a ``prereg_revealed`` event** (lane C, ruling L-C3). A reveal ends
    the blind and cannot be undone, so who ended it and when has to be
    discoverable in the event log, not only in a ``revealed_ts`` column
    somebody would have to know to go and look at. The event is written HERE
    rather than by any one caller, so the CLI verb and the dashboard button
    produce the identical record; ``session_id`` is the only thing the two
    differ on, and it carries the dashboard's own session when the reveal
    comes from a browser.

    The payload carries the two COMMITTED HASHES, never the revealed content:
    an event log is not the place to un-blind a procedure a second time, and
    the hashes are what ties this row to a later verdict's
    ``prereg_compliant`` stamp.

    A tampered escrow writes NO event -- nothing was revealed. The voiding is
    recorded by the row's own ``status``, and the refusal reaches the caller.

    ``dest_dir`` is a server-side choice everywhere it matters:
    ``trialerror.dashboard.writes`` never takes one from a request body, since
    an HTTP caller naming a write path is a path-traversal primitive rather
    than a feature.
    """
    row = _require_prereg(store, prereg_id)
    if row["status"] == "voided":
        raise PreregVoidedError(f"prereg {prereg_id!r} is already voided; cannot reveal")
    if row["status"] == "revealed":
        # The row carries no revealed_path (only the ts), so this message
        # names the ts and points at the event rather than guessing a
        # directory -- the first reveal may have chosen its own dest_dir.
        raise PreregAlreadyRevealedError(
            f"prereg {prereg_id!r} was already revealed at {row['revealed_ts']!r}; "
            f"revealing again would overwrite that timestamp and log a second "
            f"prereg_revealed event -- the revealed_path is on the first one"
        )
    # Resolved BEFORE anything is written: `event.session_id` is a same-file FK
    # to `session`, so a bogus id would fail the insert -- after the blind had
    # already been broken and the row marked revealed. An attribution problem
    # must refuse before the irreversible act, not after it.
    event_session_id = _resolve_event_session(store, session_id)

    escrow_path = Path(row["escrow_path"])
    try:
        content = json.loads(escrow_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        store_update(store, "prereg", pk_column="prereg_id", pk_value=prereg_id, changes={"status": "voided"})
        raise PreregTamperedError(f"prereg {prereg_id!r}: escrow file unreadable ({exc}); voided") from exc

    procedure = content.get("procedure", "")
    params = content.get("params", {})
    recomputed_procedure_sha = sha256_hex(procedure)
    recomputed_params_sha = sha256_hex(canonical_json(params))
    if recomputed_procedure_sha != row["procedure_sha256"] or recomputed_params_sha != row["params_sha256"]:
        store_update(store, "prereg", pk_column="prereg_id", pk_value=prereg_id, changes={"status": "voided"})
        raise PreregTamperedError(
            f"prereg {prereg_id!r}: escrowed content no longer matches its committed hash; voided"
        )

    dest_dir = Path(dest_dir) if dest_dir is not None else store.program_root / "prereg" / "revealed"
    dest_path = dest_dir / f"{prereg_id}.json"
    atomic_write_text(dest_path, json.dumps(content, ensure_ascii=False, indent=2))

    ts = now()
    store_update(store, "prereg", pk_column="prereg_id", pk_value=prereg_id, changes={"revealed_ts": ts, "status": "revealed"})

    # Imported at call time, not at module import: trialerror.events.api
    # imports trialerror.stores, and so does this module -- a top-level import
    # here is fine today and is one refactor away from a cycle. The row is
    # written AFTER the status update, so an event never claims a reveal that
    # did not land (both are on ops.db, but they are two auto-commits).
    from trialerror.events.api import append_event

    append_event(
        store,
        event_type="prereg_revealed",
        payload={
            "prereg_id": prereg_id,
            "revealed_path": str(dest_path),
            "procedure_sha256": row["procedure_sha256"],
            "params_sha256": row["params_sha256"],
        },
        session_id=event_session_id,
        ts=ts,
    )
    return {
        "prereg_id": prereg_id,
        "title": content.get("title"),
        "procedure": procedure,
        "params": params,
        "revealed_ts": ts,
        "revealed_path": str(dest_path),
        "status": "revealed",
    }


def check_prereg_compliance(
    store: Store,
    *,
    prereg_id: str,
    executed_procedure: str,
    executed_params: Mapping[str, Any] | None = None,
) -> bool:
    """Recompute the sha256 of the procedure/params ACTUALLY executed and
    compare against what ``prereg_id`` committed to. ``True`` iff both
    hashes match exactly. Never mutates the prereg row (unlike
    :func:`reveal_prereg`) — this is a pure compliance check, callable any
    number of times, before or after a reveal, and is what
    ``trialerror.verify.hypothesis``/``trialerror.verify.citecheck`` call to stamp a
    verdict's ``prereg_compliant`` field.

    Raises :class:`~trialerror.verify.errors.PreregNotFoundError` /
    :class:`~trialerror.verify.errors.PreregVoidedError` exactly like
    :func:`reveal_prereg` — a voided commitment cannot be complied with."""
    row = _require_prereg(store, prereg_id)
    if row["status"] == "voided":
        raise PreregVoidedError(f"prereg {prereg_id!r} is voided; compliance is undefined")
    executed_params = dict(executed_params) if executed_params is not None else {}
    procedure_sha = sha256_hex(executed_procedure)
    params_sha = sha256_hex(canonical_json(executed_params))
    return procedure_sha == row["procedure_sha256"] and params_sha == row["params_sha256"]
