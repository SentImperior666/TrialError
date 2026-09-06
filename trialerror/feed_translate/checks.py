"""The feed translator's doctor checks:

- ``feed_translation_failures`` -- how many CURRENT translations the
  faithfulness gate withheld. A withheld translation is invisible in the
  dashboard by design (the original stays), so without this check a
  systematically broken translator would look exactly like a translator
  nobody had run yet. This is the number that makes fail-closed
  observable rather than silent.
- ``feed_translations_stale`` -- current translations generated under an
  older ``translator_version`` than the one the code now ships
  (``docs/reviews/AISPEAK_TRANSLATOR_DESIGN.md`` Section 5 step 8, the
  ``summaries_stale`` mirror). ``feed_post`` is append-only, so a
  deliberate version bump after a style-contract fix is the ONLY thing
  that can make a translation stale -- there is no mutable original to
  drift against.

Auto-discovered by ``trialerror.util.doctor.discover_and_register_checks``
purely because this file lives at ``trialerror/feed_translate/checks.py`` --
dropping the file is the whole registration step, no shared file touched
(the convention ``trialerror/events/checks.py`` and
``trialerror/summarize/checks.py`` both document).

Shape follows ``trialerror.events.checks`` exactly: one read-only
``sqlite3.Connection`` against ``ops.db``, one plain SQL query, no full
four-DB :class:`~trialerror.stores.store.Store`. See
:func:`trialerror.feed_translate.api.count_gate_failures` for the same
predicate stated in prose, and
``trialerror.summarize.api.find_stale_or_missing_document_summaries``'s
TRIALERROR-DEV-NOTE for why the two implementations are deliberate rather
than an oversight.
"""

from __future__ import annotations

import sqlite3

from trialerror.feed_translate.api import CURRENT_TRANSLATOR_VERSION
from trialerror.stores import paths
from trialerror.stores.connection import connect
from trialerror.util.doctor import CheckResult, DoctorContext, register_check

__all__ = ["check_feed_translation_failures", "check_feed_translations_stale"]

_CATEGORY = "feed_translate"


def _open_ops(ctx: DoctorContext, name: str) -> tuple[sqlite3.Connection | None, CheckResult | None]:
    if ctx.program_root is None:
        return None, CheckResult(
            name=name, category=_CATEGORY, status="skip",
            message="program_root not configured; cannot resolve ops.db path",
        )
    path = paths.ops_db_path(ctx.program_root)
    if not path.exists():
        return None, CheckResult(
            name=name, category=_CATEGORY, status="skip",
            message="ops.db not found (program not yet initialized)",
        )
    conn = connect(path, read_only=True)
    if not _table_exists(conn, "feed_post_translation"):
        conn.close()
        return None, CheckResult(
            name=name, category=_CATEGORY, status="skip",
            message="feed_post_translation not present (ops.db predates schema v4)",
        )
    if not _column_exists(conn, "feed_post_translation", "gate_status"):
        conn.close()
        return None, CheckResult(
            name=name, category=_CATEGORY, status="skip",
            message="feed_post_translation.gate_status not present (ops.db predates schema v6)",
        )
    return conn, None


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})").fetchall())


@register_check("feed_translation_failures", category=_CATEGORY)
def check_feed_translation_failures(ctx: DoctorContext) -> CheckResult:
    """Current translations withheld by the faithfulness gate
    (``gate_status='fail'``). ``warn``, never ``fail``: a withheld
    translation is the guard WORKING -- the operator still sees the
    original, nothing is corrupted, and the correct response is to read
    the reasons and fix the translator, not to treat the program as
    broken."""
    conn, skip = _open_ops(ctx, "feed_translation_failures")
    if skip is not None:
        return skip
    assert conn is not None
    try:
        rows = conn.execute(
            "SELECT translation_id, post_id, gate_reasons FROM feed_post_translation "
            "WHERE status = 'current' AND gate_status = 'fail' ORDER BY created_ts DESC"
        ).fetchall()
    finally:
        conn.close()

    count = len(rows)
    status = "warn" if count else "pass"
    message = (
        f"{count} current translation(s) withheld by the faithfulness gate "
        "(the original post is still shown -- read gate_reasons and re-translate)"
        if count
        else "no translations withheld by the faithfulness gate"
    )
    return CheckResult(
        name="feed_translation_failures", category=_CATEGORY, status=status, message=message,
        details={
            "count": count,
            "translation_ids": [r["translation_id"] for r in rows],
            "post_ids": [r["post_id"] for r in rows],
        },
    )


@register_check("feed_translations_stale", category=_CATEGORY)
def check_feed_translations_stale(ctx: DoctorContext) -> CheckResult:
    """Current translations generated under a ``translator_version`` older
    than :data:`trialerror.feed_translate.api.CURRENT_TRANSLATOR_VERSION` --
    re-run ``trialerror feed translate --pending`` after a version bump to
    clear them."""
    conn, skip = _open_ops(ctx, "feed_translations_stale")
    if skip is not None:
        return skip
    assert conn is not None
    try:
        rows = conn.execute(
            "SELECT translation_id, post_id, translator_version FROM feed_post_translation "
            "WHERE status = 'current' AND translator_version != ?",
            (CURRENT_TRANSLATOR_VERSION,),
        ).fetchall()
    finally:
        conn.close()

    count = len(rows)
    status = "warn" if count else "pass"
    message = (
        f"{count} current translation(s) predate translator_version "
        f"{CURRENT_TRANSLATOR_VERSION!r} (style contract changed since they were generated)"
        if count
        else f"every current translation is at translator_version {CURRENT_TRANSLATOR_VERSION!r}"
    )
    return CheckResult(
        name="feed_translations_stale", category=_CATEGORY, status=status, message=message,
        details={
            "count": count,
            "translation_ids": [r["translation_id"] for r in rows],
            "post_ids": [r["post_id"] for r in rows],
        },
    )
