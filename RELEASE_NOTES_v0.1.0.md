## TrialError v0.1.0 — first public release

A research-operations exoskeleton for Claude Code: the layer that turns "the orchestrator
remembers the protocol" into "the tooling refuses to let the protocol lapse."

### What's in

- Session lifecycle with refusing boot/close rituals; versioned law with digest-pin
  verification (a stale pin boot-blocks agents).
- Budget-at-spawn enforcement: an unbooked subagent spawn is refused by hook, not
  discouraged by docs.
- Quote-anchored corpus: ingest -> OCR -> chunk -> embed -> index, every retrieval result
  carries a citation anchor; license tiers enforced at the serving layer (restricted
  sources cap verbatim quotes at 20 words).
- Typed artifacts + a critique-gate state machine (union_applied is the only terminal pass).
- Durable background jobs (claim/lease/heartbeat, resumable from checkpoint after a kill).
- An operable local dashboard: search over the corpus, feed, deliberation rooms with
  convergence trajectories, a unified decision queue, and eight token-guarded write actions.
- Scholarly literature clients (OpenAlex, Semantic Scholar, arXiv, Unpaywall) plus a local
  semantic index builder over the full arXiv corpus (~3.57M papers via a published
  embeddings dataset).
- Per-project dashboard extensions and a skill that guides a coding agent to build them.
- Two MCP servers, one CLI, a Claude Code plugin (hooks + nine skills).

### Verified

2,035 tests passing (17 skipped: optional-dependency and live-hardware items, enumerated
explicitly by `trialerror accept` rather than silently omitted). Windows 11, Python 3.12.

### Known limitations

Windows-first (Linux untested); real OCR/embedding backends need a local GPU and are
acceptance-tracked as explicit skips until verified on your machine. The README's
"Roadmap to the next release" section lists what is still coming.
