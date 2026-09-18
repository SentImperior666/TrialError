# vast.ai as an embedding GPU, and CPU query embedding

Status: design + implementation on branch `feature/vastai-embed`. **Not yet run against the live vast.ai API.**
Every vast.ai price, every non-DEV throughput figure and the exact API response shapes are **estimates or
assumptions** until the operator's first authorised run confirms them (section 9).

Labels used below: **[measured]** = read from a file produced by a real run on this laptop; **[spec]** = taken
from a vendor or vast.ai document, not measured here; **[estimate]** = derived by me, not measured.

---

## 1 · Problem

1. Embedding runs only on the DEV GPU (RTX 5080 Laptop, 16 GB). That GPU is shared and often busy, so
   documents have waited up to 69 h for vectors. The operator wants a rented vast.ai GPU as a second place
   to run the same job.
2. `TRIALERROR_FEEDBACK.md` item 15: with `[ingest.embed] backend = "offload"`, every vector-touching query
   mode (`vector`, `hybrid`, and the default `auto`) raises `OffloadNotRunnable`. The engine asks the
   *ingest* backend to embed the query string, and for offload that backend is a sentinel that refuses to run.

## 2 · Interface integration: vast.ai is an executor of the existing offload queue

### 2.1 What already exists

```
ingest handler (run_embed)
   └─ load_embed_backend([ingest.embed])  ->  OffloadMarker   (backend = "offload")
   └─ offload.stage.offload_embed_vectors
         step 1  done/<job>/ present  -> verify_published (payload sha, model_key, dims, chunk ids)
                                         + config-hash check -> write emb rows (the one write path)
         step 3  else queue_marker(pending/<job>/chunks.jsonl + manifest)  -> park (environmental)
DEV GPU:  trialerror offload worker  -> claim -> pull -> backend.embed_batch -> result.json -> push -> publish
```

The sandbox never trusts the GPU side. It writes rows only from `done/<job>/`, after it has checked the
payload sha256 of every output, `model_key`, `dims`, the exact chunk-id list, and that the manifest's
`config_hash` matches the **current** `[ingest.embed]` table.

### 2.2 What this change adds

vast.ai is a **second executor for the same queue**. The handler, the manifest, the job ledger, the
`done/` layout, `verify_published` and the `emb` write path are **unchanged**:

```
trialerror vastai run
   ├─ select pending EMBED markers from <program>/offload/pending/        (LocalTransport)
   ├─ plan: tokens -> est. compute s -> TTL -> worst-case $  (refuse if > cap)
   ├─ guard: tier; high tier needs an operator approval (section 4.1)
   ├─ lease: create instance -> [run] -> DESTROY (finally)                 (section 5)
   │     run = for each job: offload.worker._process_one(...)   <-- the DEV worker's own code
   │           with backends.embed() = RemoteEmbedBackend (JSON lines over SSH to the instance)
   └─ result lands in done/<job>/ exactly like a DEV result; the SAME stage.py step 1 verifies it
```

Reusing `worker._process_one` is deliberate: `result.json`, output sha256s, error publication, heartbeats
and claim return are produced by the code that already produces them for DEV. A vast.ai result therefore
**cannot** take a different verification path. There is only one.

### 2.3 Why switching is one line of configuration

```toml
[ingest.embed]
backend   = "offload"
model_key = "qwen3-4b"
dims      = 2048
gpu       = "vastai"      # "dev" (default) | "vastai"   <- the switch
```

* `gpu` selects which executor is **allowed** to serve the queue. `trialerror vastai run` refuses to rent
  anything unless `gpu = "vastai"`. With `gpu = "dev"` (or absent) nothing about today's behaviour changes.
* `gpu`, and the new `[ingest.embed.query]` sub-table, are **excluded from `config_hash`**
  (`offload.marker.ROUTING_KEYS`). They decide *where* vectors are computed, not *what* vectors are. So
  flipping the switch neither re-queues finished work nor invalidates in-flight markers. Every existing
  program's hash is byte-identical, because those keys are absent from existing tables.
* Callers (`run_embed`, `run_index`, `retrieve.engine`), job records, the ledger and the store are untouched.
  The vast.ai-specific settings (tiers, caps, image, key path) live in a separate `[vastai]` table, which is
  also outside the embed hash.

Remaining asymmetry: the DEV worker runs on a different machine's toml and is not told about `gpu`. If both
executors are started, they race for the same markers, and the queue's atomic-rename claim gives each marker
one winner. Both outputs are valid. That is a waste of money, not a correctness problem, and it is written down
here rather than coded around.

