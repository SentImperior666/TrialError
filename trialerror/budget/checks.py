"""Budget-subsystem doctor checks. Auto-discovered by
``trialerror.util.doctor.discover_and_register_checks`` exactly like M0's
``license_audit`` and M1's ``store_schema_version``/``xid_dangling``
(design Section 5.2 doctor row: "framework + license-audit in M0; each
module registers its own checks") - dropping this file is the entire
registration step.

Five checks. Four read platform.db (cross-program, so none of them needs
``DoctorContext.program_root``); the fifth reads the plan-quota capture on
disk. The first two:

- ``budget_dangling_launches``: PROVISIONAL/RUNNING bookings whose TTL has
  expired - the platform-wide, TTL-based cousin of the session-scoped
  dangling-launch check M6's ``session close``/Stop hook perform (design
  Section 5.4 Stop row); this one is visible from `trialerror doctor` without an
  open session at all, and catches bookings orphaned by a crashed session
  (never marked ``abandoned``) that M6's own-session check wouldn't see.
  What it does NOT claim is that an elapsed TTL means a crashed session: the
  TTL is a guess made at booking time, so the check splits the rows on
  session liveness where it can see ops.db and says which of the two
  readings it cannot distinguish where it cannot
  (:mod:`trialerror.budget.dangling`, shared with the dashboard card).
- ``budget_pool_overspend``: pools whose current (spent + committed) *
  billed_multiplier has crossed ``hard_pct`` of ``cap_tokens``.

A third, ``agent_model_matches_booking``, arrived with the spawn gate's
guard of the same name: over reconciled launches that recorded which model
they actually ran on, it reports any that ran below their booked class. It
is the only one of the three that can report ``fail`` -- a dangling booking
or an over-cap pool is a state to notice, while a crossed model floor is a
rule that was broken. A spawned model this program's ``[model_classes]``
table cannot place is a third outcome and reports ``warn``: unverifiable is
not the same finding as below-the-floor.

A fourth, ``reconcile_provenance`` (lane FB-3, D-FB-13 (d)), reports where
this program's settled token numbers came from: ``warn`` on any reconciled
launch whose ``reconcile_source`` is ``manual`` -- a number nobody can trace
-- with the ``transcript``/``event``/``estimate`` counts carried beside it so
a program moving from asserted to measured provenance can watch that ratio
move.

A fifth, ``quota_capture_stale`` (lane FB-3 item 8), is the only one that
reads no store at all: it reports the AGE of the statusLine plan-quota
capture against ``[budget] quota_max_age_s``. The capture is written on a
Claude Code UI tick, so it goes stale exactly while a session sits idle --
and a number that WAS true is the kind a reader trusts without checking its
age, so the age has to be reported by something that is not the reader. The
booking gate is what refuses on it.

Every one of these except ``agent_model_matches_booking`` reports ``warn``
(design's "visible-not-refused" pattern, Section 5.4 mid-flight-staleness
note) - a doctor check flags, it does not itself refuse anything; refusal is
the hook's job.
"""

from __future__ import annotations

import json

from trialerror.budget import dangling
from trialerror.stores import paths
from trialerror.stores.connection import connect
from trialerror.util.doctor import CheckResult, DoctorContext, register_check

#: How many offender rows a check's ``details`` may carry (fix pass V-5).
#: A doctor result is serialised into the dashboard's sidecar state file and
#: its doctor panel on every run, so a list that grows with a program's whole
#: history is a growing payload on a surface that is refreshed constantly.
#: The totals stay in ``counts``; the rows are a place to start looking.
_MAX_LISTED_ROWS = 20

__all__ = [
    "check_budget_dangling_launches",
    "check_budget_pool_overspend",
    "check_agent_model_matches_booking",
    "check_reconcile_provenance",
    "check_quota_capture_stale",
]


def _platform_db_path(ctx: DoctorContext) -> "object":
    # fix-accept (C-0064): honor an explicit ctx.platform_root (the
    # --platform-root CLI flag / an acceptance journey's own param) before
    # falling back to TRIALERROR_PLATFORM_ROOT/~/.trialerror -- this used to call
    # paths.platform_db_path() with no root at all, ignoring ctx entirely.
    return paths.platform_db_path(root=ctx.platform_root)


