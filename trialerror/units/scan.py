"""``scan(projects_root, host=..., store=..., statusline_dir=None)`` -- the
one function ``trialerror units scan`` runs. Design Section 2.2 (``scan.py``).

Walks every project slug directory under ``projects_root``, classifies each
file (:mod:`trialerror.units.paths`), reads only the ones that changed since
the last scan (trap 6: never reload an unchanged multi-GB transcript), sums
each unit's usage under the fixed rule (:mod:`trialerror.units.reader`),
dedupes message ids a resumed/forked session copied from an earlier file,
backfills subagent/workflow-agent metadata from ``.meta.json``, and applies
the Remote Control pattern from the statusLine's own ``sessions.json``
(:mod:`trialerror.units.statusline``) when a ``statusline_dir`` is given.

Read-only on the transcripts themselves (never written, moved, or deleted);
all writes land in the caller's ``store.platform`` ``unit``/``unit_msg``
tables via the validated write API.
"""

from __future__ import annotations

import datetime
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trialerror.stores import get, insert, update
from trialerror.stores.errors import ValidationError
from trialerror.stores.store import Store
from trialerror.units.paths import Classified, FileKind, classify
from trialerror.units.reader import TranscriptSummary, read_transcript
from trialerror.units.statusline import read_sessions
from trialerror.util.timeutil import now, parse

__all__ = ["ScanReport", "scan"]

EXTRACTOR_VERSION = "units-1"

#: design Section 2.2 step 4: "more than 1 h after".
_RC_GAP_SECONDS = 3600


@dataclass
class ScanReport:
    files_read: int = 0
    files_skipped: int = 0
    files_ignored: int = 0
    units_by_source: dict[str, int] = field(default_factory=dict)
    messages_deduplicated: int = 0
    remote_control_sessions: list[str] = field(default_factory=list)
    duration_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "files_read": self.files_read,
            "files_skipped": self.files_skipped,
            "files_ignored": self.files_ignored,
            "units_by_source": dict(self.units_by_source),
            "messages_deduplicated": self.messages_deduplicated,
            "remote_control_sessions": list(self.remote_control_sessions),
            "duration_s": self.duration_s,
        }


@dataclass(frozen=True)
class _FileEntry:
    project_slug: str
    rel_path: str
    classified: Classified
    abs_path: Path
    size: int
    mtime_ns: int


