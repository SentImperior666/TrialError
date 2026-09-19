---
name: ideation-round
description: Run one pre-registered ideation round in nine phases — frame and escrow it, assign one arm per lens over stratified corpus slices, diverge solo under recipe cards, screen for novelty mechanically then with a blind judge, optionally rotate, converge in rooms at the >90% bar, gate and reveal, adopt or kill, and retro. Use this when the user asks for a brainstorm/ideation round, a multi-lens pass over the corpus, an assumption-buster review, or a round's novelty screen or rooms.
---

# /ideation-round — nine phases, pre-registered, seeded, blind where it counts

A **round** is the unit. Every round is launch-booked, pre-registered,
seeded, logged and closed with typed artifacts. Every phase below names the
verb it runs on; nothing in a later phase may change a parameter an earlier
phase escrowed.

## Phase 0 — FRAME (you, no spawns)

1. **Write the round charter** with a context frame (goal / prior state /
   what changes / why) and the bounded-planner block below. It names: the
   question, the home cluster(s), the slice pool, the roster plan with arms,
   the card allocation in within-lens blocks, the room admission order rule,
   the room hyperparameters, the stopping rules and named contingencies.

2. **Snapshot the corpus**: `trialerror query stats`, register row count,
   cluster map id, idea-archive count, and the content hashes of the
   reference sets the screen will measure against. Record them on the round.

3. **Answer the pre-mortem for every judged step** (below) — each "yes"
   needs a named mitigation or the round does not spawn.

4. **Pre-register blind, before any spawn:**

   ```
   trialerror prereg commit --title "<round>" --procedure "aiif-round-v2: <what this round runs>" \
     --params '{"arm_mode":"per_lens","weights":"40,40,20","far_lens_floor":2,"slices_per_lens":5,
                "dedupe_cos":0.92,"cards":{"<block>":["CARD","CARD"]},"external_query_mode":"neutral_abstract",
                "judged_label_scope":"flagged+hits+20pct","room_admission":"seeded-stratified-all",
                "admission_seed":"<seed>","collapse_rerun":{"rule":"..."},"alarms":"descriptive-only",
                "O":["O1","O2","O3","O4","O5","O6","O7"],"adoption_rule":"...","reweight_rule":"..."}'
   ```

   Phase 0 escrows the admission RULE and the admission SEED, never the
   admission order's `hash`. The order is a draw over the round's
   **consolidated** pool, and nothing is consolidated until Phase 3b has run
   — so a hash here is either invented or back-filled later. It gets its own
   escrow in Phase 5, before the first room opens.

   Generators and judges see only the public criteria text. Never back-fill a
   prereg: a round that ran before its escrow is reported as non-compliant,
   never re-described.

5. **Check the model floors.** The program's `[models]` table must carry
   `ideation`, `moderation`, `room_participant`, `novelty_judge` and `gates`
   at `top`, with `consolidation` and `screen` at `mid`. A pool that cannot
   afford `top` returns `DEFERRED` and the round waits — idle beats shallow.
   Never downgrade a phase to fit a pool.

6. **Build the roster.** One row per lens, each with a vantage, a seat and
   its card block (repeat `--recipe-card` IN the seeded order the lens writes
   them in):

   ```
   trialerror lens roster --round-id <ROUND-id> --add \
     --lens-name "<lens>" --vantage "<angle>" --model-class top \
     --seat <standard|assumption_buster|control> [--recipe-card CARD --recipe-card CARD]
   ```

   Seat every round with:
   - **standard** lenses with vantages rotated from the previous round;
   - exactly one **assumption_buster** holding the farthest slice and the
     `NEGATE` card — its job is structurally different (attack the round's own
     framing, with a stake) and it is never skipped for convenience;
   - exactly one **control** seat, which carries **no card and no
     `requirements` field** and runs the pre-framework brief at the same slice
     size and budget. It is a measurement seat: it is **excluded from the arm
     mix and from the far floor**, it is not counted toward the round's seat
     count, and it is what the adoption rule compares the framework against. A
     control seat carrying a card is refused, and rightly — it would report a
     comparison the round did not run.

## Phase 1 — SLICE (mechanical, no model)

Dry-run the stratification first, so you can sanity-check the tercile cuts
before anything is logged:

```
trialerror lens stratify --model-key <embedding model_key> \
  --home <DOC_ID> [--home <DOC_ID> ...] --candidate <DOC_ID> [--candidate <DOC_ID> ...] \
  [--cluster-of '{"doc_id":"cluster_id",...}']
```

Then assign for real, **one arm per lens**:

```
trialerror lens assign --round-id <ROUND-id> --arm-per-lens \
  --slices-per-lens 5 --seed <seed> --weights 40,40,20 --far-floor 2 \
  --inter-cluster-mandate --home <DOC_ID> --candidate <DOC_ID> [...] --launch-id <your launch_id>
```

`--arm-per-lens` is the assignment semantics this protocol runs: the
40/40/20 weights with a hard floor of two split the **roster** across the
arms, so every lens gets ONE arm and draws its whole slice from it (roster 6
→ 3 near / 1 moderate / 2 far; roster 12 → 5/5/2). `--far-floor` then counts
far LENSES, not far slices; the buster is pre-placed in the far arm and
counts toward that floor; the control lands in the modal arm and counts
toward neither. `tier` becomes a lens property every idea it writes
inherits, which is what makes per-arm n mean "lenses per arm".

Without `--arm-per-lens` the same flags split each lens's own slice instead —
the harness's earlier generalisation, a different design, and **not** this
protocol's. Running it is allowed only as a declared interim: say so in the
prereg params, write `tier=mixed` on the records, and report the arm outcome
from set-distance terciles, labelled as such.

Slice size is **5 documents, cap 6** — a lens that reads more is not reading
its slice, it is browsing.

Before any booking, these must pass:

```
trialerror doctor --program-root <root> --only far_arm_floor_honored --only far_lens_floor_honored \
  --only no_duplicate_slice --only cluster_coverage --only recipe_rotation_honored
```

Post the assignment table (lens, seat, arm, set_distance, card block, seed)
to the round's feed thread with a context frame **before any spawn**.

## Phase 2 — DIVERGE, solo (the expensive phase)

Book from the export, then spawn one `lens` agent per row:

```
trialerror lens export --round-id <ROUND-id>
```

Each exported row's `attrs` carries the lens's `roster_id` and its
`assign_ids`, and booking with `**row` keeps them on the launch — which is
how the retrieval scope and the citation audit resolve that launch's slice.
A booking made any other way must carry the link itself:

```
trialerror budget book --purpose ideation --agent-kind lens ... \
  --assign-id <ASGN-id> [--assign-id <ASGN-id> ...]
```

An `--assign-id` that names no row refuses the whole booking (nothing is
booked), rather than linking part of a slice.

Each lens's prompt carries, verbatim: its slice doc_ids, its vantage and
seat, **its card block**, the idea-record schema, its per-lens record count,
and an **abstract exclusion list** — covered cluster ids, home cells and
register families, and nothing else. Never a prior round's idea text, never a
rubric, never a dossier, never a verdict, never another lens's output. Spawn
with the `launch_id:` token in the prompt; the spawn gate consumes the
booking and refuses a spawn whose model is below the class the booking
claimed.

**Full-text feed posts, under the lens's own name.** Every lens posts its
FULL finding text to the round's thread — never a summary written in the
lens's name, and authorship is derived server-side from the posting
launch_id. Write each record through the idea writer and back-fill its feed
post ref.

Records come back as JSON and are written through the intake verb, which
validates the whole file before writing any of it (one bad record → nothing
written) and refuses a field the schema does not read:

```
trialerror lens intake --round-id <ROUND-id> --records <records.json> \
  --author-launch <lens launch_id> [--assign-id <ASGN-id> ...] [--arm <arm>]
```

Each record carries `statement`, `probe` and `provenance` as an OBJECT with
`docs: [doc_id, ...]` (the slice documents it was written from — `slice` is
accepted as an alias), `operation_declared` on both axes
(`opportunity:<...>, method:<...>`), and `requirements` as a list or as
bullets.

Close the phase with `trialerror lens log --round-id <ROUND-id>`: it reports
`posted` per lens with the lens's own launch, and a booked lens that never
posted is a dropped launch to chase, not a quiet skip. The gate suite's
`lens_log_reconciled` reads exactly that shape — it FAILs any unposted lens,
and it FAILs a log that reconciles no lens at all, so a mistyped
`--round-id` reads as a refusal rather than as a clean round.

