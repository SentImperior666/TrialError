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

## 1a. Two-machine split — the program on one box, the GPU on another

Skip this section entirely if the machine running your program is the machine with the GPU.

It exists for the other shape: the program lives somewhere always-on and CPU-only, and the GPU
is in a laptop that is off most of the time. Section 1's config cannot express that — the
paths there are per-machine, and neither machine can call the other on demand. `backend =
"offload"` is the answer: the CPU box queues the work, the GPU box drains the queue whenever it
happens to be on.

### What each side's config says

**The program's box** (call it the *queue side*) — its `trialerror.toml`:

```toml
[ingest]
require_real_backends = true      # see "Fail-closed" below

[ingest.ocr]
backend = "offload"
expect_backend = "marker"         # optional: pin what the worker must report back

[ingest.embed]
backend = "offload"
model_key = "qwen3-4b"            # REQUIRED - emb rows are keyed by it, so it is never defaulted
dims = 2048
```

**The GPU box** keeps a program root of its own whose `trialerror.toml` is an ordinary section-1
config naming the real local installs (`marker_single_exe`, `python_exe`, `module_dir`). That
program root is not a second copy of your research — it is only how the worker learns where your
models are. Nothing is written into it.

### The restricted key (you generate it; no agent ever handles key material)

On the GPU box:

```powershell
ssh-keygen -t ed25519 -f ~/.ssh/te_offload -N ""
```

On the queue box, copy `deploy/sandbox/offload-shell.sh` next to the program (edit its
`TE_OFFLOAD_ROOT` if the program root is not the default), `chmod 700` it, and append ONE line to
`~/.ssh/authorized_keys`:

```
restrict,command="/home/<you>/offload-shell.sh" <contents of te_offload.pub>
```

That wrapper is the security of the key. It accepts only
`list | claim | pull | push | publish | return | heartbeat`, validates the job id against
`[A-Za-z0-9._-]+` (refusing `.` and `..`), reads a pushed archive's member list BEFORE extracting
anything and refuses any member that is not a flat regular file, caps one transfer at 2 GB in each
direction (`TE_OFFLOAD_MAX_PUSH_BYTES` / `TE_OFFLOAD_MAX_PULL_BYTES` — over the cap the verb fails
rather than truncating), and confines every path to the queue directory. `restrict` removes
forwarding, PTY allocation and `~/.ssh/rc` on top.

**Do not let the GPU box choose the wrapper's environment.** `TE_OFFLOAD_ROOT` decides which
directory the key can reach at all, so it must come from the queue box and never from the client:
keep every `TE_OFFLOAD_*` name out of `sshd_config`'s `AcceptEnv`, leave `PermitUserEnvironment`
off, and write no `~/.ssh/environment` for that account. If you do not control that SSH server's
configuration, hard-code the paths as literals in your copy of the script instead of leaving the
`${TE_OFFLOAD_*:-...}` defaults in place.

Verify both halves before you rely on it:

```powershell
ssh te-offload list      # exit 0, empty on an empty queue
ssh te-offload bash      # must fail: "offload-shell: unknown verb 'bash'"
```

Finally, on the GPU box, `~/.ssh/config`:

```
Host te-offload
    HostName <queue box>
    User <you>
    IdentityFile ~/.ssh/te_offload
    IdentitiesOnly yes
```

### Running it

```powershell
trialerror offload worker --remote te-offload --program-root C:/path/to/dev-program
```

It claims each queued job, pulls the inputs, runs marker/Qwen3 locally, pushes the outputs,
publishes, and exits with **"Queue empty - safe to switch DEV off"**. Add `--stay` to keep
polling. A second copy refuses immediately (a single-instance lock — two workers would fight over
the GPU). Ctrl+C returns the current claim; closing the lid cannot, which is why the queue side
returns any claim whose heartbeat has been silent for 60 minutes (`trialerror offload reclaim`).

On the queue side, two commands belong in whatever loop already runs `trialerror jobs tick`:

```bash
trialerror offload reclaim   # return claims from a worker that went away
trialerror offload kick      # un-delay a parked job whose result has landed; sweep finished ones
```

`trialerror offload status` prints the counts. The dashboard's HOME "what needs a human" panel
carries the same line: *"N documents wait for the DEV GPU"*.

### What waiting costs (nothing) and what failing costs (a bounded amount)

A parked stage fails *environmentally*, so its retry budget is never consumed: three weeks with
the GPU box switched off leaves every job at `attempts = 0`. A GPU failure is different — it is
counted in the marker as `offload_attempts`, and after three of them the job lands in
`offload/failed/<job_id>/` with an `error.json` and the ledger row settles `abandoned`. That
split is deliberate: an absent machine is not a failure, and a document that crashes marker every
single time must stop consuming GPU minutes.

### Fail-closed (why `require_real_backends = true` is worth setting)

