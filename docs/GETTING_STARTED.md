# Getting started

## Before any of this: see it working

If you have not seen TrialError run yet, start with a populated demo program
rather than an empty scaffold:

```powershell
trialerror demo seed --dir .\demo-program
```

That builds a small research program with a corpus, budget pools, a critique
gate mid-review, two deliberation rooms, and a part-discharged course ladder,
then prints the `dashboard serve` command to open it. It is the fastest way to
see what every panel is *for* before you have data of your own — a fresh
scaffold renders every panel's zero state, which tells you nothing.

The seeded program is disposable and self-contained (it keeps its own account
ledger in `<program>/.platform`, so your real `~/.trialerror` is untouched),
and its corpus is entirely synthetic — every source is prefixed `[DEMO]`.
Delete the directory when you are done and read on.

## Three ways to start

Pick the one that matches where you're coming from — none of them are mutually
exclusive, and path 1 is worth reading even if you're headed for 2 or 3 (it's the
fastest way to see the whole system work end to end on a throwaway scratch program
before you touch real data).

1. **Fresh program, no existing data.** `trialerror program init` scaffolds a new program
   and you build up a corpus from scratch. This is the default path; the rest of this
   document (§0–§6 below) walks it in full.
2. **You already have a research project organized your own way** — a corpus of
   papers, notes, ledgers, a differently-shaped repo — and want to start using TrialError
   against it *without moving or restructuring anything*. Load the
   `import-existing-project` skill (`plugin/skills/import-existing-project/SKILL.md`):
   it walks inventory → interview → bridge (`trialerror.toml` `[paths]` +
   `[paths].ingest_roots` pointed INTO your existing tree, or a true link — a directory
   junction on Windows, a symlink on Linux/macOS — when one is unavoidable; no data
   physically moves) → register → validate.
3. **You want your existing project's own ledgers/logs to become real TrialError rows** —
   not just files TrialError can read alongside them, but data migrated into
   `ruling`/`artifact`/`event`/... tables. This is a deeper, per-project migration, not
   a file-level bridge, and not something a generic skill can do safely — it needs a
   bespoke design pass over your specific data shapes: an inventory of your project's
   own stores, a field-by-field map onto TrialError's tables, and a dry-run/validate/import
   runner (a tenant-migration tool, analyze -> dry-run -> validate -> import, gated
   behind an explicit review step before any real write).

---

A first, real session on TrialError (path 1 above): scaffold a program, boot a session,
ingest a document, search it with citations, citation-check a draft, then close with a
rendered handoff.

Every command below is copy-pasteable on Linux, macOS, and Windows, and uses only commands
and flags that exist in the shipped `trialerror/cli/*.py` code (verified against source, not
the design doc — see the note at the end of `docs/OPERATOR_GUIDE.md` for the one place they
disagree). A `console` fence is platform-neutral — type it exactly as written, in whatever
shell you have; the few steps that genuinely differ between shells (line continuations,
writing a file, the shape of an absolute path) are given twice, bash first and PowerShell
second, rather than making you translate. Every `trialerror` command prints a JSON envelope
(`{ok, command, result|error, nextActions, meta}`); the walkthrough calls out the fields
worth reading, but the full payload is always there if you want more than that.

## 0. Install

```console
cd research-harness
pip install -e .
trialerror --version
```

## 1. Scaffold a program

```console
# Linux / macOS
trialerror program init demo --dir ~/research/demo-program
# Windows PowerShell
trialerror program init demo --dir C:\research\demo-program
```

This creates a `trialerror.toml` in that directory (a commented starter — program id,
model policy, license posture, ingest OCR/embed backend, litapi provider defaults, all
optional except `[program].id`), the design's per-program layout (`raw/`, `archive/`,
`memory/`, `law/`, `handoffs/`, `artifacts/`, `requests/`), and runs the initial
migration over all four DBs (`stores/knowledge.db`, `stores/ops.db`, `stores/jobs.db`,
plus the platform `~/.trialerror/platform.db`). Every command discovers its program root by
walking up from the current directory looking for `trialerror.toml`
(`trialerror.util.config.find_program_root`), or via `--program-root` (see the placement
note in "Gotchas" below).

```console
# Linux / macOS
cd ~/research/demo-program
# Windows PowerShell
cd C:\research\demo-program
```

Re-running `trialerror program init` against an existing scaffold refuses
(`already_scaffolded`) rather than overwriting your `trialerror.toml` — it names the next
step (`trialerror session boot`) instead.

## 2. First account + boot a session

TrialError tracks budget/spend per **account** (a Claude Code login), not per program. On a
brand-new program there are no accounts yet, so the first boot bootstraps one:

```console
trialerror session boot --create-account "my-account"
```

Read the result's `result.bundle` — this is the same "boot bundle" a live Claude Code
`SessionStart` hook would inject as pre-loaded context: pin status, whether this is the
account's first-ever session, dangling launches, unread inbox count, budget headroom,
and the L0 memory index. Note `result.session_id` — you'll pass it to the next step.

