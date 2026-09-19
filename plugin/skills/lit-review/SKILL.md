---
name: lit-review
description: Answer a research question against the ingested corpus, paper-qa-shaped — search, gather evidence with citations, draft an answer, then re-search on any gap before finalizing. Two further modes: a prior-art bundle (stratified retrieval returning anchors and excerpts with no answer drafted, feeding a novelty screen) and a bounded hunt (a fixed, logged query list over the external indexes). Use this whenever the user asks a question that should be answered FROM the corpus (not from general knowledge), asks for a literature summary/survey over what's already ingested, asks for the prior art on a record or idea, or asks for a bounded literature hunt on a named topic.
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
   pairing right the first time rather than citing "close enough." **A
   definition worth pinning** (a term the corpus actually defines, not
   just uses) goes into the lexicon rather than only into the answer text:
   `trialerror term propose --lemma "<lemma>" --gloss "<own-words
   reading>" --evidence anchor:<the same anchor_id> --by-launch <your
   launch_id>` (design `docs/reviews/LANE_E_TERM_STORE_DESIGN.md` §4,
   manual route) — own words, never the source sentence verbatim, same
   grounding discipline as the citation marker beside it.

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

## Mode: prior-art bundle (retrieve and anchor; draft nothing)

When the ask is "what is already known near THIS record" rather than "what is
the answer", the output is a bundle, not prose. Run the same stratified
retrieval the hypothesis pipeline runs — near/moderate/far terciles over
embedding distance, 40/40/20 with a far floor of 2 — and return, per
neighbour: its anchor, its arm (near/moderate/far), and its excerpt under the
fence (≤20 words for a `commercial_restricted` source, never the raw chunk).

Three rules make a bundle a bundle:

- **Draft no answer.** No synthesis, no "this suggests", no verdict on
  whether the record is new. A bundle that carries a conclusion has made a
  judgment the screen's own blind judge is supposed to make later, with
  different inputs.
- **Keep the arms.** Report the three arms separately and name empty ones.
  "Nothing in the far arm" is a finding; silently returning four near hits
  reads as coverage.
- **Say what was searched.** The query text, the mode, the k, and the corpus
  snapshot — a bundle whose provenance is unrecorded cannot be re-measured
  against a later one.

The bundle feeds a novelty screen's external reference set. It never
substitutes for one: the screen decides scope and the judge assigns labels.

## Mode: bounded hunt (a query list, fixed up front and logged)

When the ask is "find the literature on X before we commit to a claim about
X", write the query list FIRST, record it, then run exactly those queries:

```
trialerror events append --type lit_hunt_queries --payload '{"topic":"<topic>","queries":["...","..."]}'
trialerror lit search --query "<query>"                      # repeat, once per query on the list
trialerror lit arxiv-semantic --q "<query>" --k 20           # the same list against the local index
trialerror lit acquire --doi <doi> --launch-id <your launch_id>   # only by the lawful OA route
```

- **The list is fixed before the first query runs.** A hunt that grows its
  own list as it goes is a hunt that stops when it finds something
  agreeable.
- **Every query is logged, with its result count** — including the ones that
  returned nothing. A query list with the empty hits removed is a different
  instrument from the one that was committed to.
- **Query text is a literature topic, never content from a record, an idea or
  an unpublished claim.** What leaves the machine is the subject, not the
  work.
- **Outcome per item on the list: corroborated (a second independent source)
  or still a probe.** Write that down against each one. A hunt that ends
  without saying which of its targets remain single-source has not finished,
  and nothing downstream may rest a decision rule on a probe.
- Paywalled or unavailable material goes on the request list rather than
  being worked around.

## Review plan for anything larger than one question (HoH-F4)

A one-off question needs no plan. A survey, a keystone-feeding review, or any
answer you expect to take more than one search/gather cycle gets a plan on
this template before step 1, posted to the review's feed thread or written at
the top of the draft:

> **Objective (bounded but locally complete).** One paragraph: the question
> this review answers, stated so that anyone can tell when it is answered.
> **Sub-questions (at most three)**, each already answerable from what the
> corpus holds (`corpus_stats` first) — a sub-question that needs a source not
> yet ingested is an `/ingest` request, not a priority.
> Order: gaps and contradictions in the program's standing claims first,
> extensions second.
> **Explicitly excluded:** the adjacent topics that would be interesting but
> are not this question. Name them.
> **Preservation gate** — what must NOT regress: previously verified claims
> are not silently softened to fit new evidence; every existing marker keeps
> resolving; fenced sources stay fenced.
> **Acceptance gate** — the smallest end-to-end check: every claim sentence
> carries a resolving marker, quote-check is clean, and any gap is stated as
> a gap rather than filled from general knowledge.
> Do not reuse the previous review's plan. Re-derive this one from the
> question and the latest corpus state.

Source: Harness-of-Harness §3.4.1 and Appendix A.2 (arXiv:2609.01481,
CC BY 4.0) — at most three achievable priorities, an exclusion list, a
Preservation Gate and an Acceptance Gate, blockers before extensions, never
inherit the previous plan. Adapted to a literature review; not a verbatim copy.

## When NOT to apply

- The question IS a hypothesis being adjudicated — `/verify-hypothesis` is
  the only sanctioned path to a verdict row; a lit-review answer must not
  stand in for it.
- The source that would answer it is not in the corpus — `/ingest` first;
  do not answer from general knowledge and cite nothing.
- The user wants the raw text of a `commercial_restricted` source — the fence
  is structural; this skill never reaches for `--unfenced`.
- You would cite "close enough" — a marker bound to an anchor that does not
  support the sentence is worse than an honest gap.
