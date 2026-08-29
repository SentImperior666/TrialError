"""Independence clustering. Design Section 8.2 step 4 ("Aggregate: label
distribution + independence clustering (syndication/near-duplicate sources
counted once; hyperresearch union-find pattern -- v1 for web corpora,
trivial for curated ones)"), explicitly deferred in the v0 build
(:mod:`trialerror.verify.hypothesis`'s own module docstring: "Independence
clustering / syndication discount is explicitly deferred to v1 per design
Section 8.2 -- not implemented here"). This module is that v1 piece,
Section 11's "hypothesis pipeline hardening" scope item.

**What "independent" means here (two signals, unioned):**

1. **Source-lineage** -- two evidence chunks trivially share a lineage when
   they come from the SAME ``source`` row (same book/paper/report cited
   twice), and non-trivially when they come from two DIFFERENT ``source``
   rows that nonetheless share an author+venue signature (the same paper
   registered twice under different ``source_id``s -- a preprint and its
   published version, or a duplicate registration -- design's own
   "syndication ... sources counted once" wording).
2. **Embedding proximity** -- two chunks whose vectors are near-identical
   (:func:`trialerror.retrieve.vecsearch.cosine_similarity` above
   :data:`DEFAULT_PROXIMITY_THRESHOLD`) are treated as the same underlying
   content regardless of what their ``source`` rows say -- catches
   near-duplicate text that source metadata alone would miss (a chapter
   reprinted in an anthology under a fresh title/author string, a web
   mirror with no author/venue fields at all).

**The algorithm (design's own words: "hyperresearch union-find pattern"):**
a disjoint-set over the evidence's chunk ids, unioned by both signals above,
computed with a plain :class:`UnionFind` (no vendored dependency -- the
whole algorithm is ~15 lines and this build's lane has no license to add a
new dependency, matching :mod:`trialerror.lens.vectors`/:mod:`trialerror.lens.stratify`'s
own "plain Python, no numpy" posture). The number a verdict actually wants
is not the chunk COUNT but the CLUSTER count -- :func:`independence_stats`'s
``effective_independent_count`` -- "a claim 'supported by 12 chunks' from
one book is 1 source" (this build's own brief, verbatim).

TRIALERROR-DEV-NOTE (no schema change): ``knowledge.verdict`` has no free-form
metadata column to persist a clustering onto (design's fixed DDL: subject_kind
| subject_id | procedure | procedure_version | label | evidence | prereg_id?
| prereg_compliant? | reproduction_ref? | ts | issued_by_launch -- see
``trialerror/stores/schema/knowledge.py``). Independence stats are therefore
carried in the PIPELINE'S OWN return dict (what
:func:`trialerror.verify.hypothesis.run_hypothesis_verification` hands back, and
what the CLI's ``AgentEnvelope`` result surfaces to a caller) rather than
invented as a new DB column or smuggled into the ``evidence`` JSON array
(whose documented shape is citation evidence -- ``{anchor_id?, chunk_id?,
stance?, note?}`` -- not aggregate statistics). This build's lane owns
``trialerror/verify/`` and ``trialerror/eval/`` only; a schema migration is a
cross-cutting change outside that scope even if it were otherwise desirable.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

from trialerror.retrieve import engine as retrieve_engine
from trialerror.retrieve.vecsearch import cosine_similarity, fetch_vectors
from trialerror.stores.store import Store

__all__ = [
    "DEFAULT_PROXIMITY_THRESHOLD",
    "UnionFind",
    "source_lineage_key",
    "cluster_evidence",
    "independence_stats",
]

#: Cosine-similarity floor above which two chunks are treated as the same
#: underlying content for independence purposes (a judgment call this
#: module names explicitly, same posture as ``trialerror.lens.stratify``'s own
#: "distance metric -- a judgment call the design names but does not spell
#: out arithmetically" note). 0.92 is deliberately high (near-duplicate,
#: not merely topically-similar) -- two chunks that both happen to discuss
#: the same rule in similar language are NOT the same source; two chunks
#: that are the same paragraph reprinted verbatim are.
DEFAULT_PROXIMITY_THRESHOLD = 0.92

_WS_RE = re.compile(r"\s+")


class UnionFind:
    """A plain disjoint-set over a fixed universe of string ids -- "the
    hyperresearch union-find pattern" design Section 8.2 names by name.
    Path-compressing :meth:`find`; :meth:`union` always attaches the
    LEXICOGRAPHICALLY LARGER root under the smaller one, so the resulting
    cluster ids (each cluster's root) are stable and deterministic
    regardless of the order :meth:`union` calls happen to run in -- the
    same "same seed -> byte-identical output" bar
    ``trialerror.lens.stratify``/``trialerror.retrieve.vecsearch`` hold themselves to,
    applied here to clustering rather than ranking."""

    def __init__(self, items: Iterable[str]) -> None:
        self._parent: dict[str, str] = {item: item for item in items}

    def find(self, x: str) -> str:
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if rb < ra:
            ra, rb = rb, ra
        self._parent[rb] = ra

    def groups(self) -> dict[str, list[str]]:
        """``root -> [members...]`` (members in insertion order), one entry
        per distinct cluster currently in the set."""
        out: dict[str, list[str]] = {}
        for item in self._parent:
            out.setdefault(self.find(item), []).append(item)
        return out


def _norm(value: Any) -> str:
    return _WS_RE.sub(" ", str(value or "")).strip().lower()


def source_lineage_key(source_row: Mapping[str, Any]) -> str | None:
    """A same-lineage signature for one ``source`` row: normalized
    ``authors::venue`` when both are present (the strongest signal --
    design's "syndication ... sources counted once"), falling back to
    ``authors::title`` when there's no venue (a self-published/web source),
    or ``None`` when there isn't even an author to key on (nothing to
    dedupe against beyond same-``source_id``, which
    :func:`cluster_evidence` already handles directly -- returning ``None``
    here is deliberate, not a degenerate empty-string key that would
    spuriously union every author-less source together)."""
    authors = _norm(source_row.get("authors"))
    venue = _norm(source_row.get("venue"))
    title = _norm(source_row.get("title"))
    if authors and venue:
        return f"{authors}::{venue}"
    if authors and title:
        return f"{authors}::{title}"
    return None


def _chunk_source_rows(store: Store, chunk_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """``chunk_id -> its source row`` (``authors``/``venue``/``title``/
    ``source_id``), for every chunk id that resolves through
    ``chunk -> document -> source``. A chunk id that doesn't resolve (bad
    input, or a chunk from a different program's fixture) is simply absent
    -- same "missing is absent" convention ``fetch_vectors`` itself uses."""
    out: dict[str, dict[str, Any]] = {}
    for chunk_id in dict.fromkeys(chunk_ids):  # dedupe, stable order
        row = store.knowledge.execute(
            """
            SELECT s.source_id AS source_id, s.authors AS authors, s.venue AS venue, s.title AS title
            FROM chunk c
            JOIN document d ON c.doc_id = d.doc_id
            JOIN source s ON d.source_id = s.source_id
            WHERE c.chunk_id = ?
            """,
            (chunk_id,),
        ).fetchone()
        if row is not None:
            out[chunk_id] = dict(row)
    return out


def cluster_evidence(
    store: Store,
    *,
    evidence: Sequence[Mapping[str, Any]],
    model_key: str | None = None,
    proximity_threshold: float = DEFAULT_PROXIMITY_THRESHOLD,
) -> dict[str, Any]:
    """Cluster ``evidence`` (any sequence of dicts carrying a ``chunk_id``
    key -- a hypothesis pipeline's ``labeled_evidence``, a
    ``stratified_retrieve`` arm, or a bare list of chunk ids wrapped as
    ``{"chunk_id": ...}``) into same-lineage/near-duplicate groups.

    Returns ``{"clusters": {root_chunk_id: [chunk_id, ...]}, "cluster_of":
    {chunk_id: root_chunk_id}, "n_chunks", "n_clusters", "proximity_method":
    "embedding" | "lineage_only"}``. ``proximity_method`` records whether
    the embedding-proximity signal actually ran (it degrades to
    ``"lineage_only"``, never an error, when nothing in ``evidence`` has a
    stored vector under the resolved ``model_key`` -- the same documented,
    non-silent fallback posture
    :func:`trialerror.verify.hypothesis._distance_tercile_pools` uses for its own
    missing-vectors case)."""
    chunk_ids = [e["chunk_id"] for e in evidence if e.get("chunk_id")]
    uf = UnionFind(chunk_ids)
    if not chunk_ids:
        return {"clusters": {}, "cluster_of": {}, "n_chunks": 0, "n_clusters": 0, "proximity_method": "lineage_only"}

    source_rows = _chunk_source_rows(store, chunk_ids)

    # Signal 1a: same source_id -- the trivial, always-available case.
    by_source_id: dict[str, list[str]] = {}
    for chunk_id, row in source_rows.items():
        by_source_id.setdefault(row["source_id"], []).append(chunk_id)
    for group in by_source_id.values():
        for member in group[1:]:
            uf.union(group[0], member)

    # Signal 1b: different source_id, same author+venue (or author+title)
    # lineage -- syndicated/duplicate-registered sources.
    by_lineage: dict[str, list[str]] = {}
    for chunk_id, row in source_rows.items():
        key = source_lineage_key(row)
        if key is not None:
            by_lineage.setdefault(key, []).append(chunk_id)
    for group in by_lineage.values():
        for member in group[1:]:
            uf.union(group[0], member)

    # Signal 2: embedding proximity -- near-duplicate content regardless of
    # what the source rows say. Best-effort: an absent vector tier degrades
    # to "lineage_only", never an exception (a fresh corpus with no
    # embeddings run yet must still be able to cluster on lineage alone).
    proximity_method = "lineage_only"
    resolved_model_key = model_key
    if resolved_model_key is None:
        resolved_model_key, _backend = retrieve_engine._resolve_embed_backend(store)
    vectors = fetch_vectors(store, resolved_model_key, chunk_ids)
    if vectors:
        proximity_method = "embedding"
        ids_with_vec = list(vectors.keys())
        for i in range(len(ids_with_vec)):
            for j in range(i + 1, len(ids_with_vec)):
                a, b = ids_with_vec[i], ids_with_vec[j]
                if cosine_similarity(vectors[a], vectors[b]) >= proximity_threshold:
                    uf.union(a, b)

    groups = uf.groups()
    cluster_of = {chunk_id: root for root, members in groups.items() for chunk_id in members}
    return {
        "clusters": groups,
        "cluster_of": cluster_of,
        "n_chunks": len(chunk_ids),
        "n_clusters": len(groups),
        "proximity_method": proximity_method,
    }


def independence_stats(
    store: Store,
    *,
    evidence: Sequence[Mapping[str, Any]],
    model_key: str | None = None,
    proximity_threshold: float = DEFAULT_PROXIMITY_THRESHOLD,
) -> dict[str, Any]:
    """The number a verdict actually wants: EFFECTIVE independent-source
    count, not raw chunk count (this build's brief, verbatim: "a claim
    'supported by 12 chunks' from one book is 1 source"). Thin summary over
    :func:`cluster_evidence` -- ``effective_independent_count`` is the
    cluster count; ``syndication_discount`` is how much the raw count
    overstated independence (``0.0`` when every chunk was already its own
    cluster, approaching ``1.0`` as more chunks collapse into fewer
    sources)."""
    clustering = cluster_evidence(store, evidence=evidence, model_key=model_key, proximity_threshold=proximity_threshold)
    cluster_sizes = {root: len(members) for root, members in clustering["clusters"].items()}
    n_chunks = clustering["n_chunks"]
    n_clusters = clustering["n_clusters"]
    return {
        "raw_chunk_count": n_chunks,
        "effective_independent_count": n_clusters,
        "largest_cluster_size": max(cluster_sizes.values()) if cluster_sizes else 0,
        "syndication_discount": round(1.0 - (n_clusters / n_chunks), 4) if n_chunks else 0.0,
        "clusters": clustering["clusters"],
        "cluster_of": clustering["cluster_of"],
        "proximity_method": clustering["proximity_method"],
    }
