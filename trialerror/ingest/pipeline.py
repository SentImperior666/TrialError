"""Ingestion orchestration: acquire -> register -> normalize/OCR-route ->
chunk -> embed -> index (design Section 6). This module is what
``trialerror/cli/ingest.py`` and (indirectly, via job payloads) the handlers in
``trialerror.ingest.handlers`` call; it owns no job-body logic itself (that's
``handlers.py`` -- this module only builds/validates rows and enqueues the
next stage's job).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from trialerror.ingest.errors import (
    LicenseRouteRefusedError,
    PathOutOfTreeError,
    SourceNotFoundError,
)
from trialerror.ingest.normalizers import (
    MEDIA_TYPES_DIRECT,
    MEDIA_TYPES_NEEDING_OCR,
    detect_media_type,
)
from trialerror.jobs import ledger
from trialerror.stores.store import Store
from trialerror.stores.writer import get, insert
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

__all__ = [
    "DEFAULT_INGEST_ROOTS",
    "DEFAULT_ARCHIVE_DIR",
    "sha256_file",
    "resolve_ingest_roots",
    "assert_in_tree",
    "allowed_acquisition_routes",
    "register_source",
    "estimate_cost",
    "COST_GATE_PAGE_THRESHOLD",
    "stage_job_kind_and_payload",
    "add_document",
    "requeue_stage",
]

#: design Section 6: "register refuses paths outside raw/inbox globs" --
#: default relative roots when a program's trialerror.toml doesn't override
#: ``paths.ingest_roots``.
DEFAULT_INGEST_ROOTS = ("raw", "inbox")

#: design Section 3.2 per-program scaffold: "``archive/``" -- default when
#: a program's trialerror.toml doesn't override ``[paths].archive_dir``
#: (the import-design notes (internal, not in this export) Sec 5 knob #5).
DEFAULT_ARCHIVE_DIR = "archive"

#: design Section 6: "requires --yes beyond a config threshold" -- default
#: page-count threshold when trialerror.toml doesn't override
#: ``ingest.cost_gate_page_threshold``.
COST_GATE_PAGE_THRESHOLD = 50

#: TRIALERROR-DEV-NOTE (schema-v2, build-v1-schemav2): ``job.kind``'s CHECK
#: constraint (``trialerror/stores/schema/jobs.py``) used to allow only
#: ``('ocr','embed','index','extract','ingest_batch','watch','custom')`` --
#: ``normalize``/``chunk`` rode ``kind='custom'`` with ``payload['handler']``
#: set instead (docs/INTEGRATION_NOTES.md item 8; docs/the migration-plan notes (internal, not in this export)
#: Section 4 item 2). The jobs.db v2 migration
#: (``jobs_v2_kind_check_adds_normalize_and_chunk``) adds both as first-class
#: kinds, so :func:`stage_job_kind_and_payload` below now maps every stage
#: name straight through as its own ``kind`` -- no more ``'custom'``
#: wrapping for these two. ``_CUSTOM_STAGE_KINDS`` is kept (now empty by
#: construction, not hardcoded) purely as the extension point for a GENUINE
#: custom stage some future caller wants to ride ``kind='custom'`` +
#: ``payload['handler']`` for (a stage that will never become a first-class
#: ``job.kind`` value) -- add its name here, exactly as ``normalize``/
#: ``chunk`` used to live here, and it gets the same wrapping without
#: touching the function body. Backward compat: a pre-migration
#: ``kind='custom'``/``payload={'handler': 'normalize'|'chunk'}`` job row
#: enqueued before this change (or by ``trialerror.jobs.worker.spawn_worker``'s
#: own ``kind``/``payload`` passthrough) still dispatches correctly --
#: ``trialerror.jobs.worker.run_one``'s handler-name resolution unwraps
#: ``payload['handler']`` for ANY ``kind='custom'`` row exactly as before
#: this change (untouched by this migration), and a first-class
#: ``kind='normalize'``/``'chunk'`` row resolves the SAME registered handler
#: names (``trialerror.ingest.handlers`` registers ``@register_handler("normalize")``/
#: ``"chunk"`` verbatim) via that same function's already-generic
#: ``handler_name = claimed["kind"]`` path -- both spellings resolve to the
#: same handler, proven in ``tests/test_ingest_pipeline.py``.
_CUSTOM_STAGE_KINDS: frozenset[str] = frozenset()


def stage_job_kind_and_payload(stage: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Map a logical pipeline stage name to the ``(kind, payload)`` pair
    ``trialerror.jobs.ledger.enqueue`` needs. Every stage -- including
    ``normalize``/``chunk``, first-class ``job.kind`` values as of schema-v2
    -- rides its own name as ``kind`` unchanged, except any stage listed in
    :data:`_CUSTOM_STAGE_KINDS` (a genuine custom, never a first-class
    ``job.kind`` value), which rides ``kind='custom'`` with
    ``payload['handler']`` set to the stage name instead."""
    if stage in _CUSTOM_STAGE_KINDS:
        return "custom", {**payload, "handler": stage}
    return stage, payload


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_ingest_roots(program_root: Path, config: dict[str, Any] | None) -> list[Path]:
    paths_cfg = (config or {}).get("paths", {})
    roots = paths_cfg.get("ingest_roots", list(DEFAULT_INGEST_ROOTS))
    resolved: list[Path] = []
    for r in roots:
        p = Path(r)
        resolved.append(p if p.is_absolute() else (program_root / p))
    return [p.resolve() for p in resolved]


