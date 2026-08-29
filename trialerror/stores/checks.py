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
from pathlib import Path

from trialerror.stores import paths
from trialerror.stores.connection import connect
from trialerror.stores.migrate import current_version, latest_version
from trialerror.stores.store import SCHEMA_MODULES
from trialerror.stores.xid import XID_REGISTRY
from trialerror.util.doctor import CheckResult, DoctorContext, register_check

__all__ = ["check_store_schema_version", "check_xid_dangling", "check_anchors_dangling"]

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
    per_db: dict[str, dict] = {}
    mismatches: list[str] = []
    for db_kind in _DB_KINDS:
        path = _db_path(ctx, db_kind)
        if path is None or not path.exists():
            per_db[db_kind] = {"status": "skip", "reason": "database file not found"}
            continue
        conn = connect(path, read_only=True)
        try:
            current = current_version(conn)
        finally:
            conn.close()
        expected = latest_version(SCHEMA_MODULES[db_kind].MIGRATIONS)
        match = current == expected
        per_db[db_kind] = {"current_version": current, "expected_version": expected, "match": match}
        if not match:
            mismatches.append(f"{db_kind} (user_version={current}, expected={expected})")

    status = "fail" if mismatches else "pass"
    message = (
        f"{len(mismatches)} DB(s) not on the expected schema version: {', '.join(mismatches)}"
        if mismatches
        else "all present DB(s) on their expected schema version"
    )
    return CheckResult(
        name="store_schema_version", category="stores", status=status, message=message, details=per_db
    )


def _xid_dangling_count(source_conn: sqlite3.Connection, table: str, col: str, target_path: Path, target) -> int:
    source_conn.execute("ATTACH DATABASE ? AS xid_target_db", (str(target_path),))
    try:
        row = source_conn.execute(
            f"SELECT COUNT(*) FROM {table} t "
            f"LEFT JOIN xid_target_db.{target.table} tgt ON t.{col} = tgt.{target.pk_column} "
            f"WHERE t.{col} IS NOT NULL AND tgt.{target.pk_column} IS NULL"
        ).fetchone()
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
    try:
        for (table, col), target in XID_REGISTRY.items():
            source_kind = TABLE_DB.get(table)
            if source_kind is None:
                continue  # defensive; every registry table is a real table
            if source_kind not in open_conns:
                open_conns[source_kind] = connect(paths_by_kind[source_kind])
            count = _xid_dangling_count(
                open_conns[source_kind], table, col, paths_by_kind[target.db], target
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
        name="xid_dangling", category="stores", status=status, message=message, details={"offenders": offenders}
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
