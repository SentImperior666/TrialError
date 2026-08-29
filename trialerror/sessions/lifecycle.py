"""Session boot/close as refusing tools. Design Section 12 (M6 row):
"open/close/refusals, account binding at boot, boot bundle, ... course-check."
Design Section 5.4 (SessionStart/Stop rows) + Section 4.3 (binding rules:
"``session boot`` binds the session to an ``account_id`` ... hooks and
``book_launch`` read the open session's account — pool attribution is never
guessed"). REQUIREMENTS Section 1.3: "boot = a tool call that verifies
pins, arms watchdog, replays deltas; close = a tool call that refuses if
launches dangle or the digest is stale."

**Design philosophy (matches ``trialerror.budget.pools``'s own split):** expected
"can't do this yet" outcomes — ambiguous account, session already open,
dangling launches, stale pin, unread inbox, missing course-check — are
returned as structured, non-``ok`` result objects (:class:`BootResult` /
:class:`CloseResult`), never raised. Only a genuine programming error
(an unknown ``session_id`` passed to a function that requires one to
already exist) raises. This is design Section 5.1's cross-cutting rule
("errors returned as structured content ... never exceptions") applied to
this module's own core API, not just its CLI/hook wrappers — the same
choice ``trialerror.budget.errors`` documents for the budget module.

**TRIALERROR-DEV-NOTE (the ``abandoned`` status has no assigned owner):** the
design's own ``session`` DDL (Section 4.2) enumerates
``status IN ('open','closed','abandoned')`` but no design section assigns
any module the job of transitioning a session INTO ``abandoned`` — without
some such path that state is unreachable dead schema, and a session that
crashed without a clean close would block every future boot forever
(``resolve_open_session`` already refuses to guess between >1 open
session, and a fresh :func:`boot_session` call defaults to REUSING rather
than fighting an existing open session). M6 owns session lifecycle, so M6
supplies the one API that can reach it: :func:`abandon_session` and its
CLI action ``trialerror session abandon`` (not named in design Section 5.2's
CLI table — flagged here as a faithful-closest-reading addition, the same
class of decision M1's synthetic-PK TRIALERROR-DEV-NOTE and M3's over-cap-math
TRIALERROR-DEV-NOTE made for their own underspecified corners).

**TRIALERROR-DEV-NOTE (the "unread close checklist" refusal, design Section
5.2's ``session`` CLI row):** the design names this refusal condition once,
without defining what "the close checklist" IS. The closest concrete,
checkable reading available anywhere in the design/REQUIREMENTS text is
the user's inbox (``ops.inbox_item``, read at boot per REQUIREMENTS
Section 1.3's own "feed inbox" bullet) — a checklist of things the user
asked for that the orchestrator must acknowledge before ending the
session. :func:`close_session` therefore refuses while any inbox item is
unread, exactly as it refuses on dangling launches or a stale digest.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from trialerror.budget.gate import resolve_open_session
from trialerror.budget.pools import budget_status
from trialerror.events.api import append_event, read_inbox
from trialerror.jobs.ledger import list_jobs
from trialerror.law.service import PinVerifyResult, current_pin, diff_foreign, lookup_rulings, verify_pin
from trialerror.sessions.handoff import latest_handoff, resolve_handoffs_dir, write_handoff_with_supersession
from trialerror.stores import get, insert, update
from trialerror.stores.store import Store
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

__all__ = [
    "AccountResolution",
    "resolve_account_for_boot",
    "BootResult",
    "boot_session",
    "CloseReadiness",
    "evaluate_close_readiness",
    "CloseResult",
    "close_session",
    "abandon_session",
    "session_status",
]


# ---------------------------------------------------------------------------
# account resolution (F14: "mandatory --account when more than one account
# is registered; the single-account default otherwise")
# ---------------------------------------------------------------------------


@dataclass
class AccountResolution:
    ok: bool
    account_id: str | None
    code: str
    message: str
    accounts: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "account_id": self.account_id,
            "code": self.code,
            "message": self.message,
            "accounts": self.accounts,
        }


def _list_accounts(store: Store) -> list[dict]:
    rows = store.platform.execute("SELECT * FROM account ORDER BY created_ts ASC").fetchall()
    return [dict(r) for r in rows]


def resolve_account_for_boot(
    store: Store,
    *,
    account_id: str | None = None,
    create_account_label: str | None = None,
    now_ts: str | None = None,
) -> AccountResolution:
    """Design Section 4.3 / review F14. Never guesses across >1 registered
    account; never silently defaults when the caller was explicit.

    ``create_account_label`` is a bootstrap convenience: no design section
    assigns any CLI group ownership of ``account`` CRUD (it is absent from
    Section 5.2's CLI table entirely), yet F14's binding rule presupposes
    at least one account already exists. Without SOME creation path the
    very first ``trialerror session boot`` on a brand-new program could never
    succeed. TRIALERROR-DEV-NOTE: this is that minimal path — full account
    management remains unowned/TBD for a later module.
    """
    ts = now_ts or now()
    if account_id is not None:
        row = get(store, "account", pk_column="account_id", pk_value=account_id)
        if row is None:
            return AccountResolution(False, None, "unknown_account", f"no account {account_id!r} registered")
        return AccountResolution(True, account_id, "given", "account explicitly given via --account")

    if create_account_label is not None:
        new_account_id = new_id("ACC")
        insert(store, "account", {"account_id": new_account_id, "label": create_account_label, "created_ts": ts})
        return AccountResolution(
            True, new_account_id, "created", f"bootstrapped new account {new_account_id!r} ({create_account_label!r})"
        )

    accounts = _list_accounts(store)
    if not accounts:
        return AccountResolution(
            False,
            None,
            "no_accounts",
            "no accounts registered; pass --create-account <label> to bootstrap the first one, "
            "or --account <id> if one already exists elsewhere",
            accounts=accounts,
        )
    if len(accounts) == 1:
        return AccountResolution(
            True,
            accounts[0]["account_id"],
            "single_account_default",
            "single registered account used by default (F14)",
            accounts=accounts,
        )
    return AccountResolution(
        False,
        None,
        "account_required",
        f"{len(accounts)} accounts registered; --account is mandatory (F14) -- account attribution is never guessed",
        accounts=accounts,
    )


# ---------------------------------------------------------------------------
# shared launch/job/memory queries
# ---------------------------------------------------------------------------

_LIVE_STATES = ("PROVISIONAL", "RUNNING")


def _launches_for_session(store: Store, session_id: str, states: Sequence[str] = _LIVE_STATES) -> list[dict]:
    placeholders = ",".join("?" for _ in states)
    rows = store.platform.execute(
        f"SELECT * FROM launch WHERE session_id = ? AND state IN ({placeholders})",
        (session_id, *states),
    ).fetchall()
    return [dict(r) for r in rows]


def _orphaned_launches_for_account(store: Store, account_id: str, *, exclude_session_id: str | None = None) -> list[dict]:
    """PROVISIONAL/RUNNING launches under ``account_id`` NOT belonging to
    ``exclude_session_id`` (normally: the session boot just opened/reused)
    -- these are the crash-leftover bookings from a session that never
    closed cleanly (design Section 5.4 SessionStart row: "dangling
    launches" in the boot bundle)."""
    rows = store.platform.execute(
        "SELECT * FROM launch WHERE account_id = ? AND state IN ('PROVISIONAL','RUNNING')", (account_id,)
    ).fetchall()
    out = []
    for r in rows:
        row = dict(r)
        if exclude_session_id is not None and row["session_id"] == exclude_session_id:
            continue
        out.append(row)
    return out


def _active_jobs(store: Store) -> list[dict]:
    """M2 integration point: "list_jobs ... is your read surface for
    'background jobs still running' close warnings." Informational only
    (never a refusal) — a detached worker legitimately outlives the CC
    session that spawned it; that is the entire point of the jobs
    ledger (design Section 4.4)."""
    jobs: list[dict] = []
    for state in ("claimed", "running"):
        jobs.extend(list_jobs(store, state=state))
    return jobs


def _l0_memory_for_account(store: Store, account_id: str) -> list[dict]:
    """Design Section 5.4 boot bundle: "L0 memory index". M11 (the memory
    module) has not landed as of this build — this is a direct, minimal
    read over the M1-built ``memory_item`` table rather than a dependency
    on an unbuilt subsystem. TRIALERROR-DEV-NOTE for M11: once
    ``trialerror.memory`` ships its progressive-disclosure search, boot should
    switch to calling it instead of this direct query (unchanged shape:
    active L0 rows for this account)."""
    rows = store.ops.execute(
        "SELECT * FROM memory_item WHERE account_id = ? AND tier = 'L0' AND status = 'active' ORDER BY key",
        (account_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _most_recent_finished_session(store: Store, *, exclude_session_id: str | None = None) -> dict | None:
    rows = store.ops.execute(
        "SELECT * FROM session WHERE status IN ('closed','abandoned') AND session_id != ? "
        "ORDER BY opened_ts DESC LIMIT 1",
        (exclude_session_id or "",),
    ).fetchall()
    return dict(rows[0]) if rows else None


def _bundle_sha(bundle: Mapping[str, Any]) -> str:
    canonical = json.dumps(bundle, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# boot
# ---------------------------------------------------------------------------


@dataclass
class BootResult:
    ok: bool
    code: str
    message: str
    session_id: str | None = None
    account_id: str | None = None
    bundle: dict | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "code": self.code,
            "message": self.message,
            "session_id": self.session_id,
            "account_id": self.account_id,
            "bundle": self.bundle,
        }


def _build_bundle(
    store: Store,
    session_row: Mapping[str, Any],
    *,
    ts: str,
    pin_check: PinVerifyResult | None,
    config: Mapping[str, Any] | None = None,
) -> dict:
    session_id = session_row["session_id"]
    account_id = session_row["account_id"]

    prior = _most_recent_finished_session(store, exclude_session_id=session_id)
    first_session = prior is None
    foreign: list[dict] = []
    if session_row.get("boot_pin_version") is not None:
        # Law exists as of THIS boot; figure out what (if anything) this
        # account has not seen yet.
        if prior is not None and prior.get("boot_pin_version"):
            try:
                foreign = diff_foreign(store, prior["boot_pin_version"])
            except ValueError:
                foreign = []
        elif not first_session:
            # A prior session existed but booted before any ruling had
            # ever been appended (its own boot_pin_version is None) -- it
            # never saw ANY law, so everything currently active is new to
            # this account, not just what changed since some prior pin.
            foreign = lookup_rulings(store, status="active")

    inbox_items = read_inbox(store, session_id=session_id, mark_read=True)
    dangling = _orphaned_launches_for_account(store, account_id, exclude_session_id=session_id)
    budget = budget_status(store, account_id=account_id)
    memory_l0 = _l0_memory_for_account(store, account_id)
    active_jobs = _active_jobs(store)

    # FX-note (the import-design notes (internal, not in this export) Sec 5 knob #3): this used to be its own
    # independent literal (`store.program_root / "handoffs"`), duplicating
    # `trialerror.sessions.handoff.HANDOFFS_DIR_NAME` and able to drift from it
    # silently -- now both call the same resolver.
    handoffs_dir = resolve_handoffs_dir(store.program_root, config)
    latest_path = latest_handoff(handoffs_dir)
    handoff_markdown = latest_path.read_text(encoding="utf-8") if latest_path is not None else None

    return {
        "session_id": session_id,
        "account_id": account_id,
        "opened_ts": ts,
        "boot_pin_version": session_row.get("boot_pin_version"),
        "pin_status": pin_check.to_dict() if pin_check is not None else None,
        "first_session": first_session,
        "foreign_since_last": foreign,
        "dangling_launches": dangling,
        "inbox_unread_count": len(inbox_items),
        "inbox_items": inbox_items,
        "budget": budget,
        "memory_l0": memory_l0,
        "active_jobs": active_jobs,
        "latest_handoff_path": str(latest_path) if latest_path is not None else None,
        "latest_handoff_markdown": handoff_markdown,
    }


def boot_session(
    store: Store,
    *,
    account_id: str | None = None,
    create_account_label: str | None = None,
    queue: Sequence[Any] | None = None,
    reuse_open: bool = True,
    now_ts: str | None = None,
    config: Mapping[str, Any] | None = None,
) -> BootResult:
    """THE boot ritual. Design Section 4.3: "``session boot`` binds the
    session to an ``account_id``". REQUIREMENTS Section 1.3: "boot = a tool
    call that verifies pins ... replays deltas."

    Refusal ladder:

    1. A session is already open and ``reuse_open=False`` -> refused
       (``session_already_open``); with the default ``reuse_open=True``
       the existing session's CURRENT bundle is returned instead
       (idempotent-safe: a live SessionStart hook may fire more than once
       per Claude Code session, e.g. on ``/clear``).
    2. Account resolution fails (ambiguous / unknown / none registered,
       see :func:`resolve_account_for_boot`) -> refused.
    3. The law ledger's hash chain fails integrity verification (a
       tampered ledger) -> refused (``law_chain_tampered``) — "law-pin at
       boot: spawn gate + session boot verify" (design review Leg 5c). A
       program with NO rulings appended yet is not a failure (nothing to
       verify); pin freshness itself is not checked here (a session
       stamps its OWN current pin at the moment it boots, so it is
       current by construction — staleness is a MID-session concern the
       spawn gate and close both re-check).

    ``config`` (default ``None``, identical to every pre-existing caller's
    behavior) is the plain ``ProgramConfig.raw`` dict — passed through to
    the boot bundle's ``[paths].handoffs_dir`` lookup (the import-design notes (internal, not in this export)
    Sec 5 knob #3).
    """
    ts = now_ts or now()

    existing = resolve_open_session(store)
    if existing is not None:
        if not reuse_open:
            return BootResult(
                False,
                "session_already_open",
                f"session {existing['session_id']!r} is already open; close it (`trialerror session close`) "
                "or abandon it (`trialerror session abandon`) before booting a new one",
                session_id=existing["session_id"],
                account_id=existing["account_id"],
            )
        bundle = _build_bundle(store, existing, ts=ts, pin_check=None, config=config)
        return BootResult(
            True,
            "reused_open_session",
            f"session {existing['session_id']!r} already open; returning its current bundle",
            session_id=existing["session_id"],
            account_id=existing["account_id"],
            bundle=bundle,
        )

    acct = resolve_account_for_boot(store, account_id=account_id, create_account_label=create_account_label, now_ts=ts)
    if not acct.ok:
        return BootResult(False, acct.code, acct.message)

    new_pin = current_pin(store)
    pin_check: PinVerifyResult | None = None
    if new_pin is not None:
        pin_check = verify_pin(store, new_pin)
        if not pin_check.valid:
            return BootResult(
                False,
                "law_chain_tampered",
                f"law ledger failed integrity verification at boot: {pin_check.reason} "
                "(run `trialerror doctor --only law_chain_integrity` before booting)",
            )

    session_id = new_id("SESS")
    insert(
        store,
        "session",
        {
            "session_id": session_id,
            "account_id": acct.account_id,
            "opened_ts": ts,
            "status": "open",
            "boot_pin_version": new_pin,
            "queue": json.dumps(list(queue or []), ensure_ascii=False),
        },
    )
    session_row = get(store, "session", pk_column="session_id", pk_value=session_id)
    assert session_row is not None  # just inserted, same connection

    bundle = _build_bundle(store, session_row, ts=ts, pin_check=pin_check, config=config)
    sha = _bundle_sha(bundle)
    update(store, "session", pk_column="session_id", pk_value=session_id, changes={"boot_bundle_sha": sha})
    bundle["boot_bundle_sha"] = sha

    append_event(
        store,
        event_type="session_boot",
        session_id=session_id,
        payload={
            "account_id": acct.account_id,
            "account_resolution": acct.code,
            "boot_pin_version": new_pin,
            "first_session": bundle["first_session"],
            "dangling_launch_count": len(bundle["dangling_launches"]),
            "foreign_ruling_count": len(bundle["foreign_since_last"]),
            "inbox_unread_count": bundle["inbox_unread_count"],
        },
        ts=ts,
    )

    return BootResult(
        True, "booted", f"session {session_id!r} opened", session_id=session_id, account_id=acct.account_id, bundle=bundle
    )


# ---------------------------------------------------------------------------
# close readiness (shared by close_session AND the Stop hook's narrower check)
# ---------------------------------------------------------------------------


@dataclass
class CloseReadiness:
    ready: bool
    dangling_launches: list[dict]
    pin_check: PinVerifyResult | None
    problems: list[str]

    def to_dict(self) -> dict:
        return {
            "ready": self.ready,
            "dangling_launches": self.dangling_launches,
            "pin_check": self.pin_check.to_dict() if self.pin_check is not None else None,
            "problems": self.problems,
        }


def evaluate_close_readiness(store: Store, session_id: str) -> CloseReadiness:
    """The {dangling launches, stale pin} half of the close refusal ladder
    — design Section 5.4's Stop row scope EXACTLY ("if open session has
    dangling PROVISIONAL/RUNNING launches or a stale digest"); reused
    verbatim by :func:`close_session`, which layers hook-armed / inbox /
    course-check refusals on top (its own, WIDER scope per Section 5.2's
    ``session`` CLI row)."""
    session = get(store, "session", pk_column="session_id", pk_value=session_id)
    if session is None:
        raise ValueError(f"no such session: {session_id!r}")

    dangling = _launches_for_session(store, session_id)
    pin_check: PinVerifyResult | None = None
    if session.get("boot_pin_version"):
        pin_check = verify_pin(store, session["boot_pin_version"])

    problems: list[str] = []
    if dangling:
        problems.append(f"{len(dangling)} launch(es) still PROVISIONAL/RUNNING under this session")
    if pin_check is not None and not pin_check.valid:
        problems.append(f"law pin check failed: {pin_check.reason}")

    return CloseReadiness(ready=not problems, dangling_launches=dangling, pin_check=pin_check, problems=problems)


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


@dataclass
class CloseResult:
    ok: bool
    code: str
    message: str
    session_id: str | None = None
    close_report: dict | None = None
    handoff_path: str | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "code": self.code,
            "message": self.message,
            "session_id": self.session_id,
            "close_report": self.close_report,
            "handoff_path": self.handoff_path,
        }


def close_session(
    store: Store,
    *,
    course_check: Mapping[str, Any],
    session_id: str | None = None,
    notes: str | None = None,
    hook_alive_override_ruling_id: str | None = None,
    now_ts: str | None = None,
    config: Mapping[str, Any] | None = None,
) -> CloseResult:
    """THE close ritual. Design Section 5.2 ``session`` CLI row: "close
    REFUSES on dangling launches / stale digest / unread close checklist."
    REQUIREMENTS Section 1.3: "course-check (3 lines: rungs / build-vs-
    theory split / drift flag)" — required, per design Section 9.3:
    "course-check JSON field required at close."

    Refusal ladder (each one returns ``CloseResult(ok=False, code=...)``,
    never raises):

    1. no open session / wrong ``session_id`` -> ``not_open``.
    2. ``course_check`` missing/empty -> ``course_check_required``.
    3. zero ``hook_alive`` events recorded for this session -> refused
       (``hooks_disabled``) UNLESS ``hook_alive_override_ruling_id`` names
       a real ruling (design Section 5.4 SessionStart row: "close refuses
       with override-only path").
    4. dangling PROVISIONAL/RUNNING launches -> ``dangling_launches``.
    5. stale/tampered law pin -> ``stale_digest``.
    6. unread inbox items -> ``unread_checklist`` (see module TRIALERROR-DEV-NOTE).
    7. (FX-8, C-0064) some hook fired but subagent activity (a
       ``subagent_return`` event, or a launch this session's booking
       actually consumed -- state RUNNING/RECONCILED) is recorded with no
       ``spawn_gate`` hook_alive marker specifically -> refused
       (``hooks_partial``), same override-ruling-only escape as
       ``hooks_disabled`` (skipped when step 3 already used its own
       override -- that already excuses the whole hooks-liveness concern
       for this close). Checked LAST so the more specific/actionable
       ``dangling_launches`` refusal still fires first for a genuinely
       dangling RUNNING launch.

    On success: writes ``close_report``/``course_check`` onto the session
    row, status -> ``closed``, renders + writes a new suffixed handoff
    (superseding the prior one), and appends a ``session_close`` event.

    ``config`` (default ``None``, identical to every pre-existing caller's
    behavior) resolves ``[paths].handoffs_dir`` (the import-design notes (internal, not in this export) Sec 5
    knob #3) for the handoff this close writes.
    """
    ts = now_ts or now()

    if session_id is None:
        open_session = resolve_open_session(store)
        if open_session is None:
            return CloseResult(False, "not_open", "no OPEN session in this program's ops.db")
        session_id = open_session["session_id"]

    session = get(store, "session", pk_column="session_id", pk_value=session_id)
    if session is None:
        return CloseResult(False, "unknown_session", f"no such session: {session_id!r}", session_id=session_id)
    if session["status"] != "open":
        return CloseResult(
            False, "not_open", f"session {session_id!r} is {session['status']!r}, not open", session_id=session_id
        )

    if not course_check:
        return CloseResult(
            False,
            "course_check_required",
            "close requires a non-empty course_check (rungs / build-vs-theory split / drift flag) — "
            "design Section 9.3",
            session_id=session_id,
        )

    hook_alive_count = store.ops.execute(
        "SELECT COUNT(*) FROM event WHERE session_id = ? AND type = 'hook_alive'", (session_id,)
    ).fetchone()[0]
    override_used = False
    if hook_alive_count == 0:
        if hook_alive_override_ruling_id is None:
            return CloseResult(
                False,
                "hooks_disabled",
                "no hook_alive events recorded for this session -- hooks were disabled or never armed; "
                "close refuses without an override ruling citation (design Section 5.4)",
                session_id=session_id,
            )
        ruling = get(store, "ruling", pk_column="ruling_id", pk_value=hook_alive_override_ruling_id)
        if ruling is None:
            return CloseResult(
                False,
                "unknown_override_ruling",
                f"--override-ruling-id {hook_alive_override_ruling_id!r} does not name an existing ruling",
                session_id=session_id,
            )
        override_used = True

    readiness = evaluate_close_readiness(store, session_id)
    if readiness.dangling_launches:
        return CloseResult(
            False,
            "dangling_launches",
            f"{len(readiness.dangling_launches)} launch(es) still PROVISIONAL/RUNNING under this session; "
            "reconcile them (`trialerror budget reconcile`) before close",
            session_id=session_id,
            close_report={"dangling_launches": readiness.dangling_launches},
        )
    if readiness.pin_check is not None and not readiness.pin_check.valid:
        return CloseResult(
            False,
            "stale_digest",
            f"law pin check failed at close: {readiness.pin_check.reason}",
            session_id=session_id,
        )

    unread = read_inbox(store, session_id=session_id, mark_read=False)
    if unread:
        return CloseResult(
            False,
            "unread_checklist",
            f"{len(unread)} unread inbox item(s) -- read them (`trialerror inbox read`) before closing",
            session_id=session_id,
            close_report={"unread_inbox_count": len(unread)},
        )

    # FX-8 (C-0064 lens B EP-1 Bypass C): "some hook fired this session"
    # (the hooks_disabled check above) is coarser than "the SPAWN GATE
    # fired" -- SessionStart can be armed while PreToolUse:Task is off, in
    # which case spawns run ungated and leave no launch row at all (so the
    # dangling-launches check above never sees them either). Checked LAST
    # in the ladder (after the more specific/actionable dangling_launches
    # refusal, which must keep firing for a genuinely dangling RUNNING
    # launch regardless of this check -- design Section 12 M6 acceptance:
    # "close refused w/ dangling launch fixture"): a session with subagent
    # activity (a subagent_return event, or a launch this session's own
    # booking actually consumed to RUNNING/RECONCILED) but no spawn_gate
    # hook_alive marker is exactly that quiet corner -- refuse the same
    # override-ruling-only way hooks_disabled does.
    if not override_used:
        subagent_return_count = store.ops.execute(
            "SELECT COUNT(*) FROM event WHERE session_id = ? AND type = 'subagent_return'", (session_id,)
        ).fetchone()[0]
        consumed_launch_count = store.platform.execute(
            "SELECT COUNT(*) FROM launch WHERE session_id = ? AND state IN ('RUNNING', 'RECONCILED')",
            (session_id,),
        ).fetchone()[0]
        if subagent_return_count > 0 or consumed_launch_count > 0:
            spawn_gate_alive = False
            for row in store.ops.execute(
                "SELECT payload FROM event WHERE session_id = ? AND type = 'hook_alive'", (session_id,)
            ).fetchall():
                try:
                    if json.loads(row["payload"]).get("hook") == "spawn_gate":
                        spawn_gate_alive = True
                        break
                except (TypeError, ValueError):
                    continue
            if not spawn_gate_alive:
                if hook_alive_override_ruling_id is None:
                    return CloseResult(
                        False,
                        "hooks_partial",
                        f"{subagent_return_count} subagent_return event(s) and/or {consumed_launch_count} "
                        "consumed (RUNNING/RECONCILED) launch(es) recorded this session, but no "
                        "spawn_gate hook_alive marker -- spawns may have run ungated (SessionStart "
                        "armed, PreToolUse:Task off); close refuses without an override ruling "
                        "citation (design Section 5.4, FX-8)",
                        session_id=session_id,
                    )
                ruling = get(store, "ruling", pk_column="ruling_id", pk_value=hook_alive_override_ruling_id)
                if ruling is None:
                    return CloseResult(
                        False,
                        "unknown_override_ruling",
                        f"--override-ruling-id {hook_alive_override_ruling_id!r} does not name an existing ruling",
                        session_id=session_id,
                    )
                override_used = True

    launch_rows = store.platform.execute(
        "SELECT state, COUNT(*) AS c FROM launch WHERE session_id = ? GROUP BY state", (session_id,)
    ).fetchall()
    launch_counts = {r["state"]: r["c"] for r in launch_rows}
    event_rows = store.ops.execute(
        "SELECT type, COUNT(*) AS c FROM event WHERE session_id = ? GROUP BY type", (session_id,)
    ).fetchall()
    event_counts = {r["type"]: r["c"] for r in event_rows}
    active_jobs = _active_jobs(store)

    close_report: dict[str, Any] = {
        "closed_ts": ts,
        "launch_counts": launch_counts,
        "event_counts": event_counts,
        "active_jobs_at_close": active_jobs,
        "hook_alive_override_ruling_id": hook_alive_override_ruling_id if override_used else None,
        "final_pin": session["boot_pin_version"],
        "notes": notes,
    }

    update(
        store,
        "session",
        pk_column="session_id",
        pk_value=session_id,
        changes={
            "status": "closed",
            "closed_ts": ts,
            "close_report": json.dumps(close_report, ensure_ascii=False),
            "course_check": json.dumps(dict(course_check), ensure_ascii=False),
        },
    )
    closed_session = get(store, "session", pk_column="session_id", pk_value=session_id)
    assert closed_session is not None

    write_result = write_handoff_with_supersession(
        store, session=closed_session, close_report=close_report, course_check=dict(course_check), now_ts=ts,
        config=config,
    )
    # write_handoff_with_supersession enriches close_report with
    # handoff_filename internally; persist that SAME enriched dict back so
    # the DB row and the rendered file agree byte-for-byte (what makes
    # trialerror.sessions.handoff.rerender_handoff reproduce the original file).
    close_report = write_result.close_report
    update(
        store,
        "session",
        pk_column="session_id",
        pk_value=session_id,
        changes={"close_report": json.dumps(close_report, ensure_ascii=False)},
    )

    append_event(
        store,
        event_type="session_close",
        session_id=session_id,
        payload={
            "code": "closed",
            "handoff_filename": write_result.filename,
            "superseded_handoff": write_result.superseded_path,
        },
        ts=ts,
    )

    return CloseResult(
        True,
        "closed",
        f"session {session_id!r} closed",
        session_id=session_id,
        close_report=close_report,
        handoff_path=write_result.path,
    )


# ---------------------------------------------------------------------------
# abandon (TRIALERROR-DEV-NOTE in module docstring: reaches the design's own
# 'abandoned' status value, unowned by any other module)
# ---------------------------------------------------------------------------


def abandon_session(
    store: Store, *, session_id: str, reason: str | None = None, now_ts: str | None = None
) -> CloseResult:
    """Mark a crashed/never-closed session ``abandoned`` (design Section
    4.2 DDL: ``status IN ('open','closed','abandoned')``) so a future boot
    is not blocked forever by an open session nobody will ever close.
    Unlike :func:`close_session`, this does NOT render a handoff (an
    abandoned session has no course-check narrative to hand off) and does
    NOT check dangling launches or the pin — those launches simply remain
    visible as orphaned at the next boot (design Section 5.4: "abandonment
    is visible as an ``abandoned`` session + PROVISIONAL rows next boot",
    review Leg 5d)."""
    ts = now_ts or now()
    session = get(store, "session", pk_column="session_id", pk_value=session_id)
    if session is None:
        return CloseResult(False, "unknown_session", f"no such session: {session_id!r}", session_id=session_id)
    if session["status"] != "open":
        return CloseResult(
            False,
            "not_open",
            f"session {session_id!r} is {session['status']!r}, not open (only an OPEN session can be abandoned)",
            session_id=session_id,
        )

    close_report = {"abandoned": True, "reason": reason, "abandoned_ts": ts}
    update(
        store,
        "session",
        pk_column="session_id",
        pk_value=session_id,
        changes={"status": "abandoned", "closed_ts": ts, "close_report": json.dumps(close_report, ensure_ascii=False)},
    )
    append_event(store, event_type="session_abandon", session_id=session_id, payload={"reason": reason}, ts=ts)
    return CloseResult(
        True, "abandoned", f"session {session_id!r} marked abandoned", session_id=session_id, close_report=close_report
    )


# ---------------------------------------------------------------------------
# read-only status
# ---------------------------------------------------------------------------


def session_status(store: Store, *, session_id: str | None = None) -> dict:
    """Read-only snapshot — no side effects (inbox is PEEKED, never marked
    read; contrast :func:`boot_session`, which does mark it read as part
    of the boot ritual itself). Used by ``trialerror session status`` and
    reusable by hooks that want to check readiness without mutating
    anything."""
    if session_id is None:
        session = resolve_open_session(store)
        if session is None:
            return {"open": False}
    else:
        session = get(store, "session", pk_column="session_id", pk_value=session_id)
        if session is None:
            return {"open": False, "error": f"no such session: {session_id!r}"}

    is_open = session["status"] == "open"
    readiness = evaluate_close_readiness(store, session["session_id"]) if is_open else None
    unread = read_inbox(store, mark_read=False) if is_open else []
    hook_alive_count = store.ops.execute(
        "SELECT COUNT(*) FROM event WHERE session_id = ? AND type = 'hook_alive'", (session["session_id"],)
    ).fetchone()[0]

    return {
        "open": is_open,
        "session": session,
        "readiness": readiness.to_dict() if readiness is not None else None,
        "unread_inbox_count": len(unread),
        "hook_alive_count": hook_alive_count,
        "active_jobs": _active_jobs(store),
    }
