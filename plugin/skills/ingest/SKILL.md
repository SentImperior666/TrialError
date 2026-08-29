---
name: ingest
description: Batch-ingest documents into a TrialError program's knowledge store — register a source, acquire a document under it, confirm the pre-ingestion cost-estimate gate, and drive the normalize/OCR -> chunk -> embed -> index pipeline to completion. Use this when the user hands you paper(s)/book(s)/rulebook(s)/web pages to add to the corpus, or when resuming a pipeline a prior session left mid-flight.
---

# /ingest — batch ingestion with a cost-estimate confirm gate

Design Section 6 (ingestion pipeline) + Section 12 M7 row. Every stage is
idempotent, content-hash-keyed, and resumable via the jobs ledger — killing
a worker mid-stage never loses progress, it just needs a `jobs tick` +
another `start-worker` to pick back up.

1. **Register the source** (one row per work; sha256-dedups automatically
   — a duplicate `--content-file` refuses with `dedup_of` pointing at the
   existing source, no second pipeline run):

   ```
   trialerror ingest add-source --kind paper --title "<title>" \
     --license-tier <open|academic_oa|user_owned_scan|commercial_restricted|unknown> \
     --acquisition-route <author_posted|institutional|publisher_oa|user_scan|user_delivered|api|web> \
     --launch-id <your launch_id> --content-file <path> [--authors "..."] [--year N] [--url ...]
   ```

   License fields are REQUIRED at intake (design's licensing posture, C-0048/49
   — never guess a tier; ask the user if unstated). `register` refuses an
   `--acquisition-route` outside the program's configured allowlist
   (`trialerror.toml [license]`), when one is configured.

2. **Acquire + enqueue the document** under that source. The raw file MUST
   resolve under a configured ingest root (default `raw/` or `inbox/` —
   the "manifest-glob wart" this design named and closed):

   ```
   trialerror ingest add --source-id <SRC-id> --path raw/<file> --launch-id <your launch_id>
   ```

   This prints a **zero-LLM dry-run cost estimate** (pages, chunks, embed
   tokens, est. GPU minutes). Read it. Beyond the configured page threshold
   (default 50) it REFUSES without `--yes` — re-run with `--yes` only after
   you've actually looked at the estimate, never reflexively.

3. **Run the pipeline to completion.** `add` only enqueues the first stage
   (`normalize` for a directly-normalizable format, `ocr` for a scanned
   pdf/image route); each stage's handler auto-enqueues the next
   (normalize → chunk → embed → index) as it completes. Drive it with a
   worker:

   ```
   trialerror jobs start-worker --foreground --mode loop --kinds normalize,ocr,custom,embed,index --max-idle-polls 2
   ```

   (`normalize`/`chunk` ride `kind=custom` per the jobs ledger's CHECK
   constraint — the `--kinds` filter above already accounts for that.) For
   a long OCR/embed run you don't want to babysit inline, drop
   `--foreground` to detach it and poll with `trialerror jobs list` / `trialerror
   ingest status --doc-id <id>` instead.

4. **Fake vs. real backends** — `trialerror.toml`'s default (or an unconfigured
   program) uses the deterministic `fake` OCR/embed backends: no GPU
   needed, safe for a smoke run. The REAL marker-OCR and Qwen3-Embedding-4B
   backends are config-pathed under `[ingest.ocr]`/`[ingest.embed]` in
   `trialerror.toml` (`backend = "marker"` / `"qwen3-4b"` plus the executable/
   module paths) — GPU-hardware, this machine only, never assume it's
   available in a generic session.

5. **Verify.** `trialerror ingest status --doc-id <id>` shows element/chunk/
   anchor counts; `trialerror ingest doctor` runs the ingest-specific health
   checks (chunker/embedding staleness, `anchors_dangling` both halves —
   run this after every batch, not just when something looks wrong).

6. **Injection defense runs automatically** at normalize time (the
   book-to-skill sanitizer, vendored MIT) — you don't invoke it separately,
   but a normalize failure citing sanitizer findings means the source
   document itself may be malicious; do not blindly retry, read the
   diagnostic.

7. **Never transcribe copyrighted rulebook text verbatim** while working
   the pipeline manually (e.g. spot-checking OCR output) — the standing
   law (see `research/ops/corrections.md` in the orchestrator repo, C-####)
   caps grounding quotes at ≤20 words, direct-from-image only, structured
   extraction (D-COC-1 adapted protocol). `trialerror query search`'s own
   serving-path fence enforces this for `commercial_restricted` sources at
   READ time regardless — see `/lit-review`.

8. **Acquisition queue** (a source you don't have yet): `trialerror ingest
   request --source-id <SRC-id> --to requested` transitions the state
   machine (`wanted → requested → delivered → verifying → archived →
   indexed`, or `rejected`/`failed`); `trialerror ingest requests-md` renders
   `requests/REQUESTS.md`, the human-facing view the user fulfills against.
