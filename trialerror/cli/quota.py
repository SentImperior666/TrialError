"""``trialerror quota`` -- design ``L4_quota-policy.md`` Section 2.5's CLI
surface over :mod:`trialerror.quota`: ``import``, ``rate fit``/``rate show``,
``forecast``, ``status``, ``month``, ``notify``.

Every table this group touches (``quota_capture``, ``quota_rate``,
``quota_notice``, plus a read of L3's ``unit``) lives in ``platform.db``, so
-- mirroring :mod:`trialerror.cli.units`'s own rationale verbatim -- this
group opens ONLY the platform connection, never a program's ops/knowledge/
jobs stores.

Registration rule (design Section 5.2): auto-discovered; adding this module
never touches ``trialerror/cli/__init__.py``."""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trialerror.stores import paths as store_paths
from trialerror.stores.connection import connect
from trialerror.stores.migrate import apply_migrations
from trialerror.stores.schema import platform as platform_schema
from trialerror.util.envelope import error_envelope, next_action, ok_envelope
from trialerror.util.timeutil import now as now_ts

GROUP_NAME = "quota"
HELP = "The exchange rate between work and quota, the monthly cap, the new-account guard (design F6 first slice)."


def _add_platform_root_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--platform-root", default=argparse.SUPPRESS,
        help="override the platform root (default: TRIALERROR_PLATFORM_ROOT or ~/.trialerror)",
    )


def _env_account_label() -> str:
    """Review S5: once the custodian sets ``TRIALERROR_ACCOUNT`` (design
    Section 5), every capture carries that label -- a plain ``quota rate
    fit``/``rate show``/``forecast``/``status``/``notify`` with no
    ``--account-label`` must fit/read THAT account, not silently the
    unlabelled ``""`` one (which, combined with B1, would then show a
    zero-window rate for an account nothing was ever fit against). Read at
    ``register()`` time (once per process, like every other argparse
    default), so a real invocation's own environment always wins."""
    return os.environ.get("TRIALERROR_ACCOUNT", "")


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    actions = parser.add_subparsers(dest="action", metavar="<action>")

    p_import = actions.add_parser("import", help="import a host's rate_limits.jsonl into quota_capture")
    _add_platform_root_arg(p_import)
    p_import.add_argument("--history", required=True, help="path to that host's rate_limits.jsonl")
    p_import.add_argument("--host", required=True, help="a label for this host (e.g. dev, sandbox)")
    p_import.set_defaults(handler=_run_import)

    p_rate = actions.add_parser("rate", help="fit or show the dollars<->window-points exchange rate")
    rate_sub = p_rate.add_subparsers(dest="rate_action", metavar="<action>")

    p_rate_fit = rate_sub.add_parser("fit", help="fit the rate from imported capture history")
    _add_platform_root_arg(p_rate_fit)
    p_rate_fit.add_argument("--account-label", default=_env_account_label(), help="default: TRIALERROR_ACCOUNT, else unlabelled")
    p_rate_fit.set_defaults(handler=_run_rate_fit)

    p_rate_show = rate_sub.add_parser("show", help="the latest fitted rate, in plain words")
    _add_platform_root_arg(p_rate_show)
    p_rate_show.add_argument("--account-label", default=_env_account_label())
    p_rate_show.set_defaults(handler=_run_rate_show)

    p_rate.set_defaults(handler=_run_rate_no_action)

    p_forecast = actions.add_parser("forecast", help="usd or tokens -> window points, from the fitted rate")
    _add_platform_root_arg(p_forecast)
    p_forecast.add_argument("--program-root", default=argparse.SUPPRESS, help="for [quota.prices] (only used by --tokens)")
    p_forecast.add_argument("--account-label", default=_env_account_label())
    p_forecast.add_argument("--window", default="five_hour", choices=["five_hour", "seven_day"])
    p_forecast.add_argument("--usd", type=float, default=None)
    p_forecast.add_argument("--tokens", default=None, metavar="MODEL:TYPE=N,...")
    p_forecast.add_argument("--current-pct", type=float, default=None, help="the latest capture's pct for --window, to report points remaining before 80/95")
    p_forecast.set_defaults(handler=_run_forecast)

    p_month = actions.add_parser("month", help="this month's spend against [quota.monthly].limit_usd")
    _add_platform_root_arg(p_month)
    p_month.add_argument("--program-root", default=argparse.SUPPRESS)
    p_month.add_argument("--account-label", default=None, help="default: [quota.monthly].account_label, else TRIALERROR_ACCOUNT")
    p_month.add_argument("--limit-usd", type=float, default=None, help="default: [quota.monthly].limit_usd")
    p_month.add_argument("--counts", default=None, choices=["all", "over_window"], help="default: [quota.monthly].counts")
    p_month.add_argument("--month-start-day", type=int, default=None)
    p_month.set_defaults(handler=_run_month)

    p_status = actions.add_parser("status", help="one screen: windows, account age, rate age, month spend, notices")
    _add_platform_root_arg(p_status)
    p_status.add_argument("--program-root", default=argparse.SUPPRESS)
    p_status.add_argument("--account-label", default=_env_account_label())
    p_status.add_argument("--quota-dir", default=None)
    p_status.set_defaults(handler=_run_status)

    p_notify = actions.add_parser("notify", help="record limit_hit flags and push one combined credit-risk alert (L9 part E)")
    _add_platform_root_arg(p_notify)
    p_notify.add_argument("--program-root", default=argparse.SUPPRESS, help="for [packet] notify_cmd, [quota] packet_dir")
    p_notify.add_argument("--account-label", default=_env_account_label())
    p_notify.add_argument("--quota-dir", default=None, help="for L9 part E's credit-risk reading (default: TRIALERROR_QUOTA_DIR or ~/.trialerror/quota)")
    p_notify.add_argument(
        "--host", default=None,
        help="N6 (fix check): this host's label for L9 part E's credit-risk reading (default: TRIALERROR_HOST, else 'local')",
    )
    p_notify.set_defaults(handler=_run_notify)

    parser.set_defaults(handler=_run_no_action)
    return parser


