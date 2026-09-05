# User setup checklist

Everything in this file is **user-owned** (accounts, keys, local paths, or your real
machine) — nothing `pip install -e .` gets you automatically. It's extracted from
`docs/DESIGN_v0.md`'s USER-SETUP appendix (§14) plus the concrete config fields the
shipped code actually reads, so you can act on it directly rather than cross-reference
the design doc.

**Nothing here requires payment for v0.** Everything with a cost is either free-tier or
explicitly optional-and-skipped-by-default.

## v0 — what you need right now

Nothing. v0 runs entirely on:

- your existing Claude Code subscription (TrialError is an exoskeleton around it, not a
  separate paid service),
- local SQLite (WAL mode — no server, no Docker),
- the deterministic **fake** OCR/embed backends by default (`trialerror/ingest/backends.py`)
  — every pipeline stage works with zero GPU/model dependency out of the box.

Everything below this line is either (a) needed only once you switch to the real local
models, (b) needed only for v1 acquisition features, or (c) optional observability.

## 0a. Linux / container — what changes, what doesn't

Everything in "v0" above runs unmodified on Linux or inside a container: the runtime has
no Windows-only dependency beyond one detached-process launch technique
(`DETACHED_PROCESS` on Windows, POSIX `setsid` — `start_new_session=True` — everywhere
else), and that POSIX arm now has its own test coverage
(`tests/test_posix_detach.py`) alongside the pre-existing Windows-only tests. Concretely:

- **Program root / `[paths]` knobs** — `find_program_root`, `resolve_configured_path`,
  and every knob this file's later sections mention (`stores_dir`, `ingest_roots`, …) are
  built on `pathlib.Path`, not string-splitting — a Linux path works exactly like a
  Windows one everywhere in the CLI.
- **Platform root** — still `TRIALERROR_PLATFORM_ROOT` if set, else `Path.home() /
  ".trialerror"` (`trialerror/stores/paths.py::platform_root()`) — `Path.home()` resolves
  to the container/Linux user's own home directory, same mechanism, no code change.
- **`marker_single_exe` / `python_exe` / `module_dir` (§1 below) stay per-machine, GPU-
  bound paths regardless of where the `trialerror` process itself runs** — these name a
  local GPU install on whichever machine actually has one; a Linux/container host with no
  GPU simply leaves `[ingest.ocr]`/`[ingest.embed]` unconfigured (falls back to the
  deterministic `Fake*Backend`, same as any fresh program) rather than pointing them at
  itself.
