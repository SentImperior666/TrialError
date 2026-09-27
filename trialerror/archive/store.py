"""The transcript archive: a content-addressed copy of a folder of session
transcripts, with a small SQLite index beside it.

Claude Code deletes a transcript some days after the session's last activity
(30 by default), and the transcripts are the only complete record of what an
agent did. This module keeps every version of every file it has ever seen,
outside every repository, so nothing is lost when the host deletes the
original. One archive directory serves one host::

    <dest>/objects/<sha[0:2]>/<sha256>.gz    gzip of the file's exact bytes
    <dest>/index.db                          the index (schema below)
    <dest>/audits/AUDIT_<host>_<date>.md     written by ``archive audit``

``index.db`` is its own SQLite file, not a TrialError store, so it needs no
store migration and cannot collide with another lane's migration.

Nothing in here reads a transcript's *content* other than to copy and hash its
bytes; the audit module reads timestamps and a version string only.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import os
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

__all__ = [
    "ArchiveError",
    "SCHEMA_VERSION",
    "PRUNE_AFTER_DAYS",
    "MAX_FILE_BYTES",
    "kind_of",
    "is_secret",
    "walk_source",
    "utc_iso",
    "parse_ts",
    "open_index",
    "run_archive",
    "restore",
    "archive_status",
    "object_path",
    "exclusive_lock",
    "git_working_tree_above",
    "default_opener",
]

SCHEMA_VERSION = "1"
#: A superseded object is pruned once it has been superseded for this long.
PRUNE_AFTER_DAYS = 30
#: Files above this size are skipped with a warning, never truncated.
MAX_FILE_BYTES = 2 * 1024 ** 3
_CHUNK = 1 << 20
_COMMIT_EVERY = 200
#: How long a connection waits for the index's lock before giving up.
_BUSY_TIMEOUT_S = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS object (sha256 TEXT PRIMARY KEY, size INTEGER NOT NULL,
    stored_size INTEGER NOT NULL, stored_ts TEXT NOT NULL, superseded_by TEXT, deleted_ts TEXT);
CREATE TABLE IF NOT EXISTS path_state (host TEXT NOT NULL, rel_path TEXT NOT NULL,
    latest_sha256 TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
    kind TEXT NOT NULL, first_seen_ts TEXT NOT NULL, last_seen_ts TEXT NOT NULL, gone_ts TEXT,
    PRIMARY KEY (host, rel_path));
CREATE TABLE IF NOT EXISTS snapshot (id INTEGER PRIMARY KEY AUTOINCREMENT, host TEXT NOT NULL,
    rel_path TEXT NOT NULL, sha256 TEXT NOT NULL REFERENCES object(sha256), size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL, seen_ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run (id INTEGER PRIMARY KEY AUTOINCREMENT, host TEXT NOT NULL,
    started_ts TEXT NOT NULL, finished_ts TEXT, scanned INTEGER, new_objects INTEGER,
    bytes_stored INTEGER, superseded INTEGER, gone INTEGER, errors INTEGER);
CREATE INDEX IF NOT EXISTS snapshot_path ON snapshot(host, rel_path, id);
CREATE INDEX IF NOT EXISTS snapshot_sha ON snapshot(sha256);
"""


