# Operator guide

The deeper reference: every CLI group, the two MCP servers and how to register them with
Claude Code, the enforcement model (what refuses what and why), the detached-job operating
pattern, and the doctor checks catalog.

Everything here is verified against the shipped code (`trialerror/cli/*.py`, `trialerror/mcp/*.py`,
`plugin/hooks/*.py`, `trialerror/*/checks.py`) as of this build, not just the design document —
see **Where design and code disagree** at the end for the one confirmed gap.

## Command reference

Every `trialerror <group> <verb>` prints an `AgentEnvelope`
(`{ok, command, protocolVersion, result|error, nextActions, meta}`, JSON by default,
`--format text` for a human-readable rendering). Most groups accept `--program-root`
(default: discovered by walking up from CWD for a `trialerror.toml`) and `--platform-root`
(default: `TRIALERROR_PLATFORM_ROOT` env var, or `~/.trialerror`).

### `--program-root`/`--platform-root` placement (standardized, FX-12)

**`trialerror --program-root X --platform-root Y <group> <verb> ...` — before the group name
— now works uniformly across every group.** These are GLOBAL arguments on the top-level
parser (`trialerror/cli/__init__.py`). Every group's own historical placement (below) also
still works exactly as it always did; if you give the flag in more than one place, the
one closest to the actual verb wins.

| Where it also works (back-compat) | Groups | Historical rule (still honored) |
|---|---|---|
| After the action | `artifact`, `gate`, `law`, `memory`, `mcp`, `prereg`, `verify` | `trialerror law append --summary X --program-root Y` |
| After the action | `events`, `feed`, `inbox`, `ingest`, `jobs`, `lit`, `obs`, `query`, `session` | `trialerror events append --type t --payload {} --program-root Y` |
| Between the group and the subcommand | `budget`, `lens` | `trialerror budget --program-root Y book --session-id X` |
| Anywhere (flat, no subcommands) | `accept`, `doctor` | `trialerror doctor --program-root Y` |

Before FX-12, these three historical placements actively **conflicted** — the same flag
silently reverted to CWD-discovery in one group and was an outright argparse error in
another. That's fixed structurally (every group's own declaration now uses
`default=argparse.SUPPRESS` so it only ever *overrides* the global, never silently
resets it) — the table above is now purely "what also still works", not "what you must
get exactly right". `trialerror <group> <verb> --help` always shows the exact set of flags
that parser recognizes.