- **Proven in a real container deployment, not just theoretically portable** — a checkout
  bind-mounted into a Linux container (`pip install -e`'d at image build time), platform
  root pointed at a dedicated directory via `TRIALERROR_PLATFORM_ROOT`, and the long-running
  processes supervised as a detached, foregrounded loop (`dashboard serve --foreground` /
  `jobs start-worker --foreground` in a supervisor's own restart loop — the `--foreground`
  flags there are deliberate: a supervisor wants the CLI to block, not self-detach a second
  time).

## 1. Local models — marker OCR + Qwen3-Embedding-4B

The real backends are **config-pathed, never hardcoded** — TrialError shells out to your
existing installs as subprocesses. Nothing installs them for you; nothing runs them
until you configure a program's `trialerror.toml` to point at them.

| Config | Field | What it needs |
|---|---|---|
| `[ingest.ocr]` | `backend = "marker"` | Set once you've decided to stop using the fake OCR stand-in |
| | `marker_single_exe` | Absolute path to your `marker_single` executable (the marker-pdf CLI). GPU-only — no CPU fallback is attempted; a non-zero exit surfaces as a job failure. |
| | `marker_version` (optional) | Defaults to `"1.10.2"` |
| | `marker_extra_args` (optional) | Extra CLI args passed through verbatim |
| `[ingest.embed]` | `backend = "qwen3-4b"` (or any name — anything other than `"fake"` routes to the real backend) | |
| | `python_exe` | Absolute path to the Python interpreter **inside the venv that has the embedding model's dependencies installed** (torch/sentence-transformers) — this process itself never needs those installed |
| | `module_dir` | Directory containing `embed_backend.py` (the `load_backend(name).embed_batch(...)` module this shells out to) |
| | `dims` (optional) | Defaults to `2048` (the matryoshka-truncated dimension) |

Example:

```toml
[ingest.ocr]
backend = "marker"
marker_single_exe = "/home/you/tools/marker/venv/bin/marker_single"

[ingest.embed]
backend = "qwen3-4b"
python_exe = "/home/you/research/tools/embeddings_local/venv/bin/python"
module_dir = "/home/you/research/tools/embeddings_local"

# Windows equivalents — same keys, drive-letter paths, and a venv keeps its
# interpreter under Scripts/ rather than bin/:
#   marker_single_exe = "C:/tools/marker/venv/Scripts/marker_single.exe"
#   python_exe = "C:/research/tools/embeddings_local/venv/Scripts/python.exe"
#   module_dir = "C:/research/tools/embeddings_local"
```

**"Absolute" means absolute for the platform actually running the command.** `pathlib`
decides that against the host, so a `C:/...` value read on Linux has no drive, reads as
*relative*, and gets joined onto the program root — a real directory, created without
complaint, in the wrong place. Every `[paths]` key (`stores_dir`, `archive_dir`,
`law_digest_path`, `handoffs_dir`, `requests_path`, `memory_dir`, `ingest_roots`) now
raises a `ConfigError` naming the mismatch rather than resolving it silently; the three
`[ingest.*]` paths above are handed straight to the OS, so a wrong-platform value there
surfaces as a job failure on a missing executable instead.

**Why "your existing" tools**: the design ports the operator's own already-proven local
`marker_ocr`/`embeddings_local` tooling rather than reimplementing OCR or embedding —
if you don't already have a working `marker_single` install and an `embed_backend.py`
module for a local Qwen3 embedding model, that installation is out of scope for this
harness and needs to happen first, on its own terms.

**Status honestly**: neither real backend has been run against a live GPU on this build —
`RealMarkerOcrBackend` has a test that self-skips without `marker_single` on PATH;
`RealQwenEmbedBackend` has no execution coverage beyond argument-construction. The first
real ingest you run with these configured *is* the live verification.

## 2. Optional: local Phoenix trace sink

Entirely optional observability — every span emission no-ops silently if this isn't
installed or isn't running, so skipping this costs you nothing but trace visibility.

```console
pip install -e '.[obs]'
trialerror obs start-phoenix
trialerror obs status
```

The extras are quoted deliberately: `[obs]` is a glob pattern to most shells, so a bare
`pip install -e .[obs]` happens to work under bash but dies under zsh with `no matches
found` — quoting costs nothing and is correct everywhere.

`start-phoenix` launches a detached local `phoenix serve` (SQLite-backed, zero Docker,
zero account) at `http://localhost:6006`. `trialerror obs smoke` emits one span of each kind
(launch/retrieval/verification/job) so you can confirm the round trip in the Phoenix UI.
License: Elastic License v2, cleared for internal use (run locally, never resold/forked).

## 3. v1 acquisition features — SHIPPED (v3-acquisition build); act on this section now

The v1 acquisition-API integrations (OpenAlex / Semantic Scholar / arXiv / Unpaywall
clients, plus the `trialerror lit acquire` acquisition→ingest command) **landed** in the
v3-acquisition build. `trialerror lit doctor` (via the `litapi_providers_ready` check) reports
exactly which of the four providers below are ready right now — the table's numbers were
last verified 2026-08-29 against each provider's own live docs; two of the four facts
below changed materially from the original design doc's assumptions, flagged
**CHANGED** below).