def _program_model_classes(ctx: DoctorContext) -> dict[str, str] | None:
    """The program's ``[model_classes]`` table, or ``None``.

    The spawn gate resolves a spawned model name WITH this table (see
    ``trialerror.hooks.spawn_gate._evaluate``), so the post-hoc check has to
    resolve it with the same table or the two disagree about the same name:
    the gate would allow a spawn through the escape hatch its own refusal
    message advertises, and this check would then report that launch as an
    offender (finding V-1). Absent config is not an error here -- platform.db
    is cross-program and a doctor run may have no program root at all -- so
    every way of not having a table lands on ``None``, which is exactly what
    ``classify_model`` takes for "defaults only"."""
    if ctx.program_root is None:
        return None
    from trialerror.util.config import ConfigError, load_config

    try:
        config = load_config(ctx.program_root / "trialerror.toml")
    except ConfigError:
        return None
    table = {str(k): str(v) for k, v in config.model_classes.items()}
    return table or None


def _liveness_readings(ctx: DoctorContext) -> tuple[set[str] | None, set[str], str | None]:
    """``(alive_session_ids, sessions_known_here, unavailable_reason)``.

    Bookings live in platform.db (cross-program), sessions and their
    ``hook_alive`` events in ops.db (per-program), so a doctor run with no
    program root -- a perfectly ordinary way to run this check -- has only
    half the evidence. ``None`` says so, and
    :func:`trialerror.budget.dangling.split_by_liveness` degrades to the
    single undifferentiated list this check has always reported. Same
    absent-config-is-not-an-error posture as
    :func:`_program_model_classes`.

    Three DIFFERENT absences produce that ``None`` -- no program root, no
    ops.db yet, an ops.db this process cannot read -- and the check used to
    report all three as the first (fix-accept, V-4). A run that DID get a
    program root was told something false about its own invocation, in the
    one item whose thesis is that a surface which knows something must say
    it. The reason is now returned with the reading.

    The second element is every session id this ops.db knows at all, open or
    closed: it is how the check tells "no evidence of life" from "this
    program is not where this launch's evidence lives" (V-5)."""
    if ctx.program_root is None:
        return None, set(), "this run has no program root, so no session/hook_alive evidence was read"
    ops_path = paths.ops_db_path(ctx.program_root)
    if not ops_path.exists():
        return None, set(), (
            "this program has no ops.db yet, so it has no sessions to read hook_alive evidence from"
        )
    conn = connect(ops_path, read_only=True)
    try:
        return dangling.sessions_with_hook_liveness(conn), dangling.sessions_known_here(conn), None
    except Exception as exc:  # noqa: BLE001 - a doctor check never fails over its own evidence
        return None, set(), (
            f"this program's ops.db could not be read for session/hook_alive evidence "
            f"({type(exc).__name__})"
        )
    finally:
        conn.close()


@register_check("budget_dangling_launches", category="budget")
def check_budget_dangling_launches(ctx: DoctorContext) -> CheckResult:
    """Live bookings whose TTL has elapsed -- and, where this run can tell,
    which of them are past TTL *with a session still alive behind them*.

    The old message called every one of them an orphan "likely left by a
    crashed session". The TTL cannot support that claim: it is a guess made
    at booking time, and an elapsed TTL is exactly as consistent with a
    launch still running under a TTL that was set too short. This check now
    reports what it knows and names what it cannot know
    (:func:`trialerror.budget.dangling.dangling_message`); the same two
    lists and the same sentence appear on the dashboard's budget card, which
    reads the same module, so the card and the doctor cannot disagree about
    the same launch.

    ``budget heartbeat --launch-id`` is the fix for the first reading (a
    live launch pushing its own booking's ``booked_ts`` forward);
    ``budget reconcile`` or ``session close``'s abandon path is the fix for
    the second."""
    path = _platform_db_path(ctx)
    if not path.exists():
        return CheckResult(
            name="budget_dangling_launches",
            category="budget",
            status="skip",
            message="platform.db not found (no account has booked anything yet)",
        )
    conn = connect(path, read_only=True)
    try:
        rows = dangling.past_ttl_rows(
            conn, columns=("launch_id", "account_id", "session_id", "state", "booked_ts", "booking_ttl_s")
        )
    finally:
        conn.close()

    alive_ids, known_here, unavailable_reason = _liveness_readings(ctx)
    offenders, alive = dangling.split_by_liveness(rows, alive_ids)
    # V-11 (FB-1b item 5): the word comes from the shared module now, so the
    # dashboard's budget card prints THIS severity rather than a reader
    # inferring one from the length of the offender list beside it.
    status = dangling.past_ttl_status(rows)
    details: dict = {"offenders": offenders, "past_ttl_session_alive": alive}
    if unavailable_reason is not None:
        details["liveness_evidence"] = f"unavailable: {unavailable_reason}"
    else:
        # V-5: a booking whose session this ops.db has never heard of was made
        # in another program (platform.db is shared, ops.db is not). It is in
        # `offenders` because THIS program has no evidence about it -- which is
        # not the same finding as "its session is gone", and the empty
        # `past_ttl_session_alive` list cannot say the difference on its own.
        foreign = dangling.foreign_session_rows(rows, known_here)
        if foreign:
            details["past_ttl_foreign_session"] = [r["launch_id"] for r in foreign]
            details["liveness_evidence"] = (
                f"partial: {len(foreign)} of these launch(es) were booked by a session this "
                "program's ops.db does not know (bookings are shared across programs, sessions are "
                "not), so this run has no liveness evidence either way about them -- run the check "
                "from the program that booked them to get one"
            )
        else:
            details["liveness_evidence"] = (
                f"read: this program's ops.db reports {len(alive_ids or ())} session(s) open and "
                "recording hook_alive events"
            )
    return CheckResult(
        name="budget_dangling_launches",
        category="budget",
        status=status,
        message=dangling.dangling_message(offenders, alive),
        details=details,
    )