## Phase 3 — NOVELTY SCREEN, in two halves

**3a, mechanical, incremental, no model** — after each lens batch posts:

```
trialerror lens screen --round-id <ROUND-id> --mechanical [--external-query-mode neutral_abstract \
  --external-provider <index>] [--alarms '<pre-registered values>']
```

It merges near-duplicates (cosine ≥ 0.92 **and** the same home cell), flags
records sitting on an inventory row at the same threshold, records the
distance statistics and within-round terciles, retrieves prior art
stratified, queries the external index under the pre-registered mode only,
and reports declared-operation entropy, the pairwise-similarity distribution
and template mass. Nothing a generator ever sees. Distances are recorded and
**never thresholded into "novel"**.

**3b, judged, at round end only** — a separate top-class verifier launch with
disjoint context:

```
trialerror lens screen --round-id <ROUND-id> --judged-prep --seed <seed> \
  --judge-envelopes-out <dir>
trialerror lens screen --round-id <ROUND-id> --record-verdicts <labels.json> --launch-id <verifier launch_id> \
  --prereg-id <PREREG-id> --executed-procedure-file <procedure.md> --executed-params '<params>'
```

**The judge never sees a plant id.** `--judged-prep` writes a `judge_view`
per envelope with a masked `J-<n>` id (the mask stays in the batch file), and
`--judge-envelopes-out <dir>` writes one masked view per file — that is what
goes into the judge's prompt. `--record-verdicts` takes labels keyed by the
masked id, the real id or a mix.

**Pass the procedure as a FILE.** `--executed-procedure-file` hashes what is
on disk; `--executed-procedure "$(cat file)"` strips the trailing newline, so
the hash differs from the committed one and `prereg_compliant=false` is
stamped for a procedure that was followed. The two flags are mutually
exclusive, and a mismatch names which of the two hashes (procedure, params)
disagreed.

The judged scope is flagged records + retrieval hits + a seeded 20% sample of
the rest; everything else carries the mechanical `no-close-neighbour` and
`unjudged` pair, which is **not** `new-mechanism`. Plants ride in every
judged batch and a missed inventory plant fails the batch: the labels are
still written and marked, and nothing is consolidated on that judge's word.
Nothing is killed in this phase, and **no dossier field gates or orders room
admission** — "this exists as register row X" is information for the room,
not a verdict.

## Phase 4 — ROTATE / RECOMBINE (optional, only on a trigger)

Three triggers, and no others: the collapse monitor is green and budget
allows (brainwriting — re-spawn each lens with k=3 consolidated records from
other lenses, farthest from its own and from a different arm or home cluster,
in ONE envelope, under a rotated card, authorship withheld; derivative
records carry `parent_ids` and re-enter Phase 3 as a delta); a stalled line
is re-attacked by a FRESH lens launch with a different vantage, a
one-paragraph resumption cue and the stalled text withheld; or the
pre-registered `collapse_rerun` contingency fires. At most one re-run per
round, never a re-sample, never a rule change outside the escrowed
contingency.

## Phase 5 — CONVERGE (rooms)

**Admission order first, and escrowed.** Every consolidated idea is roomed;
the order is a seeded draw stratified on (arm, card), packed into rooms of
the charter size in charter-size batches, with the remainder carried in that
same order:

```
trialerror room admission-order --round-id <ROUND-id> --seed <seed>
```

**Escrow the order in its own second commit, here** — after Phase 3b, before
the first room opens, while no room's result exists to re-order toward:

```
trialerror prereg commit --title "<round> admission order" \
  --procedure "aiif-round-v2 admission order over the consolidated pool" \
  --params '{"round_id":"<ROUND-id>","room_admission":"seeded-stratified-all","admission_seed":"<seed>",
             "admission_order_hash":"<hash from the command above>","n_ideas":<count>}'
```

That commit is the round's `admission_escrow`, and the gate suite compares
the order run against it, naming which escrow it read. Phase 0's params carry
the rule and the seed; this one carries the order. Dossier labels, distances
and the collapse flag never gate or order admission.

**A Phase 4 derivative closes the pool before the order is escrowed.** A
derivative re-enters Phase 3 and consolidates into the same pool, which
changes the draw — so either every derivative is consolidated before this
commit, or the derivatives carry to the NEXT round. Never re-escrow an order
mid-sitting: that is a re-ordering with a new hash on it.

