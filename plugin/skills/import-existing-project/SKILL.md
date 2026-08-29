---
name: import-existing-project
description: Guided procedure to bring an existing (non-TrialError) research project into TrialError WITHOUT moving or restructuring it — inventory the foreign project, interview the user on what maps where, bridge via trialerror.toml [paths] + [paths].ingest_roots (or a Windows directory junction when a true link is unavoidable), register sources, and validate with doctor + a search smoke. Use this when the user has an existing research project (papers, notes, a corpus, ledgers, a differently-shaped repo) organized their own way and wants to start using TrialError against it, or asks how to "import"/"switch to"/"migrate into"/"get started with" TrialError without losing or restructuring their existing files.
---

# /import-existing-project — bridge, don't move

User ruling C-0068(a) (verbatim, the origin of this skill): *"there might
be other people who developed their research projects in a different way
and now want to switch to TrialError format... it can't be done programmatically
for all of the possible projects, so we need a skill for claude code with
an explicit instruction to use it to symlink files from non-TrialError research
project to TrialError structured project."*

The governing principle is **bridge, don't move**: the foreign project's
own directory layout, git history, and (often huge) tool/model caches
never move on disk. TrialError's `trialerror.toml` `[paths]` knobs and
`[paths].ingest_roots` point INTO the existing tree instead, and only
TrialError's own small derived output (chunks, embeddings, rendered views)
lands under the new program root. Nothing about this procedure is
mechanical end-to-end — every numbered step below is a judgment call you
make WITH the user, not a script you run unattended. It genuinely cannot
be done programmatically for an arbitrary foreign project (C-0068(a)'s own
framing) — that is exactly why this is a skill (guided judgment) and not a
CLI command (a fixed transform).

This skill was distilled from a real import (a 40GB, multi-year, pre-TrialError
research program with its own idiosyncratic layout) done this same way: fresh
scaffold outside the foreign repo, then config-bridged rather than copied. The
worked design doc for that specific import is not included in this distribution
(it names the source project); every reasoning step from it is folded into the
procedure below instead. The DEEPER option (§2 point 4 below, real schema
migration rather than a config bridge) is a bigger, bespoke build -- treat this
section as the shape it takes (inventory -> field map -> dry-run -> validate ->
gated import), not a turnkey command.

## 0. Before you start

- This is a multi-turn conversation with the user, not a single command.
  Budget for it: inventory, then a real back-and-forth on the mapping,
  then bridge, then register, then validate. Do not shortcut the
  interview step (§2) by guessing at the mapping yourself — a wrong
  guess here means re-registering sources later against the wrong root.
- `trialerror program init <name> --dir <path>` still runs first, exactly like
  any fresh program (see `docs/GETTING_STARTED.md` path 1) — this skill
  picks up from there, it doesn't replace it. Scaffold the new program
  root somewhere **outside** the foreign project's own directory tree (a
  sibling directory, not nested inside it). the import-design notes (internal, not in this export) §4 found
  that nesting a live SQLite-WAL program root inside an already-busy
  foreign tree risks antivirus/indexer/gitignore collisions and blurs
  "the foreign project" and "the TrialError program" into one ambiguous
  identity — a clean, separate root keeps `find_program_root`/`trialerror
  doctor` unambiguous and keeps the foreign repo's own git history and
  citation web (relative-path references inside its notes/ledgers)
  completely untouched.
- Windows-first: every command below is PowerShell or `cmd.exe`. If the
  user is on macOS/Linux, the `[paths]` config-bridge steps (§4a) are
  identical; swap the junction step (§4b) for a plain `ln -s`.

## 1. Inventory the foreign project

Before proposing any mapping, actually look. Don't ask the user to
describe their project from memory when you can enumerate it yourself:

```powershell
Get-ChildItem -Path <foreign-root> -Depth 1 | Select-Object Name, Mode
# per top-level dir: size + rough file count (skip .git and any obvious
# model-cache/vendor dir up front -- these can be tens of GB and you only
# need an order-of-magnitude read, not a byte-exact count)
Get-ChildItem <foreign-root>\<dir> -Recurse -File | Measure-Object -Property Length -Sum
```

For each top-level directory, form a hypothesis before asking:
- **Sources / a corpus** — PDFs, papers, rulebooks, scraped pages, OCR
  captures. Candidate for `[paths].ingest_roots` + `trialerror ingest
  add-source`/`add`.
- **Notes, a wiki, a running log** — markdown that's read/written by a
  human or a prior tool, not machine-rendered. Candidate for staying
  external and referenced, or for `[paths].memory_dir` if it's genuinely
  agent-facing memory.
- **Ledgers / structured records** — a rulings log, an artifact registry,
  a corrections/decisions file, event logs. These map onto TrialError TABLES
  (`ruling`, `artifact`, `event`, ...), not files — flag them as
  candidates for the deeper migration option (§2 point 4), not a
  file-level bridge.
- **A request/acquisition queue** — candidate for `[paths].requests_path`
  once sources start flowing through `trialerror ingest request`.