@register_check("budget_pool_overspend", category="budget")
def check_budget_pool_overspend(ctx: DoctorContext) -> CheckResult:
    """Pools whose projected billed spend has crossed their own hard cap --
    judging only the pool a booking could actually land in.

    D-FB-14 (1). This used to evaluate EVERY row in ``budget_pool``, which on
    any account that has rolled a period means judging last week's pool too.
    A superseded pool cannot move: ``book_launch`` targets the pool with the
    latest ``period_start`` per (account_id, model_class), and a superseded
    pool's ``spent_visible_tokens`` is frozen at whatever it was when the new
    one was created. Reporting it as an offender asks an operator to fix a
    number nothing can change, every run, forever -- which is how a check
    stops being read. Superseded pools are still REPORTED, with their frozen
    numbers, in their own list: reconstructing a past period is a real
    question, and "not judged" is not "not shown".

    The pool each booking was judged against is on the launch row since
    platform-v2, so per-pool committed sums are exact rather than inferred
    from (account_id, model_class) -- see
    :func:`trialerror.budget.pools.committed_visible_tokens` for how a
    pre-v2 row (no ``pool_id``) is read.

    Every number here comes from
    :func:`trialerror.budget.pools.pool_report`, which is what ``budget
    status`` and ``budget pools`` print, so a pool this check names can be
    read from the CLI and the two cannot disagree (D-FB-14 (2))."""
    from trialerror.budget.pools import pool_report

    path = _platform_db_path(ctx)
    if not path.exists():
        return CheckResult(
            name="budget_pool_overspend",
            category="budget",
            status="skip",
            message="platform.db not found (no pools configured yet)",
        )
    conn = connect(path, read_only=True)
    try:
        report = pool_report(conn)
    finally:
        conn.close()

    judged = [entry for entry in report if entry["judged"]]
    superseded = [entry for entry in report if not entry["judged"]]
    offenders = [entry for entry in judged if entry["over_hard"]]

    status = "warn" if offenders else "pass"
    if offenders:
        named = "; ".join(
            f"{entry['pool_id']} ({entry['account_id']}/{entry['model_class']}, "
            f"{entry['period']} from {entry['period_start']}): "
            f"projected {entry['projected_billed_tokens']:.0f} billed tokens vs hard cap "
            f"{entry['hard_cap']:.0f}"
            for entry in offenders
        )
        message = f"{len(offenders)} current pool(s) over their hard cap -- {named}"
    else:
        message = f"no current pool over its hard cap ({len(judged)} judged)"
    if superseded:
        message += (
            f"; {len(superseded)} superseded pool(s) reported with their frozen numbers and not "
            "judged (nothing can book against them)"
        )

    return CheckResult(
        name="budget_pool_overspend",
        category="budget",
        status=status,
        message=message,
        details={"offenders": offenders, "judged": judged, "superseded": superseded},
    )


