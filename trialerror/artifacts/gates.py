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
import hashlib
import sqlite3
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from trialerror.artifacts._txn import raw_insert, raw_update
from trialerror.artifacts.errors import (
    GateEntryConditionError,
    IllegalTransitionError,
    OperatorFailRefusedError,
    RegistrationRefusedError,
)
from trialerror.artifacts.state_machine import assert_legal_transition
from trialerror.events.api import append_event_in_txn
from trialerror.stores import get as store_get
from trialerror.stores.errors import ValidationError, XidTargetMissingError
from trialerror.stores.store import Store
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
    "register_with_deviation",
    "register_failed",
    "fail_on_reproduction",
    "OPERATOR_FAIL_PATH",
    "verify_edit",
    "send_back_edit",
]

#: Matches ``gate.verdict``'s CHECK constraint (``trialerror/stores/schema/ops.py``).
VERDICT_VALUES = frozenset({"PASS", "PASS_WITH_EDITS", "FAIL"})

#: Matches ``gate.reproduction_status``'s CHECK constraint.
REPRODUCTION_STATUS_VALUES = frozenset({"match", "mismatch", "unrun"})

_GATE_ID_RE_PREFIX = "CR"

#: ``gate_transition.evidence.path`` of the one transition
#: :func:`fail_on_reproduction` writes. :func:`register_failed` accepts a gate
#: without a critic ``FAIL`` only when its move into ``failed`` carries it.
OPERATOR_FAIL_PATH = "operator_failed_reproduction"

