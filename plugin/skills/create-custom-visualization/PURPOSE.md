# PURPOSE — /create-custom-visualization

**Problem this skill exists for.** Every program wants its own view of its own
data, and the six built-in dashboard panels cannot anticipate them; absorbing
per-program panels into the core would turn the dashboard into a fork magnet
and leak program-specific logic into a general tool. The skill guides a
coding agent to build a panel that lives under the program root
(`trialerror_ext/panels/` manifest + builder), reads the store read-only, ships
an offline test, and is rendered by the dashboard with crash isolation when
that program is active. Origin: operator ruling C-0070, quoted verbatim in
SKILL.md; sibling in spirit to `/import-existing-project`.

**Mined patterns it leans on.** This skill originates in a ruling rather than
in a mined mechanism. The vocabulary it applies is WikiSkill's per-layer
mutation contract — the record is immutable, knowledge compounds, and a
procedural asset such as a panel is the revertible layer that must never
mutate the record (`docs/mining/G25-operator-2026-09__wikiskill.md`, F1;
arXiv:2608.27454) — which is why a panel is read-only over the store by
construction. The doctor-registry-as-data-source pattern behind the built-in
doctor panel, and the plan/dry-run/apply repair pipeline as its adopt-later
affordance, come from engram
(`docs/mining/G25-operator-2026-09__engram.md`, F6); Empryo's structured
capability health matrix is the adopt-later shape for a richer status panel
(`docs/mining/G25-operator-2026-09__empryo.md`, F5).

**Evolution.**
- Created 2026-09 from ruling C-0070.
- 2026-09-05 (launch `LNCH-01M1R8J1H9QN0RZVCE1MKR6W6G`): added this sidecar and
  a "When NOT to apply" section (wikiskill-F4).

*Mining reports live under `docs/mining/` in the harness repository and are
not part of the public export; primary sources are named inline.*
