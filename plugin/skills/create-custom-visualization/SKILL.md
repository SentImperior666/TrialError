---
name: create-custom-visualization
description: Guided procedure to build a custom dashboard panel for YOUR program -- inventory what your program's stores actually hold, design within the dashboard's legibility rules, scaffold a panel.toml + builder.py extension under your own program root, write an offline test, and verify it live with `trialerror dashboard serve`. Use this when the user wants a bespoke visualization, chart, or view on top of their program's data that the six built-in dashboard panels (session/budget/jobs/gates/corpus/doctor) don't cover, or asks to "add a panel", "build a custom view", "visualize X on the dashboard", or "make a dashboard extension".
---

# /create-custom-visualization -- your data, your panel, your program root

User ruling C-0070 (verbatim, the origin of this skill): *"custom
visualizations are PER-PROJECT extensions, not core TrialError surfaces ...
TrialError gains a create-custom-visualization SKILL that guides a user's coding
agent to build such panels (protocol: program-root trialerror_ext/panels/
manifests + builders over the read-only store, rendered by the dashboard
when that program is active) -- sibling in spirit to the C-0068
import-existing-project skill."*

The governing principle: **a custom panel lives in YOUR program, not in
TrialError.** TrialError ships the protocol (discovery + the generic renderer +
crash isolation) and this one skill; every panel itself is your own
program's file, written against your own program's data, registered
nowhere in TrialError's own repository. This mirrors `import-existing-project`'s
"bridge, don't move" principle one layer up: TrialError's job is to make room
for your code, never to absorb it.

**The pattern to follow:** read `trialerror/dashboard/ext.py`'s own module
docstring for the full discovery protocol. A panel that finds no matching
data in the active program's stores should return a documented
`{"status": "awaiting_data", ...}` payload (plus a clearly-labeled `"demo"`
block showing the intended shape) rather than erroring or showing nothing --
"read what exists, degrade honestly."

## 0. Before you start

- One panel = one directory: `<your-program-root>/trialerror_ext/panels/<name>/`,
  containing exactly two files, `panel.toml` (manifest) and `builder.py`
  (data). `trialerror.dashboard.ext` discovers every such directory under the
  ACTIVE program root -- nothing to register, nothing to toggle. A program
  with no `trialerror_ext/panels/` directory shows none; that is the entire
  mechanism behind "this view only loads when this program is active."
- Read `trialerror/dashboard/ext.py`'s module docstring once before writing your
  first `builder.py` -- it states plainly that a `builder.py` is imported
  and run as ordinary Python, in-process, with no sandbox. That is
  intentional (it is YOUR OWN trusted program code, exactly like anything
  else your `trialerror.toml` already points TrialError at), and it is also why a
  broken panel is contained (crash-isolated into one broken tab's worth of
  JSON) rather than sandboxed -- know the difference before you rely on
  either property.
- A panel's `build_panel(rostore, program_root)` gets a **read-only**
  store (`trialerror.dashboard.store_ro.RoStore`) -- the same one every core
  panel builder uses. It cannot write, even by accident: the underlying
  SQLite connections are opened `mode=ro` at the OS level. Query it freely;
  never look for a write path around it.

## 1. Inventory what your program's stores actually hold

Don't design a panel around what you assume is there -- open a real
`RoStore` against your program root and look, the same "actually enumerate
it" discipline `import-existing-project` §1 uses for a foreign project's
files:

```python
from trialerror.dashboard.store_ro import open_store_ro

rostore = open_store_ro("<your-program-root>")
for kind in ("platform", "ops", "knowledge", "jobs"):
    conn = getattr(rostore, kind)
    if conn is None:
        print(kind, "-- not initialized")
        continue
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()]
    print(kind, tables)
rostore.close()
```

For a specific table, `PRAGMA table_info(<table>)` gives you columns; a
`SELECT * FROM <table> LIMIT 5` gives you real values, not a schema's
promise of them. If your panel's data is opaque JSON in a generic column
(the census rows in `knowledge.record.payload` are exactly this shape --
see the worked example), inspect a few real rows before committing to a
field list; a schema-shaped table and a JSON blob column need different
inventory habits, and guessing at either wastes the scaffold step below.