def _run_no_action(_args: argparse.Namespace) -> dict:
    return error_envelope(
        "quota", "no_action", "specify an action: import, rate, forecast, month, status, notify",
        next_actions=[next_action(["trialerror", "quota", "--help"], "list quota actions")],
    )


def _run_rate_no_action(_args: argparse.Namespace) -> dict:
    return error_envelope("quota rate", "no_action", "specify an action: fit, show")


# ---------------------------------------------------------------------------
# platform-only connection (mirrors trialerror.cli.units's own rationale)
# ---------------------------------------------------------------------------
def _open_platform(args: argparse.Namespace) -> sqlite3.Connection:
    override = getattr(args, "platform_root", None)
    platform_root = Path(override) if override else store_paths.platform_root()
    conn = connect(store_paths.platform_db_path(root=platform_root))
    apply_migrations(conn, platform_schema.MIGRATIONS)
    return conn


def _default_program_root(*, refuse_harness: bool = True) -> Path:
    """F5 (fix check, built in the second fix step): every default program
    root goes through :func:`find_program_root` (``TRIALERROR_PROGRAM_ROOT``,
    then a walk up from the current folder), so a `quota` command that OPENS
    A STORE (writes to it, or routes a push through it) gets L8 part F's
    ``program_root_is_harness`` refusal instead of silently doing so from
    the harness's own checkout -- the same fix :mod:`trialerror.cli.budget`
    needed (review S5) for the same reason.

    N-a (fix check, third fix step): F1's own exempt list names `quota`
    read-only. `status`/`month`/`forecast` only READ config (and, rarely,
    `month` appends a packet item once past 80% -- a bonus record, not this
    command's job); `notify` is the one verb that opens the packet/outbox
    store to route a real push. Callers for the former pass
    ``refuse_harness=False``; `notify` keeps the default."""
    from trialerror.util.config import find_program_root

    return find_program_root(refuse_harness=refuse_harness) or Path.cwd()