A pool smaller than one room seats nobody: every record lands in the
remainder, and the result says so in a `note` naming the flag. A round-0 dry
run of two ideas is exactly that case — pass `--ideas-per-room 2`, declare
the smaller sitting in the prereg params, and read the note rather than an
empty `rooms` list as a failed draw.

Open each room with the procedure's flags on:

```
trialerror room create --topic "<idea>" --participants "<lens>,<lens>" \
  --dps '[{"dp_id":"DP1","prompt":"<criterion (a)>","idea_id":"IDEA-x","dossier_labels":{...}}]' \
  --rank-all --blind-first-turn --buster "<lens>" --by-launch <your launch_id>
```

- **Participants**: 2–3 lenses from different home clusters, with the buster
  present wherever the roster allows. A participant whose lens authored any
  idea the room vets is refused at creation and again at scoring — checked by
  LENS NAME, because every turn is its own spawn and the same lens posts
  under a new launch id each time.
- **Blind first turn**: round 1 is simultaneous; no participant sees another's
  round-1 turn while round 1 is running.
- **Turn kinds**: `position`, `question`, `closure`. A `closure` in a
  participant's own first round on a point is refused — weak entailment
  first, and no preference statements before round 2.
- **The buster's position** is recorded once and re-injected verbatim into
  every one of its envelopes. Do not paraphrase it, shorten it, or let it be
  re-derived per spawn.
- **One DP per idea**: the record verbatim, the two criterion questions, and
  the dossier's **labels** only — never its rationale, its distances or its
  flags.
- **Rank-all before any verdict**: each participant files a complete ranking
  of every idea on each criterion and a discrete stance per idea
  (`trialerror room stance --file ...`). The room cannot converge until every
  seat has filed.
- **Neutral extraction before scoring**: run the extract pass per point and
  record it (`trialerror room extracts --file ...`), then score with
  `--require-extracts`. The moderator reads the extracts, never the raw
  prose, and never the author of anything.
- **Label first, number computed**: the moderator returns the discrete label;
  `agreement_pct` is computed from the structured stances against the fixed
  **>90% bar**, which is the selection variable and is named as such in every
  report. Room survival is both-or-eliminated at that bar.

Escalate a deadlock with `trialerror room freeze --reason "<what is unresolved>"`
rather than talking a room into agreement.

## Phase 6 — CRITIC GATE, then REVEAL

Take the round synthesis and the probe report through `/gate-critic`. Tier 1
includes the `aiif_round` suite; Tier 2 carries this round's pre-mortem in
the critic's brief. Then reveal the pre-registration and check compliance
against the procedure actually run. A non-compliant round is **reported as
non-compliant** — never re-described to match what it did.

**6b — regularities note.** Read the survivors once and write a ≤300-word
note of what they share and what every killed idea had in common. The NEXT
round's Phase 0 may turn one regularity into a card constraint. It may never
become a selection rule inside the round that produced it.

## Phase 7 — ADOPT / KILL / LOG

Adoption is `idea.status='promoted'` plus a registered verdict record plus
entry into the expansion loop with stated adoption and kill conditions —
nothing else counts as "this round produced an idea". A kill is
both-or-eliminated at the bar: `status='eliminated'`, archived with reasons,
revivable only by ruling. Near-duplicates fold to `merged`. Eliminated and
merged records stay in the reference sets forever.

Report the pre-registered outcomes with **per-cell n disclosed and no
significance language** — per-arm and per-card n is single digits, so every
comparison is directional. Register the probe report. The operator sees the
reveal beside the results and rules on anything beyond the pre-committed
rules.

**A term coined or redefined in the round goes into the lexicon here, not
later:** `trialerror term propose --origin ideation --origin-ref IDEA-<id>
--evidence idea:IDEA-<id> --lemma "<lemma>" --gloss "<own-words reading>"
--by-launch <your launch_id>`. When the idea is promoted, `trialerror term
accept <SENSE-id> --by-launch <id>` in the same pass; when it does not
survive, `trialerror term retire <SENSE-id> --by-launch <id>` rather than
leaving an orphaned proposal for someone else to find.

## Phase 8 — RETRO (operator)