| # | What | Why | Cost | Notes |
|---|---|---|---|---|
| 1 | **CHANGED** — An OpenAlex API key (`[litapi.openalex].api_key_path` in `trialerror.toml`, pointed at a file holding the key — never inline in `trialerror.toml` itself) | OpenAlex made a key **mandatory as of 2026-02-13** — the old "keyless polite pool via `mailto=`" is discontinued outright. Without a key you get a one-time 100-credit grace allowance, then HTTP 409 on every call after | **Free**, no payment method required. Free tier: $1.00/100,000-credit daily budget (resets midnight UTC), 100 req/s hard ceiling — comfortably covers single-researcher-scale lookups | Signup/docs: https://help.openalex.org (Pricing article). Academic/hardship upgrades: `support@openalex.org`. Until configured, `trialerror lit doctor` reports OpenAlex as `needs-key` |
| 2 | **CHANGED** — A Semantic Scholar API key (`[litapi.semanticscholar].api_key_path`) | The "1000 req/sec shared" figure on their product page is marketing copy, not the enforced limit — the real unauthenticated ceiling is a **5,000-req/5-min pool shared globally across every unauthenticated caller on the planet** (~16.7 req/s aggregate, not per-caller). A free key raises this to a dedicated **1 RPS** tier on search/batch/recommendations (10 RPS elsewhere) | Free, but keyed — apply via the request form on their product page; **requests from free email domains are rejected**, approval has historically run ~1 month backlogged (figures dated 2024, current backlog unconfirmed) | Signup: https://www.semanticscholar.org/product/api. Apply from a non-free-email domain, framed as first-party use, given the backlog. Until configured, `trialerror lit doctor` reports Semantic Scholar as `throttled-shared-pool` (it still works, just degraded — not a hard blocker) |
| 3 | arXiv — **nothing to do**, already ready | Fully keyless by design, no account, no signup. The client enforces the documented **1 request/3 seconds** ToU limit itself (`ArxivProvider`'s own rate limiter) | Free | `trialerror lit doctor` always reports arXiv as `ready` |
| 4 | An email identifier for Unpaywall (`[litapi.unpaywall].mailto` in `trialerror.toml` — same field name OpenAlex's old `mailto` used, reused for Unpaywall's `email=` param) | Unpaywall requires an `email=` query parameter on **every** call (identification only, not gated auth — no signup, no account). Without it, `UnpaywallProvider` refuses every call outright (`ProviderConfigError`) rather than silently omitting the param | Free, no account created | Uses your existing email address in outbound API query params only — this is a **decision to confirm**, not a task to complete: are you comfortable with that usage? Docs: https://unpaywall.org/faq. Until configured, `trialerror lit doctor` reports Unpaywall as `needs-email` |

Minimal `trialerror.toml` to get all four to `ready`:

```toml
[litapi.openalex]
api_key_path = "keys/openalex.key"

[litapi.semanticscholar]
api_key_path = "keys/semanticscholar.key"

[litapi.unpaywall]
mailto = "you@example.org"
```

**`trialerror lit acquire`** (the acquisition→ingest command): `trialerror lit acquire --doi <doi>|--arxiv
<id> --launch-id <launch>` resolves metadata across all four providers, then looks for a
**legal** open-access PDF using ONLY arXiv's own PDF link or Unpaywall's verified
`best_oa_location` (never a paywall-circumvention attempt — same C-0048/49 posture as every
other acquisition path in this harness) — found, it downloads and registers+ingests the
document automatically; not found anywhere, it files a `wanted` request-queue row
(`requests/REQUESTS.md`) with metadata prefilled for you to fulfill by hand. None of steps 1/2
above block this command from running at all — a keyless OpenAlex/Semantic Scholar just means
weaker metadata reconciliation, and a missing Unpaywall email just means OA resolution falls
back to arXiv-only (or the request queue) until you configure it.

**Explicitly skipped, decision already made**: scite.ai Pro ($50/mo, citation-stance
classification) — the design's default is to skip it; the in-house
citecheck+contracrow-hypothesis pipeline covers the same need. Revisit only if
verification volume concentrates heavily on DOI-indexed academic literature.

## 3b. Plan-quota feed - replace budget screenshots (one paste per account)

Claude Code >= 2.1.80 reports your plan rate-limit windows (5-hour session %,
weekly %, reset times) in its statusLine JSON - the exact numbers you have been
screenshotting. TrialError ships the capture side; you wire it with ONE settings key
per Claude Code account. In `~/.claude/settings.json` (top level) add:

```json
"statusLine": {
  "type": "command",
  "command": "<path-to-your-venv>/bin/python <path-to-your-trialerror-checkout>/trialerror/obs/statusline_capture.py"
}
```

**Name an interpreter by absolute path, not a bare `python`.** A stock Linux install ships
`python3` and no `python` at all, so `"command": "python <path>/statusline_capture.py"`
exits 127 and the status line simply never appears — and even where `python` does resolve
it is not necessarily the interpreter that has `trialerror` importable. Your venv's own
`bin/python` (Windows: `Scripts\python.exe`) settles both questions at once, regardless of
what is on `PATH` when Claude Code spawns the command.

What you get:

- a live status line in the terminal: `TRIALERROR | 5h 36% r18:00Z | 7d 11% | ctx 31% | Fable 5`
- every tick tees the quota into `~/.trialerror/quota/` (atomic `latest.json` +
  a throttled `rate_limits.jsonl` history - at most one row per 5 min unless a
  window moves >= 1 point)
- `trialerror budget quota` reads it anywhere (freshness-checked, 15-min bar);
  `--ingest --account-id ACC-...` records it as a `quota_snapshot(source=api)` row
- the dashboard's budget panel carries a `plan_quota` block automatically

Rules of precedence are unchanged: your screenshot ingests
(`source=screenshot`) still override everything on conflict; this feed is the
always-on estimate killer, not a new ground truth. Caveats: subscription
sessions only (API-key sessions omit the field); data updates only while a
Claude Code session is actually running on that account; on a second account,
add the same key to THAT account's `settings.json` (set `CLAUDE_CONFIG_DIR`
distinctly if both run on one machine, so `account_hint` distinguishes them).

