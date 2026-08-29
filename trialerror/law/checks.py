"""M4's doctor checks. Build brief: "doctor = trialerror/law/checks.py with
@register_check (e.g. digest-lockstep check: digest hash matches ledger
state; pin-format check)." Auto-discovered by
``trialerror.util.doctor.discover_and_register_checks`` exactly like every other
subsystem's ``checks.py`` (M1's ``store_schema_version``/``xid_dangling``/
``anchors_dangling``, M0's ``license_audit``) — dropping this file is the
entire registration step, no shared file touched.

Uses ``DoctorContext.program_root`` (an M0-owned field) to resolve ops.db,
same convention as ``trialerror/stores/checks.py``; any DB file that doesn't
exist yet, or a program that has never appended a ruling, is reported
``skip`` — not a doctor failure.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

from trialerror.law.chain import verify_chain
from trialerror.law.digest import digest_sha256, render_digest
from trialerror.stores import paths
from trialerror.stores.connection import connect
from trialerror.util.doctor import CheckResult, DoctorContext, register_check

__all__ = ["check_law_digest_lockstep", "check_law_chain_integrity", "check_law_pin_format"]

_DIGEST_VERSION_RE = re.compile(r"^v(\d+)$")
_PIN_RE = re.compile(r"^v\d+@\d{4}-\d{2}-\d{2}$")


def _ops_conn_or_none(ctx: DoctorContext) -> sqlite3.Connection | None:
    if ctx.program_root is None:
        return None
    path = paths.ops_db_path(ctx.program_root)
    if not path.exists():
        return None
    return connect(path, read_only=True)


def _latest_digest_row(conn: sqlite3.Connection) -> sqlite3.Row | None:
    rows = conn.execute("SELECT * FROM law_digest").fetchall()
    if not rows:
        return None

    def _n(row: sqlite3.Row) -> int:
        m = _DIGEST_VERSION_RE.match(row["version"])
        return int(m.group(1)) if m else -1

    return max(rows, key=_n)


@register_check("law_digest_lockstep", category="law")
def check_law_digest_lockstep(ctx: DoctorContext) -> CheckResult:
    """Recomputes the digest's rendered markdown from the CURRENT active
    ``ruling`` rows (at the latest digest version's stamped version/
    generated_ts) and compares its sha256 against both (a) the
    ``law_digest.content_sha256`` the DB itself recorded, and (b) the
    on-disk rendered file's actual byte hash. (a) catches ledger rows
    mutated after the fact outside ``append_ruling`` (bypassing the
    validated write API — the same class of adversarial write
    ``xid_dangling`` plants in its own tests); (b) catches a hand-edited
    ``LAW_DIGEST.md`` (Design Sec 4: "rendered ... never hand-edited")."""
    conn = _ops_conn_or_none(ctx)
    if conn is None:
        return CheckResult(
            name="law_digest_lockstep",
            category="law",
            status="skip",
            message="ops.db not found (program_root not configured, or program not yet initialized)",
        )
    try:
        latest = _latest_digest_row(conn)
        if latest is None:
            return CheckResult(
                name="law_digest_lockstep",
                category="law",
                status="skip",
                message="no law_digest rows yet (no ruling has ever been appended)",
            )
        active = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM ruling WHERE status = 'active' ORDER BY rowid"
            ).fetchall()
        ]
        recomputed_text = render_digest(active, version=latest["version"], generated_ts=latest["generated_ts"])
        recomputed_hash = digest_sha256(recomputed_text)
        db_ok = recomputed_hash == latest["content_sha256"]

        rendered_abs = Path(ctx.program_root) / latest["rendered_path"]
        file_exists = rendered_abs.is_file()
        file_hash = hashlib.sha256(rendered_abs.read_bytes()).hexdigest() if file_exists else None
        file_ok = file_exists and file_hash == latest["content_sha256"]

        details = {
            "version": latest["version"],
            "stored_content_sha256": latest["content_sha256"],
            "recomputed_content_sha256": recomputed_hash,
            "db_lockstep_ok": db_ok,
            "rendered_path": latest["rendered_path"],
            "file_exists": file_exists,
            "file_content_sha256": file_hash,
            "file_lockstep_ok": file_ok,
        }
        status = "pass" if (db_ok and file_ok) else "fail"
        if status == "pass":
            message = f"digest {latest['version']} matches ledger state and on-disk file"
        else:
            problems = []
            if not db_ok:
                problems.append("stored content_sha256 does not match a re-render of the current ledger")
            if not file_exists:
                problems.append(f"rendered file missing at {rendered_abs}")
            elif not file_ok:
                problems.append("on-disk file's hash does not match law_digest.content_sha256 (hand-edited?)")
            message = "; ".join(problems)
        return CheckResult(name="law_digest_lockstep", category="law", status=status, message=message, details=details)
    finally:
        conn.close()


@register_check("law_chain_integrity", category="law")
def check_law_chain_integrity(ctx: DoctorContext) -> CheckResult:
    """Tamper-evidence over the append log itself (Design Sec 4.2:
    ``ledger_sha256_after``, "hash chain over the append sequence") —
    independent of ``law_digest_lockstep`` above: a ledger could be
    tampered in a way that still happens to re-render to a matching digest
    hash (e.g. an edited-then-reverted-summary row whose superseded
    neighbor was also touched) only if EVERY active ruling's exact bytes
    are reproduced; the chain instead protects the full append history,
    including superseded rows the digest no longer renders at all."""
    conn = _ops_conn_or_none(ctx)
    if conn is None:
        return CheckResult(
            name="law_chain_integrity",
            category="law",
            status="skip",
            message="ops.db not found (program_root not configured, or program not yet initialized)",
        )
    try:
        result = verify_chain(conn)
        status = "pass" if result.ok else "fail"
        return CheckResult(
            name="law_chain_integrity",
            category="law",
            status=status,
            message=result.detail,
            details=result.to_dict(),
        )
    finally:
        conn.close()


@register_check("law_pin_format", category="law")
def check_law_pin_format(ctx: DoctorContext) -> CheckResult:
    """Scans persisted pin-shaped values for the ``'vNN@YYYY-MM-DD'`` shape
    (Design Sec 4.2 / ``trialerror.law.service.parse_pin``): every non-null
    ``session.boot_pin_version`` in this program's ops.db must parse. A
    malformed stored pin would make ``verify_pin`` refuse every spawn for
    that session with a confusing "malformed_pin" reason instead of the
    intended "stale pin" one — this check catches the write-time bug
    instead of surfacing it only at the next spawn attempt."""
    conn = _ops_conn_or_none(ctx)
    if conn is None:
        return CheckResult(
            name="law_pin_format",
            category="law",
            status="skip",
            message="ops.db not found (program_root not configured, or program not yet initialized)",
        )
    try:
        rows = conn.execute(
            "SELECT session_id, boot_pin_version FROM session WHERE boot_pin_version IS NOT NULL"
        ).fetchall()
        offenders = {r["session_id"]: r["boot_pin_version"] for r in rows if not _PIN_RE.match(r["boot_pin_version"])}
        status = "fail" if offenders else "pass"
        message = (
            f"{len(offenders)} session(s) with a malformed boot_pin_version"
            if offenders
            else f"all {len(rows)} recorded boot_pin_version value(s) well-formed"
        )
        return CheckResult(
            name="law_pin_format", category="law", status=status, message=message, details={"offenders": offenders}
        )
    finally:
        conn.close()
