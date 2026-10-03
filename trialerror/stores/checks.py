"""M1's doctor checks: schema-version match per DB, XID-dangling scan,
``anchors_dangling`` counter. Auto-discovered by
``trialerror.util.doctor.discover_and_register_checks`` exactly like M0's
``license_audit`` — dropping this file is the entire registration step, no
shared file touched (design Section 5.2 doctor row: "framework +
license-audit in M0; each module registers its own checks").

``DoctorContext.program_root`` (an M0-owned field, used as-is — not
extended) supplies the ops/knowledge/jobs DB locations; the platform DB is
resolved via ``DoctorContext.platform_root`` when the caller supplies one
(the ``--platform-root`` CLI flag / an acceptance journey's own param),
falling back to ``trialerror.stores.paths.platform_db_path()``'s own
``TRIALERROR_PLATFORM_ROOT``-env-or-``~/.trialerror`` resolution otherwise, since
platform.db is not per-program (fix-accept, C-0064: this used to ignore
``ctx`` entirely and always re-derive from the env var/default, which is
why ``trialerror accept`` could false-positive against a real machine's
``~/.trialerror/platform.db``). Any DB file that doesn't exist yet is reported
``skip`` (a program that hasn't been initialized, or a fresh platform
install, is not a doctor failure).
"""

from __future__ import annotations

import sqlite3
import tomllib
from pathlib import Path

from trialerror.stores import paths
from trialerror.stores.connection import connect
from trialerror.stores.migrate import current_version, latest_version, read_provenance
from trialerror.stores.store import SCHEMA_MODULES
from trialerror.stores.xid import XID_REGISTRY
from trialerror.util.doctor import CheckResult, DoctorContext, register_check

__all__ = [
    "check_store_schema_version",
    "check_store_newer_than_code",
    "check_xid_dangling",
    "check_anchors_dangling",
]

_DB_KINDS = ("platform", "ops", "knowledge", "jobs")


def _load_paths_config(program_root: Path) -> dict | None:
    """Best-effort ``[paths]`` config load from ``<program_root>/trialerror.toml``
    -- the same private-per-module loader convention every other doctor
    ``checks.py`` uses for its own config reads (e.g.
    ``trialerror.ingest.checks._active_model_key``), mirroring the "ambient, no
    caller opt-in needed" spirit ``trialerror.stores.store.open_store``'s own
    ``_auto_load_paths_config`` established for ``[paths].stores_dir``
    (the import-design notes (internal, not in this export) Sec 5 knob #1).

    fix-doctor-config-awareness (build-v2-polish): before this, every
    program-scoped check below resolved ops/knowledge/jobs DB paths via
    ``paths.*_db_path(ctx.program_root)`` with NO config argument at all --
    the hardcoded ``"stores"`` literal, even for a program whose
    ``trialerror.toml`` relocated ``[paths].stores_dir`` elsewhere (open_store
    itself already honors the knob; `trialerror doctor` did not), so `trialerror
    doctor --program-root X` on a knob-relocated program reported every
    program-scoped DB as missing (or worse, silently inspected a stale file
    left over at the OLD default location). See
    ``tests/test_config_paths_knobs.py``'s relocation fixture for the
    round-trip this now passes. Missing/invalid ``trialerror.toml`` -> ``None``
    (reproduces the exact pre-existing hardcoded-literal behavior)."""
    from trialerror.util.config import CONFIG_FILENAME, load_config

    cfg_path = program_root / CONFIG_FILENAME
    if not cfg_path.is_file():
        return None
    try:
        return load_config(cfg_path).raw
    except Exception:
        return None


def _db_path(ctx: DoctorContext, db_kind: str) -> Path | None:
    if db_kind == "platform":
        # precedence: an explicit ctx.platform_root (threaded from the
        # --platform-root CLI flag or an acceptance journey's own param)
        # wins over TRIALERROR_PLATFORM_ROOT, which wins over the ~/.trialerror
        # default -- paths.platform_db_path(root=None) already implements
        # the env/default half of that fallback, so passing ctx.platform_root
        # straight through (None when the caller didn't supply one) is the
        # entire fix (fix-accept, C-0064): before this, the "platform" DB
        # kind ignored ctx entirely and always re-derived from the env var/
        # default, so a real machine's ~/.trialerror/platform.db could leak into
        # a `trialerror accept` run even though the journey resolved its own
        # scratch platform_root.
        return paths.platform_db_path(root=ctx.platform_root)
    if ctx.program_root is None:
        return None
    config = _load_paths_config(ctx.program_root)
    if db_kind == "ops":
        return paths.ops_db_path(ctx.program_root, config)
    if db_kind == "knowledge":
        return paths.knowledge_db_path(ctx.program_root, config)
    if db_kind == "jobs":
        return paths.jobs_db_path(ctx.program_root, config)
    raise ValueError(f"unknown db_kind {db_kind!r}")