Decide, from what you actually saw:
- **What table(s) does this panel read?** -- becomes `min_schema` in your
  manifest (advisory documentation, not enforced by the loader -- it exists
  so the NEXT person reading your `panel.toml` knows what your builder
  expects without opening `builder.py`).
- **Does the data exist yet, always, sometimes, or never?** A panel over
  data that might not have landed yet (an early-pipeline program, a fresh
  `trialerror program init`) needs the "read what exists, degrade honestly"
  shape from §3 below -- decide this now, not as an afterthought once your
  happy-path builder already crashes on an empty table.

## 2. Design within the dashboard's rules

The current dashboard (`trialerror/dashboard/static/dashboard.html` +
`dashboard.css`) is deliberately MINIMAL-FUNCTIONAL: every panel, core or
extension, renders through the SAME generic JSON-to-DOM renderer (nested
dicts become key/value tables, arrays of objects become row tables,
booleans become a `.status-badge` span) -- see that file's own
`FRONTEND-CONTRACT` comment. Your `build_panel` returns plain JSON-shaped
data (dicts, lists, strings, numbers, booleans, `None`); you never write
HTML, and you never need to. A handful of the dashboard's own design rules
still apply to how you SHAPE that data, carried over from the design
review that will eventually re-skin this page
(`docs/reviews/REDESIGN_V2_RATIONALE.md`, `design/dashboard-v2/Tokens.dc.html`):