## 3c. Optional: alphaXiv MCP connection (real semantic search + full text)

**Not wired into `litapi` at all** -- alphaXiv's API is an MCP server, not a REST endpoint,
so it doesn't fit the `Provider` protocol (`get_by_doi`/`get_by_arxiv`/`search`/
`get_citations`) the way OpenAlex/Semantic Scholar/arXiv/Unpaywall do. It's a standalone,
opt-in MCP connection you register yourself with `claude mcp add`, same pattern as this
repo's own `trialerror-ops`/`trialerror-knowledge` servers (`docs/OPERATOR_GUIDE.md`). `trialerror lit
doctor` (`litapi_providers_ready`) reports an `alphaxiv` readiness row alongside the four
real providers so you always know its current gate state, but no code in this package ever
calls alphaXiv directly.

**What it adds over OpenAlex + Semantic Scholar (already in this harness):** genuine hybrid
keyword+embedding search across "all of research" (not just title-relevance), **full
extracted paper text** (neither existing provider gives you more than metadata/abstract),
page-level PDF Q&A, and a researcher graph (follow/profile lookups). Findings as of
2026-08-29 (verified live against alphaXiv's own docs/pricing pages):

- **Pricing: no paid tier exists yet.** No pricing page is published (`alphaxiv.org/pricing`
  returns 404); the docs mention research/profile tools "count against your assistant
  quota" without disclosing a number, and third-party coverage (as of mid-2026) describes it
  as free with no ads/paywall rolled out. Treat "free today" as current-state, not a
  guarantee -- there's nothing published locking that in.
- **Account: required.** Default auth is OAuth 2.1 (your MCP client opens a browser
  sign-in on first use); for headless/scripted use, create an API key under **Settings >
  API Keys** instead and send it as `Authorization: Bearer <key>`.
- **This session does not, and will not, create that account or accept any ToS on your
  behalf** -- account creation and key generation are exactly the kind of step reserved for
  you (see this file's own posture: everything here is user-owned).

**Your steps** (all manual, all yours):

1. Go to `https://www.alphaxiv.org/`, sign up / sign in.
2. If you want headless/scripted use (not just interactive Claude Code sessions): Settings
   > API Keys > create a key. Save it to a local file -- never paste it into `trialerror.toml`
   directly.