@register_check("agent_model_matches_booking", category="budget")
def check_agent_model_matches_booking(ctx: DoctorContext) -> CheckResult:
    """The post-hoc half of the spawn gate's guard of the same name: over
    every RECONCILED launch that recorded which model it actually ran on
    (``attrs.spawned_model``), did any of them run on a class below the one
    the booking claimed?

    The gate refuses a live mismatch at spawn time. This check answers the
    same question across a program's whole history, which is the question
    that matters after the fact -- a gate that was bypassed, disabled, or
    simply not yet installed when a launch ran leaves no other trace.

    ``fail``, not ``warn``, unlike this module's other two: a dangling
    booking or an over-cap pool is a state to notice, while "booked one
    class, ran on a cheaper one" is a floor that was crossed, and reporting
    it as a suggestion would be reporting it wrongly. Launches that recorded
    no ``spawned_model`` make no claim and are counted as unattested rather
    than judged -- the count is in the message, so silence about them is
    never mistaken for a clean bill.

    Two offender kinds, kept apart (verification finding V-1). A model that
    resolves BELOW the booked class crossed the floor: ``fail``. A model
    that resolves to no class at all crossed nothing -- it is a name this
    program never taught anyone. The gate resolves such a name through the
    program's own ``[model_classes]`` table and lets it through when the
    table places it, so a spawn the gate approved by exactly the escape
    hatch its refusal message advertises must not come back after the fact
    as an offender of a rule it did not break. This check therefore reads
    the same table from the same file, and what still will not resolve is
    reported as ``warn`` in its own words, naming the same one-line fix --
    never folded into the below-the-floor headline.
    """
    from trialerror.budget.policy import classify_model, meets_minimum

    model_classes = _program_model_classes(ctx)
    path = _platform_db_path(ctx)
    if not path.exists():
        return CheckResult(
            name="agent_model_matches_booking",
            category="budget",
            status="skip",
            message="platform.db not found (no account has booked anything yet)",
        )
    conn = connect(path, read_only=True)
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT launch_id, account_id, purpose, model_class, model, attrs FROM launch "
                "WHERE state = 'RECONCILED'"
            ).fetchall()
        ]
    finally:
        conn.close()

    if not rows:
        return CheckResult(
            name="agent_model_matches_booking",
            category="budget",
            status="skip",
            message="no reconciled launches on file",
        )

    offenders = []
    unclassified = []
    attested = 0
    for row in rows:
        try:
            attrs = json.loads(row["attrs"]) if row["attrs"] else {}
        except (TypeError, ValueError):
            attrs = {}
        spawned_model = attrs.get("spawned_model") if isinstance(attrs, dict) else None
        if not spawned_model:
            continue
        attested += 1
        spawned_class = classify_model(spawned_model, model_classes=model_classes)
        if spawned_class is None:
            unclassified.append(
                {
                    "launch_id": row["launch_id"], "purpose": row["purpose"],
                    "booked_model_class": row["model_class"], "spawned_model": spawned_model,
                    "spawned_class": None, "reason": "spawned model resolves to no model class",
                }
            )
            continue
        if not meets_minimum(spawned_class, row["model_class"]):
            offenders.append(
                {
                    "launch_id": row["launch_id"], "purpose": row["purpose"],
                    "booked_model_class": row["model_class"], "spawned_model": spawned_model,
                    "spawned_class": spawned_class, "reason": "spawned below the booked class",
                }
            )

    if not attested:
        return CheckResult(
            name="agent_model_matches_booking",
            category="budget",
            status="skip",
            message=f"no reconciled launch recorded a spawned_model ({len(rows)} reconciled, 0 attested)",
        )

    unresolved_note = (
        f"; {len(unclassified)} more ran on a model no [model_classes] entry resolves"
        if unclassified
        else ""
    )
    if offenders:
        status = "fail"
        message = (
            f"{len(offenders)} reconciled launch(es) ran on a model below the class their "
            f"booking claimed{unresolved_note}"
        )
    elif unclassified:
        status = "warn"
        message = (
            f"{len(unclassified)} reconciled launch(es) ran on a model this program cannot "
            "classify (no floor shown to be crossed; name them in [model_classes] in "
            "trialerror.toml, the same table the spawn gate reads)"
        )
    else:
        status = "pass"
        message = (
            f"every attested launch ran at or above its booked class "
            f"({attested} of {len(rows)} reconciled launch(es) recorded a spawned_model)"
        )
    return CheckResult(
        name="agent_model_matches_booking",
        category="budget",
        status=status,
        message=message,
        details={
            "offenders": offenders,
            "unclassified": unclassified,
            "attested": attested,
            "reconciled": len(rows),
        },
    )