def _load_raw_config(args: argparse.Namespace) -> dict[str, Any] | None:
    program_root = Path(getattr(args, "program_root", None) or _default_program_root(refuse_harness=False))
    cfg_path = program_root / "trialerror.toml"
    if not cfg_path.is_file():
        return None
    from trialerror.util.config import ConfigError, load_config

    try:
        return load_config(cfg_path).raw
    except ConfigError:
        return None


#: Review S1: entrypoint values known to carry NO status line (design
#: Section 2.3 step 4's "a headless unit ... no status line"). The design
#: text names `cli`, but the review's real-transcript evidence points the
#: other way: in this Claude Code install, `cli` is the INTERACTIVE terminal
#: entrypoint (which DOES have a status line -- excluding it would drop the
#: very windows the rate is measured from), `-p`/SDK runs are `sdk-*`
#: (confirmed headless), and the desktop app's Code tab -- `claude-desktop`,
#: which has no status line -- was the one entrypoint present in
#: every recent transcript that the OLD predicate (`cli` OR `sdk-*`) never
#: matched at all. This is the short-term fix the review recommends; whether
#: `cli` itself should ALSO exclude, despite carrying a status line in every
#: sample checked, is an open question for the design owner -- the design's
#: own text still says `cli`.
_HEADLESS_ENTRYPOINT_SQL = "(entrypoint = 'claude-desktop' OR entrypoint LIKE 'sdk-%')"


def _headless_active_fn(conn: sqlite3.Connection):
    """design Section 2.3 step 4's "a headless unit ... active in the
    window ON EITHER HOST" exclusion.

    Review S1, two further fixes to the original query:
    - it required nothing of `first_ts`, and ``COALESCE(first_ts,'')`` made
      a unit with an unknown start (`first_ts IS NULL`) match every window
      ever (`'' <= end` is always true) -- one such unit excluded 10 of 12
      windows in the review's probe. Requiring `first_ts IS NOT NULL` means
      a unit this uncertain excludes nothing, rather than everything.
    - `last_ts IS NULL` (a session still open when scanned) is now treated
      as `last_ts = first_ts` -- a point in time, not open-ended forever --
      instead of matching every later window too.

    NOT fixed here, and not fixable from this one connection alone: `unit`
    holds only the rows scanned into THIS platform.db, which in practice
    today is DEV's own transcripts -- there is no cross-host pull for units
    the way `quota import --host sandbox` pulls the sandbox's capture
    history into the SAME file. "On either host" is therefore only as true
    as whichever hosts have actually had `trialerror units scan` run against
    this platform.db. Whether the other host's units were ever scanned is not
    checked here."""
    import time

    def _check(_five_resets: int, window_start_epoch: float, window_end_epoch: float) -> bool:
        start = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(window_start_epoch))
        end = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(window_end_epoch))
        row = conn.execute(
            f"SELECT 1 FROM unit WHERE {_HEADLESS_ENTRYPOINT_SQL} "
            "AND first_ts IS NOT NULL AND substr(first_ts,1,19) <= ? "
            "AND substr(COALESCE(last_ts, first_ts),1,19) >= ? LIMIT 1",
            (end, start),
        ).fetchone()
        return row is not None

    return _check


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------
def _run_import(args: argparse.Namespace) -> dict:
    from trialerror.quota.capture_import import import_history
    from trialerror.quota.rate import mixed_account_windows

    conn = _open_platform(args)
    try:
        report = import_history(conn, args.history, args.host)
        labels = [r[0] for r in conn.execute("SELECT DISTINCT account_label FROM quota_capture").fetchall()]
        mixed = {label: sorted(mixed_account_windows(conn, label)) for label in labels}
        report["mixed_account_windows"] = {k: v for k, v in mixed.items() if v}
        return ok_envelope("quota import", result=report)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# rate fit / show
