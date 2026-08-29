"""``trialerror.verify`` — the verification suite. Design Section 12 (M9 row):
"citecheck (mechanical + deterministic sampling + escalation queue);
hypothesis pipeline (retrieve -> vendored contracrow classify -> aggregate
-> verdict artifact, Section 8.2 v0-minimal); reproduction runner (Section
8.3); prereg commit/reveal CLI (platform-tree escrow)." Design Section 8
(Verification workflows) is this package's spec.

Five submodules, each independently importable:

- :mod:`trialerror.verify.errors` — this package's exception hierarchy.
- :mod:`trialerror.verify.prereg` — blind pre-registration: commit (hash +
  escrow under the PLATFORM tree, outside the program repo — design
  Section 4.2's "the blind is physical, not conventional"), reveal
  (tamper-checked), and compliance recomputation.
- :mod:`trialerror.verify.verdicts` — the generic ``knowledge.verdict`` writer
  every procedure (citecheck/contracrow/reproduction/custom) records
  through; also what M14's ``trialerror-ops`` ``record_verdict`` MCP tool now
  calls (see ``trialerror/mcp/ops.py``'s own TRIALERROR-DEV-NOTE, superseded by this
  build).
- :mod:`trialerror.verify.citecheck` — the two-tier citation-check pipeline
  (design Section 8.1): a zero-LLM mechanical pass, then a deterministic
  escalation queue an external judge (agent or test fake) fills.
- :mod:`trialerror.verify.hypothesis` — the hypothesis-vs-literature pipeline
  (design Section 8.2): optional prereg, stratified retrieve, per-chunk
  contracrow classification via the same judge-envelope contract, label
  aggregation, verdict recording.
- :mod:`trialerror.verify.reproduce` — the reproduction runner (design Section
  8.3): re-runs a verdict's ``reproduction_ref`` script, byte-exact sha
  comparison, and the ONE call that couples into M10's gate state machine
  (``trialerror.stores.update(store, "gate", ..., changes={"reproduction_status":
  ..., "reproduction_ref": ...})``).
- :mod:`trialerror.verify.checks` — this package's doctor checks.

**LLM-judgment boundary (stated once, applies to both pipelines below):**
neither :mod:`trialerror.verify.citecheck` nor :mod:`trialerror.verify.hypothesis`
calls an LLM itself. Both build a "judgment request" envelope (plain dict:
the text to classify, the evidence/anchor it is being checked against, the
fixed label vocabulary) and accept a ``judge`` callable —
``judge(envelope) -> label`` — that a real subagent fills at runtime, or a
deterministic fake fills in tests. This package shapes the work (evidence
assembly, citation bundles, label taxonomy, verdict-record writing); it
never makes the classification call itself.
"""

from __future__ import annotations

from trialerror.verify.errors import (
    CitecheckError,
    InvalidProcedureError,
    InvalidSubjectKindError,
    PreregNotFoundError,
    PreregTamperedError,
    PreregVoidedError,
    ReproductionRefError,
    VerdictNotFoundError,
    VerifyError,
)

__all__ = [
    "VerifyError",
    "InvalidProcedureError",
    "InvalidSubjectKindError",
    "PreregNotFoundError",
    "PreregTamperedError",
    "PreregVoidedError",
    "VerdictNotFoundError",
    "ReproductionRefError",
    "CitecheckError",
]
