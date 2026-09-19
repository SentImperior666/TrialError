"""M7: Ingestion MVP. Design Section 6 (acquire -> register -> normalize ->
OCR-route -> chunk -> embed -> index -> extract) + Section 12's M7 row.

Public entry points a caller outside this package should use:

- :mod:`trialerror.ingest.pipeline` -- ``add_document`` (acquire+register+dedup+
  cost-gate+enqueue), the request-queue transitions.
- :mod:`trialerror.ingest.handlers` -- the ``@register_handler`` job bodies
  (``normalize``/``ocr``/``chunk``/``embed``/``index``/``extract``) that
  ride M2's ledger; imported for side effects by
  ``trialerror.jobs.registry.discover_and_register_handlers``.
- :mod:`trialerror.ingest.checks` -- doctor checks; imported for side effects by
  ``trialerror.util.doctor.discover_and_register_checks``.
- :mod:`trialerror.ingest.stream` -- ``stream_v1``, the canonical serialization
  M8/M9 readers depend on byte-identically.
"""

from __future__ import annotations
