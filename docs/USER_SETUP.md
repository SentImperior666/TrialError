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
| | `max_range_pixels` (optional) | **Page-range chunking.** The pixels of page raster ONE `marker_single` invocation may hold; a document bigger than that is run in ranges (`--page_range A-B`) and the pages are concatenated with absolute numbers. Default `64000000` — A4 at `bounded_dpi` is 3,562,596 px, so with a 1.1 safety factor that is **16 pages per range**; in bytes, `64,000,000 × 3` = 192 MB at 3 bytes per pixel, of which the safety factor reserves about a tenth. `0` means no bound: one invocation for the document, which is the behaviour a 540-page scan measured at ~58 GB of commit. **Every range is another `marker_single` process and another model load**, so a large-format scan at this default can be hundreds of invocations — raise the budget, or lower marker's render DPI, knowingly. **That byte figure is a LOWER bound on one component, not the invocation's memory**: it counts page raster only, and against a real run it under-states the process by two orders of magnitude. **Size it from a measurement.** As one machine's measurement (marker-pdf 1.10.2, a 16 GB-GPU laptop, ~1500 × 2300 pt pages planned at 192 dpi): a fixed **~14.5–16 GB per invocation** (models + CUDA context) plus **~0.22–0.25 GB per page**, whole-machine peak commit **34–35 GB** on 16-page ranges and **32.6 GB** on 8-page ones, at ~7–8 pages a minute including the reload. The procedure: run one small range, read the peak commit off the worker's per-range log line (`range_wall_s`, `peak_rss_bytes`) or the result manifest, derive `pages = (budget − fixed) / per-page`, then set `max_range_pixels = pages × page_pixels_at_planning_dpi`. Below about **30 pages a range the fixed cost dominates**, so very small ranges buy little memory and cost many reloads. See the operator guide's "OCR page-range chunking". |
| | `bounded_dpi` (optional) | The DPI the planner estimates a page's pixel area at. Default `192`. It is the harness's own arithmetic and is **not** passed to marker, so it must be **at least** the DPI your stack really renders at: marker rasterises every page of a range at `highres_image_dpi` (marker 1.x default **192**, alongside a 96-DPI low-res pass) and holds them all at once. Page area goes as DPI squared, so planning at 96 against a 192 render is four times too generous and the bound stops bounding — which is how a real scan died with a `MemoryError` inside marker's own rasteriser. If you pass `--highres_image_dpi N` in `marker_extra_args` the planner uses `max(bounded_dpi, N)` by itself: raised, never lowered. |
| | `page_range_numbering` (optional) | Which convention your marker release numbers `--paginate_output`'s `{N}` markers by under the range flag: `"absolute"` (the page's own index in the document — **what marker-pdf 1.10.x does**), `"relative"` (counted from the start of the invocation), or `"auto"` (the default), which resolves it once per document from the ranges' own output and refuses by name rather than guessing. Only needed if a document cannot decide itself, or to pin a release you already know. |
| | `page_range_flag` (optional) | The flag your `marker_single` takes a page range on. Default `--page_range`. Probed once against `--help` before the first chunked run and refused by name if absent — a range flag that is silently ignored turns each of N ranges into a full-document run. A release that renames it is a one-line change here. |
| `[ingest.embed]` | `backend = "qwen3-4b"` (or any name — anything other than `"fake"` routes to the real backend) | |
| | `python_exe` | Absolute path to the Python interpreter **inside the venv that has the embedding model's dependencies installed** (torch/sentence-transformers) — this process itself never needs those installed |
| | `module_dir` | Directory containing `embed_backend.py` (the `load_backend(name).embed_batch(...)` module this shells out to) |
| | `dims` (optional) | Defaults to `2048` (the matryoshka-truncated dimension) |
| | `session` (optional) | Defaults to `true`: ONE driver process per backend, started on the first batch and reused for every later one, so the model loads once instead of once per batch. `false` restores the one-process-per-batch protocol — an escape hatch for a driver that dislikes being long-lived, not a tuning knob. |
| | `batch_size` (optional) | Defaults to `8` on a single-machine program. With `session = true` this is a GPU batch size, not a model-load budget, so raise it if the card has room. (The two-machine worker has its own `--batch-size`, default `64`.) |
| `[ingest.embed.query]` | `backend` (optional) | **Only needed if the document side cannot embed HERE** — i.e. `[ingest.embed] backend = "offload"`, where the model runs on another machine. A search still has to embed the query in this process. `"same"` (the default) uses the document backend; **`"llama_server"`** talks to a long-lived `llama-server` sidecar on loopback and is the production choice on a CPU-only machine — it needs `tokenizer_model_path` (the GGUF whose *vocabulary* pre-truncates the input; opened `vocab_only`, so no model weights are loaded in this process) plus optional `url` (`http://127.0.0.1:8871`), `timeout_s` (60), `n_ctx` (**2049** — one wider than the geometry, because the server refuses a request of exactly its context length; the doctor line refuses a sidecar whose own `-c` is narrower than this, since that combination answers short queries and fails long ones mid-run), `native_dims` (2560), `query_prompt`, `sidecar_name` (which `[sidecars.<name>]` serves this URL, so a refusal names the verb that starts yours), and a `[sidecars.<name>]` table to start it with; `"llama_cpp"` runs the same GGUF encoder **in this process** with `model_path` (plus optional `n_ctx`, `n_threads`, `n_threads_batch`, `pooling`, `native_dims`, `query_prompt`) and is the reference path — leave `n_threads_batch` unset unless you know better than `min(n_threads, <cgroup CPU quota>)`, which is what it defaults to and what the difference between a 3-second and a 23-second query turned out to be — a positive value is honoured verbatim, and `0` or a negative one is refused rather than read as "unset" or handed to the library, where a non-positive thread count means "size the pool from every visible CPU"; `"offload"` states "nothing embeds here" explicitly. `model_key`/`dims` are INHERITED from `[ingest.embed]` and refused if you state them differently — a query vector from another key ranks your corpus in an order that means nothing. `trialerror doctor --only query_embed_backend_runnable` tells you whether yours works, and `trialerror query search` says in its envelope's warnings when it had to fall back to the full-text tier. |
| `[sidecars.<name>]` | `command` (required) | **The argv of a process this program needs RUNNING** (today: the embedding server the `llama_server` query backend talks to). A LIST of strings, never a string — a string would have to be split by a shell, and a config that reaches a shell can reach a pipeline. `trialerror sidecar start <name>` is the only thing that reads it; there is no `--cmd` flag anywhere. |
| | `env` (optional) | A table of environment variables merged over the current ones — a vendored runtime usually needs `LD_LIBRARY_PATH` here. Only the KEYS are ever reported back. |
| | `cwd` (optional) | Working directory for the process (default: the program root). |
| | `health_url` (optional) | Probed by `sidecar status` and the `sidecar_alive` doctor check; a 2xx is healthy. Without it, the only thing either can report is that the pid is alive — which is what they say, rather than calling it healthy. `health_timeout_s` (5) bounds the probe. |
| | `restart` (optional) | `"never"` (the default) or `"always"`. `"always"` means **`sidecar status` restarts it if it finds it dead** — supervision here is a poll, so the supervisor is whatever already runs on a loop and calls `status`; nothing claims to watch from a process that has exited. |
| `[ingest.quality]` | the four bounds (all optional) | **Extraction quality — a health signal, not a gate.** `glued_token_rate_max` (0.10), `unusable_chars_max` (200), `terminator_density_min` (1.0), `chars_per_page_cv_max` (1.5) bound the four measures a document's extracted text is scored on; `min_tokens` (200) is the size floor: a document shorter than that is measured and reported (`below_min_tokens`) but never counted `suspect`, because three of the four measures are rates whose denominator is the document's own text and a twelve-token note trips them by arithmetic rather than by bad extraction (`0` turns the floor off; it does not affect `refuse_below`). `worst_n` (10) sizes a report and `sample` (50) / `seed` (0) size and fix the sample the `extraction_quality_suspect` doctor check draws each run (`sample` is clamped to at least 1 -- the check samples by design, and "measure everything" is spelled `trialerror ingest quality --all`, not `sample = 0`/`-1`). Nothing here refuses anything: read the numbers with `trialerror ingest quality --doc-id ...` or `--all --worst 10`. See the operator guide's "Extraction quality" section for each measure's denominator. |
| | `refuse_below` (optional) | **Absent by default, and the only knob here that can stop an ingest.** An inline table of the same bound names (e.g. `refuse_below = { glued_token_rate_max = 0.35 }`); only the measures you name are compared, and an empty table reads as unconfigured. When a document's text is worse than a stated bound the normalize/OCR stage writes `document.status = 'failed'`, does not enqueue the chunk stage, and records the numbers and reasons (`ingest status --doc-id` reads them back). Its elements and archived text are kept so you can see what was refused, and `ingest rechunk`/`re-embed` are refused for it (they would re-derive exactly what the refusal withheld); relax the bound and re-run the extraction stage, or fix the route and re-ingest. A `trialerror.toml` that cannot be parsed fails the job rather than quietly not refusing, and so does a bound written as something that is not a number (`terminator_density_min = "eight"`) -- this one value is never replaced by a default. |
| `[budget]` | `quota_max_age_s` (optional) | **How old the plan-quota capture may be before a booking refuses against it.** Default `900` (15 minutes). The capture is written by the statusLine script on a Claude Code UI tick, so the reading goes stale exactly while a session sits idle — which is also when it is most likely to be consulted before sizing a booking. `trialerror budget quota` reports the standing and the age; `trialerror doctor --only quota_capture_stale` warns; and **both booking surfaces refuse** on a stale reading — `trialerror budget book` and the `book_launch` MCP tool — unless `--allow-stale-quota` / `allow_stale_quota` is passed, which books anyway and records the reading it overrode on the launch. Nothing captured is *not* stale: a program that has not wired the statusLine has no reading to be out of date, and refusing its bookings would make an optional feed mandatory by accident (screenshot snapshots remain the ground truth either way). `--fresh-within-s` on `budget quota`/`budget check` overrides this for one reading. |

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
complaint, in the wrong place. Every `[paths]` key (`stores_dir`, `index_dir`, `run_dir`, `archive_dir`,
`law_digest_path`, `handoffs_dir`, `requests_path`, `memory_dir`, `ingest_roots`) now
raises a `ConfigError` naming the mismatch rather than resolving it silently; the three
`[ingest.*]` paths above are handed straight to the OS, so a wrong-platform value there
surfaces as a job failure on a missing executable instead.