def _walk_classified(projects_root: Path) -> list[_FileEntry]:
    out: list[_FileEntry] = []
    if not projects_root.is_dir():
        return out
    for slug_dir in sorted(p for p in projects_root.iterdir() if p.is_dir()):
        for path in sorted(slug_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(slug_dir).as_posix()
            classified = classify(rel)
            if classified is None:
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            out.append(
                _FileEntry(
                    project_slug=slug_dir.name,
                    rel_path=rel,
                    classified=classified,
                    abs_path=path,
                    size=st.st_size,
                    mtime_ns=st.st_mtime_ns,
                )
            )
    return out


def _unit_key(host: str, session_id: str, agent_id: str | None) -> str:
    return f"{host}/{session_id}/{agent_id or '-'}"


def _existing_units_by_key(store: Store, host: str) -> dict[str, dict[str, Any]]:
    rows = store.platform.execute("SELECT * FROM unit WHERE host = ?", (host,)).fetchall()
    return {row["unit_key"]: dict(row) for row in rows}


def _upsert_unit(store: Store, unit_key: str, existing: dict[str, Any] | None, row: dict[str, Any]) -> None:
    if existing is None:
        try:
            insert(store, "unit", row)
        except ValidationError:
            # a concurrent scan (or a race within this one) created it first
            update(store, "unit", pk_column="unit_key", pk_value=unit_key, changes={k: v for k, v in row.items() if k != "unit_key"})
    else:
        update(store, "unit", pk_column="unit_key", pk_value=unit_key, changes={k: v for k, v in row.items() if k != "unit_key"})


def _mtime_iso(path: Path) -> str:
    dt = datetime.datetime.fromtimestamp(path.stat().st_mtime, tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _read_meta(path: Path) -> dict[str, Any]:
    """Tolerant meta.json read -- a corrupt or unreadable meta file is
    reported as empty, never a scan-aborting error (same tolerance
    :mod:`trialerror.units.reader` gives a transcript line)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _bump(report_units_by_source: dict[str, int], source: str) -> None:
    report_units_by_source[source] = report_units_by_source.get(source, 0) + 1


def scan(
    projects_root: Path | str,
    *,
    host: str,
    store: Store,
    statusline_dir: Path | str | None = None,
    dry_run: bool = False,
) -> ScanReport:
    t0 = time.perf_counter()
    projects_root = Path(projects_root)
    report = ScanReport()

    entries = _walk_classified(projects_root)
    jsonl_entries = [e for e in entries if e.classified.kind in (FileKind.MAIN, FileKind.SUBAGENT_JSONL)]
    meta_entries = [e for e in entries if e.classified.kind in (FileKind.SUBAGENT_META, FileKind.WORKFLOW_AGENT_META)]
    # Workflow journal files are classified (never silently "ignored" as an
    # unrecognized path) but not summed for usage: their record shape is
    # unverified (an open item in the design's list of unknowns) and they are not one unit's
    # transcript in the sense the fixed rule assumes -- a documented
    # deviation from a literal reading of the design.
    report.files_ignored = len(entries) - len(jsonl_entries) - len(meta_entries)

    existing = _existing_units_by_key(store, host)

    # ---- step 1/2: main + subagent jsonl, changed files only ----------
    to_read = []
    for e in jsonl_entries:
        agent_id = e.classified.agent_id if e.classified.kind == FileKind.SUBAGENT_JSONL else None
        unit_key = _unit_key(host, e.classified.session_id, agent_id)
        prior = existing.get(unit_key)
        if (
            prior is not None
            and prior.get("transcript_size") == e.size
            and prior.get("transcript_mtime_ns") == e.mtime_ns
            and prior.get("usage_source") in ("transcript", "transcript_partial")
        ):
            report.files_skipped += 1
            continue
        to_read.append((unit_key, agent_id, e))

    if dry_run:
        # Report which files WOULD be read/skipped, and by how many units
        # the store would change source -- no transcript is opened, no
        # unit/unit_msg row is written or deleted.
        report.files_read = len(to_read)
        report.units_by_source = {}
        for row in existing.values():
            _bump(report.units_by_source, row["usage_source"])
        report.duration_s = round(time.perf_counter() - t0, 3)
        return report

    read_results: list[tuple[str, str | None, _FileEntry, TranscriptSummary]] = []
    for unit_key, agent_id, e in to_read:
        summary = read_transcript(e.abs_path)
        read_results.append((unit_key, agent_id, e, summary))
        report.files_read += 1

    # design Section 2.2 step 2: "Process files ordered by their first_ts."
    read_results.sort(key=lambda r: r[3].first_ts or "")

    for unit_key, agent_id, e, summary in read_results:
        store.platform.execute("DELETE FROM unit_msg WHERE unit_key = ?", (unit_key,))

        kind = "main" if e.classified.kind == FileKind.MAIN else "subagent"
        parent_unit_key = None if kind == "main" else _unit_key(host, e.classified.session_id, None)

        sums = [0, 0, 0, 0, 0, 0]
        n_kept = 0
        for mid, mu in sorted(summary.messages.items(), key=lambda kv: (kv[1].ts or "", kv[0])):
            owner = store.platform.execute("SELECT unit_key FROM unit_msg WHERE msg_id = ?", (mid,)).fetchone()
            if owner is not None and owner["unit_key"] != unit_key:
                report.messages_deduplicated += 1
                continue
            store.platform.execute(
                "INSERT OR REPLACE INTO unit_msg (msg_id, unit_key, ts) VALUES (?, ?, ?)", (mid, unit_key, mu.ts)
            )
            sums[0] += mu.input_tokens
            sums[1] += mu.cache_write
            sums[2] += mu.cache_read
            sums[3] += mu.output
            sums[4] += mu.cache_write_1h
            sums[5] += mu.cache_write_5m
            n_kept += 1

        row = {
            "unit_key": unit_key,
            "host": host,
            "kind": kind,
            "session_id": e.classified.session_id,
            "agent_id": agent_id,
            "workflow_run_id": None,
            "parent_unit_key": parent_unit_key,
            "project_slug": e.project_slug,
            "entrypoint": summary.entrypoint,
            "cc_version": summary.version,
            "models": json.dumps(sorted(summary.models), ensure_ascii=False),
            "first_ts": summary.first_ts,
            "last_ts": summary.last_ts,
            "conversation_last_ts": summary.conversation_last_ts,
            "n_messages": n_kept,
            "usage_input": sums[0],
            "usage_cache_write": sums[1],
            "usage_cache_read": sums[2],
            "usage_output": sums[3],
            "usage_cache_write_1h": sums[4],
            "usage_cache_write_5m": sums[5],
            "usage_source": "transcript",
            "transcript_path": str(e.abs_path),
            "transcript_sha256": summary.sha256,
            "transcript_size": e.size,
            "transcript_mtime_ns": e.mtime_ns,
            "extractor_version": EXTRACTOR_VERSION,
            "scanned_ts": now(),
        }
        _upsert_unit(store, unit_key, existing.get(unit_key), row)
        existing[unit_key] = {**(existing.get(unit_key) or {}), **row}

    # ---- step 3: subagent / workflow-agent .meta.json -------------------
    for e in meta_entries:
        c = e.classified
        kind = "subagent" if c.kind == FileKind.SUBAGENT_META else "workflow_agent"
        unit_key = _unit_key(host, c.session_id, c.agent_id)
        meta = _read_meta(e.abs_path)
        parent_unit_key = _unit_key(host, c.session_id, None)

        prior = existing.get(unit_key)
        if prior is not None:
            # A sibling .jsonl already produced this unit (or a prior scan
            # did) -- backfill the fields only meta.json carries, never
            # touch its usage_source/sums.
            changes = {
                "agent_type": meta.get("agentType") if meta.get("agentType") is not None else prior.get("agent_type"),
                "spawn_tool_use_id": meta.get("toolUseId") if meta.get("toolUseId") is not None else prior.get("spawn_tool_use_id"),
                "workflow_run_id": c.workflow_run_id if c.workflow_run_id is not None else prior.get("workflow_run_id"),
            }
            update(store, "unit", pk_column="unit_key", pk_value=unit_key, changes=changes)
            existing[unit_key] = {**prior, **changes}
            continue

        models = []
        if isinstance(meta.get("model"), str) and meta["model"]:
            models = [meta["model"]]
        row = {
            "unit_key": unit_key,
            "host": host,
            "kind": kind,
            "session_id": c.session_id,
            "agent_id": c.agent_id,
            "workflow_run_id": c.workflow_run_id,
            "parent_unit_key": parent_unit_key,
            "spawn_tool_use_id": meta.get("toolUseId"),
            "agent_type": meta.get("agentType"),
            "project_slug": e.project_slug,
            "models": json.dumps(models, ensure_ascii=False),
            "first_ts": _mtime_iso(e.abs_path),
            "usage_source": "none",
            "extractor_version": EXTRACTOR_VERSION,
            "scanned_ts": now(),
        }
        try:
            insert(store, "unit", row)
        except ValidationError:
            continue
        existing[unit_key] = row

    # ---- step 4: the Remote Control pattern -----------------------------
    if statusline_dir is not None:
        sessions = read_sessions(statusline_dir)
        for session_id, entry in sessions.items():
            unit_key = _unit_key(host, session_id, None)
            prior = existing.get(unit_key)

            if prior is None:
                # design Section 2.2 step 4 (S-2 fix round): "If the unit
                # has no transcript file at all, set usage_source =
                # 'statusline_total'." sessions.json carries no cwd/project
                # field (trialerror.obs.statusline_capture's own recorded
                # shape), so project_slug is genuinely unknowable here --
                # "(unknown)" says so rather than guessing.
                row = {
                    "unit_key": unit_key,
                    "host": host,
                    "kind": "main",
                    "session_id": session_id,
                    "project_slug": "(unknown)",
                    "first_ts": entry.first_ts,
                    "last_ts": entry.last_ts,
                    "usage_source": "statusline_total",
                    "statusline_cost": json.dumps(entry.cost, ensure_ascii=False) if entry.cost is not None else None,
                    "extractor_version": EXTRACTOR_VERSION,
                    "scanned_ts": now(),
                }
                try:
                    insert(store, "unit", row)
                except ValidationError:
                    continue
                existing[unit_key] = row
                report.remote_control_sessions.append(session_id)
                continue

            if prior.get("kind") != "main":
                continue
            conv_last = prior.get("conversation_last_ts")
            if not conv_last or not entry.last_ts:
                continue
            try:
                gap = (parse(entry.last_ts) - parse(conv_last)).total_seconds()
            except ValueError:
                continue
            if gap > _RC_GAP_SECONDS:
                changes = {
                    "usage_source": "transcript_partial",
                    "statusline_cost": json.dumps(entry.cost, ensure_ascii=False) if entry.cost is not None else None,
                }
                update(store, "unit", pk_column="unit_key", pk_value=unit_key, changes=changes)
                existing[unit_key] = {**prior, **changes}
                report.remote_control_sessions.append(session_id)

    # ---- final tally ------------------------------------------------------
    report.units_by_source = {}
    for row in _existing_units_by_key(store, host).values():
        _bump(report.units_by_source, row["usage_source"])
    report.duration_s = round(time.perf_counter() - t0, 3)
    return report