- **Icon-plus-label statuses, never colour alone.** The generic renderer's
  only built-in colour cue is `.status-badge` on a bare boolean. For a
  richer state (pass/warn/fail, ok/awaiting_data/ext_error, ...), return a
  short descriptive STRING (`"pass"`, `"awaiting_data"`), not a bare `true`/
  `false` standing in for a state with more than two values, and not a
  colour name. The existing panels already follow this (`data.py`'s own
  `"not_initialized"` / `"invariant_violation"` / `"never_run"` statuses,
  this protocol's own `"ext_error"`) -- match that vocabulary style: a
  short, lowercase, machine-and-human-readable word.
- **4.5:1 contrast, no large-text exemption.** If you ever add a rule to
  `dashboard.css` for your own panel (rare -- the generic renderer usually
  needs nothing), the review's own type floor is 10px, and at 10px WCAG's
  large-text exemption never applies -- every text/background pair must
  clear 4.5:1. A tiny, dependency-free checker (WCAG 2.1 relative
  luminance, `(L1 + 0.05) / (L2 + 0.05)`):

  ```python
  def _srgb_to_linear(c: float) -> float:
      c /= 255.0
      return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

  def relative_luminance(hex_color: str) -> float:
      hex_color = hex_color.lstrip("#")
      r, g, b = (int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
      r, g, b = _srgb_to_linear(r), _srgb_to_linear(g), _srgb_to_linear(b)
      return 0.2126 * r + 0.7152 * g + 0.0722 * b

  def contrast_ratio(hex_a: str, hex_b: str) -> float:
      l1, l2 = sorted((relative_luminance(hex_a), relative_luminance(hex_b)), reverse=True)
      return (l1 + 0.05) / (l2 + 0.05)

  # usage: contrast_ratio("#e8f2ec", "#0b120e") -> e.g. 15.2 (pass, >= 4.5)
  assert contrast_ratio("#e8f2ec", "#0b120e") >= 4.5
  ```

- **10px type floor.** Never author inline styles below 10px if your panel
  ever emits presentational text of its own.
- **Nothing loops that isn't live.** `design/dashboard-v2/Tokens.dc.html`'s
  own rule: "every loop renders its final frame" -- animation is one-shot
  (a value changing because the SSE watcher just pushed a real update), never
  decorative perpetual motion. A panel's data has no motion at all; that
  rule matters only if you ever add a script of your own -- don't spin,
  pulse, or animate anything that isn't reflecting a genuine state change.
- **`nav_group` is forward-looking metadata, honestly.** `panel.toml`
  carries `nav_group = "KNOW"` (a view into what the program has learned --
  a corpus, a census, a term list) or `"RUN"` (operational cockpit state --
  jobs, budgets, gates); the shipped V2 design groups navigation this way.
  The CURRENT dashboard (the one `trialerror dashboard serve` runs today) does
  not yet render grouped navigation -- it lists every core panel as a flat
  row of tabs, and does not yet build a tab for extension panels at all
  (see this skill's §5 for exactly what "verify live" means today). Set
  `nav_group` correctly anyway; it costs nothing now and is exactly the
  field the V2 rebuild will read.

## 3. Scaffold `panel.toml` + `builder.py`

```
<your-program-root>/trialerror_ext/panels/<name>/
    panel.toml
    builder.py
```

**`panel.toml`** -- a `[panel]` table, three required fields
(`title`, `nav_group`, `order`), two optional (`description`,
`min_schema`):

```toml
[panel]
title = "Job Kind Mix"
nav_group = "RUN"
order = 10
description = "Live job counts grouped by kind -- how much of each pipeline stage is queued right now."
min_schema = ["job"]
```

`nav_group` must be exactly `"KNOW"` or `"RUN"`; `order` must be an
integer (lower sorts first among your own panels -- it does not compete
with core panels, which are not reordered by this field). An invalid or
missing manifest never crashes the dashboard -- it turns your panel into
one broken tab reporting `manifest_error` (see §6's doctor check), same as
every other failure mode this protocol isolates.

**`builder.py`** -- exactly one required export:

```python
"""Job Kind Mix -- a tiny worked micro-example for this skill.

Counts live trialerror.dashboard's own `jobs.job` rows by kind. Demonstrates
the "read what exists, degrade honestly" shape: not_initialized when the
jobs store doesn't exist yet, a real histogram otherwise -- the SAME two-
state shape trialerror.dashboard.data's own build_jobs_panel already uses, so
an extension panel that reads a core table reads exactly like a core one.
"""

from __future__ import annotations


def build_panel(rostore, program_root) -> dict:
    if not rostore.is_available("jobs"):
        return {"status": "not_initialized", "message": "jobs.db not found"}

    rows = rostore.jobs.execute("SELECT kind, COUNT(*) AS n FROM job GROUP BY kind").fetchall()
    counts = {r["kind"]: r["n"] for r in rows}
    return {
        "status": "ok",
        "kind_counts": counts,
        "total_jobs": sum(counts.values()),
    }
```

`build_panel` receives `(rostore, program_root)` positionally and must
return a plain `dict` -- anything else (an exception, a non-dict return,
an import that fails, a `build_panel` that doesn't exist or has the wrong
signature) is caught by `trialerror.dashboard.ext` and turned into
`{"status": "ext_error", "message": "..."}` automatically. You do not
need to (and should not) wrap your own builder in a top-level
`try/except` for this -- that isolation is the protocol's job, not yours;
adding your own blanket catch just hides the real error from the message
the dashboard shows.

## 4. Write an offline test

Test your builder directly, against a real (throwaway) program store --
no dashboard server needed. This template is self-contained; copy it into
your own program repo's test suite (`trialerror` is your program's own
dependency, already importable):

```python
from pathlib import Path

from trialerror.dashboard.ext import build_ext_panel, load_ext_panel_entry
from trialerror.dashboard.store_ro import open_store_ro
from trialerror.stores import insert
from trialerror.stores.store import open_store


def test_job_kind_mix_panel(tmp_path):
    program_root = tmp_path / "program"
    program_root.mkdir()
    (program_root / "trialerror.toml").write_text('[program]\nid = "PROG-test"\n', encoding="utf-8")
    platform_root = tmp_path / "platform"

    # seed exactly the rows your builder needs -- here, two jobs of one kind
    store = open_store(program_root, platform_root=platform_root)
    for i in range(2):
        insert(store, "job", {
            "job_id": f"JOB-{i}", "kind": "embed", "payload": "{}",
            "state": "pending", "created_ts": "2026-01-01T00:00:00Z",
        })
    store.close()

    panel_dir = program_root / "trialerror_ext" / "panels" / "job_kind_mix"
    # in your real program this directory already exists on disk; a test
    # can also point load_ext_panel_entry() at any directory directly.
    entry = load_ext_panel_entry("job_kind_mix", panel_dir)
    assert entry.manifest_status == "ok", entry.manifest_error

    rostore = open_store_ro(program_root, platform_root=platform_root)
    try:
        result = build_ext_panel(entry, rostore, program_root)
    finally:
        rostore.close()

    assert result["status"] == "ok"
    assert result["kind_counts"] == {"embed": 2}
```

Also test the degrade path (no `jobs.db` at all -- just don't call
`open_store()` / don't insert anything) and, if your builder can raise on
malformed input, that a deliberately-broken case still comes back as
`ext_error` rather than propagating -- `build_ext_panel` guarantees this,
but your own test is what tells you your builder's error messages are
actually useful to read, not just non-fatal.

## 5. Verify live

```powershell
trialerror dashboard serve --program-root <your-program-root>
```

Then, in a second terminal (or your browser's dev tools), check the data
path end to end:

```powershell
curl http://127.0.0.1:8850/dashboard/api/ext            # the listing -- your panel's manifest info
curl http://127.0.0.1:8850/dashboard/api/ext/<name>      # your panel's live data
curl http://127.0.0.1:8850/dashboard/api/all             # panels.ext.<name> alongside every core panel
```

Be honest with yourself about what "verify live" means TODAY: the served
`dashboard.html` page's tab bar is a fixed list (session/budget/jobs/
gates/corpus/doctor) and does not yet build a tab for `panels.ext` --
that page is explicitly placeholder scaffolding pending the V2 rebuild
(`design/dashboard-v2/`, see `dashboard.html`'s own `SCOPE NOTE` comment),
and this skill does not extend it (no core-repo frontend changes ship
with the extension protocol itself). Verifying live means confirming the
THREE endpoints above return your panel's real data correctly-shaped --
not (yet) seeing a new tab render in the browser. `trialerror doctor
--program-root <your-program-root>` is the other half of "verify": it
runs `ext_panels_valid` and reports `warn` with your panel's exact
manifest/import/signature failure message if something's wrong, `pass`
if every extension panel you've declared is sound.

## 6. Don't

- **Don't register your panel anywhere in TrialError's own repository.** No
  entry in `trialerror/dashboard/data.py`'s `PANEL_BUILDERS`, no PR against
  `trialerror/dashboard/*`, nothing added to `trialerror.toml`. The panel's existence
  IS its directory under your program's own `trialerror_ext/panels/` -- that is
  the entire registration step, by design (C-0070(a)'s "loads only when
  that program is active", satisfied by discovery rooting at the active
  program root, never by a name TrialError itself has to know about).
- **Don't wrap `build_panel` in your own blanket `try/except`.** See §3 --
  `trialerror.dashboard.ext` already isolates every failure mode into
  `ext_error`; swallowing your own exceptions just replaces a specific,
  useful message with a generic one.
- **Don't write to the store.** `RoStore`'s connections are read-only at
  the SQLite driver level -- a stray `INSERT`/`UPDATE` fails loudly
  (`sqlite3.OperationalError: attempt to write a readonly database`).
  If your panel needs derived data that doesn't exist yet, compute it
  in-memory inside `build_panel` every request (the same "no rebuild
  step, re-read fresh" design the whole dashboard already follows) --
  never stage it into a table of your own inside the program's stores.
- **Don't assume your data exists.** Every core panel in `trialerror/dashboard/
  data.py` reports `{"status": "not_initialized", ...}` rather than
  crashing when its store file is missing -- match that shape (§1's second
  bullet, the worked example's `awaiting_data` payload) instead of letting
  an empty table surface as a stack trace your users read as `ext_error`.
- **Don't build the V2 visual redesign yourself.** If what you actually
  want is the census-space MAP (points, colour, layout, pan/zoom) rather
  than a JSON summary a generic table can render, that is
  `design/dashboard-v2/Atlas.dc.html`'s job, not this skill's -- this
  protocol gets your data TO the dashboard; it does not draw it for you
  beyond the generic key/value and row-table renderer.
