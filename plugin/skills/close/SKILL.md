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

## Capture lessons before closing: the `l0_abstract` authoring rule (wikiskill-F6)

Close is when a session's lessons, rules, facts and preferences are written to
memory (`trialerror memory put`). Boot and `memory search` return `l0_abstract`
lines and never bodies, so the abstract is the only thing that decides whether
an item is ever read again — an abstract that is a topic label ("notes on
reconciliation") is a line nobody expands. Every `l0_abstract` therefore
states three things, in one or two sentences:

**PROBLEM** — what went wrong, or what question kept recurring.
**ROOT CAUSE** — why; the cause, not the symptom.
**FIX** — the exact action or command that resolves it.

```
trialerror memory put --key close.reconcile-before-close --tier L0 --kind lesson \
  --account <account-id> \
  --l0-abstract "PROBLEM: close refused on a dangling launch whose subagent had long since returned. ROOT CAUSE: post_task only FLAGS a RUNNING launch for reconciliation; nothing reconciles it. FIX: run 'trialerror budget reconcile --launch-id <id> --actual-tokens <n>' for every RUNNING row before 'session close'." \
  --body "<the full lesson: exact command sequence as run, when it applies, when it does not>"
```

Body rules (the sibling pattern-page rules from the same source): root cause,
not symptom; exact command sequences copied from the run, not paraphrased;
update the existing item rather than adding a near-duplicate — `put` upserts
by `(key, account)`, so reuse the key; 10-30 lines. An abstract that is only a
label, or a body that is only a symptom, is an item to rewrite before close,
not to accept. Nothing enforces this in code yet (`put` takes any string), so
it is a review rule: read your own abstracts back once before closing.

Source: WikiSkill (arXiv:2608.27454, CC BY 4.0) §E.2 — index entries "are the
MOST IMPORTANT part of the wiki ... they determine whether inference agents
will read the full pattern pages", each stating PROBLEM + ROOT CAUSE + FIX.
Two code follow-ups are tracked separately and will surface, never rewrite,
items: save-time conflict candidates (advisory only) and age-based
`needs_review` surfacing as a doctor check.

## When NOT to apply

- To get past a refusal. Dangling launches, a stale pin and an unread inbox
  are the cause to fix; `--override-ruling-id` is for a real ruling from the
  user, never a manufactured one.
- The session crashed — `trialerror session abandon` is the tool, not close.
- To hand-edit a handoff. Both handoff files are rendered views over the
  session row; `render-handoff` re-flushes them.
- As a mid-session checkpoint. The course-check is an end-of-session object;
  mid-session notes go to events or the feed.
