"""The spawn gate's atomic booking claim. Design Section 5.4 (PreToolUse:
Task row) + review finding F2 (BLOCKING, now closed): "the spawn gate
CONSUMES the booking via one conditional UPDATE (state->RUNNING iff
PROVISIONAL + open session + unexpired TTL), with model-class and
law-pin checks in the same gate; any non-PROVISIONAL token -> exit 2."

This module is import-testable on its own (pytest calls :func:`evaluate_spawn`
directly against a fixture store) - the standalone script
``plugin/hooks/spawn_gate.py`` is a thin adapter that reads Claude Code's
PreToolUse hook JSON off stdin and turns a :class:`GateResult` into the
hook protocol's exit code (0 allow / 2 refuse). Design Section 12 M3 row:
"live-CC hook tests are orchestrator-executed integration items" - the
LOGIC this module implements is exactly what those integration tests would
exercise; it does not require a live Claude Code session to verify.

**One booking = one spawn, structurally.** The claim below is a single
``UPDATE ... WHERE launch_id=? AND state='PROVISIONAL' AND session_id=? AND
<TTL unexpired>`` statement. Two concurrent attempts to consume the SAME
``launch_id`` race at the SQLite level (WAL + ``busy_timeout`` serializes
them per ``trialerror.stores.connection``): whichever UPDATE commits first flips
``state`` away from ``PROVISIONAL``, so the second one's ``WHERE`` clause
matches zero rows and its ``rowcount`` is 0 - no second UPDATE ever sees
``state='PROVISIONAL'`` again for that row. A copy-pasted stale token
cannot ride an old booking (the adversarial "token-reuse-refused" case).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from trialerror.stores import get
from trialerror.stores.store import Store
from trialerror.util.timeutil import now

__all__ = [
    "LAUNCH_ID_RE",
    "GateResult",
    "extract_launch_id_token",
    "resolve_open_session",
    "resolve_booking_identity",
    "evaluate_spawn",
    "evaluate_spawn_for_open_session",
]

#: Design Section 5.4: "extracts the `launch_id:` token from the subagent
#: prompt." ``new_id("LNCH")`` (trialerror.util.ids) produces ``LNCH-<26-char
#: Crockford-Base32 ULID>``; matched case-sensitively, `:` or `=` separator
#: to tolerate either "launch_id: LNCH-..." or "launch_id=LNCH-..." prompt
#: conventions.
LAUNCH_ID_RE = re.compile(r"\blaunch_id\s*[:=]\s*(LNCH-[0-9A-Z]{20,32})\b")


def extract_launch_id_token(prompt_text: str | None) -> str | None:
    """Pull the first ``launch_id:`` token out of a subagent prompt, or
    ``None`` if none is present."""
    if not prompt_text:
        return None
    m = LAUNCH_ID_RE.search(prompt_text)
    return m.group(1) if m else None


@dataclass
class GateResult:
    """The spawn gate's verdict. ``allowed=True`` iff the booking was just
    atomically consumed (PROVISIONAL -> RUNNING) by THIS call."""

    allowed: bool
    code: str
    message: str
    launch_id: str | None = None
    next_command: list[str] | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "code": self.code,
            "message": self.message,
            "launch_id": self.launch_id,
            "next_command": self.next_command,
            "details": self.details,
        }


_NO_TOKEN_NEXT_COMMAND = [
    "trialerror",
    "budget",
    "book",
    "--purpose",
    "<purpose>",
    "--model-class",
    "<top|mid|small>",
    "--model",
    "<model>",
]


def resolve_open_session(store: Store) -> dict | None:
    """The program's single OPEN session, or ``None`` if zero are open.
    Raises :class:`RuntimeError` if more than one is open - ops.db is
    scoped to one program, and design Section 4.2/4.3 assume exactly one
    open session governs booking/spawn attribution at a time; more than one
    is a bug elsewhere (e.g. a crashed session never marked ``abandoned``),
    not a case the gate should silently pick a winner for."""
    rows = store.ops.execute("SELECT * FROM session WHERE status = 'open'").fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        raise RuntimeError(
            f"{len(rows)} sessions are OPEN simultaneously in this program's ops.db "
            "(expected at most 1) - the spawn gate refuses to guess which one owns "
            "this spawn attempt"
        )
    return dict(rows[0])


def resolve_booking_identity(
    store: Store, *, session_id: str | None = None, program_id: str | None = None
) -> tuple[str, str, dict[str, str]]:
    """``(session_id, program_id, resolved_from)`` for a booking. Lane FB-7
    item 6.

    ``budget status`` and ``budget reconcile`` resolve the open session on
    their own (lane FB-1's F7 pattern); ``budget book`` refused without
    ``--session-id`` and ``--program-id``, so the one verb an operator runs
    most demanded back two values the harness already knows. The session id
    is bound at ``session boot`` and the program id is
    ``trialerror.toml``'s ``[program] id`` -- neither is a judgement call.

    ``resolved_from`` names where each value came from (``"flag"``,
    ``"open_session"``, ``"config"``), so an envelope can say what it
    assumed rather than leaving an operator to infer it.

    **Explicit always wins**, and nothing is guessed:

    - ``session_id`` given -> used verbatim (the downstream precondition
      check still refuses a closed one, which is the right place for that).
    - no open session -> :class:`~trialerror.budget.errors.NoOpenSessionError`,
      naming the flag AND ``session boot``.
    - more than one open session -> :func:`resolve_open_session`'s own
      ``RuntimeError``, surfaced rather than swallowed. Picking one would
      book against a session nobody named, which is precisely the state
      that produces bookings nothing can reconcile.
    - ``program_id`` absent and no readable ``[program] id`` ->
      :class:`ValueError` naming the flag and the config key.
    """
    from trialerror.budget.errors import NoOpenSessionError

    resolved_from: dict[str, str] = {}
    if session_id:
        resolved_from["session_id"] = "flag"
    else:
        session = resolve_open_session(store)  # RuntimeError for >1, deliberately not caught
        if session is None:
            raise NoOpenSessionError(
                "no --session-id given and no OPEN session in this program's ops.db to read one from: "
                "pass --session-id, or run `trialerror session boot`"
            )
        session_id = str(session["session_id"])
        resolved_from["session_id"] = "open_session"

    if program_id:
        resolved_from["program_id"] = "flag"
    else:
        from trialerror.util.config import CONFIG_FILENAME, ConfigError, load_config

        try:
            program_id = load_config(store.program_root / CONFIG_FILENAME).program_id
        except ConfigError as exc:
            raise ValueError(
                f"no --program-id given and this program's {CONFIG_FILENAME} could not supply one "
                f"([program] id): {exc}"
            ) from exc
        resolved_from["program_id"] = "config"
    return str(session_id), str(program_id), resolved_from


def _latest_law_digest_version(store: Store) -> str | None:
    row = store.ops.execute(
        "SELECT version FROM law_digest ORDER BY generated_ts DESC LIMIT 1"
    ).fetchone()
    return row["version"] if row is not None else None


def _check_law_pin_freshness(store: Store, session: Mapping[str, Any]) -> str | None:
    """Design Section 5.4 mid-flight-staleness note: "the next spawn gate
    ... catch[es] it." A session with no recorded ``boot_pin_version``, or
    a program with no ``law_digest`` rows yet (M4 not wired, or simply
    "before the first law append"), is NOT a staleness failure - there is
    nothing to compare against. Returns an error message if stale, else
    ``None``."""
    pin = session.get("boot_pin_version")
    if not pin:
        return None
    latest = _latest_law_digest_version(store)
    if latest is None or latest == pin:
        return None
    # A session's pin is the law service's ``format_pin`` shape,
    # ``<version>@<YYYY-MM-DD>``, while ``law_digest.version`` is the bare
    # ``<version>``: compare the version part (2026-09-16: the dated pin the
    # SessionStart hook records was refused as stale by this exact-string
    # compare on every direct Agent spawn, while ``law verify`` called the
    # same pin current -- the Workflow tool never fires this hook, which is
    # how the mismatch stayed hidden). A dated pin whose version part is the
    # latest digest version is fresh; anything else is stale.
    pin_version = str(pin).split("@", 1)[0]
    if pin_version == latest:
        return None
    return (
        f"session booted at law pin {pin!r} but the latest law_digest version is "
        f"{latest!r} - pin is stale (run `trialerror law verify` / reboot the session)"
    )


def _check_model_policy(
    store: Store, launch_row: Mapping[str, Any], policy: Mapping[str, str] | None
) -> tuple[str, str] | None:
    """Re-check model policy AT SPAWN TIME (defense in depth over the check
    ``book_launch`` already performed - design Section 5.4: "the same check
    verifies model_class against model policy for the stated purpose").
    An accepted override is recorded in ``launch.attrs.override_ruling_id``
    at booking time (see ``trialerror.budget.pools.book_launch``, whose own
    ``_check_model_policy`` already refuses an unknown ruling id AT
    BOOKING TIME).

    EP-1 Bypass D (C-0064 fix-tier3): this function used to accept
    ``override_ruling_id`` by mere PRESENCE, trusting book-time validation
    unconditionally -- true for every launch row ``book_launch`` itself
    created, but not for one hand-forged via a raw ``trialerror.stores.insert``
    that skips ``book_launch`` entirely (the review's own framing: "naive
    successors don't hand-forge attrs", OBSERVATION not BLOCKING). Now
    re-checks existence here too -- one extra read against ``ops.ruling``,
    only on the already-rare path where model policy is NOT met on its own
    (the fast, no-extra-query path for a compliant booking is unaffected).

    Returns ``(message, code)`` on refusal, ``None`` on pass. ``code`` is
    ``"model_policy_violation"`` (no override at all) or
    ``"override_ruling_unknown"`` (an override_ruling_id present but not a
    real ``ops.ruling`` row)."""
    from trialerror.budget.policy import meets_minimum, required_class_for_purpose

    minimum = required_class_for_purpose(dict(policy) if policy else None, launch_row["purpose"])
    if meets_minimum(launch_row["model_class"], minimum):
        return None
    attrs_raw = launch_row.get("attrs")
    attrs = json.loads(attrs_raw) if attrs_raw else {}
    override_ruling_id = attrs.get("override_ruling_id")
    if override_ruling_id:
        ruling = get(store, "ruling", pk_column="ruling_id", pk_value=override_ruling_id)
        if ruling is None:
            return (
                f"launch.attrs.override_ruling_id {override_ruling_id!r} does not name an existing "
                "ops.ruling row -- refused even though book-time validation should already have "
                "caught this (this launch row was likely created outside trialerror.budget.pools.book_launch)",
                "override_ruling_unknown",
            )
        return None
    return (
        f"purpose {launch_row['purpose']!r} requires model_class >= {minimum!r}, "
        f"booking has {launch_row['model_class']!r} with no override ruling",
        "model_policy_violation",
    )


def _check_agent_model_matches_booking(
    launch_row: Mapping[str, Any],
    agent_model: str | None,
    model_classes: Mapping[str, str] | None,
) -> tuple[str, str] | None:
    """``agent_model_matches_booking``: the model a subagent is actually
    being SPAWNED with must be at least the class its booking claimed.

    :func:`_check_model_policy` above checks one half -- "does this purpose
    require a class the booking meets". It structurally cannot see the
    other: book ``top``, spawn something cheaper, and every check upstream
    still passes while the work is done on a model the round's own floors
    ruled out. This is that other half.

    ``agent_model=None`` (the Task call named no model, and no agent file
    named one either) asserts nothing and cannot be a mismatch -- it passes.
    A non-empty model that resolves to no class does NOT pass: a spawn whose
    model cannot be placed is a spawn this gate cannot verify, and the
    gate's posture for anything it cannot verify is to fail closed. The fix
    is one line in ``trialerror.toml``'s ``[model_classes]``, which the refusal
    names in full.

    Returns ``(message, code)`` on refusal, ``None`` on pass."""
    from trialerror.budget.policy import NO_CLAIM_MODEL_VALUES, classify_model, meets_minimum

    if agent_model is None or str(agent_model).strip() == "":
        return None
    spawned_class = classify_model(agent_model, model_classes=dict(model_classes) if model_classes else None)
    if spawned_class is None:
        if str(agent_model).strip().lower() in NO_CLAIM_MODEL_VALUES:
            return None
        return (
            f"the spawn names model {agent_model!r}, which resolves to no model class - this gate "
            "cannot check it against the booking's class and will not guess. Declare it in "
            "trialerror.toml's [model_classes] table (e.g. "
            f'{str(agent_model).strip().lower()!r} = "top") and retry',
            "agent_model_unclassified",
        )
    booked_class = launch_row["model_class"]
    if meets_minimum(spawned_class, booked_class):
        return None
    return (
        f"booking claims model_class {booked_class!r} for purpose {launch_row['purpose']!r}, but the "
        f"spawn names model {agent_model!r} ({spawned_class!r} class) - booking one class and spawning "
        f"a cheaper one is the exact failure this guard exists for; book at {spawned_class!r}, or "
        f"spawn a {booked_class!r} model",
        "agent_model_below_booking",
    )


def evaluate_spawn(
    store: Store,
    prompt_text: str | None,
    *,
    session_id: str,
    policy: Mapping[str, str] | None = None,
    agent_model: str | None = None,
    model_classes: Mapping[str, str] | None = None,
    now_ts: str | None = None,
) -> GateResult:
    """The spawn gate's core decision, given an explicit ``session_id``
    (the caller - normally :func:`evaluate_spawn_for_open_session` - is
    responsible for resolving which session that is). Deterministic and
    side-effect-free EXCEPT for the one conditional ``UPDATE`` that fires
    only when every check upstream of it has already passed.
    """
    ts = now_ts or now()

    token = extract_launch_id_token(prompt_text)
    if token is None:
        return GateResult(
            allowed=False,
            code="no_launch_id_token",
            message="no `launch_id:` token found in the subagent prompt - an unbooked "
            "spawn is refused (budget-at-spawn, design Section 1 commitment 1)",
            next_command=_NO_TOKEN_NEXT_COMMAND,
        )

    row = get(store, "launch", pk_column="launch_id", pk_value=token)
    if row is None:
        return GateResult(
            allowed=False,
            code="unknown_launch_id",
            message=f"no booking found for {token!r} in platform.db",
            launch_id=token,
            next_command=_NO_TOKEN_NEXT_COMMAND,
        )

    if row["session_id"] != session_id:
        return GateResult(
            allowed=False,
            code="wrong_session",
            message=f"{token!r} was booked under a different session ({row['session_id']!r}); "
            f"the currently open session is {session_id!r} - book a fresh launch under it",
            launch_id=token,
            next_command=_NO_TOKEN_NEXT_COMMAND,
        )

    if row["state"] != "PROVISIONAL":
        return GateResult(
            allowed=False,
            code="token_not_provisional",
            message=f"{token!r} is in state {row['state']!r}, not PROVISIONAL - it has already "
            "been consumed by a previous spawn (or refused/deferred/reconciled/abandoned). "
            "One booking = one spawn: book a new launch_id.",
            launch_id=token,
            next_command=_NO_TOKEN_NEXT_COMMAND,
            details={"state": row["state"]},
        )

    session = get(store, "session", pk_column="session_id", pk_value=session_id)
    if session is not None:
        pin_msg = _check_law_pin_freshness(store, session)
        if pin_msg:
            return GateResult(
                allowed=False,
                code="stale_law_pin",
                message=pin_msg,
                launch_id=token,
                next_command=["trialerror", "law", "verify"],
            )

    policy_result = _check_model_policy(store, row, policy)
    if policy_result:
        policy_msg, policy_code = policy_result
        return GateResult(
            allowed=False,
            code=policy_code,
            message=policy_msg,
            launch_id=token,
            next_command=_NO_TOKEN_NEXT_COMMAND,
        )

    agent_model_result = _check_agent_model_matches_booking(row, agent_model, model_classes)
    if agent_model_result:
        agent_msg, agent_code = agent_model_result
        return GateResult(
            allowed=False,
            code=agent_code,
            message=agent_msg,
            launch_id=token,
            next_command=_NO_TOKEN_NEXT_COMMAND,
            details={"agent_model": agent_model, "booked_model_class": row["model_class"]},
        )

    # ---- the atomic claim (F2) ------------------------------------------
    # Single conditional UPDATE: only flips PROVISIONAL -> RUNNING if the
    # row STILL is PROVISIONAL, belongs to this session, and its TTL has
    # not expired since booking. WAL + busy_timeout (trialerror.stores.connection)
    # serialize concurrent attempts against the SAME row; whichever commits
    # first wins the WHERE clause, the loser's rowcount is 0.
    with store.platform:
        cur = store.platform.execute(
            "UPDATE launch SET state = 'RUNNING' "
            "WHERE launch_id = :launch_id AND state = 'PROVISIONAL' AND session_id = :session_id "
            "AND julianday(:now) <= julianday(booked_ts) + (booking_ttl_s / 86400.0)",
            {"launch_id": token, "session_id": session_id, "now": ts},
        )
        consumed = cur.rowcount == 1

    if consumed:
        return GateResult(
            allowed=True,
            code="consumed",
            message=f"{token!r} consumed: PROVISIONAL -> RUNNING; spawn allowed",
            launch_id=token,
            details={"model_class": row["model_class"], "model": row["model"], "purpose": row["purpose"]},
        )

    # Lost the race, or the TTL expired between the SELECT above and the
    # UPDATE (re-read to report the precise reason instead of a generic one).
    fresh = get(store, "launch", pk_column="launch_id", pk_value=token) or row
    if fresh["state"] != "PROVISIONAL":
        return GateResult(
            allowed=False,
            code="token_not_provisional",
            message=f"{token!r} was consumed by a concurrent spawn attempt an instant ago "
            f"(now in state {fresh['state']!r}) - one booking = one spawn",
            launch_id=token,
            next_command=_NO_TOKEN_NEXT_COMMAND,
            details={"state": fresh["state"]},
        )
    return GateResult(
        allowed=False,
        code="ttl_expired",
        message=f"{token!r}'s booking TTL ({row['booking_ttl_s']}s from {row['booked_ts']}) has "
        "expired - book a fresh launch_id",
        launch_id=token,
        next_command=_NO_TOKEN_NEXT_COMMAND,
    )


def evaluate_spawn_for_open_session(
    store: Store,
    prompt_text: str | None,
    *,
    policy: Mapping[str, str] | None = None,
    agent_model: str | None = None,
    model_classes: Mapping[str, str] | None = None,
    now_ts: str | None = None,
) -> GateResult:
    """Convenience wrapper the hook script uses: resolves the program's
    open session itself (rather than requiring the caller to know it) and
    refuses outright if there isn't exactly one."""
    session = resolve_open_session(store)
    if session is None:
        return GateResult(
            allowed=False,
            code="no_open_session",
            message="no OPEN session in this program's ops.db - boot a session before spawning "
            "(`trialerror session boot`)",
            next_command=["trialerror", "session", "boot"],
        )
    return evaluate_spawn(
        store,
        prompt_text,
        session_id=session["session_id"],
        policy=policy,
        agent_model=agent_model,
        model_classes=model_classes,
        now_ts=now_ts,
    )