#: Evidence paths only this module's named functions write. :func:`advance_gate`
#: refuses evidence that claims one, so a generic transition cannot pass for
#: an operator decision (or a registration) it is not.
_RESERVED_EVIDENCE_PATHS = frozenset({OPERATOR_FAIL_PATH, "register_with_deviation", "register_failed"})


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
    :func:`verify_edit` touches them.

    Keys this function does not name are CARRIED THROUGH, not dropped --
    :func:`send_back_edit` adds ``sent_back``/``sent_back_note``/
    ``sent_back_by_launch``/``sent_back_ts`` to an entry, and a later
    ``record_verdict`` that re-normalizes an array containing them must
    not silently erase an objection. The seven keys above are still forced
    to their canonical shape on every entry, so the documented DDL shape
    is a guaranteed SUBSET of what is stored, never a maximum."""
    normalized: list[dict[str, Any]] = []
    for e in edits or []:
        entry = dict(e)
        entry.update(
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
        normalized.append(entry)
    return normalized


def _union_entry_problems(gate: dict[str, Any], *, ignore_reproduction: bool = False) -> list[str]:
    """Every reason the gate may not enter ``union_applied``, collected rather
    than stopping at the first, so a caller sees the whole picture.
    ``ignore_reproduction=True`` leaves out the reproduction condition only:
    :func:`register_with_deviation` shares the verdict and blocking-edit
    conditions and replaces the reproduction one with its own."""
    problems: list[str] = []

    verdict = gate.get("verdict")
    if verdict not in ("PASS", "PASS_WITH_EDITS"):
        problems.append(f"verdict must be PASS or PASS_WITH_EDITS to apply union, got {verdict!r}")

    edits = _parse_edits(gate.get("edits"))
    unverified_blocking = [e["edit_id"] for e in edits if e.get("blocking") and not e.get("verified")]
    if unverified_blocking:
        problems.append(f"blocking edit(s) not yet verified: {unverified_blocking}")

    if not ignore_reproduction:
        reproduction_status = gate.get("reproduction_status")
        if reproduction_status == "mismatch":
            problems.append("reproduction_status is 'mismatch'")
    return problems


def _check_union_entry(gate: dict[str, Any]) -> None:
    """The F10-resolution enforcement: everything the transition INTO
    ``union_applied`` must verify before it is allowed to land. Collects
    every violation (rather than failing on the first) so a caller sees the
    whole picture in one refusal — the same "combine every reason" style
    ``trialerror.law.service.verify_pin`` uses for its own multi-check refusal."""
    problems = _union_entry_problems(gate)
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
    # A reserved path is a string; checking the type first keeps an unhashable
    # path (a list, an object) from raising TypeError here instead of passing.
    if (
        isinstance(evidence, Mapping)
        and isinstance(evidence.get("path"), str)
        and evidence["path"] in _RESERVED_EVIDENCE_PATHS
    ):
        raise IllegalTransitionError(
            f"gate {gate_id!r}: evidence path {evidence.get('path')!r} is written only by its own verb, "
            "never by a generic transition"
        )
    _require_launch_exists(store, by_launch, field_name="by_launch")
    gate = _require_gate(store, gate_id)
    if to_state == "registered" and gate["state"] in ("gated", "failed"):
        # These two edges are in the graph so recorded transitions validate,
        # but they are legal only inside the two functions that write the
        # transition themselves, after their own preconditions.
        raise IllegalTransitionError(
            f"gate {gate_id!r}: {gate['state']!r} -> 'registered' is not a generic transition; use "
            "register_with_deviation (a disclosed deviation; CLI: `trialerror artifact register --with-deviation`) or "
            "register_failed (a recorded FAIL; CLI: `trialerror artifact register --as-failed`)"
        )
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


def _artifact_file_path(store: Store, artifact: Mapping[str, Any], file: str | Path | None) -> tuple[Path, str]:
    """The file a registration binds to: ``file`` when given, else
    ``artifact.path``. A relative path is tried as given, then against the
    program root. Returns ``(path, the name as written)``."""
    raw = str(file) if file is not None else str(artifact["path"])
    path = Path(raw)
    if not path.is_file() and not path.is_absolute():
        path = store.program_root / path
    return path, raw


def _read_frozen_artifact(
    store: Store,
    artifact: Mapping[str, Any],
    gate: Mapping[str, Any],
    *,
    file: str | Path | None = None,
) -> tuple[str, str, str, str]:
    """The registered file's text, refused unless its sha256 is one the gate
    holds: ``artifact.sha256`` (the submitted bytes, declared when the artifact
    was created for review) or ``gate.post_edit_sha256`` (the corrected bytes,
    recorded by :func:`verify_edit` when the gate's last blocking edit was
    verified). The disclosure a registration rests on must be in bytes the
    gate itself can vouch for, not in an edited copy whose hash nobody
    recorded.

    Returns ``(text, sha256_used, which, path_used)``: ``which`` is
    ``submitted`` or ``post_edit``, and ``path_used`` is the absolute path the
    bytes were read from."""
    path, raw = _artifact_file_path(store, artifact, file)
    if not path.is_file():
        raise RegistrationRefusedError(
            f"artifact {artifact['artifact_id']!r}: its file {raw!r} is missing, so what it "
            "discloses cannot be checked"
        )
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    submitted = artifact.get("sha256")
    corrected = gate.get("post_edit_sha256") or None
    if digest == submitted:
        which = "submitted"
    elif corrected is not None and digest == corrected:
        which = "post_edit"
    else:
        raise RegistrationRefusedError(
            f"artifact {artifact['artifact_id']!r}: its file's hash is {digest}; the gate knows the submitted "
            f"hash {submitted} (and the corrected hash {corrected or 'none'}). A disclosure in a changed file "
            "is not the disclosure the gate saw. Pass `--file` with a copy of the submitted file, or register "
            "the corrected file once the gate has recorded it"
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RegistrationRefusedError(
            f"artifact {artifact['artifact_id']!r}: its file is not UTF-8 text ({exc})"
        ) from exc
    return text, digest, which, str(path.resolve())


def _require_registrable_artifact(store: Store, gate: dict[str, Any]) -> dict[str, Any]:
    artifact = store_get(store, "artifact", pk_column="artifact_id", pk_value=gate["artifact_id"])
    if artifact is None:
        raise RegistrationRefusedError(f"gate {gate['gate_id']!r}: its artifact {gate['artifact_id']!r} does not exist")
    if artifact["status"] != "in_gate" or artifact.get("gate_id") != gate["gate_id"]:
        raise RegistrationRefusedError(
            f"artifact {artifact['artifact_id']!r} is {artifact['status']!r} with gate {artifact.get('gate_id')!r}; "
            f"only the artifact's current gate ({gate['gate_id']!r}) can register it"
        )
    return artifact


def _gate_suite_record(raw: Any) -> dict[str, Any] | None:
    """The gate-suite record a ``reproduction_ref`` string holds, or ``None``
    when it is anything else (a byte-exact ``verify reproduce`` mismatch, say)."""
    try:
        record = json.loads(raw) if raw else None
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict) or record.get("kind") != "gate_suite" or not isinstance(record.get("checks"), list):
        return None
    return record


def _failing_checks_of(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    from trialerror.eval.gate_suites import check_status

    return [c for c in record["checks"] if isinstance(c, dict) and check_status(c) == "fail"]


def _failing_gate_suite_checks(gate: dict[str, Any]) -> list[str]:
    """The names of the checks that failed in the gate-suite record
    ``reproduction_ref`` holds. Any other kind of reproduction record (a
    byte-exact ``verify reproduce`` mismatch, say) is not a disclosed
    deviation and is refused."""
    record = _gate_suite_record(gate.get("reproduction_ref"))
    if record is None:
        raise RegistrationRefusedError(
            f"gate {gate['gate_id']!r}: reproduction_ref is not a gate-suite record, so there is no failing check "
            "to disclose. A reproduction mismatch is not a deviation an artifact can disclose"
        )
    return [str(c.get("name")) for c in _failing_checks_of(record)]


def _commit_registration(
    store: Store,
    *,
    gate_id: str,
    artifact_id: str,
    expected_state: str,
    gate_changes: dict[str, Any],
    artifact_changes: dict[str, Any],
    evidence: Any,
    by_launch: str,
    ts: str,
    supersedes: str | None,
    what: str,
) -> None:
    """The one transaction both registration functions share: re-fetch the
    gate under the write lock and re-check its state (the OB-2 race fix
    ``register_artifact`` uses), move it to ``registered``, write its
    ``gate_transition`` row, and flip the artifact."""
    conn = store.ops
    conn.execute("BEGIN IMMEDIATE")
    try:
        fresh = conn.execute("SELECT * FROM gate WHERE gate_id = ?", (gate_id,)).fetchone()
        if fresh is None or fresh["state"] != expected_state:
            raise RegistrationRefusedError(
                f"gate {gate_id!r}: {what} needs the gate at {expected_state!r} — "
                + (f"it is at {fresh['state']!r}" if fresh is not None else "it no longer exists")
            )
        fresh_artifact = conn.execute("SELECT status FROM artifact WHERE artifact_id = ?", (artifact_id,)).fetchone()
        if fresh_artifact is None or fresh_artifact["status"] != "in_gate":
            raise RegistrationRefusedError(f"artifact {artifact_id!r} is no longer in its gate")
        if supersedes:
            prior = conn.execute("SELECT status FROM artifact WHERE artifact_id = ?", (supersedes,)).fetchone()
            if prior is None or prior["status"] != "registered":
                raise ValidationError(
                    f"{what}: supersedes={supersedes!r} does not name an existing 'registered' artifact"
                )
            raw_update(conn, "artifact", pk_column="artifact_id", pk_value=supersedes, changes={"status": "superseded"})
        raw_update(conn, "gate", pk_column="gate_id", pk_value=gate_id, changes={**gate_changes, "state": "registered"})
        raw_insert(
            conn,
            "gate_transition",
            {
                "gate_id": gate_id,
                "from_state": expected_state,
                "to_state": "registered",
                "ts": ts,
                "by_launch": by_launch,
                "evidence": json.dumps(evidence, ensure_ascii=False),
            },
        )
        raw_update(
            conn, "artifact", pk_column="artifact_id", pk_value=artifact_id,
            changes={
                **artifact_changes,
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
        raise ValidationError(f"{what}: integrity violation: {exc}") from exc
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _registered_bytes_evidence(sha: str, which: str, path: str, note: str | None) -> dict[str, Any]:
    """What the registration evidence says about the bytes it bound to."""
    evidence: dict[str, Any] = {"registered_sha256": sha, "registered_bytes": which, "registered_path": path}
    if note is not None and str(note).strip():
        evidence["note"] = str(note)
    return evidence


def register_with_deviation(
    store: Store,
    *,
    gate_id: str,
    deviations: Sequence[Mapping[str, str]],
    decided_by: str,
    by_launch: str,
    supersedes: str | None = None,
    ts: str | None = None,
    file: str | Path | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Register an artifact whose gate suite failed on something the artifact
    itself discloses, by an operator decision (without this, a report that
    already states a deviation in its own deviations table stays unregistered
    because one gate check found the very thing the report says).

    ``deviations`` is ``[{"check": <suite check name>, "reason": <plain
    words>, "report_ref": <a string that appears in the artifact>}]``. Every
    refusal is a :class:`~trialerror.artifacts.errors.RegistrationRefusedError`
    with a plain-words message:

    1. the gate exists and is ``gated``;
    2. its ``reproduction_status`` is ``mismatch`` (``match`` takes the normal
       path; ``unrun`` has nothing to disclose);
    3. every other condition for ``union_applied`` holds (a passing verdict,
       every blocking edit verified) -- shared with the normal path through
       :func:`_union_entry_problems`;
    4. ``reproduction_ref`` is a gate-suite record, and the deviations cover
       its failing checks EXACTLY: each failing check at least once, no
       deviation naming a check that did not fail;
    5. the registered file contains every ``report_ref`` verbatim -- the
       disclosure has to be in the artifact's own text. The file is ``file``
       when given, else ``artifact.path``; either way its sha256 must be one
       the gate holds: the submitted hash, or the corrected hash the gate
       recorded when its last blocking edit was verified
       (:func:`_read_frozen_artifact`);
    6. ``decided_by`` is non-empty.

    Then, in one transaction, the gate moves ``gated -> registered`` with
    ``disposition='deviation_disclosed'``, a ``gate_transition`` row records
    the path, the decision and the deviations, and the artifact becomes
    ``registered`` with ``disposition='registered_with_deviation'``. Returns
    the artifact row. This is one of only two writers of a ``gated`` or
    ``failed`` gate to ``registered``; :func:`advance_gate` refuses both."""
    if not by_launch:
        raise ValueError("register_with_deviation: by_launch is required")
    _require_launch_exists(store, by_launch, field_name="by_launch")
    if not isinstance(decided_by, str) or not decided_by.strip():
        raise RegistrationRefusedError(
            "register_with_deviation: decided_by is required -- the operator decision that accepts the deviation"
        )
    gate = _require_gate(store, gate_id)
    if gate["state"] != "gated":
        raise RegistrationRefusedError(
            f"gate {gate_id!r} is {gate['state']!r}; registering with a deviation needs a gate at 'gated'"
        )
    status = gate.get("reproduction_status")
    if status == "match":
        raise RegistrationRefusedError(
            f"gate {gate_id!r}: the gate suite passed (reproduction_status 'match'), so there is nothing to "
            "disclose -- use the normal path (apply-union, then register)"
        )
    if status != "mismatch":
        raise RegistrationRefusedError(
            f"gate {gate_id!r}: reproduction_status is {status!r}; only a gate suite that ran and failed "
            "('mismatch') can be registered with a disclosed deviation"
        )
    problems = _union_entry_problems(gate, ignore_reproduction=True)
    if problems:
        raise RegistrationRefusedError(
            f"gate {gate_id!r}: cannot register with a deviation — " + "; ".join(problems)
        )
    failing = _failing_gate_suite_checks(gate)
    if not deviations:
        raise RegistrationRefusedError(
            f"gate {gate_id!r}: no deviation given; the failing check(s) {failing} each need one"
        )
    named: list[str] = []
    for d in deviations:
        check, reason, report_ref = d.get("check"), d.get("reason"), d.get("report_ref")
        if not (isinstance(check, str) and check and isinstance(reason, str) and reason.strip()
                and isinstance(report_ref, str) and report_ref):
            raise RegistrationRefusedError(
                f"gate {gate_id!r}: each deviation needs a check, a reason and a report_ref; got {dict(d)!r}"
            )
        named.append(check)
    uncovered = [c for c in failing if c not in named]
    stray = sorted({c for c in named if c not in failing})
    if uncovered or stray:
        raise RegistrationRefusedError(
            f"gate {gate_id!r}: the deviations must cover exactly the checks that failed -- failing: {failing}; "
            f"not covered: {uncovered or 'none'}; named but not failing: {stray or 'none'}"
        )
    artifact = _require_registrable_artifact(store, gate)
    text, registered_sha, registered_bytes, registered_path = _read_frozen_artifact(
        store, artifact, gate, file=file
    )
    absent = [d["report_ref"] for d in deviations if d["report_ref"] not in text]
    if absent:
        raise RegistrationRefusedError(
            f"artifact {artifact['artifact_id']!r}: the artifact's own text does not contain {absent!r}; a "
            "deviation is registered only when the artifact itself discloses it"
        )

    ts = ts or now()
    recorded = [{"check": d["check"], "reason": d["reason"], "report_ref": d["report_ref"]} for d in deviations]
    _commit_registration(
        store, gate_id=gate_id, artifact_id=artifact["artifact_id"], expected_state="gated",
        gate_changes={"disposition": "deviation_disclosed", "deviation_ref": json.dumps(recorded, ensure_ascii=False)},
        artifact_changes={"disposition": "registered_with_deviation"},
        evidence={
            "path": "register_with_deviation", "decided_by": decided_by, "deviations": recorded,
            **_registered_bytes_evidence(registered_sha, registered_bytes, registered_path, note),
        },
        by_launch=by_launch, ts=ts, supersedes=supersedes, what="register_with_deviation",
    )
    return store_get(store, "artifact", pk_column="artifact_id", pk_value=artifact["artifact_id"])


