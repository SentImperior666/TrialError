# PURPOSE — /import-existing-project

**Problem this skill exists for.** People arrive with a research project
organised their own way — papers, notes, ledgers, a repo shaped by years of
habit and often tens of gigabytes of tool caches. Moving it breaks git
history, caches and muscle memory, and no fixed transform can cover an
arbitrary layout. So the skill bridges instead of moving: `trialerror.toml`
`[paths]` knobs and `[paths].ingest_roots` point into the existing tree, a
junction or symlink is used only when a true link is unavoidable, sources are
registered, and the result is validated with doctor plus a search smoke. Every
step is a judgment made with the user, which is why it is a skill and not a
CLI command. Origin: operator ruling C-0068(a), quoted verbatim in SKILL.md.

**Mined patterns it leans on.** This skill originates in a ruling rather than
in a mined mechanism; the findings it applies are the fail-loud,
never-silently-bucket write guardrail (refuse rather than guess where a
foreign file "belongs"; `program init`'s `already_scaffolded` refusal is the
same instinct) from engram
(`docs/mining/G25-operator-2026-09__engram.md`, F8), the doctor-check registry
as the validation surface of choice, with its plan/dry-run/apply repair
pipeline as the adopt-later extension (same report, F6), and the
backend-times-operation health probe from Empryo as the shape a richer
post-import smoke would take
(`docs/mining/G25-operator-2026-09__empryo.md`, F5, adopt-later).

**Evolution.**
- Created 2026-09 from ruling C-0068(a).
- 2026-09-05 (launch `LNCH-01M1R8J1H9QN0RZVCE1MKR6W6G`): added this sidecar and
  a "When NOT to apply" section (wikiskill-F4).

*Mining reports live under `docs/mining/` in the harness repository and are
not part of the public export; primary sources are named inline.*