Re-running `trialerror session boot` (no flags) while a session is already open is idempotent:
it returns the same open session's bundle rather than erroring. Use `--fresh` if you
specifically want a "refuse if one is already open" boot instead.

```console
trialerror session status
```

is the read-only way to re-check the same bundle later without re-booting.

## 3. Book a launch

Every write that TrialError attributes to "who did this" (`registered_by_launch`,
`created_by_launch`, artifact/gate actors, …) needs a `launch_id` — a booked unit of
agent spend, XID-validated against `platform.launch`. In a live Claude Code session this
happens automatically (the orchestrator books, the `PreToolUse:Task` hook consumes the
booking when it spawns a subagent — see `docs/OPERATOR_GUIDE.md`'s enforcement section).
Working from a bare terminal, as in this walkthrough, book one yourself:

bash (a trailing `\` continues the line):

```bash
trialerror budget book \
  --session-id <SESS-id from step 2> \
  --program-id demo \
  --agent-kind orchestrator \
  --model-class top \
  --model claude-opus \
  --purpose "getting-started walkthrough" \
  --est-tokens 5000
```

PowerShell (a trailing backtick continues the line):

```powershell
trialerror budget book `
  --session-id <SESS-id from step 2> `
  --program-id demo `
  --agent-kind orchestrator `
  --model-class top `
  --model claude-opus `
  --purpose "getting-started walkthrough" `
  --est-tokens 5000
```

Copy `result.launch_id` (also echoed in `meta.prompt_fragment`) — you'll pass it as
`--launch-id`/`--by-launch` to every command below. A brand-new account has no budget
pool yet, and that's fine: with no pool configured, `book` books unconditionally
(`state="PROVISIONAL"`) — pools only start capping bookings once one exists. Create one
when you want real cap enforcement:

```console
trialerror budget pools --create --account-id <ACC-id> --model-class top --period weekly --cap-tokens 1000000
```

(`<ACC-id>` is in the boot bundle's `result.account_id` from step 2.)

## 4. Ingest a document

### Fake vs. real backends

Ingestion's OCR and embedding stages are pluggable. **v0 defaults to deterministic fake
backends** (`trialerror/ingest/backends.py`: `FakeOcrBackend`, `FakeEmbedBackend`) so the whole
pipeline works with zero GPU/model dependency — this is what a fresh `trialerror.toml` gives
you with no `[ingest]` table at all, and it's what this walkthrough uses. When you're
ready to point at the real local models, add to `trialerror.toml`:

```toml
[ingest.ocr]
backend = "marker"
marker_single_exe = "/home/you/tools/marker/venv/bin/marker_single"   # never hardcoded — config-pathed

[ingest.embed]
backend = "qwen3-4b"
python_exe = "/home/you/research/tools/embeddings_local/venv/bin/python"
module_dir = "/home/you/research/tools/embeddings_local"

# Windows equivalents — same keys, drive-letter paths, and the venv's interpreter
# lives under Scripts/ rather than bin/:
#   marker_single_exe = "C:/path/to/marker_single.exe"
#   python_exe = "C:/path/to/embeddings_local/venv/Scripts/python.exe"
#   module_dir = "C:/path/to/research/tools/embeddings_local"
```

The real backends shell out to your existing marker/Qwen3 tooling as subprocesses — see
`docs/USER_SETUP.md` for what has to exist at those paths, and note this build has not
yet been live-verified against a real GPU (flagged honestly in `trialerror accept`'s output).

**An absolute config path must be absolute for the platform actually running it.**
`pathlib` decides "is this absolute?" against the host, so a `C:/...` value read on Linux
is not merely unusable — it has no drive on POSIX, reads as *relative*, and gets joined
onto the program root, silently producing a real directory in the wrong place. Every
`[paths]` key (`stores_dir`, `archive_dir`, `law_digest_path`, `handoffs_dir`,
`requests_path`, `memory_dir`, `ingest_roots`) now raises a `ConfigError` naming the
mismatch instead of resolving it wrongly; the `[ingest.*]` executable paths above are
handed straight to the OS, so a wrong-platform value there surfaces as a job failure on a
missing executable.

### Register a source, then a document

bash — a quoted heredoc writes the bytes verbatim (UTF-8, no BOM, on any UTF-8 locale):

```bash
cat > raw/hello.md <<'EOF'
# Hello TrialError

Tabletop role-playing games use dice pools to resolve uncertain outcomes during play.
EOF

trialerror ingest add-source \
  --kind web --title "Hello TrialError fixture" \
  --license-tier open --acquisition-route web \
  --launch-id <launch_id>
```

PowerShell — `-Encoding utf8` is stated explicitly because Windows PowerShell 5.1 defaults
`Out-File` to UTF-16:

```powershell
"# Hello TrialError`n`nTabletop role-playing games use dice pools to resolve uncertain outcomes during play." | Out-File -Encoding utf8 raw/hello.md

trialerror ingest add-source `
  --kind web --title "Hello TrialError fixture" `
  --license-tier open --acquisition-route web `
  --launch-id <launch_id>
```

Copy `result.source.source_id` from the output, then:

```console
trialerror ingest add --source-id <source_id> --path raw/hello.md --launch-id <launch_id>
```

`result.job.job_id` is the first pipeline job (`normalize`, since `.md` is a directly
normalizable format — no OCR route needed for this fixture). Each stage enqueues the
next one on completion (normalize → chunk → embed → index), so one worker loop drains
the whole pipeline:

```console
trialerror jobs start-worker --mode loop --foreground
```

This runs inline in your terminal and exits once the queue has been idle for a few polls.
Check progress any time with:

```console
trialerror ingest status --doc-id <doc_id>
trialerror jobs list --state complete
```

If `add` refuses with a `cost_gate_refused` error (estimated pages over the configured
threshold, default 50), it names the exact `--yes` command to re-run.

## 5. Search + citecheck

```console
trialerror query search "dice pools resolve uncertain outcomes"
```

Every result row carries a `citation.anchor.anchor_id` — copy one. This is the same
engine the `trialerror-knowledge` MCP `search` tool serves live agents.

To citation-check a draft against the corpus, write a file with a
`[[cite:ANC-<id>]]` marker immediately after the sentence it supports (this exact marker
syntax is `trialerror.verify.citecheck`'s mechanical-pass convention — the design doc doesn't
pin one, so read this as the shipped code's contract):

bash:

```bash
printf '%s\n' 'Tabletop role-playing games use dice pools to resolve uncertain outcomes during play. [[cite:ANC-<id>]]' > draft.md

trialerror verify citecheck draft.md --by-launch <launch_id>
```

PowerShell:

```powershell
"Tabletop role-playing games use dice pools to resolve uncertain outcomes during play. [[cite:ANC-<id>]]" | Out-File -Encoding utf8 draft.md

trialerror verify citecheck draft.md --by-launch <launch_id>
```

The mechanical pass auto-passes a citation whose sentence shares a 6-word shingle (or a
number) with the anchor's quoted text; anything it can't resolve mechanically is queued
for escalation (`--judgments-file` supplies external judgments — omit it to see which
pairs would need one).

## 6. Close + handoff

Session close is a **refusing** tool by design — it fails on dangling (unreconciled)
launches, an unread inbox, or a stale law-digest pin. Reconcile your booking first:

```console
trialerror budget reconcile --launch-id <launch_id> --actual-tokens 4200
```

**One more refusal to expect, working from a bare terminal like this walkthrough**: close
also checks that at least one `hook_alive` event was recorded for the session — proof the
`SessionStart`/`Stop` hooks were actually armed. Since this walkthrough booted via the
CLI directly rather than through a live Claude Code `SessionStart` hook, no such event
exists, and close will refuse (`hooks_disabled`) unless you cite an override ruling:

```console
trialerror law append --summary "bare-terminal walkthrough session; hooks were never armed by design"
```

Copy `result.ruling_id` from that, then close (`--course-check` is a required JSON blob —
the design's course-adherence check from session lifecycle §9.3):

bash:

```bash
trialerror session close \
  --course-check '{"rungs":"n/a","build_vs_theory":"n/a","drift_flag":false}' \
  --override-ruling-id <ruling_id>
```

PowerShell:

```powershell
trialerror session close `
  --course-check '{"rungs":"n/a","build_vs_theory":"n/a","drift_flag":false}' `
  --override-ruling-id <ruling_id>
```

In a real Claude Code session with the plugin's hooks installed, `SessionStart` records
`hook_alive` for you automatically and you won't need `--override-ruling-id` at all.

On success this renders a new `handoffs/HANDOFF_<date>.md` file (auto-superseding the
previous one, never hand-edited — it's a view over `ops.db`, not a source of truth). If
close refuses, it names the exact fix in `nextActions` (reconcile a launch, read the
inbox, or `trialerror law diff-foreign` to see what changed under you).

## What's next

- The full command reference (every group, the two MCP servers, the enforcement model,
  the doctor catalog): `docs/OPERATOR_GUIDE.md`.
- One-time account/key/local-model setup: `docs/USER_SETUP.md`.
- Sanity-check your whole install any time with `trialerror accept` — it runs this exact
  journey (fake backends, a fresh scratch program) end to end and reports pass/fail per
  step, plus the GPU/live-Claude-Code items that still need your real machine.

## Gotchas worth knowing up front

- **`--program-root`/`--platform-root` can go almost anywhere now, but each group still
  has one "natural" spot** — `trialerror --program-root X <group> <verb> ...` (before the
  group name) always works, and so does every group's own historical placement (most
  groups: after the verb, e.g. `verify citecheck ... --program-root X`; `budget`/`lens`:
  right after the group name). If you give it in more than one place, the LAST one (the
  one closest to the actual verb) wins. The full per-group historical convention is in
  `docs/OPERATOR_GUIDE.md`'s **Command reference** section.
- **`trialerror doctor` needs `--program-root`** to see program-scoped checks (schema version,
  dangling XIDs, stale chunks/embeddings, anchors) — without it, those checks silently
  skip rather than failing loudly (a known, tracked gap).
