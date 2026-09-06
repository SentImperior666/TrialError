# PURPOSE — /ingest

**Problem this skill exists for.** Ingestion that silently processes the same
work twice, loses hours of OCR when a worker is killed, guesses a license tier
because nobody asked, or starts a 900-page OCR run without anyone confirming
the cost. Every stage is idempotent and content-hash keyed, resumable through
the jobs ledger, and the skill front-loads the two human decisions — the
license fields at intake and the cost-estimate confirm gate — before any
pipeline stage is enqueued.

**Mined patterns that motivated it.** The OCR backbone and its
concurrency/mode-tiering delta are marker's
(`docs/mining/G19-pdfparse__marker.md`); web-page-to-markdown acquisition is
jina-reader's preset-and-block-chunking model
(`docs/mining/G18-webparse__jina-reader.md`). The pre-ingestion
cost-estimate-and-confirm gate is book-to-skill's, reinforced by cognee's
zero-call `dry_run` estimator (`docs/mining/G22-docstruct-3__book-to-skill.md`,
`docs/mining/S4-rag-2__cognee.md`); the same book-to-skill report supplied the
invisible-codepoint / Trojan-Source sanitizer that runs on ingested text,
with arxiv-mcp-server's untrusted-content handling as the second source
(`docs/mining/S2-scilit-2__arxiv-mcp-server.md`). Resumability comes from
atomic's claim/lease/heartbeat ledger
(`docs/mining/G21-docstruct-2__atomic.md`). From the 2026-09 round: pinned,
integrity-verified provisioning of the external binaries and weights this
pipeline depends on (rowboat-F6, `docs/mining/G25-operator-2026-09__rowboat.md`)
is the provisioning discipline recorded for the sandbox image and offload
host, not something this skill performs.

**Evolution.**
- Created with the plugin (design Section 6; M7).
- 2026-09-05 (launch `LNCH-01M1R8J1H9QN0RZVCE1MKR6W6G`): added this sidecar and
  a "When NOT to apply" section (wikiskill-F4).

*Mining reports live under `docs/mining/` in the harness repository and are
not part of the public export; primary sources are named inline.*
