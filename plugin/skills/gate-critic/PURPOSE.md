# PURPOSE — /gate-critic

**Problem this skill exists for.** A reviewer that can edit will eventually
rewrite; an author that can review will eventually pass itself; an artifact
that keeps changing while it is under review makes any verdict meaningless.
Gated artifact types (keystones) therefore go through a fixed loop: a
mechanical structural validator, then a genuinely separate, Read-only critic
that returns a discrete verdict with evidence-anchored edits, then a
differently tool-locked applier that applies the edit union and verifies each
blocking edit, and only then registration — the state machine
`draft -> submitted -> gated -> union_applied -> registered` refuses every
shortcut. The skill exists so that the human-shaped parts of that loop (who is
spawned with which tools, what they are told, what is pinned before they look)
are done the same way every time.

**Mined patterns that motivated it.** The two-tier mechanical-then-LLM shape
and the `[Read, Edit]` patch-stage subagent are hyperresearch's
(`docs/mining/G23-search__hyperresearch.md`; design Section 5.3 names the
pattern). The critic's anti-padding contract (specific edits, no "tighten
this"; a null verdict is allowed) is antivibe's
(`docs/mining/G16-review-provenance__antivibe.md`). The validator-before-critic
gate mirrors claude-code-spec-workflow's two-tier validator
(`docs/mining/S7-etl-specflow__claude-code-spec-workflow.md`); keeping the
critic blind to the generator's prompt is the-startup's information-barrier
dispatch (`docs/mining/G13-swarm-1__the-startup.md`). From the 2026-09 round:
the frozen, read-only candidate and the black-box/white-box separation are
Harness-of-Harness §3.4.3 (`docs/mining/G25-operator-2026-09__harness-of-harness.md`,
F3; arXiv:2609.01481), and the letter-vs-spirit pre-mortem run before any judge
spawns is "AI Finds A Way" §4/§7
(`docs/mining/G25-operator-2026-09__ai-finds-a-way.md`, F1; arXiv:2608.23875),
with the same three questions AIIF v2 §5.1 uses. Decoupling judged content from
its author's self-description (ai-finds-a-way-F2) is the standing follow-up for
the day a real subagent judge is wired into rooms.

**Evolution.**
- Created with the plugin (design Sections 5.3, 9.4; M10).
- 2026-09-05 (launch `LNCH-01M1R8J1H9QN0RZVCE1MKR6W6G`): added this sidecar and
  a "When NOT to apply" section (wikiskill-F4); the frozen-candidate digest
  rule and black-box-first ordering (HoH-F3); the required pre-mortem section
  (ai-finds-a-way-F1).

*Mining reports live under `docs/mining/` in the harness repository and are
not part of the public export; primary sources are named inline.*