| Group | Verbs | Notes |
|---|---|---|
| `program` | `init` | Scaffolds a fresh program: `trialerror program init <name> [--dir <path>]` (default `--dir`: `./<name>` under CWD). Writes a commented starter `trialerror.toml`, the design's per-program layout (`raw/`, `archive/`, `memory/`, `law/`, `handoffs/`, `artifacts/`, `requests/`), and runs the initial migration. Refuses (`already_scaffolded`) rather than overwrite an existing `trialerror.toml`. `list`/`info` (named in the design doc) are NOT implemented — v0 has no cross-program registry to back them; see `trialerror/cli/program.py`'s own docstring. |
| `session` | `boot`, `close`, `render-handoff`, `status`, `abandon` | `boot` reuses an already-open session idempotently unless `--fresh`; first-ever boot needs `--create-account <label>`. `close` requires `--course-check '<json>'` and **refuses** on dangling launches, an unread inbox, or a stale law-digest pin. `abandon` is a real fifth verb not named in the design doc's table — for marking a crashed/never-closed session `abandoned`. |
| `budget` | `book`, `reconcile`, `status`, `pools`, `snapshot-ingest`, `calibrate`, `rollup` | `book` returns a `launch_id` token (also in `meta.prompt_fragment`) and refuses without an open session or against model policy — but **not** for a missing pool: with no `budget_pool` row configured for the account/model-class yet, `book` books unconditionally as `PROVISIONAL` (pools only start capping once one exists via `pools --create`). `pools --create` makes a new pool; without `--create` it lists. `rollup` sums est/actual tokens over a `parent_launch` tree. |
| `law` | `append`, `lookup`, `digest`, `verify`, `diff-foreign` | `append` and the digest regeneration are one atomic write — there is no way to add a ruling without the digest moving in lockstep. `verify --pin vNN@date` is the exact check the spawn gate runs. `diff-foreign` lists rulings appended (by any session/account) since a given pin. |
| `events` | `append`, `tail`, `export` | Free-form `--type` key + JSON `--payload`; a secret-redaction pass runs before every write. `export` renders byte-stable jsonl, optionally `--split-by-workpackage`. |
| `feed` | `post`, `threads`, `read` | Full-text agent voices. Authorship is **never** a free-text flag — it's derived from `--launch-id` (or, if omitted, the open session as `orchestrator:<session_id>`). `post --new-thread <title>` opens a thread (requires `--launch-id`); `post --thread-id <id>` posts into an existing one. |
| `inbox` | `post`, `read` | `inbox post` is the user's one API-backed write path — no hand-appended files. `read` marks items read unless `--no-mark-read`. |
| `ingest` | `add-source`, `add`, `doctor`, `rechunk`, `re-embed`, `status`, `request`, `requests-md` | `add-source` registers + dedups on `content_sha256`. `add` acquires a document under a source and enqueues the first pipeline stage (`normalize` or `ocr`, by media type) — refuses past a page-count cost threshold (default 50) unless `--yes`. `doctor` runs just the 6 ingest-specific checks. `rechunk`/`re-embed` re-enqueue one stage. `request` drives the acquisition-queue state machine; `requests-md` renders `requests/REQUESTS.md`. |
| `jobs` | `list`, `start-worker`, `tick`, `pause`, `resume`, `logs` | See **Detached jobs** below. |
| `query` | `search`, `quote`, `similar`, `stats` | The same retrieval engine the `trialerror-knowledge` MCP server serves live agents. `search --unfenced` is the one CLI-only, human-flagged escape hatch past the commercial-license serving fence — the MCP `search` tool never exposes it. |
| `verify` | `citecheck`, `hypothesis`, `reproduce` | `citecheck <file\|claim-set.json\|artifact_id> --by-launch X` — mechanical pass first (6-word-shingle/number match + anchor resolve), unresolved pairs escalate (supply `--judgments-file` or they come back `escalation_selected`/`escalation_not_sampled`). `hypothesis` REQUIRES `--judgments-file` covering every retrieved chunk (this process never calls an LLM itself — judgments are supplied by the caller). `reproduce <verdict_id>` re-runs a verdict's `reproduction_ref` script and byte-compares its sha. |
| `prereg` | `commit`, `reveal`, `status` | `commit` hash-locks a procedure+params blind, escrowed under the **platform** tree (`~/.trialerror/escrow/<program>/`, outside the program repo — a physical, not conventional, blind). `reveal` tamper-checks against the committed hash before copying content into the program tree. |
| `artifact` | `create`, `register`, `list`, `show` | `create` makes a `draft` row. `register` is refused for a `gated=1` template type unless its gate is in `union_applied`. |
| `gate` | `open`, `submit`, `verdict`, `apply-union`, `verify-edit`, `advance` | The state machine: `draft → submitted → gated|failed → union_applied → registered`. `advance` is the generic low-level entry point (refuses any illegal edge); the others are named shortcuts for specific legal transitions. `apply-union` is the terminal-pass gate: it enforces verdict ∈ {PASS, PASS_WITH_EDITS}, every **blocking** edit `verified=true`, and `reproduction_status != mismatch`. |
| `memory` | `search`, `put`, `sync-export`, `sync-import`, `merge` | `search --id <item_id>` fetches one item's full body (the progressive-disclosure "step 2"); `search --boot-bundle` returns the same L0-index-plus-targeted-abstracts payload session boot injects. `put` upserts by `(key, account)`. `sync-export`/`sync-import` round-trip `memory/*.md` for git sync; a merge conflict from `sync-import` is never auto-resolved — list it with bare `memory merge`, resolve with `--group <id> --keep left\|right\|both`. |
| `lens` | `roster`, `stratify`, `assign`, `log`, `export` | AMENDMENT-3 ideation machinery, generalized. `stratify` is a dry-run score+tercile-cut (no write); `assign` does the real seeded quota draw and writes `lens_assignment` rows (default weights 40/40/20 near/moderate/far, far-arm floor 2). `export` hands back rows shaped for `budget book`. |
| `obs` | `status`, `start-phoenix`, `smoke` | All no-op gracefully if the `obs` extra isn't installed. `start-phoenix` launches a detached local `phoenix serve` (the same detach technique as job workers: `DETACHED_PROCESS \| CREATE_NEW_PROCESS_GROUP` on Windows, `start_new_session=True` — i.e. `setsid()` — on POSIX). `smoke` emits one span of each of the four kinds (launch/retrieval/verification/job) and reports whether they flushed. |
| `mcp` | `ops`, `knowledge` | Starts the named stdio MCP server; **blocks** for its lifetime (serves until stdin closes). Not meant to be run interactively — see **Registering the MCP servers** below. |
| `accept` | *(no subcommands)* | Runs the M15 acceptance harness: a full clean-checkout-shaped smoke journey against a scratch program (discarded after), plus an enumeration of the GPU/live-Claude-Code items that still need a real machine. `--skip-gpu-live-cc-enumeration` to omit the latter. |
| `doctor` | *(top-level, no subcommands)* | `--license-audit` for just the vendored-file header scan; `--only CHECK_NAME` (repeatable) to run specific checks; **`--program-root` is required to see program-scoped checks** (schema version, dangling XIDs, stale chunks/embeddings/anchors) — without it they silently skip. See the full catalog below. |