## 3 · The workload and the tier calculation

### 3.1 The real workload [measured]

Source: `univestal-tabletop-engine/research/tools/embeddings_local/embed_backend.py` and
`results/qwen3-4b.json` (the WKP-061 bake-off on this laptop's GPU).

| quantity | value | label |
|---|---|---|
| model | `Qwen/Qwen3-Embedding-4B`, sentence-transformers, last-token pooling | [measured] (code) |
| precision | bfloat16 weights | [measured] (code) |
| stored dims | 2048 (matryoshka truncation of native 2560, then L2 re-normalise) | [measured] |
| query prompt | bundled `prompt_name="query"`; documents: no prefix | [measured] (code) |
| throughput on DEV | **4,291 tokens/s**, 3.5 chunks/s (200 chunks, batch 4, max_seq 1024) | [measured] |
| peak VRAM on DEV | **11,224 MiB** at batch 4 | [measured] |
| model load | 8.94 s from local cache | [measured] |
| tokens per byte of corpus text | 0.286 | [measured] (`corpus_token_estimate.json`) |
| whole current corpus | ~6.3 M tokens -> **~25 min of DEV-GPU compute** | [estimate] from the two lines above |
| TrialError chunk cap | 1,024 tokens (`chunk.token_count` CHECK) | [measured] (schema) |

Consequence for tiers: the backlog is a **scheduling** problem, not a compute problem. A whole corpus is
about 25 GPU-minutes. For a typical run, fixed overhead (instance boot, image pull, `pip install`, an ~8 GB
model download) is larger than compute. That is why the default is mid and why high rarely pays for itself
(section 3.4).

### 3.2 VRAM floor

The measured 11.2 GB peak leaves under 1 GB free on a 12 GB card. That is not enough headroom for a
long-tail 1,024-token batch, so **every tier requires ≥ 16 GB** (`min_vram_gb`). High requires ≥ 24 GB,
because the cards that are actually faster all have at least that much.

### 3.3 Throughput model [estimate]

```
est_tokens_s(gpu) = 4291 [measured, DEV] × factor(gpu) [estimate]
compute_s         = tokens / est_tokens_s
tokens            = Σ utf-8 bytes of the chunk texts × 0.286 [measured ratio]
tokens_per_dollar = est_tokens_s × 3600 / dph_total
```

`factor` is the GPU's throughput relative to the DEV GPU, which is 1.0. It is **not measured** for any card
except DEV. I derived it from the relative dense FP16/BF16 tensor throughput (FP32 accumulate) in vendor
specifications, and I assumed the model is compute-bound in the prefill-only embedding pass. Treat it as
±50%. The factors are configurable (`[vastai.gpu_factors]`). Every run records the **measured** tokens/s of
the card it got in a `vastai_run` event, so the table can be recalibrated from real runs.

| tier | GPU allowlist (vast.ai `gpu_name`) | factor [estimate] | min VRAM | max $/h (ceiling, **not a price**) | min reliability |
|---|---|---|---|---|---|
| low | RTX 4060 Ti, RTX 5060 Ti, RTX A4000, RTX 4000Ada | 0.5-0.6 | 16 GB | 0.25 | 0.95 |
| **mid (default)** | RTX 3090, RTX 3090 Ti, RTX 4070 Ti Super, RTX 4080, RTX 4080S, RTX 5070 Ti, RTX 5080, RTX A5000 | 0.9-1.4 | 16 GB | 0.45 | 0.97 |
| high | RTX 4090, RTX 5090, L40S, A100 PCIE, A100 SXM4, H100 PCIE, H100 SXM | 1.8-6.0 | 24 GB | 2.50 | 0.98 |

Mid is the band around the DEV GPU (0.9-1.4×), as the brief asks. At run time the runner does not pick
"the cheapest". It picks the offer with the **highest `est_tokens_s / dph_total`** among offers that pass the
tier's allowlist, VRAM, price ceiling and reliability filters. That applies the throughput-per-dollar
criterion to the offers that actually exist at that moment.

**Prices.** I have no vast.ai API key and made no API call, so **there are no observed vast.ai prices in
this document.** The `max $/h` column holds ceilings the operator sets, chosen so that mid stays within the
same order of cost as the DEV card's electricity-free alternative. They are placeholders to be confirmed
with `trialerror vastai plan`, which performs one read-only offer search and prints the live offers, the
selection, the TTL and the worst-case cost without renting anything.

### 3.4 Worked example [estimate]

A 50-document batch × ~40 chunks × ~800 tokens ≈ 1.6 M tokens. Defaults: `startup_s = 1200`,
`safety = 2.0`, `grace_s = 300`.

| tier | factor | compute | TTL = 1200 + 2×compute + 300 | worst case at ceiling |
|---|---|---|---|---|
| mid | 1.0 | 373 s | 2,246 s (0.62 h) | 0.62 h × $0.45 = **$0.28** |
| high | 2.5 | 149 s | 1,798 s (0.50 h) | 0.50 h × $2.50 = **$1.25** |

High finishes about 7 minutes sooner and costs about 4.5× more. **High beats mid on tokens per dollar only
when `dph_high / dph_mid < factor_high / factor_mid`**, and overhead dominates any batch smaller than several
million tokens. High is for a genuine emergency, which is why it is gated (section 4.1).

## 4 · Guardrails

### 4.1 An agent switching to the high tier

Requirement: editing `trialerror.toml` must not be enough.

1. **Two independent conditions.** `[vastai] tier = "high"` **and** a valid approval file at
   `<program_root>/keys/vastai-high-tier.approval`. The approval is a JSON document:
   `tier`, `program` (sha256 of the resolved program root), `issued`, `expires` (at most 24 h after
   `issued`), `max_job_usd`, `nonce`, and `mac = HMAC-SHA256(vast.ai API key, canonical body)`.
2. **Why an agent cannot trivially satisfy it.**
   * The `mac` is keyed by the **vast.ai API key**, which exists only in the operator-placed key file. Minting
     a valid approval therefore requires reading a secret out of `keys/`. That act is forbidden to agents by
     standing rule, it is conspicuous in any transcript, and no TrialError entry point performs it on the
     agent's behalf.
   * The one entry point that mints approvals (`trialerror vastai approve-high`, and the library function
     behind it) **refuses unless stdin and stdout are both a TTY**. It then prints a random challenge that
     must be typed back. Agent tool shells run without a TTY. The check lives *inside* the minting function,
     not only in the CLI, so `python -c "...mint..."` hits it as well.
   * Approvals are short-lived (≤ 24 h), bound to one program root, and bound to a dollar ceiling. The
     effective per-job cap on the high tier is `min([vastai].max_job_usd, approval.max_job_usd)`, so an edited
     toml cannot raise the spend the operator approved.
   * Honest limit: an agent that deliberately reads the key file and writes its own HMAC code can forge an
     approval. The design makes that deliberate circumvention of two rules, not a casual edit. The loud
     after-the-fact signals below still fire, because they do not depend on the approval being genuine.
3. **Loud on every use.** Each high-tier run prints a multi-line `!!!` banner to **stderr** (tier, GPU, $/h,
   worst-case $, approval expiry) and appends a `vastai_high_tier_use` **event** to the store *before* the
   instance is created.
4. **`doctor` check `vastai_high_tier`.** `warn` if `tier = "high"` is configured, if a valid approval is
   present, or if a `vastai_high_tier_use` event exists in the last 7 days.
5. **Default is never high.** An absent `tier` means mid, and an unknown tier name is an error. There is no
   CLI flag that overrides the tier.

### 4.2 A GPU running longer than the job

1. **Lifecycle** (section 5). Create, then run, then **destroy in `finally`**. The `finally` runs on success,
   on exception, on `KeyboardInterrupt` and on TTL expiry. Destroy is retried up to 5 times with backoff. It
   is then confirmed by reading the instance list. A failed destroy is printed loudly and left for the reaper.
2. **Hard wall-clock TTL** `= min(ttl_cap_s, startup_s + safety × compute_s + grace_s)`. The default cap is
   4 h and cannot be exceeded. A watchdog thread destroys the instance at the deadline **even mid-job** and
   kills the SSH channel. The in-flight job is handed back unrun, so no offload attempt is burned, because the
   failure was a sizing miss rather than a GPU fault. No further job starts. If one job alone would exceed the
   cap, the run refuses to start. Mechanism: `LeaseExpired` subclasses `KeyboardInterrupt`, so the DEV worker's
   `_process_one` returns the claim unrun, as it does for Ctrl-C, and no `except Exception` block can
   swallow it.
3. **Per-job dollar cap.** `worst_case_usd = offer.dph_total × TTL_h`. If it exceeds `max_job_usd` (default
   $3.00), the run is **refused before any instance is created**. The worst case is used, not the expected
   cost, because the TTL is the most the operator can be billed.
4. **Independent reaper.** `trialerror vastai reap` lists the account's instances and destroys every
   TrialError-tagged one (label `trialerror|<program-hash>|<run_id>|<deadline-epoch>`) that meets any of
   these conditions:
   (a) it is past its deadline;
   (b) its local run record says finished or failed;
   (c) its run record names a PID on this host that is no longer alive;
   (d) it has a TrialError prefix but an unparseable label.
   (e) it belongs to this program but has no local run record at all. The runner writes that record
   before it calls create, so a missing record means the owner is gone.
   The reaper needs no state from the process that died. The label alone carries the deadline. Run it from
   the sandbox's supervise loop or Task Scheduler.
5. **In-instance dead man's switch** (defence in depth). The instance's `onstart` script runs
   `sleep <TTL+grace>; kill 1`, so the container stops itself even if every local process and the reaper are
   dead. vast.ai stops GPU billing for a stopped instance, though storage billing continues until the reaper
   destroys it. **[assumption, unverified live]**
6. **`doctor` check `vastai_live_instances`.** Reports every live tagged instance through a read-only list
   call when a key path is configured, plus every local run record not marked destroyed. `fail` if any is
   past its deadline, `warn` if any is live.
7. **No keep-alive.** There is no option to keep an instance, reuse one across runs, or extend a TTL. The
   config loader rejects `keep_alive`, `reuse_instance` and `ttl_extend` keys by name, so adding one later
   takes a deliberate code change.

## 5 · Instance lifecycle (state diagram)

```
                 plan refused (tier / approval / $ cap / TTL cap / no offer)
   [PLANNED] ──────────────────────────────────────────────────────────────▶ [REFUSED]  (nothing rented)
       │ create (PUT /asks/<offer>/, label carries deadline) ── error ──▶ [CREATE_FAILED] ─┐
       ▼                                                                                    │ reaper
   [CREATED] ── record run file (pid, host, instance_id, deadline) ──┐                      │ destroys any
       │ wait for running + ssh (bounded by TTL)                     │                      │ tagged
       ▼                                                             │ watchdog at deadline │ leftovers
   [RUNNING] ── for each job: _process_one ── publish to done/ ──────┤ ─────────────────────┤
       │ success │ exception │ Ctrl-C │ TTL expiry                   │                      │
       ▼─────────▼───────────▼────────▼──────────────────────────────▼                      │
   [DESTROYING]  (finally: DELETE /instances/<id>/, retried; confirm absent)                │
       │ ok                                     │ still present                             │
       ▼                                        ▼                                           │
   [DESTROYED]  run file status=destroyed   [DESTROY_FAILED]  loud stderr, doctor fail ─────┘
```

Nothing persists on the instance after destroy, because vast.ai's destroy deletes the container and its
disk. Vectors travel back over the SSH channel into the local work directory, then into `done/<job>/`, and
become `emb` rows only through `stage.py` step 1.

## 6 · CPU query embedding (fixes feedback item 15)

* **New factory** `ingest.backends.load_query_embed_backend([ingest.embed])`. For non-offload backends it
  returns exactly what `load_embed_backend` returns, so existing behaviour is unchanged. For offload (DEV or
  vast.ai) it builds a **`CpuQueryEmbedBackend`** from `[ingest.embed.query]`:
  ```toml
  [ingest.embed.query]
  python_exe = "C:/.../embeddings_local/.venv/Scripts/python.exe"
  module_dir = "C:/.../embeddings_local"
  precision  = "bfloat16"     # bfloat16 (default) | float32
  ```
  It runs the **same `embed_backend.py`** the GPU side runs, with `device = "cpu"`, in a subprocess. The query
  prompt, pooling, matryoshka truncation and L2 re-normalisation are therefore the same code. The vast.ai
  instance receives a byte copy of that same file, and its sha256 is recorded in the run event.
* **Comparability is enforced, not assumed.**
  1. `model_key` and `dims` of the query backend are taken from `[ingest.embed]` itself, so they cannot
     differ from the key the rows are stored under.
  2. **Calibration probe.** Before first use, and again whenever
     `(model_key, dims, precision, module sha256)` changes, the backend re-embeds the 3 shortest stored chunks
     as *documents* on CPU. It compares them to their stored GPU vectors. If the minimum cosine is below
     `min_calibration_cosine` (default 0.99 [estimate]), the backend refuses. A pass is cached in
     `<program_root>/offload/query-embed-calibration.json`. This catches a wrong model, a wrong
     normalisation, a changed runner module, and precision drift, all with one mechanism.
* **Degrading instead of crashing.** If the query embedder is unconfigured or refuses, `auto` and `hybrid`
  fall back to lexical ranking. They print a stderr line naming the fix and set `stats.vector_unavailable`.
  Explicit `--mode vector` still hard-fails, because it asked for vectors by name. This is item 15's
  preferred fix 1 plus fix 2.
* **Memory footprint [estimate].** Qwen3-Embedding-4B has ~4.0 B parameters.
  * bfloat16: ~8.0 GB of weights plus ~0.5-1 GB of runtime, so **~9 GB peak** in the subprocess.
  * float32: ~16 GB, which is **not viable** on this laptop at 98% commit charge.

  The subprocess exits after each query, so memory is returned immediately and never stays resident beside
  the store. The cost is latency: model load plus one short forward pass is estimated at **15-60 s per
  query** on CPU. That is acceptable for an agent's occasional query and poor for bulk querying.
* **Quantised option: considered, not shipped.** Dynamic int8 (`torch.ao.quantization.quantize_dynamic`
  over `Linear`) would cut weights to ~4.5 GB [estimate]. It changes vectors by an unmeasured amount. It
  could only be enabled behind the calibration probe above, which would reject it if drift is too large. I
  did not add it because I cannot measure its cosine drift without loading the model, which this session
  must not do. The calibration mechanism makes adding it later safe: set `precision = "int8"` in a follow-up,
  and the probe decides.

## 7 · Failure modes

| failure | effect | handling |
|---|---|---|
| key file missing or unreadable | nothing rented | refuse with the configured *path* named. The contents are never printed |
| no offer passes the tier filters | nothing rented | refuse. `plan` shows why |
| create succeeds, SSH never comes up | billed until destroy | wait bounded by the TTL; `finally` destroys |
| model download slow | TTL may expire | watchdog destroys; job returned unrun; loud message says to raise `startup_s` |
| remote OOM or exception | job's error published | the existing `_handle_worker_error` burns one offload attempt (same as DEV) |
| vectors wrong dims / count / model | rejected | `verify_published` and `offload_embed_vectors` checks move it to `failed/` (same as DEV) |
| local process killed (-9, power loss) | instance orphaned | reaper (deadline in label, dead PID) plus in-instance `kill 1` |
| destroy API error | instance may live | 5 retries, then loud stderr, a `vastai_destroy_failed` event, doctor `fail`, reaper |
| DEV worker and vast.ai both running | duplicate compute | atomic claim; one winner per job. Documented, not prevented |
| query embedder drifts | wrong rankings | calibration probe refuses; auto/hybrid degrade to lexical |

## 8 · Not done, and why

* **No live run.** No vast.ai call was made and no GPU was rented. Renting is operator-gated.
* **Endpoint shapes unconfirmed.** `POST /api/v0/bundles/`, `PUT /api/v0/asks/<id>/`,
  `GET /api/v0/instances/`, `DELETE /api/v0/instances/<id>/`, the `new_contract` field and `gpu_ram` in MB come
  from vast.ai's public docs [spec]. Instance fields `actual_status`, `ssh_host`, `ssh_port` and `label` are
  from memory [assumption]. All of them are isolated in `trialerror/vastai/api.py`.
* **The remote SSH channel is untested live.** Tests mock it. It uses the operator's SSH identity (a path in
  config, opened by `ssh`, never by TrialError).