- **Generated/rendered output** (a dashboard, a digest, a rendered
  view) — usually superseded by TrialError's own rendered views; don't bridge
  these, they'll be regenerated.
- **Large model/tool caches** (embeddings, OCR models, a vector index) —
  candidates for `[ingest.ocr]`/`[ingest.embed]` config pathing (already
  fully config-driven — see the shipped `trialerror.toml` template's commented
  `[ingest.ocr]`/`[ingest.embed]` tables), never for copying or a
  junction. the import-design notes (internal, not in this export)'s own inventory found a single 34GB
  model-cache directory — the strongest argument in that document for why
  "bridge, don't move" matters in practice, not just in principle.
- **Everything else** (build/tool scripts, an old pre-TrialError enforcement
  layer, superseded prior attempts) — likely has no TrialError home at all.
  That's fine; name it and move on. the import-design notes (internal, not in this export) §7 calls this
  "loss analysis" and gives every such item a named disposition (a
  `legacy/` marker) rather than silently dropping it — do the same:
  surface the list to the user, don't quietly ignore it.

Produce a short table (dir → rough size/count → your hypothesis) and show
it to the user before step 2 — this is the concrete artifact the
interview in §2 reacts to.

## 2. Interview the user on the mapping

Walk your inventory table with the user, one row at a time, and pin down:

1. **What becomes a registered source corpus** (→ `[paths].ingest_roots`,
   then `trialerror ingest add-source`/`add`)? Confirm license posture per
   source up front — `trialerror ingest add-source` REQUIRES
   `--license-tier`/`--acquisition-route` at intake (never guess; ask).
2. **What's agent memory vs. what's a human-facing document that stays
   external?** Only genuine tiered agent memory (L0/L1/L2, the kind
   `trialerror memory sync-export`/`sync-import` renders) belongs under
   `[paths].memory_dir` — a research journal or a wiki is not memory in
   this sense and should usually stay where it is, referenced by prose,
   not bridged.
3. **What stays external, permanently** — large caches, the foreign
   project's own git history, anything with hundreds of embedded
   relative-path citations that would break if moved (the import-design notes (internal, not in this export)
   §4's `curriculum/archive` example: renaming it breaks every `S###`-style
   citation across the whole corpus). Naming this list explicitly is as
   important as naming what DOES get bridged.
4. **Fresh-scaffold-and-bridge, or a deeper migration?** This skill
   performs the first (§3–§6 below). If the user actually wants foreign
   data to become real TrialError TABLE rows (ledger entries as `ruling`
   rows, an artifact registry as `artifact` rows, event logs as `event`
   rows) rather than just file-level bridges, that's a different,
   heavier undertaking — point them at `docs/the migration-plan notes (internal, not in this export)` as
   the worked example and say so explicitly (see the last "Don't" item,
   §7); don't silently attempt it as part of this skill.

Do not proceed to §3 until the user has confirmed the mapping. A wrong
guess here is expensive to unwind later (re-registering sources against a
different root, or a `[paths]` config edit racing an in-flight ingest job).

## 3. Scaffold (if not already done)

```powershell
trialerror program init <name> --dir <new-program-root>
```

This writes `trialerror.toml` with every `[paths]` knob present, commented out,
at its default — see the template's own `[paths]` block for the exact
list (`stores_dir`, `archive_dir`, `law_digest_path`, `handoffs_dir`,
`requests_path`, `memory_dir`, `ingest_roots`). You'll uncomment and edit
the ones the interview in §2 identified.

## 4. Create the bridges

### 4a. Prefer `trialerror.toml` `[paths]` — no data moves

This is the primary mechanism, and should cover almost every case. Edit
the new program's `trialerror.toml`:

```toml
[paths]
# point INTO the foreign project -- absolute paths, forward slashes even
# on Windows (pathlib accepts them, and it sidesteps TOML's backslash
# escaping rules entirely)
ingest_roots = ["C:/path/to/foreign-project/papers", "C:/path/to/foreign-project/scans"]

[ingest.embed]
backend = "qwen3-4b"
python_exe = "C:/path/to/foreign-project/tools/embeddings_local/venv/Scripts/python.exe"
module_dir = "C:/path/to/foreign-project/tools/embeddings_local"

[ingest.ocr]
backend = "marker"
marker_single_exe = "C:/path/to/foreign-project/tools/marker_ocr/marker_single.exe"
```