class ArchiveError(Exception):
    """A refusal or failure with a stable ``code`` the CLI turns into an envelope."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


# --------------------------------------------------------------------------- time


def utc_iso(when: datetime | None = None) -> str:
    when = when or datetime.now(timezone.utc)
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(text: str) -> datetime:
    """An ISO-8601 timestamp (``Z`` or an offset; a bare one is taken as UTC)."""
    value = text.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ArchiveError("bad_input", f"not an ISO timestamp: {text!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ----------------------------------------------------------------- classification

_SECRET_DIRS = frozenset({"keys", ".ssh"})


def _parts(rel_path: str) -> list[str]:
    return [p for p in rel_path.replace("\\", "/").split("/") if p]


def is_secret(rel_path: str) -> bool:
    """Whether a path must never be copied: key folders, ``*.key``, ``.ssh``,
    ``.credentials*``, ``rclone.conf`` and ``cookies.txt``."""
    parts = [p.lower() for p in _parts(rel_path)]
    if not parts:
        return False
    if any(p in _SECRET_DIRS for p in parts):
        return True
    name = parts[-1]
    return name.endswith(".key") or name.startswith(".credentials") or name in {"rclone.conf", "cookies.txt"}


def kind_of(rel_path: str) -> str:
    """The kind of a file from its path relative to the ``projects`` folder
    (first component: the project folder)."""
    parts = _parts(rel_path)
    inner = parts[1:]
    if not inner:
        return "other"
    name = inner[-1]
    if "tool-results" in inner[:-1]:
        return "tool_result"
    if name == "journal.jsonl":
        return "workflow_journal"
    if "subagents" in inner[:-1]:
        if name.endswith(".meta.json"):
            return "subagent_meta"
        if name.endswith(".jsonl"):
            return "subagent"
    if "workflows" in inner[:-1] and name.endswith(".json"):
        return "workflow_meta"
    if len(inner) == 1 and name.endswith(".jsonl"):
        return "main"
    return "other"


def walk_source(src: Path) -> Iterator[tuple[str, Path, os.stat_result | None, str | None]]:
    """Yield ``(rel_path, abs_path, stat, skip_reason)`` for every regular file
    under ``src``, in a stable order. ``skip_reason`` is ``"secret"`` or
    ``"unreadable"``, else ``None``; secret folders are never entered."""
    for root, dirs, files in os.walk(src, followlinks=False):
        rel_root = Path(root).relative_to(src).as_posix()
        dirs[:] = sorted(d for d in dirs if d.lower() not in _SECRET_DIRS)
        for name in sorted(files):
            rel = name if rel_root == "." else f"{rel_root}/{name}"
            path = Path(root) / name
            if is_secret(rel):
                yield rel, path, None, "secret"
                continue
            try:
                st = os.lstat(path)
            except OSError:
                yield rel, path, None, "unreadable"
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            yield rel, path, st, None


# ------------------------------------------------------------------ safety checks


def git_working_tree_above(path: Path) -> Path | None:
    """The nearest ancestor (or the path itself) holding a ``.git``, else ``None``."""
    cur = Path(os.path.abspath(path)).resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _check_destination(src: Path, dest: Path) -> None:
    repo = git_working_tree_above(dest)
    if repo is not None:
        raise ArchiveError(
            "archive_in_repo",
            f"the destination {dest} is inside the git working tree {repo}; an archive of transcripts "
            "must live outside every repository",
            {"repo": str(repo)},
        )
    src_r, dest_r = src.resolve(), dest.resolve()
    if dest_r == src_r or src_r in dest_r.parents:
        raise ArchiveError(
            "dest_in_src", f"the destination {dest} is inside the source {src}; the archive would copy itself"
        )


# ------------------------------------------------------------------------- lock


@contextlib.contextmanager
def exclusive_lock(path: Path) -> Iterator[bool]:
    """Hold an exclusive OS lock on ``path``; yield ``False`` (without waiting)
    when another holder has it. The lock dies with the process, so a crash
    never leaves a stale lock behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    acquired = False
    try:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            acquired = False
        yield acquired
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


# ------------------------------------------------------------------------ index