3. Register the MCP server. OAuth mode (simplest -- a browser sign-in prompt appears on
   first use):
   ```console
   claude mcp add --transport http alphaxiv https://api.alphaxiv.org/mcp/v1
   ```
   Or, equivalent `.mcp.json` (same shape `docs/OPERATOR_GUIDE.md` uses for the two
   in-repo MCP servers -- key-gated: only include the `Authorization` header if you created
   an API key in step 2, and never commit the key itself, only reference where you keep it):
   ```json
   {
     "mcpServers": {
       "alphaxiv": {
         "type": "http",
         "url": "https://api.alphaxiv.org/mcp/v1",
         "headers": {
           "Authorization": "Bearer <your-api-key-here>"
         }
       }
     }
   }
   ```
   Omit the whole `"headers"` block for OAuth-only use -- the browser sign-in flow needs no
   header at all.
4. Flip the readiness gate so `trialerror lit doctor` stops reporting `alphaxiv` as `disabled`
   (this does NOT make any code call alphaXiv -- it only changes what the doctor check
   reports, since nothing in `litapi` consumes this section):
   ```toml
   [litapi.alphaxiv]
   enabled = true
   api_key_path = "keys/alphaxiv.key"   # omit entirely if you're using OAuth, not a key
   ```

## 3d. Optional, EXPERIMENTAL/FRAGILE: arxivxplorer.com search client (C-0069)

**Off by default; read `trialerror/litapi/providers/arxivxplorer_web.py`'s own module
docstring for the full robots.txt disclosure before turning this on.** It replays the exact
browser-equivalent search request `arxivxplorer.com`'s own frontend makes (recovered by
live browser-network inspection, not guesswork -- see that module's docstring), under
C-0069's binding guardrails: >=3s pacing, a sqlite response cache, a default 200/day
request cap, honest non-spoofed identification, and metadata/search only (never bulk
content harvesting). It stays a standalone `Provider` you construct directly -- it is
**not** wired into `trialerror.litapi.client.DEFAULT_CLIENTS`/`ALL_CLIENTS`, so nothing calls it
unless your own code explicitly does.

**The one thing to know before enabling it:** the actual API host this module calls,
`search.arxivxplorer.com` (not `arxivxplorer.com` itself), publishes
`robots.txt: User-agent: * / Disallow: /`. That's a machine-readable "no automated
crawlers" signal, not a Terms of Service, and this module makes exactly ONE
browser-equivalent request per `search()` call (never a crawl) -- but it's new information
C-0069's own text didn't have (that ruling described the FRONTEND host's robots.txt, which
is absent/404, not this one). Read that module's docstring in full before you decide.

```toml
[litapi.arxivxplorer]
enabled = true              # default false -- required, or the provider refuses to construct
daily_request_cap = 200     # lower this if you want to be more conservative
```

## 3e. Optional: all-arXiv semantic search (`trialerror.arxiv_index`, build-arxiv-kaggle-index)

A standalone local semantic-search index over arXiv Xplorer author `tomtum`'s
Kaggle-published `openai-arxiv-embeddings` dataset (MIT license, OpenAI
`text-embedding-3-large`, 3072-dim, ~34.9GB zip, weekly updates). See
`trialerror/arxiv_index/`'s own package docstring for the full architecture. **Everything
below is the operator's own step** — no agent session created a Kaggle account (account
creation is a prohibited agent action regardless).

**File format — CONFIRMED (fix-arxiv-ingest-layout session, direct inspection of the real
33GB zip download)**, superseding the original ASSUMED-jsonl placeholder below: the zip
has **exactly 2 members**, no titles/abstracts/authors/categories/doi anywhere in it:

- `papers.csv` (~0.10GB uncompressed) — header `index,id,journal`, one row per paper, e.g.
  `0,0704.0001,arxiv`. `journal` is empty for most rows (`journal_ref` in the index ends up
  `NULL` for those).