The failure this guards against is silent. A one-character typo in a table name
(`[ingest.embeded]`) leaves a program that looks configured, runs without error, and writes
hash-derived 16-dimensional stand-in vectors into a knowledge store whose whole purpose is
retrieval — and nobody notices until search quality is quietly wrong, months of ingest later. So:

- an unparseable `trialerror.toml` now raises instead of falling back to the defaults;
- with `require_real_backends = true`, an absent or `backend = "fake"` `[ingest.ocr]`/
  `[ingest.embed]` table is refused at load time;
- the worker refuses to start against a program root configured for fake (or offload) backends;
- a published result that reports a fake backend, the wrong model key, the wrong dimensionality,
  a mismatched chunk list, a different config hash, or a payload whose sha256 does not match is
  quarantined in `offload/failed/` instead of being folded into the record;
- `trialerror doctor --only fake_backend_rows` finds fake rows already in a program that declared
  it would not accept them.

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
- inside the research sandbox nothing to paste: `deploy/sandbox/te-boot.sh` writes this
  `statusLine` key into the container user's settings on first boot (idempotent) and the
  image already sets `TRIALERROR_QUOTA_DIR`; note the feed exists only in interactive
  terminal sessions -- the Claude desktop app's Code tab never runs the statusLine
  command, so a desktop-driven session stays on screenshots

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

## 3f. Optional: web-page → corpus ingestion (`trialerror webfetch`)