@register_check("reconcile_provenance", category="budget")
def check_reconcile_provenance(ctx: DoctorContext) -> CheckResult:
    """Where did this program's settled token numbers come from?

    D-FB-13 (d). ``launch.actual_tokens`` is the number every cap check,
    every calibration and every weekly reading is ultimately built on, and
    until platform-v2 there was exactly one way to put it there: a person
    typing ``--actual-tokens``. ``reconcile_source`` recorded which label
    that person chose, which is not the same as recording where the number
    came from -- ``transcript``, ``estimate`` and ``manual`` are all
    caller-asserted, and nothing could tell them apart from a measurement.

    ``budget reconcile --from-event`` now reads the host's own ``usage``
    object off the launch's ``subagent_return`` event and records
    ``reconcile_source = 'event'``, a value :func:`trialerror.budget.pools.
    reconcile_launch` refuses from any other caller. That makes the split
    real, and this check is what reports it:

    - **warn** when any reconciled launch reads ``manual`` -- a number
      nobody can trace. Not ``fail``: on a host that sends no usage, or for
      a launch spawned by a tool that fires no hooks (the Workflow tool),
      ``--actual-tokens`` IS the documented path, and failing a program for
      using it would teach an operator to ignore the check. What the warn
      says is "these are the numbers with no evidence behind them", which
      is a thing to know before trusting a multiplier derived from them.
    - the ``transcript`` / ``event`` / ``estimate`` counts ride the details
      either way, at INFO level in the message: a program moving from
      asserted to measured provenance can watch that ratio move, which is
      the only way anybody finds out whether the hook is actually firing.

    Launches reconciled before platform-v2 carry no ``reconcile_source`` at
    all (the column is nullable). They are counted as ``unrecorded`` and
    named in their own line rather than folded into ``manual``: "settled
    before this program recorded provenance" and "settled by hand" are
    different facts, and only the second one has a fix.
    """
    path = _platform_db_path(ctx)
    if not path.exists():
        return CheckResult(
            name="reconcile_provenance",
            category="budget",
            status="skip",
            message="platform.db not found (no account has reconciled anything yet)",
        )
    conn = connect(path, read_only=True)
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT launch_id, account_id, purpose, actual_tokens, reconciled_ts, reconcile_source "
                "FROM launch WHERE state = 'RECONCILED' ORDER BY reconciled_ts DESC"
            ).fetchall()
        ]
    finally:
        conn.close()

    if not rows:
        return CheckResult(
            name="reconcile_provenance",
            category="budget",
            status="skip",
            message="no reconciled launches on file",
        )

    counts: dict[str, int] = {}
    for row in rows:
        source = row["reconcile_source"] or "unrecorded"
        counts[source] = counts.get(source, 0) + 1
    manual = [row for row in rows if row["reconcile_source"] == "manual"]
    unrecorded = [row for row in rows if not row["reconcile_source"]]

    tally = ", ".join(f"{count} {source}" for source, count in sorted(counts.items()))
    if manual:
        status = "warn"
        message = (
            f"{len(manual)} of {len(rows)} reconciled launch(es) carry hand-asserted actuals "
            f"(reconcile_source=manual) -- numbers with no recorded measurement behind them "
            f"({tally}). `budget reconcile --from-event` records the host's own usage where the "
            "PostToolUse hook captured one"
        )
        if len(manual) > _MAX_LISTED_ROWS:
            message += (
                f"; the {_MAX_LISTED_ROWS} most recently settled are listed in the details, the "
                "rest are in the count"
            )
    else:
        status = "pass"
        message = f"no reconciled launch carries hand-asserted actuals ({tally})"
    if unrecorded:
        message += (
            f"; {len(unrecorded)} more were settled before this program recorded provenance at all "
            "(reconcile_source is null on a pre-platform-v2 row)"
        )

    return CheckResult(
        name="reconcile_provenance",
        category="budget",
        status=status,
        message=message,
        details={
            "counts": counts,
            "reconciled": len(rows),
            # The offender list is the manual one: the other counts are a
            # reading, not a finding, and a details block that listed every
            # measured launch as well would bury the ones worth looking at.
            #
            # Fix pass V-5: and it is the MOST RECENT few of them, not all of
            # them. Every other budget offender list is an anomaly set that
            # stays small; this one is most of a program's past -- on a real
            # store, 1,839 hand-reconciled launches made a 393 KB envelope,
            # which `trialerror.dashboard.doctor_run.run_doctor_and_persist`
            # then serialises into the sidecar state file and the doctor
            # panel on every run, growing with history. The number that
            # matters is in `counts`; the rows are a sample to start from.
            "manual": manual[:_MAX_LISTED_ROWS],
            "manual_total": len(manual),
            "manual_listed": min(len(manual), _MAX_LISTED_ROWS),
            "manual_truncated": len(manual) > _MAX_LISTED_ROWS,
            "unrecorded": [row["launch_id"] for row in unrecorded[:_MAX_LISTED_ROWS]],
            "unrecorded_total": len(unrecorded),
            "measured": counts.get("event", 0),
            "asserted": sum(counts.get(k, 0) for k in ("manual", "transcript", "estimate")),
        },
    )