- `vectors.dat` (~43.86GB uncompressed) — the SAME papers' embeddings as raw concatenated
  little-endian float32, **no framing between rows** (exactly `dims*4` bytes per row, row
  `i` aligned to `papers.csv` row `i`). Confirmed integer-exact for the real file: 3,569,548
  data rows, `vectors.dat` size exactly `3,569,548 × 3072 × 4` bytes.

`trialerror lit arxiv-index build` auto-detects this csv+dat layout from the member names (no
config knob needed) and reads both members as two concurrent streams — see
`trialerror/arxiv_index/ingest.py`'s own module docstring for the full mechanics (streaming,
resume, and the row-count integrity check it enforces at completion). The original
jsonl-based ASSUMED layout (`member_glob`, still below) is kept only as a fallback for a
differently-shaped file and for this build's own offline test fixtures — the real download
does not use it.

**No title/abstract in the index yet**: since `papers.csv` carries no title/abstract, a
query's results come back as arxiv ids + distances only. Hydrating titles for a query's
top-`k` (via the existing keyless `trialerror.litapi.providers.arxiv.ArxivProvider`, no API key
needed) is a small follow-up, not yet built — flagged as an open seam in
`trialerror/arxiv_index/ingest.py`'s own module docstring.

**Disk**: this machine needs **≥80GB free** before starting (`trialerror lit arxiv-index build`
refuses below that — a hard preflight gate, not a warning). The zip itself never fully
extracts (streaming ingest, `zipfile` member reads only) — budget the 34.9GB download plus
headroom for the destination index db (roughly the same order of magnitude as the zip, since
raw vectors dominate the payload either way).

1. **Create a Kaggle account** (free) at `kaggle.com` if you don't have one, then create an
   API token: **Account settings → API → Create New Token** — this downloads
   `kaggle.json`. Place it at `~/.kaggle/kaggle.json` (the Kaggle CLI's own default lookup
   path).
2. **Download the dataset zip** — either works:
   - Kaggle CLI: `pip install kaggle` then
     `kaggle datasets download -d tomtum/openai-arxiv-embeddings -p <download-dir>`
   - Manual browser download: `https://www.kaggle.com/datasets/tomtum/openai-arxiv-embeddings`
     → Download button (needs the free account from step 1, no payment).
3. **Build the index**:
   ```console
   trialerror lit arxiv-index build --zip <download-dir>/openai-arxiv-embeddings.zip --program-root .
   ```
   Runs in-process by default (Ctrl+C-safe — re-run the exact same command to resume; it
   picks up from the last committed batch via the jobs ledger's checkpoint, never
   reprocessing already-indexed rows twice). Add `--detach` to run it as a background worker
   instead (`trialerror jobs logs <job-id>` to follow it). **Duration estimate**: this build's own
   offline synthetic-fixture tests run in well under a second at a few dozen rows; the real
   ~2.7-2.9M-row / 34.9GB corpus was never run end-to-end by any agent session (no
   credentials to do so) — expect a genuinely long batch job (likely low hours, dominated by
   zip decompression + insert throughput, not network or GPU), and budget accordingly before
   walking away from it unattended for the first run.
4. **Query it**:
   ```console
   trialerror lit arxiv-semantic --q "retrieval-augmented generation evaluation metrics" --k 10
   ```
   Requires `[litapi.arxiv_index].api_key_path` pointed at a file holding your OpenAI API
   key (query-time embedding only — the corpus vectors are already precomputed, this never
   re-embeds the dataset). Cost is one `text-embedding-3-large` call per query (a few tens of
   tokens, a small fraction of a cent at $0.13/1M input tokens) — `arxiv-semantic`'s own
   output reports the estimated cost alongside results.
5. **`trialerror doctor`** now reports an `arxiv_index_ready` row (absent/building/ready, row
   count, dims sanity) once you've run step 3.