* **No OCR on vast.ai.** The brief is embedding only. The runner selects only `stage = "embed"` markers.
* **No int8 query path** (section 6).
* **No resident query server.** One subprocess per query trades latency for memory. Given 98% commit charge,
  that trade is deliberate.
* **Tier factors are uncalibrated** except DEV's. They are recalibrated from the `vastai_run` events.
* **No automatic trigger.** `trialerror vastai run` is explicit, because spending money should be an
  explicit act. It can be scheduled once the operator trusts it.

## 9 · Before the first live run (operator)

1. Place the vast.ai API key in `<program_root>/keys/vastai.key` and set `[vastai] api_key_path`.
2. Register an SSH public key with the vast.ai account, and set `[vastai] ssh_identity_path` to the private
   key's path.
3. Set `[ingest.embed.query]` (python_exe/module_dir of `embeddings_local`) and run one
   `trialerror query search "..."`. This runs the calibration probe and checks the real CPU path, which the
   tests did not do (they stub the model).
4. `trialerror vastai plan`. This makes one read-only search and prints live offers and prices. Adjust the
   tier `max_dph` ceilings to what you see.
5. Set `gpu = "vastai"` and run `trialerror vastai run --max-jobs 1` for one small document. Afterwards, check
   `trialerror vastai reap --dry-run` (expect nothing) and the vast.ai console (expect no instance).
6. Put `trialerror vastai reap` on a schedule (e.g. every 15 min).
