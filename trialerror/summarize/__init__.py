"""``trialerror.summarize`` — the L1 summary tier. Design Section 11 ("summary
tier (L1 overviews)") / Section 7 pipeline step 5, applying the OpenViking
L0/L1/L2 progressive-loading pattern (``docs/mining/
G01-memory-1__OpenViking.md``) to the knowledge corpus: per-document and
per-collection overview summaries, generated via judgment envelopes
(agents execute the LLM work at runtime — the house ``trialerror/verify``
pattern, deterministic fakes in tests), stored durably in a versioned
``knowledge.summary`` table (``knowledge_v3_summary_table`` migration),
and served as a retrieval tier so cheap overview context precedes
expensive chunk retrieval.

Four submodules:

- :mod:`trialerror.summarize.errors` — this package's exception hierarchy.
- :mod:`trialerror.summarize.api` — envelope building, versioned storage,
  lookup/listing, and the shared staleness-key computation
  (:func:`~trialerror.summarize.api.compute_subject_sha256`) both the doctor
  check and the batch job handler call.
- :mod:`trialerror.summarize.handlers` — the ``summarize`` job handler
  (envelope-producing, riding the M2 ledger like M7's ``extract`` does —
  see its own module docstring for the "no LLM in the jobs/CLI layer"
  boundary this observes).
- :mod:`trialerror.summarize.checks` — the ``summaries_stale`` doctor check.

**LLM-judgment boundary (stated once, mirrors ``trialerror/verify/__init__.py``
verbatim in spirit):** nothing in this package calls an LLM. It builds a
"judgment request" envelope (the subject's context, the word cap, the
D-COC-1 fence instruction when the subject cites a ``commercial_restricted``
source) and accepts the resulting authored text from a caller — a real
subagent at runtime, or a deterministic fake in tests. This package shapes
the work (context assembly, staleness keys, embedded-quote fence
enforcement, versioned supersession); it never authors summary text
itself.
"""

from __future__ import annotations

from trialerror.summarize.errors import (
    InvalidSubjectKindError,
    SubjectNotFoundError,
    SummarizeError,
    SummaryFenceViolationError,
    SummaryNotFoundError,
)

__all__ = [
    "SummarizeError",
    "InvalidSubjectKindError",
    "SubjectNotFoundError",
    "SummaryNotFoundError",
    "SummaryFenceViolationError",
]
