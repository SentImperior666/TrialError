"""GPU offload (lane L0-C, design ``docs/reviews/lane0/LANE0_SANDBOX_RELOCATION_DESIGN.md``
section 4, decision D5).

The sandbox (CPU-only, always on) runs the program; the GPU stages (marker
OCR, Qwen3 embeddings) run ONLY on DEV (Windows, RTX 5080, intermittently
on). The seam is at the HANDLER level, not inside a backend: an ingest
stage whose configured backend is ``"offload"`` does not run a model at
all. It writes an offload marker (job id, stage, input payloads, config
hash, expected outputs) into the program's ``offload/`` queue and parks
the ledger job with an environmental failure; a DEV worker
(``trialerror offload worker``) claims it over a verb-restricted SSH key,
runs the real backend locally, publishes the outputs, and the parked stage
completes from those published outputs on its next claim.

Module map::

    marker.py     the ``OffloadMarker`` backend sentinel the ingest loaders
                  return for ``backend = "offload"`` (its ``run()``/
                  ``embed_batch()`` raise -- nothing must ever call a model
                  through it)
    protocol.py   the on-disk queue: layout, manifests, atomic-rename
                  claims, heartbeats, publish/return, reclaim, and the
                  server-side verb implementations the in-process transport
                  and ``deploy/sandbox/offload-shell.sh`` both mirror
    shell.py      a pure-Python port of ``offload-shell.sh``'s command
                  parsing + refusal rules (the wrapper is the security of
                  the restricted key; this port is its test oracle and the
                  local transport's gate)
    transport.py  ``Transport`` protocol + ``SshTransport`` (the real DEV
                  worker's one connection per verb) and ``LocalTransport``
                  (in-process; what the tests drive, no SSH anywhere)
    stage.py      the sandbox side of the seam: the five-step
                  resolve-or-park logic the ``ocr``/``embed`` handlers call
    worker.py     the DEV side: claim -> pull -> run the real backend ->
                  push -> publish, with a single-instance lock
    lock.py       that single-instance lock (``msvcrt``/``fcntl``)
    checks.py     doctor: ``offload_backlog`` / ``offload_stale_claims`` /
                  ``offload_failed`` / ``fake_backend_rows``
    dashboard_items.py   HOME's "what needs a human" line

Deliberately import-free at package level: ``trialerror.ingest.backends``
imports :mod:`trialerror.offload.marker`, so anything this ``__init__``
imported eagerly would be pulled into that import chain.
"""
