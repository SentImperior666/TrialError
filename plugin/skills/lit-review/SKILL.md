---
name: lit-review
description: Answer a research question against the ingested corpus, paper-qa-shaped — search, gather evidence with citations, draft an answer, then re-search on any gap before finalizing. Use this whenever the user asks a question that should be answered FROM the corpus (not from general knowledge), or asks for a literature summary/survey over what's already ingested.
---

# /lit-review — search → gather-evidence → answer with citations

Design Section 5.3 (`/lit-review`) + Section 7 (retrieval contract) +
Section 5.1 (`trialerror-knowledge` MCP server, 11 tools). Every result row this
loop touches carries a non-null citation block — never answer from a
snippet you cannot cite back to an anchor.

1. **Search.** Prefer the `trialerror-knowledge` MCP `search` tool (or `trialerror
   query search "<question>"` from the CLI) over guessing keywords —
   `mode=auto` runs FTS prefilter → vector → RRF fusion. Filter by
   `source_id`/`kind`/`license_tier`/`year` when the question implies a
   scope. Read `citation.anchor` on every row you plan to use, not just
   `text`.

2. **Gather evidence**, not just the top hit. Pull `get_chunk` for full
   surrounding context on a promising result, `get_document_outline` to
   see where in the source it sits, `similar` to find nearby chunks that
   might sharpen or contradict the answer, `graph_neighbors` if the
   question is relational (entity/claim edges). `memory_search` (L0→L1→L2
   progressive disclosure) if the question might already be answered by a
   standing lesson/fact rather than raw corpus text.

3. **The serving-path license fence is structural, not optional.**
   `commercial_restricted` sources come back `fenced:true` — a ≤20-word
   excerpt, never the raw chunk. The MCP `search` tool has NO bypass
   parameter; do not try to reconstruct the full passage from repeated
   fenced calls. `trialerror query search --unfenced` exists ONLY as a
   human-flagged, logged, non-agent CLI escape hatch (design Section 7) —
   never invoke it on the user's behalf from inside a lit-review loop.

4. **Draft the answer with inline citations.** Every claim sentence gets a
   `[[cite:<anchor_id>]]` marker immediately after it, bound to the anchor
   that actually supports it — this is what `/verify-hypothesis` and
   `trialerror verify citecheck` bind against later, so get the marker-to-anchor
   pairing right the first time rather than citing "close enough."

5. **Re-gather on any gap.** If the draft needs a claim you don't have
   solid evidence for, go back to step 1 with a narrower/rephrased query —
   do not fill the gap from general knowledge and cite nothing, and do not
   silently soften the claim to something the evidence happens to support.

6. **Quote-check before finalizing.** For any direct quote in the answer,
   `resolve_quote`/`trialerror query quote "<exact text>"` confirms it still
   resolves byte-exact to an anchor (`quote_sha256` match) — a quote that
   comes back `NOT_FOUND` means you paraphrased something and marked it as
   a quote; fix the marker, don't force it.

7. **Corpus awareness.** `corpus_stats`/`trialerror query stats` before a big
   review tells you what's actually indexed (source/doc/chunk counts,
   index freshness) — don't promise coverage of a source that hasn't
   finished the ingest pipeline yet (`trialerror ingest status --doc-id <id>`
   if unsure; `/ingest` if it needs adding first).

8. **Hand off to verification when the answer matters.** A one-off
   question ends here. An answer feeding a keystone artifact or a
   hypothesis claim should go through `trialerror verify citecheck` (mechanical
   + deterministic-sampled LLM escalation) before it's trusted upstream —
   see the verify CLI group; `/verify-hypothesis` is the dedicated loop
   when the question IS a hypothesis, not just a question.