**Off by default, and turning it on is a decision about egress rather than a
formality** (C-0069's disabled-by-default gate). Read this whole section before you
add the line.

### What it does

You give it a URL — or a markdown file full of them — and it puts the article's text
in the corpus: cleaned of navigation, adverts and scripts, with the provenance you
would need to cite it (final URL, fetch time, content hash, robots verdict, declared
licence) and quote anchors that keep resolving. PDFs and GitHub repositories reached
from a links list work too. Anything it *cannot* lawfully fetch — a paywall, a bot
wall, a page that only exists after JavaScript runs — ends as a `wanted` row in
`requests/REQUESTS.md` for you to deliver by hand through the path you already use.
Nothing is ever bypassed: there is no stealth browser, no CAPTCHA solver and no
user-agent spoofing anywhere in this code, by ruling and by design.

### The shape of it, in one paragraph

Fetching happens in a **separate process from everything else**. The half with network
access never sees the corpus or any secret; the half that parses the page — which is
where hostile input actually lands — has no network at all. They share one directory
and nothing else: a request goes in as a small JSON file, the bytes come back beside a
provenance record, and the research side re-checks the size and hash of every byte
before one of them reaches the corpus. On the sandbox those halves are two containers.
On a workstation they are two commands in two terminals, which is what makes the whole
pipeline testable before any image is rebuilt.

### Turning it on for a program

```toml
[webfetch]
enabled = true                      # default false — this line IS the decision
queue_dir = "/workspace/webfetch"   # shared with the fetch process; a relative path
                                    # is joined onto the program root
contact_mailto = "you@example.org"  # goes in the User-Agent: C-0069 says identify
                                    # honestly, so this is a real address of yours
honor_tdm_optout = false            # record noai/TDMRep signals but do not enforce
                                    # them (the internal-research posture; every
                                    # signal is written to the row either way)

[license]
# Both routes are required: `web` for a page that was fetched, `user_delivered` for
# the `wanted` row a refusal leaves behind. Without them the fetch works and the
# bookkeeping refuses, which is a confusing way to find out.
allowed_acquisition_routes = ["web", "user_delivered", "api", "author_posted"]
```

On the sandbox, add `sandbox = true`. That switches the config loader to fail-closed:
`mode` must be `"allowlist"`, `require_sidecar` must be true, and `contact_mailto`
must be set, or every verb refuses and says which rule it broke. This is deliberate —
the failure it prevents (a mistyped posture quietly widening what may be fetched) is
otherwise silent.

**Which hosts may be fetched is not in this file and cannot be.** It lives in
`webfetch/policy/allowed-hosts.conf` on the host machine, one exact fully-qualified
name per line, mounted read-only into the fetch process and not visible from the
research container at all. That is the point: an agent inside the sandbox can *ask*
for a host and cannot approve one, so a prompt-injected agent cannot name a machine to
send data to. You approve hosts with `te-webfetch.sh allow <host>` or, for a delivered
list, `te-webfetch.sh import-list <file>`, which shows you the distinct hosts first.

### Running it

Two terminals. The fetch loop — the container's entrypoint on the sandbox, a plain
command on a workstation:

```console
trialerror webfetch sidecar --foreground --queue ./webfetch-queue --policy ./webfetch/policy
```

and the program itself:

```console
# see what a delivered list contains, and which hosts it needs, without touching anything
trialerror webfetch batch --list "deliveries/Requested links.md" --launch-id LNCH-… --dry-run

# enqueue it (idempotent — re-running the same list enqueues nothing)
trialerror webfetch batch --list "deliveries/Requested links.md" --launch-id LNCH-…

# one URL at a time
trialerror webfetch add --url https://example.org/article --launch-id LNCH-… \
    --license-tier open        # your judgment; outranks whatever the page claims

# the jobs worker does the actual work, on its own loop
trialerror jobs start-worker --mode once

# where everything is, and what it became
trialerror webfetch status --list "deliveries/Requested links.md"
trialerror webfetch report --list "deliveries/Requested links.md"
```

`report` is the one to read, and the number that matters is `unaccountedFor`. A run is
finished when every link is either an indexed document or a refusal carrying a reason —
not when some particular number of documents exists. Two links out of eight ending in
`wanted` is a normal, correct outcome for a list containing a paywalled newsletter and
a JavaScript-rendered marketing site.

### The other three verbs

- `trialerror webfetch links <fetch_id>` — the links that page pointed at. They are
  **recorded and never followed**: there is no crawling here, and a link you want
  comes back through `add`, which re-runs every check from the start.
- `trialerror webfetch refresh --all --older-than 30d --launch-id LNCH-…` — a
  conditional re-fetch, never automatic. A `304`, or an unchanged article after
  cleaning, costs one request and produces no new document; a changed page becomes a
  **new document under the same source**, and the old chunks and anchors are never
  mutated.
- `trialerror webfetch proposals` — hosts an agent asked for and you have not
  approved. Reading them is all this side can do; `te-webfetch.sh review` on the host
  is where you decide.

### When an unattributed fetch is one you made on purpose

`trialerror doctor` fails on a fetch naming a launch nobody booked, and the trail it
reads is append-only — nothing on the research side may edit or truncate it, because a
trail its own suspect can rewrite is not a trail. So the deliberate test of that alarm
(write a request naming a made-up launch, watch both surfaces go red) would otherwise
leave the check red for the life of the program, which is exactly how an operator learns
to ignore a category. `trialerror webfetch ack --fetch-id WF-… --launch-id LNCH-… --note
"why"` is the answer, and it is an addition rather than a deletion: it records that a
named person, working under a launch that *is* booked, has accounted for that id. The
audit line stays exactly where it is, the offender keeps appearing in the check's
details with your note attached, and `trialerror webfetch acks` lists the current
acknowledgement for every id — with a count of how many times each has been re-signed,
because re-acknowledging replaces the row and the earlier signature then lives only in
the `webfetch_ack` event log. The host machine has the same verb for its own
authoritative line — `te-webfetch.sh ack <fetch_id|job_id> [note]` — and you want both,
because they are two independent records read by two independent checks.

**An acknowledgement is a boundary, not a switch.** It covers the fetches already on
record when you signed and nothing that arrives afterwards, so a new unattributed fetch
turns the check red again whether it carries a new id or *reuses the one you
acknowledged* — the second case is called out by name ("arrived AFTER the
acknowledgement"). That bound is what keeps the feature honest, because the acknowledged
ids are printed: they are in the green line and in `webfetch acks`, so anything that can
read a doctor run can see which ids have been signed for, and without a bound signing
for an id would hand out a permanent exemption for it. To cover a genuinely new line
under an id you already know about, acknowledge it again — a second deliberate,
attributed, logged act, not something the first one granted in advance. On the host the
same bound is a `covers=N` count on the `acknowledged.conf` line (the host log carries no
per-line timestamp to compare against); `te-webfetch.sh ack` writes it, says out loud
when it is widening one, and reports a hand-written entry that has none as ignored.

### What none of these commands will ever print

A line of a fetched page. Every verb returns identifiers, counts and reasons; the text
lives under `raw/web/<host>/` and reaches an agent only through retrieval, where the
existing quote fence applies. That is not tidiness — it is what stops a page saying
"ignore your previous instructions" from being read aloud into the context of the
agent that fetched it.

### Two honest caveats

- A fetch is attributed to a launch id, and the harness *detects* a bogus one rather
  than preventing it: any process inside the research container can write a
  syntactically valid launch id into a request. What bounds the damage is the host
  allowlist and the daily caps, not the attribution — and `trialerror doctor` and
  `te-status.sh` both flag an unattributed fetch within one cycle. One you made on
  purpose is retired with `webfetch ack` (above), never by editing the trail.
- Pages tagged `unknown` (the default, when neither you nor the page says otherwise)
  are served **unfenced** by the retrieval layer, consistent with the internal-research
  posture. Tag commercial sources with `--license-tier commercial_restricted` and the
  existing ≤20-word excerpt fence applies to them.

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

Items 1-4 above are exactly what the deployment handover gate's live-session phase
discharges when you run it in a container: `trialerror accept --suite e2e` enumerates
those human steps beside the automated ones, each with the exact command and the
criterion it is judged against.

None of these eight block using TrialError today — v0's fake backends and the offline
subprocess test suite cover everything else. They're the honest remainder between
"tested" and "verified live," and they're the reason `trialerror accept`'s summary always
carries 8 `skip` entries alongside its real pass/fail checks until you've personally
worked through them on this machine.
