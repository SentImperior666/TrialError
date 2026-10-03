"""Readers for knowledge.db's five citation-mapper kinds (design Section 1:
"the citation mapper's five namespaces ... SRC, DOC, ELM, CHK and ANC"). ``ELM`` uses
the generic reader (``base.PREFIX_TABLE``); SRC, DOC, CHK and ANC get their
own joins, per design Section 2's worked examples.
"""

from __future__ import annotations

from trialerror.resolve.base import Description, register

_TITLE_LIMIT = 120


def _row(conn, table: str, pk_column: str, pk_value: str) -> dict | None:
    if conn is None:
        return None
    row = conn.execute(f"SELECT * FROM {table} WHERE {pk_column} = ?", (pk_value,)).fetchone()
    return dict(row) if row is not None else None


def describe_src(stores, id_: str) -> Description:
    row = _row(stores.knowledge, "source", "source_id", id_)
    if row is None:
        return Description(id=id_, kind="SRC", kind_words="a source", found=False, store="knowledge" if stores.knowledge is not None else None)
    state = f"{row['request_state']} ({row['license_tier']})"
    return Description(
        id=id_, kind="SRC", kind_words="a source", title=row.get("title"), state_words=state,
        # Section 2: "purpose is built from what the store knows. When it
        # knows nothing, it says so" -- ``source`` (docs/OPERATOR_GUIDE.md,
        # ``_acquisition_items``' own note) carries no purpose column.
        purpose="no purpose is recorded for this source", found=True, store="knowledge",
    )


def describe_doc(stores, id_: str) -> Description:
    row = _row(stores.knowledge, "document", "doc_id", id_)
    if row is None:
        return Description(id=id_, kind="DOC", kind_words="a document", found=False, store="knowledge" if stores.knowledge is not None else None)
    related: list[tuple[str, str, str]] = []
    src = _row(stores.knowledge, "source", "source_id", row.get("source_id") or "")
    if src is not None:
        related.append((row["source_id"], "a source", src.get("title") or row["source_id"]))
    return Description(
        id=id_, kind="DOC", kind_words="a document", title=row.get("rel_path"), state_words=row.get("status"),
        purpose=f"from the source {src['title']!r}" if src else "no source is recorded for this document",
        related=related, found=True, store="knowledge",
    )


def describe_chk(stores, id_: str) -> Description:
    """Design note: "CHK: page range, document path, source title." — the exact
    three facts a bare chunk id gives no hint of."""
    row = _row(stores.knowledge, "chunk", "chunk_id", id_)
    if row is None:
        return Description(id=id_, kind="CHK", kind_words="a document chunk", found=False, store="knowledge" if stores.knowledge is not None else None)
    doc = _row(stores.knowledge, "document", "doc_id", row.get("doc_id") or "")
    src = _row(stores.knowledge, "source", "source_id", doc.get("source_id") or "") if doc else None
    pages = None
    if row.get("page_start") is not None:
        pages = f"page {row['page_start']}" if row["page_start"] == row.get("page_end") else f"pages {row['page_start']}-{row.get('page_end')}"
    title = (row.get("text") or "")[:_TITLE_LIMIT].strip() or None
    related: list[tuple[str, str, str]] = []
    if doc is not None:
        related.append((row["doc_id"], "a document", doc.get("rel_path") or row["doc_id"]))
    if src is not None:
        related.append((doc["source_id"], "a source", src.get("title") or doc["source_id"]))
    purpose_bits = [b for b in (pages, doc.get("rel_path") if doc else None, src.get("title") if src else None) if b]
    purpose = ("a chunk of " + ", ".join(purpose_bits)) if purpose_bits else "no document is recorded for this chunk"
    return Description(
        id=id_, kind="CHK", kind_words="a document chunk", title=title, state_words=pages,
        purpose=purpose, related=related, found=True, store="knowledge",
    )


def describe_anc(stores, id_: str) -> Description:
    row = _row(stores.knowledge, "quote_anchor", "anchor_id", id_)
    if row is None:
        return Description(id=id_, kind="ANC", kind_words="a quote anchor", found=False, store="knowledge" if stores.knowledge is not None else None)
    doc = _row(stores.knowledge, "document", "doc_id", row.get("doc_id") or "")
    related = [(row["doc_id"], "a document", doc.get("rel_path") or row["doc_id"])] if doc else []
    title = (row.get("quote_text") or "")[:_TITLE_LIMIT].strip() or None
    state = f"page {row['page_number']}" if row.get("page_number") is not None else None
    return Description(
        id=id_, kind="ANC", kind_words="a quote anchor", title=title, state_words=state,
        purpose="marks where a quoted span sits in its document", related=related, found=True, store="knowledge",
    )


register("SRC", describe_src)
register("DOC", describe_doc)
register("CHK", describe_chk)
register("ANC", describe_anc)
