"""The law service's public API. Design Section 12 (M4 row): "append+digest
atomic, hash chain, pin verify, foreign diff, rendered LAW_DIGEST.md."
Design Section 4.2: "Lockstep is structural: ``trialerror law append`` writes
the ruling AND regenerates the digest in one transaction; there is no API
that does one without the other. ``trialerror law verify --pin vNN@date`` is
what hooks call; mismatch = refusal."

TRIALERROR-DEV-NOTE (transaction mechanics, in-lane): ``trialerror.stores.writer.
insert()`` commits per call (``with conn:`` internally) — calling it twice
in a row for ``ruling`` then ``law_digest`` would NOT be one transaction, so
:func:`append_ruling` does not use it for those two writes. Instead it
reuses ``trialerror.stores.writer.table_columns`` (read-only, already exported)
for the same unknown-column validation and issues its own parameterized
INSERTs under an explicit ``BEGIN IMMEDIATE`` / ``COMMIT`` / ``ROLLBACK``
block — the exact pattern ``trialerror.stores.migrate.apply_migrations`` already
uses and tests, for the same reason (DDL/DML that must land as one unit
under Python sqlite3's legacy transaction control). Neither ``ruling`` nor
``law_digest`` has an XID column (confirmed against ``trialerror.stores.xid.
XID_REGISTRY``), so no cross-store validation is skipped by this route.
The rendered markdown file is written to disk with ``atomic_write_text``
AFTER that DB transaction commits: a filesystem write cannot itself be
part of a SQLite transaction, so this is the one place "atomic" means
"the two DB rows are one transaction; the file is a re-derivable view of
them, written best-effort right after" rather than one indivisible unit
across both storage systems — recoverable via ``render_current_digest_to_
disk`` / ``trialerror law digest --render`` if a crash lands between the two.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from trialerror.law.chain import GENESIS_HASH, ChainVerifyResult, compute_ledger_hash, verify_chain
from trialerror.law.digest import digest_sha256, render_digest
from trialerror.stores.errors import ValidationError
from trialerror.stores.store import Store
from trialerror.stores.writer import table_columns
from trialerror.util.atomic import atomic_write_text
from trialerror.util.config import configured_path_value
from trialerror.util.timeutil import now

__all__ = [
    "RENDERED_PATH",
    "AppendResult",
    "append_ruling",
    "lookup_rulings",
    "get_current_digest",
    "RenderResult",
    "render_current_digest_to_disk",
    "PinVerifyResult",
    "verify_pin",
    "current_pin",
    "diff_foreign",
    "format_pin",
    "parse_pin",
]

#: Design Section 3.2 per-program scaffold: "law/LAW_DIGEST.md". Relative
#: to ``store.program_root`` — stored in the DB as this same relative
#: string (matches ``tests/_store_fixtures.py``'s hand-seeded row).
RENDERED_PATH = "law/LAW_DIGEST.md"

_RULING_ID_RE = re.compile(r"^C-(\d+)$")
_DIGEST_VERSION_RE = re.compile(r"^v(\d+)$")
_PIN_RE = re.compile(r"^(v\d+)@(\d{4}-\d{2}-\d{2})$")


def _raw_insert(conn: sqlite3.Connection, table: str, row: Mapping[str, Any]) -> None:
    """Same unknown-column validation as ``trialerror.stores.writer.insert``,
    minus the auto-commit — see TRIALERROR-DEV-NOTE above for why this module
    needs that split."""
    columns = table_columns(conn, table)
    unknown = set(row) - columns
    if unknown:
        raise ValidationError(f"{table}: unknown column(s) {sorted(unknown)!r} (not in {sorted(columns)!r})")
    cols = list(row.keys())
    placeholders = ",".join("?" for _ in cols)
    sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
    conn.execute(sql, [row[c] for c in cols])


def _next_ruling_id(conn: sqlite3.Connection) -> str:
    """'C-####' style per program (Design Sec 4.2 DDL comment on
    ``ruling_id``) — sequential, derived as max-existing-suffix + 1 rather
    than a row COUNT, so it stays correct even if rows were seeded out of
    band (e.g. a origin-project-migration import under legacy prefixes, Sec 11 v1)."""
    rows = conn.execute("SELECT ruling_id FROM ruling").fetchall()
    max_n = 0
    for r in rows:
        m = _RULING_ID_RE.match(r["ruling_id"])
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"C-{max_n + 1:04d}"


def _next_digest_version(conn: sqlite3.Connection) -> str:
    """'vNN' style (matches the real ledger's ``corrections_pin: v64@...``
    convention — no zero-padding)."""
    rows = conn.execute("SELECT version FROM law_digest").fetchall()
    max_n = 0
    for r in rows:
        m = _DIGEST_VERSION_RE.match(r["version"])
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"v{max_n + 1}"


def format_pin(version: str, generated_ts: str) -> str:
    """``'vNN@YYYY-MM-DD'`` — the exact shape named in Design Sec 4.2
    (``trialerror law verify --pin vNN@date``) and matching the real origin-project ledger's
    ``corrections_pin: v64@2026-08-29`` convention."""
    return f"{version}@{generated_ts[:10]}"


def parse_pin(pin: str) -> tuple[str, str]:
    """Split a pin string into ``(version, date)``. Raises ``ValueError``
    on anything not matching ``vNN@YYYY-MM-DD`` — the "pin-format check"
    named in the build brief reuses this same regex (see
    ``trialerror/law/checks.py``)."""
    m = _PIN_RE.match(pin or "")
    if not m:
        raise ValueError(f"malformed pin (expected 'vNN@YYYY-MM-DD'): {pin!r}")
    return m.group(1), m.group(2)


@dataclass
class AppendResult:
    ruling_id: str
    ruling: dict
    digest_version: str
    digest: dict
    pin: str
    ledger_sha256_after: str
    rendered_path: str
    rendered_abs_path: str

    def to_dict(self) -> dict:
        return {
            "ruling_id": self.ruling_id,
            "ruling": dict(self.ruling),
            "digest_version": self.digest_version,
            "digest": dict(self.digest),
            "pin": self.pin,
            "ledger_sha256_after": self.ledger_sha256_after,
            "rendered_path": self.rendered_path,
            "rendered_abs_path": self.rendered_abs_path,
        }


def append_ruling(
    store: Store,
    *,
    summary: str,
    ts: str | None = None,
    verbatim_quote: str | None = None,
    standing_clauses: Sequence[str] | None = None,
    domains: Sequence[str] | None = None,
    supersedes: str | None = None,
    supersedes_note: str | None = None,
    render_to_disk: bool = True,
    config: Mapping[str, Any] | None = None,
) -> AppendResult:
    """THE one way to add law to the ledger. Design Sec 4.2: "there is no
    API that does one without the other" — this function is the entire
    public surface for creating a ``ruling`` row; nothing in ``trialerror.law``
    lets a caller insert one without the paired digest bump landing in the
    same ops.db transaction (see module TRIALERROR-DEV-NOTE for the transaction
    mechanics and the DB-vs-file boundary of "atomic").

    ``verbatim_quote`` is nullable (F20(c): real ledgers hold summary-only
    entries); ``supersedes`` names an existing ACTIVE ruling to flip to
    'superseded' as part of this same transaction, or leave both
    ``supersedes``/``supersedes_note`` unset for a fresh standing ruling
    that supersedes nothing. ``supersedes_note`` may be given even without
    ``supersedes`` (F20(c): real supersession targets are often prose, not
    another ruling row — e.g. "all '18-game' corpus figures").

    ``config`` is the plain ``ProgramConfig.raw`` dict (or ``None``, the
    default — identical to every pre-existing caller's behavior);
    ``[paths].law_digest_path`` overrides :data:`RENDERED_PATH` for BOTH
    the ``law_digest.rendered_path`` value stored this call AND the file
    actually written (the import-design notes (internal, not in this export) Sec 5 knob #2). A later
    :func:`render_current_digest_to_disk`/:func:`get_current_digest` needs
    no ``config`` of its own — it reads the already-resolved path back out
    of the stored row, the same way ``rendered_path`` has always worked.
    """
    if not summary or not summary.strip():
        raise ValueError("append_ruling: summary is required and must be non-empty")
    if supersedes is not None and not supersedes.strip():
        raise ValueError("append_ruling: supersedes, if given, must be non-empty")

    ts = ts or now()
    standing_clauses_json = json.dumps(list(standing_clauses or []), ensure_ascii=False)
    domains_json = json.dumps(list(domains or []), ensure_ascii=False)

    conn = store.ops
    conn.execute("BEGIN IMMEDIATE")
    try:
        ruling_id = _next_ruling_id(conn)
        prev_row = conn.execute(
            "SELECT ledger_sha256_after FROM ruling ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        prev_hash = prev_row["ledger_sha256_after"] if prev_row is not None else GENESIS_HASH

        ruling_row: dict[str, Any] = {
            "ruling_id": ruling_id,
            "ts": ts,
            "verbatim_quote": verbatim_quote,
            "summary": summary,
            "standing_clauses": standing_clauses_json,
            "domains": domains_json,
            "supersedes": supersedes,
            "supersedes_note": supersedes_note,
            "status": "active",
        }
        ledger_sha256_after = compute_ledger_hash(prev_hash, ruling_row)
        ruling_row["ledger_sha256_after"] = ledger_sha256_after

        _raw_insert(conn, "ruling", ruling_row)

        if supersedes:
            cur = conn.execute(
                "UPDATE ruling SET status = 'superseded' WHERE ruling_id = ? AND status = 'active'",
                (supersedes,),
            )
            if cur.rowcount == 0:
                raise ValidationError(
                    f"append_ruling: supersedes={supersedes!r} does not name an existing active ruling"
                )

        active_rows = conn.execute(
            "SELECT * FROM ruling WHERE status = 'active' ORDER BY rowid"
        ).fetchall()
        active = [dict(r) for r in active_rows]

        digest_version = _next_digest_version(conn)
        rendered_text = render_digest(active, version=digest_version, generated_ts=ts)
        content_sha256 = digest_sha256(rendered_text)

        rendered_path = configured_path_value(config, "law_digest_path", RENDERED_PATH)
        digest_row: dict[str, Any] = {
            "version": digest_version,
            "generated_ts": ts,
            "content_sha256": content_sha256,
            "rendered_path": rendered_path,
        }
        _raw_insert(conn, "law_digest", digest_row)

        conn.execute("COMMIT")
    except sqlite3.IntegrityError as exc:
        conn.execute("ROLLBACK")
        raise ValidationError(f"append_ruling: integrity violation: {exc}") from exc
    except Exception:
        conn.execute("ROLLBACK")
        raise

    rendered_abs_path = store.program_root / rendered_path
    if render_to_disk:
        atomic_write_text(rendered_abs_path, rendered_text)

    pin = format_pin(digest_version, ts)
    return AppendResult(
        ruling_id=ruling_id,
        ruling=dict(ruling_row),
        digest_version=digest_version,
        digest=dict(digest_row),
        pin=pin,
        ledger_sha256_after=ledger_sha256_after,
        rendered_path=rendered_path,
        rendered_abs_path=str(rendered_abs_path),
    )


def lookup_rulings(
    store: Store,
    *,
    ruling_id: str | None = None,
    domain: str | None = None,
    status: str | None = None,
    query: str | None = None,
) -> list[dict]:
    """Filtered read over the ruling ledger, in append order. All filters
    are optional and AND together; ``domain`` matches against the JSON
    ``domains`` array (substring match on its serialized form — a full
    JSON-aware query is retrieval-engine territory, out of M4 scope)."""
    conn = store.ops
    sql = "SELECT * FROM ruling WHERE 1=1"
    params: list[Any] = []
    if ruling_id:
        sql += " AND ruling_id = ?"
        params.append(ruling_id)
    if status:
        sql += " AND status = ?"
        params.append(status)
    if domain:
        sql += " AND domains LIKE ?"
        params.append(f'%"{domain}"%')
    if query:
        sql += " AND (summary LIKE ? OR verbatim_quote LIKE ?)"
        like = f"%{query}%"
        params.extend([like, like])
    sql += " ORDER BY rowid"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_current_digest(store: Store) -> dict | None:
    """The latest ``law_digest`` row (highest ``vNN``), or ``None`` if no
    ruling has ever been appended in this program."""
    conn = store.ops
    rows = conn.execute("SELECT * FROM law_digest").fetchall()
    if not rows:
        return None

    def _n(row: sqlite3.Row) -> int:
        m = _DIGEST_VERSION_RE.match(row["version"])
        return int(m.group(1)) if m else -1

    latest = max(rows, key=_n)
    return dict(latest)


def current_pin(store: Store) -> str | None:
    """The pin string a fresh boot should stamp (Design Sec 4.2's
    ``session.boot_pin_version``) — ``None`` if nothing has been appended
    yet."""
    digest = get_current_digest(store)
    if digest is None:
        return None
    return format_pin(digest["version"], digest["generated_ts"])


@dataclass
class RenderResult:
    version: str
    rendered_path: str
    rendered_abs_path: str
    matches_stored_hash: bool
    content_sha256: str
    stored_content_sha256: str

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "rendered_path": self.rendered_path,
            "rendered_abs_path": self.rendered_abs_path,
            "matches_stored_hash": self.matches_stored_hash,
            "content_sha256": self.content_sha256,
            "stored_content_sha256": self.stored_content_sha256,
        }


def render_current_digest_to_disk(store: Store) -> RenderResult:
    """Re-render the file for the CURRENT digest version from ops.db truth
    without creating a new ruling or digest version — a pure "flush the
    view to disk" recovery op (``trialerror law digest --render``): the file
    got deleted, or a process died between ``append_ruling``'s DB commit
    and its file write. Does not mutate any row (Design Sec 3.2: rendered
    files are views "regenerated by the API that mutates the canonical
    row" — this is the read-only counterpart used when nothing needs to
    change in the DB, only the view needs to catch up to it)."""
    digest = get_current_digest(store)
    if digest is None:
        raise ValueError(
            "render_current_digest_to_disk: no law_digest rows exist yet (append a ruling first)"
        )
    conn = store.ops
    active = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM ruling WHERE status = 'active' ORDER BY rowid"
        ).fetchall()
    ]
    text = render_digest(active, version=digest["version"], generated_ts=digest["generated_ts"])
    computed = digest_sha256(text)
    rendered_abs_path = store.program_root / digest["rendered_path"]
    atomic_write_text(rendered_abs_path, text)
    return RenderResult(
        version=digest["version"],
        rendered_path=digest["rendered_path"],
        rendered_abs_path=str(rendered_abs_path),
        matches_stored_hash=(computed == digest["content_sha256"]),
        content_sha256=computed,
        stored_content_sha256=digest["content_sha256"],
    )


@dataclass
class PinVerifyResult:
    valid: bool
    given_pin: str | None
    current_pin: str | None
    pin_stale: bool
    chain_ok: bool
    reason: str
    chain_detail: str = ""

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "given_pin": self.given_pin,
            "current_pin": self.current_pin,
            "pin_stale": self.pin_stale,
            "chain_ok": self.chain_ok,
            "reason": self.reason,
            "chain_detail": self.chain_detail,
        }


def verify_pin(store: Store, pin: str | None) -> PinVerifyResult:
    """*** THE integration point ***

    Design Sec 5.4 (``PreToolUse: Task`` / ``spawn_gate.py``): "the same
    check verifies model_class against model policy for the stated
    purpose, and law-pin freshness. Any failure ... -> exit 2 (spawn
    REFUSED)." Design Sec 4.2 / 9.1: "``trialerror law verify --pin vNN@date``
    is what hooks call; mismatch = refusal."

    Contract for M3 (``plugin/hooks/spawn_gate.py``) and M6 (session
    boot/close): call ``trialerror.law.verify_pin(store, pin=<the pin the
    caller is holding>)`` — typically the open session's
    ``session.boot_pin_version`` (a same-file column on ``ops.session``,
    read by the caller; this function takes the pin as a plain string and
    has no dependency on ``trialerror.sessions``, keeping the law/sessions
    subsystem boundary from Design Sec 2's table intact). A hook translates
    ``PinVerifyResult(valid=False, ...)`` into its own refusal (spawn_gate:
    exit 2 with the ``.reason`` string in its message; session close/Stop:
    block-once per Sec 5.4). This function is stateless, read-only, and
    cheap (one chain walk over what is expected to be a small ledger) —
    safe to call at every spawn without caching, which is what makes
    spawn-time enforcement possible without a live daemon.

    Checks BOTH freshness (the given pin matches the current digest
    version+date) AND integrity (the hash chain over the ruling ledger is
    unbroken) — "law-pin freshness" alone would miss a tampered-but-not-
    stale ledger, and Design Sec 12's M4 acceptance list names both
    "stale pin fails law verify" and "tampered ledger detected by chain
    verify" as things THIS module must catch; folding chain verification
    into the one function hooks actually call is what makes the tamper
    check load-bearing rather than a check nobody invokes.

    Mid-flight staleness (Design Sec 5.4, F16): a concurrent append after
    a subagent is already spawned is deliberately NOT re-checked inside
    that running subagent — this function is only ever called at discrete
    checkpoints (spawn gate, boot, close), never injected into a live
    subagent's every tool call, so mid-flight drift stays visible-not-
    refused by construction, not by any special-casing here.
    """
    digest = get_current_digest(store)
    chain_result: ChainVerifyResult = verify_chain(store.ops)

    if digest is None:
        return PinVerifyResult(
            valid=False,
            given_pin=pin,
            current_pin=None,
            pin_stale=True,
            chain_ok=chain_result.ok,
            reason="no_law_digest_exists (append a ruling before any spawn can be trusted)",
            chain_detail=chain_result.detail,
        )

    current = format_pin(digest["version"], digest["generated_ts"])

    if not pin:
        return PinVerifyResult(
            valid=False,
            given_pin=pin,
            current_pin=current,
            pin_stale=True,
            chain_ok=chain_result.ok,
            reason="no_pin_given",
            chain_detail=chain_result.detail,
        )

    try:
        parse_pin(pin)
    except ValueError:
        return PinVerifyResult(
            valid=False,
            given_pin=pin,
            current_pin=current,
            pin_stale=True,
            chain_ok=chain_result.ok,
            reason=f"malformed_pin: {pin!r} (expected 'vNN@YYYY-MM-DD')",
            chain_detail=chain_result.detail,
        )

    pin_stale = pin != current
    valid = (not pin_stale) and chain_result.ok

    reasons: list[str] = []
    if pin_stale:
        reasons.append(f"stale pin: given {pin!r}, current is {current!r}")
    if not chain_result.ok:
        reasons.append(f"chain tampered: {chain_result.detail}")
    reason = "; ".join(reasons) if reasons else "pin current and chain intact"

    return PinVerifyResult(
        valid=valid,
        given_pin=pin,
        current_pin=current,
        pin_stale=pin_stale,
        chain_ok=chain_result.ok,
        reason=reason,
        chain_detail=chain_result.detail,
    )


def diff_foreign(store: Store, pin: str) -> list[dict]:
    """Rulings appended strictly after the pin's digest version was
    generated (Design Sec 9.1: "diff-foreign surfaces other-session
    appends since your pin"). Boundary is ``ruling.ts`` compared against
    that ``law_digest.generated_ts`` — ``append_ruling`` always stamps
    both the new ruling row and its paired digest bump with the SAME
    ``ts`` in one call, so this is exact for anything appended through
    this module's API, without needing a stored FK from digest to ruling
    (``law_digest`` has no such column in Design Sec 4.2's DDL)."""
    version, _date = parse_pin(pin)
    conn = store.ops
    row = conn.execute("SELECT generated_ts FROM law_digest WHERE version = ?", (version,)).fetchone()
    if row is None:
        raise ValueError(f"diff_foreign: no law_digest row for pin version {version!r}")
    threshold_ts = row["generated_ts"]
    rows = conn.execute("SELECT * FROM ruling WHERE ts > ? ORDER BY rowid", (threshold_ts,)).fetchall()
    return [dict(r) for r in rows]