# ---------------------------------------------------------------------------
def _run_rate_fit(args: argparse.Namespace) -> dict:
    from trialerror.quota.rate import fit_rate

    conn = _open_platform(args)
    try:
        result = fit_rate(
            conn, args.account_label, now_ts=now_ts(), headless_active=_headless_active_fn(conn),
        )
        return ok_envelope("quota rate fit", result=result)
    finally:
        conn.close()


def _latest_rate_row(conn: sqlite3.Connection, account_label: str, window: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM quota_rate WHERE account_label = ? AND window = ? ORDER BY id DESC LIMIT 1",
        (account_label, window),
    ).fetchone()
    return dict(row) if row else None


def _age_words(fitted_ts: str, now: datetime) -> str:
    try:
        fitted = datetime.fromisoformat(fitted_ts.replace("Z", "+00:00"))
    except ValueError:
        return "an unknown time"
    seconds = max(0.0, (now - fitted).total_seconds())
    if seconds < 3600:
        return f"{int(seconds // 60)} minutes"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} hours"
    return f"{seconds / 86400:.1f} days"


_RATE_STALE_AFTER_DAYS = 7


def _maybe_rate_stale_notice(conn: sqlite3.Connection, account_label: str, window: str, fitted_ts: str, now: datetime) -> None:
    try:
        fitted = datetime.fromisoformat(fitted_ts.replace("Z", "+00:00"))
    except ValueError:
        return
    if (now - fitted).total_seconds() < _RATE_STALE_AFTER_DAYS * 86400:
        return
    existing = conn.execute(
        "SELECT 1 FROM quota_notice WHERE account_label = ? AND kind = 'rate_stale' AND period = ? "
        "AND detail = ? LIMIT 1",
        (account_label, window, fitted_ts),
    ).fetchone()
    if existing:
        return
    conn.execute(
        "INSERT INTO quota_notice (account_label, kind, period, created_ts, detail) VALUES (?, 'rate_stale', ?, ?, ?)",
        (account_label, window, now_ts(), fitted_ts),
    )
    conn.commit()


def _format_exclusion_reasons(row: dict[str, Any]) -> str:
    import json as _json

    try:
        detail = _json.loads(row.get("detail") or "{}")
    except ValueError:
        detail = {}
    reasons = detail.get("exclusion_reasons") if isinstance(detail, dict) else None
    if reasons:
        return ", ".join(f"{reason} x{count}" for reason, count in sorted(reasons.items()))
    if isinstance(detail, dict) and detail.get("fallback_from"):
        return "the five-hour fallback also has no usable rate"
    return "reasons not recorded"


def _run_rate_show(args: argparse.Namespace) -> dict:
    conn = _open_platform(args)
    try:
        now = datetime.now(timezone.utc)
        out: dict[str, Any] = {}
        lines: list[str] = []
        for window in ("five_hour", "seven_day"):
            row = _latest_rate_row(conn, args.account_label, window)
            out[window] = row
            if row is None:
                lines.append(f"{window}: no fit yet -- run `trialerror quota rate fit`")
                continue
            _maybe_rate_stale_notice(conn, args.account_label, window, row["fitted_ts"], now)
            # B1: a fit with zero usable windows is never shown as a real
            # rate -- design's own words for it, verbatim.
            if row["method"] == "no_estimate":
                lines.append(
                    f"{window}: no usable windows yet ({row['excluded_windows']} excluded: "
                    f"{_format_exclusion_reasons(row)})."
                )
                continue
            ci = (
                # N7: the bootstrap interval undercovered slightly in a
                # 200-run check (83.5% observed against a nominal 90%) --
                # "roughly" says so instead of promising an exact 90%.
                f" (roughly 90% between {row['ci_low']:.3f} and {row['ci_high']:.3f})"
                if row["ci_low"] is not None and row["ci_high"] is not None
                else ""
            )
            lines.append(
                f"One dollar of work costs about {row['points_per_usd']:.3f} points of the {window} window{ci}, "
                f"from {row['n_windows']} windows; last fitted {_age_words(row['fitted_ts'], now)} ago."
            )
        out["text"] = "\n".join(lines)
        return ok_envelope("quota rate show", result=out)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# forecast