**A `handoffs_dir` outside the program root is refused.** `session close` renders the
handoff AND marks the session closed, so a `[paths].handoffs_dir` that resolves outside
this program's root would write one program's close into another program's tree. That is
what a `trialerror.toml` copied from another program gets wrong, so it is an opt-in:

```toml
[paths]
handoffs_dir = "/srv/research/closes"   # absolute, outside this program

[session]
handoffs_dir_outside_root = true        # required for the line above; default false
```

Without the flag, `session boot` and `session close` both refuse with
`handoffs_dir_outside_root` and nothing is written. Leave `handoffs_dir` relative (the
default, `handoffs`) and the key never applies.

**Why "your existing" tools**: the design ports the operator's own already-proven local
`marker_ocr`/`embeddings_local` tooling rather than reimplementing OCR or embedding —
if you don't already have a working `marker_single` install and an `embed_backend.py`
module for a local Qwen3 embedding model, that installation is out of scope for this
harness and needs to happen first, on its own terms.

**Status honestly**: neither real backend has been run against a live GPU on this build —
`RealMarkerOcrBackend` has a test that self-skips without `marker_single` on PATH.
`RealQwenEmbedBackend`'s *protocol* is covered end to end against a stand-in driver that
speaks it (startup count, per-request timeout, crash-then-restart, shutdown), but no test
here has ever loaded the actual model. The first real ingest you run with these configured
*is* the live verification of the model half.

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
trialerror offload worker --remote te-offload --backend-config-root C:/path/to/dev-program
```

`--backend-config-root` is the root whose `trialerror.toml` names your local marker/Qwen3
installs; the queue comes from `--remote` (or `--queue-root` for a queue on this same box), never
from that root. `--program-root` is still accepted as a deprecated alias for it.

It claims each queued job, pulls the inputs, runs marker/Qwen3 locally, pushes the outputs,
publishes, and exits with **"Queue empty - safe to switch DEV off"**. `trialerror offload doctor
--program-root <root>` is the pre-flight: it says which root was read, what each stage names, and
whether it resolves here. Add `--stay` to keep
polling. A second copy refuses immediately (a single-instance lock — two workers would fight over
the GPU). Ctrl+C returns the current claim; closing the lid cannot, which is why the queue side
returns any claim whose heartbeat has been silent for 60 minutes (`trialerror offload reclaim`).

**The worker holds the model.** One embedding-driver process is started on the first batch
and reused for every batch of every job in the run — the model is loaded once, not once per
batch. (It used to be once per batch: a 217-chunk document at `--batch-size 8` meant 28
loads of a multi-gigabyte model to do 28 batches of real work.) So `--batch-size` is now a
GPU batch size and defaults to **64**; lower it only if the card runs out of VRAM. In
`--format text` output you should see `driver started` **once** near the top and a
`ran in <n>s` line per job — that pair is how you confirm from the log alone that the model
is not being reloaded. A second `driver started` means the driver crashed and the next job
restarted it; the failure line just above it carries the driver's own stderr. The driver is
closed when the run ends, so nothing keeps holding the GPU after the queue-empty message.

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

Terminal is not permanent, though — it is just *terminal until a human decides otherwise*. Once
you have fixed whatever the GPU was choking on and deployed it to the worker,
**`trialerror jobs retry <job_id> --reason "<what was fixed>"`** on the sandbox puts the marker
back in `pending/` with `offload_attempts` reset and the ledger row back to `pending` with its
own attempts reset.
The failed attempt is kept, not deleted: `offload/failed/<job_id>/` moves to
`offload/failed/_retried/<job_id>.<stamp>/`, which `offload status` counts under `retried` and the
`offload_failed` doctor check does not fire on. See the operator guide's *Detached jobs* section
for the refusals and the two-machine recipe.

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

**Per stage, since FB-1 item F5.** `[ingest.ocr] require_real` and `[ingest.embed] require_real`
each default to the global `[ingest] require_real_backends`, so nothing written before these keys
existed resolves any differently — and a program with a real embedder and no OCR stack (or the
reverse) no longer has to choose between refusing every ingest and losing the guarantee on the
stage it *can* run:

```toml
[ingest]
require_real_backends = true   # the program-wide declaration