## The two MCP servers

Both are hand-rolled stdio JSON-RPC 2.0 servers (`trialerror/mcp/protocol.py`) implementing the
MCP 2025-06-18 spec's tools-only subset directly — no `resources`/`prompts`/`sampling`.
Tool-count is asserted in tests (`trialerror-ops` = 12, `trialerror-knowledge` = 11), matching the
design's own per-context tool-ceiling reasoning (§5.1): attach Haiku-class subagents to
only the one server they need.

**`trialerror-knowledge`** (read-only, 11 tools) — `search`, `get_chunk`, `get_source`,
`get_document_outline`, `resolve_quote`, `similar`, `graph_neighbors`, `corpus_stats`,
`memory_search`, `list_requests`, `poll_job`.

**`trialerror-ops`** (side-effecting, 12 tools) — `session_status`, `budget_status`,
`book_launch`, `reconcile_launch`, `append_event`, `post_feed`, `read_inbox`, `law_lookup`,
`register_artifact`, `gate_advance`, `prereg_commit`, `record_verdict`.

### Registering them with Claude Code

Neither server auto-registers — the plugin manifest (`plugin/.claude-plugin/plugin.json`)
carries no `mcpServers` entry, and no `.mcp.json` ships in this repo. Register each server
yourself with `claude mcp add`, e.g. scoped to your program directory so it's only active
there:

```console
# Linux / macOS
claude mcp add --scope project --transport stdio trialerror-ops -- trialerror mcp ops --program-root ~/research/demo-program
claude mcp add --scope project --transport stdio trialerror-knowledge -- trialerror mcp knowledge --program-root ~/research/demo-program
# Windows PowerShell
claude mcp add --scope project --transport stdio trialerror-ops -- trialerror mcp ops --program-root C:\research\demo-program
claude mcp add --scope project --transport stdio trialerror-knowledge -- trialerror mcp knowledge --program-root C:\research\demo-program
```

`--scope project` writes to `.mcp.json` at the project root (shareable via git);
`--scope local` (the default) is private to your machine; `--scope user` registers
user-wide across every project. The equivalent hand-written `.mcp.json`:

```json
{
  "mcpServers": {
    "trialerror-ops": {
      "type": "stdio",
      "command": "trialerror",
      "args": ["mcp", "ops", "--program-root", "/home/you/research/demo-program"]
    },
    "trialerror-knowledge": {
      "type": "stdio",
      "command": "trialerror",
      "args": ["mcp", "knowledge", "--program-root", "/home/you/research/demo-program"]
    }
  }
}
```

No shell reads this file, so the path must be a real absolute path — `~` is not expanded
here. On Windows the same field is `"C:\\research\\demo-program"`: the doubled backslashes
are JSON's own escaping, not a second path separator, which is why `"C:/research/demo-program"`
(forward slashes, no escaping to get wrong) is the easier thing to write.