# ---------------------------------------------------------------------------
def _run_forecast(args: argparse.Namespace) -> dict:
    from trialerror.quota.forecast import (
        PriceMissingError,
        forecast_from_usd,
        parse_tokens_spec,
        remaining_points,
        tokens_to_usd,
    )

    if (args.usd is None) == (args.tokens is None):
        return error_envelope("quota forecast", "bad_input", "specify exactly one of --usd or --tokens")

    conn = _open_platform(args)
    try:
        rate_row = _latest_rate_row(conn, args.account_label, args.window)
        if rate_row is None:
            return error_envelope(
                "quota forecast", "no_rate", f"no fitted rate yet for account {args.account_label!r} / {args.window}; "
                "run `trialerror quota rate fit` first",
            )
        # B1: a fitted row with zero usable windows must refuse, never
        # silently answer "0 points" for a wave that in fact costs
        # something. n_windows/method are echoed either way, so a caller
        # can tell "never fit" (no_rate, above) from "fit, but empty".
        if rate_row.get("method") == "no_estimate":
            return error_envelope(
                "quota forecast", "rate_has_no_windows",
                f"the fitted rate for account {args.account_label!r} / {args.window} has no usable windows yet "
                f"({rate_row['excluded_windows']} excluded); import more capture history with cost, then refit",
                details={"n_windows": rate_row["n_windows"], "excluded_windows": rate_row["excluded_windows"], "method": rate_row["method"]},
            )
        if args.usd is not None:
            usd = args.usd
        else:
            config = _load_raw_config(args)
            try:
                entries = parse_tokens_spec(args.tokens)
                usd = tokens_to_usd(entries, config)
            except (ValueError, PriceMissingError) as exc:
                code = "no_price_for_model" if isinstance(exc, PriceMissingError) else "bad_tokens_spec"
                return error_envelope("quota forecast", code, str(exc))
        result = forecast_from_usd(usd, rate_row)
        result["remaining"] = remaining_points(args.current_pct)
        return ok_envelope("quota forecast", result=result)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# month
# ---------------------------------------------------------------------------
def _monthly_config(args: argparse.Namespace) -> dict[str, Any]:
    config = _load_raw_config(args)
    table = (config or {}).get("quota", {}).get("monthly", {}) if isinstance(config, dict) else {}
    table = table if isinstance(table, dict) else {}
    return {
        "account_label": (
            args.account_label
            if args.account_label is not None
            else (table.get("account_label") or _env_account_label())
        ),
        "limit_usd": args.limit_usd if args.limit_usd is not None else table.get("limit_usd"),
        "counts": args.counts if args.counts is not None else (table.get("counts") or "all"),
        "month_start_day": args.month_start_day if args.month_start_day is not None else int(table.get("month_start_day") or 1),
    }


def _run_month(args: argparse.Namespace) -> dict:
    from trialerror.quota.monthly import due_levels, month_period_label, month_start_ts, packet_item_payload, record_notices, spend_this_month

    cfg = _monthly_config(args)
    if not cfg["limit_usd"]:
        return ok_envelope(
            "quota month",
            result={"note": "no [quota.monthly].limit_usd configured; the monthly cap stays silent", "spend": None},
        )
    conn = _open_platform(args)
    try:
        now = datetime.now(timezone.utc)
        month_start = month_start_ts(now, cfg["month_start_day"])
        month_start_epoch = datetime.fromisoformat(month_start.replace("Z", "+00:00")).timestamp()
        period = month_period_label(now, cfg["month_start_day"])
        spend = spend_this_month(
            conn, cfg["account_label"], counts=cfg["counts"], month_start_epoch=month_start_epoch, now_epoch=now.timestamp(),
        )
        new_levels = record_notices(conn, cfg["account_label"], period, spend, cfg["limit_usd"], now_ts())
        result: dict[str, Any] = {
            "account_label": cfg["account_label"], "period": period, "counts": cfg["counts"],
            "spend_usd": spend, "limit_usd": cfg["limit_usd"], "pct": (spend / cfg["limit_usd"]) * 100.0,
            "new_notices": new_levels,
        }
        if any(row["level"] == 80 for row in new_levels):
            program_root = Path(getattr(args, "program_root", None) or _default_program_root(refuse_harness=False))
            cfg_path = program_root / "trialerror.toml"
            if cfg_path.is_file():
                from trialerror.packet.store import PacketError, add_item, packet_settings

                try:
                    settings = packet_settings(program_root)
                    item, warnings = add_item(settings, packet_item_payload(spend, cfg["limit_usd"]))
                    result["packet_item"] = {"id": item.get("id"), "warnings": warnings}
                except PacketError as exc:
                    result["packet_item_error"] = str(exc)
        return ok_envelope("quota month", result=result)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