`[paths].ingest_roots` accepts absolute paths and is read by every
`trialerror ingest add` call (`trialerror.ingest.pipeline.resolve_ingest_roots`) —
this is the knob that already worked before this skill existed, and
covers "register documents that live in the foreign tree" completely on
its own. The other six `[paths]` knobs (`stores_dir`, `archive_dir`,
`law_digest_path`, `handoffs_dir`, `requests_path`, `memory_dir`) relocate
where TRIALERROR'S OWN output lands — use them only when the user specifically
wants a TrialError-rendered view to live inside the foreign tree (e.g.
`memory_dir` pointed at the foreign project's own cross-account-synced
`memory/` directory, so an existing sync convention keeps working
unchanged — the import-design notes (internal, not in this export) §4's own recommendation). Leave a knob at
its commented-out default unless the user has a specific reason to move
it — relocating `stores_dir`/`archive_dir` etc. without a reason just adds
indirection for no benefit.

### 4b. Windows directory junctions — only when a TRUE link is needed

Reach for this only when some tool or convention needs a path to
physically exist at a fixed location relative to the program root or the
foreign tree (not just "TrialError needs to read from here" — §4a already
covers that) — e.g. the user wants `<program-root>/raw` to transparently
BE a foreign directory for a workflow that hardcodes that relative path.

**Use a directory junction (`mklink /J`), not a symlink.** A junction
needs no admin privileges and no Developer Mode; a symlink
(`mklink /D`, or PowerShell's `New-Item -ItemType SymbolicLink`) requires
either an elevated shell or Developer Mode enabled — state this to the
user explicitly if they ask why the command below isn't `/D`. Junctions
only link DIRECTORIES (not individual files) and only work within the
same local machine's NTFS volumes (not a UNC network share) — for a
single file, prefer NOT bridging it at all (register it as a source
instead, §3), since a hardlink (`mklink /H`) has none of a junction's
transparency for tooling that stats the file and is easy to lose track of.

```cmd
:: cmd.exe -- create <program-root>\raw as a junction pointing at the
:: foreign tree's own document directory. Order matters: LINK path first,
:: TARGET path second.
mklink /J "<program-root>\raw" "C:\path\to\foreign-project\papers"
```

```powershell
# PowerShell equivalent
New-Item -ItemType Junction -Path "<program-root>\raw" -Target "C:\path\to\foreign-project\papers"
```

Verify it landed as a junction (not a copy) before moving on:

```powershell
Get-Item "<program-root>\raw" | Select-Object LinkType, Target
```

A junction to `raw` still needs nothing extra in `[paths].ingest_roots`
(the default already includes `"raw"`) — `trialerror ingest add` reads through
the junction transparently, exactly as if the foreign files lived there
directly.

## 5. Register sources against the bridged paths

```powershell
trialerror ingest add-source --kind <paper|book|web|rulebook|dataset|report|other> --title "<title>" `
  --license-tier <open|academic_oa|user_owned_scan|commercial_restricted|unknown> `
  --acquisition-route <author_posted|institutional|publisher_oa|user_scan|user_delivered|api|web> `
  --launch-id <your launch_id> --content-file <path-into-the-foreign-tree>

trialerror ingest add --source-id <SRC-id> --path <path-into-the-foreign-tree-or-junction> --launch-id <your launch_id>
```

Same pipeline as `/ingest` from here — a cost-estimate gate, then
`trialerror jobs start-worker` to drive normalize → chunk → embed → index. See
`/ingest` for the full mechanics; this skill's job ends at "the bridge
exists and the first source registers cleanly."

## 6. Validate

```powershell
trialerror doctor --program-root <new-program-root>
trialerror query search "<a phrase you know is in the bridged corpus>" --program-root <new-program-root>
```

`doctor` catches a broken bridge early (a stale junction, a `[paths]`
typo pointing at a directory that doesn't exist) before it becomes a
confusing ingest failure. The search smoke confirms the bridged content
is actually reachable end-to-end, not just that a source row got created.
If either fails, fix the bridge (§4) before registering more sources —
don't work around a doctor failure by ingesting anyway.

## 7. Don't

- **Never restructure the foreign project in place.** Not even a rename
  that "would make more sense" — the import-design notes (internal, not in this export) §4 rejected exactly
  this option (TrialError's scaffold names collide semantically with common
  foreign layouts, e.g. `archive/`, `requests/`) because "doing it
  properly" breaks every embedded relative-path citation across the
  foreign project's own notes/ledgers, and "doing it naively" (bolting
  TrialError's dirs onto the foreign root beside the existing, differently-
  shaped ones of the same name) just creates two confusing parallel trees
  forever.
- **Never move a file that has embedded-path citations elsewhere** — a
  source `S047.md` referenced by exact relative path from dozens of other
  files is exactly the kind of thing that looks like a harmless tidy-up
  and isn't. If in doubt, grep the foreign project for the filename
  before touching it.
- **Large model/tool caches stay put.** Bridge them via
  `[ingest.ocr]`/`[ingest.embed]` config paths (§4a), never copy and
  never junction — a junction over a 30GB+ cache buys nothing a config
  path doesn't already give you, and doubles the ways the location can
  drift.
- **Don't guess the mapping.** If the interview (§2) didn't cover a
  directory in your inventory, ask before bridging it — an unbridged
  "stays external, unreferenced for now" is always a safe default; a
  wrongly-bridged directory is not.
- **Don't attempt the deeper table-level migration inside this skill.**
  If the user wants foreign ledgers/logs to become real TrialError rows (not
  just files TrialError can read), stop and point them at
  `docs/the migration-plan notes (internal, not in this export)` instead (see §2 point 4) — that is a
  separate, heavier, per-project design exercise, not a repeatable
  procedure this skill can perform generically.
