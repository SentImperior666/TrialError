"""The L1 summary tier's doctor checks. Build brief: "doctor:
summaries_stale (docs newer than their summary)." Auto-discovered by
``trialerror.util.doctor.discover_and_register_checks`` purely because this
file lives at ``trialerror/summarize/checks.py`` -- zero shared-file edits
needed (the same convention ``trialerror/ingest/checks.py``'s own module
docstring documents).

Mirrors ``trialerror.stores.checks.check_anchors_dangling``'s shape exactly: a
read-only ``sqlite3.Connection`` opened directly against
``knowledge.db``, one plain SQL query, no full four-DB ``Store``. See
``trialerror.summarize.api.find_stale_or_missing_document_summaries``'s own
TRIALERROR-DEV-NOTE for why this is a deliberate re-statement of that
function's predicate rather than a call to it.
"""

from __future__ import annotations

from trialerror.stores import paths
from trialerror.stores.connection import connect
from trialerror.util.doctor import CheckResult, DoctorContext, register_check

__all__ = ["check_summaries_missing", "check_summaries_stale"]


def _knowledge_path(ctx: DoctorContext):
    if ctx.program_root is None:
        return None
    return paths.knowledge_db_path(ctx.program_root)


def _skip(name: str) -> CheckResult:
    return CheckResult(
        name=name,
        category="summarize",
        status="skip",
        message="knowledge.db not found (program_root not configured, or program not yet initialized)",
    )


@register_check("summaries_missing", category="summarize")
def check_summaries_missing(ctx: DoctorContext) -> CheckResult:
    """Documents with at least one ``element`` row (normalized -- there is
    something TO summarize) but no ``status='current'`` ``summary`` row at
    all yet."""
    path = _knowledge_path(ctx)
    if path is None or not path.exists():
        return _skip("summaries_missing")
    conn = connect(path, read_only=True)
    try:
        rows = conn.execute(
            """
            SELECT d.doc_id FROM document d
            WHERE EXISTS (SELECT 1 FROM element e WHERE e.doc_id = d.doc_id)
              AND NOT EXISTS (
                SELECT 1 FROM summary s
                WHERE s.subject_kind = 'document' AND s.subject_id = d.doc_id AND s.status = 'current'
              )
            """
        ).fetchall()
    finally:
        conn.close()
    count = len(rows)
    status = "warn" if count else "pass"
    message = f"{count} normalized document(s) with no summary yet" if count else "no documents missing a summary"
    return CheckResult(
        name="summaries_missing", category="summarize", status=status, message=message,
        details={"doc_ids": [r["doc_id"] for r in rows]},
    )


@register_check("summaries_stale", category="summarize")
def check_summaries_stale(ctx: DoctorContext) -> CheckResult:
    """Design brief: "summaries_stale (docs newer than their summary)" --
    documents whose CURRENT summary's ``subject_sha256`` no longer matches
    the document's CURRENT ``sha256`` (the document was re-normalized, OR
    re-chunked in a way that changed its normalized text, since the
    summary was generated). ``trialerror summarize run`` (or the ``summarize``
    job handler's auto-discovery) re-summarizes these -- the SAME
    predicate this check reports (see module docstring)."""
    path = _knowledge_path(ctx)
    if path is None or not path.exists():
        return _skip("summaries_stale")
    conn = connect(path, read_only=True)
    try:
        rows = conn.execute(
            """
            SELECT s.subject_id AS doc_id, s.summary_id FROM summary s
            JOIN document d ON s.subject_id = d.doc_id
            WHERE s.subject_kind = 'document' AND s.status = 'current' AND s.subject_sha256 != d.sha256
            """
        ).fetchall()
    finally:
        conn.close()
    count = len(rows)
    status = "warn" if count else "pass"
    message = f"{count} document summary(ies) stale (document content changed since generation)" if count else "no stale document summaries"
    return CheckResult(
        name="summaries_stale", category="summarize", status=status, message=message,
        details={"doc_ids": [r["doc_id"] for r in rows], "summary_ids": [r["summary_id"] for r in rows]},
    )