6. **Weekly refresh**: the dataset's own Kaggle page updates roughly weekly (per its
   `dateModified`/version-counter metadata). Re-running step 2 for a fresh zip and step 3
   against it is additive/idempotent (existing rows are skipped, not re-inserted) — there is
   no separate "diff/delta" mode in this build; a full re-run against the newer zip is the
   supported refresh path.

**If the real download's file format doesn't match the CONFIRMED csv+dat layout above**
(e.g. a future weekly refresh changes shape — unlikely but not verified against every
possible future version): `trialerror lit arxiv-index build` fails loudly and immediately
(`ArxivIndexIngestError`/`SchemaAssumptionError`) rather than silently indexing garbage —
either at the upfront `vectors.dat` size-vs-`dims*4` check, on the first csv row that can't
be parsed, or at the final row-count integrity assertion (`trialerror/arxiv_index/ingest.py`'s
module docstring covers all three). `db_path` is the one config knob that's always safe to
change:

```toml
[litapi.arxiv_index]
db_path = "data/arxiv_index.sqlite3"  # gitignored; relative to program_root unless absolute
member_glob = "*.jsonl"    # only consulted as a FALLBACK when the zip has no papers.csv +
                            # vectors.dat pair at all (the ORIGINAL assumed jsonl layout,
                            # see trialerror/arxiv_index/ingest.py's module docstring) — irrelevant
                            # for the real download, which always uses the csv+dat layout.
```

If the real files turn out to be a genuinely different FORMAT (parquet, a different column
layout, a different vector wire format) rather than just a renamed member, that's a small
follow-up build to `trialerror/arxiv_index/ingest.py`'s csv+dat branch, not a config change — flag
it back to your Claude Code session with the actual file listing/header.

## 4. GPU and live-Claude-Code steps — need your real machine

These eight items cannot be completed by any agent working in a sandboxed session —
they require your actual GPU and an actual live Claude Code session with this plugin
installed. `trialerror accept` enumerates all eight automatically on every run (as `skip`
entries, never silently omitted) so you always know what's outstanding:

```console
trialerror accept
```

**Live Claude Code round trips** (install the plugin — `claude --plugin-dir
<path-to-plugin>` — and the two MCP servers first; see `docs/OPERATOR_GUIDE.md`):

1. **SessionStart round trip** — start/resume/`\clear`/`\compact` a real session and
   confirm the boot bundle actually appears as injected context.
2. **`PreToolUse:Task` spawn-gate firing** — invoke the `Task` tool without a booked
   `launch_id:` token and confirm Claude Code itself surfaces the exit-2 refusal to the
   agent.
3. **`Stop`-hook close check** — leave a launch dangling or the digest stale, then stop
   (or let the session end), and confirm it blocks once with the checklist (and allows a
   second stop).
4. **Task-matcher wiring** — confirm the `PreToolUse` hook fires only for `Task` calls,
   never `Bash`/`Read`/etc., in a real session (not just the script's own internal guard).
5. **`trialerror-knowledge` MCP smoke** — register it in a real session and confirm all 11
   tools are actually offered to and callable by a live agent.
6. **`trialerror-ops` MCP smoke: book → spawn → reconcile** — call `book_launch` via the MCP
   tool, spawn a real `Task` with the returned `launch_id` (exercising item 2 above live),
   then `reconcile_launch`.

**GPU backend verification** (needs the local models from §1 above, actually installed):

7. **`RealMarkerOcrBackend` against a real scanned PDF** — set `[ingest.ocr]
   backend="marker"`, ingest an actual scanned-image PDF (not the fake backend's
   form-feed-delimited text stand-in), confirm OCR output and page anchors are correct.
8. **`RealQwenEmbedBackend` against the real embedding venv** — set `[ingest.embed]
   backend="qwen3-4b"`, ingest a real document, confirm embeddings are produced and
   indexed correctly (matryoshka 2048, instruction-aware).

None of these eight block using TrialError today — v0's fake backends and the offline
subprocess test suite cover everything else. They're the honest remainder between
"tested" and "verified live," and they're the reason `trialerror accept`'s summary always
carries 8 `skip` entries alongside its real pass/fail checks until you've personally
worked through them on this machine.