To load the Claude Code **plugin** (hooks + skills) for a session without installing it
anywhere permanent, point Claude Code at the `plugin/` directory directly:
`claude --plugin-dir ~/research/research-harness/plugin` (Windows:
`claude --plugin-dir C:\research\research-harness\plugin`). This is stated for completeness
— **live verification of hooks/MCP inside an actual Claude Code session has not yet been
done on this build** (see **What's unverified** below); everything above is exercised only
by real-subprocess tests (`tests/test_mcp_ops_protocol.py`,
`tests/test_mcp_knowledge_protocol.py`, `tests/test_spawn_gate_hook.py`), never a live
Claude Code round trip.

## The enforcement model — what refuses what

TrialError's stated thesis (commitment 1, `docs/DESIGN_v0.md` §1) is "enforcement over
convention": nothing load-bearing is a prompt. Four refusing surfaces, all backed by
real code (not policy text):

| Surface | Refuses | Mechanism |
|---|---|---|
| **`PreToolUse:Task/Agent` hook** (`plugin/hooks/spawn_gate.py`) | A subagent-spawn call — Claude Code 2.1.x invokes this as the `Agent` tool; `Task` is a legacy alias name the matcher and hook still accept (found live 2026-09-05, FU-11) — whose prompt carries no valid `launch_id:` token, or one whose booking isn't `PROVISIONAL`/isn't the open session's/has an expired TTL, or whose model class violates `trialerror.toml`'s `[models]` policy for the stated purpose | Exit code 2 blocks the tool call; stderr carries the exact `trialerror budget book` command to fix it. The booking is consumed atomically on success (a conditional `PROVISIONAL→RUNNING` UPDATE) — the SAME `launch_id` token cannot ride a second spawn. Passes through (exit 0) for any tool call whose name isn't `Task`/`Agent`, or if it can't open the program's stores at all it fails **closed** (exit 2) since a subagent-spawn call it can't verify is treated as unsafe. |
| **`Stop` hook** (`plugin/hooks/stop_check.py`) | Stopping a session that still has dangling launches or a stale law pin | Blocks **once** with a checklist (exit 2); Claude Code's own `stop_hook_active` flag means the *second* stop attempt always passes — it can never trap the user in a loop. Fails **open** (allows the stop) on any internal error, unlike the spawn gate. |
| **`trialerror session close`** | The same dangling-launch/stale-digest condition, plus an unread inbox, plus (unless `--override-ruling-id` cites an existing ruling) a session where hooks were never observed to fire at all (`hook_alive` event count = 0) | Returns a structured error naming the exact fix in `nextActions` — reconcile a launch, read the inbox, or `law diff-foreign`. |
| **`trialerror gate` / `trialerror artifact register`** | Registering a `gated=1` artifact type whose gate isn't in `union_applied`; any gate-state transition that isn't a legal edge in the state machine; entering `union_applied` with an unverified blocking edit or a `reproduction_status=mismatch` | `IllegalTransitionError`/`GateEntryConditionError` → structured error; `gate advance` is the one mutation path and rejects every illegal edge (property-tested). |

**A hook that fails closed still has to run at all.** `plugin/hooks/hooks.json` invokes all
four hooks through the `trialerror` console script — `trialerror hook session-start`,
`hook spawn-gate`, `hook post-task`, `hook stop-check` — rather than `python <path>/<name>.py`,
because a bare `python` does not exist on a stock Linux install (only `python3` does) and a
hook whose interpreter is missing exits 127 without ever evaluating anything. That turns the
spawn gate's fail-closed refusal into a silent no-op, which is strictly worse than a refusal;
naming the program instead of an interpreter also guarantees the hook runs in the same
environment `trialerror` was installed into. The `plugin/hooks/*.py` files remain as
by-path-invocable shims for an older `hooks.json` or a hand-rolled `settings.json`.

**Mid-flight staleness is visible, not silently prevented**: a law ruling appended by a
concurrent session while your subagent is already running does not kill that subagent —
the next spawn, the next Stop, and session close all catch it. This is a stated design
trade-off (§5.4), not an oversight.

**Hooks can be disabled.** If they are, `SessionStart`'s `hook_alive` event never fires,
and `session close` refuses (override-only, citing a ruling) rather than silently
proceeding as if enforcement had been on the whole session.

## Detached jobs — the operating pattern

Long-running work (OCR, embedding, chunking, indexing) never runs inline inside an MCP
call or blocks a CLI command past a few seconds — it's a row in `jobs.db`'s durable
ledger, claimed/leased/heartbeat by a worker process (`trialerror/jobs/ledger.py`, ported from
the `atomic` scheduler pattern).

- **Enqueue**: `trialerror ingest add` (and `rechunk`/`re-embed`) create jobs; ingestion stages
  auto-chain — each handler enqueues the next stage's job on its own completion.
- **Run**: `trialerror jobs start-worker` — `--mode once` claims and runs a single job then
  exits; `--mode loop` polls until idle (`--max-idle-polls`, default 3) or
  `--max-iterations` is hit. `--foreground` runs inline in your terminal (what a detached
  child itself invokes); omit it and the command spawns a real detached background
  process — `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` on Windows, `start_new_session=True`
  (a `setsid()` in the child, detaching it from the controlling terminal) on POSIX — and
  returns immediately with its `pid` and `log_path`.
- **Lease/heartbeat**: default lease 900s (15 min), heartbeats renew it; a crashed
  worker's job is reclaimed by the next `trialerror jobs tick` and resumes from its
  `checkpoint` — no work is silently lost, and there's no separate watchdog process to
  keep alive (staleness is a query, not a loop that can die quietly).
- **Failure classing**: environmental failures (GPU busy, rate limit) `defer_until`
  without consuming a retry attempt; logic failures consume one with exponential backoff
  (60s base, capped at 1h; `max_attempts` default 3).
- **Inspect**: `trialerror jobs list [--state ...] [--kind ...]`, `trialerror jobs logs <job_id>`
  for its ledger event history — this doubles as the background-worker dashboard; no
  transcript-reading required to know what a worker is doing.
- **Pause/resume**: `trialerror jobs pause <job_id>` is cooperative (the worker stops at its
  next heartbeat); `trialerror jobs resume <job_id>` makes it claimable again but does **not**
  itself spawn a worker — follow it with `start-worker --job-id <id>`.

## Doctor checks catalog

`trialerror doctor` runs every check registered by every subsystem (28 checks across 14
categories as of this build); each subsystem owns its own `checks.py`, auto-discovered —
adding a new one never touches a shared file.

| Category | Checks |
|---|---|
| `stores` | `store_schema_version`, `xid_dangling` (cross-store reference scan), `anchors_dangling` (doc_sha256 half) |
| `ingest` | `chunker_missing`, `chunker_outdated`, `embedding_missing`, `embedding_stale`, `anchor_spot_resolve` (quote_sha256 half) |
| `jobs` | `stale_lease`, `heartbeat_age` |
| `law` | `law_digest_lockstep`, `law_chain_integrity`, `law_pin_format` |
| `budget` | `budget_dangling_launches`, `budget_pool_overspend` |
| `events` | `event_secret_leak`, `feed_author_integrity` |
| `sessions` | `session_multiple_open`, `session_hook_alive` |
| `artifacts` | `gated_type_without_gate`, `orphan_gate_transition`, `gate_illegal_transition_history` |
| `memory` | `memory_unresolved_conflict_groups`, `memory_l0_index_budget` |
| `lens` | `far_arm_floor_honored`, `no_duplicate_slice`, `cluster_coverage` |
| `retrieve` | `fence_integrity` (license-fence spot-check), `retrieval_latency` |
| `verify` | `verdict_evidence_anchors`, `prereg_escrow_integrity` |
| `obs` | `obs_exporter_reachable`, `obs_span_drop_counter` |
| `util` | `license_audit` (vendored/ header + manifest scan) |

`--only <name>` runs one (repeatable for several); `--license-audit` is shorthand for
`--only license_audit`; program-scoped checks (everything except `license_audit`) need
`--program-root` or they report nothing rather than failing loudly — a known gap (see
below).

## What's unverified (stated honestly, not hidden)

Straight from this build's own `trialerror accept` enumeration:

1. **Live Claude Code round trips** — `SessionStart` bundle injection, the `PreToolUse:Task/Agent`
   spawn-gate actually blocking a real spawn, the `Stop`-hook checklist, the `hooks.json`
   matcher actually scoping to `Task`/`Agent` calls (and only those), and both MCP servers' tool lists actually
   being offered to a live agent — all proven only by real-subprocess tests, never inside
   an actual Claude Code session yet.
2. **Real GPU backends** — `RealMarkerOcrBackend` has a `skipif`-gated smoke test (self-skips
   without `marker_single` on PATH); `RealQwenEmbedBackend` has no execution coverage
   beyond construction — this is the one deliberately-named zero-coverage gap in the build.
3. **`trialerror doctor` lacks a `--repo-root`-aware default for `--program-root`** — pass it
   explicitly, every time, on a program (a known, tracked gap).

## Where design and code disagree

`docs/DESIGN_v0.md` §3.2/§5.2 describes `program: init, list, info` in its CLI table.
**`init` now exists** (`trialerror/cli/program.py`, shipped C-0064 fix-tier2-cli FX-16/FX-10) —
`trialerror program init <name> [--dir <path>]`, matching §3.2's worked example. `list`/`info`
remain unimplemented: v0 has no cross-program registry anywhere (`platform.db` scopes
rows BY `program_id`; it doesn't enumerate known programs), so building them would mean
inventing new schema rather than exposing something that already exists — left as an
explicit v1 ticket (tracked internally) rather than built speculatively.
Every other module's `nextActions` that used to point at the nonexistent command
(`trialerror/cli/artifact.py`, `gate.py`, `law.py`, `session.py`, `lens.py`, `prereg.py`,
`verify.py`, `mcp.py`) now point at the real, runnable syntax.
