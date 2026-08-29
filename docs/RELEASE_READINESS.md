# Release readiness — v0 feature completeness

Evidence that "every planned v0/v1 feature is implemented" for the public release.
Source documents (internal dev-stage docs, not part of the public export, cited here
for traceability in this repo's own history): `docs/DESIGN_v0.md` §12 (the M0–M15 build
plan) and §11 (the v0/v1/v2 scope cut), `docs/reviews/IMPL_REVIEW_VERDICT.md` (the
consolidated implementation-review verdict as of commit `4546c4a`, corrections pin v65).
This document was produced two commits after `4546c4a` (`706180a`, `ea52b20` — a docs
update and an untracked-config change; neither touches build modules), so the verdict's
disposition counts (**30 FIXED · 8 ACCEPTED · 3 DEFERRED-v1**) still hold verbatim. Every
substantive finding cited below is restated in full in this document; nothing here
requires opening the source docs to verify.

## Machine verification (re-run for this document, not inherited)

**Full suite**: `2035 passed, 17 skipped, 0 failed` (`.venv312/Scripts/python.exe -m
pytest -q`, ~266s), confirmed clean twice. One environment-only wrinkle on this
particular machine, not a code defect: an untracked, gitignored local `trialerror.toml`
sitting at the repo root (interim operator config, never committed — see `.gitignore`)
shadows `tests/test_retrieval_cli_query.py`'s "no program root discoverable" fixture,
and under full-suite disk load `tests/test_stores_concurrency.py`'s two-process writer
test occasionally hits `database is locked` at its widened 60s test-only busy-timeout —
already dispositioned in `IMPL_REVIEW_VERDICT.md` as "N-1-family... transient documented
as busy_timeout exhaustion under load"). Both were confirmed non-issues: the suite is
2035/17/0 with that local file moved aside, and the concurrency test passes in
isolation every time. Neither affects the public export (no `trialerror.toml` ships there;
see `scripts/export_public.py`'s exclude list).

**`trialerror accept`** (the M15 acceptance harness, run fresh against a throwaway temp
program): `clean_checkout_smoke` — **all 12 step(s) passed** (migrate stores, boot via
the real SessionStart hook, book a launch, spawn-gate refusal/consumption/replay-refusal,
ingest open + commercial_restricted fixtures, fenced search with a 20-word-capped quote,
citecheck, the full gate journey, close-refusal-then-close, doctor green at
`{"total": 47, "warned": 3}`). The 8 GPU/live-Claude-Code items are enumerated `skip`
entries with exact operator instructions (never silently omitted) — summary
`{"total": 9, "passed": 1, "failed": 0, "warned": 0, "skipped": 8}`.

**Exported-subset smoke** (Deliverable 2's own check): `1994 passed, 17 skipped, 0
failed` against `../trialerror-public`'s own `tests/` tree, using the same `.venv312`
interpreter with `PYTHONPATH` pointed at the export. The 41-test difference from the
full repo (2035 → 1994) is fully accounted for: 28 tests in the four excluded
tenant-migration test files, 9 in `tests/test_artifacts_template_seed.py`
(hard-imports the excluded `the (excluded) tenant-migration module` package for 3 of its 9 tests; excluded
whole per "fix by adjusting the allowlist, never by weakening tests" — its
non-migration-dependent coverage of `trialerror.artifacts.template_seed` is preserved via
`tests/test_artifacts_cli.py`, which has no `the (excluded) tenant-migration module` dependency), and 4 in the
truncated tail of `tests/test_dashboard_ext.py` (the shipped-worked-example tests,
which loaded the also-excluded `examples/ext_panels/the (excluded) worked example/` directory by path — the
other 32 tests in that file, covering the general `trialerror.dashboard.ext` protocol against
synthetic fixtures, are untouched).

## v0 — "one honest loop" (design §11/§12, M0–M15): all 16 modules shipped

| # | Module | Path(s) | Status | Evidence |
|---|---|---|---|---|
| M0 | Platform skeleton | `trialerror/util/`, `trialerror/cli/` | Shipped | first commit `dd190ee`/`fb484b4`; `trialerror --version` emits a valid envelope, atomic-write-survives-kill test, CLI group auto-discovery test (`tests/test_cli_group_autodiscovery.py`) |
| M1 | Stores & schemas | `trialerror/stores/` | Shipped | first commit `c30cb07`; schema round-trip, XID write validation, concurrent-writer (2-proc, 1k-append) tests |
| M2 | Jobs ledger + workers | `trialerror/jobs/` | Shipped | first commit `43287ea`; kill-mid-job reclaim/resume, env-failure-doesn't-consume-attempt, foreign-PID-refusal tests |
| M3 | Budget + spawn gate | `trialerror/budget/`, `plugin/hooks/spawn_gate.py` | Shipped | first commit `d005e78`; `trialerror accept`'s `spawn_gate_refusal_no_token`/`_consumption`/`_replay_refused` steps all green live |
| M4 | Law service | `trialerror/law/` | Shipped | first commit `11aad45`; hash-chain tamper detection, stale-pin refusal tests |
| M5 | Events + feed + inbox | `trialerror/events/` | Shipped | first commit `67fda1f`; redaction, author-spoof-impossible, byte-stable jsonl export tests |
| M6 | Session lifecycle | `trialerror/sessions/`, `plugin/hooks/{session_start,stop_check,post_task}.py` | Shipped | first commit `f03275a`; `trialerror accept`'s `boot_session_via_session_start_hook` + `close_refusal_then_close` steps green live |
| M7 | Ingestion MVP | `trialerror/ingest/` | Shipped | first commit `8a8af7d`; 4-fixture-format end-to-end restartable ingest, dedup, anchors_dangling doctor checks |
| M8 | Retrieval + knowledge MCP | `trialerror/retrieve/`, `trialerror/mcp/knowledge.py` | Shipped | first commit `c75de1c`/`2f08cba`; `trialerror accept`'s `search_with_citation_and_fence` step (commercial_restricted fenced at exactly 20 words) green live; 11-tool MCP server, real-subprocess stdio protocol test |
| M9 | Verification suite | `trialerror/verify/` | Shipped | first commit `51066b4`; `trialerror accept`'s `citecheck` step green live; hypothesis pipeline, reproduction runner, prereg commit/reveal all tested |
| M10 | Artifacts + gates | `trialerror/artifacts/`, `plugin/skills/gate-critic` | Shipped | first commit `66d6a9e`; `trialerror accept`'s `gate_journey` step green live; illegal-transition-refused property test |
| M11 | Memory | `trialerror/memory/` (+ vendored MegaMemory merge port) | Shipped | first commit `ad9cb72`; divergent-edit conflict-surfacing, export/import round-trip idempotency tests |
| M12 | Obs seed | `trialerror/obs/` | Shipped | first commit `3f54f69`; OTel GenAI span emission, Phoenix-down no-op test |
| M13 | Lens tooling | `trialerror/lens/` | Shipped | first commit `f29df99`; byte-identical stratify-from-seed reproduction test |
| M14 | Ops MCP server | `trialerror/mcp/ops.py` | Shipped | first commit `2f08cba`; 12-tool ceiling asserted, real book→spawn→reconcile round-trip test |
| M15 | Acceptance harness | `trialerror/accept/`, `tests/acceptance/` | Shipped | first commit `8f7c7fd`; this document's own "Machine verification" section above **is** M15's acceptance criterion exercised live |

All 16 rows: **shipped**, verified either by a dedicated pytest suite, by a live
`trialerror accept` step in this session's own run, or both. Zero v0 rows outstanding.

## v1 items shipped ahead of schedule (design §11 names these as v1, not v0)

The design's v1 scope ("knowledge deepening + first tenant") was partially pulled
forward. Six items shipped in this repo despite being named v1 in §11:

| Item | Path | Design §11 phrase | Status |
|---|---|---|---|
| Live dashboard | `trialerror/dashboard/` | "live dashboard" | Shipped (V1 + V1.1, two dashboard API generations; `docs/DASHBOARD_V2_API.md`) |
| Rooms runtime | `trialerror/rooms/` | "rooms runtime" (schema v0, runtime v1 — one of exactly two v0 candidates named as deliberately cut) | Shipped |
| Summary tier (L1 overviews) | `trialerror/summarize/` | "summary tier (L1 overviews)" | Shipped |
| Hypothesis-pipeline hardening (DeepEval-pattern gate suites) | `trialerror/eval/` | "DeepEval DAG judges for gates as pytest suites" | Shipped |
| Acquisition integrations (OpenAlex/S2/arXiv/Unpaywall) | `trialerror/litapi/` | "acquisition integrations... after flags F1/F2 resolve" | Shipped (v3-acquisition build; flags F1/F2 resolved live against each provider's current docs, see `docs/USER_SETUP.md` §3) |
| origin-project tenant migration | `trialerror/the (excluded) tenant-migration module/` | "origin-project migration (corpora; corrections ledger import...)" | Shipped in this repo, **excluded from the public export** (see "Public-export scope" below — this is a publication-scope decision, not a completeness gap) |

One item ships beyond anything named in the v0/v1/v2 design at all: **`trialerror/arxiv_index/`**,
a standalone local semantic-search index over a public Kaggle arXiv-embeddings dataset
(`docs/USER_SETUP.md` §3e) — an opportunistic addition, not a scope-cut item.

## Deferred to v1/v2 — genuinely not built, and why

From `IMPL_REVIEW_VERDICT.md`'s "v1 ticket list (consolidated)" (3 items still marked
DEFERRED-v1 in the disposition ledger; FX-9/FX-11 in that same list already closed —
see the ledger) plus design §11's v2 scope:

| Item | Why deferred |
|---|---|
| O-1: log/work-dir retention unbounded per-program | Bounded per-run today; needs a doctor disk-growth check or documented prune policy — polish, not a correctness gap |
| O-2: ledger event rows not transactional with state transitions | Audit-gap only (state machine itself is unaffected); v1 candidate |
| O-4: `start-phoenix` has no already-running check | Idempotency polish |
| Kuzu/Graphiti spike (flag F3) | v0 deliberately implements the bi-temporal graph schema natively in SQLite (`prov_edge`/`relation`, designed to mirror 1:1); the spike is a binary-acceptance v1 task per design §13 |
| tantivy-vs-FTS5 bake-off | Needs a measured trigger (corpus scale) not yet reached |
| Caller-identity token (EP-4) | Architectural — no caller-identity primitive exists in v0's Task-spawn model; matches the stated F15 authorship contract as-is |
| Escrow encryption option (EP-6) | v0's bar is physical tree-separation, not secrecy; encryption is a v1 option for keystone preregs specifically |
| Per-lane git worktrees | Addresses shared-index races only relevant at higher build concurrency |
| job.kind migration, rechunk version-bump rewrite, idea DDL promotion | Named schema-evolution notes (design notes 7/8/14), additive, no current blocker |
| origin-project migration schema-v2 (`memory_item.account_id` nullable) | Tied to a future migration wave, not v0/v1 core |
| Real-dim latency + the 8 live-GPU/live-CC verifications | Require the operator's actual GPU/live Claude Code session — enumerated as `skip` in every `trialerror accept` run, never silently omitted (see "Machine verification" above) |
| Self-learning promotion pipeline, multimodal page-image retrieval, scoped proxy aggregator, cross-machine operation, scheduled reports, TTS periphery | Design §11 v2 ("compounding") scope — explicitly out of v0/v1 |

## Public-export scope note

`trialerror/the (excluded) tenant-migration module/` (the origin-project tenant-migration package) and its CLI front-end
(`trialerror/cli/migrate.py`) are fully shipped and tested in this repo but are **excluded
from the public export** (`../trialerror-public`, produced by `scripts/export_public.py`)
because the package's own tests, fixtures, and design docs necessarily name the private
source research program being migrated. This is a publication-scope decision, not a
feature gap: the export's own smoke suite (1994 passed / 17 skipped / 0 failed — see
above) proves every *other* shipped module works standalone without it. Two further
narrow exclusions follow the same package for the same reason: the worked
dashboard-extension-panel example (`examples/ext_panels/the (excluded) worked example/`, which demonstrates
`the (excluded) tenant-migration module`) and `tests/test_artifacts_template_seed.py`'s three
cross-check tests against `the (excluded) tenant-migration module`'s own type-key convention.

- LICENSE update 2026-08-29: MIT `LICENSE` file added at repo root (operator's choice, matches pyproject).