@register_check("store_schema_version", category="stores")
def check_store_schema_version(ctx: DoctorContext) -> CheckResult:
    """Every present DB's ``PRAGMA user_version`` against the version this
    client declares -- and, on a mismatch, WHICH WAY it points.

    F17 (lane FB-acq item 5): this check used to compute ``current ==
    expected`` and fail on anything else, so "this store has not been migrated
    yet" and "this store was migrated by a NEWER client than the one reading it
    now" arrived as the same flat failure with the same two numbers, although
    the two call for opposite actions (migrate the store vs. upgrade the
    client) and only one of them is dangerous. A store that is AHEAD of the
    client is now reported by direction, and -- when the runner's own
    :data:`~trialerror.stores.migrate.PROVENANCE_TABLE` can account for every
    version in between and all of them only ADDED to the schema -- as a
    ``warn`` rather than a ``fail``: an older client can read and write every
    table and column it knows about, so the honest reading is "upgrade the
    client", not "this store is broken". A gap in that history, or one
    non-additive migration in it, stays a failure: writing to a store whose
    shape has moved under you in a way this client cannot see is exactly what
    the check exists to stop.

    The MATCHING case keeps exactly its three keys (``current_version``,
    ``expected_version``, ``match``) -- a healthy program's details dict is
    byte-identical to what it was.
    """
    per_db: dict[str, dict] = {}
    parts: list[str] = []
    n_fail = 0
    n_warn = 0
    for db_kind in _DB_KINDS:
        path = _db_path(ctx, db_kind)
        if path is None or not path.exists():
            per_db[db_kind] = {"status": "skip", "reason": "database file not found"}
            continue
        conn = connect(path, read_only=True)
        try:
            current = current_version(conn)
            expected = latest_version(SCHEMA_MODULES[db_kind].MIGRATIONS)
            newer: list[dict] = []
            if current > expected:
                newer = [
                    {"version": row["version"], "name": row["name"], "additive": row["additive"]}
                    for row in read_provenance(conn)
                    if expected < int(row["version"]) <= current
                ]
        finally:
            conn.close()
        details: dict = {"current_version": current, "expected_version": expected, "match": current == expected}
        if current == expected:
            per_db[db_kind] = details
            continue
        head = f"{db_kind} (user_version={current}, expected={expected})"
        if current < expected:
            details["direction"] = "store_older"
            per_db[db_kind] = details
            n_fail += 1
            parts.append(
                f"{head}: this store is older than the client -- open it once with this client "
                "(any store-opening command) to migrate it"
            )
            continue
        details["direction"] = "store_newer"
        details["newer_migrations"] = newer
        accounted = {int(entry["version"]) for entry in newer} == set(range(expected + 1, current + 1))
        if not accounted:
            # No row for at least one of the versions in between: the runner
            # that applied it predates the provenance table, or a newer client
            # holds migrations this one has no name for. Either way this client
            # cannot say what changed.
            details["additive_only"] = None
            per_db[db_kind] = details
            n_fail += 1
            parts.append(
                f"{head}: this client is older than the store and the newer migration(s) are not known "
                "to be additive: upgrade the client before writing"
            )
            continue
        additive_only = all(int(entry["additive"]) == 1 for entry in newer)
        details["additive_only"] = additive_only
        per_db[db_kind] = details
        if additive_only:
            n_warn += 1
            parts.append(
                f"{head}: this client is older than the store: upgrade the client "
                f"(the {len(newer)} newer migration(s) are additive)"
            )
        else:
            n_fail += 1
            parts.append(
                f"{head}: this client is older than the store and the newer migration(s) are not known "
                "to be additive: upgrade the client before writing"
            )

    status = "fail" if n_fail else ("warn" if n_warn else "pass")
    message = (
        f"{len(parts)} DB(s) not on the expected schema version: {'; '.join(parts)}"
        if parts
        else "all present DB(s) on their expected schema version"
    )
    return CheckResult(
        name="store_schema_version", category="stores", status=status, message=message, details=per_db
    )


