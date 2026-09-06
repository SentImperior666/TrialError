# PURPOSE — /ideation-round

**Problem this skill exists for.** An ad-hoc "pick some documents and
brainstorm" pass produces rounds nobody can reproduce, that only ever look at
the nearest neighbours of the home documents, and whose findings reach the
record as an orchestrator's summary rather than the lens's own words. The
round therefore draws seeded, logged, stratified slices (near / moderate / far
over embedding distance, with a far-arm floor and an inter-cluster mandate),
seats at least one assumption-buster whose job is to attack the round's own
framing, books and spawns each lens through the budget gate, requires
full-text feed posts under the lens's server-derived name, and audits that
every booked lens actually posted before the round is consolidated.

**Mined patterns that motivated it.** Parallel, independent lens workers with
isolated failure and an audit/revise step are Lacuna Deep Research's design
(`docs/mining/G17-papers__arxiv-2606.26246-research-structured-knowledge.md`).
Keeping each lens blind to the others' slices and to the consolidation rubric
is the-startup's information-barrier dispatch
(`docs/mining/G13-swarm-1__the-startup.md`). The far-arm floor's rationale —
keep distant, low-similarity candidates alive because objective-driven
selection is deceptive for hard problems — is the stepping-stone argument in
"AI Finds A Way" §7.3 (`docs/mining/G25-operator-2026-09__ai-finds-a-way.md`,
F6; arXiv:2608.23875), and the same paper's letter-vs-spirit pre-mortem (F1) now
gates every judged step of a round. The bounded-but-locally-complete plan
block (at most three priorities, an exclusion list, Preservation and Acceptance
gates, never inheriting the previous plan) is Harness-of-Harness §3.4.1 /
Appendix A.2 (`docs/mining/G25-operator-2026-09__harness-of-harness.md`, F4;
arXiv:2609.01481). WikiSkill's rollout/diagnose blindness result (F8) is
recorded as a phase rule for the design phase that follows, not enforced here.

**Evolution.**
- Created with the plugin (design Sections 5.3, 9.6; M13), generalizing the
  origin program's AMENDMENT-3 stratification machinery.
- 2026-09-05 (launch `LNCH-01M1R8J1H9QN0RZVCE1MKR6W6G`): added this sidecar and
  a "When NOT to apply" section (wikiskill-F4); the round-plan block (HoH-F4);
  the pre-mortem for judged steps (ai-finds-a-way-F1).

*Mining reports live under `docs/mining/` in the harness repository and are
not part of the public export; primary sources are named inline.*
