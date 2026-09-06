"""The ``backend = "offload"`` sentinel.

Design v3 delta N2 (section 4 / D5): the offload branch lives in the ingest
HANDLERS, not in a backend, "because backends carry no job/document
identity and embed in batches of eight". A backend object still has to
exist, though -- ``trialerror.ingest.backends.load_ocr_backend`` /
``load_embed_backend`` are the one factory pair the handlers call, and
``run_index`` reads ``backend.model_key`` off whatever they return even
when no embedding is performed. So the loaders special-case ``"offload"``
and hand back an :class:`OffloadMarker`: a backend-SHAPED object that
carries the identity fields (``name``/``version``/``model_key``/``dims``)
and whose ``run()``/``embed_batch()`` raise :class:`OffloadNotRunnable`
if anything ever tries to compute through it.

That raise is the fail-closed half of D13: a handler that forgets its
offload branch, or a future caller that resolves a backend and calls it
directly, gets a loud logic failure -- never a silent fake vector or a
silently CPU-executed model on a box that has no GPU.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Sequence

__all__ = [
    "OFFLOAD_BACKEND_NAME",
    "OffloadNotRunnable",
    "OffloadMarker",
    "is_offload_config",
    "config_hash",
]

#: The one string a program's ``[ingest.ocr]``/``[ingest.embed]`` table
#: sets to route that stage through the GPU offload queue.
OFFLOAD_BACKEND_NAME = "offload"


class OffloadNotRunnable(RuntimeError):
    """Raised when something calls ``run()``/``embed_batch()`` on an
    :class:`OffloadMarker`. Always a bug in the caller (the handler's
    offload branch is what should have run instead), so it is a plain
    ``RuntimeError`` subclass: the jobs worker settles it as a LOGIC
    failure, which is exactly right -- retrying it unchanged cannot help."""


class OffloadMarker:
    """A backend-shaped stand-in for a stage whose model runs on DEV.

    ``kind`` is ``"ocr"`` or ``"embed"``. The identity attributes are read
    from the program's own config table so the sandbox can state, up
    front, what it EXPECTS the DEV worker to produce (the manifest's
    ``expect`` block): for ``embed`` that is ``model_key`` + ``dims``,
    which also keep ``run_index``'s ``model_key`` lookup and the
    ``emb``/``vec_chunks__<model_key>`` key space correct while the vectors
    themselves are still in flight.
    """

    def __init__(self, kind: str, config: dict[str, Any] | None = None):
        if kind not in ("ocr", "embed"):
            raise ValueError(f"OffloadMarker kind must be 'ocr' or 'embed', got {kind!r}")
        cfg = dict(config or {})
        self.kind = kind
        self.config = cfg
        self.name = OFFLOAD_BACKEND_NAME
        self.version = "1"
        if kind == "embed":
            model_key = cfg.get("model_key")
            if not model_key:
                # Fail-closed (D13): emb rows are keyed by model_key, so a
                # defaulted one would silently open a parallel key space
                # the DEV worker's real backend never writes into.
                raise ValueError(
                    "ingest.embed.backend = 'offload' requires ingest.embed.model_key in "
                    "trialerror.toml (the model_key the DEV worker's real backend produces, "
                    "e.g. 'qwen3-4b') -- emb rows are keyed by it, so it cannot be defaulted"
                )
            self.model_key = str(model_key)
            self.dims = int(cfg.get("dims", 2048))
        else:
            #: What the DEV worker's OCR backend must call itself. ``None``
            #: (the default) means "any non-fake backend"; set
            #: ``[ingest.ocr].expect_backend`` to pin one.
            self.expect_backend = cfg.get("expect_backend") or None
            self.expect_version = cfg.get("expect_version") or None

    # -- the two Protocol methods, both closed ------------------------------
    def run(self, *, input_path: Path, work_dir: Path):  # noqa: ARG002 - signature parity
        raise OffloadNotRunnable(
            "ingest.ocr.backend = 'offload': OCR runs on the DEV GPU worker, never in this "
            "process. The ocr handler's offload branch (trialerror.offload.stage) should have "
            "run instead of calling the backend directly."
        )

    def embed_batch(self, texts: Sequence[str], *, kind: str = "document"):  # noqa: ARG002
        raise OffloadNotRunnable(
            "ingest.embed.backend = 'offload': embedding runs on the DEV GPU worker, never in "
            "this process. The embed handler's offload branch (trialerror.offload.stage) should "
            "have run instead of calling the backend directly."
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"OffloadMarker(kind={self.kind!r})"


def is_offload_config(config: dict[str, Any] | None) -> bool:
    """``True`` when this ``[ingest.ocr]``/``[ingest.embed]`` table routes
    its stage through the offload queue."""
    return (config or {}).get("backend") == OFFLOAD_BACKEND_NAME


def config_hash(config: dict[str, Any] | None) -> str:
    """A stable sha256 over one stage's config table.

    Stamped into the manifest and echoed back by the DEV worker in
    ``result.json``: a result produced against a different configuration
    than the one that queued the work is refused rather than folded into
    the record. JSON with sorted keys (not ``repr``) so the digest does not
    depend on dict insertion order or on Python's own formatting."""
    payload = json.dumps(config or {}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