@register_check("store_newer_than_code", category="stores")
def check_store_newer_than_code(ctx: DoctorContext) -> CheckResult:
    """L8 part F, F2: names the path of any present store whose schema
    version is AHEAD of this client's latest known migration.

    Standalone from :func:`check_store_schema_version`'s richer
    additive/non-additive fail split -- this one check has a single job, to
    warn by NAME the moment any store is newer than the code reading it, with
    no judgment about whether that is safe. That is how
    ``research-harness/stores/ops.db``, left at ops v13 by lane code that had
    opened the harness repository's own ``trialerror.toml`` as its program
    root by mistake, would have shown itself before anything wrote through it
    -- a ``trialerror doctor`` run against that root would have named the
    path. Part F's ``program_root_is_harness`` refusal (F1) stops the mistake
    itself; this check is the independent tripwire for a store some OTHER
    client already moved. Never ``fail``: whether a newer store is dangerous
    is exactly the judgment ``store_schema_version`` already makes -- this
    check only ever needs to say where to look.
    """
    newer: list[dict] = []
    for db_kind in _DB_KINDS:
        path = _db_path(ctx, db_kind)
        if path is None or not path.exists():
            continue
        conn = connect(path, read_only=True)
        try:
            current = current_version(conn)
            expected = latest_version(SCHEMA_MODULES[db_kind].MIGRATIONS)
        finally:
            conn.close()
        if current > expected:
            newer.append(
                {"db": db_kind, "path": str(path), "current_version": current, "expected_version": expected}
            )

    status = "warn" if newer else "pass"
    message = (
        "; ".join(
            f"{entry['path']} is at schema version {entry['current_version']}, newer than this "
            f"client's latest known migration ({entry['expected_version']})"
            for entry in newer
        )
        if newer
        else "no present store is newer than this client's latest known migration"
    )
    return CheckResult(
        name="store_newer_than_code", category="stores", status=status, message=message, details={"newer": newer}
    )


def _program_id(ctx: DoctorContext) -> str | None:
    """The program's own id from ``trialerror.toml`` (``[program] id``), or None when
    the root has no readable toml. Used to scope PLATFORM-sourced XID references: a
    platform.db is shared by every program under one account, so a ``launch`` row that
    belongs to another program legitimately points at a session in THAT program's
    ops.db, not this one's."""
    if ctx.program_root is None:
        return None
    toml_path = Path(ctx.program_root) / "trialerror.toml"
    try:
        with open(toml_path, "rb") as fh:
            cfg = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    pid = (cfg.get("program") or {}).get("id") if isinstance(cfg, dict) else None
    return str(pid) if pid else None


def _has_column(conn: sqlite3.Connection, table: str, col: str) -> bool:
    return any(row[1] == col for row in conn.execute(f"PRAGMA table_info({table})").fetchall())


def _xid_dangling_count(
    source_conn: sqlite3.Connection,
    table: str,
    col: str,
    target_path: Path,
    target,
    scope: tuple[str, str] | None = None,
) -> int:
    source_conn.execute("ATTACH DATABASE ? AS xid_target_db", (str(target_path),))
    try:
        sql = (
            f"SELECT COUNT(*) FROM {table} t "
            f"LEFT JOIN xid_target_db.{target.table} tgt ON t.{col} = tgt.{target.pk_column} "
            f"WHERE t.{col} IS NOT NULL AND tgt.{target.pk_column} IS NULL"
        )
        params: tuple = ()
        if scope is not None:
            sql += f" AND t.{scope[0]} = ?"
            params = (scope[1],)
        row = source_conn.execute(sql, params).fetchone()
        return int(row[0])
    finally:
        source_conn.execute("DETACH DATABASE xid_target_db")