def open_index(dest: Path, *, create: bool = True) -> sqlite3.Connection:
    """Open ``<dest>/index.db`` (creating it when ``create``). Autocommit is on;
    callers open transactions explicitly."""
    index = dest / "index.db"
    if not create and not index.is_file():
        raise ArchiveError("no_archive", f"no archive index at {index}")
    dest.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(index), timeout=_BUSY_TIMEOUT_S, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.executescript(_SCHEMA)
    db.execute("INSERT OR IGNORE INTO meta(k, v) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
    return db


def object_path(dest: Path, sha: str) -> Path:
    return dest / "objects" / sha[:2] / f"{sha}.gz"


# ------------------------------------------------------------------ opening sources


def default_opener(path: Path | str, mode: str = "rb") -> Any:
    """Open a source file for reading without ever blocking its writer or its
    deleter. Python's ``open`` on Windows leaves out ``FILE_SHARE_DELETE``, so while
    a transcript is being hashed nothing else could delete or rename-over it (Claude
    Code's own clean-up, or a rewrite by rename). Here the file is opened with all
    three share modes; elsewhere ``open`` already behaves that way."""
    if os.name != "nt" or mode != "rb":
        return open(path, mode)
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    generic_read, share_all, open_existing, attribute_normal = 0x80000000, 0x7, 3, 0x80
    handle = kernel32.CreateFileW(os.fspath(path), generic_read, share_all, None, open_existing, attribute_normal, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except OSError:
        kernel32.CloseHandle(handle)
        raise
    return os.fdopen(fd, "rb")


# ------------------------------------------------------------------------- run


def _store_one(
    db: sqlite3.Connection,
    dest: Path,
    host: str,
    rel: str,
    path: Path,
    st: os.stat_result,
    prev: sqlite3.Row | None,
    *,
    now_iso: str,
    dry_run: bool,
    opener: Callable[..., Any],
    stats: dict[str, Any],
) -> None:
    """Read one file once: hash it, compress it into a temp file, and (when the
    content is new) move it into ``objects/``. Streaming, so a 1 GB transcript
    never sits in memory. While reading, the hash of the first ``prev_size``
    bytes is taken too: when it equals the previous version's hash, the new
    content extends the old and the old object is superseded."""
    prev_size = prev["size"] if prev is not None else None
    hasher = hashlib.sha256()
    prefix_hex = hashlib.sha256(b"").hexdigest() if prev_size == 0 else None
    total = 0
    tmp: Path | None = None
    out = None
    gz = None
    try:
        if not dry_run:
            tmp_dir = dest / "tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            tmp = tmp_dir / f"obj.tmp{os.getpid()}"
            out = open(tmp, "wb")
            gz = gzip.GzipFile(fileobj=out, mode="wb", compresslevel=6, mtime=0)
        with opener(path, "rb") as src_file:
            while True:
                chunk = src_file.read(_CHUNK)
                if not chunk:
                    break
                if prev_size is not None and prefix_hex is None and total + len(chunk) >= prev_size:
                    split = prev_size - total
                    hasher.update(chunk[:split])
                    prefix_hex = hasher.copy().hexdigest()
                    hasher.update(chunk[split:])
                else:
                    hasher.update(chunk)
                total += len(chunk)
                if gz is not None:
                    gz.write(chunk)
        if gz is not None:
            gz.close()
            out.flush()
            os.fsync(out.fileno())
            out.close()
            out = None
        sha = hasher.hexdigest()
        existing = db.execute("SELECT deleted_ts FROM object WHERE sha256 = ?", (sha,)).fetchone()
        if existing is None or existing["deleted_ts"] is not None:
            stored_size = 0
            if not dry_run:
                final = object_path(dest, sha)
                final.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tmp, final)
                tmp = None
                stored_size = final.stat().st_size
            db.execute(
                "INSERT OR REPLACE INTO object(sha256, size, stored_size, stored_ts, superseded_by, deleted_ts) "
                "VALUES (?, ?, ?, ?, NULL, NULL)",
                (sha, total, stored_size, now_iso),
            )
            stats["new_objects"] += 1
            stats["bytes_stored"] += stored_size
        prev_sha = prev["latest_sha256"] if prev is not None else None
        if prev_sha is not None and prev_sha != sha and total > (prev_size or 0) and prefix_hex == prev_sha:
            cur = db.execute(
                "UPDATE object SET superseded_by = ? WHERE sha256 = ? AND superseded_by IS NULL", (sha, prev_sha)
            )
            stats["superseded"] += cur.rowcount
        if prev_sha != sha:
            db.execute(
                "INSERT INTO snapshot(host, rel_path, sha256, size, mtime_ns, seen_ts) VALUES (?,?,?,?,?,?)",
                (host, rel, sha, total, st.st_mtime_ns, now_iso),
            )
        db.execute(
            "INSERT INTO path_state(host, rel_path, latest_sha256, size, mtime_ns, kind, first_seen_ts, "
            "last_seen_ts, gone_ts) VALUES (?,?,?,?,?,?,?,?,NULL) "
            "ON CONFLICT(host, rel_path) DO UPDATE SET latest_sha256 = excluded.latest_sha256, "
            "size = excluded.size, mtime_ns = excluded.mtime_ns, kind = excluded.kind, "
            "last_seen_ts = excluded.last_seen_ts, gone_ts = NULL",
            (host, rel, sha, total, st.st_mtime_ns, kind_of(rel), now_iso, now_iso),
        )
    finally:
        if gz is not None and not gz.closed:
            gz.close()
        if out is not None:
            out.close()
        if tmp is not None:
            with contextlib.suppress(OSError):
                tmp.unlink()


def _superseded_since(db: sqlite3.Connection, sha: str) -> str | None:
    """When ``sha`` was superseded: the first time its superseding content was seen."""
    row = db.execute(
        "SELECT MIN(s.seen_ts) AS ts FROM snapshot s JOIN object o ON o.superseded_by = s.sha256 "
        "WHERE o.sha256 = ?",
        (sha,),
    ).fetchone()
    return row["ts"] if row and row["ts"] else None


def _unlink_pruned(dest: Path, shas: list[str]) -> None:
    """Delete the files of objects whose rows already say deleted. Runs AFTER the
    transaction that recorded the deletion is committed: a kill in between leaves
    an orphan file (harmless, swept by the next run), never a live row with no file."""
    for sha in shas:
        with contextlib.suppress(OSError):
            object_path(dest, sha).unlink()


def _prune(db: sqlite3.Connection, dest: Path, now: datetime, dry_run: bool) -> tuple[int, list[str]]:
    """Mark old superseded objects deleted; returns how many, and the shas whose files
    are to be unlinked once the marks are committed (these, and any earlier orphans)."""
    cutoff = utc_iso(now - timedelta(days=PRUNE_AFTER_DAYS))
    rows = db.execute(
        "SELECT sha256 FROM object WHERE superseded_by IS NOT NULL AND deleted_ts IS NULL "
        "AND sha256 NOT IN (SELECT latest_sha256 FROM path_state)"
    ).fetchall()
    pruned = 0
    for row in rows:
        since = _superseded_since(db, row["sha256"])
        if since is None or since > cutoff:
            continue
        db.execute("UPDATE object SET deleted_ts = ? WHERE sha256 = ?", (utc_iso(now), row["sha256"]))
        pruned += 1
    to_unlink = [
        r["sha256"]
        for r in db.execute("SELECT sha256 FROM object WHERE deleted_ts IS NOT NULL")
        if object_path(dest, r["sha256"]).exists()
    ]
    return pruned, ([] if dry_run else to_unlink)


def run_archive(
    src: str | Path,
    dest: str | Path,
    host: str,
    *,
    dry_run: bool = False,
    prune: bool = True,
    now: datetime | None = None,
    opener: Callable[..., Any] = default_opener,
) -> dict[str, Any]:
    """Copy every new or changed file under ``src`` into the archive at ``dest``.

    Returns the run's counters; ``{"locked": True, ...}`` when another run holds
    the archive. Raises :class:`ArchiveError` on a refusal."""
    src_p, dest_p = Path(src), Path(dest)
    if not host or not host.strip():
        raise ArchiveError("bad_input", "--host must be a non-empty label")
    if not src_p.is_dir():
        raise ArchiveError("src_not_found", f"the source folder {src_p} does not exist or is not a folder")
    _check_destination(src_p, dest_p)
    now = now or datetime.now(timezone.utc)
    if dry_run:
        with _dry_run_index(dest_p) as db:
            return _run_locked(db, src_p, dest_p, host, dry_run=True, prune=prune, now=now, opener=opener)
    dest_p.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        with contextlib.suppress(OSError):
            os.chmod(dest_p, 0o700)
    with exclusive_lock(dest_p / ".lock") as acquired:
        if not acquired:
            return {"locked": True, "message": "another archive run holds this archive; nothing done"}
        _clear_tmp(dest_p)
        db = open_index(dest_p)
        try:
            return _run_locked(db, src_p, dest_p, host, dry_run=False, prune=prune, now=now, opener=opener)
        finally:
            db.close()


def _clear_tmp(dest: Path) -> None:
    """Sweep temp object files left by a run that was killed. Only a run that holds the
    archive's lock calls this, so nothing in ``tmp/`` can belong to a live run."""
    tmp_dir = dest / "tmp"
    if tmp_dir.is_dir():
        for leftover in tmp_dir.iterdir():
            with contextlib.suppress(OSError):
                leftover.unlink()


@contextlib.contextmanager
def _dry_run_index(dest: Path) -> Iterator[sqlite3.Connection]:
    """A dry run writes nothing and never holds the index's write lock (it takes no
    ``.lock``, so it could otherwise block a real run): it works on an in-memory copy
    of the index, read once through a read-only connection."""
    index = dest / "index.db"
    db = sqlite3.connect(":memory:", isolation_level=None)
    if index.is_file():
        source = sqlite3.connect(f"{index.resolve().as_uri()}?mode=ro", uri=True, timeout=_BUSY_TIMEOUT_S)
        try:
            source.backup(db)
        finally:
            source.close()
    else:
        db.executescript(_SCHEMA)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        with contextlib.suppress(sqlite3.Error):
            db.execute("ROLLBACK")
        db.close()


def _run_locked(
    db: sqlite3.Connection,
    src: Path,
    dest: Path,
    host: str,
    *,
    dry_run: bool,
    prune: bool,
    now: datetime,
    opener: Callable[..., Any],
) -> dict[str, Any]:
    now_iso = utc_iso(now)
    stats: dict[str, Any] = {
        "scanned": 0,
        "skipped_unchanged": 0,
        "skipped_secret": 0,
        "skipped_large": 0,
        "new_objects": 0,
        "bytes_stored": 0,
        "superseded": 0,
        "gone": 0,
        "pruned": 0,
        "errors": [],
    }
    run_id: int | None = None
    if not dry_run:
        run_id = db.execute("INSERT INTO run(host, started_ts) VALUES (?, ?)", (host, now_iso)).lastrowid
    db.execute("BEGIN")
    seen: set[str] = set()
    to_unlink: list[str] = []
    pending = 0
    for rel, path, st, skip in walk_source(src):
        if skip == "secret":
            stats["skipped_secret"] += 1
            continue
        seen.add(rel)
        if st is None:
            stats["errors"].append({"path": rel, "error": "could not stat the file"})
            continue
        stats["scanned"] += 1
        if st.st_size > MAX_FILE_BYTES:
            stats["skipped_large"] += 1
            stats["errors"].append({"path": rel, "error": f"skipped: {st.st_size} bytes is over the 2 GB limit"})
            continue
        prev = db.execute("SELECT * FROM path_state WHERE host = ? AND rel_path = ?", (host, rel)).fetchone()
        if prev is not None and prev["size"] == st.st_size and prev["mtime_ns"] == st.st_mtime_ns:
            db.execute(
                "UPDATE path_state SET last_seen_ts = ?, gone_ts = NULL WHERE host = ? AND rel_path = ?",
                (now_iso, host, rel),
            )
            stats["skipped_unchanged"] += 1
            continue
        try:
            _store_one(db, dest, host, rel, path, st, prev, now_iso=now_iso, dry_run=dry_run, opener=opener, stats=stats)
        except OSError as exc:
            stats["errors"].append({"path": rel, "error": f"{type(exc).__name__}: {exc}"})
            continue
        pending += 1
        if pending >= _COMMIT_EVERY and not dry_run:
            db.execute("COMMIT")
            db.execute("BEGIN")
            pending = 0
    for row in db.execute("SELECT rel_path FROM path_state WHERE host = ? AND gone_ts IS NULL", (host,)).fetchall():
        if row["rel_path"] not in seen:
            db.execute(
                "UPDATE path_state SET gone_ts = ? WHERE host = ? AND rel_path = ?", (now_iso, host, row["rel_path"])
            )
            stats["gone"] += 1
    if prune:
        stats["pruned"], to_unlink = _prune(db, dest, now, dry_run)
    if dry_run:
        db.execute("ROLLBACK")
    else:
        db.execute(
            "UPDATE run SET finished_ts = ?, scanned = ?, new_objects = ?, bytes_stored = ?, superseded = ?, "
            "gone = ?, errors = ? WHERE id = ?",
            (
                utc_iso(),
                stats["scanned"],
                stats["new_objects"],
                stats["bytes_stored"],
                stats["superseded"],
                stats["gone"],
                len(stats["errors"]),
                run_id,
            ),
        )
        db.execute("COMMIT")
        _unlink_pruned(dest, to_unlink)
    stats["dry_run"] = dry_run
    stats["host"] = host
    return stats


# ---------------------------------------------------------------------- restore


def _read_pruned(dest: Path, sha: str, depth: int) -> bytes | None:
    """The bytes of a version whose file was pruned. A version is superseded only when
    the next one extends it, so it is exactly the first ``size`` bytes of its
    ``superseded_by`` object (itself possibly pruned: follow the chain to a live one)."""
    if depth > 10_000:
        return None
    db = open_index(dest, create=False)
    try:
        row = db.execute("SELECT size, superseded_by, deleted_ts FROM object WHERE sha256 = ?", (sha,)).fetchone()
    finally:
        db.close()
    if row is None or row["superseded_by"] is None or row["deleted_ts"] is None:
        return None
    parent = read_object(dest, row["superseded_by"], _depth=depth + 1)
    data = parent[: row["size"]]
    if len(data) != row["size"] or hashlib.sha256(data).hexdigest() != sha:
        raise ArchiveError(
            "hash_mismatch", f"pruned object {sha} does not match the start of its superseding version", {"sha256": sha}
        )
    return data


def read_object(dest: Path, sha: str, *, _depth: int = 0) -> bytes:
    """The exact original bytes of an object, after checking they hash to ``sha``. A
    pruned version is rebuilt from the version that superseded it."""
    path = object_path(dest, sha)
    if not path.is_file():
        rebuilt = _read_pruned(dest, sha, _depth)
        if rebuilt is not None:
            return rebuilt
        raise ArchiveError("object_missing", f"the archive has no object file for {sha}", {"sha256": sha})
    try:
        data = gzip.decompress(path.read_bytes())
    except (OSError, EOFError) as exc:
        raise ArchiveError("hash_mismatch", f"object {sha} cannot be decompressed: {exc}", {"sha256": sha}) from exc
    if hashlib.sha256(data).hexdigest() != sha:
        raise ArchiveError("hash_mismatch", f"object {sha} does not hash to its own name", {"sha256": sha})
    return data


def restore(
    dest: str | Path,
    out: str | Path,
    *,
    sha: str | None = None,
    host: str | None = None,
    rel_path: str | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Write the exact original bytes of one archived file to ``out`` and check
    the hash. Give ``sha``, or ``host`` and ``rel_path`` (with ``as_of`` for the
    version the archive had seen at or before that time)."""
    dest_p, out_p = Path(dest), Path(out)
    if bool(sha) == bool(host or rel_path):
        raise ArchiveError("bad_input", "give either --sha, or --host with --path (not both)")
    if not sha and not (host and rel_path):
        raise ArchiveError("bad_input", "--host and --path go together")
    db = open_index(dest_p, create=False)
    try:
        if sha:
            row = db.execute("SELECT sha256 FROM object WHERE sha256 = ?", (sha,)).fetchone()
            if row is None:
                raise ArchiveError("not_found", f"no object {sha} in the archive")
            chosen = sha
        else:
            rel = rel_path.replace("\\", "/")
            if as_of:
                row = db.execute(
                    "SELECT sha256 FROM snapshot WHERE host = ? AND rel_path = ? AND seen_ts <= ? "
                    "ORDER BY id DESC LIMIT 1",
                    (host, rel, utc_iso(parse_ts(as_of))),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT latest_sha256 AS sha256 FROM path_state WHERE host = ? AND rel_path = ?", (host, rel)
                ).fetchone()
            if row is None:
                raise ArchiveError("not_found", f"the archive has no record of {rel} on host {host}")
            chosen = row["sha256"]
    finally:
        db.close()
    data = read_object(dest_p, chosen)
    if out_p.exists():
        raise ArchiveError("out_exists", f"{out_p} already exists; restore never overwrites")
    out_p.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_p.with_name(out_p.name + f".tmp{os.getpid()}")
    tmp.write_bytes(data)
    os.replace(tmp, out_p)
    return {"sha256": chosen, "bytes": len(data), "out": str(out_p), "verified": True}


# ----------------------------------------------------------------------- status


def archive_status(dest: str | Path) -> dict[str, Any]:
    """Per host: files, bytes stored, last run, gone and superseded counts."""
    dest_p = Path(dest)
    db = open_index(dest_p, create=False)
    try:
        hosts = [r["host"] for r in db.execute("SELECT DISTINCT host FROM path_state ORDER BY host")]
        report: dict[str, Any] = {}
        for host in hosts:
            files = db.execute(
                "SELECT COUNT(*) AS n FROM path_state WHERE host = ? AND gone_ts IS NULL", (host,)
            ).fetchone()["n"]
            gone = db.execute(
                "SELECT COUNT(*) AS n FROM path_state WHERE host = ? AND gone_ts IS NOT NULL", (host,)
            ).fetchone()["n"]
            objs = db.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(o.stored_size), 0) AS b, "
                "COALESCE(SUM(o.superseded_by IS NOT NULL), 0) AS sup FROM object o WHERE o.deleted_ts IS NULL "
                "AND o.sha256 IN (SELECT DISTINCT sha256 FROM snapshot WHERE host = ?)",
                (host,),
            ).fetchone()
            last = db.execute(
                "SELECT started_ts, finished_ts, errors FROM run WHERE host = ? ORDER BY id DESC LIMIT 1", (host,)
            ).fetchone()
            report[host] = {
                "files": files,
                "gone": gone,
                "objects": objs["n"],
                "bytes_stored": objs["b"],
                "superseded": objs["sup"],
                "last_run": dict(last) if last else None,
            }
        return {"dest": str(dest_p), "hosts": report}
    finally:
        db.close()