@register_check("quota_capture_stale", category="budget")
def check_quota_capture_stale(ctx: DoctorContext) -> CheckResult:
    """Is the plan-quota capture fresh enough to size a booking against?

    Lane FB-3 item 8, custodian observation (b) (2026-09-15). The capture is
    written by the statusLine script, which Claude Code runs on a UI tick --
    so the reading goes stale precisely when the orchestrator is idle, which
    is also when it is most likely to be consulted before booking a pool. A
    number that WAS true is the kind a reader trusts without checking its
    age, so the age has to be reported by something that is not the reader.

    ``warn``, never ``fail``: the feed is optional (screenshots remain the
    ground truth, design Section 4.3) and a stale reading is a state to
    notice, not a rule that was broken. The booking gate is what refuses --
    ``budget book`` / the ``book_launch`` MCP tool, overridable with
    ``--allow-stale-quota`` / ``allow_stale_quota``, recorded on the launch.

    ``skip`` when nothing has ever been captured: a program that has not
    wired the statusLine has no reading to be out of date, and reporting
    that absence as staleness would make an optional feed look broken. The
    message names the wiring anyway, because "nothing captured" is worth
    seeing once.

    The freshness bar is ``[budget] quota_max_age_s`` from this program's
    ``trialerror.toml`` (default 900 s). With no program root there is no
    config to read and the default applies -- said out loud in the details
    rather than left as an assumption."""
    from trialerror.budget.quota import (
        quota_status,
        resolve_max_age_s,
        stale_capture_message,
        staleness,
    )

    config = None
    config_source = "default (this run has no program root, so no [budget] table was read)"
    if ctx.program_root is not None:
        from trialerror.util.config import ConfigError, load_config

        try:
            config = load_config(ctx.program_root / "trialerror.toml").raw
            config_source = "this program's trialerror.toml"
        except ConfigError:
            config_source = "default (no readable trialerror.toml)"

    max_age = resolve_max_age_s(config)
    status = quota_status(fresh_within_s=max_age)
    reading = staleness(status, max_age_s=max_age)
    details = {
        "standing": reading["standing"],
        "age_s": reading["age_s"],
        "max_age_s": max_age,
        "captured_ts": reading.get("captured_ts"),
        "bar_read_from": config_source,
    }

    if reading["standing"] == "absent":
        return CheckResult(
            name="quota_capture_stale",
            category="budget",
            status="skip",
            message=(
                "no plan-quota capture on file -- wire the statusLine per USER_SETUP.md, or rely on "
                "screenshot snapshots (which remain the ground truth either way)"
            ),
            details=details,
        )
    if reading["stale"]:
        return CheckResult(
            name="quota_capture_stale",
            category="budget",
            status="warn",
            message=stale_capture_message(reading) + " -- `budget book` refuses against it unless "
            "--allow-stale-quota is passed",
            details=details,
        )
    age = reading["age_s"]
    return CheckResult(
        name="quota_capture_stale",
        category="budget",
        status="pass",
        message=(
            f"the plan-quota capture is {age:.0f}s old, within the {max_age}s bar"
            if isinstance(age, (int, float))
            else f"the plan-quota capture is within the {max_age}s bar"
        ),
        details=details,
    )