Round card and Since-you-left. Roster, card catalogue, thresholds and
hyperparameters change **only between rounds**, each an event line naming the
signal that triggered it. The in-round `collapse_rerun` contingency changes
nothing here: its allocation rule was escrowed at FRAME.

## Stopping rules and contingencies — fixed at FRAME, first to fire wins

- 400 pre-dedupe records.
- Three consecutive lens batches each under 20% **mechanically-new** (not a
  near-duplicate of the archive and not flagged against the inventory — the
  incremental 3a measure). The judged "new-mechanism" rate is **not** a
  stopping rule: it is not measured until round end.
- Roster exhausted with coverage complete.
- Floor of 100 post-dedupe records before rooms, else report to the operator.
- **Collapse**, measured per batch by 3a: median pairwise cosine above the
  pre-registered bound, or declared-operation entropy below its floor. Alarm
  values are **descriptive only** until a round's own first batch sets them —
  a flag raised against a threshold invented at screen time is a decision
  rule written after seeing the data. When a flag does fire, acknowledge it
  (`trialerror events append --type round_collapse_acknowledged --payload
  '{"round_id":"<ROUND-id>","batch_id":"<batch>"}'`) or fire the escrowed
  `collapse_rerun`; never close a round over an unacknowledged flag.
- **Budget**: a `DEFERRED` booking pauses the round. Never downgrade.
- **Time**: a round not closed within four sittings closes as PARTIAL with
  its prereg revealed. Room batches not yet run carry to the next round's
  Phase 5 **in their pre-registered order**.

## Round plan: bounded but locally complete

Write the round's plan to this template in Phase 0. It goes into every lens's
prompt verbatim (after the slice and seat framing) and opens the round's feed
thread, so a reader can tell what the round set out to move:

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
> seed-reproducible arms, the far-lens floor, the full-text rule, the >90%
> bar.
> **Acceptance gate** — the smallest end-to-end check that says the round
> succeeded: every booked lens posted, `trialerror lens log` clean, every
> consolidated record carrying its screen record, the synthesis artifact gated
> and the prereg revealed.
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

## Pre-mortem for every judged step of a round — REQUIRED

A round has judged steps: the screen's judge labelling records, a moderator
scoring convergence, a critic on the round's synthesis. Before any of them
spawns, answer the three questions for that step and record them in an event
row (`trialerror events append --type round_premortem --payload
'{"round_id":"<ROUND-id>","step":"...","proxy_gameable":...,
"harness_escapable":...,"judge_steerable":...,"mitigations":[...]}'`) and in
the prereg text. Each "yes" needs a named mitigation or the step does not
spawn:

- **(i) Is the proxy itself gameable?** Can a turn be written to *read*
  convergent, or a record to *sound* new (vague enough to retrieve nothing),
  without being so?
- **(ii) Is the harness escapable?** Can a lens see another lens's slice, the
  rubric, or the register; can your own ordering of the rooms leak a
  preference?
- **(iii) Can the judge be steered by the content it judges?** Self-assessment
  in the record ("unlike any existing system"), framing aimed at the
  moderator, authorship visible to the scorer?

Standing mitigations this procedure already carries: slices are seeded and
logged; authorship is derived server-side from the launch id; the far-lens
floor is not a score anything can climb; lenses generate blind to the rubric
and to each other, and only the planning phases see everything; the admission
order is escrowed before any dossier exists; judge envelopes carry no
authorship and no self-assessment; the moderator reads neutral extracts and
`agreement_pct` is computed from structured stances rather than read out of
prose. Same three questions as the gate's own pre-mortem; paper: "AI Finds A
Way" (arXiv:2608.23875) §5 and §7.

## When NOT to apply

- The question has one angle and wants one cited answer — that is
  `/lit-review`, not a round.
- The lenses cannot be booked at the required class. Idle beats shallow:
  wait, do not downgrade the roster to fit.
- You would skip the `assumption_buster` seat, or the `control` seat, "for
  convenience" — then it is not a round, and the adoption rule has nothing to
  compare.
- You have not pre-registered, or you have already seen results and would
  commit the procedure now.
- To produce a summary written in a lens's name, or to let a lens improvise
  its own slice.
- On a corpus that has not finished ingesting — the stratification is only as
  reproducible as the embedding set it draws from, and an unembedded
  inventory refuses the screen outright.
