---
name: close
description: Run the TrialError session close ritual at the end of an orchestrator session on a TrialError-scaffolded program — refuses if launches are dangling, the law pin has gone stale, hooks were never armed, or inbox items are still unread; on success renders a new suffixed handoff and marks the prior one superseded. Use this at the end of an orchestrator session.
---

# /close — session close ritual

`trialerror session close` REFUSES (never silently skips a step) on any of:

1. **Dangling launches.** Reconcile every launch you booked this session
   first:

   ```
   trialerror budget reconcile --launch-id <id> --actual-tokens <n>
   ```

2. **Unread inbox items** (the close checklist). Read them:

   ```
   trialerror inbox read
   ```

3. **A stale law pin.** If a ruling was appended since this session
   booted, close refuses. Investigate before proceeding — do not paper
   over it:

   ```
   trialerror law diff-foreign --pin <your boot_pin_version>
   ```

4. **Hooks never armed** (zero `hook_alive` events recorded this
   session). This is override-only — do not manufacture a ruling to get
   past it; get a real one from the user first:

   ```
   trialerror session close ... --override-ruling-id C-####
   ```

Once the above are clear, close with a REQUIRED course-check object (the
origin-project convention: rungs climbed, build-vs-theory split, a drift flag) as
JSON:

```
trialerror session close --course-check '{"rungs": "...", "build_vs_theory": "...", "drift_flag": false}' --notes "one-line summary"
```

On success the result names the newly-rendered handoff file (under
`handoffs/`) and confirms the previous one now carries a supersession
notice — never hand-edit either file; both are rendered views over the
`session` row in ops.db. If the handoff file is ever lost or corrupted
without the session row changing, `trialerror session render-handoff
--session-id <id>` re-flushes it from ops.db truth without bumping to a
new suffix.

If a session crashed instead of closing cleanly (the Stop hook or a
future boot reports it), close is not the right tool — use
`trialerror session abandon --session-id <id> --reason "..."` so the next
`trialerror session boot` is not blocked by a session nobody will ever close.
