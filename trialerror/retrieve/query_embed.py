"""Query-side embedding resolution + the comparability (calibration) probe.

``docs/VASTAI_EMBED_DESIGN.md`` section 6. For an offload program the query
string is embedded locally on CPU by
:class:`~trialerror.ingest.backends.CpuQueryEmbedBackend`, while the chunk
vectors came from a GPU (DEV or vast.ai). Running the same model file is necessary,
but it does not guarantee comparability. A different precision, a changed runner module or
a mis-pointed ``module_dir`` would all produce vectors that "work" and rank
wrongly. So before the first use, and after any identity change, the CPU
backend re-embeds the few SHORTEST stored chunks as documents and compares them
against their stored GPU vectors; below ``min_calibration_cosine`` it
refuses. A pass is cached per identity in
``<program_root>/offload/query-embed-calibration.json``.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from trialerror.ingest.backends import (
    CpuQueryEmbedBackend,
    EmbedBackend,
    QueryEmbedUnavailable,
    load_query_embed_backend,
)
from trialerror.util.atomic import atomic_write_text
from trialerror.util.timeutil import now

__all__ = ["CALIBRATION_FILENAME", "CALIBRATION_PROBES", "resolve_query_backend", "ensure_calibrated"]

CALIBRATION_FILENAME = "query-embed-calibration.json"
CALIBRATION_PROBES = 3


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def _identity_key(backend: CpuQueryEmbedBackend) -> str:
    ident = {**backend.identity(), "python_exe": str(backend.python_exe)}
    return hashlib.sha256(json.dumps(ident, sort_keys=True).encode("utf-8")).hexdigest()


def _calibration_path(program_root: Path) -> Path:
    return Path(program_root) / "offload" / CALIBRATION_FILENAME


def ensure_calibrated(store: Any, backend: CpuQueryEmbedBackend) -> dict[str, Any]:
    """Raise :class:`QueryEmbedUnavailable` unless ``backend`` reproduces the
    stored chunk vectors closely enough. Returns the calibration record."""
    from trialerror.retrieve.vecsearch import fetch_vectors

    path = _calibration_path(store.program_root)
    key = _identity_key(backend)
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cached = {}
    if cached.get("identity_key") == key and cached.get("passed") is True:
        return cached

    rows = store.knowledge.execute(
        "SELECT chunk_id, text FROM chunk ORDER BY length(text) ASC, chunk_id ASC LIMIT 50"
    ).fetchall()
    ids = [r[0] for r in rows]
    stored = fetch_vectors(store, backend.model_key, ids) if ids else {}
    probes = [(r[0], r[1]) for r in rows if r[0] in stored][:CALIBRATION_PROBES]
    if not probes:
        # Nothing stored under this model_key yet: nothing to be incomparable
        # WITH (the vector tier will score nothing). Not cached -- the probe
        # runs as soon as vectors exist.
        return {"passed": None, "reason": "no stored vectors to calibrate against"}

    fresh = backend.embed_batch([t for _, t in probes], kind="document")
    cosines = [_cos(list(v), list(stored[cid])) for (cid, _), v in zip(probes, fresh)]
    record = {
        "identity_key": key,
        "identity": backend.identity(),
        "probes": [cid for cid, _ in probes],
        "cosines": cosines,
        "min_cosine": min(cosines),
        "threshold": backend.min_calibration_cosine,
        "passed": min(cosines) >= backend.min_calibration_cosine,
        "ts": now(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(record, indent=2))
    if not record["passed"]:
        raise QueryEmbedUnavailable(
            f"CPU query embedder is not comparable with the stored '{backend.model_key}' chunk vectors: "
            f"min cosine {record['min_cosine']:.4f} < {backend.min_calibration_cosine} on {len(probes)} "
            f"re-embedded chunk(s) (precision={backend.precision}). Check [ingest.embed.query] module_dir "
            f"points at the SAME embed_backend.py the GPU side runs; see {path}"
        )
    return record


def resolve_query_backend(store: Any, embed_cfg: dict[str, Any]) -> tuple[str, EmbedBackend]:
    """``(model_key, backend)`` for embedding a query string. Offload
    programs get the calibrated CPU backend; every other program gets the
    same backend its chunks were embedded with (unchanged behaviour)."""
    backend = load_query_embed_backend(embed_cfg)
    if isinstance(backend, CpuQueryEmbedBackend):
        ensure_calibrated(store, backend)
    return backend.model_key, backend
