---
name: boot
description: Run the TrialError session boot ritual at the start of a research-harness orchestrator session on a TrialError-scaffolded program — verifies the law pin, binds the account, and surfaces dangling launches, unread inbox, budget headroom, and the last handoff. Use this at the start of an orchestrator session, or when SessionStart reported it could not boot automatically.
---

# /boot — session boot ritual

The `SessionStart` hook (`plugin/hooks/session_start.py`) already ran this
automatically when this Claude Code session started, UNLESS it reported a
diagnostic on stderr (e.g. ambiguous account — more than one account is
registered and none was specified, or zero accounts registered yet). This
skill is the manual/fallback path; hooks are already armed either way — it
does not "arm" anything.

1. Check whether a session is already open and read its bundle:

   ```
   trialerror session status
   ```

2. If nothing is open (or the hook reported an account-resolution
   failure), boot explicitly:

   ```
   trialerror session boot --account <account-id>
   ```

   First-ever boot on a brand-new program (zero accounts registered yet):

   ```
   trialerror session boot --create-account "<your label>"
   ```

   `trialerror session boot` is idempotent by default: if a session is already
   open it returns that session's CURRENT bundle rather than refusing.

3. Read the returned bundle in full before doing anything else:
   - `pin_status` / `foreign_since_last` — law rulings appended since your
     last session; read them before spawning anything (mid-flight
     staleness is visible-not-refused, but boot-time staleness should not
     be ignored).
   - `dangling_launches` — PROVISIONAL/RUNNING launches orphaned by a
     session that never closed cleanly. Investigate before spawning more;
     if that prior session crashed, mark it `trialerror session abandon
     --session-id <id>` once you understand why.
   - `inbox_items` — the user's unread messages (already marked read by
     boot; do not re-fetch, but do NOT skip reading them here).
   - `budget` — pool headroom per model class.
   - `active_jobs` — detached workers (OCR/embed/index/...) still running
     from a prior session; they legitimately outlive it.
   - `latest_handoff_markdown` — the previous session's handoff, if any.

4. Do not re-run `trialerror session boot` again this session unless you closed
   or abandoned the current one — with `--fresh` it refuses outright while
   a session is already open (without `--fresh`, the default, it is a
   no-op that just re-reads the bundle).

## Reading the L0 memory index (wikiskill-F6)

The bundle's L0 memory index is a list of `l0_abstract` lines, never bodies.
Under the authoring rule (`/close`), each line states PROBLEM + ROOT CAUSE +
FIX; read them as such — expand with `trialerror memory search --id <item_id>`
only the items whose PROBLEM matches the work this session is about to do, and
leave the rest folded. A line that is only a topic label tells you nothing
about whether to expand it: note it as an item to rewrite at close rather
than expanding every ambiguous line "to be safe".

## When NOT to apply

- A session is already open and you have read its bundle — re-running is a
  no-op, and `--fresh` refuses outright; neither re-arms anything.
- To "arm" hooks. Hooks are armed by the plugin, not by this skill; a session
  with zero `hook_alive` events is a disabled-hooks problem, not a boot
  problem.
- As a substitute for reading `foreign_since_last`. A successful boot is not
  the same as having read the rulings it surfaced.
- On a prior session that crashed — `trialerror session abandon` first, then
  boot.
