# AIIF — the ideation protocol as a TrialError feature

Status: published design (2026-09-06). This document describes the protocol as a harness feature. Nothing program-specific appears in it by design: no corpus names, no program identifiers, no round charters. Its companion, the evidence base (`docs/AIIF_EVIDENCE_BASE.md`), lists the sources behind each evidence code and follows this document. Section 9 lists what is planned rather than shipped; the README's roadmap carries the same items.

## 0 · What AIIF is, in one paragraph

AIIF (AI Ideation Framework) is the protocol TrialError uses to run an **ideation round**: a booked, pre-registered, seeded unit of work in which several independent agent "lenses" generate candidate ideas from stratified slices of a corpus, a mechanical screen and a judged screen measure how novel and how well-grounded each idea is, a small recombination pass and a set of adversarial "rooms" test the survivors, and a critic gate decides what is adopted, killed or logged. Its design choices are not taste: each one binds to a finding from the creativity, group-cognition or LLM-ideation literature (see the evidence base), and every tunable carries a decision rule fixed before data. A round always includes a **matched-budget control lens** running the pre-AIIF brief, so "AIIF improved ideation" is a measured claim, not an assumption.

## 1 · Where it lives in the harness

| Piece | Harness surface | State |
|---|---|---|
| Lens, verifier and critic agents | `plugin/agents/{lens,critic,verifier}.md` (tool-locked; model Fable) | shipped |
| Round driver | `plugin/skills/ideation-round/SKILL.md` | shipped as Phases 0-8 |
| Slicing (near / moderate / far arms per lens) | `trialerror lens stratify`, `trialerror lens assign --arm-per-lens` | shipped; a lens's arm lives on its assignment rows and is re-derived for the export rather than stored on the roster row |
| Mechanical novelty screen | `trialerror lens screen --mechanical` (`trialerror/lens/novelty.py`) | shipped |
| Judged screen | verifier launches over novelty dossiers, `verdict` rows with a procedure version | envelopes, plants and label recording shipped; the launch that spawns the judge is not in this code |
| Rooms (converge phase) | `trialerror room …` (admission order, blind first turn, turn kinds, rank-all stances, neutral extracts, computed `agreement_pct`) | shipped; the extract and moderator passes take their results back through the CLI rather than spawning them |
| Pre-registration and reveal | `trialerror prereg commit / reveal`, blind params, escrow hash | shipped |
| Critic gate | `/gate-critic`, `trialerror gate …` | shipped |
| Budgets and model floors | `budget book` per spawn; `[models]` table in `trialerror.toml`; the spawn gate compares the spawned model with the booked class | shipped; the program must create its own table (the scaffold ships the example commented out, as it ships every table) |
| Acceptance | `trialerror eval gate --suite aiif_round` (fourteen checks over one round's artifacts) | shipped |
| Convergent discovery (after a round closes) | `trialerror lens recheck` enqueues the `convergent_recheck` job; links land on `idea.convergent_with` | shipped |

## 2 · Principles (each names the mechanism it binds)

The evidence codes (E1–E10) are expanded in the evidence base document.

| # | Principle | Evidence | Binds to |
|---|---|---|---|
| P1 | Engineer distance and constraint into the **inputs**; never ask for them in the prompt. | E1, E2, E9a | slice assignment; recipe cards; the lens brief |
| P2 | Near and moderate slices dominate; **far** is a measured minority arm of lenses with a floor of two lenses. Each lens's whole slice comes from one arm; the arm is a lens property inherited by its ideas. | E1 | `lens assign --arm-per-lens`; outcome O4 |
| P3 | Break the default template by an **operator**, not by exhortation: constraint-bearing recipe cards; a "state requirements first" record field measured as a hypothesis, not assumed. | E2 | cards (§4); `requirements` field (§5) |
| P4 | **Solo divergence first**; exchange is a later, heterogeneous, small phase; nothing generates inside a room. | E3, E4, E5 | Phase 2 solo lenses; Phase 4 brainwriting (k=3, farthest-first); rooms only in Phase 5 |
| P5 | Quantity, then **delayed selection**; judged evaluation is a separate phase with separate launches and criteria the generators never saw. The mechanical half of the screen may run incrementally because no generator sees it. | E4, E9c | Phase 3 split |
| P6 | A round is a **distribution** — monitored, and single-source claims stay single-source until corroborated. Entropy over a move taxonomy, template mass and pairwise distance are recorded per batch; a collapsed batch triggers a pre-registered contingency, never a re-sample or a mid-round rule change. | E9a, E9c | collapse monitor; `collapse_rerun` contingency |
| P7 | **Dissent is a seat** with a stake and a consistent recorded position, and its effect is measured. | E6 | `assumption_buster` seat with the farthest slice; NEGATE card; position envelope in rooms; outcome O7 |
| P8 | Rooms run **anti-hidden-profile** procedure by construction: blind, weak-entailment first turn; no preferences before round 2; rank-all before verdicts; declared slice provenance; external append-only record; critical-thinking norm. | E7, E8 | room configuration and turn kinds |
| P9 | Novelty is **relative to named reference sets at a timestamp**; convergent discovery is logged, never penalised. | E1, E9c | novelty dossier + reference snapshot hash; re-check job |
| P10 | **Every judge is a target.** Judges see content, not framing or identity; discrete or pairwise over continuous wherever anything optimises against the output; no iterated optimisation against a judge. | E9c | pre-mortem; envelope rules; gate checklist |
| P11 | **Value is what an idea runs against.** Every record carries an executable probe sketch; adoption requires the program's representation and proof standard, not a judge's taste. | E9b | idea record `probe` |
| P12 | **In-vivo over benchmark, with a matched-budget control.** Every tunable has a decision rule fixed before data; a rule may only act on cells with at least two lenses. | methodology (pre-registration) | prereg; CONTROL seat; outcomes O1–O7 |
| P13 | **The judge is an instrument; validate it.** Plants, inter-judge agreement, a predictive check, ordinal arithmetic only. | E10 | §7.3 calibration |
| P14 | **Inspectable structure over prose; keep stepping stones alive.** Records, hashes, provenance; far floor; nothing pruned on a proxy score; killed ideas revivable by ruling. | E9c, E8 (external records) | idea schema; archive |
| P15 | **Emergence needs a genre.** The record schema, the decision-point prompts and a fixed convergence bar are the genre; rooms without them produce conversation, not theory. | E8 | room decision-point payloads |
| P16 | **Nothing load-bearing is a prompt**, and where it still is, the document says so. Every barrier is classed *enforced* (code), *audited* (doctor check) or *convention* (prompt), and conventions are listed with their audit. | E9c | §6; the pre-mortem |

## 3 · One round, phase by phase

0. **FRAME** (orchestrator, no spawns). Round charter with a context frame; corpus snapshot and reference-set hashes; blind pre-registration of every parameter (arm mode, weights, far floor, slices per lens, dedupe threshold, cards per block, external-query mode, the room admission RULE and its seed, contingencies); model floors; roster (standard seats with rotated vantages, one assumption-buster with the farthest slice, one CONTROL seat outside the roster count).
1. **SLICE** (mechanical). Stratify the corpus by embedding distance from the home cells into near / moderate / far terciles; assign whole slices per lens from one arm under the seeded weights and the far floor.
2. **DIVERGE, solo** (lenses, the expensive phase). Each lens is booked and spawned with its slice, vantage, seat, its card block (two cards in seeded order for standard lenses), the record schema and an abstract exclusion list; it never sees peer ideas, dossiers, rubrics or the program's inventory. It returns records; the orchestrator posts them verbatim under the lens's own launch identity.
3. **NOVELTY SCREEN**, two halves. 3a mechanical and incremental: dedupe, known-mechanism flagging, distance statistics against the reference sets, pairwise similarity distribution, declared-operation counts. 3b judged: verifier launches read dossiers (requirements, statement, home cell, probe, retrieved evidence) with author, seat, card and rationale stripped, label against fixed vocabularies, with plants and a re-judge share for calibration.
4. **ROTATE / RECOMBINE** (optional). Brainwriting: each lens re-spawned with k=3 consolidated records from other lenses, farthest from its own and from a different arm or cluster, in one envelope.
5. **CONVERGE** (rooms). Every consolidated idea is roomed; seeded stratified admission on (arm, card), with the ORDER escrowed in its own commit here — after the judged screen, which is when the pool it is drawn over first exists, and before the first room opens; rooms of the charter size in batches; decision points with a fixed convergence bar; the buster's position envelope; neutral extracts for the moderator.
6. **CRITIC GATE and REVEAL.** Synthesis and probe report through the two-tier gate (mechanical suite, then the read-only critic with the pre-mortem in its brief); pre-registration revealed at the gate.
7. **ADOPT / KILL / LOG.** Adoption is a status plus a registered verdict plus entry into the program's expansion loop with kill conditions; kills are archived and revivable by ruling.
8. **RETRO** (operator). Round card; catalogue, threshold and hyperparameter changes only between rounds, each an event line with its triggering signal.

## 4 · Recipe cards

At most four cards per round, drawn from a rotating catalogue; every standard lens writes under two cards in a seeded order (a within-lens block design) so card is not confounded with vantage, slice or seed; every card in play is held by at least two lenses. The buster holds NEGATE; the CONTROL seat holds no card. Every card ends with the same instantiation step: name the probe that would test the idea. The card catalogue (status in brackets): MISMATCH — a slice document whose constraints conflict with the home cell's default exemplar; every idea must satisfy both [evidence-backed]; SCALE-SHIFT — the same mechanism at three scales; what must the state model change? [evidence-backed]; FAILURE-FIRST — start from a known failure class and design what prevents it [evidence-backed]; TRANSFER — carry a mechanism from the slice into the home cluster by mapping relations, not surfaces; two relations must transfer [evidence-backed]; INVERT — reverse a flow, an order or an asymmetry [design hypothesis]; FORMALIZE-LOCAL — an exact state transition for one under-specified mechanism [single-source]; REPLACE — swap one component for one that does a different job [single-source]; DECOUPLE — separate two things the literature treats as one [single-source]; NEGATE (buster) — ideas that hold only if a standing assumption is false, citing the far slice [evidence-backed]; CONTROL — the pre-AIIF brief at the same slice size, arm and budget [control].

## 5 · The idea record

`id · requirements (2–5 lines; the H-AF hypothesis field; absent in CONTROL records) · statement (≤150 words) · home cell · assumed circle · provenance {set id, docs, recipe card, declared operation, mismatch doc?} · tier (= the lens's arm) · set distance · probe (2–6 lines: what one would implement or simulate, against what) · author rationale (judges never see it) · surprise (self-report; judges never see it)`. Records are hashed and archived; the archive is never reset; generators see only covered-region labels from it, never idea bodies.

## 6 · Roles and information barriers

| Role | Sees | Never sees | Barrier class |
|---|---|---|---|
| Lens (standard / buster / CONTROL) | slice, vantage, seat, card block, schema, abstract exclusion list | peer ideas, dossiers, verdicts, rubrics, novelty feedback, escrowed weights, the inventory | inventory: **enforced** (server-side source-kind exclusion in the retrieval engine, lifted only by naming the kind explicitly) on every surface an agent reaches through the knowledge tools — the ranked ones and the id- and quote-addressed ones alike, the graph tier included; not on the two API/CLI-only graph walks or the counts-only corpus summary, which the engine names rather than implying "every"; slice scope: **enforced** for a launch whose booking declares its slice, resolved the way the audit resolves it (the slice attr, else the booking's assignment or roster rows — all three emitted by `lens export`), and a declared-EMPTY slice restricts to nothing rather than lifting the restriction; **audited** (`lens_citations_within_slice`) for a launch booked outside that path, which declares none of the three |
| Recombiner | k=3 peer records without authorship | judge output, its own prior scoring | envelope (enforced) |
| Mechanical screen | statements + structured fields | rationale, surprise | code |
| Novelty judge | dossier fields + retrieved text, pairwise | author, seat, card, rationale, surprise, other judges' labels | envelope (enforced) |
| Room participant | the decision-point record, criteria, dossier labels, prior turns from round 2 | idea author identity, dossier rationale, moderator scores | envelope + a NEITHER rule |
| Moderator | neutral extracts, rank-all and final stances | authors, raw self-assessments, raw prose at scoring | envelope (code) |
| Critic | artifact, revealed prereg, evidence bundles, pre-mortem | generator prompts, the draft synthesis | tools allowlist (enforced) |
| Operator | feed, dashboard, reveal, round card | escrow before reveal | prereg escrow (enforced) |

## 7 · Measurement

7.1 **Novelty** is a dossier, not a score: distances to named reference sets (the corpus at a snapshot, the program's request lists, the idea archive, an external literature query in statement mode), a hit table, and labels from fixed vocabularies (requested / variant / new; present / adjacent / absent; on-topic / off-topic; unscreenable).
7.2 **Value** is the probe: adoption requires the program's own representation and proof standard.
7.3 **Instrument validation**: planted paraphrases and planted rewrites of known items (catch rates as acceptance thresholds), inter-judge agreement (κ), an embedding–human correlation reported and used to label arms as "embedding-defined" when low, ordinal arithmetic only.
7.4 **Outcomes** O1–O7 (screen survival, judged novelty, arm effects, buster effect, room outcomes, calibration) with decision rules fixed at FRAME; the CONTROL cell binds a rule only when it holds at least two lenses, else the round yields a recorded recommendation.

## 8 · Anti-gaming

Judges see content only; discrete or pairwise judgments wherever anything optimises against the output; no iterated optimisation against a judge; plants in every batch; the letter-vs-spirit pre-mortem written at FRAME for every barrier; every barrier classed enforced / audited / convention with its audit named.

## 9 · What is not settled (honest list)

Three things that read as code but are procedure: the room's neutral-extract pass and its moderator scoring pass are envelopes in and structured results back, so the launches that produce those results are the caller's, not this code's -- the same boundary the judged screen states for itself; and the `agreement_pct` a room converges on is computed from the participants' structured stances only where those stances were filed, which a room that skipped the rank-all point has not done. The convergent re-check's external half has not been run against a live index in this tree. The per-launch retrieval scope is enforced only where the booking declares a slice — through the slice attr, the assignment rows or the roster, all three of which the standard export emits: a lens booked by hand with none of them is covered by the audit, not by the engine, and the audit reports after the fact. The novelty screen records distances and routes ideas to a judge; it never turns a distance into "novel", and the judged half needs a judge this code does not call — the screen builds the envelopes and takes discrete labels back, and the step that spawns the judge is not in it. The screen's external reference set reaches whichever index the run names; no run in this tree has yet been made against a live one. Two seams stay hypotheses to be measured: the `requirements` field (H-AF) and the card catalogue's single-source entries.

## 10 · How this document changes

Only between rounds, with a revision-log entry naming the triggering signal (a round outcome, a new source in the evidence base, or a harness change). Sources enter through the evidence base first; a principle changes only when its evidence line changes.