def assert_in_tree(path: Path, roots: list[Path]) -> None:
    """Design Section 6: "register refuses paths outside raw/inbox globs
    (the ops-manifest-as-source wart)" -- refuses a raw file path that does
    not resolve under any of ``roots``. Raises :class:`PathOutOfTreeError`,
    never silently coerces."""
    resolved = path.resolve()
    for root in roots:
        try:
            resolved.relative_to(root)
            return
        except ValueError:
            continue
    raise PathOutOfTreeError(
        f"{path} is outside every configured ingest root {[str(r) for r in roots]} "
        "(the ops-manifest-as-source wart this check exists to catch)"
    )


def allowed_acquisition_routes(config: dict[str, Any] | None) -> set[str] | None:
    """``None`` means "no restriction beyond the DDL's own CHECK
    constraint" (a fresh scaffold with no ``[license]`` posture configured
    yet is permissive by default -- design Section 6's allowlist is a
    PROGRAM-level posture choice, not a hardcoded platform one)."""
    license_cfg = (config or {}).get("license", {})
    routes = license_cfg.get("allowed_acquisition_routes")
    return set(routes) if routes is not None else None


def register_source(
    store: Store,
    *,
    kind: str,
    title: str,
    license_tier: str,
    acquisition_route: str,
    registered_by_launch: str,
    authors: str | None = None,
    year: int | None = None,
    venue: str | None = None,
    url: str | None = None,
    doi: str | None = None,
    arxiv_id: str | None = None,
    isbn: str | None = None,
    content_sha256: str | None = None,
    rights_notes: str | None = None,
    request_state: str = "delivered",
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Register (or dedup onto) one ``source`` row. Design Section 6 stage
    1/2: "License fields REQUIRED at intake; legitimacy bar enforced" +
    "sha256 -> UNIQUE; duplicate -> dedup_of, no second pipeline run".

    A ``content_sha256`` that already matches an existing source is NOT
    re-inserted (that would violate the table's own UNIQUE index) --
    instead the existing row is returned with ``dedup_of`` set to its own
    ``source_id`` in the RETURNED dict (the canonical row itself is never
    mutated to self-reference; ``dedup_of`` is the caller-facing dedup
    signal, matching design's "returns the existing row with dedup_of
    set").
    """
    allowed = allowed_acquisition_routes(config)
    if allowed is not None and acquisition_route not in allowed:
        raise LicenseRouteRefusedError(
            f"acquisition_route {acquisition_route!r} is outside this program's allowed "
            f"routes {sorted(allowed)!r} (license posture, trialerror.toml [license])"
        )

    if content_sha256 is not None:
        existing = store.knowledge.execute(
            "SELECT * FROM source WHERE content_sha256 = ?", (content_sha256,)
        ).fetchone()
        if existing is not None:
            row = dict(existing)
            row["dedup_of"] = row["source_id"]
            return row

    source_id = new_id("SRC")
    row = {
        "source_id": source_id,
        "kind": kind,
        "title": title,
        "authors": authors,
        "year": year,
        "venue": venue,
        "url": url,
        "doi": doi,
        "arxiv_id": arxiv_id,
        "isbn": isbn,
        "content_sha256": content_sha256,
        "license_tier": license_tier,
        "acquisition_route": acquisition_route,
        "rights_notes": rights_notes,
        "request_state": request_state,
        "registered_ts": now(),
        "registered_by_launch": registered_by_launch,
    }
    return insert(store, "source", row)


def estimate_cost(raw_path: Path, media_type: str) -> dict[str, Any]:
    """Zero-LLM dry-run estimate (design Section 6: "prints a zero-LLM
    dry-run estimate (pages, chunks, embed tokens, est GPU minutes)").
    Deliberately cheap/approximate -- a gate signal, not a billing figure."""
    size_bytes = raw_path.stat().st_size
    pages: int
    if media_type in ("pdf-text", "pdf-scan"):
        try:
            from pypdf import PdfReader

            pages = len(PdfReader(str(raw_path)).pages)
        except Exception:
            pages = max(1, size_bytes // 3000)
    else:
        pages = max(1, size_bytes // 3000)  # ~3KB/"page" of prose, a rough proxy

    est_tokens = pages * 500  # ~500 tokens/page, rough proxy
    est_chunks = max(1, -(-est_tokens // 1024))  # ceil div by the 1024 chunk cap
    est_gpu_minutes = round((pages * (0.5 if media_type == "pdf-scan" else 0.0)) + (est_tokens / 20000), 2)
    return {
        "pages": pages,
        "est_chunks": est_chunks,
        "est_embed_tokens": est_tokens,
        "est_gpu_minutes": est_gpu_minutes,
        "size_bytes": size_bytes,
    }


def add_document(
    store: Store,
    *,
    program_root: Path,
    source_id: str,
    raw_path: Path,
    created_by_launch: str,
    media_type: str | None = None,
    config: dict[str, Any] | None = None,
    yes: bool = False,
) -> dict[str, Any]:
    """Acquire+register one document under an already-registered source,
    then enqueue its first pipeline job (``normalize`` for a directly
    normalizable format, ``ocr`` for a pdf-scan/image route -- design
    Section 6 stages 3/4).

    Returns ``{"document": <row>, "job": <enqueued job row>, "cost_estimate": {...}}``
    on success. Raises :class:`~trialerror.ingest.errors.PathOutOfTreeError` for
    an out-of-tree ``raw_path``; raises ``ValueError`` (cost-gate refusal)
    when the estimate exceeds the configured page threshold and ``yes`` is
    not set.
    """
    program_root = Path(program_root)
    raw_path = Path(raw_path)
    roots = resolve_ingest_roots(program_root, config)
    assert_in_tree(raw_path, roots)

    source = get(store, "source", pk_column="source_id", pk_value=source_id)
    if source is None:
        raise SourceNotFoundError(f"no such source: {source_id!r}")

    resolved_media_type = media_type or detect_media_type(raw_path)

    threshold = (config or {}).get("ingest", {}).get("cost_gate_page_threshold", COST_GATE_PAGE_THRESHOLD)
    cost_estimate = estimate_cost(raw_path, resolved_media_type)
    if cost_estimate["pages"] > threshold and not yes:
        raise ValueError(
            f"cost gate: estimated {cost_estimate['pages']} pages exceeds the {threshold}-page "
            f"threshold for {raw_path} -- pass yes=True (CLI: --yes) to proceed. estimate={cost_estimate}"
        )

    doc_id = new_id("DOC")
    try:
        rel_raw = str(raw_path.resolve().relative_to(program_root.resolve()).as_posix())
    except ValueError:
        rel_raw = str(raw_path)

    placeholder_sha256 = sha256_file(raw_path)
    # the import-design notes (internal, not in this export) Sec 5 knob #5: [paths].archive_dir overrides the
    # "archive" literal (default unchanged when config has no [paths] table
    # -- .as_posix() keeps the stored rel_path forward-slash-joined exactly
    # like the unconfigured default always was, whether archive_dir itself
    # came in relative or with OS-native separators).
    archive_dir_value = (config or {}).get("paths", {}).get("archive_dir", DEFAULT_ARCHIVE_DIR)
    rel_path = (Path(archive_dir_value) / f"{doc_id}.txt").as_posix()
    doc_row = insert(
        store,
        "document",
        {
            "doc_id": doc_id,
            "source_id": source_id,
            "rel_path": rel_path,
            "raw_path": rel_raw,
            "media_type": resolved_media_type,
            # placeholders -- both columns are NOT NULL; the normalize/ocr
            # handler overwrites them (and sha256, below) with the real
            # values once normalization actually runs (design Section
            # 4.1: normalizer_id/version describe the normalizer that
            # PRODUCED the current element set, which doesn't exist yet).
            "normalizer_id": "pending",
            "normalizer_version": "0",
            "sha256": placeholder_sha256,
            "status": "registered",
        },
    )

    if resolved_media_type in MEDIA_TYPES_NEEDING_OCR:
        stage = "ocr"
    elif resolved_media_type in MEDIA_TYPES_DIRECT:
        stage = "normalize"
    else:
        from trialerror.ingest.errors import UnsupportedMediaTypeError

        raise UnsupportedMediaTypeError(f"media_type {resolved_media_type!r} has no route (normalize or OCR)")

    job_kind, job_payload = stage_job_kind_and_payload(
        stage,
        {
            "doc_id": doc_id,
            "raw_path": rel_raw,
            "media_type": resolved_media_type,
            "created_by_launch": created_by_launch,
        },
    )
    job = ledger.enqueue(store, kind=job_kind, payload=job_payload, job_id=f"JOB-ingest-{doc_id}")
    return {"document": doc_row, "job": job, "cost_estimate": cost_estimate}


def requeue_stage(store: Store, *, doc_id: str, kind: str, created_by_launch: str) -> dict[str, Any]:
    """Enqueue (or re-enqueue) a specific pipeline stage for ``doc_id`` --
    used by ``trialerror ingest rechunk``/``re-embed`` (design Section 4.1:
    "rechunk/re-embed fix the first four [doctor counts] as resumable
    jobs") and by a manual resume after a resolved failure. ``kind`` is the
    logical STAGE name (``"chunk"``/``"embed"``/...), mapped to the real
    ``job.kind`` via :func:`stage_job_kind_and_payload`."""
    job_kind, job_payload = stage_job_kind_and_payload(kind, {"doc_id": doc_id, "created_by_launch": created_by_launch})
    job_id = f"JOB-ingest-{doc_id}-{kind}-{new_id('R')}"
    return ledger.enqueue(store, kind=job_kind, payload=job_payload, job_id=job_id)