[ingest.ocr]
backend = "fake"
require_real = false           # ...except this stage, deliberately
```

The key lives inside the stage's own table, so a stage whose table is absent cannot be exempted
that way — an absent table is itself one of the two refused conditions. The query-side
`[ingest.embed.query]` table follows the EMBED stage's resolved requirement, not the global flag.
`fake_backend_rows` follows the same resolution: fake embeddings fail only where the embed stage is
required to be real, fake OCR only where the OCR stage is, and each half of the message names the
key that decided it.

### `[ingest] fulltext_before_embed` — search before the GPU run

```toml
[ingest]
fulltext_before_embed = true    # the DEFAULT; set false for the old chunk -> embed -> index chain
```

The pipeline runs chunk → embed → index, so on a program whose embed stage is parked for a
GPU window — `backend = "offload"`, or a queue held until the machine is free — nothing
ingested since had any full-text search: the text was in the store and the one stage that
puts it in `chunk_fts` was queued behind a stage that needs hardware.

With this on, the `chunk` handler enqueues a **full-text-only** `index` job
(`JOB-ingest-<doc>-index-fulltext`) beside the `embed` one. It writes `chunk_fts` and the
tantivy index and touches no vector table, no `emb` row and no model — which is what makes
it safe to run before a single embedding exists — and it does **not** advance
`document.status`, because a row reading `indexed` with no vector would be the exact skew
the ingest doctor checks exist to catch. The vector side of `index` still runs after
`embed`, under its own model-keyed job. `trialerror doctor --only fulltext_index_stale` is
what reports the full-text side; `trialerror ingest reindex-fulltext` is the repair.

Set it `false` only if you want the pre-FB-6 ordering back; there is no cost to leaving it
on, since the full-text pass is idempotent and the real `index` job re-runs over the same
chunks without duplicating a row.

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

## 3g. Optional: the judged novelty screen as a configurable instrument (`[lens.novelty]`)

Skip this section if your rounds judge records against the inventory and the corpus with
the design's own label vocabularies — that is the default and needs no config at all.

It exists for the other shape. A round that judges a **literature** rather than a mechanic
wants the judge to see the nearest **archive rows** (prior rounds' candidates and request
rows) instead of register rows, to label them in its own words, and to seed plants it
defines. Every knob below has a command-line flag that overrides it, so a one-off round
needs nothing here; the config is for the rounds a programme runs the same way every time.

```toml
[lens.novelty]
judged_sets = ["R2", "R4"]          # which reference sets the judge is shown and labels
labels_file = "rounds/labels.json"  # this round's own label vocabularies + canonical mapping
plants_file = "rounds/plants.json"  # the plants this round seeds
batch_fail_on = ["area"]            # which plant kinds' misses FAIL a batch
```

| Key | What it means | Default |
|---|---|---|
| `judged_sets` | The reference sets the judge is shown and returns a label for. `R2` is the archive of idea rows, `R3` the inventory, `R4` the corpus. R5 is evidence for the R4 label, not a labelled set of its own, and rides inside that bundle. An undeclared set is **absent** from every envelope, not empty, and no verdict row is written for it. | `["R3", "R4"]` |
| `labels_file` | A JSON file of this round's own label vocabularies and their canonical mapping onto the design's fixed ones. The judge is shown the round's spellings; each verdict row stores the round label **and** `label_canonical` beside it. | none — the design's own vocabularies |
| `plants_file` | The plants this round seeds, on top of (or instead of) the harness battery. | none |
| `batch_fail_on` | Which plant kinds' misses fail the batch. A miss on any other kind is reported and counted, never hidden, and does not fail. | `["inventory"]` |

A relative path is read **against the program root**, not the current directory: a config
row is a property of the program, and a round run from two different shells has to read the
same file. The matching flags are `--judged-sets`, `--labels-file`, `--plants-file` and
`--batch-fail-on`; each one wins over its config row.

The labels file, in full — every key optional:

```json
{
  "R2": {"labels": ["requested", "variant", "new"],
         "canonical": {"requested": "same", "variant": "variant", "new": "new-mechanism"}},
  "R4": {"labels": ["present", "adjacent", "absent"],
         "canonical": {"present": "stated", "adjacent": "adjacent", "absent": "absent"}},
  "extra": {"seed": ["on-topic", "off-topic"]},
  "unscreenable": "unscreenable"
}
```

**The canonical mapping must be total over the round's labels, and must land inside the
design's own vocabulary for that set.** Both are refusals by name. An unmapped label would
sit in a verdict row and be absent from every report of it; a canonical value no reader has
a column for defeats the one thing the mapping is for. A declared set the file says nothing
about keeps the design's vocabulary under an identity mapping, so a round can re-spell one
set without restating the others. A block for a set the round did **not** declare is a
refusal by name rather than a block quietly dropped: a set-name typo would otherwise leave
the judge on the design's words for the set it really is shown. The file is hashed onto the
batch, and recording labels against a vocabulary whose hash disagrees with the batch's is
refused — the judge answered in the vocabulary it was shown. The hash is computed over the
declared sets, so a labels file is resolved against the **batch's** own sets at
`--record-verdicts` time.

`unscreenable` — the round's word or the design's — is a valid answer for **every declared
set**, including one whose own `labels` list never offers it: the word says the record states
no mechanism to compare with anything, which is a fact about the record rather than a claim
about a reference set. It is scored as a non-catch for a plant, counted in κ as its own
category, and written with `label_canonical = unscreenable`. What the judge is SHOWN is still
each set's own list, so no round's envelopes change.

`extra.seed` re-spells the seed-work vocabulary; the **first** label is the on-topic one
(the same "strongest first" convention the design's own label tuples use). `unscreenable` is
the round's own word for a record with no statable mechanism, and it is what the seed-count
report looks for. A round that re-spells it must also **list that word in the `labels` of the
set it belongs to** (mapped onto `unscreenable`) — the word is only ever read back off an
answer, so one no declared set offers the judge would be refused at `--record-verdicts` and
the seed-count report would never fire. That is a refusal when the file loads. The design's
own spelling stays acceptable whatever the sets offer (the corpus vocabulary never offers it).

The plants file takes one lenient key, `extra`: any keys at all (`literature`, `unlock`,
`seeds`, …), rendered into a single `record.extra_text` field the judge sees, so a round may
put its own three fields on a plant in one place instead of folding them into the statement
by hand. **A round's own intake records take the same key**, rendered by the same function
into the same one envelope field (`idea.extra`, knowledge schema v11) — the envelope's shape
is what keeps a plant indistinguishable from a record, and a key one of them could not hold
was a tell.

A plant also takes two optional bookkeeping keys, neither of which a judge ever sees.
**`class`** is the round's own class for the plant (a `present`/`adjacent`/`absent` battery,
say): free text up to 40 characters, which groups the calibration card's `by_class` table.
It is NOT `kind` — `kind` says how the plant was BUILT and is one of `area`, `paraphrase`,
`inventory`, `custom`, and a file that spells a class there is refused once, naming every
offender and the four kinds. **`batch`** seeds a plant into one judged batch only: a plant
that declares one rides in the batch whose `--batch-id` matches it, one that declares none
rides in every batch as it always did, and a `--batch-id` no plant declares injects none of
the batched ones and says so in the batch's `warnings`. `--pair-ratings` is lenient the same way — a `pair_id` or a `why` beside
`a`/`b`/`human` is ignored and named in the calibration card's `warnings`. And an
`archived` intake row may omit its `probe` (or pass `null`); a candidate may not.

Bad values are `labels_file_refused` / `plants_file_refused` envelopes naming the offending
label or plant, before the screen embeds a single statement.

**Two flags with no config row.** `--reembed-archive` re-embeds every R2 archive row instead
of reading the per-model idea-vector cache (`vec_ideas`, knowledge schema v10): the cache is
keyed by the statement's hash, so a changed statement is re-embedded anyway and the flag is
for the case that key cannot see — a backend whose weights or pooling changed under an
unchanged model key. `--batch-id` names the batch a recording run reads, and that batch's
own `judged_sets` are then the authority: `--record-verdicts` and `--record-calibration`
need no `--judged-sets` at all, and one that disagrees with the batch is refused by name.

The full operator-facing model — the plants file's fields, the archive round, and
calibration mode — is in `docs/OPERATOR_GUIDE.md`, "Ideation rounds".

## 3i. Optional but recommended: the numpy fast path (`[retrieve] numpy_fastpath`)

```bash
pip install -e ".[fast]"     # or just: pip install numpy
```

Four places in this codebase score vectors by hand: the retrieval tier's cosine and ranking,
the lens stratifier's distance and candidate scoring, the document-vector pooler, and the
novelty screen's corpus, archive and inventory nearest-neighbour passes. Every one of them is
a Python loop over 2048-dimension vectors — fine on the few hundred rows a full-text
prefilter hands it, and hopeless on the hundred thousand an unbounded pass does. A 36-subject
calibration batch over a 108k-chunk corpus ran for more than ten minutes, almost all of it
spent building per-float Python objects rather than doing arithmetic.

With numpy importable, those scans decode the stored vectors straight out of their BLOBs with
`numpy.frombuffer` and score them in blocked float64 matmuls. On a 20,000 × 256 fixture the
whole BLOB → decode → score path measured **17× faster** (1391.7 ms → 81.5 ms); the scan
alone over an already-resident matrix, 26×.

**Which number applies to which scan, because the gap between them is the whole design
point.** The 17× belongs to a caller that stays in the buffer from `numpy.frombuffer` onward
— in this tree that is `lens screen --baseline --corpus-mode vector`, which reads the whole
vector table once per pass as a matrix, and `search(mode="vector")` through the resident
matrix cache. A caller that hands the scan a list of Python lists still pays for building the
array and measures about 3×: the cost of an unbounded scan is not the arithmetic, it is
materialising two hundred million Python floats to do it with. A bounded scan — anything a
full-text prefilter has already cut to a few hundred rows — stays on the plain path by
design and measures nothing either way.

**numpy is not a dependency of this package and is not becoming one.** It is imported lazily,
every entry point works without it, and the plain-Python path is the DEFINITION of the
answer — `trialerror/util/vecmath.py`'s own tests compare the two rather than pinning a
number either of them happens to produce. Where identity actually matters (`top_k`, i.e. any
ranking), numpy is used only to NARROW: it scores every row, takes a superset of the *k*
best, and then computes the returned ids, their order and their scores with the plain cosine.
Same answer, byte for byte, whether or not numpy is installed.

```toml
[retrieve]
numpy_fastpath = "auto"   # default when the key is absent: numpy when importable
numpy_fastpath = "off"    # never numpy, whatever is installed
```

| Value | What happens |
|---|---|
| `auto` (default) | numpy when it imports; the plain path when it does not, or when its import raises |
| `off` | the plain path, always |

`off` exists so that a program which sees something it cannot explain has one line to turn
the whole thing off with. `TRIALERROR_NUMPY_FASTPATH` sets the same thing for one process.
An unrecognised value is noted once on stderr and read as unset — a typo in a performance
knob must not stop a program answering.

Memory is bounded by construction and does not grow with the corpus: a scan block holds at
most 20,000 rows AND at most 32 MiB of packed float32 source, whichever is smaller, so a
three-million-row table is scanned in the same ~64 MB a hundred-row one would need a fraction
of (41 MB at 256 dimensions). That figure is the SCAN's own scratch, and
`tests/test_util_vecmath_memory.py` measures it rather than trusting this sentence. What a
caller then holds is its own: a decoded matrix is `rows × dims × 4` bytes resident by
definition, which is what a matrix is and still an order of magnitude below the per-float
Python objects it replaces.

## 3h. Optional: the duplicate-candidate gate (`[lexicon]`)

Skip this section unless your term store has grown past a few thousand names. The gate is
calibrated to open **hundreds to low thousands** of pending duplicate candidates on a store
of that size, and every knob below has a default that needs no config at all.

```toml
[lexicon]
duplicate_coverage_min = 0.5              # rule 1e: how much of the shorter name the shared rare words must cover
name_in_text_requires_informative = true  # rule 1e: a contained name needs a rare word of its own
```

| Key | What it means | Default |
|---|---|---|
| `duplicate_bm25_floor` | The first stage's score line — a trigram hit scoring above it is not looked at. | `-0.5` |
| `duplicate_informative_token_fraction` | A whole word is **informative** when fewer than this share of the store's terms carry it. Computed live from the store at scan time, never a baked word list: what counts as a generic category word is a property of the corpus you imported. | `0.02` |
| `duplicate_informative_token_min_df` | The floor under that fraction, so it does not degenerate on a small store (2% of 60 terms is 1.2, which no shared word could clear). | `3` |
| `duplicate_coverage_min` | **Rule 1e.** The shared informative tokens must cover at least this share of the **shorter** name's informative tokens before the pair is opened on the token route. Compared with `>=`, so a two-word name sharing one of its two rare words passes at exactly half. `0.0` turns the rule off and restores the previous behaviour; a value outside `[0, 1]` is a percentage written where a fraction goes and falls back to the default rather than silently closing the route. | `0.5` |
| `name_in_text_requires_informative` | **Rule 1e.** A name found written whole inside the other side's text qualifies by containment only if it carries an informative token of its own — a name made of a function word plus the family word ("the reading") sits inside half the glosses in any store, which is a fact about prose rather than evidence about two terms. `false` restores the previous behaviour. | `true` |
| `duplicate_similarity_floor` | The whole-name trigram-similarity line the third route uses — the route that catches a misspelling or a run-together compound, which shares no whole word *because* it is nearly the same string. | `0.5` |

**Both rule-1e keys tighten.** A program that sets neither behaves as it did plus the new
rule: strictly fewer candidates, never more. Setting the two to `0.0` / `false` reaches the
old behaviour exactly, which is how a change like this stays auditable against what it
replaced.

**Changing any of these only affects candidates opened afterwards** unless you re-scan:

```bash
trialerror term scan --rescan --dry-run --by-launch <LNCH-...>   # reports, writes nothing
trialerror term scan --rescan --by-launch <LNCH-...>
```

The dry run reports `withdrawn_count`, `withdrawn_by_coverage` and the values actually in
force, so a sweep can be read before it is applied. It re-asks the gate only of pending
system-opened rows nobody has touched — a candidate somebody has ruled on is never taken
back. See `docs/OPERATOR_GUIDE.md`, "The duplicate-candidate gate".

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