def fail_on_reproduction(
    store: Store,
    *,
    gate_id: str,
    decided_by: str,
    reason: str,
    by_launch: str,
    ts: str | None = None,
) -> dict[str, Any]:
    """The operator's decision that a gate whose gate-suite reproduction failed
    is a failed result, although the critic passed it: ``gated -> failed``.

    Without it such a gate can go nowhere the operator wants: its
    ``mismatch`` keeps it out of ``union_applied``, and :func:`register_failed`
    wanted a critic ``FAIL`` it will never have. After it,
    :func:`register_failed` accepts the gate (see there).

    Refusals (:class:`~trialerror.artifacts.errors.OperatorFailRefusedError`),
    each before anything is written:

    1. ``decided_by`` or ``reason`` is empty;
    2. the gate is not ``gated`` -- the one state this path names (a ``FAIL``
       verdict already lands at ``failed``; ``union_applied`` cannot hold a
       mismatch; ``draft``/``submitted`` have no reproduction yet; ``failed``
       and ``registered`` are past it);
    3. its ``reproduction_status`` is not ``mismatch``.

    It only ever moves a gate toward ``failed``. The critic's ``verdict``,
    ``edits`` and the reproduction columns are not written: the one
    ``UPDATE`` sets ``gate.state``, and one ``gate_transition`` row is added
    whose evidence carries the decision, the reason and the reproduction
    reference -- both in one transaction, re-checked under its write lock."""
    if not by_launch:
        raise ValueError("fail_on_reproduction: by_launch is required")
    if not isinstance(decided_by, str) or not decided_by.strip():
        raise OperatorFailRefusedError(
            "fail_on_reproduction: decided_by is required -- the operator decision that fails the gate"
        )
    if not isinstance(reason, str) or not reason.strip():
        raise OperatorFailRefusedError("fail_on_reproduction: reason is required -- say in words why it failed")
    _require_launch_exists(store, by_launch, field_name="by_launch")
    _require_gate(store, gate_id)
    ts = ts or now()

    conn = store.ops
    conn.execute("BEGIN IMMEDIATE")
    try:
        fresh = conn.execute("SELECT * FROM gate WHERE gate_id = ?", (gate_id,)).fetchone()
        if fresh is None:
            raise ValueError(f"no such gate: {gate_id!r}")
        gate = dict(fresh)
        if gate["state"] != "gated":
            raise OperatorFailRefusedError(
                f"gate {gate_id!r} is {gate['state']!r}; an operator-decided failure on a mismatched "
                "reproduction needs the gate at 'gated'"
            )
        if gate.get("reproduction_status") != "mismatch":
            raise OperatorFailRefusedError(
                f"gate {gate_id!r}: reproduction_status is {gate.get('reproduction_status')!r}; this path fails "
                "only a gate whose recorded reproduction is 'mismatch'"
            )
        evidence = {
            "path": OPERATOR_FAIL_PATH,
            "decided_by": decided_by,
            "reason": reason,
            "reproduction_status": gate["reproduction_status"],
            "reproduction_ref": gate.get("reproduction_ref"),
        }
        _execute_transition(conn, gate=gate, to_state="failed", by_launch=by_launch, evidence=evidence, ts=ts)
        conn.execute("COMMIT")
    except sqlite3.IntegrityError as exc:
        conn.execute("ROLLBACK")
        raise ValidationError(f"fail_on_reproduction: integrity violation: {exc}") from exc
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _require_gate(store, gate_id)


