# PURPOSE — /close

**Problem this skill exists for.** A session that ends without reconciling its
launches, reading its inbox, or checking the law pin leaves dangling state for
the next session — often on a different account — to discover the hard way;
handoffs written as stacked addenda drift until nobody knows which is current.
Close therefore REFUSES rather than skips (dangling launches, unread inbox,
stale pin, hooks never armed), requires a course-check object, renders a fresh
suffixed handoff from the session row and marks the prior one superseded. It
is also the moment a session's lessons, rules, facts and preferences are
written to memory, so it carries the authoring rule for what an `l0_abstract`
must say.

**Mined patterns that motivated it.** The structured, per-session handoff is
learn-harness-engineering's five-section `session-handoff.md` template
(`docs/mining/G15-research-evolve__learn-harness-engineering.md`). Rendering
the handoff as a view over an append-only record follows zero's
`metadata.json` + `events.jsonl` session log
(`docs/mining/G10-harness-4__zero.md`). Reconcile-before-close exists because
ECC's Stop-hook cost tracker documented the same 2.5-3x cost-inflation the
origin program found independently (`docs/mining/G09-harness-3__ECC.md`); the
launch states close inspects come from atomic's claim/lease/heartbeat ledger
(`docs/mining/G21-docstruct-2__atomic.md`). The PROBLEM + ROOT CAUSE + FIX rule
for memory abstracts, and the pattern-page body rules (root cause not symptom,
exact command sequences, update-don't-duplicate, 10-30 lines), are WikiSkill's
(`docs/mining/G25-operator-2026-09__wikiskill.md`, F6 and its DATA MODELS
section; arXiv:2608.27454). Two engram findings are the code half of the same
concern and are tracked as separate lanes: save-time conflict candidates
(advisory only) and age-based `needs_review` surfacing as a doctor check
(`docs/mining/G25-operator-2026-09__engram.md`, F4 and F5).

**Evolution.**
- Created with the plugin (design Section 5.3; `/handoff` folded in).
- 2026-09-05 (launch `LNCH-01M1R8J1H9QN0RZVCE1MKR6W6G`): added this sidecar,
  a "When NOT to apply" section (wikiskill-F4) and the l0_abstract authoring
  rule with a worked example (wikiskill-F6).

*Mining reports live under `docs/mining/` in the harness repository and are
not part of the public export; primary sources are named inline.*
