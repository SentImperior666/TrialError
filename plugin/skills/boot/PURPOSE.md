# PURPOSE — /boot

**Problem this skill exists for.** An orchestrator session that starts without
its ground truth spends its first minutes re-deriving state, or worse, acts on
law that changed since its last session: it spawns against a stale pin, books
launches on top of ones a crashed session left dangling, and never reads the
messages the user posted overnight. The boot ritual makes that context arrive
pre-loaded (law-pin status and foreign rulings, dangling launches, unread
inbox, budget headroom, detached jobs still running, the last handoff, the L0
memory index) and puts the reading of it BEFORE any spawn. The `SessionStart`
hook does this automatically; the skill is the manual/fallback path for the
cases the hook reports it cannot resolve (ambiguous or zero accounts).

**Mined patterns that motivated it.** The tiered boot bundle is Athena-Public's
attention-budget boot loading (`docs/mining/G03-memory-3__Athena-Public.md`;
measured 69% boot-token saving) — an L0 index plus targeted L1 items, capped by
`[memory] token_budget`. The index-then-detail shape of that bundle is
claude-mem's three-layer progressive disclosure and hook exit-code contract
(`docs/mining/G02-memory-2__claude-mem.md`). The "pre-loaded — do not re-fetch"
instruction is claude-code-spec-workflow's `get-spec-context` bundling
(`docs/mining/S7-etl-specflow__claude-code-spec-workflow.md`). Surfacing
detached workers that legitimately outlive a session follows codemap's
PID-ownership-verified daemon liveness
(`docs/mining/G05-codestruct-2__codemap.md`). What a good L0 line must contain
(PROBLEM + ROOT CAUSE + FIX) is WikiSkill's index-entry rule
(`docs/mining/G25-operator-2026-09__wikiskill.md`, F6; arXiv:2608.27454).

**Evolution.**
- Created with the plugin (design Section 5.3 / 5.4, SessionStart hook).
- 2026-09-05 (launch `LNCH-01M1R8J1H9QN0RZVCE1MKR6W6G`): added this sidecar and
  a "When NOT to apply" section (wikiskill-F4), plus the L0 line-reading note
  (wikiskill-F6).

*Mining reports live under `docs/mining/` in the harness repository and are
not part of the public export; primary sources are named inline.*