def _operator_fail_record(store: Store, gate_id: str) -> dict[str, Any] | None:
    """The evidence of the gate's move into ``failed`` when
    :func:`fail_on_reproduction` wrote it (``gated -> failed`` with
    :data:`OPERATOR_FAIL_PATH`), else ``None``."""
    row = store.ops.execute(
        "SELECT from_state, evidence FROM gate_transition WHERE gate_id = ? AND to_state = 'failed' "
        "ORDER BY ts DESC, rowid DESC LIMIT 1",
        (gate_id,),
    ).fetchone()
    if row is None or row["from_state"] != "gated":
        return None
    try:
        evidence = json.loads(row["evidence"]) if row["evidence"] else None
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(evidence, dict) or evidence.get("path") != OPERATOR_FAIL_PATH:
        return None
    return evidence


def register_failed(
    store: Store,
    *,
    gate_id: str,
    failure_ref: str | None = None,
    decided_by: str,
    by_launch: str,
    supersedes: str | None = None,
    ts: str | None = None,
    file: str | Path | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Register an artifact as a FAILED result: the gate failed and the
    operator decided the failure belongs on the record (without this, a
    result that failed stays off the record). Refusals
    (:class:`~trialerror.artifacts.errors.RegistrationRefusedError`):

    1. the gate is ``failed``;
    2. it failed in one of two ways: its ``verdict`` is ``FAIL`` (the
       critic's), or it reached ``failed`` through :func:`fail_on_reproduction`
       (the operator's decision on a ``mismatch`` reproduction, which must
       still read ``mismatch``). A review abandoned without either -- a bare
       ``gate advance --to failed`` -- is not a failed result;
    3. ``failure_ref`` is given and the registered file contains it verbatim,
       the artifact's own statement of what failed. On the operator's path
       ``failure_ref`` is optional: the failure is stated by the gate's own
       record instead (see below), and a ``failure_ref`` that IS given must
       still appear in the registered file. On the critic's ``FAIL`` path it
       stays mandatory;
    4. ``decided_by`` is non-empty;
    5. the registered file is ``file`` when given, else ``artifact.path``, and
       its sha256 is one the gate holds (:func:`_read_frozen_artifact`).

    **The failure basis is the gate's record.** On the operator's path the
    evidence gains ``failure_basis``: the decision to fail the gate and the
    checks that failed, both read from the ``gated -> failed`` transition's
    own evidence (its frozen ``reproduction_ref``), never from the live
    ``gate`` columns. When that reference is not a gate-suite record, the
    registration is refused.

    Then, in one transaction, the gate moves ``failed -> registered`` with
    ``disposition='failure_registered'``, a ``gate_transition`` row records
    the path, the decision and the reference (and, on the second way, the
    ``basis`` and the decision that failed the gate), and the artifact
    becomes ``registered`` with ``disposition='registered_failed'``. The
    critic's verdict is left as it was on either way. Returns the artifact
    row."""
    if not by_launch:
        raise ValueError("register_failed: by_launch is required")
    _require_launch_exists(store, by_launch, field_name="by_launch")
    if not isinstance(decided_by, str) or not decided_by.strip():
        raise RegistrationRefusedError(
            "register_failed: decided_by is required -- the operator decision that puts the failure on the record"
        )
    gate = _require_gate(store, gate_id)
    if gate["state"] != "failed":
        raise RegistrationRefusedError(
            f"gate {gate_id!r} is {gate['state']!r}; registering a failed result needs a gate at 'failed'"
        )
    operator_fail = None
    if gate.get("verdict") != "FAIL":
        operator_fail = _operator_fail_record(store, gate_id)
        if operator_fail is None:
            raise RegistrationRefusedError(
                f"gate {gate_id!r}: its verdict is {gate.get('verdict')!r}, not 'FAIL', and it did not reach "
                "'failed' by an operator decision on a mismatched reproduction (`trialerror gate "
                "fail-reproduction`) -- a review that was abandoned is not a failed result"
            )
        if operator_fail.get("reproduction_status") != "mismatch":
            raise RegistrationRefusedError(
                f"gate {gate_id!r}: the decision that failed it recorded reproduction_status "
                f"{operator_fail.get('reproduction_status')!r}, not 'mismatch'; the basis of that decision does not hold"
            )
        record = _gate_suite_record(operator_fail.get("reproduction_ref"))
        if record is None:
            raise RegistrationRefusedError(
                f"gate {gate_id!r}: the failed checks cannot be read from the gate's record: the reproduction "
                "reference frozen with the decision that failed it is not a gate-suite record"
            )
        failure_basis = {
            "kind": OPERATOR_FAIL_PATH,
            "decided_by": operator_fail.get("decided_by"),
            "failing_checks": [
                {"name": str(c.get("name")), "message": c.get("message")} for c in _failing_checks_of(record)
            ],
        }
    else:
        failure_basis = None
    if failure_ref is None or not isinstance(failure_ref, str) or not failure_ref:
        if failure_basis is None:
            raise RegistrationRefusedError(
                f"gate {gate_id!r}: failure_ref is required -- a string from the artifact that states what failed"
            )
        if failure_ref is not None:
            raise RegistrationRefusedError(
                f"gate {gate_id!r}: failure_ref is empty -- give a string from the artifact that states what "
                "failed, or leave it out to register on the gate's own record"
            )
    artifact = _require_registrable_artifact(store, gate)
    text, registered_sha, registered_bytes, registered_path = _read_frozen_artifact(
        store, artifact, gate, file=file
    )
    if failure_ref and failure_ref not in text:
        raise RegistrationRefusedError(
            f"artifact {artifact['artifact_id']!r}: the artifact's own text does not contain {failure_ref!r}; a "
            "failed result is registered only when the artifact itself states the failure"
        )

    ts = ts or now()
    _commit_registration(
        store, gate_id=gate_id, artifact_id=artifact["artifact_id"], expected_state="failed",
        gate_changes={"disposition": "failure_registered"},
        artifact_changes={"disposition": "registered_failed"},
        evidence={
            "path": "register_failed", "decided_by": decided_by,
            **({"failure_ref": failure_ref} if failure_ref else {}),
            **(
                {"basis": OPERATOR_FAIL_PATH, "failed_by": operator_fail.get("decided_by"), "failure_basis": failure_basis}
                if operator_fail is not None else {}
            ),
            **_registered_bytes_evidence(registered_sha, registered_bytes, registered_path, note),
        },
        by_launch=by_launch, ts=ts, supersedes=supersedes, what="register_failed",
    )
    return store_get(store, "artifact", pk_column="artifact_id", pk_value=artifact["artifact_id"])


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
    already passed.

    **Concurrency (WA-1, sweep batch W3, the worst case in that finding).**
    ``edits`` is ONE JSON column holding the whole array, so marking one
    entry is a read-modify-write of every entry. Before this fix that
    read-modify-write straddled two auto-commits: six appliers verifying
    six DIFFERENT edits on the same gate each read the array, changed
    their own entry, and wrote the whole thing back — the last writer's
    copy won and five acknowledged verifications vanished with no audit
    trail (``verify_edit`` writes no event, by design, so nothing recorded
    that they had happened). The entire read-modify-write now runs inside
    one ``BEGIN IMMEDIATE`` transaction (:func:`_mutate_edit_in_txn`),
    which restores per-EDIT granularity: concurrent verifications of
    different edits all survive, and concurrent verifications of the SAME
    edit are idempotent rather than interleaved."""
    if not by_launch:
        raise ValueError("verify_edit: by_launch is required")
    _require_launch_exists(store, by_launch, field_name="by_launch")

    stamped = ts or now()
    outcome: dict[str, Any] = {"post_edit_recorded": False, "post_edit_note": None}

    def _apply(entry: dict[str, Any]) -> None:
        entry["applied"] = True
        entry["applied_by_launch"] = by_launch
        entry["verified"] = True
        entry["verified_note"] = verified_note
        entry["verified_ts"] = stamped

    def _record_corrected_bytes(
        gate: dict[str, Any], before: list[dict[str, Any]], after: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """When THIS call makes every blocking edit verified, the gate reads
        the artifact's file and keeps its hash (and the moment) itself."""
        def all_verified(entries: list[dict[str, Any]]) -> bool:
            blocking = [e for e in entries if e.get("blocking")]
            return bool(blocking) and all(e.get("verified") for e in blocking)

        if not all_verified(after) or all_verified(before):
            return {}
        artifact = store_get(store, "artifact", pk_column="artifact_id", pk_value=gate["artifact_id"])
        try:
            if artifact is None:
                raise OSError("its artifact row does not exist")
            path, _raw = _artifact_file_path(store, artifact, None)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            outcome["post_edit_note"] = (
                f"the corrected file could not be read ({type(exc).__name__}), so the gate recorded no "
                "corrected hash; a registration can still bind to the submitted bytes"
            )
            return {}
        outcome["post_edit_recorded"] = True
        return {"post_edit_sha256": digest, "post_edit_ts": stamped}

    row = _mutate_edit_in_txn(
        store, caller="verify_edit", gate_id=gate_id, edit_id=edit_id, mutate=_apply,
        also_changes=_record_corrected_bytes,
    )
    return {**row, **outcome}


def send_back_edit(
    store: Store,
    *,
    gate_id: str,
    edit_id: str,
    by_launch: str,
    note: str,
    ts: str | None = None,
) -> dict[str, Any]:
    """The non-destructive counterpart to :func:`verify_edit`: the applier
    (or the operator at the dashboard) objects to a critic's edit and
    sends it BACK instead of applying it.

    Like :func:`verify_edit`, this is NOT a state transition — it mutates
    one entry of the ``edits`` JSON array and writes no ``gate_transition``
    row. The entry is marked ``applied=False, verified=False,
    sent_back=True`` plus ``sent_back_note`` / ``sent_back_by_launch`` /
    ``sent_back_ts``. Because it leaves the entry UNVERIFIED,
    :func:`_check_union_entry` is untouched by design: a sent-back
    blocking edit still blocks ``union_applied``, with the same
    "blocking edit(s) not yet verified" refusal. Sending back is a request
    for work, not a way around the gate.

    ``note`` is REQUIRED — a send-back with no stated objection is the
    freeze-without-reason case (``trialerror.rooms.api.freeze_room`` refuses
    it for the same reason).

    Unlike verify (whose record is the JSON entry itself, design 12.12),
    a send-back DOES emit an event, ``gate_edit_sent_back``, in the same
    transaction: the objection has to be discoverable by whoever has to do
    the work, and nothing else in the schema would carry it.

    Refuses (:class:`ValueError`) if the gate is not ``gated``, if
    ``edit_id`` names no edit on it, or if that edit is already
    ``verified`` (re-opening a verification is a verdict-level act, not an
    applier-level one); refuses
    (:class:`~trialerror.stores.errors.XidTargetMissingError`) if
    ``by_launch`` names no real launch."""
    if not by_launch:
        raise ValueError("send_back_edit: by_launch is required")
    if not note or not str(note).strip():
        raise ValueError("send_back_edit: note is required (a send-back with no stated objection is not actionable)")
    _require_launch_exists(store, by_launch, field_name="by_launch")
    stamped = ts or now()

    def _apply(entry: dict[str, Any]) -> None:
        if entry.get("verified"):
            raise ValueError(
                f"send_back_edit: edit {edit_id!r} on gate {gate_id!r} is already verified; "
                "a verified edit cannot be sent back (record a new verdict instead)"
            )
        entry["applied"] = False
        entry["verified"] = False
        entry["sent_back"] = True
        entry["sent_back_note"] = note
        entry["sent_back_by_launch"] = by_launch
        entry["sent_back_ts"] = stamped

    return _mutate_edit_in_txn(
        store, caller="send_back_edit", gate_id=gate_id, edit_id=edit_id, mutate=_apply,
        event=lambda gate: (
            "gate_edit_sent_back",
            {
                "gate_id": gate_id,
                "edit_id": edit_id,
                "artifact_id": gate.get("artifact_id"),
                "note": note,
                "by_launch": by_launch,
            },
        ),
        ts=stamped,
        by_launch=by_launch,
    )


def _mutate_edit_in_txn(
    store: Store,
    *,
    caller: str,
    gate_id: str,
    edit_id: str,
    mutate: Callable[[dict[str, Any]], None],
    event: Callable[[dict[str, Any]], tuple[str, dict[str, Any]]] | None = None,
    ts: str | None = None,
    by_launch: str | None = None,
    also_changes: Callable[[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The ONE place a single entry of ``gate.edits`` is mutated, and the
    close of WA-1's worst case (see :func:`verify_edit`'s own docstring for
    what the race lost).

    The gate row is re-read INSIDE a ``BEGIN IMMEDIATE`` transaction, so
    the ``edits`` array this mutates is the array as it stands under the
    write lock — never a copy read before some other writer's commit. The
    state precondition is re-checked on that fresh row too: a gate that
    left ``gated`` while this caller was deciding refuses rather than
    writing into a gate that already passed its union check.

    ``event``, when given, is called with the fresh gate row and returns
    ``(event_type, payload)`` for one ``event`` row written inside the SAME
    transaction — so a mutation and its audit record land together or not
    at all. ``by_launch`` must already be XID-validated by the public
    caller (``trialerror.artifacts._txn``'s contract).

    ``also_changes``, when given, is called inside the same transaction with
    ``(fresh gate row, the edits before, the edits after)`` and returns extra
    ``gate`` columns to write in the same UPDATE as the ``edits`` array."""
    conn = store.ops
    conn.execute("BEGIN IMMEDIATE")
    try:
        fresh = conn.execute("SELECT * FROM gate WHERE gate_id = ?", (gate_id,)).fetchone()
        if fresh is None:
            raise ValueError(f"no such gate: {gate_id!r}")
        gate = dict(fresh)
        if gate["state"] != "gated":
            raise ValueError(
                f"{caller}: gate {gate_id!r} must be 'gated' to change an edit, is {gate['state']!r}"
            )
        edits = _parse_edits(gate.get("edits"))
        match = next((e for e in edits if e["edit_id"] == edit_id), None)
        if match is None:
            raise ValueError(f"{caller}: no edit {edit_id!r} on gate {gate_id!r}")
        edits_before = json.loads(json.dumps(edits))
        mutate(match)
        changes: dict[str, Any] = {"edits": json.dumps(edits, ensure_ascii=False)}
        if also_changes is not None:
            changes.update(also_changes(gate, edits_before, edits))
        raw_update(conn, "gate", pk_column="gate_id", pk_value=gate_id, changes=changes)
        if event is not None:
            event_type, payload = event(gate)
            append_event_in_txn(conn, event_type=event_type, payload=payload, launch_id=by_launch, ts=ts)
        conn.execute("COMMIT")
    except sqlite3.IntegrityError as exc:
        conn.execute("ROLLBACK")
        raise ValidationError(f"{caller}: integrity violation on gate {gate_id!r}: {exc}") from exc
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _require_gate(store, gate_id)