def _run_status(args: argparse.Namespace) -> dict:
    from trialerror.budget.quota import quota_status
    from trialerror.quota.accounts import classify_account

    conn = _open_platform(args)
    try:
        now = datetime.now(timezone.utc)
        windows = quota_status(args.quota_dir)

        import json as _json

        # S5: resolve exactly as trialerror.budget.quota does (TRIALERROR_QUOTA_DIR
        # first) -- this used to skip straight to the home-directory default,
        # so on a host whose TRIALERROR_QUOTA_DIR points elsewhere than the home
        # directory (a container's mounted quota dir, say) it would look for
        # accounts.json in the wrong place
        # entirely, and any label read there would come back "new, unknown
        # date" instead of the account's real history.
        quota_dir = args.quota_dir or os.environ.get("TRIALERROR_QUOTA_DIR") or os.path.join(os.path.expanduser("~"), ".trialerror", "quota")
        accounts_path = Path(quota_dir) / "accounts.json"
        try:
            accounts_doc = _json.loads(accounts_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            accounts_doc = {}
        account = classify_account(accounts_doc, args.account_label, now=now)

        rates = {w: _latest_rate_row(conn, args.account_label, w) for w in ("five_hour", "seven_day")}

        cfg = _monthly_config(argparse.Namespace(account_label=args.account_label, limit_usd=None, counts=None, month_start_day=None, program_root=getattr(args, "program_root", None)))
        month_result = None
        if cfg["limit_usd"]:
            from trialerror.quota.monthly import month_period_label, month_start_ts, spend_this_month

            month_start = month_start_ts(now, cfg["month_start_day"])
            month_start_epoch = datetime.fromisoformat(month_start.replace("Z", "+00:00")).timestamp()
            spend = spend_this_month(conn, cfg["account_label"], counts=cfg["counts"], month_start_epoch=month_start_epoch, now_epoch=now.timestamp())
            month_result = {"spend_usd": spend, "limit_usd": cfg["limit_usd"], "pct": (spend / cfg["limit_usd"]) * 100.0, "period": month_period_label(now, cfg["month_start_day"])}

        open_notices = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM quota_notice WHERE account_label = ? AND sent_ts IS NULL ORDER BY id DESC LIMIT 20",
                (args.account_label,),
            ).fetchall()
        ]

        text = _status_text(windows, account, rates, month_result, open_notices, now)
        return ok_envelope(
            "quota status",
            result={"windows": windows, "account": account, "rate": rates, "month": month_result, "open_notices": open_notices, "text": text},
        )
    finally:
        conn.close()