@register_check("xid_dangling", category="stores")
def check_xid_dangling(ctx: DoctorContext) -> CheckResult:
    from trialerror.stores.store import TABLE_DB

    paths_by_kind = {kind: _db_path(ctx, kind) for kind in _DB_KINDS}
    if any(p is None for p in paths_by_kind.values()):
        return CheckResult(
            name="xid_dangling",
            category="stores",
            status="skip",
            message="program_root not configured; cannot resolve ops/knowledge/jobs DB paths",
        )
    missing = {k: p for k, p in paths_by_kind.items() if not p.exists()}
    if missing:
        return CheckResult(
            name="xid_dangling",
            category="stores",
            status="skip",
            message=f"{len(missing)} DB file(s) not yet created: {sorted(missing)}",
            details={"missing": {k: str(v) for k, v in missing.items()}},
        )

    offenders: dict[str, int] = {}
    total = 0
    open_conns: dict[str, sqlite3.Connection] = {}
    program_id = _program_id(ctx)
    scoped_columns: list[str] = []
    try:
        for (table, col), target in XID_REGISTRY.items():
            source_kind = TABLE_DB.get(table)
            if source_kind is None:
                continue  # defensive; every registry table is a real table
            if source_kind not in open_conns:
                open_conns[source_kind] = connect(paths_by_kind[source_kind])
            # A platform table is shared across the account's programs; a reference
            # from it INTO a per-program store is only checkable for rows that belong
            # to THIS program (first seen live in the e2e, whose smoke and corpus
            # programs share one scratch platform: the smoke program's launch pointed
            # at the smoke program's session and read as dangling here).
            scope: tuple[str, str] | None = None
            if (
                source_kind == "platform"
                and target.db != "platform"
                and program_id is not None
                and _has_column(open_conns[source_kind], table, "program_id")
            ):
                scope = ("program_id", program_id)
                scoped_columns.append(f"{table}.{col}")
            count = _xid_dangling_count(
                open_conns[source_kind], table, col, paths_by_kind[target.db], target, scope=scope
            )
            if count:
                offenders[f"{table}.{col} -> {target.db}.{target.table}"] = count
                total += count
    finally:
        for conn in open_conns.values():
            conn.close()

    status = "fail" if total else "pass"
    message = (
        f"{total} dangling XID reference(s) across {len(offenders)} column(s)"
        if total
        else "no dangling XID references"
    )
    return CheckResult(
        name="xid_dangling",
        category="stores",
        status=status,
        message=message,
        details={"offenders": offenders, "scoped_to_program": program_id, "scoped_columns": scoped_columns},
    )


@register_check("anchors_dangling", category="stores")
def check_anchors_dangling(ctx: DoctorContext) -> CheckResult:
    """Design Section 4.1/F6: anchors whose ``doc_sha256`` no longer matches
    the current document's ``sha256`` (the document was re-normalized since
    the anchor was stamped). This is the SQL-comparable half of the
    ``anchors_dangling`` counter; the other half (``quote_sha256`` spot-
    resolve against the live ``stream_v1(doc)`` text) needs the ``stream_v1``
    function and normalizer outputs, which are M7's — this check reports
    what it can from schema alone and is designed to be extended, not
    replaced, once M7 lands (see build report deviations).

    Reported as ``warn`` (never ``fail``): staleness here is an expected,
    routine signal during a re-normalization window (design: "makes
    affected anchors stale by query, not by read-time surprise"), not a
    structural integrity violation the way a dangling XID is.
    """
    path = _db_path(ctx, "knowledge")
    if path is None or not path.exists():
        return CheckResult(
            name="anchors_dangling",
            category="stores",
            status="skip",
            message="knowledge.db not found (program_root not configured, or program not yet initialized)",
        )
    conn = connect(path, read_only=True)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM quote_anchor qa "
            "JOIN document d ON qa.doc_id = d.doc_id "
            "WHERE qa.doc_sha256 != d.sha256"
        ).fetchone()
    finally:
        conn.close()
    count = int(row[0])
    status = "warn" if count else "pass"
    message = (
        f"{count} anchor(s) stale (doc_sha256 mismatch vs. current document)"
        if count
        else "no stale anchors (doc_sha256 check)"
    )
    return CheckResult(
        name="anchors_dangling",
        category="stores",
        status=status,
        message=message,
        details={"doc_sha256_mismatches": count, "quote_sha256_spot_resolve": "not yet implemented (M7 scope)"},
    )
