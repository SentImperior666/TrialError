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
`--format text` for a human-readable rendering). When you author a **workflow script** for this CLI on Windows, write it with LF line endings —
that is a Claude Code tool check on the script file, not a harness check, and CRLF makes it refuse
the script before any `trialerror` command runs. A `nextActions` entry is a promise that its
`argv` runs: every one emitted is conditional on state a reader can see in the same envelope, is
parseable by the shipped top-level parser (a guard test drives the surfaces and parses every entry),
and carries no placeholder token. A command with nothing to suggest emits an empty list rather than
generic advice. Most groups accept `--program-root`
(default: discovered by walking up from CWD for a `trialerror.toml`) and `--platform-root`
(default: `TRIALERROR_PLATFORM_ROOT` env var, or `~/.trialerror`).

Envelopes are written as UTF-8 whatever the host console claims: `main()` reconfigures
stdout/stderr to `encoding="utf-8", errors="replace"` before any command runs. Without that,
a console on a legacy codepage (a Windows console, or any process whose `PYTHONIOENCODING`
names cp1252) raises `UnicodeEncodeError` inside `print` on the first title or author name
the codepage cannot represent — and because a text stream encodes the whole string before
touching the buffer, **the failing write is discarded whole: the caller gets no line at all,
not a partial one**, for a command that had already succeeded.

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
| `budget` | `book`, `heartbeat`, `reconcile`, `status`, `check`, `pools`, `snapshot-ingest`, `calibrate`, `rollup`, `quota` | **`book --session-id` and `--program-id` are optional**: with neither flag it books under the program's single OPEN session (bound at `session boot`) and under `[program] id` from `trialerror.toml`, reporting which of the two it resolved in `result.resolved_from` (`flag` | `open_session` | `config`) — a booking that silently chose its own session is otherwise indistinguishable in the envelope from one that was told which. With no open session it refuses naming both ways out; with **more than one** it refuses by name (`multiple_open_sessions`) rather than picking, because that is exactly the state that produces bookings nothing can reconcile; a program with no readable `[program] id` refuses (`program_id_unresolved`). Explicit flags always win. The MCP `book_launch` tool takes the same defaults and no longer requires `program_id`. `book --assign-id A` (repeatable) links a lens booking to the `lens_assignment` rows it covers — see *Ideation rounds* below. `book` returns a `launch_id` token (also in `meta.prompt_fragment`) and refuses without an open session or against model policy — but **not** for a missing pool: with no `budget_pool` row configured for the account/model-class yet, `book` books unconditionally as `PROVISIONAL` (pools only start capping once one exists via `pools --create`). `pools --create` makes a new pool; without `--create` it lists. `reconcile --spawned-model <model>` records what the launch ACTUALLY ran on in `launch.attrs.spawned_model` — the post-hoc half of the spawn gate's `agent_model_matches_booking` guard, and what the doctor check of that name reads; without it a launch simply makes no claim about its model. `rollup` sums est/actual tokens over a `parent_launch` tree. **`heartbeat --launch-id L`** is what a still-running launch says instead of reconciling early: it refreshes that booking's `booked_ts` and NOTHING else (not `est_tokens`, not `booking_ttl_s`, not the state), so a launch that outlived the TTL guessed for it stops reading as past-TTL. Refused unless the program's OPEN session is the one that booked it (`no_open_session` / `launch_not_owned`), refused with `multiple_open_sessions` when two sessions are open (a heartbeat needs one session to speak for the booking, and this is the state that produces orphan-looking bookings in the first place), refused for a settled booking, and every refresh writes a `launch_heartbeat` event — a TTL that keeps moving must be visible in the ledger. **`status --account-id` is optional**: with no flag it reads the account the program's OPEN session is bound to (`session.account_id`, the same row every booking reads), reporting which it used in `account_resolved_from`. With no open session it refuses naming both ways out (the flag, or `session boot`); with more than one open session it surfaces that refusal rather than picking one. Each pool also carries **`visible_headroom_to_soft`** (`max(soft_cap − projected, 0) / billed_multiplier` — the headroom in the unit a booking's `--est-tokens` is written in, not the plan meter's) and **`binding_limit`** (`soft` until the soft line is crossed, then `hard`); the envelope's top-level `binding_limit` names the tightest pool of the account, or is `null` for an account with no pool at all (uncapped and unmeasured, which is not the same as unlimited). **`check`** prints `status` and `quota` in one envelope with that binding limit named in a sentence; it composes the two and adds no arithmetic of its own, and with nothing captured the quota half degrades to `available: false` plus its own note rather than failing. |
| `law` | `append`, `lookup`, `digest`, `verify`, `diff-foreign` | `append` and the digest regeneration are one atomic write — there is no way to add a ruling without the digest moving in lockstep. `verify --pin vNN@date` is the exact check the spawn gate runs. `diff-foreign` lists rulings appended (by any session/account) since a given pin. |
| `events` | `append`, `tail`, `export` | Free-form `--type` key + JSON `--payload`; a secret-redaction pass runs before every write. `export` renders byte-stable jsonl, optionally `--split-by-workpackage`. |
| `feed` | `post`, `threads`, `read`, `translate`, `translations` | Full-text agent voices. Authorship is **never** a free-text flag — it's derived from `--launch-id` (or, if omitted, the open session as `orchestrator:<session_id>`). `post --new-thread <title>` opens a thread (requires `--launch-id`); `post --thread-id <id>` posts into an existing one. `translate` ENQUEUES a plain-English translation job for `--post-id` / `--thread-id` / `--pending` (it never translates inline and never calls a model); `translations` reads what was stored, `--gate-status fail` listing what the faithfulness gate withheld. |
| `inbox` | `post`, `read` | `inbox post` is the user's one API-backed write path — no hand-appended files. `read` marks items read unless `--no-mark-read`. |
| `ingest` | `add-source`, `add`, `doctor`, `rechunk`, `re-embed`, `retract`, `purge-embeddings`, `reindex-fulltext`, `reindex-vectors`, `quality`, `status`, `request`, `requests-md` | `add-source` registers + dedups on `content_sha256`. `add` acquires a document under a source and enqueues the first pipeline stage (`normalize` or `ocr`, by media type) — refuses past a page-count cost threshold (default 50) unless `--yes`. **A registered document is not a searchable one**: `add`'s result carries `searchable: false` and the `pending_stage` a worker still has to run, and its `nextActions` name the job plus the small-batch way to run it inline (`jobs start-worker --foreground --job-id <id>`). `lit acquire` says the same two things from the same place, and an `acquired` outcome whose source deduped onto an already-ingested document is the one case that reports `searchable: true`. There is no `--drain` flag: running the queue stays the queue's own verb. When the enqueued stage is `ocr`, the result also carries `stage_backend` (which backend will read this document's pages), and an envelope **`warnings`** entry — never stderr — fires when that backend is the deterministic stand-in or `[ingest.ocr]` is absent altogether, naming `[ingest.ocr]` and the `require_real_backends` / per-stage `require_real` keys that would refuse it outright. `lit acquire` mirrors the same warning from the same place. `doctor` runs just the 7 ingest-specific checks. `rechunk`/`re-embed` re-enqueue one stage (both refuse a retracted document — they would re-derive exactly what the retraction removed; re-ingest the raw file instead, which makes a new document). **`re-embed` only ever ADDS rows, for the configured `model_key`** — a run that dies halfway must never leave chunks with no embedding at all — so after a backend change the superseded key's rows are still in `emb` and **`purge-embeddings` is the companion step that takes them out**, once the new key is known to be complete. **`retract --doc-id D --launch-id L --reason "…"`** is the corpus's undo: it removes every derived row (element, chunk, `emb` rows no other document shares, `quote_anchor`, `chunk_fts`, `vec_chunks__*`), the derived archive text and any derived PDF tree, and the document's entries in the full-text index — the `document` and `source` rows STAY, and a retraction record + a `document_retracted` event carry the reason. Idempotent; refused without `--launch-id`, for an unknown doc, or while `claim` rows are anchored in the document. It also **cancels the document's own queued work** — every pending/paused/failed job of that document is settled `abandoned` with the retraction's reason and listed in `jobs_cancelled` — and **names what it could not cancel**: a job a worker is holding (`claimed`/`running`) rides `jobs_held` with its worker and a `jobs_note`, because settling it would let that worker complete a row the ledger had already closed, and the run-time guard fires at CLAIM, so that one stage will finish against the document you just withdrew. Pause it, then `jobs abandon`. On a DjVu document `raw_path` points at the derived PDF this removes, so the surviving row dangles by design — the event's `raw_path_removed` names it. **`purge-embeddings --model-key K --launch-id L [--doc-id D] [--dry-run]`** removes every `emb` row stamped with a SUPERSEDED embed `model_key` plus that key's `vec_chunks__*` entries, one transaction per document, and reports `{model_key, documents, rows_deleted, index_entries_deleted, chunks_now_without_any_embedding, dry_run}` with an `embeddings_purged` event; refused for the key `[ingest.embed]` configures (that one is the live search surface) and without a registered `--launch-id`; `--dry-run` counts and touches nothing; idempotent. This is what clears a `fake_backend_rows` failure after a real-backend migration — check `chunks_now_without_any_embedding` is 0 before you trust the corpus. `reindex-fulltext` rebuilds the tantivy index from `chunk`. **`reindex-vectors --model-key K --launch-id L [--dry-run]`** is its vector counterpart and the companion it is NOT: `reindex-fulltext` rebuilds a file-backed index beside the database and so needs no launch, while this one rewrites `vec_chunks__<key>` rows INSIDE `knowledge.db` from that key's `emb` rows and replaces the key's live semantic-search surface, so it is XID-attributed like every other write verb. One entry per chunk the key has embedded, delete-and-refill in ONE transaction (an interrupted rebuild leaves the old partial index, never an empty one), the `vec_index_registry` row brought up to date, and a `vectors_reindexed` event; reports `{model_key, emb_rows, vec_rows_before, vec_rows_after, dry_run}`. Refused without a registered `--launch-id`, for a key this program has never embedded or indexed under, for a key whose `emb` rows disagree about `dims`, and for an existing `vec0` table this connection cannot read; `--dry-run` projects the counts and touches nothing; idempotent. This is the repair for a `vector_index_stale` finding — a corpus whose embeddings exist and whose vector index does not answer for them. `quality` measures extraction quality read-only — `--doc-id` for one document, `--all [--sample N --seed S] [--worst N]` for the corpus (see "Extraction quality" below). `status` also carries two additive keys: `quality` (that document's four numbers and the thresholds they were judged against) and `pipeline` (the stage chain, a derived `pipeline_state`, and the next action). `request` drives the acquisition-queue state machine; `requests-md` renders `requests/REQUESTS.md`. |
| `jobs` | `list`, `start-worker`, `tick`, `kick`, `pause`, `resume`, `abandon`, `retry`, `logs` | See **Detached jobs** below. `abandon --reason` settles a cancellable job terminally on purpose; **`retry <job_id> --reason "…" [--by-launch L] [--max-attempts N] [--clear-checkpoint]`** is the way back out of that state — the only one, and it accepts `failed` and `abandoned` and nothing else. |
| `offload` | `worker`, `worker-control`, `worker-status`, `reclaim`, `kick`, `status`, `doctor` | The GPU offload queue — a file-tree protocol between the program that owns the corpus and the machine that owns the card. One verb runs on the **worker** machine and the rest on the **queue** side. `worker [--remote ALIAS | --queue-root PATH] [--stay] [--poll-interval-s S] [--max-jobs N] [--max-polls N] [--worker-id ID] [--backend-config-root ROOT] [--work-root PATH] [--lock-path PATH] [--batch-size N] [--keep-resident]` claims jobs and runs the real local backends against them; the lock file makes it single-instance per work root. `worker-status [--worker-id ID] [--queue-root PATH] [--heartbeat-interval-s S]` reads back what each worker last said it was doing — state, progress, pace, ETA, beat age — and is what to check before concluding a run is wedged: a worker silent for longer than the lost window (2× its beat interval + 60 s) is *lost*, which is the ordinary closed-laptop state. **`worker-control --worker-id ID --by-launch L (--pause | --resume | --stop | --clear) [--job-id J] [--even-if-absent]`** is the cooperative interface, and cooperative is literal: nothing here kills a process. The worker acts at its next checkpoint — between embed batches, and between OCR page ranges — so a stop lands on a unit boundary with the claim handed back and the GPU minutes already spent kept. `--by-launch` is required (ruling L-E4): a control act is somebody's act. `reclaim [--expiry-s S]` returns claims whose heartbeat stopped (default 3600 s) so the work can be claimed again; `kick` adopts interrupted publishes, un-delays parked jobs whose result landed and sweeps completed ones; `status` prints the queue's counts (what the status wrapper shows), with retried markers counted separately under `retried`; `doctor` runs this subsystem's own checks in one place, the way `ingest doctor` does. Every verb takes `--program-root`. See **Detached jobs** and **OCR page-range chunking** below. |
| `query` | `search`, `quote`, `similar`, `stats` | The same retrieval engine the `trialerror-knowledge` MCP server serves live agents. `search --unfenced` is the one CLI-only, human-flagged escape hatch past the commercial-license serving fence — the MCP `search` tool never exposes it. **A search that returns nothing explains itself**: when the result set is empty, the query is not blank and the full-text tier actually ran, `stats` gains a per-term candidate count (at most 8 terms, through the same backend that served the search) and the list of terms no chunk matches. Both counts are scoped to the filters the search itself ran under (`--source-id`, `--kind`, `--license-tier`, `--year`, and a launch's declared slice), so a zero means "nothing under these filters", not "nothing in the corpus". Every lexical backend ANDs a multi-term query, so one unknown term — a typo, a term of art this corpus does not use — returns nothing for a query whose other terms have hundreds of hits. The envelope's `nextActions` then carries the same search with exactly those terms dropped (the largest subset that can match), and nothing is emitted when every term is dead or when no single term is at fault. The MCP `search` tool inherits both keys; its input schema is unchanged. |
| `verify` | `citecheck`, `hypothesis`, `reproduce` | `citecheck <file\|claim-set.json\|artifact_id> --by-launch X` — mechanical pass first (6-word-shingle/number match + anchor resolve), unresolved pairs escalate (supply `--judgments-file` or they come back `escalation_selected`/`escalation_not_sampled`). `hypothesis` REQUIRES `--judgments-file` covering every retrieved chunk (this process never calls an LLM itself — judgments are supplied by the caller). `reproduce <verdict_id>` re-runs a verdict's `reproduction_ref` script and byte-compares its sha. |
| `prereg` | `commit`, `reveal`, `status` | `commit` hash-locks a procedure+params blind, escrowed under the **platform** tree (`~/.trialerror/escrow/<program>/`, outside the program repo — a physical, not conventional, blind). `reveal` tamper-checks against the committed hash before copying content into the program tree. |
| `artifact` | `create`, `register`, `list`, `show` | `create` makes a `draft` row. `register` is refused for a `gated=1` template type unless its gate is in `union_applied`. |
| `gate` | `open`, `submit`, `verdict`, `apply-union`, `verify-edit`, `advance` | The state machine: `draft → submitted → gated|failed → union_applied → registered`. `advance` is the generic low-level entry point (refuses any illegal edge); the others are named shortcuts for specific legal transitions. `apply-union` is the terminal-pass gate: it enforces verdict ∈ {PASS, PASS_WITH_EDITS}, every **blocking** edit `verified=true`, and `reproduction_status != mismatch`. |
| `memory` | `search`, `put`, `sync-export`, `sync-import`, `merge`, `candidates`, `judge`, `stale`, `reviewed` | `search --id <item_id>` fetches one item's full body (the progressive-disclosure "step 2"); `search --boot-bundle` returns the same L0-index-plus-targeted-abstracts payload session boot injects. `put` upserts by `(key, account)`. `sync-export`/`sync-import` round-trip `memory/*.md` for git sync; a merge conflict from `sync-import` is never auto-resolved — list it with bare `memory merge`, resolve with `--group <id> --keep left\|right\|both`. `candidates`/`judge` are the 2026-09 mining adoption (engram-F4): every `put` runs a BM25 pass over existing items and files anything similar as an **unjudged, advisory** candidate — the save is never blocked, delayed, or altered, and no machine writes a verdict (`judge --actor-kind system` is refused; an agent rules under its own name). `stale`/`reviewed` are engram-F5: `stale` computes, per item kind, whether a review half-life has elapsed (rule 365d, fact/lesson 180d, preference/index 90d) and `reviewed <id>` records that you looked and left it standing. Decay **only surfaces** — nothing expires, unpins, or downgrades on a timer. |
| `lens` | `roster`, `stratify`, `assign`, `log`, `slice-distances`, `intake`, `export`, `screen`, `recheck` | AMENDMENT-3 ideation machinery, generalized (the round's own mechanics — what a plant tests, what a judge sees, what a record must carry — have their own section, *Ideation rounds*, below). `stratify` is a dry-run score+tercile-cut (no write); `assign` does the real seeded quota draw and writes `lens_assignment` rows (default weights 40/40/20 near/moderate/far, far-arm floor 2). **`assign --arm-per-lens`** switches the semantics: the weights then split the ROSTER across the arms rather than each lens's own slice, so every lens gets ONE arm and draws its whole slice from it (roster 6 → 3 near / 1 moderate / 2 far; roster 12 → 5/5/2), the `assumption_buster` seat is pre-placed far and the `control` seat lands in the modal arm, and `--far-floor` counts far LENSES instead of far slices. `roster --add --seat control` is the matched-budget measurement seat (no card, no `requirements` field); `--recipe-card CARD` is repeatable and **order-preserving** — it is the lens's seeded card block — and a control seat carrying a card is refused. `log` returns the round's assignment rows AND its per-lens reconciliation — `rows` (one per assigned lens, with `posted`, `n_ideas`, `n_feed_posts` and the lens's own launch), `n_lenses` and `offenders`, each offender saying why — which is the shape the `aiif_round` gate suite's `lens_log_reconciled` check consumes. `intake --round-id R --records FILE --author-launch L [--assign-id A ...] [--arm ARM]` writes a lens's returned records as `idea` rows, validating the whole file before writing any of it. `export` hands back rows shaped for `budget book`, with `arm_mode`, `arm`, `recipe_cards` and `assign_ids` in `attrs`. **`screen`** is the novelty screen, in three separate invocations because they run in three different launches. `screen --mechanical` (no model, incremental) merges near-duplicates at cosine >= 0.92 *with the same home cell*, flags records sitting on an inventory row at the same threshold, records `d_prov`/`d_home`/`leap`/`H_prior` and within-round terciles, retrieves prior art stratified 40/40/20 (far floor 2), queries the external index under `--external-query-mode none|neutral_abstract|statement` paired with `--external-provider none|arxiv-index|litapi` — the mode says what text may leave the machine, the provider says where it goes, and naming one without the other is refused rather than half-done (every query logged as a `novelty_external_query` event with its launch and its mode, logged even when the provider raises) — and reports declared-operation entropy, the pairwise-similarity distribution and template mass against `--alarms` (descriptive when none are pre-registered). It writes one dossier per record and an adjudication draft under `artifacts/rounds/<round>/`; re-running screens only what arrived since. `screen --judged-prep --seed S` reads those dossiers back and builds the judge's batch: scope = flagged + retrieval hits + a seeded 20% sample of the rest, envelopes carrying raw record fields and retrieved text only (no rationale, seat, card or assumed circle, and self-assessment sentences stripped), five inventory plants and five paraphrase plants shuffled in among them, and a 10% second-judge sample. `screen --record-verdicts FILE --launch-id L` takes the discrete labels back (`same/variant/recombination/new-mechanism/unscreenable` against the inventory, `stated/implied/adjacent/absent` against corpus and external hits), scores the plants FIRST, writes `verdict` rows with `procedure=custom`, `procedure_version=novelty-v2` and the round's `--prereg-id`, and consolidates **every survivor** — the judged ones under their labels, the rest under the mechanical `no-close-neighbour`/`unjudged` pair written as its own row, because Phase 5 rooms every consolidated idea and nothing is pruned on a proxy. An idea that WAS scoped and came back unlabelled is the one case that is not consolidated: those ids are reported in `unlabelled_scope`, and a share above 10% caveats the batch. `prereg_compliant` is stamped only when `--executed-procedure` / `--executed-procedure-file` (with `--executed-params`) names what the round actually ran — otherwise the column stays NULL and the envelope says why, and a mismatch says WHICH of the two hashes disagreed. A second recording for the same subjects is refused unless `--supersede` is passed, and the superseding rows name the rows they replace. **A missed inventory plant fails the batch**: status `reopened_with_caveat`, the labels still written and marked, nothing consolidated. Distances are recorded and never thresholded into "novel" — an unjudged record reads `no-close-neighbour, unjudged`, which is not `new-mechanism`. **`screen --baseline`** is a read-only pass over a round's records giving each one's corpus nearest-neighbour cosine and the distribution over any `--status`/`--where` subset, with the percentile convention named in the output; **`slice-distances`** evaluates a pre-registered "farthest from the nearest home medoid" rule and hashes the picks — both are in *Ideation rounds* below. **`recheck --round-id R`** enqueues the scheduled convergent-discovery pass as a `custom` ledger job (`handler: convergent_recheck`) rather than running it inline: it is a whole-round pass, it reaches an external index when a mode names one, and it is meant to be resumable, which is what the job ledger is for and what a CLI process that exits is not. The pass re-retrieves every non-`raw` record of the round against the corpus AS IT STANDS NOW (and, under `--external-query-mode` paired with `--external-provider`, the external index), compares what it finds with what that record's own dossier already recorded, and writes anything new onto `idea.convergent_with` with an `idea_convergent_linked` event beside it. **It never re-scores**: not a label, not a status, not a verdict row — a convergence found after a round closed is logged, never applied, and the only write the handler can reach is that one column. `merged` and `eliminated` rows are re-checked too (they stay in the reference sets forever, and an eliminated idea whose twin surfaces later is exactly the finding this pass exists to log); `--status` narrows that, `--idea-id` names records outright — and the status filter applies to a named list too, because naming a record does not screen it. **A record with no novelty dossier on file is refused**, with the reason carried in the job checkpoint and the rest of the round still re-checked: "new" is measured against what the screen recorded, so with nothing recorded every neighbour comes back as a convergent discovery, which is the screen run late under another name. `--allow-unscreened` takes that reading deliberately and lifts the status filter with it. The job checkpoints per record, so a paused run does not re-issue an external query it has already made. Run it with `jobs start-worker --job-id <JOB-id>`, or leave it for an open-queue worker. |
| `room` | `create`, `status`, `post`, `score`, `stance`, `extracts`, `freeze`, `converge-check`, `export`, `admission-order` | The brainstorm-rooms runtime. `create` opens a room over 2-3 participants and its discussion points; `post` appends one turn; `score` records the moderator's judgment; `converge-check` reports (or, with `--apply`, applies) the `open → converged` transition at the fixed **>90% agreement bar**, refusing unless EVERY idea point is at or above it. The framework procedure is add-only on top of that and off unless asked for. `create --blind-first-turn` makes round 1 simultaneous: no participant envelope carries a prior turn until round 1 is complete. `post --kind position|question|closure` records the turn's kind, and a `closure` in the author's OWN first round on a point is refused — weak entailment first. `create --rank-all` appends one procedural `RANK-ALL` point; each participant files a complete ranking and a discrete per-point stance through `stance --file` (refused whole if it does not cover every point on both criteria), and `converge-check --apply` refuses until every seat has filed. Once stances exist, **`score` takes `--label` and refuses `--agreement-pct`**: the number is computed from the structured stances (per criterion, the modal share; the WEAKER criterion binds, because the rule is both-or-eliminated) and the judge's job is the discrete label, which is stated first in the result and in the event. `extracts --file` records the neutral extract pass for a point — one extract per turn, all five fields, refused if partial — after which the moderator's envelope carries the extracts and withholds the raw prose; `score --require-extracts` turns a missing pass into a refusal. `create --buster <participant>` names the assumption-buster seat so its recorded position is re-injected VERBATIM into its own envelopes. `author_launch` is dropped from every moderator envelope. NEITHER ownership is checked by LENS NAME at `create` and again at `score` — a re-spawned lens posts under a new launch id every turn, so the launch comparison alone never fires twice for the same lens. **`admission-order --round-id R --seed S`** is the order every consolidated idea of a round is roomed in: a seeded draw stratified on (arm, card), packed into rooms of 6 in batches of 4-8 (the charter band, overridable with `--no-enforce-batch-band`), the trailing partial room carried as the remainder in that same order. Each cell's members are sorted before the seeded shuffle, so the same pool handed over in a different order draws the same order and the same hash. A pool smaller than ONE room seats nobody: every record is carried in the remainder and the result says so in a `note` naming `--ideas-per-room` (a two-idea dry run is a declared room size of 2, not a failed draw). Nothing from a dossier is a parameter of it, and its `hash` is what a round escrows so the order it ran can be compared with the order it committed to. That escrow is a SECOND `prereg commit`, taken after the judged screen and before the first room opens: the pool is the round's consolidated records, so the order does not exist at frame time, and a hash in the frame-time params is either invented or back-filled. The `aiif_round` gate suite reads either place and names which one it read. |
| `term` | `propose`, `accept`, `reject`, `decide`, `merge`, `supersede`, `retire`, `review`, `mark-reviewed`, `scan`, `backfill-records`, `backfill-claims`, `relink`, `reindex`, `list`, `show`, `status` | The lexicon term store (`docs/reviews/LANE_E_TERM_STORE_DESIGN.md`). `propose --lemma --gloss --evidence <kind:id>...` needs at least one evidence token (`anchor:`/`record:`/`claim:`/`idea:`) or refuses (`SenseWithoutEvidenceError`) — the quote-grounding law applied to the lexicon; `--status current` runs the same accept path a later `accept` would. `decide <REL-...> --decision same_as\|variant_of\|scoped\|not_conflict\|unrelated\|rejected` resolves one pending conflict/duplicate candidate — `scoped` needs a repeatable `--disambiguator SENSE=text` for every member sense, `not_conflict` needs `--into SENSE`. `merge <TERM> --into <TERM>` folds the first into the second in one launch-attributed step. `retire` accepts a SENSE id only in this build — a TERM id is refused by name (`term_retire_not_implemented`; `lexicon.api` exposes no term-level retire yet). `scan`, `backfill-records`, `backfill-claims` and `relink` are E2 seams: until `trialerror.lexicon.{scan,candidates,backfill}` land, each reports `{"status":"unavailable",...}` rather than crashing or silently doing nothing. Every mutating verb requires `--by-launch` except `reindex` (FTS maintenance only). |
| `sidecar` | `start`, `status`, `stop` | Supervises the long-lived helper processes this program needs running — today the embedding server the `llama_server` query backend talks to. The argv comes from `[sidecars.<name>]` and the verbs take a NAME, so there is no `--cmd` and nothing reaches a shell. `start` is idempotent (a live recorded process is reported, not duplicated); `status` is also the supervisor — it restarts a `restart = "always"` sidecar it finds dead, and `--no-restart` reports without touching anything; `stop` is SIGTERM then SIGKILL after `--grace-s`, only ever against the pid this program recorded *and* still owns (the recorded pid is checked against the kernel's start time and argv for it, so a recycled pid is never signalled), with every wait bounded. State and logs live under the program's gitignored `run/` dir. See "Sidecars" below, and `trialerror doctor --only sidecar_alive`. |
| `obs` | `status`, `start-phoenix`, `smoke`, `audit-digest` | The first three no-op gracefully if the `obs` extra isn't installed. `start-phoenix` launches a detached local `phoenix serve` (the same detach technique as job workers: `DETACHED_PROCESS \| CREATE_NEW_PROCESS_GROUP` on Windows, `start_new_session=True` — i.e. `setsid()` — on POSIX). `smoke` emits one span of each of the four kinds (launch/retrieval/verification/job) and reports whether they flushed. `audit-digest` needs no extra at all: it builds one deterministic, verdict-free digest of a day of agent activity — see **The activity digest** below. |
| `mcp` | `ops`, `knowledge` | Starts the named stdio MCP server; **blocks** for its lifetime (serves until stdin closes). Not meant to be run interactively — see **Registering the MCP servers** below. |
| `accept` | *(no subcommands; `--suite smoke\|e2e`)* | Runs an acceptance suite. `--suite smoke` (the default) is the M15 harness: a full clean-checkout-shaped smoke journey against a scratch program (discarded after), plus an enumeration of the GPU/live-Claude-Code items that still need a real machine. `--suite e2e` runs the deployment handover-gate journeys (corpus, dashboard, ops, offload) against REQUIRED explicit `--program-root`/`--platform-root` and a shell-clock `--run-id`, recording one `e2e_check` event per check; `--phase report` lists every check id with its recorded status or `MISSING`, and the suite enumerates the human steps alongside the automated ones, each with the exact command and the criterion it is judged against. `--skip-gpu-live-cc-enumeration` omits the enumerated items from either suite. |
| `doctor` | *(top-level, no subcommands)* | `--license-audit` for just the vendored-file header scan; `--only CHECK_NAME` (repeatable) to run specific checks; **`--program-root` is required to see program-scoped checks** (schema version, dangling XIDs, stale chunks/embeddings/anchors) — without it they silently skip. See the full catalog below. |

### What `--actual-tokens` counts

A **visible token** is the total the HOST reports for a launch — the workflow counter for a
workflow run, or a single Agent's completion total. Its composition is the host's and is
unverified here: this harness records the number it was handed, and nothing in it can check what
went into it.

- `billed_multiplier` (per pool, default 2.75) is what maps visible tokens onto the plan meter, and
  it is **learned** — `budget calibrate` derives it from a pair of `quota_snapshot(source=screenshot)`
  readings. Every cap in `budget status` is in billed tokens; `visible_headroom_to_soft` is the same
  headroom converted back into the unit a booking's `--est-tokens` is written in.
- `reconcile_source` (`transcript` | `estimate` | `manual`) is a **caller-asserted label**, not a
  verified provenance: it records what the caller says the number came from. A fourth value,
  `event`, is the one that is not asserted — see *Reconcile provenance* below.
- Reconcile from the counter, **never** from a cache-write proxy. Cache writes are not the launch's
  visible total and do not scale to it by any fixed factor, so a reconcile taken from them silently
  mis-sizes every pool projection that follows.

### Reconcile provenance — measured, or asserted

`launch.actual_tokens` is what every cap check, every calibration and every weekly reading is
built on. There are two ways to put a number there, and the difference is recorded:

| how | `reconcile_source` | the split columns |
|---|---|---|
| `budget reconcile --launch-id X --actual-tokens N` | `transcript` / `estimate` / `manual` — whichever label the caller chose | left NULL |
| `budget reconcile --launch-id X --from-event` | `event`, which no caller can assert | filled from the host's own usage object |

`--from-event` reads the newest `subagent_return` event for that launch. The PostToolUse hook
writes one after every subagent return, carrying the `usage` object the host sent — total plus
the input / cache-creation / cache-read / output split — or an explicit `usage: null` when the
host sent nothing, so *"the host sent nothing"* stays distinguishable from *"nobody looked"*.
Three refusals, each naming `--actual-tokens` as the path out: no event on file (the Workflow
tool fires no hooks, and a session may have run with hooks disabled), `usage: null`, and a
usage object the reader could not reduce to a total. The success envelope names the event row
it read, so a reconciliation's provenance is followable to the row rather than resting on a
label. `event` is set only by that verb — `--reconcile-source` does not offer it, and the write
path refuses it from every other caller including the `book_launch` MCP tool's sibling tool.
Passing `--reconcile-source` alongside `--from-event` is **refused** rather than ignored: the
number and its label come from the same place, and a label the command cannot honour is not a
preference to drop silently.

The split is worth having because a launch whose 11,455 input tokens were all cache READS is a
cheap launch and one whose were all fresh is not, and `actual_tokens` alone cannot tell them
apart. It is reported by `budget rollup` and on the dashboard's budget card, always beside an
`attested` count: a composition drawn from one member of a two-launch tree must never read as
the tree's. A NULL split means nobody measured it, which is the one thing four zeros could not
say. `trialerror doctor --only reconcile_provenance` is the standing reading of the ratio.

### Pool rules — which pool a reading is about

`book_launch` only ever targets the **current** pool per (account, model class): the one with
the latest `period_start`. A superseded pool cannot move — its `spent_visible_tokens` is frozen
at whatever it was when the next period's pool was created — so:

- `budget_pool_overspend` judges current pools only, and reports superseded ones with their
  frozen numbers and no verdict. Judging a frozen pool asks for a fix that does not exist.
- `budget pools` prints every pool with the same projected / soft_cap / hard_cap / committed /
  headroom columns `budget status` computes, each labelled `current` or `superseded`, from the
  same function the doctor calls — so a pool the doctor names by id can be read from the CLI
  and the two cannot disagree about it.
- Each booking records the `pool_id` it was judged against (including a REFUSED one: that
  refusal is evidence about one pool's cap). Per-pool committed sums are therefore exact
  instead of inferred from (account, class), which used to sweep last period's live bookings
  into the new period's number the moment a pool rolled over. A booking made before that column
  existed carries no `pool_id` and counts toward the current pool, which is where the older
  arithmetic put it. On a `platform.db` still on v1 (nothing has opened a writable store since
  the upgrade) that column does not exist at all: the read-only surfaces fall back to the
  account+class sum and say so in each pool's `committed_attribution`, rather than failing the
  reader.
- **The committed side is per-pool; the spent side is not yet.** `reconcile` credits the
  *current* pool's `spent_visible_tokens`, not the pool the launch was booked and judged
  against, so a launch settled after a rollover moves this period's spent total. That is what
  makes a superseded pool's spent number frozen, and it is why the two halves of a pool's
  arithmetic are not symmetrical yet. Roadmap.

**A commitment is not a spend.** `committed_visible_tokens` is the live (PROVISIONAL + RUNNING)
`est_tokens` of the booking rows themselves — no hook, no state a spawn gate has to advance, so
a booking from a session with no hooks at all is counted from the moment it is written. It is
in the projected number and in *neither* the pool's `spent_visible_tokens` (only `reconcile`
moves that) *nor* the plan meter. That is why a PROVISIONAL booking can look missing to someone
reading either of those; `budget check` now names the outstanding commitment with its per-state
breakdown, and `committed_by_state` rides every pool entry.

### Quota staleness — a reading that has gone quiet

The plan-quota feed is written by the statusLine script, which Claude Code runs on a UI tick.
So the capture goes stale exactly while a session sits idle — which is also when it is most
likely to be consulted before sizing a booking, and a number that WAS true is the kind a reader
trusts without checking its age.

- The bar is `[budget] quota_max_age_s` in `trialerror.toml` (default 900 s); `--fresh-within-s`
  still wins for a one-off reading.
- `budget quota` reports the standing (`fresh` / `stale` / `absent`) with the age, and names the
  capture that refreshes it. `budget check` reports the same standing against the same bar, in
  its `quota.staleness` block and in its summary line — one program, one config, one capture,
  one answer, whichever surface is asked.
- **The booking gate refuses** on a stale capture — both `trialerror budget book` and the
  `book_launch` MCP tool, from one shared function. `--allow-stale-quota` (CLI) /
  `allow_stale_quota` (MCP) books anyway and records the reading it overrode on the launch, so
  the decision is on the record beside the numbers.
- Nothing captured is **not** stale: a program that has not wired the statusLine has no reading
  to be out of date, and refusing its bookings would make an optional feed mandatory by
  accident. Screenshot snapshots remain the ground truth either way.

### The activity digest — `obs audit-digest`

```
trialerror obs audit-digest --since 24h [--until ISO] [--transcripts DIR] [--history FILE]
                       [--program-root DIR] [--previous-dir DIR] [--out PATH]
                       [--allowed-write-root R ...] [--sensitive-path P ...] [--allowed-host H ...]
```

One deterministic digest of everything the agents in a session did in a window:
Claude Code transcripts (recursively, so subagent transcripts are included and
marked `kind: "subagent"`), the persisted shell history, the program's own event
rows, the doctor's FAIL/WARN rows at digest time, and — with `--previous-dir` —
the volume medians of earlier digests. Every top-level key is always present; a
source that is absent or unreadable becomes a `coverage` row **with a reason**,
because a missing source is itself something the reader must act on rather than
read past. Without `--out` the digest rides in the envelope's
`result.digest`; with `--out` it is written there (mode 600) and the envelope
carries the path, the sha256 and the counts.

The verb decides nothing. It counts, tags and masks; the rubric that turns a
digest into a verdict lives in the `sandbox-audit` skill, so every reader —
another agent or a person — applies the same one.

**Classifier tags.** Each shell command (from a transcript or from history)
carries zero or more:

| Tag | Earned by |
|---|---|
| `destructive` | recursive+force `rm` against a root-like path; force push; `git reset --hard`; rebase; `filter-branch`/`filter-repo`; `DROP TABLE`; `dd of=`; `mkfs`; `chmod`/`chown` on a containment or sensitive path; `crontab -e`/`-r`; truncating a ledger-shaped file |
| `network` | `curl`, `wget`, `ssh`, `scp`, `sftp`, `rsync` over ssh, `nc`, a package install from an explicit index URL, a `git` clone/fetch/pull/push of a URL, or any bare URL. Hosts are extracted and compared against `--allowed-host` |
| `permission` | `--dangerously-skip-permissions`, `--permission-mode`, or an edit naming `settings.json` / `settings.local.json` / `hooks.json` / a plugin manifest |
| `secret_path` | a token matching `--sensitive-path` (defaults: `/run/secrets`, `~/.ssh`, `secrets/`, `*.key`, `*.pem`, `rclone.conf`, `.env`, private-key filenames) |
| `package_install` | `pip` / `npm` / `pnpm` / `yarn` / `apt` install |
| `container` | `docker`, `docker compose`, `podman`, `nerdctl` |
| `cron` | `crontab`, `systemctl`, a path under `/etc/cron` |
| `git_push` | any `git push` |
| `encode` | `base64`, `openssl enc`, `xxd`, `uuencode`, or an archive being created |
| `exfil_suspect` | the encode-then-send pair: one command carrying both `encode` and `network`, or an `encode` command whose immediate successor in the same session is a `network` one — both halves are tagged |

**Judgement is opt-in, not assumed.** `inside_allowed_roots` and a network
call's `allowed` are `null` when no `--allowed-write-root` / `--allowed-host`
was declared. An empty policy yields "not judged", never a verdict the
deployment never asked for.

**Redaction.** Credential-shaped substrings are replaced with `<masked:N>`
(`N` = characters removed): `sk-…`, `ghp_`-family and `github_pat_` tokens,
`AKIA…`, the token after `Bearer`, the id in a ping/notify URL, any UUID, the
*value* of a query parameter whose name looks secret-ish (`…key=`, `…token=`,
`…secret=`), and any remaining hex run of 24+ or base64-ish run of 32+
characters. Beyond that, **file contents and environment dumps never enter the
digest at all** — only paths, command text, hosts and counts — and the input
keys that carry file bodies are not even scanned. Each list is capped
(`volume.caps`) with the drop counted (`volume.truncated`); `volume`'s totals
are computed before capping, so a spike stays visible in a capped digest.

Two things to expect when reading a digest. **Masking is the last step, never
the first** — a command is classified and its hosts extracted from the raw text,
then the masked copy is stored — so a random-looking subdomain is still tagged
`network` and still judged against `--allowed-host` even though its label comes
back as `<masked:34>`. And **the 24+ hex rule catches ordinary git commit ids**:
`git show --stat <masked:40> -- some/path` is what a routine command looks like
here. The verb, flags and paths survive, so the command's meaning does; recover
the id from the transcript at the row's session id and timestamp. The rule
over-masks deliberately — one that tried to tell a commit id from a token would
eventually get one of them wrong.

### Memory items — the `l0_abstract` authoring rule

`memory search`, `search --boot-bundle` and the SessionStart hook return
`l0_abstract` lines and never bodies (progressive disclosure; the
`memory_l0_index_budget` doctor check warns when the L0 tier alone outgrows
`[memory] token_budget`). The abstract is therefore the only thing that decides
whether an item is ever read. Rule, adopted 2026-09-05 from WikiSkill's
index-entry rule (arXiv:2608.27454): every `l0_abstract` states
**PROBLEM + ROOT CAUSE + FIX** in one or two sentences — never a topic label.
Bodies: root cause, not symptom; exact command sequences as run; update rather
than duplicate (`put` upserts by `(key, account)` — reuse the key); 10-30
lines. The `/close` skill carries a worked `memory put` example and `/boot`
reads the index under the same rule. Not enforced in code — `put` accepts any
string — so it is a review rule for the operator and for the close ritual. Two
code follow-ups are tracked separately and will surface items rather than
rewrite them: save-time conflict candidates (advisory only) and age-based
`needs_review` surfacing as a doctor check.

## The two MCP servers

Both are hand-rolled stdio JSON-RPC 2.0 servers (`trialerror/mcp/protocol.py`) implementing the
MCP 2025-06-18 spec's tools-only subset directly — no `resources`/`prompts`/`sampling`.
Tool-count is asserted in tests (`trialerror-ops` = 12, `trialerror-knowledge` = 12 — design
§5.1's original 11 plus lane e's E3 step, which added the read-only `term_lookup`), matching the
design's own per-context tool-ceiling reasoning (§5.1): attach subagents (Fable-tier, C-0088) to
only the one server they need.

**`trialerror-knowledge`** (read-only, 12 tools) — `search`, `get_chunk`, `get_source`,
`get_document_outline`, `resolve_quote`, `similar`, `graph_neighbors`, `corpus_stats`,
`memory_search`, `list_requests`, `poll_job`, `term_lookup` (lane e E3: look up one lexicon
term by lemma or term_id — senses, aliases, open-conflict flag; anchor-backed evidence
excerpts fenced by the anchor's source license_tier exactly like `search`/`get_chunk`).

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
- **DjVu route**: `.djvu`/`.djv` sources get a `djvu` stage in front of `normalize`/`ocr`
  (`trialerror.ingest.normalize_djvu`): it shells out to DjVuLibre's `ddjvu -format=pdf` to
  produce a derived PDF under `<archive_dir>/derived/<doc_id>/` (durable — a sibling of
  `archive/<doc_id>.txt`, never `jobs_work/` scratch space, which this document's `raw_path`
  now permanently points at), and probes the text layer with `djvutxt` (>=200 non-whitespace
  characters over the document counts as "has one"). **When there is a text layer, that layer
  is the document's text**: the stage calls `djvutxt --page=N` once per page and those page
  texts become the elements directly, with the derived PDF kept for viewing only. (It used to
  route the derived PDF through the PDF text normalizer instead, and cross-check the verdict
  against that PDF's own extractable text — which on the first real `.djvu` threw away a
  484,825-character text layer to OCR images of the same pages, because `ddjvu` had not carried
  the text into the PDF.) The cross-check's purpose is kept, applied to the extracted per-page
  text: it must average at least 20 *usable* characters per page (U+FFFD and control bytes do
  not count), or the document takes the OCR route. **No text layer** sends the derived PDF
  through the same `ocr` job a native scanned PDF uses. Which source of truth was used is on
  the job checkpoint as `djvu_text_source` (`djvutxt` or `ocr`), alongside the page count and
  the derived PDF's own verdict. Install the
  Debian package `djvulibre-bin` (ships both binaries); on a machine without it on `PATH`,
  point `[ingest.djvu] ddjvu_exe`/`djvutxt_exe` at their paths in `trialerror.toml` (a
  configured path is validated the same way a bare command on `PATH` is — a typo or a
  machine-specific stale path raises the same named "tool missing" error, not a bare
  `FileNotFoundError`). A document's `normalizer_id`/`normalizer_version` read
  `djvu-ddjvu`/the `ddjvu --version` probe (or `unknown`) instead of the generic normalizer id
  for anything that went through this route; the derived PDF's own sha256/route/text-layer
  signal is on the `djvu` job's own checkpoint, readable via `trialerror jobs list` (not
  `jobs logs`, which only shows event history). `DEFAULT_DJVU_TIMEOUT_S` (1800s) exceeds the
  default job lease (900s, same trap the real OCR backend's own timeout has) — pair
  `[ingest.djvu] timeout_s` with a matching `--lease-s` for a real long-running book
  conversion so this worker's own timeout fires before the lease-expiry reclaim would.
- **Run**: `trialerror jobs start-worker` — `--mode once` claims and runs a single job then
  exits; `--mode loop` polls until idle (`--max-idle-polls`, default 3) or
  `--max-iterations` is hit. `--foreground` runs inline in your terminal (what a detached
  child itself invokes); omit it and the command spawns a real detached background
  process — `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` on Windows, `start_new_session=True`
  (a `setsid()` in the child, detaching it from the controlling terminal) on POSIX — and
  returns immediately with its `pid` and `log_path`.
- **Two-machine split — the worker holds the model**: on the GPU box, `trialerror offload
  worker` keeps ONE embedding-driver process alive for the whole run and reuses it for
  every batch of every job, instead of launching a driver (and reloading a multi-gigabyte
  model) per batch. Measured before this change: a 217-chunk document at `--batch-size 8`
  cost 28 model loads for 28 batches of real work. Two things follow for you. First,
  `--batch-size` now means what it says — how many sequences to hand the GPU at once — and
  its default is **4**, measured rather than chosen: at 64 the driver batched 16 sequences of up
  to 2048 tokens in bf16 and a 16 GB card spilled about 16 GB into system RAM, at roughly six
  seconds per chunk; the same queue at 4 ran several times faster with bit-identical vectors.
  Raise it only with a measurement from the card in front of you — past the point where the
  working set leaves VRAM the failure mode is not an error, it is a run that is quietly ten
  times slower.
  Second, the run's `--format text` log says **`driver started`** exactly once, followed by
  one `ran in <n>s` line per job; a second `driver started` means the driver crashed and
  was restarted, and the failure just before it carries the driver's stderr head.
  `[ingest.embed] session = false` restores the old one-process-per-batch protocol if some
  driver ever needs it. The worker closes the driver when the queue empties, so nothing is
  left holding the GPU after *"Queue empty — safe to switch DEV off"*.
- **Deploy the queue host's wrapper before the worker.** The progress payload the worker sends on
  `heartbeat` is optional by design, and a wrapper that does not know a key refuses the whole
  payload — so a worker newer than its wrapper beats *bare* (it says so once, as `heartbeat payload
  refused by the queue`) and keeps its claim stamped instead of losing every beat to a status file.
  Update `deploy/sandbox/offload-shell.sh` first and the mismatch never arises.
- **The root the worker reads is the BACKEND CONFIG root** (D-FB-6): `trialerror offload worker
  --backend-config-root <root>` names the root whose `trialerror.toml` says where marker/Qwen3 live
  (`[ingest.ocr]`, `[ingest.embed]`) — and nothing else. The queue comes from `--remote` or
  `--queue-root`, which is exactly what the old name `--program-root` kept implying it did not.
  The old spelling still works (a hidden alias resolving to the same value, so the DEV launcher and
  `supervise.sh` keep working unchanged) and every envelope from a run that used it carries the
  deprecation note twice over: in `meta.deprecated_flags` for a JSON reader, and as a `warnings`
  entry, which is the half `--format text` actually prints — the launcher that still spells the old
  name runs `--format text`, so a note only `meta` carried reached exactly the operators who had
  already moved (verify V-2).
- **After a fix on master: update the worker, then `jobs retry` the jobs that met the defect.**
  A marker that exhausted its DEV attempts is terminal: `failed/<job_id>/` holds its
  `manifest.json` and `error.json`, the stage refuses with *"terminal after N DEV attempt(s)"*
  on every remaining ledger attempt, and the row abandons. Fixing the worker does nothing for
  it by itself — the terminal directory is what the stage reads, not the worker's version. The
  order is: deploy the fix to the GPU box (and `deploy/sandbox/offload-shell.sh` if the wrapper
  changed), then, on the sandbox, **`trialerror jobs retry <job_id> --reason "<what was
  fixed>"`** once per job. That verb moves the two halves in one direction: the queue entry
  first, the ledger row second.
  - The marker goes back to `queued/` **and** `pending/` with `offload_attempts` reset to 0 and
    a `retried` stamp appended (`ts`, `reason`, `previous_attempts`, `previous_error_sha256`),
    so the GPU worker claims it on its next poll. The input payload is rebuilt from the record
    — the raw file, or the document's chunk rows — because the terminal transition purged the
    queue's copy; if that payload is no longer on the sandbox (the raw file was moved, the
    document was retracted, the chunk rows changed), the retry **refuses by name and says what
    is missing** rather than queueing a job the worker cannot run.
  - **The failed attempt is kept, never deleted.** `failed/<job_id>/` moves to
    `failed/_retried/<job_id>.<stamp>/` with its `manifest.json`/`error.json` untouched and a
    `retried.json` note beside them. That directory is the evidence that the GPU time was spent
    and what it was spent on. `offload status` counts it under `retried`, separately from
    `failed`, and the `offload_failed` doctor check does not fire on it — the human it asks for
    has already been.
  - A `parked_largeformat/` entry for the same job id is **left alone and named in the
    envelope's `warnings`**: this harness never wrote it, so whether the parked copy should also
    be released is your call, not a side effect of retrying the ledger row.
  - **Interrupted halfway, run it again.** The queue move is idempotent and detectable, so a
    `jobs retry` that died between its two halves is finished by repeating the same command:
    the second call finds the queue entry already back with its stamp, completes the jobs-store
    half, and says so (`offload.already_retried`) without making a second evidence directory or
    a second stamp.
- **`trialerror offload doctor`** runs this subsystem's own checks in one place, the way `ingest
  doctor` does for ingestion: the queue's five (`offload_backlog`, `offload_stale_claims`,
  `offload_failed`, `worker_heartbeat_stale`, `offload_control_orphaned`), the record's
  `fake_backend_rows`, and `offload_backend_root_resolved` — which root was read, what each stage
  names, and whether it resolves on the machine you ran it from. It registers nothing of its own:
  every check is also `trialerror doctor --only <name>`, so this verb is a convenience and never a
  second source of truth. On the queue host a well-configured program reads `pass` with both stages
  named `'offload'` ("not run on this machine"); on the GPU box it is the pre-flight for the stages
  that box really runs — it tells you they resolve before you wait for a worker to refuse. Read
  `pass` with care in one direction (verify V-3): a stage declared `backend = "fake"` or
  `backend = "offload"` with no `require_real_backends` contributes nothing to the severity, because
  that is the queue host's own normal state — and `trialerror offload worker` declines to run either
  of those stubs (`fake_backend_refused`). So on the GPU box, check `runs_here` in the details: the
  verb pre-flights the stages this machine runs, not a worker's willingness to run stubs it is
  configured for. It exits non-ok when any check **fails**.
- **Pause, resume and stop a GPU worker** (ruling C-0097): anything the interface can start,
  the interface can pause and stop — *cooperatively*. `trialerror offload worker-control
  --worker-id <id> --pause|--resume|--stop --by-launch <LNCH>` leaves one small request in the
  queue; the worker reads it off its next heartbeat reply and acts at its next **cooperative
  checkpoint**: between embed batches, after marker returns, or between jobs. Nothing is killed
  — there is no preemptive kill anywhere in this harness — so a request is a request, and
  `trialerror offload worker-status` is how you watch it land.
  - `--pause` finishes the unit in flight, **keeps the claim** and keeps heartbeating with state
    `paused` — including a pause taken between jobs, where it beats under its own idle id so the
    card can still show `PAUSED` and the next word can still reach it. A pause lasts until you
    resume or stop it; a launcher that passes the worker a pause bound gets a `pause_expired` line
    in the log rather than a worker that quietly goes back to work still reporting `paused`.
  - `--resume` continues from the next unit. Nothing is repeated and nothing is skipped.
  - `--stop` finishes the unit in flight, writes a partial result into the worker's own work
    directory (`status: "stopped"`, with the unit count), hands the claim back through the
    ordinary `return` verb so the job is `pending` again, and exits. The partial result is a
    record of the GPU time spent, never a result: it is never published.
  - **Every control act carries a launch.** No `--by-launch`, no control — the refusal names the
    missing launch, and the act lands in the event log as `offload_worker_control`. A request for
    a worker that has not reported refuses too (`--even-if-absent` leaves it waiting on purpose).
    The only other refusal is **the same word asked for twice while no beat has reported reading
    it**: any *different* word is accepted at any time, so a pause can always be resumed and
    always escalated to a stop.
  - **A request ends when it has been acted on.** The worker reports the last word it applied
    (`control_seen`, on every beat), and the sandbox clears a request once that word has been
    acknowledged and has nothing left to stand for: a `resume` as soon as it is read, a `pause` or
    a `stop` once the worker has gone idle or exited. `worker-status` and `kick` (which the jobs
    loop runs every cycle) do the clearing and name what they cleared in `reaped` — which is why
    **restarting a worker you stopped does not stop it again**. A stop a worker is still *ignoring*
    is deliberately left on disk: that is the evidence `worker_heartbeat_stale` fails on.
  - **`--clear` drops a pending request** without asking for anything — for a request no worker
    ever read (left for a worker that did not start, or one you changed your mind about before any
    of the clearing verbs ran). It is an act like the others: `--by-launch`, and logged.
  - **A stale request is ignored.** A control file older than one hour reads as "no request", so a
    laptop switched off for a week does not come back and stop itself over last Tuesday.
  - **OCR's unit is the RANGE when the document is chunked, and the whole job when it is not.**
    A document big enough to need page-range chunking (see *OCR page-range chunking* below) is run
    range by range, `units_total` is the number of ranges, and the checkpoint falls between them —
    a pause point every 16 A4 pages at the default budget, instead of one at the end. A document that
    fits one invocation is run exactly as it always was, so a pause or stop asked for mid-OCR takes
    effect only after marker returns. Embed jobs checkpoint every batch, so at the default
    `--batch-size 4` a long document offers a pause point every four chunks.
  - **The JOBS card shows it.** One row per worker above the queue counts — state, job, progress,
    pace, ETA, heartbeat age, settings — with `PAUSED` / `STOPPING` / `LOST` chips, and the same
    chips on the collapsed card. `lost` is the heartbeat age past 2x the beat interval + 60 s, and
    a run that has ended reports `exited` rather than `idle`, so "nothing claimed" and "nobody
    there" are different readings on the card.
    `te-status.sh` prints the same rows after the queue counts. The dashboard's
    `worker-control` write action does exactly what the CLI verb does, through the same function.
  - **On a job-kind switch the idle backend is unloaded** — the embedding driver's subprocess
    exits and its VRAM comes back before marker starts, because the measured alternative was both
    models resident at once and 0.9 GB of free RAM. `--keep-resident` restores the old behaviour
    on a machine with room for both; a switch costs about a minute of reloading.
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
- **Retry — the way back from `failed`/`abandoned`.** `resume` covers `paused` and nothing
  else, so until this verb existed a job that had settled unsuccessfully had no sanctioned
  route back at all: after a tool-side fix, the documents that met the defect FIRST were the
  only ones the fix could not help, and the remaining option was editing `jobs.db` by hand,
  which this harness's rules forbid. **`trialerror jobs retry <job_id> --reason "…"`** takes a
  `failed` or `abandoned` row back to `pending` with `attempts` reset to 0 and
  `next_attempt_ts`/`claimed_by`/`lease_expires_ts`/`settled_ts`/`failure_class` cleared. What
  it does **not** clear: `last_error` is kept behind a `retried <ts>: ` prefix, so `jobs list`
  still shows what the job failed with; the `checkpoint` is kept too (`--clear-checkpoint` when
  the cursor itself is what was wrong). `--max-attempts N` (1–10) gives it a new budget;
  omitted, the old one stands. `--reason` is required — a row that came back from terminal with
  no stated cause is one nobody can audit — and it lands, with the previous state, the previous
  attempts and the previous `last_error` in full, on a `job_retried` ledger event.
  It **refuses by name, naming the state it found**, for `complete` (nothing to redo), `pending`
  (already claimable — `jobs kick` clears a retry delay), `claimed`/`running` (a worker holds it:
  pause it, let it stop, then retry the settled row) and `paused` (that is `jobs resume`, which
  is a different act — it does not reset attempts or clear the failure class). Like `resume`, it
  spawns nothing: follow it with `jobs kick` or `start-worker`.
- **Polling and waking** (2026-09 mining adoption rowboat-F8): a looping worker's idle nap
  is jittered deterministically per worker (`--jitter-frac`, default ±25% of
  `--poll-interval-s`; `0` restores a fixed nap) so N workers launched in the same second
  stop claiming on the same tick. `trialerror jobs kick` writes a wake token next to
  `jobs.db` and every napping worker ends its nap at its next tick — the way to say "a job
  was just enqueued, stop waiting" without waiting out a poll interval. A kick wakes
  workers that already exist; it never spawns one, and `--no-wake-signal` opts a worker out.

## Query-time embeddings — the side of retrieval that runs *here*

A two-machine program embeds its corpus on the machine with the GPU: `[ingest.embed]
backend = "offload"`, and the local backend is a stand-in whose `embed_batch` raises by
design. That is correct for documents, and it is a problem for retrieval, because a search
has to embed something no document stage will ever see — the query itself — and it has to
do it in the process answering the search. Until this was separated, every vector-tier
call on such a program raised in the caller: `query search` printed a traceback,
`verify hypothesis` and `lens screen` died mid-run, and only `--mode fts` worked.

**The table.** `[ingest.embed.query]` names the backend that embeds query-time text. It is
optional, and its default is `backend = "same"` — the document backend object itself, so a
program that never writes the table behaves exactly as it did before the table existed.

| `backend` | What it is | Needs |
|---|---|---|
| `"same"` (default) | the document backend, unchanged | nothing |
| `"offload"` | states the current situation explicitly: nothing embeds here | nothing |
| `"llama_server"` | **the production choice on a CPU-only machine**: the same GGUF encoder in a long-lived sidecar on loopback, reached over HTTP | `tokenizer_model_path` (the GGUF whose *vocabulary* pre-truncates the input; no weights are loaded here); optional `url` (`http://127.0.0.1:8871`), `timeout_s` (60), `n_ctx` (2049), `native_dims` (2560), `query_prompt`, `sidecar_name` (which `[sidecars.<name>]` serves this URL — used only so a refusal can name the verb that starts *your* sidecar rather than one it guessed) |
| `"llama_cpp"` | a GGUF encoder run on the CPU **in this process** — the reference implementation of the recipe | `model_path`; optional `n_ctx` (2048), `n_threads` (all cores), `n_threads_batch` (see below), `pooling` (`"last"`), `native_dims` (2560), `query_prompt` |
| `"fake"` | the deterministic hash stand-in — for a scratch program or a test, never for a corpus you intend to search | nothing (refused outright under `[ingest] require_real_backends = true`) |

**Both sides must agree on `model_key` and `dims`, and resolution refuses when they do
not**, naming both. This is the one hard rule here: a query vector produced under key B and
ranked against `vec_chunks__A` does not error, it returns 16,000 rows in an order that means
nothing. The sub-table therefore does not normally state either value — they are inherited
from `[ingest.embed]`, which is what makes a two-line query table a complete
configuration:

```toml
[ingest.embed]
backend = "offload"        # documents are embedded on the GPU machine
model_key = "an-embed-key" # the key that machine's backend stamps on every row
dims = 2048

[ingest.embed.query]
backend = "llama_server"   # queries are embedded here, by the sidecar, under the same key
tokenizer_model_path = "/models/an-embedding-model.gguf"
```

**Which of the two local backends.** Both implement the same recipe — the same prompt, the
same token-level truncation, the same normalise/slice/normalise — through the same two
functions, so their vectors agree with each other to 0.9998 cosine and with a GPU-embedded
corpus to ~0.9994. What differs is the runtime:

| | `llama_server` (sidecar) | `llama_cpp` (in process) |
|---|---|---|
| 64-token query, measured on a 12-thread CPU capped to a 10-CPU quota | **2.65 s** | 3.2 s tuned, 22.9 s untuned |
| model load | once, by the sidecar; a CLI call pays an HTTP round trip | once per process (~0.8 s warm, 3.5 s cold) |
| peak resident set | ~5.3 GB, in the sidecar | up to 7.0 GB, in *this* process |
| needs | a running sidecar (`trialerror sidecar`) | nothing but the wheel and the file |

So: the sidecar for anything that answers queries, the in-process backend as the reference
path and for a one-off where starting a server is not worth it. A 256-token query costs ~15 s
on either — nothing measured above ~100 tokens meets a 3-second bar.

**`n_threads_batch`, and why it is the setting that matters.** An embedding decode does not
use `n_threads` at all (that pool is for generation); it uses the *batch* pool, which
`llama-cpp-python` sizes from the CPUs the process can SEE. Inside a cgroup whose quota is
smaller than that, the extra worker threads spin-wait at every graph barrier and the same
query costs **22.5 s instead of 3.2 s** for a vector identical to the bit. The harness
therefore defaults `n_threads_batch` to `min(n_threads, <cgroup CPU quota>)`, reading
`cpu.max` (cgroup v2) or `cpu.cfs_quota_us`/`cpu.cfs_period_us` (v1) — at the path
`/proc/self/cgroup` reports for this process, walking up to the root, so a nested cgroup is
read rather than missed — and falling back to `os.cpu_count()` when nothing anywhere caps it.
`[ingest.embed.query] n_threads_batch` overrides it, above the quota included: an override
that could not override the cap would not be one. **`0` and negatives are refused** at
construction rather than honoured: they are not thread counts, and llama.cpp reads a
non-positive one as "size the pool from the visible CPU count", which is the 22.5 s behaviour
this setting exists to remove. All three numbers are reported in
`query_embed_backend_runnable`'s detail and in any refusal reason, because a slow encoder does
not otherwise name its cause; if another backend in the same process already loaded that GGUF
with different settings, the detail also carries an `effective` block with the numbers the
resident model actually carries (one process holds one resident copy per file, and the first
construction wins — a second copy would cost gigabytes to make a report tidier). The same arithmetic applies to the sidecar's own
`-t` flag, which is why the reference command below runs it at 8 rather than 12.

**The sidecar's off-by-one.** `llama-server` refuses a request of *exactly* its context
length, so the sidecar is configured one id wider than the geometry (`n_ctx = 2049`) while
the client truncates content to 2047 ids and the server's own EOS makes 2048 — one below the
context, by construction. The prepared string is byte-identical to the in-process client's.
That property is *joint* with the `-c` you start the sidecar with, so it is checked rather
than assumed: `runnable()` reads the served context off `/props` and **refuses** a sidecar
narrower than `[ingest.embed.query] n_ctx` (a server started `-c 2048` against a client at
2049 answers every short query and fails only on long inputs, mid-run — the exact failure
this arithmetic exists to prevent), naming both numbers and the flag. A build that does not
report its context is reported `context_check: unverified` and is never a blocker, on the
same reading as the served-model check.
`tokenizer_model_path` is that client's own copy of the vocabulary, opened `vocab_only` — it
reads the GGUF's vocabulary and **no model weights**, so a CLI process that embeds one query
allocates kilobytes for it, not gigabytes.

**The wheel's own log never reaches stdout.** Loading that vocabulary prints
`llama_context: n_ctx_seq (512) > n_ctx_train (0) -- possible training context overflow` on
some builds, and it printed it to file descriptor 1, ahead of the JSON envelope the CLI
then wrote there — so every `json.load` of an embedding command's output raised and a
script calling one could not read its answer. Every call into the wheel (both `Llama`
constructions, both tokenize paths, the in-process embed) now runs with fd 1 redirected to
fd 2. At the descriptor, because the writer is C code holding fd 1 and `verbose=False` does
not reach it; not through the wheel's `llama_log_set`, whose signature has moved between
releases and whose silencer would quietly stop silencing after an upgrade. **Nothing is
discarded** — the text lands on stderr, where an operator reading a terminal still sees it,
and stdout carries exactly one JSON object.

Every failure of either backend is reported as TEXT by
`trialerror doctor --only query_embed_backend_runnable`, never as a traceback: a missing
library, a missing model file, a wheel whose C library disagrees with the local one (an
`OSError` from inside `ctypes`), a sidecar that is not running, a sidecar still loading its
model, and — fail-closed, because the failure is otherwise silent — a sidecar found serving a
*different* model than `tokenizer_model_path` names.

One thing to know about the two knobs that interact: the retrieval instruction
(`query_prompt`) is prepended to the query and the two share one token budget of
`n_ctx - 1`, so a `query_prompt` long enough to fill that budget leaves no room for the
query. That is refused, naming both numbers, rather than silently truncating the prompt and
embedding an instruction with no query in it. With the default prompt (~85 tokens) and the
default `n_ctx` (2048) there is nothing to do here; if you shorten `n_ctx` or write a long
custom prompt, this is the failure you will see.

**Degrade or refuse, by caller.** One rule, applied per surface rather than globally,
because "no query vector" means different things to a search and to a verdict:

- **`query search` (and the MCP `search` tool), modes `auto`/`hybrid`/`graph`** — runs the
  full-text tier, reports `stats.vector_skipped_reason`, keeps `tiers_used` honest, and
  carries a `query_embed_backend_unrunnable` entry in the envelope's `warnings` naming the
  doctor check and this config table. The results are real results; what would have been
  wrong is not saying that half the pipeline did not run.
- **`query search --mode vector`** — an envelope **error** (`query_embed_backend_unrunnable`)
  with the doctor command as its next action. The vector tier *is* the search in that mode;
  there is nothing to fall back to.
- **`verify hypothesis`** — **refuses**, before it commits a pre-registration or calls a
  judge, and with no full-text carve-out. It is a status-changing read (C-0096): a verdict
  written off a narrower instrument while the record says `mode = "hybrid"` describes the
  instrument, not the corpus.
- **`lens screen`** (and the plant battery, and the convergent re-check) — **refuses**.
  `lens screen --corpus-mode fts` restricts the **R4 retrieval tier** to full text and
  records itself in every dossier it produces (`corpus_mode`, and
  `stratify_method = "rank_fallback"` where the near/moderate/far split could not be made by
  distance) — use it deliberately, not to get past the refusal: it does not remove the
  screen's own need to embed each record's statement for R1/R2/R3, so the refusal stands in
  either mode and says so.
- **`query similar --id <chunk_id>`** — unaffected in every case. It ranks a **stored**
  vector and embeds nothing.

**The resident similarity matrix.** The two ranking paths whose universe is the whole table
— `query similar`, and `query search --mode vector` with no filters — used to deserialise
every row of `vec_chunks__<key>` in Python: on a 16,000 × 2,048 corpus, 5.7 s of wall clock
per query, nearly all of it per-row unpacking. They now rank against a cached float32
matrix under `<index_dir>/vecmatrix/<key>.npy` (plus a `.ids.json` sidecar): **0.10 s warm,
0.21 s in a fresh process, 3.1 s the once when it has to build**, and 131 MB on disk for
that shape. Three things worth knowing:

- **The answer does not change.** numpy narrows; the final ordering is computed by the very
  function the uncached path uses, on the same vectors, so the returned rows and scores are
  byte-identical (tie order included). Below 2,000 candidate rows the matrix is not engaged
  at all, which means every two-stage `auto`/`hybrid` search — capped at 500 full-text
  candidates — is on exactly the path it always was.
- **Freshness is a fingerprint** (row count, max rowid, the registry's `created_ts`),
  checked on every query and rebuilt on any difference, so a stale cache is never served.
  `ingest reindex-vectors` and `ingest purge-embeddings` also drop it explicitly, and
  `trialerror doctor --only vecmatrix_stale` reports one that has drifted.
  `trialerror query stats` reports presence, staleness and both fingerprints per model key.
- **numpy is optional**, exactly as sqlite-vec is: absent, every call declines and the old
  path answers. The cache lives under the gitignored index dir and is rebuildable from
  `knowledge.db` at any time — delete it freely.

**`[session] default_account`.** A program with more than one registered account refuses to
boot without `--account`, because attribution is never guessed — and the SessionStart hook
has no flags to pass. `[session] default_account = "<account_id>"` is the program's standing
answer in writing; it is checked against the register on every boot and refuses when it
names an account the program does not have (including when a single account would otherwise
have defaulted — a key nobody reads is a key that is quietly wrong). An explicit `--account`
still wins.

## Sidecars — the processes this program needs running

A sidecar is a process the program needs *running* but does not own the code of. Today there
is one tenant: the embedding server the `llama_server` query backend talks to, which loads a
multi-gigabyte model once and answers on loopback for the rest of the day.

```
trialerror sidecar start <name> [--cmd-from-config]
trialerror sidecar status [<name>] [--no-restart]
trialerror sidecar stop <name> [--grace-s 10]
```

**The command comes from config, never from an argument.** `[sidecars.<name>]` carries the
argv, the working directory and the environment; the verbs take a NAME. There is no `--cmd`,
no shell string, and a `command` given as a string is refused rather than split — an agent
should be able to start the process an operator configured and should not be able to start
something else. (`--cmd-from-config` is the only mode; the flag exists to say so out loud.)
The reference configuration, with generic paths — fill in your own:

```toml
[sidecars.embed]
command = [
  "/opt/llama.cpp/llama-server",
  "-m", "/models/an-embedding-model.gguf",
  "--embedding", "--pooling", "last", "--embd-normalize", "-1",
  "-c", "2049", "-b", "2049", "-ub", "2049",   # MAX_SEQ + 1 — see the off-by-one above
  "-t", "8",                                   # threads, not more than the cgroup quota
  "--host", "127.0.0.1", "--port", "8871",
]
env = { LD_LIBRARY_PATH = "/opt/llama.cpp" }   # a vendored runtime usually needs this
health_url = "http://127.0.0.1:8871/health"
restart = "always"
```

**No host act, and no tmux dependency.** `start` spawns the process detached (its own
session, so it survives the CLI and does not take your Ctrl-C) and records it under the
program's `run/` directory — `run/sidecars/<name>.json` carries the pid, the argv, the
health URL, `started_at`, `heartbeat_at` and the log path, and `run/sidecars/<name>.log`
carries the process's own output. Any later process can therefore report on it and stop it.
Running the verb inside a tmux window is a convenience, not a requirement. `run/` is
gitignored and is worthless after a reboot: delete it freely while nothing is running.

**Supervision is a poll, and nothing here pretends otherwise.** `restart = "always"` means
"`status` restarts this if it finds it dead" — so the thing doing the supervising is whatever
already runs on a loop and calls `status` (the jobs loop, a cron, an operator). A daemon that
claimed to be watching from a process that has exited would be worse than none.
`--no-restart` asks the same questions and changes nothing, which is what the doctor check
uses. `restart` defaults to `"never"`.

Three smaller properties worth knowing:

- **`start` is idempotent, including under concurrency.** A start against a live recorded
  process reports `already_running` and spawns nothing: two model servers on one port is a
  multi-gigabyte mistake whose only symptom is a bind failure in a log file nobody reads. The
  read-then-spawn window is held under an exclusive lock on `run/sidecars/<name>.lock`, so a
  poll loop restarting a dead sidecar at the same moment you start it produces one process and
  one recorded pid — not several, of which `stop` could reach only the recorded one. A start
  that cannot take the lock refuses with `start_in_progress` and spawns nothing.
- **The heartbeat is evidence, and nothing that only reads it moves it.** `heartbeat_at` is
  rewritten only when `status` has confirmed the process alive *and* not failing its health
  check, so its age answers "when did anything last verify this?" That age is reported back as
  `heartbeat_age_s` in every `status` row and in the doctor detail, and the doctor check asks
  for the reading *without* the refresh — a reader that refreshed the timestamp it reports
  would be the only thing keeping it fresh. A sidecar with no `health_url` reports
  `health.configured: false` rather than a green light nobody earned; one that HAS a
  `health_url` but is not running reports `configured: true` with `skipped` naming why nothing
  was asked, because "not probed" and "not configured" are different statements.
- **`stop` only ever signals the pid this program recorded *and still owns*** — SIGTERM, then
  SIGKILL after `--grace-s` — and is idempotent, because the state it is asked to reach is
  "not running". There is no kill-by-name and no port scanning anywhere in the group. A pid is
  never trusted on its own: `start` records the kernel's own facts about the process beside it
  (its start time, and the argv the kernel reports for it), and every verb checks them, so a
  state file left by an earlier boot whose pid has since been recycled makes `start` spawn,
  `status` report `dead` and `stop` report `stale_pid` and signal nothing — rather than
  adopting, or killing, a stranger. A zombie counts as gone, and every wait is bounded: past
  five seconds after the SIGKILL, `stop` reports `kill_timeout` and keeps the state file for a
  retry instead of spinning.

`trialerror doctor --only sidecar_alive` is the one-line version of all of it: **warn**, never
fail (a dead embedding sidecar costs the vector tier, which every retrieval surface already
degrades from and says so), naming each configured sidecar that is not running or not
answering.

## Extraction quality — measure first, refuse second

Every stage of this pipeline can succeed on a document whose text is unusable. A
mis-encoded text layer normalizes to control bytes; a scan of a two-column page OCRs into
interleaved half-sentences; a word-spacing model that gave up glues a page into one
4,000-character "token". Nothing raises, the document reaches `status = 'indexed'`, and the
first thing that notices is an agent reading nonsense out of a citation — by which point
the text has been chunked, embedded, indexed and quoted.

**Four numbers, each with its denominator stated.** They are computed over the *sanitised
element text* a document already carries, so measuring costs one read and no model.

| Measure | What it counts | Denominator | Default bound |
|---|---|---|---|
| `glued_token_rate` | whitespace tokens longer than 25 characters | **all tokens, counted by the chunker's own `estimate_tokens()`** — the same number that cuts this text into 1,024-token chunks, called rather than re-implemented | `glued_token_rate_max = 0.10` |
| `unusable_char_count` | U+FFFD plus the non-whitespace C0/C1 control bytes (the normalizer's own regex, imported — not a second copy) | **none, deliberately.** An absolute count: a rate would divide a mis-decoded page's burst of replacement characters by a whole book and report a reassuring 0.0001 | `unusable_chars_max = 200` |
| `terminator_density` | sentence terminators (`.` `?` `!` and the CJK/fullwidth closing forms) | **1,000 characters of text.** The reading-order proxy: ordinary prose lands at 8–20, a stream of layout fragments near zero | `terminator_density_min = 1.0` |
| `chars_per_page_cv` | evenness of characters per page | **the mean characters-per-page over the document's declared `page_count`** (a coefficient of variation is already normalised, which is what makes a 12-page paper comparable to a 900-page book). The vector is padded with zeros up to `page_count`, so a page that carried no text counts as the zero it is — otherwise a long scan with text on a handful of pages measures perfectly even, and the same document measures grossly uneven if its extractor happened to emit empty rows for the blank pages. `null` unless the document has a `page_count` **and** at least one page carries text | `chars_per_page_cv_max = 1.5` |

The defaults are roughly an order of magnitude away from what ordinary prose measures, on
purpose: a WARN an operator learns to ignore is worse than no check at all.

**A measure is never a verdict.** A glossary is legitimately terminator-poor and a
table-heavy appendix legitimately glued-token-rich. That is why the doctor check is
warn-only and why refusal is opt-in. And a document with **no** extracted text is
`measurable: false`, never suspect: "not normalized yet" is a statement the pipeline's own
stage chain already makes.

**And a document too small for a rate to mean anything is never suspect either.** Three of
the four measures are rates or densities over the document's own text, so below
`[ingest.quality] min_tokens` (default 200) they report arithmetic rather than extraction:
one long URL in a twelve-token note is a glued-token rate of 0.08, and a note with no full
stop is a terminator density of 0.0 — which is why the first day this check ran on a live
corpus its WARN was made of 8-to-31-token notes. Those documents are still measured and
still reported (`below_min_tokens` in `ingest quality --all` and in the doctor check's
details, and the check's message says how many there were); they are simply never counted
`suspect`. The floor is a property of the denominators, not a fifth threshold on quality,
so it suppresses the verdict rather than adding a measure — and it does **not** touch
`refuse_below`, which only ever does what the file literally says. `min_tokens = 0` turns
the floor off.

**Reading the numbers.**

```
trialerror ingest quality --doc-id <doc>              # one document, with the thresholds it was judged against
trialerror ingest quality --all --worst 10            # every document, worst first
trialerror ingest quality --all --sample 200 --seed 7 # a reproducible subset of a large corpus
trialerror doctor --only extraction_quality_suspect   # the sampled health signal
```

`--all` measures the whole corpus and reports `corpus_documents` beside `measured`, so a
sampled answer can never be mistaken for a complete one; `suspect_by_measure` says which of
the four bounds the corpus is failing, which is the difference between "the OCR route is
wrong" and "these are glossaries". The verb writes nothing — no job, no row, no config.
`--sample`, `--seed` and `--worst` all describe a corpus pass, so all three are **refused**
beside `--doc-id` rather than ignored (an ignored flag is an operator who believes they
asked for something), and `--sample` below 1 is refused for the same reason.

**The doctor check samples.** `extraction_quality_suspect` measures `[ingest.quality]
sample` documents (default 50) drawn under `[ingest.quality] seed`, because a full corpus
measurement on every `trialerror doctor` run would make the cheapest health command in the
system proportional to the corpus — paid every time anyone asks about anything. The sample
is seeded so two runs against an unchanged corpus name the same documents instead of
looking like a corpus that changes whenever it is read. `sample` is clamped to at least one
document, so no configured value can turn this check into the full scan it exists to avoid;
the exhaustive pass is a verb (`ingest quality --all`), never a threshold. It is **warn, never fail**, it
skips a corpus with no documents, and a document whose measurement itself raises is
reported in `details.unreadable` rather than turning into a failed check.

**Configuration.** All of it optional; a program that writes none of it measures against
the defaults above and refuses nothing.

```toml
[ingest.quality]
glued_token_rate_max = 0.10     # warn above this
unusable_chars_max = 200        # warn above this
terminator_density_min = 1.0    # warn below this
chars_per_page_cv_max = 1.5     # warn above this
min_tokens = 200                # below this a document is measured, reported, never suspect (0 = no floor)
worst_n = 10                    # how many worst-first rows a report carries
sample = 50                     # documents per doctor run
seed = 0                        # that sample's seed
```

**Refusal is opt-in, and absent by default.** `[ingest.quality] refuse_below` is the one
knob here that can stop a document's pipeline, so it only ever does what the file literally
says:

```toml
[ingest.quality]
refuse_below = { glued_token_rate_max = 0.35, unusable_chars_max = 5000 }
```

- **Only the measures named are compared.** A `refuse_below` that bounds the glued-token
  rate has not thereby bounded terminator density. An empty table reads as unconfigured,
  never as "refuse everything".
- When a document's text is worse than a stated bound, the **normalize/OCR stage** writes
  `document.status = 'failed'` (the only value the schema's CHECK allows for "this did not
  end in a usable state" — the same value `ingest retract` writes, for the same reason) and
  **does not enqueue the chunk stage**. Nothing downstream ever chunks, embeds, indexes or
  cites that text.
- The job itself settles `complete`, because the job did exactly what it was asked to. The
  **document's** status is the statement — which is why `pipeline_state` reads it ahead of
  every job row.
- The numbers, the reasons and the thresholds that produced them land in a `record` row
  under `register_key = 'ingest.quality_refusal'` (the same register, for the same reason,
  as the retraction ledger: no schema change in this lane), and an `ingest_quality_refused`
  event is appended. `ingest status --doc-id` reads them back.
- **The elements and the archived stream text survive** a refusal, on purpose: the operator
  has to be able to look at what was refused, and a refusal that destroyed its own evidence
  would be unarguable.
- **The refusal holds against the repair verbs too.** `ingest rechunk` and `ingest re-embed`
  on a refused document are refused (`DocumentQualityRefusedError`), exactly as they are on
  a retracted one: both re-derive, so either would walk the document back to `indexed` with
  the refusal still on file, and the refusal would hold only against the stage that wrote
  it.
- **Two ways out, both deliberate.** Relax (or drop) `refuse_below` and re-run the
  **extraction** stage — `normalize`/`ocr`/`djvu` re-measure the text and write
  `document.status` from what it now measures, so a document that now passes chains onward
  by itself; or fix the extraction route and re-ingest the raw file. Nothing else clears a
  refusal, and there is no separate flag to reset: the record stays as history, and
  `pipeline_state` stops reading it as the current state the moment `document.status` says
  something other than `failed`.
- A `trialerror.toml` this process cannot parse **fails the job** rather than defaulting to
  "no refusal configured" — the fail-closed read (D13). Defaulting is the one behaviour
  this must never have: a program whose operator configured a refusal would otherwise stop
  refusing the day a typo landed in an unrelated table. Because that read also enforces
  `[ingest] require_real_backends`, a program that declares it and configures no real
  backend now fails at the **normalize** stage rather than further down the chain.
- **A bound that is not a number fails the job too**, for the same reason and in the same
  direction: `refuse_below = { terminator_density_min = "eight" }` used to fall back to the
  *warn* default and refuse real documents against 1.0. Every other threshold here falls
  back to its default when it cannot be read — a mistyped warn bound can only make a signal
  read against the default — but this is the one value that can stop an ingest, so it is
  never substituted. (A number written as a string, `"0.35"`, is still read as 0.35.)

## `ingest status` — what is happening, not only what was written

`trialerror ingest status --doc-id <doc>` used to report the document row and three counts,
which answers "what has been written" and not "why has this been at zero chunks for an
hour". It now carries two **additive** keys — every key it always had is unchanged and
still present, and both new ones degrade into an `{"error": ...}` value rather than
raising, because `status` is the command an operator runs precisely when something is
wrong.

**`quality`** — the four numbers for that document, `suspect`/`reasons`, and the thresholds
they were judged against (a bare `suspect: true` invites the reader to guess what it was
measured against, and the answer is a config file they may not have open).

**`pipeline`** — the stage chain, and one derived word for it. `job` has no `doc_id`
column, so the link is the one the pipeline already maintains: the payload's `doc_id`, with
the `JOB-ingest-<doc>` id convention as a **fallback**, and `match_kind` says per row which
of the two found it (`payload`, `job_id_prefix`, or both) — a chain that implied a foreign
key the ledger does not have would be worse than no chain.

`pipeline_state` cannot be read off `job.state`, because four of the six ledger states mean
something different depending on the columns beside them:

| `pipeline_state` | The ledger shape behind it | What to do |
|---|---|---|
| `queued` | a `pending` job with nothing beside it — an environmental defer whose `next_attempt_ts` has **passed** counts here, because nothing re-claims a job on its own — or, when nothing is outstanding and the document never reached `indexed`, no job at all (the `reason` says which) | `trialerror jobs start-worker`, or enqueue the stage yourself |
| `running` | `claimed`/`running`, with the worker named | wait; `jobs logs --job-id` shows progress |
| `parked` | `paused` (an operator's own hold), or `pending` with `failure_class = 'environmental'` and a `next_attempt_ts` **still in the future** (compared against now with the same rule the ledger's claim predicate uses) — typically a stage waiting for a result the GPU machine has not published yet | `jobs resume --job-id` for a hold; nothing for a defer, which clears when its condition does |
| `failed-will-retry` | `failed` with attempts left; the `reason` says whether the backoff is still pending, has elapsed, or was never recorded (in which case the job is claimable now) | read the cause, then wait for the retry or run a worker |
| `failed` | `abandoned` (or `failed` out of attempts) — **or** a document whose own status is `failed`: a quality refusal, or a retraction. A dead stage is reported ahead of a retryable sibling: a document that needs a human needs one whatever its healthier stages are about to do | the `reason` names which of the three, and `next_action` the command |
| `complete` | every stage settled and the document reached `indexed` | nothing |

`next_action` is a sentence and `next_argv` the same thing as argv, so the envelope's
`nextActions` entry is built from the command rather than by parsing prose. On a program
that offloads a stage to a GPU worker, each stage also carries an `offload` annotation
saying where that job's manifest currently sits (`pending`/`claimed`/`done`/`failed`); on a
single-machine program the key is absent rather than empty.

### `ingest add` — the route is checked before anything is written

`--media-type` names a **route key**, not an IANA media type: `md`, not `text/markdown`. The
accepted keys are `djvu`, `epub`, `html`, `image`, `md`, `pdf-scan`, `pdf-text`, and a value
outside them is refused with the list. It is refused **before the document row is inserted**
— the old order left a `registered` document with no job behind it, which doctor counted,
`ingest status` listed, and nothing would ever move. Omit the flag and the extension route
resolves it: `.md`/`.markdown` → `md`, `.html`/`.htm` → `html`, `.epub` → `epub`,
`.png`/`.jpg`/`.tif` → `image`, `.djvu` → `djvu`, and `.pdf` → `pdf-text` or `pdf-scan` by
its text layer.

### Full-text search does not wait for the GPU

The pipeline runs chunk → embed → index, so on a program whose embed stage is parked for a
GPU run — an offloaded stage waiting for the other machine, a queue held until the GPU
window — nothing ingested since had any full-text search at all: the text was in the store
and the one stage that puts it in `chunk_fts` was queued behind a stage that needs hardware.

The `chunk` handler therefore enqueues a **full-text-only** `index` job beside the `embed`
one, under `[ingest] fulltext_before_embed` (default on; see `docs/USER_SETUP.md`). It
writes `chunk_fts` and the tantivy index and touches no vector table at all — which is what
makes it safe before a single embedding exists. Two things it deliberately does NOT do:

* it does not advance `document.status`. A row reading `indexed` with no vector in the active
  model's table is exactly the lie the ingest doctor counts exist to prevent, and the status
  would otherwise run `chunked` → `indexed` → `embedded` → `indexed`, going backwards in the
  middle. The document stays `chunked` until `embed` moves it.
* it does not take the post-embed job's id. `ledger.enqueue` is create-only, so a shared id
  would swallow the hand-off that fills the vector table and a program would index its text
  and never its vectors with nothing raised. The full-text job is
  `JOB-ingest-<doc>-index-fulltext`; the vector one still carries the model key.

Doctor's `fulltext_index_stale` is the check that says whether the full-text side is
current — it reads the index, not the status — and `trialerror ingest reindex-fulltext`
remains the repair.

## OCR page-range chunking — a bound on the run, not a kill on the process

marker builds every high-resolution page image **up front**, so one invocation's memory grows
with `pages x page area`. Measured (C-0098): a single 540-page large-format scan committed about
58 GB on the GPU machine, next to a 4B embedding driver that wanted 19 GB of its own. Four scans
of 299–540 pages had to be paused at job level, and the machine was one allocation from the pager.

Nothing here watches a process's memory and shoots it. A kill would turn the failure into a
retry loop at exactly the size that provokes it, and this subsystem's first principle is that GPU
minutes are never recomputed. Instead the **run is sized so the peak never arrives**: the backend
reads the document's page tree, works out how many pages of raster fit a budget, and runs
`marker_single … --page_range A-B` once per range.

**The arithmetic, and the knobs that move it.**

| Knob | What it does | Default |
|---|---|---|
| `[ingest.ocr] max_range_pixels` | The pixels of page raster **one invocation** may hold. `0` (or any non-positive value) means no bound: one invocation for the document, which is the behaviour this replaces. | `64000000` |
| `[ingest.ocr] bounded_dpi` | The DPI the planner estimates a page's pixel area at. It is the harness's own arithmetic and is not passed to marker, so **it must be at least the DPI your stack really renders at**. marker asks its provider for the images of the *whole range* at `highres_image_dpi` before it recognises anything — marker 1.x defaults that to **192**, alongside a 96-DPI low-res pass — so the default here is the larger of the two. If you pass `--highres_image_dpi N` in `marker_extra_args`, the planner uses `max(bounded_dpi, N)` on its own: **raised, never lowered**. | `192` |
| `[ingest.ocr] page_range_numbering` | Which convention your marker release numbers `{N}` page markers by under the range flag — see "Which page is `{N}`?" below. `"absolute"`, `"relative"`, or `"auto"` to read it off the output. | `auto` |

A4 is 8.27 × 11.69 inches, so at 192 DPI a page is 1,586.7 × 2,245.3 = **3,562,596 pixels**. With
a 1.1 safety factor for the buffers a page costs while it is being recognised,
`64,000,000 / (3,562,596 × 1.1)` = **16 pages per range** — a 540-page A4 scan runs as **34
ranges**: 33 of 16 pages (528) and a 34th of 12.

In bytes: the budget is a pixel count, and a page image is 3 bytes per pixel (8-bit RGB), so
`64,000,000 × 3` = **192 MB**, of which the safety factor reserves about a tenth — roughly 175 MB
is page raster in flight. The raster is not the whole of the 58 GB, which is the point: the knob
bounds the thing that *scales with the document*, rather than trying to predict a total that
depends on the model.

**Read that translation as a lower bound on ONE component, never as the invocation's memory.**
`pixels × 3 bytes × safety` describes the page raster and nothing else — not the models, not the
CUDA context, not the intermediate tensors a page is recognised through. Measured against a real
run it under-states the process by **two orders of magnitude** (175 MB of raster against ~15 GB of
committed memory before a single page is read). Size the knob from a measurement, by the procedure
below; the arithmetic above only tells you which way the number moves.

> **The planning DPI was 96 until a scan proved it could not be.** A fourth large-format scan in
> the same run that found the numbering bug did not fail on page numbers at all: `marker_single`
> exited 1 with a Python `MemoryError` raised inside marker's own
> `build_document → provider.get_images(page_range, highres_image_dpi) → pypdfium2 →
> PIL.Image.frombytes`. marker renders **every page of the range** at its high-res DPI and holds
> them all at once. Page area goes as DPI *squared*, so a planner sized at 96 against a 192 render
> is four times too generous in pixels — the bound was not bounding the thing that failed. If your
> deployment lowered `--highres_image_dpi`, the planner still uses 192 unless you also lower
> `bounded_dpi`; that direction is safe (smaller ranges, more invocations) and is left to you.

Planning uses the **largest** page in the document and one length for all the ranges, because the
failure is a peak: a run holds every page image it has built, so the worst range decides whether
the machine pages. A book of 500 portrait pages and 3 fold-out plates is planned against the
plates.

**Sizing a large-format scan: every range is another model load.** A range is one
`marker_single` process, so a document planned into N ranges pays marker's model load N times.
That cost is worth stating next to the memory it buys, because the two move in opposite
directions and only you can price them.

Worked, on the shape that provoked all of this — a 23 × 33 inch plate. At 192 DPI that page is
4,416 × 6,336 = **27,979,776 pixels**, about 84 MB of raster on its own:

| `max_range_pixels` | pages per range (raster only) | a 300-page scan | a 540-page scan |
|---|---|---|---|
| `32000000` | 1 | 300 invocations | 540 invocations |
| `64000000` (default) | 2 | 150 invocations | 270 invocations |
| `256000000` | 8 | 38 invocations | 68 invocations |

(The same page counted only 6,994,944 px at the old 96-DPI planning default, where a 32 M px
budget appeared to fit **4** pages a range and a 300–540 page scan looked like 75–135
invocations. It was never four: marker was rendering at 192 and the plan was reading the wrong
pass.)

So a plate-sized scan at the default budget is hundreds of model loads. The three ways out are
all yours to choose: **raise `max_range_pixels`** and accept the memory (8 pages of that plate is
about 672 MB of raster in flight, plus whatever the model holds); **lower marker's own render
DPI** by passing `--highres_image_dpi` in `marker_extra_args`, which the planner picks up
automatically and which trades recognition quality for pages-per-range; or **leave it** and pay
the invocations. The worker's plan line and the job's result both record `planning_dpi` and which
setting fixed it, so whichever you pick is on the record rather than in somebody's memory.

**Sizing the budget from a measurement — one machine's numbers, and the procedure that gets you
your own.**

The figures below are **one machine's measurement**, taken on the first real chunked run:
marker-pdf 1.10.2 on a laptop with a **16 GB GPU**, large-format scans of roughly 1500 × 2300 pt
pages, ranges planned at 192 dpi. They are an example of the *shape* of the cost, not a constant
to copy — your models, your card and your pages move every one of them.

- **A fixed ~14.5–16 GB per `marker_single` invocation** — the models plus the CUDA context,
  committed before the first page is read and paid again on every range, because every range is a
  fresh process.
- **Plus ~0.22–0.25 GB per page** in the range, which is the part `max_range_pixels` actually
  bounds.
- Whole-machine peak commit while those ranges ran: **34–35 GB** on 16-page ranges and **32.6 GB**
  on 8-page ranges — the ~2 GB between them is eight pages at the per-page rate, and everything
  else on that machine is in both numbers.
- Throughput **~7–8 pages a minute**, model reload included.

So the procedure, in four steps:

1. **Run one small range** of the document you are actually sizing for (a two- or four-page
   `max_range_pixels`), and let it finish.
2. **Read the peak commit** — the worker logs `range_wall_s` and, where the platform gives it
   cheaply, `peak_rss_bytes` for each range, and both ride on the job's result manifest beside the
   range's hashes. Take the fixed cost from a range of one or two pages and the per-page slope
   from the difference between two range sizes.
3. **Derive pages per range**: `pages = (budget − fixed) / per-page`, where `budget` is the memory
   you are willing to let one invocation hold.
4. **Set the knob**: `max_range_pixels = pages × page_pixels_at_planning_dpi`, with
   `page_pixels_at_planning_dpi` the largest page's area at `bounded_dpi` (the arithmetic at the
   top of this section). The planner then re-derives the same page count from the pixels.

**Below about 30 pages a range the fixed cost dominates**, and that changes which way to tune. At
~15 GB fixed and ~0.25 GB a page, a 30-page range spends ~7.5 GB on pages against ~15 GB it would
have spent anyway: halving the range saves under 4 GB and doubles the number of model loads. Very
small ranges buy very little memory and cost a great deal of wall clock — at 7–8 pages a minute
with a reload in every range, that is the trade to check before shrinking the budget again. If a
document will not fit even at one page a range, the fixed cost is the problem and the fix is a
smaller model or a bigger card, not a smaller budget.

**What each range actually cost, on the record.** The worker logs one line per range while the
job runs, and the same numbers ride on the job's result manifest in the per-range list beside
each range's `sha256`:

| Field | What it is |
|---|---|
| `range_wall_s` | Wall clock for the range, always recorded |
| `cached` | Whether the range came out of the resume cache instead of an invocation. A resumed range comes back in milliseconds, which would otherwise read as an impossibly cheap marker run |
| `peak_rss_bytes` | Peak resident memory of the `marker_single` child — **where the platform gives it cheaply**, `null` where it does not |
| `peak_rss_source` | Which reading that was, and it must be read before two of them are compared |

On POSIX the source is `getrusage(RUSAGE_CHILDREN).ru_maxrss`, which is free and **cumulative**:
it is the high-water mark over every child this worker has reaped since it started, not this
range's own peak. It therefore only rises, and a later cheaper range reports the earlier
expensive one's number. Use it as "the most any range has cost so far", which is exactly what
sizing a budget needs, and never subtract two of them. On Windows the number is skipped unless
`psutil` is already importable, in which case it is a sampled peak of the invocation's own
children — **no dependency is added for a diagnostic**. Nothing in the pipeline reads any of
these fields; they exist so the sizing procedure above can be carried out from a queue on
another machine.

**What it costs, and the refusals that keep it honest.**

- A document that plans to **one** range is run with the unchunked command, byte for byte what the
  backend always sent, and never probes anything. A small document is never refused over a flag it
  was not going to use.
- The flag's spelling is `[ingest.ocr] page_range_flag` (default `--page_range`) and is **probed
  once against `marker_single --help`** before the first chunked run. If it is absent the job
  fails by name rather than trying: a range flag that is silently ignored turns each of N ranges
  into a full-document run — the memory failure, N times over.
- A concatenation that is not **strictly increasing** is refused: two ranges produced the same
  page, or produced them out of order. A **gap** is legal — marker emits no body for a blank page
  and the backend drops it, as it always has.
- The queue side declares `expect.page_count` from the `document.page_count` column **when that
  column is populated**, and the worker refuses to publish pages when the page tree it reads
  disagrees. Two different counts mean the bytes on the GPU machine are not the bytes the record
  is about. Read the scope literally: nothing counts pages at registration, and today the only
  route that fills that column is the DjVu → derived-PDF conversion, so for a PDF acquired
  directly the field is `null` and **this check does not fire**. Absence is not disagreement — a
  document with no recorded count is published on the page numbers the OCR produced, exactly as
  it was before this lane. If you want the check on a particular document, its page count has to
  be on the row before the job is queued.

**Which page is `{N}`? — `[ingest.ocr] page_range_numbering`.** `--paginate_output` writes a
`{N}` marker above each page, and *what N counts* is a fact about your marker release, not about
this harness. **marker-pdf 1.10.x numbers by the page's own index in the document** (`absolute`);
other releases number from the start of the invocation (`relative`), which is what the range flag
alone might lead you to expect. The first real chunked run here refused eight claims in a row
because the backend assumed `relative` and the installed release was `absolute` — range 0 reads
the same either way, which is why each attempt ran exactly one range before failing.

`auto` (the default) works it out from the output, **once per document, from evidence, never by
guessing**:

- for a range `first-last` of `count` pages, a marker `N` is *relative-consistent* if
  `0 ≤ N < count` and *absolute-consistent* if `first ≤ N ≤ last`;
- a range whose markers are consistent with exactly one convention has **decided** it, and that is
  the document's convention. The first range never decides — its two windows are the same window;
- a range whose markers fit both (possible when a range is longer than its own first page number)
  is **undecided** and takes the convention an earlier or a later range established. Because the
  planner uses equal-sized ranges, every range after the first always decides;
- a marker that fits **neither** window, one range proving **both** conventions at once (what a
  release numbering from 1 rather than 0 looks like), two ranges proving **different**
  conventions, or a document no range of which can decide, are all refused **by name**. Nothing is
  concatenated and nothing is guessed.

Set `page_range_numbering = "absolute"` or `"relative"` to state it yourself; the other
convention's output is then refused with the window it violated. The convention that was used,
and what settled it, are in the job's result (`page_range_numbering`,
`page_range_numbering_decided_by`) and in the worker's log.

A resume is safe across all of this: the cache holds each range's **raw marker text**, so the
convention is re-derived over cached and fresh ranges together at concatenation time. Ranges
produced by two releases that disagree are the refusal above, never a silent mix.

**Stopping and resuming.** The cooperative checkpoint falls between ranges, so
`trialerror offload worker-control --worker-id <id> --pause|--stop` lands at a range boundary. A
stop there hands the claim back with nothing published, and the ranges already produced stay in
`<work_root>/_ranges/<job_id>` — the next claim of that job re-runs only the ranges that were
never produced. That directory sits beside the job directories rather than inside one on purpose:
a job directory is wiped at every claim and swept after a stop, which is right for a partial
result nobody may publish and exactly wrong for GPU hours a resume reuses. It is removed when the
document finishes; a job stopped and never resumed keeps its cache, which is the state it exists
for. The cache is keyed to the input's sha256, to the plan and to the arguments the invocation
carries, so a changed document, a changed budget or a changed `marker_extra_args` starts over
instead of concatenating ranges produced by two different commands.

Inside it: one `range-<first>-<last>.md` per finished range, a `.sha256` beside each one, and the
`plan.json` holding that key. Both guards are there because a range file that is only *partly* on
disk reads back as a shorter range, and short pages are indistinguishable from blank ones — a gap,
which the concatenation legitimately allows, so nothing downstream would refuse it. So the write
is atomic (a killed writer leaves the previous state, never a half file) and every read re-hashes
what it found. A range whose digest is missing or disagrees is **not in the cache**: it is run
again. If you are ever inspecting one of these directories by hand, that is the rule — a `.md`
you edit is a `.md` the next resume throws away.

The JOBS card shows `units_done / units_total ranges`; the plan itself — how many ranges, of how
many pages, over how many — is a line in the worker's own log. It is not on the card on purpose:
the heartbeat payload's `settings` block is an allowlist the queue-host wrapper shares, and a key
one side invents is refused on arrival, after which the worker beats *bare* for the whole job — a
live claim with no detail at all, and nothing failing to say so. A new number there costs a
wrapper rollout, and this one is not worth one.

**Rolling it out on a two-machine split.** The queue-host wrapper is **unchanged** — no new verb,
no new payload key, no redeployment on the box that holds the queue. What changes is the worker:
on the GPU machine, pull the harness and restart `trialerror offload worker`. The knobs live in
that machine's own `[ingest.ocr]` table, so nothing on the queue host has to know they exist, and
a worker that has not been restarted keeps running documents in one invocation exactly as before.
Paused large scans are resumed from the queue side with `trialerror jobs resume --job-id <id>`
once the restarted worker is up.

## The duplicate-candidate gate — three routes, a coverage bar, and the verb that goes back

A duplicate candidate is a `term_relation(verb='same_as', status='pending',
marked_by_kind='system')` row: a machine saying "these two names might be one thing".
Nothing it opens is a decision, so the scan is free to be noisy — but only up to the point
where the queue is a queue a person will start. The first real register import opened
**33,785** of them, mean fan-out 4.9 against a top-5 cap, which is a cap that has stopped
selecting and started counting.

**Two stages.** The first is the trigram index (`term_fts`, `tokenize='trigram'`) ranked by
`bm25`, top-5 per term, kept when the score passes `[lexicon] duplicate_bm25_floor`. The
second is the gate, and it surfaces a hit only when one of **three routes** holds:

| Route | What it asks |
|---|---|
| `informative_token` | do the two **names** share a whole word that fewer than `[lexicon] duplicate_informative_token_fraction` of this store's terms carry (and that is not a function word) — **and** do the shared words cover at least `[lexicon] duplicate_coverage_min` of the *shorter* name's informative tokens? |
| `name_in_text` | is one side's **whole** name written, as a phrase on word boundaries, inside the other side's indexed text (names plus current glosses) — the contained name carrying at least one informative token of its own, unless `[lexicon] name_in_text_requires_informative = false`? |
| `similarity` | does the pair's whole-name trigram similarity reach `[lexicon] duplicate_similarity_floor`? |

**The coverage bar is applied AFTER the frequency test, never instead of it.** The frequency
test says which shared words count at all; coverage then asks how much of a name those words
account for. One rare word shared between a two-word name and a nine-word one is a
coincidence with a rare word in it; the same word shared between two two-word names is half
of each of them. The denominator is the **shorter** side — the only name a single shared word
can plausibly be most of — and the comparison is `>=`, so a two-word name sharing exactly one
of its two informative words passes at exactly 0.5.

**Both keys are a tightening, and every existing configuration gets them.** A program that
names neither key behaves as it did *plus* the new rule: the gate now surfaces a subset of
what it surfaced before, never a superset. Set `duplicate_coverage_min = 0.0` and
`name_in_text_requires_informative = false` and you have the previous behaviour exactly —
which is the point of them being config rather than a rewrite: the change is auditable
against what it replaced, on your own store, without a second build.

**The rescan is how a calibration change reaches a queue opened before it.**

```bash
# read first -- writes nothing, and reports the knobs that were in force
trialerror term scan --rescan --dry-run --by-launch <LNCH-...>
# then the real one
trialerror term scan --rescan --by-launch <LNCH-...>
```

It re-asks the whole gate of every **pending, `same_as`, term-to-term row the scan itself
opened and nobody has touched**, and withdraws the ones the gate would no longer open:
`status` goes to `rejected` (the state a later scan already refuses to reopen, which is what
makes this idempotent) with a reason naming the route that failed, and a
`term_candidate_withdrawn` event beside each one carrying `status: "withdrawn"` — a rejection
is a judgment about two names, a withdrawal is the scan saying it should not have asked.

What it will not touch: anything **human-touched** (`marked_by_kind` other than `system`, or
a `decided_ts`/`decided_by_launch` on the row) — a machine may take back its own guess and
not a person's attention; anything not `pending`; an **exact key collision** (`shared_key`),
which is an ambiguity `find_term` still has to resolve and no gate applies to; and a row
whose `evidence` JSON cannot be read, which is reported under `skipped`.

The summary reports `examined`, `withdrawn_count`, **`withdrawn_by_coverage`** (how much of
the withdrawal the new bar is responsible for — the total alone mixes it in with every pair
the gate would have withdrawn anyway), `kept`, `kept_certainties`, `skipped_count`, and the
values actually in force: `informative_df_threshold`, `informative_token_fraction`,
`term_count`, `similarity_floor`, `coverage_min` and `name_in_text_requires_informative`. A
knob quoted on its own cannot tell you whether the one you turned was read — an operator
sweep of the fraction once moved the withdrawal count by zero on every setting because the
CLI was not passing the program's config in at all.

`--by-launch` is required: withdrawing is an act somebody ran, and an unattributed one is the
machine deciding on its own.

## Ideation rounds — what a plant tests, and what a judge sees

The novelty screen's labels are only worth what the audit behind them is worth, and the
audit is the **plant battery**. Everything in this section is about keeping that battery
honest and keeping the record schema readable by the things that read it.

### The instrument — what a round declares

The judged screen was one instrument with no knobs: the judge saw the nearest inventory
rows and the corpus passages, labelled them from two fixed vocabularies, and the plants were
built by the harness out of inventory rows and the batch's own records. A round that judges a
**literature** rather than a mechanic needs a different instrument, and it declares one. **The
default is exactly what was hardwired**, so a round that declares nothing gets the screen it
always got.

Four declarations, each a flag with a `[lens.novelty]` config row behind it (the flag wins;
see `docs/USER_SETUP.md` for the config table):

| Declaration | Flag | What changes |
|---|---|---|
| Reference sets | `--judged-sets R2,R4` | Which sets the judge is shown and labels |
| Label vocabulary | `--labels-file FILE` | What the round calls its labels, and what they map onto |
| Plants | `--plants-file FILE` | The plants the round seeds itself |
| Failing kinds | `--batch-fail-on KIND[,KIND]` | Whose misses fail the batch |

**Declared reference sets.** `R2` is the archive of idea rows, `R3` the inventory, `R4` the
corpus. R5 is evidence for the R4 label, not a labelled set of its own, and rides inside that
bundle — there is nothing to declare for it. An **undeclared set is absent from the envelope,
not empty**: `inventory_rows: []` reads to a judge, and to every later reader of the batch
file, as "the register was consulted and held nothing near this", which is a different
sentence from "this round did not judge against the register". One verdict row per idea per
**declared** set; a label for an undeclared set is refused, because the judge was never shown
that set's rows. The declaration is recorded on the batch, and **a run that RECORDS against a batch reads
the declaration off that batch**: `--record-verdicts` and `--record-calibration` take the
batch's own `judged_sets` (honouring `--batch-id`) before the labels file is resolved, so
recording against an R2,R4 batch needs no `--judged-sets` at all. That is not a convenience.
The labels file's hash is computed over the declared sets, so a file matching the batch
exactly used to be refused twice over under the CLI's default declaration — once for
carrying "a block for a set this round did not declare", and again on the hash — and the only
way through was to restate on every recording command what the batch file already said. A
`--judged-sets` (or a `[lens.novelty]` row) that DISAGREES with the batch still refuses
(`judged_sets_disagree`), naming both declarations: a round that thinks it is recording R3
answers against an R2 batch has a problem no default can fix.

The resolution is the **invocation's**, not the phase's. One `lens screen` may both prep and
record; when it does, and when nothing declares the sets, the batch being recorded against
also supplies the sets the new batch is built under — one invocation carries one declaration
rather than mixing two. Declare `--judged-sets` (or the `[lens.novelty]` row) if you want a
prep to depart from the batch you are recording against, and run the two phases separately:
a declaration that disagrees with the batch is refused, never silently overridden.

An R2 envelope carries `archive_rows` — `idea_id`, `round_id`, `status`, `statement`,
`similarity` — built by the same vector path the inventory rows use. A record is never handed
its own row, and an R2 verdict cites those rows in a note carrying the stance rather than by
`chunk_id`: an idea row is not a chunk, and a resolver handed one would report a dangling
anchor.

**Per-round label vocabularies.** `--labels-file` gives the round its own words and a
canonical mapping onto the design's fixed vocabulary. The judge is shown the round's
spellings and no others (a judge handed both is free to answer in either, and a verdict row
is not the place to discover which); each verdict row stores `label` in the round's words and
`label_canonical` in the design's, in the **same composite shape**, so a reader that parses
one can be pointed at the other and keep working. **The mapping must be total over the
round's labels** and must land inside the design's own vocabulary for that set — an unmapped
label would sit in a row and be absent from every report of it. The file is hashed onto the
batch; recording against a different hash is refused. A round that declares no vocabulary
writes none onto its batch, and its rows carry `label_canonical` equal to `label`.

**The harness battery answers in the round's words too.** A harness plant is a row the judge
is handed verbatim, so the design's answer for it is `same` or `variant` — and a round that
re-spells the set the battery is scored against (the inventory, or the first declared set
when the inventory is not declared) offers its judge neither word. The expectation is
therefore translated through the round's canonical mapping when the plants are built: every
one of the round's own labels that maps onto `same` or `variant` catches the plant, and
nothing else does. A round whose vocabulary for that set maps onto neither is refused at
`--judged-prep` (`plant_labels_unexpressible`): that battery could not be caught by any
answer its judge is able to give, so the batch would fail its own audit on a judge that did
the task. Either map one of the round's labels onto `same` or `variant`, or seed the round's
own plants with `--plants-file` and `--plants 0`. Scoring accepts both spellings of a label
in either direction, so a batch built before a labels file was introduced still scores.

**The plants file.** A JSON list of plants the round defines:

| Field | What it is |
|---|---|
| `plant_id` | required — how the labels are scored back to it; must not collide with the harness's `PLANT-<kind>-<n>` |
| `kind` | required — `area` (a rewrite of a reference row that is not a register mechanic), `paraphrase`, `inventory` or `custom` |
| `statement` | required — the planted text |
| `expected_labels` | required — `{"R2": ["requested"], "R4": [...]}`, per reference set; every set must be one the round declared and every label one the round's judge is offered |
| `donor_ref`, `source_ref` | optional — the record whose fields it borrows, and the row it was cut from (forced into its own bundle) |
| `requirements`, `home_mechanic`, `probe` | optional — declared here, or borrowed from a donor |
| `extra` | optional — **any keys the round wants** (`literature`, `unlock`, `seeds`, …), rendered into ONE envelope field, `record.extra_text`, in sorted key order with empty values dropped |

They are shuffled in through the same envelope builder as everything else, wear a donor
record's missing fields, and carry masked ids exactly like the harness's own — a plant whose
`requirements` and `probe` were two fixed strings is one a judge picks out on the shape of its
fields rather than on its content. Pair the file with `--plants 0` to seed **only** the
round's plants.

`extra` is the one lenient key, and it is lenient on purpose: a round writes this file by
hand, and the three fields a live round wanted on every plant (`literature`, `unlock`,
`seeds`) were the same three its own records carry. They go under `extra`, which takes any
keys at all and reaches the judge as a single `record.extra_text` block — one field rather
than one per key, because the envelope's SHAPE is what keeps a plant indistinguishable from
a record, and the key is present on every envelope (`null` where there is none) for the same
reason. A round that puts text there on its plants and none on its records has made the
values a tell, which is the round's decision and not something a shape can prevent. A
top-level key that is not in the table above is still refused, so a misspelt `statement` is
not quietly accepted as a note.

**Which misses fail.** `--batch-fail-on` (default `inventory`, the rule the design stakes a
batch on) names the kinds whose misses fail it. A miss on any other kind is reported and
counted, never hidden. The score carries `failures` (the list the batch turns on) beside
`inventory_failures` (inventory misses, still meaning exactly that), plus `by_kind` and
`by_set` catch rates. A batch carrying no plant of a failing kind is `unauditable` and fails
on that ground alone, and `--judged-prep` refuses to build one.

### The archive round

`trialerror lens intake --round-id <archive> --records FILE --author-launch L --status archived`
writes prior rounds' candidates and request rows as `idea` rows so a round can be judged
against them as R2. `--status` is a **default**: a record that names its own keeps it, because
the file is the more specific statement.

An `archived` row is in the reference set and is not a candidate, and three rules follow:

- **It is never consolidated**, under two locks — the mechanical screen batches `raw` rows
  only, so an archived row never reaches a judged scope, and consolidation refuses any status
  but `raw` even if it did.
- **The near-duplicate merge never folds across it, in either direction.** Folding a live
  record into an archived one would retire this round's record into an archive entry nobody
  reviewed; folding an archived row into a live one would rewrite the archive from the round
  it is the fixed background for.
- **A live record that lands on an archived row is flagged, not folded**: `archive_hit` on the
  dossier (the archived row's id, round and cosine) and `archive_hits` on the batch record. A
  flag, not a fold, for exactly the reason the KNOWN-MECHANIC flag is a flag. An empty
  `archive_hit` means "compared **within its home cell**, nothing close" — the merge buckets
  by home cell before it compares anything, so an archived row in a different cell is never
  compared and never flagged, however near it is. The judge's own R2 bundle is what covers
  that: `archive_rows` is retrieved by vector across the whole archive, cell or no cell.

**The archive is embedded once per statement per model.** R2's rows are records, not
chunks, so no `vec_chunks` table holds them, and every `--judged-prep` and `--calibration`
used to hand every `idea` row to the embedding backend — twice, since the plant battery
reads R2 and so does the batch build. On a live programme that was 76 rows and minutes per
build, growing with the archive for the rest of the programme's life. The vectors now live
in `vec_ideas` (knowledge schema v10), keyed by `idea_id` and `model_key` and carrying the
SHA-256 of the exact text that was embedded: a row whose statement changed hashes
differently and is re-embedded, a row written since the last build is embedded for the first
time, and a run under a different embedding model misses every row rather than mixing two
vector spaces in one ranking. The mechanical screen fills the cache it will later read.
`--reembed-archive` rebuilds the lot, for the one case the key cannot see — a backend whose
weights or pooling changed under an unchanged model key.

### Calibration — measuring the instrument before the round

The chicken-and-egg calibration closes: the plant battery is what makes a round's labels
trustworthy, so the first batch a programme ever judged was also the first test of whether its
judges could judge — and nobody knew what a cosine of 0.61 meant in that corpus until after
they had acted on one.

```bash
# 1. a batch of PLANTS ONLY -- no records needed, nothing consolidated
trialerror lens screen --round-id R --calibration --seed S --plants-file plants.json \
    --judge-envelopes-out out/

# 2. two judges label it; score both and write the card
trialerror lens screen --round-id R --record-calibration \
    --judge-sheet-a a.json --judge-sheet-b b.json --pair-ratings pairs.json \
    --launch-id LNCH-... [--judge-launch-a ... --judge-launch-b ...] \
    [--prereg-id PREREG-... --executed-procedure-file procedure.md --executed-params '{...}']
```

The `--prereg-*` trio is the same one `--record-verdicts` takes and is stamped the same way —
see "`prereg_compliant` — pass the procedure byte-exact" below. Pass `--prereg-id` alone and
the card and every row it writes say `not stamped` rather than claiming compliance nobody
checked.

It writes `judged/calibration-<n>.json` (masked judge views and all) and
`judged/calibration-<n>-card.json`. The card keeps four things apart:

| Field | What it answers |
|---|---|
| `catch_by_kind`, `catch_by_set` | Do the judges catch what they were handed? Per judge, from the same scorer the real batches use |
| `kappa_by_set` | Do they agree with each other? Cohen's κ per declared set over the plants **both** labelled, with `n` and the observed agreement beside it — never the number alone, because a κ over four subjects is a number and not a finding |
| `r_embedding_human` | Does the embedding agree with a person? Pearson *r* between each rated pair's cosine and its human rating, from `--pair-ratings` (`[{"a": id, "b": id, "human": 0..1}]`). Absent rather than assumed when nobody rated anything, and `None` with a `note` when it would not mean a number (fewer than three pairs, or no variance in either series) |
| `baseline_cosine_distribution` | What does a cosine mean here? Each subject's nearest corpus-neighbour cosine with its 50/90/95th percentiles. **`over` says which population**: `plants` in calibration mode (a calibration exists for the moment before a round has a record, and "over the records, n 0" is a baseline over nothing on the one card whose job is to answer this), `records` in a round. `n` beside `n_with_neighbour`, because the corpus bundle is thresholded and a distribution over the subjects that HAD a neighbour is a distribution over what retrieval already liked; `n_records` carries the same number under the name it had when there was only one population |
| `confusion` | Which label pair did they read differently? Per declared set, a dense label × label matrix of judge A against judge B — every cell present, zeros included, in the labels file's own order, with the round's `unscreenable` word among them (two judges that both answer it AGREE). A subject exactly ONE judge labelled is counted in `n_unpaired`, listed in `unpaired`, and excluded from the matrix; one NEITHER labelled is absent rather than unpaired, and the catch tables already report it. `n_paired` is the same `n` the κ was computed over |
| `disagreements` | Per declared set, every paired subject the two answered differently, with both labels — under the id the **judge** saw (`subject_id`) as well as the real one, because that is the envelope whoever re-reads the item will go looking for |
| `by_expected` | Per judge, grouped by what each plant EXPECTED rather than by its kind: `n`, `caught`, and the full histogram of what was actually answered per declared set (`null` under `(unlabelled)`). `catch_by_kind` answers a question about the battery's construction; this one answers "when the right answer was `absent`, what did this judge say", which is the thing a round changes its instructions over |
| `by_class` | The same table grouped by the plants' own free `class` tag. Empty — not one group called "everything" — when the plants file declares no classes |
| `warnings` | What this call accepted and did not read — today, extra keys on a `--pair-ratings` entry, and a `--batch-id` no plant's `batch` matches. Empty on a card where nothing was ignored |
| `prereg_id`, `prereg_compliant`, `prereg_compliance` | Was this the instrument that was pre-registered? The same three keys `--record-verdicts` reports, stamped by the same recomputation, and carried onto every verdict row the calibration writes. `prereg_compliance_detail` holds both sides' hashes when there was something to compare |

**`--pair-ratings` is lenient about extra keys.** A person writes that file, so a `pair_id`
or a `why` beside `a`/`b`/`human` is a note and not a mistake: it is ignored, and NAMED in
the card's `warnings` — named because a misspelt `human` is also an extra key, and silence
would drop a rating without saying so. A pair naming an id that is neither a plant in the
batch nor an `idea` row is still a refusal, because a correlation over the pairs that
happened to resolve is a different statistic from the one the round asked for.

`misses_by_id` names who missed what. Every row is stamped
`procedure_version=novelty-v2-calibration`: a calibration's labels are about plants, nothing
is consolidated on them, a later count of the round's own labels must not sweep them in, and
the one-submission rule must not read a calibration as the round's first submission. A
calibration with no `--seed`, no plants file, or only one judge's sheet refuses by name.

**One submission per calibration batch**, the round's own rule applied to the batch's own
rows: recording the same calibration twice is refused unless `--supersede` is passed, and the
rows it then writes name the rows they replace (`superseded` on the card). The card file is
rewritten in place either way; the verdict rows are what a second call would otherwise
double, with nothing joining the two sets.

**The rule is scoped to the round and the batch** — for a CALIBRATION. A `plant_id` is
whatever your plants file called it, so two rounds may legitimately both have a `C-1`, and
two batteries of one round may too — and a supplement battery used to be refused because an
earlier one happened to reuse twelve names, with `--supersede` the only way through and that
flag would have marked the EARLIER round's rows as replaced. `--record-calibration`'s lookup
is keyed by `(round_id, batch_id, subject_id, reference_set, procedure_version)`
(`verdict.round_id`/`batch_id`, knowledge schema v12): a verdict of another round
never blocks and is never superseded, `--supersede` replaces only rows of the same round
and batch, and the refusal names the round and batch of the rows it found. A row written before that
migration carries no round; it is read as UNKNOWN rather than "no round", so it still blocks
— scoping a guard must not loosen it.

**`--record-verdicts` is scoped to the round and NOT to the batch**, and the difference is
not a detail. Its subjects are `idea_id`s, which are unique across the programme, so the
plant-id collision above cannot happen to them — while a round that preps two judged batches
(see `--batch-id` under the plants file) would, with the batch in the key, be able to record
two contradicting labels for one idea with no refusal, no `--supersede` and nothing joining
the rows. That is design 5.2(3), *one submission per idea per judge*, undone by a key. A
second batch of the same round re-offering an already-recorded idea is therefore refused, and
the refusal names the batch(es) the prior rows came from so that "this idea was already
answered in `judged-0`" is readable without a SQL query. `--supersede` still gets it through
and still names the rows it replaces; another round is still invisible.

Unlike `--judged-prep`, a calibration that seeds no failing kind is **not** refused, and the
difference is the point: a judged batch that cannot fail its audit consolidates records on an
unchecked judge, while a calibration that catches nothing has measured a judge that catches
nothing — which is the finding.

### Reading a round's baseline — `lens screen --baseline`

A round that wants a threshold of the form *"the 90th percentile of the earlier round's
record→corpus nearest-neighbour cosine"* could not get that number out of the harness. The
calibration card's own `baseline_cosine_distribution` is over PLANTS in calibration mode,
and it is cut from bundles that already cleared the candidate-hit floor — so its percentiles
are over the subjects retrieval already liked — and an archived round could not be screened
at all. The number had to be computed by an outside script.

```bash
trialerror lens screen --round-id EARLIER --baseline --corpus-mode vector \
    [--status archived] [--where provenance.set_id=lens-1]
```

**Read-only.** It writes no verdict, dossier, idea row, batch file or event. The one
exception is stated rather than hidden: a statement not already in the per-model idea-vector
cache is embedded and cached, exactly as a screen would have cached it, so a second read of
an archived round embeds nothing.

It returns `n_records`, `n_with_neighbour`, `min`/`p50`/`p90`/`p95`/`max`, a `per_record`
list of `{idea_id, nearest_chunk_id, nearest_doc_id, cosine}`, and the `reference_snapshot`.
There is **no similarity floor**: `min` here is a real nearest neighbour, not the smallest
one that cleared a bar.

`percentile_method` is in the output and reads `index-round(p*(n-1))` — the value AT a rank,
never an interpolation between two. The identical threshold under the linear convention is a
different number and a reader cannot tell which one they are holding by looking. The
calibration card's own baseline now says the same thing on itself.

**The name spells the formula out rather than borrowing "nearest-rank", and the difference
is not pedantry.** The textbook nearest-rank percentile is the value at rank `ceil(p·n)`;
this one is the value at index `round(p·(n−1))`, and the two disagree for many small `n` —
at *n*=6, *p*=0.9 this reports the 5th value and nearest-rank the 6th; at *n*=20, *p*=0.5,
the 11th against the 10th. Because `round` is banker's rounding the gap is irregular rather
than a constant offset, so a reader who recomputed a threshold under the borrowed name would
get a number wrong in a way no rule recovers. This field exists so that "the 90th percentile
of the earlier round's distribution" means one thing.

`--status` restricts to records in one state (`archived` for a closed round). `--where
provenance.<key>=<value>` keeps only records whose resolved field equals the value, ANDed
over repeats. The `provenance.` spelling is required and is not decoration: a key resolves
as a schema column first and a `provenance`-JSON key second, so a round's own bookkeeping
(`set_id`, `arm`) filters the same way as an AIIF field that did become a column, and a bare
column name would promise something narrower. A key or value nothing carries selects nothing
rather than everything. `--corpus-mode vector` scans the whole vector table for the true
nearest neighbour; any other mode gives the nearest of what that tier proposed, and the
output records which was asked.

### Evaluating a rule-defined pick — `lens slice-distances`

A round that pre-registered *"the slice document farthest from the lens's home medoid (the
home medoid nearest to the slice), ties to the lower id"* had to compute it with an outside
script that read `lens_assignment.slice_spec`, pooled document vectors and cosined them by
hand. A pre-registered rule whose answer comes from a script nobody else has is a rule the
round cannot reproduce.

```bash
trialerror lens slice-distances --round-id R --home DOC-a,DOC-b --model-key qwen3-4b \
    [--lens lens-1 --lens lens-2]
```

**Read-only.** Per lens: `slice_doc_ids`, `home_mean_distance` (each home's MEAN cosine
distance to the slice), `nearest_home` (ties → the lower id),
`distance_to_nearest_home` per slice document, and `farthest` (ties → the lower id). The
metric is the cosine distance between doc-pooled vectors — the same one `stratify` cut the
arms with, so a rule expressed over "distance" means here what it meant there.

A document with no vector under that `model_key` is listed in `unvectorized` and takes no
part in any mean; silently dropping it would move a medoid without saying so.

`canonical_sha256` is SHA-256 over the canonical JSON of `{lens: {home, farthest}}` (sorted
keys, `,`/`:` separators, UTF-8), so two runs — or a run and an outside script — are compared
by one value instead of by reading two tables side by side. `--home` takes repetition or one
comma-separated list; the answer, and the hash, are identical either way.

### Seed work behind an `unscreenable`

A judge that answers `unscreenable` has resolved work before saying so. A label sheet may
carry it, per subject:

```json
{"IDEA-...": {"label_inventory": "unscreenable",
              "seeds": [{"ref": "DOC-a#3", "label": "on-topic"},
                        {"ref": "DOC-c#1", "label": "off-topic"}]}}
```

Seeds are **evidence, never a reference set** — they say whether the work was on topic, which
is a fact about the judging rather than a claim about a set, so there is no `label_seed` and no
R-number for them. Each lands as an evidence note on every verdict row for that subject, with
a summary note beside it, because a row is read on its own. The result's `seeds` block reports
the count per subject and `unscreenable_below_bar`: the subjects called unscreenable (in the
round's own word for it) with fewer than two on-topic seeds behind them, including the ones
with no seed work recorded at all. **Reported, never refused** — the rule is the round's own
pre-registered text and this is the number a reader checks it against.

**`unscreenable` is an answer for EVERY declared set**, including one whose own `labels`
list never offers it. The word is a statement about the RECORD — it states no mechanism to
compare with anything — not about a reference set, so a judge that answers it against the
archive has judged. Two judges on a live calibration did exactly that, against a re-spelled
R2 whose list did not repeat the word, and both answers had to be passed as *unlabelled*:
"there is nothing here to compare" recorded as "the judge said nothing", which is counted
differently everywhere downstream. It is now accepted for any declared set, scored as a
**non-catch** for a plant (a plant is a row that exists, so `unscreenable` is wrong about
it), counted in κ as its own category, and written with `label_canonical = unscreenable`
however the round spells it. What the judge is SHOWN does not change: each set's `labels`
block is still its own vocabulary, so no round's envelopes moved.

A round that re-spells `unscreenable` in its labels file still has to offer the judge that
spelling — list it in the `labels` of the set it belongs to, mapped onto `unscreenable` —
because the word is only ever read back off an answer, and a spelling no set shows the judge
is one no judge will return. That is a refusal when the file loads.

### The plant model

A judged batch carries two kinds of synthetic record, shuffled in among the real ones and
indistinguishable from them (same envelope builder, same fields, a donor record's home
cell, requirements, probe and provenance):

- An **inventory plant** IS an existing register row, so its only correct labels are
  `same` or `variant`. A judge that calls one `new-mechanism` is either being gamed or
  drifting, and either way its labels for that batch cannot be trusted — **a missed
  inventory plant fails the batch outright**.
- A **paraphrase plant** is a restatement of a record already in this batch, so its only
  correct labels are the same two. A missed one is reported and does not fail the batch.

**A plant is cut from a MECHANIC ROW, never from an arbitrary chunk.** A register holds
more than mechanics — citation-convention notes, coverage-gap lists, headings — and a
plant cut from one of those states no mechanism, so a judge doing the task honestly
answers `unscreenable`, the scorer counts a missed inventory plant, and the batch fails on
the battery's own sampling rather than on the judge. A candidate is a table row whose first
cell matches the register's row-id pattern (`<source>-M<nnn>`) with a non-empty description
cell; the **description** is the planted statement, never the chunk (which carries the id
and name a judge would read as a register row). The rows are filtered first and sampled
second, and a reference set that cannot supply `--plants` of them is a refusal
(`no_mechanic_rows`), never a short list. Each plant records `source_row_id` (the M-row) as
well as `source_ref` (the chunk the judge was handed).

**`kind` says how a plant was BUILT, and nothing else may be spelled there.** The four
words are `area`, `paraphrase`, `inventory`, `custom` — the harness's own construction
vocabulary. A live plants file spelled `kind` as the plant's CLASS (`present`, `adjacent`,
`absent`) and was refused one plant at a time, by the first offender, twelve times. The
refusal now reads the whole file and names every offender with its bad word (capped at ten,
with "and N more"), names the four kinds, and points at the key a class actually goes in.

**`class`** is that key: optional free text up to 40 characters, bounded because it becomes
a group key on the calibration card. It groups that card's `by_class` table per judge, and
**it is never shown to a judge** — a judge told "this one is an ABSENT plant" has been handed
the answer, and no real record carries the key either, so a leak would be a tell twice over.

**`batch`** seeds a plant into ONE judged batch. A plant that declares one rides only in the
batch whose `--batch-id` matches it (compared as a string, so `2` and `"2"` are one batch); a
plant that declares none rides in every batch, which is what every plants file did before the
key existed and what an unchanged file keeps doing. The batch record carries
`plants_injected` — which of the round's own plants this batch actually got — and a
`--batch-id` no plant declares injects none of the batched ones and says so in `warnings`
rather than quietly building a battery out of the unbatched plants alone. `--batch-fail-on`
is evaluated over the plants this batch carried, so a plant seeded into another batch can be
neither caught nor missed here. A calibration reads the same file by the same rule: one
plants file must not mean two different things depending on which verb read it.

**A paraphrase plant is never byte-identical to its donor.** It carries a non-identity
frame plus at least one deterministic, seeded, lossless transformation — sentence order
rotated, a coordinated sentence split in two, a subordinate clause or trailing adjunct
fronted, single-letter symbols renamed consistently — and the plant's `paraphrase_method`
names the frame, the transformations, the symbol map and any coordinating conjunction the
split consumed, so a rename and a drop are recorded rather than silent. An identity
paraphrase asks a judge whether a record is the same as itself; it catches nothing, and a
batch whose paraphrase half is identities reports a catch rate about a question nobody
asked. `--paraphrase-backend llm` is declared and refused: a battery whose plants a model
rewrote would make the audit depend on the class of system it audits.

### The judged batch, and the ids a judge sees

`lens screen --judged-prep --seed S` writes `artifacts/rounds/<round>/judged/<batch>.json`,
which carries both:

- `envelopes` — real `subject_id`s (`IDEA-…`, `PLANT-inventory-0`). The orchestrator's copy.
- `judge_views` — the same envelopes, field for field, with the subject id replaced by
  `J-<n>` under a seeded permutation, plus a `mask` back to the real ids that lives only in
  this file.

**Build the judge's prompt from `judge_views`.** An envelope's own subject id names the
plant, so a prompt builder that copies the envelope hands the judge the answer key, and a
plant a judge can pick out tests nothing. `--judge-envelopes-out DIR` writes one masked
view per file for exactly that purpose. `--record-verdicts` accepts labels keyed by the
masked id, the real id or a mix, for both the first and the second judge's sheet — the mask
is a barrier, not a trap — and a key that names neither is still refused by name.

The inventory rows in an envelope are **handed over, not retrievable**: the register is the
reference set the record is judged against, so the knowledge server excludes that source
kind from every surface a lens or a verifier holds, and a judge that tries `get_chunk` on a
`CHK-…` row gets `ChunkNotFoundError`. That error is the barrier working. The corpus
passages and literature hits in the same envelope ARE retrievable.

### `prereg_compliant` — pass the procedure byte-exact

`--executed-procedure-file FILE` hashes the file as it is on disk. Use it rather than
`--executed-procedure "$(cat file)"`: the shell strips the file's trailing newline, the
recomputed hash differs from the committed one, and a procedure that WAS followed is
stamped non-compliant. The two flags are mutually exclusive (a hash computed over two
different byte strings is a compliance claim about neither), and a mismatch now says which
of the two hashes — procedure or params — disagreed, with both short hashes beside it.

**Both recording phases stamp it**: `--record-verdicts` and `--record-calibration` take the
same `--prereg-id` / `--executed-procedure[-file]` / `--executed-params`, through the same
check. `--record-calibration` used to link the pre-registration onto every row and leave the
column NULL, which reads later exactly like a row recorded under no pre-registration at all —
and a calibration is the instrument the round's own labels are judged against, so "was this
the instrument we committed to?" is not a question it may leave blank.

Three answers, and they are three rather than two:

| What you passed | `prereg_compliant` | What the envelope says |
|---|---|---|
| no `--prereg-id` | `null` | `no prereg_id given` — nothing was promised |
| `--prereg-id` alone | `null` | `not stamped: pass executed_procedure …` — promised, **unchecked**. Not "compliant by default" |
| both | `true` / `false` | the hashes, recomputed; on `false`, which half moved |

**A mismatch is recorded, never refused.** `prereg_compliant = false` rows are written exactly
like compliant ones — a recording is evidence of what happened, and refusing it would delete
the only trace that the procedure moved.

### The record schema, enforced at intake

`trialerror lens intake --round-id R --records FILE --author-launch L [--assign-id A ...]
[--arm ARM] [--status STATUS]` writes a lens's returned records as `idea` rows
(`--status archived` is the archive intake — see "The archive round" above). Per record:

| Field | What it must be |
|---|---|
| `statement` | required — the record itself, full text |
| `probe` | required — what one would implement or simulate, against which named row — **except on an `archived` row**, which may omit it or pass `null` (from the record's own `status` or the call's `--status archived`). An archive intake writes rows that already exist, most of them written under a schema that had no probe; "a record with no probe is not finished" is a rule about a record this round asked a lens to produce, and an archived row is not a candidate and is never consolidated. A non-archived record without one is still refused, and the refusal says which rows may omit it |
| `provenance` | required — an OBJECT carrying `docs: [doc_id, …]` (the alias `slice` is accepted and copied into `docs`); the judge envelope reads `provenance.docs`, so a record without it is judged as having come from nowhere |
| `requirements` | a string or a list of lines; a list becomes newline bullets (it used to reach sqlite as a bare `type 'list' is not supported`) |
| `operation_declared` | the two-axis declaration the distribution card counts: `{"opportunity": …, "method": …}`, `opportunity:bridge, method:formalize`, or the canonical `opportunity/method` — all three are accepted and stored in the one form the card reads |
| `extra` | optional — the record's own free block, **any** keys at all (`literature`, `unlock`, `seeds`, …), stored as written (`idea.extra`, knowledge schema v11) and rendered into the one `record.extra_text` field a judge sees by exactly the function a plant's block goes through. A plant had this and a record did not, so the text was folded into the statement by hand, differently each time — and the envelope's shape is what keeps a plant indistinguishable from a record, which cuts both ways. A malformed block is refused with the record's position on it |
| `home_mechanic`, `assumed_circle`, `surprise`, `author_rationale`, `recipe_card`, `tier` | optional, stored as given |

**Intake fills the idea-vector cache.** Every record's statement is embedded here — the
first moment its vector can exist, and the moment you are sitting in front of it — so the
round's first screen does not pay for the lot inside itself, where the wait reads as the
screen being slow. It is an optimisation and is treated as one: an absent or parked
embedding backend warns (`idea_vectors.warnings` in the envelope) and the records still
land, because the screen's own fill is still there and still correct. `--no-embed` opts out
and costs time later rather than anything else.

**The whole file is validated before any of it is written**, and a write that fails anyway
takes back the rows already written: a partially intaken file makes the round's record
count, its per-arm n and its distribution card numbers about an incomplete batch, with
nothing in the store saying so. Every refusal names the record's position and the field. A
field nothing reads is refused rather than stored.

### The four files a round writes by hand, side by side

Three of these are written by a person and one by a lens, and the distinctions between them
cost a live round a round-trip each. Each is validated the moment it is read, before
anything is embedded or written.

| File | Flag | Shape | Lenient about |
|---|---|---|---|
| a round's records | `lens intake --records FILE` | `[{statement, probe, provenance: {docs: [...]}, requirements?, operation_declared?, home_mechanic?, assumed_circle?, surprise?, author_rationale?, recipe_card?, tier?, status?, extra?}]` | `extra`, which takes **any** keys and reaches the judge as one `record.extra_text` block — exactly as a plant's does. Every other unknown field is refused by position and name, and `probe` is required unless the row is `archived` |
| the round's plants | `lens screen --plants-file FILE` | `[{plant_id, kind, statement, expected_labels: {SET: [label, ...]}, donor_ref?, source_ref?, requirements?, home_mechanic?, probe?, extra?, class?, batch?}]` | `extra`, which takes **any** keys and reaches the judge as one `record.extra_text` block |
| a judge's label sheet | `lens screen --record-verdicts FILE` / `--judge-sheet-a\|-b` | `{subject_id: {label_archive?, label_inventory?, label_corpus?, seeds?: [{ref, label}]}}` | nothing — a label outside the set's vocabulary (plus the round's `unscreenable` word) is refused by name, and a key for an undeclared set is refused |
| human pair ratings | `lens screen --pair-ratings FILE` | `[{a, b, human}]`, `human` in 0..1, each id a plant in the batch or an `idea_id` | extra keys, ignored and named in the card's `warnings` |

The label sheet's keys are the reference sets' own: `label_archive` (R2), `label_inventory`
(R3), `label_corpus` (R4). A subject may be named by its MASKED id (`J-<n>`) — build the
judge's prompt from `--judge-envelopes-out`, never from the batch file, whose `subject_id`
is `PLANT-inventory-0` for a plant and therefore the answer key.

The one rule behind all four: **lenient about what a person ADDS, strict about what the
schema NEEDS.** An extra key is a note; a missing answer, an unknown label or an unresolvable
reference is a refusal, and every refusal names the position and the field.

### Linking a lens's launch to its slice

`lens_assignment.launch_id` is the launch that WROTE the row — the orchestrator running
`lens assign`. The lens's OWN launch is booked later, and it has to be linked or the
per-launch retrieval scope and `lens_citations_within_slice` both read it as "not a lens
launch": the scope does not engage and the audit skips, for exactly the launches the
barrier exists for. Two ways to link one, and one of them is required:

1. Book straight off `lens export` — its `attrs` carry `slice_doc_ids`, `assign_ids` and
   `roster_id`, and the scope reads them in that order.
2. `trialerror budget book --assign-id <id>` (repeatable), which records
   `lens_assignment.lens_launch_id`. An `assign_id` that names no row refuses the whole
   booking rather than linking part of a slice.

`lens log` then reports, per lens, whether that launch actually posted — and the gate
suite's `lens_log_reconciled` check FAILs on any lens that did not, FAILs a subject
that carries neither `rows` nor `offenders` with *log shape unrecognised* rather than
counting rows and passing, and FAILs a log that reconciles no lens at all (`rows: []`,
`n_lenses: 0`), which is what `lens log` returns for a mistyped `--round-id` or a gate
run before `lens assign`.

## Doctor checks catalog

`trialerror doctor` runs every check registered by every subsystem (**93 checks across 25
categories**); each subsystem owns its own `checks.py`, auto-discovered — adding a new one
never touches a shared file. That figure had drifted twice before anyone noticed, precisely
because nothing enforced it, so it is now pinned by a test against the live registry
(`tests/test_docs_doctor_catalog.py`): adding a check makes that test fail until this
sentence is updated with it. The table below is a reader's map of the busiest categories,
not the full registry — `trialerror doctor --json` is authoritative.

| Category | Checks |
|---|---|
| `stores` | `store_schema_version`, `xid_dangling` (cross-store reference scan), `anchors_dangling` (doc_sha256 half) |
| `ingest` | `chunker_missing`, `chunker_outdated`, `embedding_missing`, `embedding_stale` (**both resolve the active embed model key by following the embed-backend loader's branches rather than by preferring a configured key: `backend = "fake"` → `fake-DIMS`; `backend = "offload"` → the configured `ingest.embed.model_key`, which that backend demands; any other backend name → that name itself. `ingest.embed.model_key` is read only on the `backend = "offload"` branch because that is the only branch where the loader reads it — a stale `ingest.embed.model_key` line left behind after a backend change must not steer the check at a key nothing on disk carries. An offload-backed program is therefore reported against the model key the remote GPU worker really stamps on its embedding rows, never against the literal string "offload"**), `vector_index_stale` (the index side of the same pair as `embedding_stale`: chunks that HAVE an embedding row for a registered model key and NO entry in that key's `vec_chunks__<key>` table, so they are embedded and invisible to semantic retrieval anyway — **fail** for the key `[ingest.embed]` configures, warn for any other registered key or one whose table cannot be read here; compared per chunk rather than by subtracting the two totals, because the embedding cache is hash-addressed and the index is chunk-addressed, so a corpus with duplicate chunk text legitimately has more entries than rows; repair with `trialerror ingest reindex-vectors`), `anchor_spot_resolve` (quote_sha256 half), `fake_backend_rows` (fake-backend rows in a program that declared it will not accept them, resolved per stage: fake embeddings **fail** only where the embed stage is required to be real, fake OCR only where the OCR stage is, each half of the message naming the key that decided it. The embedding half also names the key this program embeds under NOW, calls the fake keys superseded rather than in use, and names the `ingest purge-embeddings --model-key <the superseded key>` command that removes them — refusing to call anything superseded when the configured key is itself fake, since purging the key the live search reads from would empty the corpus. An OCR'd document is repaired by re-ingesting it, `ingest retract` first), `extraction_quality_suspect` (**is the extracted text usable at all?** — four measures over the sanitised element text: glued-token rate (tokens longer than 25 characters, over the chunker's own `estimate_tokens()` count), characters that are not text (U+FFFD and non-whitespace control bytes, the normalizer's own regex), sentence-terminator density per 1,000 characters, and the coefficient of variation of characters per page over the document's declared page count (pages with no text count as zeros), where a page count is known. **warn**, never fail, and it SAMPLES: `[ingest.quality] sample` documents (default 50) drawn under `[ingest.quality] seed`, so the cheapest health command in the system does not become proportional to the corpus and two runs against an unchanged corpus name the same documents. A glossary is legitimately terminator-poor and a table-heavy appendix legitimately glued-token-rich, so this is a signal to read, never a verdict — `trialerror ingest quality --all --worst 10` is the exhaustive pass and `--doc-id` the single-document one. Skipped on a corpus with no documents; a document with no extracted text is unmeasurable rather than suspect, and a document shorter than `[ingest.quality] min_tokens` (default 200) is measured and counted under *below_min_tokens* -- never suspect, because three of the four measures are rates a twelve-token note trips by arithmetic) |
| `jobs` | `stale_lease`, `heartbeat_age` |
| `offload` | `offload_backlog` (documents waiting for the remote GPU worker), `offload_stale_claims`, `offload_failed` (markers past their DEV attempt budget — **a marker an operator has already retried is not one of them**: `trialerror jobs retry` moves it to `failed/_retried/<job>.<stamp>/`, which this check counts separately, in `details.retried`, and never warns on, because a finding that survives the act that resolves it is a finding nobody can clear), `worker_heartbeat_stale` (**the control law's own witness: warn when a worker holding a claim has not reported for longer than the lost window (2x its heartbeat interval + 60 s) — the ordinary closed-laptop state `offload reclaim` converges; FAIL when a stop request is older than the control TTL and the worker is still reporting itself as running, because nothing here can kill a process, so a worker that ignores control is a defect rather than a state**), `offload_control_orphaned` (a pause/resume/stop request with no live worker to read it — warn, since leaving one for a worker you are about to start is legitimate and the request expires at the TTL), `offload_backend_root_resolved` (**does the backend config root this program carries resolve to the backends it names? — the root that was read, each stage's `[ingest.<stage>] backend`, and for a stage this machine is the one to run, whether the backend object can be CONSTRUCTED and whether the executable or module directory it names exists here. Read through the worker's own `ConfigDevBackends.describe()`, which makes the same construction calls its `validate()` makes, so this check and `trialerror offload worker` cannot disagree about one root. **fail** when the root's `trialerror.toml` is there but cannot be LOADED (unparseable, or missing `[program] id`) — the worker's own config load raises and it refuses with `error.code = "bad_config"`, so a check that read the unreadable file as "nothing declared" would report `backend = "fake"` stages for a file that says `backend = "marker"` (verify V-1); **fail** when a stage this machine runs cannot be constructed (a missing `[ingest.ocr] marker_single_exe`, `[ingest.embed] python_exe` or `[ingest.embed] module_dir`, an unknown `[ingest.ocr] backend` name — on the embed side an unrecognised name IS the model key, so that stage fails on the missing `[ingest.embed] python_exe` / `[ingest.embed] module_dir` rather than on the name — a worker launched against this root refuses to start) or when a stage reads `backend = "fake"` while the program declares that stage must be real; **warn** when it constructs but a path it names is absent on this machine (the config of the machine that runs the model is legitimately readable from one that does not, and the construction — the part a worker acts on — succeeded); **pass** when every stage either resolves here or is honestly declared `backend = "offload"` / `backend = "fake"`, which is the queue host's own normal state and names the stub that said so. Resolved paths appear in the result at runtime only**) |
| `law` | `law_digest_lockstep`, `law_chain_integrity`, `law_pin_format` |
| `budget` | `budget_dangling_launches` (live bookings past their TTL, split on session liveness: a launch whose own session is still OPEN **and** still recording hook-liveness events is reported in a separate past-TTL-session-alive list — a TTL that was set too short, which **`budget heartbeat`** clears — and everything else stays in the offender list. With no `--program-root`, no ops.db yet, or an ops.db this run cannot read, the split cannot be evaluated at all and the check degrades to one undifferentiated list, with a liveness-evidence line naming which of those three absences fired rather than assuming the first. Bookings are shared across programs and sessions are not, so a past-TTL launch booked by a session this program's ops.db does not know is reported in its own foreign-session list and the evidence line reads **partial**: this run has no liveness evidence either way about that launch, which is not the same finding as a dead session. The message never claims a crashed session: an elapsed TTL cannot tell the two apart, and the dashboard's budget card prints the same sentence from the same module, and the same SEVERITY word: the check warns whenever ANY booking is past its TTL, including one whose session is demonstrably alive, and the card carries that word on the panel as *past_ttl_status* rather than leaving a reader to infer one. **Why the card's `DANGLING` chip can read 0 and settled while this check warns** (a question FB-1's verify pass asked, finding V-11): `DANGLING` counts the doctor's own *offender* list, and zero there is a true reading of that list rather than a verdict about the past-TTL question — the severity for the other half rides the **PAST TTL, SESSION ALIVE** row, which renders at that warn, and the zero chip's own tooltip says the doctor is still warning and names that row. Two lists, two readings, one severity word computed in one place (`trialerror.budget.dangling.past_ttl_status`), so neither surface can drift from the other), `budget_pool_overspend`, `agent_model_matches_booking` (**fail**, not warn — over reconciled launches that recorded the model they ran on, any that ran on a class below the one their booking claimed; a launch reconciled without --spawned-model is counted as unattested and named as such in the message, never quietly passed. Model names resolve through the program's own `[model_classes]` table — the same one the spawn gate reads, so gate and check never disagree about a name — and a name neither that table nor the built-in families can place is reported apart, at **warn**: unverifiable is a different finding from below-the-floor), `reconcile_provenance` (**where did this program's settled token numbers come from?** — *launch.actual_tokens* is what every cap check, calibration and weekly reading is built on, and until platform-v2 the only way to put a number there was a person typing `--actual-tokens`. *transcript*, *estimate* and *manual* are all caller-ASSERTED labels; *event* is the one that is not — set only by `budget reconcile --from-event`, which reads the host's own *usage* object off the launch's *subagent_return* event, and which the reconcile write path refuses from every other caller including the MCP tool. **warn** on any reconciled launch reading *manual*, a number nobody can trace; never **fail**, because on a host that sends no usage, or for a launch spawned by a tool that fires no hooks, `--actual-tokens` IS the documented path and failing a program for using it would teach an operator to ignore the check. The *transcript*/*event*/*estimate* counts ride the message either way, so a program moving from asserted to measured provenance can watch that ratio move — the only way anyone finds out whether the hook is really firing. A pre-platform-v2 row carries no source at all and is counted as *unrecorded* in its own clause: “settled before this program recorded provenance” and “settled by hand” are different facts, and only the second has a fix. The details carry the **20 most recently settled** manual rows beside the full totals rather than every one of them: unlike the other budget offender lists this one is most of a program's past, and a doctor result is serialised into the dashboard's state file and doctor panel on every run), `quota_capture_stale` (**is the plan-quota capture fresh enough to size a booking against?** — the statusLine script writes it on a Claude Code UI tick, so the reading goes stale precisely while the orchestrator is idle, which is also when it is most likely to be consulted before booking. **warn**, never fail: the feed is optional and screenshot snapshots remain the ground truth, so a stale reading is a state to notice, not a rule broken — the BOOKING GATE is what refuses (`budget book` and the *book_launch* MCP tool, overridable with `--allow-stale-quota` / *allow_stale_quota*, which is recorded on the launch beside the reading it overrode). **skip** when nothing was ever captured: a program that has not wired the statusLine has no reading to be out of date, and calling that staleness would make an optional feed look broken. The bar is `[budget] quota_max_age_s` (default 900 s) and the details say which trialerror.toml — or that the default applied because the run had no program root) |
| `events` | `event_secret_leak`, `feed_author_integrity` |
| `feed_translate` | `feed_translation_failures`, `feed_translations_stale` |
| `sessions` | `session_multiple_open`, `session_hook_alive` |
| `lexicon` | `term_conflicts_pending`, `term_duplicates_pending`, `term_senses_need_review`, `term_sense_without_evidence` (fail), `term_split_missing_disambiguator` (fail), `term_evidence_source_unlinked`, `term_fts_in_sync` (fail), `definition_claims_unprojected`, `term_system_relation_decided` (fail) |
| `artifacts` | `gated_type_without_gate`, `orphan_gate_transition`, `gate_illegal_transition_history` |
| `memory` | `memory_unresolved_conflict_groups`, `memory_l0_index_budget`, `memory_stale_items`, `memory_pending_conflict_candidates` |
| `lens` | `far_arm_floor_honored`, `no_duplicate_slice`, `cluster_coverage`, `far_lens_floor_honored` (**fail**; `--arm-per-lens` rounds only — the far arm must seat at least the HARDER of the round's own `--far-floor` and the hard floor of 2 LENSES, which the slice-level check structurally cannot see; a round may raise its own floor but not lower it below 2, and per-slice rounds are skipped, not judged), `recipe_rotation_honored` (**fail** — four bars: every **standard** lens writes under exactly 2 cards, every card in play is held by ≥ 2 of them, ≤ 4 distinct cards per round, and `NEGATE` sits on the assumption-buster seat alone. Holders and block size count standard seats only (the buster's card is its seat's, the control has none by design); the 4-card ceiling counts every non-`NEGATE` card wherever it sits, so a fifth cannot hide on an excluded seat. Rounds with no cards at all are skipped), `lens_citations_within_slice` (**fail** — every document/chunk/anchor id a lens post cites must resolve to a document in that lens's own slice, read from `launch.attrs.slice_doc_ids`, failing that the launch's roster/assignment attrs and their lens_assignment rows, and failing those the `lens_assignment.lens_launch_id` link `budget book --assign-id` writes from the assignment side — a lens booked through the CLI carries none of the exported attrs, and without that fourth source this check SKIPped for exactly the launches it exists for, naming the two ways to link one in the skip message. The audit half of the per-launch retrieval scope, which enforces the same rule only for launches whose booking declares a slice. An id resolving to a document outside the slice is a crossing (**fail**); an id resolving to no row in this corpus crossed nothing and is reported apart at **warn**, still counted in a failing run's message. Posts by launches that are not lens launches are skipped), `idea_missing_dossier` (**fail** — an idea reaches the consolidated status by passing through the novelty screen, and the screen writes one dossier per idea, so a screened row with no dossier file under its round's own directory is a row whose status asserts a measurement nothing can read back. Rounds with no directory on disk are skipped, not judged: "this round predates the screen" is a different statement from "this round lost a dossier"), `round_collapse_flag_unacknowledged` (**fail** — a raised collapse flag nobody ruled on is what makes the collapse monitor decorative. Close it by recording a "round_collapse_acknowledged" event naming the round AND the batch, or by firing the pre-registered "round_collapse_rerun" contingency; acknowledging one batch deliberately does not cover another. Batches whose alarm values are still descriptive-only have no flag to raise and are skipped), `lens_brief_contains_verdict_text` (**fail** — a generator that can see a dossier label, a room verdict or a scoring rubric writes toward it. Briefs are read from a lens launch's own `attrs.brief`/`attrs.prompt` and from "lens_brief" events; only LENS launches are read, since a critic's brief is supposed to carry the rubric and flagging it would teach an operator to ignore this check. A program recording no lens brief anywhere is **skip** with both places named — nothing to audit is a recording-discipline statement, not a clean result) |
| `retrieve` | `fence_integrity` (license-fence spot-check), `retrieval_latency`, `fulltext_index_stale` (tantivy index vs corpus; repair with `trialerror ingest reindex-fulltext`), `query_embed_backend_runnable` (**can THIS process embed a query?** — a program whose document embeddings are produced on another machine has a document backend that refuses to compute here by design, so query-time embedding needs its own backend: `[ingest.embed.query]`. **warn**, never fail, carrying the backend's own reason and a *runtime* detail naming WHICH runtime and its numbers — the in-process encoder's effective *n_threads* / *n_threads_batch* / cgroup CPU quota, or the sidecar client's URL and last health reading — the corpus is intact and `query search` still answers out of the full-text tier and says so in a warnings entry of its envelope, while `verify hypothesis` and `lens screen` refuse; skipped on a corpus with no embeddings at all), `vecmatrix_stale` (the cached resident similarity matrix's fingerprint vs its vector table — **warn**, because a stale cache is never served: the fingerprint is checked per query and a mismatch rebuilds before ranking, so what this reports is the rebuild the next unbounded ranking call will pay for) |
| `verify` | `verdict_evidence_anchors`, `prereg_escrow_integrity`, `gate_without_prereg` (**fail** — a gate whose artifact's attrs declare a round id must name a live pre-registration, found on the artifact's own `attrs.prereg_id`, on a verdict row about the artifact, or on a verdict about one of that ROUND's own records — `subject_kind='claim'`, resolved through `idea.round_id`, which is the shape the novelty screen actually writes (it links `verdict.prereg_id` per idea per reference set, never onto the artifact). A named prereg that does not resolve, or that was voided, is reported as its own offender kind rather than folded into "named none". Scope is deliberately narrow: every other gated artifact in the program is not under this rule, and widening it to every gate would make it noise an operator learns to skip) |
| `obs` | `obs_exporter_reachable`, `obs_span_drop_counter` |
| `util` | `license_audit` (vendored/ header + manifest scan) |
| `sidecar` | `sidecar_alive` (**is every process this program needs RUNNING actually running?** — one row per `[sidecars.<name>]` table: the process this program recorded, whether the pid is alive, and whether it answers its *health_url*. **warn**, never fail, on the same reading as `query_embed_backend_runnable`: a dead embedding sidecar costs the vector tier, which every retrieval surface already degrades from and reports, and on a just-booted machine it is the expected state until somebody runs one verb. Read-only in both senses — it asks `sidecar status` with restarts switched OFF *and* with the heartbeat refresh switched off, because a doctor run that silently restarted processes could not be used to find out what was wrong, and one that refreshed the heartbeat it reports would be the only thing keeping that timestamp fresh (the age is reported as *heartbeat_age_s* and is deliberately not a verdict: a sidecar's heartbeat is written by whoever last polled, so in a program where nothing polls on a schedule a threshold would fail a healthy process; the live health probe is what answers "is it serving"). A sidecar with no *health_url* is reported as `health.configured: false`, not as healthy, and one that has a *health_url* but is not running reports `configured: true` with *skipped* naming why nothing was probed; a `[sidecars.<name>]` table this supervisor cannot read (a *command* that is a string, an unknown *restart*) is reported as *misconfigured*. Skipped when the program configures none) |
| `webfetch` | `webfetch_sidecar_alive` (the fetch process's heartbeat), `webfetch_backlog`, `webfetch_refused_24h` (warns on the SSRF/exfil refusal class), `webfetch_unattributed` (a fetch no booked launch asked for — the audit trail it reads is append-only, so an intentional one, such as the deliberate forged-attribution test, is retired with `trialerror webfetch ack --fetch-id … --launch-id … --note …` and its host twin `te-webfetch.sh ack`; the line is never deleted, the acknowledged offender is still reported with its note, and the acknowledgement is bounded to what was on record when it was made, so a later fetch fails the check again whether it carries a new id or reuses the acknowledged one), `webfetch_orphans`, `webfetch_queue_disk`, `webfetch_thin_backlog` (info), `webfetch_refetch_due` (info) |
| `vastai` | `vastai_high_tier` (warns whenever the high tier is configured, approved or recently used; passes otherwise), `vastai_live_instances` (fails if any TrialError-tagged instance is past its deadline or not confirmed destroyed, warns while one is live and billing, passes when none is) |

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