def _status_text(
    windows: dict[str, Any],
    account: dict[str, Any],
    rates: dict[str, dict[str, Any] | None],
    month_result: dict[str, Any] | None,
    open_notices: list[dict[str, Any]],
    now: datetime,
) -> str:
    """N10: design Section 2.5 asks for "one screen of plain words"; ``rate
    show`` already has a ``text`` field, so ``status`` gets one too instead
    of leaving a caller to assemble one from the raw envelope."""
    lines: list[str] = []
    if windows.get("available"):
        parts = [
            f"{key} {win['used_percentage']:.0f}%"
            for key, win in (windows.get("windows") or {}).items()
            if isinstance(win, dict) and win.get("used_percentage") is not None
        ]
        age = windows.get("age_s")
        age_text = f", {age:.0f}s old" if isinstance(age, (int, float)) else ""
        lines.append("windows now: " + (", ".join(parts) if parts else "no window data") + age_text)
    else:
        lines.append("windows now: no capture yet")

    lines.append(f"account: {account.get('message')}")

    for window in ("five_hour", "seven_day"):
        row = rates.get(window)
        if row is None:
            lines.append(f"rate ({window}): no fit yet")
        elif row.get("method") == "no_estimate":
            lines.append(f"rate ({window}): no usable windows yet ({row['excluded_windows']} excluded)")
        else:
            lines.append(
                f"rate ({window}): about {row['points_per_usd']:.3f} points/dollar, "
                f"last fitted {_age_words(row['fitted_ts'], now)} ago"
            )

    if month_result is None:
        lines.append("month: no limit configured")
    else:
        lines.append(
            f"month: ${month_result['spend_usd']:.2f} of ${month_result['limit_usd']:.2f} "
            f"({month_result['pct']:.0f}%)"
        )

    lines.append(f"open notices: {len(open_notices)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# notify
# ---------------------------------------------------------------------------
def _notify_host_label(args: argparse.Namespace) -> str:
    """N6 (fix check): the literal "dev" named this machine in harness code,
    against trap 5 ("no host names in harness code. Hosts come from
    [quota.hosts]"). --host (default: TRIALERROR_HOST, else "local") names
    whichever host `quota notify` is running on -- the sandbox included, per
    S2's own smallest fix."""
    return getattr(args, "host", None) or os.environ.get("TRIALERROR_HOST") or "local"


def _local_credit_risk_reading(quota_dir: Path, account_label: str, now_epoch: float, host: str, rows_limit: int = 400):
    """L9 §3 Part E: this host's own reading, on L8's one-clock machinery
    (:mod:`trialerror.quota.reading`), built from its local capture history
    -- never the two-hourly ``quota_capture`` import, which would come too
    late (design: "not read from the two-hourly import").

    Deviation (noted in the round report): the design's full ``[quota.hosts]``
    reader -- both hosts, the sandbox's over ssh -- is L8 Part C's `quota
    admit` scope (a later wave, not yet built). Until it lands, `quota
    notify` reads only THIS host's own capture; a sandbox-only 100% reading
    would not be caught from here.
    """
    import json

    from trialerror.quota.reading import account_rows, parse_row, reading as compute_reading

    latest_path, history_path = quota_dir / "latest.json", quota_dir / "rate_limits.jsonl"
    raw_objs: list[Any] = []
    if history_path.is_file():
        try:
            lines = history_path.read_text(encoding="utf-8", errors="replace").splitlines()[-rows_limit:]
        except OSError:
            lines = []
        for line in lines:
            try:
                raw_objs.append(json.loads(line))
            except ValueError:
                continue
    if latest_path.is_file():
        try:
            raw_objs.append(json.loads(latest_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    parsed = [r for r in (parse_row(obj, host, now=now_epoch) for obj in raw_objs) if r is not None]
    if not parsed:
        return None
    scoped = account_rows(parsed, account_label, caller_host=host, now=now_epoch)
    return compute_reading(scoped, now_epoch, acct=account_label)


def _run_notify(args: argparse.Namespace) -> dict:
    from trialerror.hooks.probe_log import probes_dir
    from trialerror.quota.notify import LIMIT_HIT_FLAG_DIRNAME, check_credit_risk, process_limit_hit_flags
    from trialerror.util.config import load_config

    conn = _open_platform(args)
    try:
        program_root = Path(getattr(args, "program_root", None) or _default_program_root())
        cfg_path = program_root / "trialerror.toml"
        packet_settings_obj = None
        packet_dir_raw = None
        if cfg_path.is_file():
            from trialerror.packet.store import packet_settings

            try:
                cfg_raw = load_config(cfg_path).raw
            except Exception:
                cfg_raw = {}
            quota_cfg = cfg_raw.get("quota", {}) if isinstance(cfg_raw.get("quota", {}), dict) else {}
            packet_dir_raw = quota_cfg.get("packet_dir")
            try:
                packet_settings_obj = packet_settings(program_root)
            except Exception:
                packet_settings_obj = None

        notify_cmd = packet_settings_obj.notify_cmd if packet_settings_obj else None
        use_outbox = bool(packet_settings_obj and packet_settings_obj.outbox and not notify_cmd)
        period_month_str = datetime.now(timezone.utc).strftime("%Y-%m")

        reconcile_result: dict[str, list[str]] | None = None
        if use_outbox:
            from trialerror.packet.outbox import reconcile_receipts

            reconcile_result = reconcile_receipts(packet_settings_obj)

        def push(title: str, body: str) -> str | None:
            # E3: the same sender `packet push` uses -- notify_cmd first,
            # else the outbox (the sandbox's case: the container has no
            # sender and never sees its secret), else refuse.
            if notify_cmd:
                from trialerror.packet.build import notify_argv

                argv = notify_argv(notify_cmd, title, body)
                proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
                if proc.returncode != 0:
                    raise RuntimeError(f"notifier exited {proc.returncode}")
                return None
            if use_outbox:
                from trialerror.packet.outbox import queue_notification

                entry = queue_notification(
                    packet_settings_obj, kind="alert", packet_id=f"credit-risk-{period_month_str}",
                    title=title, body=body, trigger=None,
                )
                return entry["id"]
            raise RuntimeError("no sender configured")

        flags_dir = probes_dir() / LIMIT_HIT_FLAG_DIRNAME
        flags_report = process_limit_hit_flags(conn, flags_dir, args.account_label, now_ts())

        quota_dir = Path(
            getattr(args, "quota_dir", None) or os.environ.get("TRIALERROR_QUOTA_DIR")
            or os.path.join(os.path.expanduser("~"), ".trialerror", "quota")
        )
        now_dt = datetime.now(timezone.utc)
        local_reading = _local_credit_risk_reading(
            quota_dir, args.account_label, now_dt.timestamp(), _notify_host_label(args)
        )
        # E5: the flag trigger is judged in the same run, before the flags
        # are cleared above (process_limit_hit_flags already cleared them --
        # its own quota_notice rows are the lasting record; fresh_flags is
        # what check_credit_risk needs for its own trigger decision).
        fresh_flag_seen = bool(flags_report["fresh_flags"])
        credit = check_credit_risk(
            conn, args.account_label, local_reading, fresh_flag_seen, now_ts(), period_month_str, push,
            reconcile_result=reconcile_result,
        )
        if credit["packet_payloads"]:
            from trialerror.packet.store import PacketError, PacketSettings, add_item

            # E3 (amended): the programme's own packet store --
            # packet_settings(program_root) -- unless [quota] packet_dir
            # overrides it. S-b (fix check): this used to write ONLY when
            # packet_dir was set, so in the sandbox (no packet_dir configured)
            # the blocking item -- "the lasting record" -- was never written
            # at all, and never retried (it is built once, at first firing).
            settings = (
                PacketSettings(program_root=Path(packet_dir_raw), dir=Path(packet_dir_raw))
                if packet_dir_raw else packet_settings_obj
            )
            packet_items = []
            for payload in credit["packet_payloads"]:
                if settings is None:
                    packet_items.append({"error": "no program root resolves a packet store"})
                    continue
                try:
                    item, warnings = add_item(settings, payload)
                    packet_items.append({"id": item.get("id"), "warnings": warnings})
                except PacketError as exc:
                    packet_items.append({"error": str(exc)})
            credit["packet_items"] = packet_items

        report: dict[str, Any] = {
            "processed": flags_report["processed"],
            "backlog_cleared": flags_report["backlog_cleared"],
            "fresh_flags": flags_report["fresh"],
            "credit_risk": credit,
        }
        return ok_envelope("quota notify", result=report)
    finally:
        conn.close()
