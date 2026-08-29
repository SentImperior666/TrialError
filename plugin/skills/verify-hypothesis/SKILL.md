---
name: verify-hypothesis
description: Run the hypothesis-vs-literature pipeline — stratified retrieve, contracrow-classify each evidence chunk, aggregate a label distribution, and write a typed verdict artifact. Use this when the user asks you to check, test, or verify a hypothesis against the corpus, or when a keystone artifact needs its central claim gated on evidence rather than asserted.
---

# /verify-hypothesis — retrieve → contracrow classify → verdict artifact

Design Section 8.2 (declared v0, R4) + Section 5.3 (`/verify-hypothesis`) +
Section 12 M9 row. This is the ONLY sanctioned path to a `hypothesis`
verdict row — never hand-write a verdict, and never let a lit-review
answer stand in for this pipeline once a hypothesis is actually being
adjudicated.

1. **Preregister first — default for keystones.** Committing the
   procedure+params BLIND before you see results is what makes the verdict
   trustworthy; skip it only for a genuinely exploratory, non-keystone
   check:

   ```
   trialerror prereg commit --title "<hyp title>" --procedure "hypothesis-v1: stratified retrieve + contracrow" \
     --params '{"k_total":6,"weights":"40,40,20","far_floor":2}'
   ```

   Then pass `--prereg`/`--prereg-title` to the `hypothesis` action below
   so the committed record and this run are linked; `prereg_compliant` on
   the final verdict is stamped from that link, not asserted by hand.

2. **Run the pipeline.** Stratified retrieval forces breadth
   (near/moderate/far terciles over embedding distance, AMENDMENT-3's
   40/40/20 default + far-arm floor — the same machinery `/ideation-round`
   uses):

   ```
   trialerror verify hypothesis --text "<the hypothesis statement>" \
     --by-launch <your launch_id> --k-total 6 --weights 40,40,20 --far-floor 2 \
     --mode hybrid --judgments-file <path.json> --prereg --prereg-title "<title>"
   ```

   (Use `--id HYP-xxx` instead of `--text` if a hypothesis row already
   exists.) `--judgments-file` is REQUIRED — this CLI process never calls
   an LLM itself (design's stated judgment boundary). It is a JSON file
   `{"<chunk_id>": {"label": "...", "note": "..."}}` covering EVERY
   retrieved evidence chunk.

3. **Classify with the vendored contracrow prompt, not your own rubric.**
   For each retrieved chunk, score it against the hypothesis on the
   11-point ordinal scale from `explicit contradiction` … `lack of
   evidence` … `explicit agreement` (paper-qa contracrow, vendored
   Apache-2.0). Every sentence of your judgment must cite its own anchor —
   forced-XML response shape, not free text. Write each chunk's judgment
   into the `--judgments-file` before re-running, or run the classification
   as a booked, tool-locked read-only verifier subagent per the design and
   feed its output back in.

4. **Read the aggregate, don't just skim the top label.** The verdict's
   label distribution across near/moderate/far slices IS the finding —
   a hypothesis that only agrees in the near slice and contradicts or
   lacks evidence further out is a materially different result than one
   that holds up across all three, even if both produce the same headline
   label. Independence clustering (syndicated/near-duplicate sources
   counted once) is v1-deferred — for a corpus you know has duplicated
   sources, note that caveat explicitly in your write-up rather than
   silently trusting the raw distribution.

5. **The verdict artifact is typed and evidence-anchored** — gated if the
   hypothesis backs a keystone. Do not paraphrase the verdict's own label
   when reporting it upstream; quote it.

6. **Reproduction, when the hypothesis has an attached script:**
   `trialerror verify reproduce <verdict_id> --by-launch <id> [--gate-id <id>]`
   re-runs it and byte-compares output sha to the recorded expectation — a
   mismatch blocks `gate apply-union` on any gate this verdict feeds
   (design Section 4.2/8.3); do not talk yourself past a reproduction
   mismatch, escalate it.
