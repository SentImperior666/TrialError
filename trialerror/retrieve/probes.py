"""Canaries (design Section 3.4, ``kind='canary'``) and ``embed_coverage``.
Auto-discovered by :func:`trialerror.probes.registry.discover_and_register_
probes`.

Both canaries pick one random chunk with at least 200 characters of text and
build a query from its 4 longest alphabetic words -- never the chunk's own
text verbatim in a log line, never a search result's snippet (trap 2: only
ids/keys/counts ever leave this module).
"""

from __future__ import annotations

import re

from trialerror.probes.registry import ProbeContext, ProbeResult, register_probe
from trialerror.retrieve import engine
from trialerror.retrieve.ftssearch import fts_search

__all__ = []

_ALPHA_WORD_RE = re.compile(r"[A-Za-z]+")
_MIN_CHUNK_CHARS = 200


def _pick_canary_chunk(store) -> dict | None:
    row = store.knowledge.execute(
        "SELECT chunk_id, text FROM chunk WHERE length(text) >= ? ORDER BY RANDOM() LIMIT 1",
        (_MIN_CHUNK_CHARS,),
    ).fetchone()
    return dict(row) if row is not None else None


def _query_from_text(text: str, *, n: int = 4) -> str:
    words = sorted(set(_ALPHA_WORD_RE.findall(text)), key=len, reverse=True)
    return " ".join(words[:n])


# ---------------------------------------------------------------------------
# fulltext_canary
# ---------------------------------------------------------------------------


@register_probe("fulltext_canary", kind="canary", timeout_s=1.5)
def probe_fulltext_canary(ctx: ProbeContext) -> ProbeResult:
    if ctx.store is None:
        return ProbeResult(status="skip", detail={"reason": "no program store given"})
    chunk = _pick_canary_chunk(ctx.store)
    if chunk is None:
        return ProbeResult(status="skip", detail={"reason": "empty corpus (no chunk >= 200 chars)"})
    query = _query_from_text(chunk["text"])
    try:
        hits = fts_search(ctx.store, query, limit=20)
    except Exception as exc:  # noqa: BLE001 - a raising search tier is exactly what this canary catches
        return ProbeResult(status="fail", detail={"reason": f"{type(exc).__name__}: {exc}", "chunk_id": chunk["chunk_id"]})
    found = any(h["chunk_id"] == chunk["chunk_id"] for h in hits)
    return ProbeResult(status="pass" if found else "fail", detail={"chunk_id": chunk["chunk_id"], "found": found})


#: The exact line design Section 3.4 prescribes, verbatim modulo the reason.
DEGRADED_CONTEXT_TEMPLATE = (
    "SEARCH DEGRADED: full-text search did not find a known passage ({reason}). "
    "An empty search result is not evidence that something is absent."
)


def fulltext_canary_degraded_line(status: str, detail: dict | None = None) -> str | None:
    """The ``SEARCH DEGRADED`` line for ``SessionStart``'s
    ``additionalContext``, built from an ALREADY-RUN probe result's
    ``status``/``detail`` -- ``None`` on anything but ``"fail"``.

    B-1 fix round: this used to call :func:`probe_fulltext_canary` directly,
    bypassing :func:`trialerror.probes.registry.run_probes` entirely -- so
    the canary's result was never recorded in ``probe_run`` and never had
    the registry's own timeout (S-1). Callers now run the probe through
    ``run_probes(ctx, names=["fulltext_canary"])`` themselves (so the row IS
    recorded and the 1.5 s budget applies) and pass the resulting row's
    ``status``/``detail`` here -- a pure formatter, no execution, no
    ``ProbeContext`` needed."""
    if status != "fail":
        return None
    reason = (detail or {}).get("reason") or "the canary chunk was not returned"
    return DEGRADED_CONTEXT_TEMPLATE.format(reason=reason)


# ---------------------------------------------------------------------------
# vector_canary
# ---------------------------------------------------------------------------


@register_probe("vector_canary", kind="canary", timeout_s=90.0)
def probe_vector_canary(ctx: ProbeContext) -> ProbeResult:
    if ctx.store is None:
        return ProbeResult(status="skip", detail={"reason": "no program store given"})
    chunk = _pick_canary_chunk(ctx.store)
    if chunk is None:
        return ProbeResult(status="skip", detail={"reason": "empty corpus (no chunk >= 200 chars)"})
    try:
        result = engine.search(ctx.store, query=chunk["text"], mode="vector", k=10)
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(status="fail", detail={"reason": f"{type(exc).__name__}: {exc}", "chunk_id": chunk["chunk_id"]})

    skipped_reason = (result.get("stats") or {}).get("vector_skipped_reason")
    if skipped_reason:
        return ProbeResult(status="skip", detail={"reason": skipped_reason, "chunk_id": chunk["chunk_id"]})

    found = any(r.get("chunk_id") == chunk["chunk_id"] for r in result.get("results") or [])
    return ProbeResult(status="pass" if found else "fail", detail={"chunk_id": chunk["chunk_id"], "found": found})


# ---------------------------------------------------------------------------
# embed_coverage
# ---------------------------------------------------------------------------


@register_probe("embed_coverage", kind="canary", timeout_s=5.0)
def probe_embed_coverage(ctx: ProbeContext) -> ProbeResult:
    """Informational; ``warn`` below 95% (design Section 3.4). Coverage is
    counted against ANY ``emb`` row for a chunk's ``sha256``, not one
    specific ``model_key`` -- a documented simplification over the design's
    own hedge about which stores this covers."""
    if ctx.store is None:
        return ProbeResult(status="skip", detail={"reason": "no program store given"})
    total = ctx.store.knowledge.execute("SELECT COUNT(*) AS n FROM chunk").fetchone()["n"]
    if not total:
        return ProbeResult(status="skip", detail={"reason": "empty corpus"})
    embedded = ctx.store.knowledge.execute(
        "SELECT COUNT(DISTINCT c.chunk_id) AS n FROM chunk c JOIN emb e ON e.chunk_sha256 = c.sha256"
    ).fetchone()["n"]
    pct = round(100.0 * embedded / total, 1)
    detail = {"total_chunks": total, "embedded_chunks": embedded, "pct_embedded": pct}
    return ProbeResult(status="pass" if pct >= 95 else "warn", detail=detail)
