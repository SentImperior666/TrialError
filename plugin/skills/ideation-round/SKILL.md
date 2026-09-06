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
   spawn gate consumes the booking on subagent invocation (see `/boot`'s note
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

## Round plan: bounded but locally complete (HoH-F4)

Write the round's plan to this template before step 1. It goes into every
lens's prompt verbatim (after the slice and seat framing) and opens the
round's feed thread, so a reader can tell what the round set out to move:

> **Objective (bounded but locally complete).** One paragraph: what this
> round must move, stated so that anyone can tell when it is done.
> **Priorities (at most three)**, each already supported by the slices and
> the roster — no priority that needs material the round does not have.
> Order them blockers and regressions first (holes in the program's standing
> claims), extensions second.
> **Explicitly excluded this round:** the adjacent questions that would be
> "nice while we're here", re-framings of the program, re-litigation of
> settled rulings. Name them, so a lens cannot drift into them by accident.
> **Preservation gate** — what must NOT regress: the standing claims, the
> seed-reproducible arms (`--seed`), the far-arm floor, the full-text rule
> (C-0047).
> **Acceptance gate** — the smallest end-to-end check that says the round
> succeeded: every booked lens posted, `trialerror lens log` clean, the
> synthesis artifact created with genuinely grouped candidates.
> Do not request or reconstruct the previous round's plan. Re-derive this
> one from the program's spec and the latest evidence (the previous round's
> outputs), never from the previous round's plan.

Source: Harness-of-Harness §3.4.1 — "The objective is bounded but locally
complete ... Unrelated refactoring and opportunistic feature expansion remain
outside the loop" — and its Appendix A.2 Planner template (arXiv:2609.01481,
CC BY 4.0): at most three achievable priorities already supported by the
scaffold; broad rewrites and unrelated architecture changes excluded; a
Preservation Gate and an Acceptance Gate alongside the list; blockers and
regressions before product extensions; and "do not request or reconstruct the
previous development document" — the paper's largest single ablation delta
(-8.13). The block above is ADAPTED from those clauses to a research round;
it is not a verbatim copy of A.2.

## Pre-mortem for every judged step of a round (ai-finds-a-way-F1) — REQUIRED

A round has judged steps — a moderator scoring convergence in a room, a
screen labelling ideas, a critic on the round's synthesis artifact. Before
any of them spawns, answer the three questions for that step and record them
in an event row (`trialerror events append --type round_premortem --payload
'{"round_id":"<ROUND-id>","step":"...","proxy_gameable":...,
"harness_escapable":...,"judge_steerable":...,"mitigations":[...]}'`). Each
"yes" needs a named mitigation or the step does not spawn:

- **(i) Is the proxy itself gameable?** Can a turn be written to *read*
  convergent, or an idea to *sound* new (vague enough to retrieve nothing),
  without being so?
- **(ii) Is the harness escapable?** Can a lens see another lens's slice, the
  consolidation rubric, or the register; can the orchestrator's ordering leak
  a preference?
- **(iii) Can the judge be steered by the content it judges?** Self-assessment
  in the post ("unlike any existing system"), framing aimed at the moderator,
  authorship visible to the scorer?

Standing mitigations this skill already carries: slices are seeded and
logged; authorship is derived server-side from the launch id (C-0047); the
far-arm floor is not a score anything can climb; lenses generate blind to the
rubric and to each other, and only the planning step sees everything (the
phase rule that reconciles WikiSkill's rollout blindness with HoH's evidence
feed-forward). Stripping self-assessment from judge envelopes and decoupling
a turn from its author's framing (ai-finds-a-way-F2) is the follow-up for the
day a real subagent judge is wired into rooms. Same three questions as AIIF v2
§5.1; paper: "AI Finds A Way" (arXiv:2608.23875) §5 and §7.

## When NOT to apply

- The question has one angle and wants one cited answer — that is
  `/lit-review`, not a round.
- The lenses cannot be booked (the pool is `DEFERRED` for the required model
  class). Idle beats shallow: wait, do not downgrade the roster to fit.
- You would skip the `assumption_buster` seat "for convenience" (C-0029) —
  then it is not a round.
- To produce a summary written by the orchestrator in a lens's name, or to
  let a lens improvise its own slice.
- On a corpus that has not finished ingesting — the stratification is only
  as reproducible as the embedding set it draws from.
