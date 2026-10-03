"""The audit of a transcript archive (``trialerror archive audit``).

Eight checks, each ``pass``, ``warn`` or ``fail`` with one plain-words
sentence, written to ``<dest>/audits/AUDIT_<host>_<date>.md`` (one screen, first
line "clean" or "N problems, the first is ...") and ``.json``. No model is
involved and nothing here reads message text: the only fields ever taken from
a transcript are its ``timestamp`` values (the gap probe) and a ``version``
string (the missing-transcript check).

A *fail* is something the operator should hear about (the packet builder reads
the latest audit's json for them); a *warn* is a fact to watch.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import random
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from trialerror.archive import store
from trialerror.archive.store import ArchiveError, object_path, open_index, parse_ts, utc_iso

__all__ = ["run_audit", "latest_audit", "MAX_RUN_GAP_HOURS", "MIN_RETENTION_DAYS", "MIN_FREE_BYTES"]

MAX_RUN_GAP_HOURS = 6
MIN_RETENTION_DAYS = 365
MIN_FREE_BYTES = 10 * 1000 ** 3
DEFAULT_PERIOD_DAYS = 14
_VERSION_SCAN_LINES = 200

PASS, WARN, FAIL = "pass", "warn", "fail"


def _check(n: int, name: str, status: str, sentence: str, **details: Any) -> dict[str, Any]:
    return {"n": n, "name": name, "status": status, "sentence": sentence, "details": details}


def _hours(delta: timedelta) -> float:
    return round(delta.total_seconds() / 3600, 1)


# ------------------------------------------------------------- previous audits


def _audit_files(dest: Path, host: str) -> list[Path]:
    return sorted((dest / "audits").glob(f"AUDIT_{host}_*.json"))


def latest_audit(dest: str | Path, host: str | None = None) -> dict[str, Any] | None:
    """The newest audit json in ``dest`` (for ``host``, or any host), or ``None``."""
    audits = Path(dest) / "audits"
    if not audits.is_dir():
        return None
    pattern = f"AUDIT_{host}_*.json" if host else "AUDIT_*.json"
    best: tuple[str, dict[str, Any]] | None = None
    for path in audits.glob(pattern):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        stamp = str(data.get("generated_ts", ""))
        if best is None or stamp > best[0]:
            best = (stamp, data)
    return best[1] if best else None


def _previous(dest: Path, host: str, before: str) -> dict[str, Any] | None:
    """The newest audit for ``host`` from a day before ``before``'s day. An earlier audit of the
    same day is not "the previous audit": trends, growth and the cadence count distinct days."""
    best: tuple[str, dict[str, Any]] | None = None
    for path in _audit_files(dest, host):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        stamp = str(data.get("generated_ts", ""))
        if stamp[:10] < before[:10] and (best is None or stamp > best[0]):
            best = (stamp, data)
    return best[1] if best else None


# ------------------------------------------------------------------ the checks


def _check_runs(db: sqlite3.Connection, host: str, period_start: datetime, now: datetime) -> dict[str, Any]:
    times = [
        parse_ts(r["started_ts"])
        for r in db.execute("SELECT started_ts FROM run WHERE host = ? ORDER BY started_ts", (host,))
    ]
    if not times:
        return _check(1, "Runs", FAIL, f"the archive has never run on host {host}")
    window_start = max(period_start, times[0])
    prev = [t for t in times if t < window_start]
    seq = ([prev[-1]] if prev else []) + [t for t in times if t >= window_start]
    limit = timedelta(hours=MAX_RUN_GAP_HOURS)
    gaps = [(a, b) for a, b in zip(seq, seq[1:]) if b - a > limit]
    tail = now - seq[-1]
    biggest = max([b - a for a, b in gaps] + ([tail] if tail > limit else []), default=timedelta(0))
    unfinished = db.execute(
        "SELECT COUNT(*) AS n FROM run WHERE host = ? AND finished_ts IS NULL AND started_ts < ?",
        (host, utc_iso(now - timedelta(hours=1))),
    ).fetchone()["n"]
    if gaps or tail > limit:
        return _check(
            1,
            "Runs",
            FAIL,
            f"the archive went {_hours(biggest)} hours without running (the limit is {MAX_RUN_GAP_HOURS}); "
            f"the last run was {_hours(tail)} hours ago",
            gaps=len(gaps) + (1 if tail > limit else 0),
            last_run=utc_iso(seq[-1]),
        )
    if unfinished:
        return _check(
            1, "Runs", WARN, f"{unfinished} earlier run(s) never recorded a finish (a crash or a kill)", unfinished=unfinished
        )
    return _check(1, "Runs", PASS, f"{len(seq)} runs in the period; the last was {_hours(tail)} hours ago", runs=len(seq))


def _latest_finished_scope(db: sqlite3.Connection, host: str) -> list[str] | None:
    """B6.4: the host's latest FINISHED run's scope (a list of globs), or
    ``None`` for "everything" -- either no scope was recorded (an
    unscoped run, or an index from before B6), or nothing has finished."""
    row = db.execute(
        "SELECT scope FROM run WHERE host = ? AND finished_ts IS NOT NULL ORDER BY id DESC LIMIT 1", (host,)
    ).fetchone()
    if row is None or not row["scope"]:
        return None
    try:
        parsed = json.loads(row["scope"])
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, list) and parsed else None


def _scope_changed_on(db: sqlite3.Connection, host: str, period_start: datetime) -> str | None:
    """B6.4: the ISO date scope first differed from the previous finished
    run's, among finished runs at or after ``period_start`` -- ``None`` when
    it never changed in the period."""
    rows = db.execute(
        "SELECT started_ts, scope FROM run WHERE host = ? AND finished_ts IS NOT NULL AND started_ts >= ? "
        "ORDER BY id",
        (host, utc_iso(period_start)),
    ).fetchall()
    prev, prev_set = None, False
    for row in rows:
        if prev_set and row["scope"] != prev:
            return row["started_ts"][:10]
        prev, prev_set = row["scope"], True
    return None


def _check_coverage(db: sqlite3.Connection, host: str, src: Path | None, period_start: datetime | None = None) -> dict[str, Any]:
    if src is None:
        return _check(2, "Coverage", WARN, "not checked: no --src was given, so the source folder was not rescanned")
    if not src.is_dir():
        return _check(2, "Coverage", FAIL, f"the source folder {src} does not exist, so coverage cannot be shown")
    scope = _latest_finished_scope(db, host)
    last = db.execute("SELECT MAX(started_ts) AS t FROM run WHERE host = ?", (host,)).fetchone()["t"]
    last_epoch = parse_ts(last).timestamp() if last else None
    known = {
        r["rel_path"]: (r["size"], r["mtime_ns"])
        for r in db.execute("SELECT rel_path, size, mtime_ns FROM path_state WHERE host = ?", (host,))
    }
    missing: list[str] = []
    never: list[str] = []  # files the archive cannot copy: over the size limit, or not readable
    newer = 0
    scanned = 0
    project_folders = 0
    if scope:
        try:
            project_folders = sum(
                1 for p in src.iterdir() if p.is_dir() and store.scope_matches(p.name, scope)
            )
        except OSError:
            project_folders = 0
    for rel, _path, st, skip in store.walk_source(src, scope):
        if skip == "secret":
            continue
        if skip is not None or st is None:
            never.append(f"{rel} (could not be read)")
            continue
        if st.st_size > store.MAX_FILE_BYTES:
            never.append(f"{rel} (over the {store.MAX_FILE_BYTES // 1024 ** 3} GB limit)")
            continue
        scanned += 1
        if known.get(rel) == (st.st_size, st.st_mtime_ns):
            continue
        # A file written after the last run started is not yet due: a live session keeps
        # writing between runs. Only a file that was already at rest is a coverage gap.
        if last_epoch is not None and st.st_mtime_ns / 1e9 > last_epoch:
            newer += 1
        else:
            missing.append(rel)
    scope_note = ""
    out_of_scope_note = ""
    if scope:
        try:
            all_top = [p.name for p in src.iterdir() if p.is_dir()]
        except OSError:
            all_top = []
        out_of_scope = len(all_top) - project_folders
        scope_note = f" (scope: {', '.join(scope)})"
        if out_of_scope > 0:
            out_of_scope_note = (
                f"; {out_of_scope} project folder{'s' if out_of_scope != 1 else ''} on the host "
                "are outside the scope and are not archived"
            )
    changed_on = _scope_changed_on(db, host, period_start) if period_start is not None else None
    changed_note = f"; the scope changed on {changed_on}" if changed_on else ""
    if missing or never:
        parts = []
        if missing:
            parts.append(
                f"{len(missing)} of {scanned} files were already at rest when the last run started, "
                f"yet the archive holds a different or no version (first: {missing[0]})"
            )
        if never:
            parts.append(
                f"{len(never)} file(s) can never be archived and will be lost when the host clears them: "
                + "; ".join(never[:5])
            )
        return _check(
            2,
            "Coverage",
            FAIL,
            "; ".join(parts) + out_of_scope_note + changed_note,
            missing=missing[:20],
            missing_total=len(missing),
            never_archived=never[:20],
            never_archived_total=len(never),
            scope=scope,
        )
    note = f"; {newer} changed since the last run started (expected, the next run takes them)" if newer else ""
    scope_words = f"in the {project_folders} project folder{'s' if project_folders != 1 else ''} in scope" if scope else ""
    return _check(
        2,
        "Coverage",
        PASS,
        f"all {scanned} files{(' ' + scope_words) if scope_words else ''} are covered{scope_note}{note}"
        f"{out_of_scope_note}{changed_note}",
        scanned=scanned,
        newer=newer,
        scope=scope,
    )


def _session_version(dest: Path, db: sqlite3.Connection, host: str, project: str, session: str, cache: dict) -> str:
    key = (project, session)
    if key in cache:
        return cache[key]
    version = "unknown"
    row = db.execute(
        "SELECT latest_sha256 FROM path_state WHERE host = ? AND rel_path = ?", (host, f"{project}/{session}.jsonl")
    ).fetchone()
    if row is not None:
        path = object_path(dest, row["latest_sha256"])
        try:
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
                for i, line in enumerate(handle):
                    if i >= _VERSION_SCAN_LINES:
                        break
                    try:
                        found = json.loads(line).get("version")
                    except (ValueError, AttributeError):
                        continue
                    if isinstance(found, str) and found:
                        version = found
                        break
        except (OSError, EOFError):
            pass
    cache[key] = version
    return version


def _check_missing_transcripts(
    dest: Path, db: sqlite3.Connection, host: str, previous: dict[str, Any] | None, scope: list[str] | None = None
) -> dict[str, Any]:
    """B6.4: ``scope`` (the host's latest finished run's scope) counts only
    paths in the current scope -- an out-of-scope project's own subagent
    records were never re-checked by this run and are not this run's
    concern."""
    paths = {r["rel_path"] for r in db.execute("SELECT rel_path FROM path_state WHERE host = ?", (host,))}
    metas = [
        r["rel_path"]
        for r in db.execute(
            "SELECT rel_path FROM path_state WHERE host = ? AND kind = 'subagent_meta' AND gone_ts IS NULL", (host,)
        )
        if scope is None or store.scope_matches(r["rel_path"], scope)
    ]
    by_group: dict[str, dict[str, int]] = {}
    total = 0
    cache: dict = {}
    for rel in sorted(metas):
        if rel[: -len(".meta.json")] + ".jsonl" in paths:
            continue
        parts = rel.split("/")
        project = parts[0]
        session = parts[1] if len(parts) > 2 else ""
        version = _session_version(dest, db, host, project, session, cache)
        by_group.setdefault(project, {}).setdefault(version, 0)
        by_group[project][version] += 1
        total += 1
    prev_total = (previous or {}).get("missing_total")
    trend = ""
    if isinstance(prev_total, int):
        delta = total - prev_total
        trend = f"; {'up' if delta > 0 else 'down' if delta < 0 else 'unchanged'}" + (
            f" {abs(delta)}" if delta else ""
        ) + " since the last audit"
    if total == 0:
        return _check(3, "Missing transcripts", PASS, "every subagent record has its transcript file", missing_total=0)
    per_project = ", ".join(f"{p} {sum(v.values())}" for p, v in sorted(by_group.items(), key=lambda kv: -sum(kv[1].values())))
    return _check(
        3,
        "Missing transcripts",
        WARN,
        f"{total} subagent records have no transcript file ({per_project}){trend}; the archive cannot copy what was never written",
        missing_total=total,
        by_project=by_group,
    )


def _window(text: str) -> tuple[datetime, datetime] | None:
    if not isinstance(text, str) or ".." not in text:
        return None
    a, _, b = text.partition("..")
    try:
        start = parse_ts(a.strip())
        end = parse_ts(b.strip())
    except ArchiveError:
        return None
    if len(b.strip()) <= 10:  # a bare date: the whole day
        end = end + timedelta(days=1)
    return start, end


def _timestamps_in(path: Path, window: tuple[datetime, datetime]) -> dict[str, Any]:
    """First and last ``timestamp`` of a transcript object and how many entries fall
    in the window. Only that one key is kept from each line."""
    first = last = None
    inside = 0
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                stamp = json.loads(line).get("timestamp")
            except (ValueError, AttributeError):
                continue
            if not isinstance(stamp, str):
                continue
            first = first or stamp
            last = stamp
            try:
                when = parse_ts(stamp)
            except ArchiveError:
                continue
            if window[0] <= when < window[1]:
                inside += 1
    return {"first": first, "last": last, "in_window": inside}


def _probe_gap(dest: Path, db: sqlite3.Connection, entry: dict[str, Any]) -> dict[str, Any] | None:
    """Evidence for an open gap that names a ``project`` and a ``window`` of the
    form ``YYYY-MM-DD..YYYY-MM-DD``: which archived transcripts of that project
    hold entries in the window. Timestamps only."""
    window = _window(entry.get("window", ""))
    project = entry.get("project")
    if window is None or not isinstance(project, str) or not project:
        return None
    files = []
    for row in db.execute(
        "SELECT host, rel_path, latest_sha256 FROM path_state WHERE kind IN ('main', 'subagent') "
        "AND rel_path LIKE ? ORDER BY host, rel_path",
        (f"%{project}%/%",),
    ):
        if project not in row["rel_path"].split("/")[0]:
            continue
        obj = object_path(dest, row["latest_sha256"])
        if not obj.is_file():
            continue
        try:
            span = _timestamps_in(obj, window)
        except (OSError, EOFError):
            continue
        files.append({"host": row["host"], "rel_path": row["rel_path"], **span})
    with_entries = [f for f in files if f["in_window"] > 0]
    return {"files_checked": len(files), "files_with_entries_in_window": with_entries[:20]}


def _check_gaps(dest: Path, db: sqlite3.Connection, host: str, gap_file: Path | None, now: datetime) -> dict[str, Any]:
    path = gap_file or (dest / "gaps.json")
    if not path.is_file():
        return _check(4, "Known gaps", PASS, "no gap file, so no known gaps are open")
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            raise ValueError("the top level is not a list")
    except (OSError, ValueError) as exc:
        return _check(4, "Known gaps", FAIL, f"the gap file {path} cannot be read: {exc}")
    mine = [e for e in entries if isinstance(e, dict) and str(e.get("host", host)).lower() == host.lower()]
    open_ = [e for e in mine if not str(e.get("explanation") or "").strip()]
    if not open_:
        return _check(4, "Known gaps", PASS, f"all {len(mine)} known gap(s) have an explanation", gaps=len(mine))
    described = []
    probes: dict[str, Any] = {}
    for entry in open_:
        try:
            age = (now - parse_ts(str(entry.get("opened_ts", "")))).days
            age_text = f"open {age} days"
        except ArchiveError:
            age_text = "open, age unknown"
        described.append(f"{entry.get('id', '?')}: {entry.get('what', '?')} ({age_text})")
        probe = _probe_gap(dest, db, entry)
        if probe is not None:
            probes[str(entry.get("id", "?"))] = probe
    return _check(
        4,
        "Known gaps",
        WARN,
        f"{len(open_)} known gap(s) still without an explanation: " + "; ".join(described),
        open=len(open_),
        evidence=probes,
    )


def _sha_of_gz(path: Path) -> str:
    hasher = hashlib.sha256()
    with gzip.open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _check_integrity(dest: Path, db: sqlite3.Connection, sample: int, rng: random.Random) -> dict[str, Any]:
    live = [r["sha256"] for r in db.execute("SELECT sha256 FROM object WHERE deleted_ts IS NULL")]
    absent = [sha for sha in live if not object_path(dest, sha).is_file()]
    referenced = {
        r["sha"]
        for r in db.execute("SELECT sha256 AS sha FROM snapshot UNION SELECT latest_sha256 AS sha FROM path_state")
    }
    known = {r["sha256"]: r["deleted_ts"] for r in db.execute("SELECT sha256, deleted_ts FROM object")}
    unindexed = [sha for sha in referenced if sha not in known]
    picked = rng.sample(live, min(sample, len(live))) if live else []
    bad = []
    for sha in picked:
        path = object_path(dest, sha)
        if sha in absent:
            continue
        try:
            if _sha_of_gz(path) != sha:
                bad.append(sha)
        except (OSError, EOFError):
            bad.append(sha)
    if absent or bad or unindexed:
        parts = []
        if bad:
            parts.append(f"{len(bad)} sampled object(s) no longer match their own hash")
        if absent:
            parts.append(f"{len(absent)} object file(s) are missing")
        if unindexed:
            parts.append(f"{len(unindexed)} recorded version(s) have no object row")
        return _check(
            5, "Integrity", FAIL, "; ".join(parts), corrupted=bad[:20], missing_files=absent[:20], unindexed=unindexed[:20]
        )
    return _check(
        5,
        "Integrity",
        PASS,
        f"{len(picked)} of {len(live)} stored objects re-hashed correctly and every recorded version has its file",
        sampled=len(picked),
        objects=len(live),
    )


def _check_retention(
    db: sqlite3.Connection, host: str, settings: Path, previous: dict[str, Any] | None
) -> dict[str, Any]:
    days: Any = None
    absent = False
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
        days = data.get("cleanupPeriodDays") if isinstance(data, dict) else None
        readable = True
    except FileNotFoundError:
        readable, absent = True, True  # no settings file: the host's default clean-up period applies
    except (OSError, ValueError):
        readable = False
    since = (previous or {}).get("generated_ts")
    gone_rows = db.execute(
        "SELECT p.rel_path, o.sha256 AS obj, o.deleted_ts FROM path_state p "
        "LEFT JOIN object o ON o.sha256 = p.latest_sha256 WHERE p.host = ? AND p.gone_ts IS NOT NULL "
        + ("AND p.gone_ts > ?" if since else ""),
        (host, since) if since else (host,),
    ).fetchall()
    unarchived = [r["rel_path"] for r in gone_rows if r["obj"] is None or r["deleted_ts"] is not None]
    if not readable:
        return _check(
            6, "Retention", WARN, f"the settings file {settings} cannot be read, so the host's clean-up period is unknown",
            gone_since_last_audit=len(gone_rows),
        )
    if unarchived:
        return _check(
            6, "Retention", FAIL,
            f"{len(unarchived)} file(s) disappeared from the host without an archived copy (first: {unarchived[0]})",
            unarchived=unarchived[:20],
        )
    if absent:
        return _check(
            6, "Retention", FAIL,
            f"there is no settings file at {settings}, so the host's clean-up period is not set and Claude Code "
            "deletes transcripts after its default of 30 days",
            gone_since_last_audit=len(gone_rows),
        )
    if not isinstance(days, (int, float)) or isinstance(days, bool):
        return _check(
            6, "Retention", FAIL,
            "the host's clean-up period is not set, so Claude Code deletes transcripts after its default of 30 days",
            gone_since_last_audit=len(gone_rows),
        )
    if days < MIN_RETENTION_DAYS:
        return _check(
            6, "Retention", FAIL,
            f"the host deletes transcripts after {int(days)} days; the limit is {MIN_RETENTION_DAYS} or more",
            cleanupPeriodDays=days, gone_since_last_audit=len(gone_rows),
        )
    return _check(
        6, "Retention", PASS,
        f"the host keeps transcripts for {int(days)} days; {len(gone_rows)} file(s) went from the host since the last audit, all archived",
        cleanupPeriodDays=days, gone_since_last_audit=len(gone_rows),
    )


def _check_privacy(dest: Path) -> dict[str, Any]:
    repo = store.git_working_tree_above(dest)
    if repo is not None:
        return _check(7, "Privacy", FAIL, f"the archive sits inside the git working tree {repo}; it must live outside every repository")
    if os.name != "nt":
        mode = dest.stat().st_mode & 0o777
        if mode & 0o077:
            return _check(7, "Privacy", FAIL, f"the archive folder is readable by other users (mode {mode:03o}); it must be 700", mode=mode)
        return _check(7, "Privacy", PASS, "the archive is outside every repository and only its owner can read it")
    try:
        inside_profile = Path.home().resolve() in dest.resolve().parents
    except OSError:
        inside_profile = False
    if inside_profile:
        return _check(7, "Privacy", PASS, "the archive is outside every repository, under the user profile (its access rules are the profile's default: the owner only)")
    return _check(7, "Privacy", WARN, "the archive is outside every repository, but it is not under the user profile, so its access rules were not verified")


def _dir_bytes(dest: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(dest):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _check_space(dest: Path, previous: dict[str, Any] | None, min_free: int) -> tuple[dict[str, Any], int]:
    size = _dir_bytes(dest)
    free = shutil.disk_usage(dest).free
    before = (previous or {}).get("archive_bytes")
    growth = f"; grew {round((size - before) / 1e6, 1)} MB since the last audit" if isinstance(before, int) else ""
    sentence = f"the archive holds {round(size / 1e6, 1)} MB{growth}; {round(free / 1e9, 1)} GB of disk is free"
    if free < min_free:
        return _check(8, "Space", WARN, sentence + f" (below the {round(min_free / 1e9)} GB floor)", archive_bytes=size, free_bytes=free), size
    return _check(8, "Space", PASS, sentence, archive_bytes=size, free_bytes=free), size


# ------------------------------------------------------------------- the audit


def _cadence(dest: Path, host: str, clean_now: bool, today: str) -> str:
    """Every two weeks; monthly after two consecutive clean audits (on distinct days);
    back to two weeks after any failure."""
    by_day: dict[str, dict[str, Any]] = {}
    for path in _audit_files(dest, host):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        stamp = str(data.get("generated_ts", ""))
        if stamp[:10] < today and (stamp[:10] not in by_day or stamp > str(by_day[stamp[:10]].get("generated_ts", ""))):
            by_day[stamp[:10]] = data  # the day's last audit stands for the day
    flags = [bool(by_day[day].get("clean")) for day in sorted(by_day)]
    if not clean_now:
        return "every two weeks (this audit failed)"
    run = 1
    for flag in reversed(flags):
        if not flag:
            break
        run += 1
    return "monthly (two clean audits in a row)" if run >= 2 else "every two weeks"


def _render(host: str, date: str, headline: str, checks: list[dict[str, Any]], cadence: str) -> str:
    lines = [f"# Archive audit: {host}, {date}", "", headline, ""]
    warns = [c for c in checks if c["status"] == WARN]
    if warns and headline == "clean":
        lines += [f"({len(warns)} warning(s) to watch, listed below.)", ""]
    lines += ["| # | Check | Result | In plain words |", "|---|---|---|---|"]
    for c in checks:
        lines.append(f"| {c['n']} | {c['name']} | {c['status'].upper()} | {c['sentence'].replace('|', '/')} |")
    lines += ["", f"Next audit: {cadence}."]
    return "\n".join(lines) + "\n"


def run_audit(
    dest: str | Path,
    host: str,
    *,
    src: str | Path | None = None,
    sample: int = 50,
    gap_file: str | Path | None = None,
    settings_file: str | Path | None = None,
    now: datetime | None = None,
    min_free_bytes: int = MIN_FREE_BYTES,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """Run the eight checks on the archive at ``dest`` for ``host`` and file the
    report under ``<dest>/audits/``."""
    dest_p = Path(dest)
    now = now or datetime.now(timezone.utc)
    src_p = Path(src) if src else None
    if settings_file:
        settings = Path(settings_file)
    elif src_p is not None:
        settings = src_p.parent / "settings.json"
    else:
        settings = Path.home() / ".claude" / "settings.json"
    db = open_index(dest_p, create=False)
    try:
        now_iso = utc_iso(now)
        previous = _previous(dest_p, host, now_iso)
        period_start = parse_ts(previous["generated_ts"]) if previous and previous.get("generated_ts") else now - timedelta(days=DEFAULT_PERIOD_DAYS)
        scope = _latest_finished_scope(db, host)
        checks = [
            _check_runs(db, host, period_start, now),
            _check_coverage(db, host, src_p, period_start),
            _check_missing_transcripts(dest_p, db, host, previous, scope),
            _check_gaps(dest_p, db, host, Path(gap_file) if gap_file else None, now),
            _check_integrity(dest_p, db, sample, rng or random.Random()),
            _check_retention(db, host, settings, previous),
            _check_privacy(dest_p),
        ]
        space, size = _check_space(dest_p, previous, min_free_bytes)
        checks.append(space)
    finally:
        db.close()
    fails = [c for c in checks if c["status"] == FAIL]
    clean = not fails
    headline = "clean" if clean else f"{len(fails)} problem{'s' if len(fails) != 1 else ''}, the first is {fails[0]['sentence']}"
    date = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
    cadence = _cadence(dest_p, host, clean, date)
    missing_total = next((c["details"].get("missing_total") for c in checks if c["n"] == 3), None)
    report = {
        "host": host,
        "generated_ts": now_iso,
        "period_start": utc_iso(period_start),
        "clean": clean,
        "headline": headline,
        "cadence": cadence,
        "archive_bytes": size,
        "missing_total": missing_total,
        "checks": checks,
    }
    audits = dest_p / "audits"
    audits.mkdir(parents=True, exist_ok=True)
    stem, number = f"AUDIT_{host}_{date}", 1
    while (audits / f"{stem}.json").exists() or (audits / f"{stem}.md").exists():
        number += 1  # a second audit on the same day is kept beside the first, never over it
        stem = f"AUDIT_{host}_{date}_{number}"
    md_path = audits / f"{stem}.md"
    json_path = audits / f"{stem}.json"
    md_path.write_text(_render(host, date, headline, checks, cadence), encoding="utf-8")
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    report["report_md"] = str(md_path)
    report["report_json"] = str(json_path)
    return report
