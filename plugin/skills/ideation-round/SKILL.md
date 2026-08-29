---
name: ideation-round
description: Run a lens-roster ideation round — assign stratified corpus slices to each lens (near/moderate/far over embedding distance, AMENDMENT-3 machinery), book and spawn each lens, then collect full-text feed posts under each lens's own name. Use this when the user asks for a brainstorm/ideation round, a multi-lens pass over the corpus, or an assumption-buster review.
---

# /ideation-round — lens roster + stratified slices + full-text feed posts

Design Section 9.6 (R6) + Section 5.3 (`/ideation-round`) + Section 12 M13
row. This generalizes the origin-project AMENDMENT-3 stratification machinery: every
round draws a seeded, logged, reproducible sample — never an ad-hoc "pick
some docs" pass.

1. **Build (or extend) the round's roster.** One row per lens, each with a
   vantage angle and a seat:

   ```
   trialerror lens roster --round-id <ROUND-id> --add \
     --lens-name "<lens>" --vantage "<angle>" --model-class <top|mid|small> \
     --seat <standard|assumption_buster>
   ```

   Include at least one `assumption_buster` seat (C-0029) — its job is
   structurally different (challenge the round's own framing, not just add
   another angle) and should not be skipped for convenience.

2. **Dry-run the stratification** before committing to a seed, so you can
   sanity-check the near/moderate/far split before it's logged:

   ```
   trialerror lens stratify --model-key <embedding model_key> \
     --home <DOC_ID> [--home <DOC_ID> ...] --candidate <DOC_ID> [--candidate <DOC_ID> ...] \
     [--cluster-of '{"doc_id":"cluster_id",...}']
   ```

3. **Assign for real** — seeded, so the SAME seed reproduces byte-identical
   arms later (design/M13 acceptance criterion, verbatim):

   ```
   trialerror lens assign --round-id <ROUND-id> --slices-per-lens <n> --seed <seed> \
     --weights 40,40,20 --far-floor 2 --inter-cluster-mandate \
     --home <DOC_ID> --candidate <DOC_ID> [...] --launch-id <your launch_id>
   ```

   `--weights`/`--far-floor` default to the AMENDMENT-3 defaults (40/40/20,
   floor 2) — only override with a stated reason. `--inter-cluster-mandate`
   enforces the verbatim inter-cluster mandate when clusters were supplied.
   Restrict to specific lenses with repeated `--roster-id` if this round
   isn't running the whole roster at once.

4. **Book and spawn each lens** from the assignment export (launch-bookable
   rows, ready for `trialerror.budget.book_launch`):

   ```
   trialerror lens export --round-id <ROUND-id>
   ```

   Book each lens's launch (`trialerror budget book ...` or the `book_launch`
   MCP tool), then spawn it with the `launch_id:` token in its prompt — the
   spawn gate consumes the booking on Task invocation (see `/boot`'s note
   on the PreToolUse hook). Each lens's own prompt should carry its
   assigned slice's doc_ids and its vantage/seat framing verbatim — do not
   let a lens improvise its own slice.

5. **Full-text feed posts, under the lens's own name (C-0047).** Every
   lens posts its FULL finding text to the round's feed thread —
   never a summary written by the orchestrator on the lens's behalf, and
   authorship is derived server-side from the posting launch_id (the
   `post_feed` API/MCP tool has no author parameter to spoof).

6. **Audit the log** once every lens has posted:

   ```
   trialerror lens log --round-id <ROUND-id>
   ```

   Confirms every assignment row that was booked actually got a
   corresponding feed post — a lens that was assigned a slice and never
   posted is a dropped launch, not a quiet skip; chase it down before
   closing the round out.

7. **Consolidate, don't just link-dump.** The round's own synthesis (the
   orchestrator's job, not any one lens's) reads every full-text post,
   groups genuinely-overlapping candidates, and produces the round's
   output artifact — a numbered list of raw, unreconciled lens posts is
   not a finished round.
