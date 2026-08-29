---
name: gate-critic
description: Run a gated artifact through its two-tier review — a structural validator pass, then a tool-locked, read-only critic subagent — record the verdict, apply the edit union, and verify every blocking edit before advancing the gate to registered. Use this whenever a gated-template artifact (e.g. a keystone) needs review before registration, or when a submitted gate is sitting unreviewed.
---

# /gate-critic — structural validator, then critic (applier-verifies)

Design Section 5.3 (`/gate-critic`) + Section 9.4 (typed artifacts + gates +
applier-verifies) + Section 12 M10 row. The state machine is
`draft -> submitted -> gated -> union_applied -> registered` (or `-> failed`
on a FAIL verdict); `register_artifact` itself refuses a gated-type artifact
that hasn't reached `union_applied` — there is no way around this loop for
a gated template.

1. **Confirm the artifact is actually gate-eligible** before doing
   anything else — only a `template.gated=1` type_key requires this path;
   `trialerror artifact show --id <ART-id>` tells you the type and current
   status. If it's `draft` and ungated, `trialerror artifact register` alone is
   the right tool, not this skill.

2. **Open + submit the gate**, once the artifact itself is finished and
   ready for review (never open a gate on a half-written draft to "hold a
   place in line"):

   ```
   trialerror gate open --artifact-id <ART-id>
   trialerror gate submit --id <CR-id> --by-launch <your launch_id> [--evidence '{"...":"..."}']
   ```

3. **Tier 1 — structural validator.** Mechanical, deterministic: does the
   artifact conform to its `template` type's required sections/fields?
   Every citation marker (`[[cite:ANC-...]]`) resolvable? Run `trialerror verify
   citecheck <artifact_id> --by-launch <id>` as part of this tier — a
   structural failure here should be fixed and RE-submitted, not carried
   into tier 2 for the critic to also flag.

4. **Tier 2 — critic, spawned as a genuinely separate, tool-locked
   subagent.** Book and spawn it with a prompt that carries this
   restriction VERBATIM (design's own binding language — do not paraphrase
   it away):

   > "You are reviewing this artifact for the gate process. This is
   > VALIDATION ONLY — do not modify ANY files. You may only read. Produce
   > a verdict (PASS / PASS_WITH_EDITS / FAIL) with specific, evidence-
   > anchored edits if not a clean PASS."

   The critic subagent should be tool-locked to `[Read]` only — no
   `Edit`/`Write`/`Bash`. If your environment can't enforce that
   structurally, the prompt restriction above is load-bearing; do not spawn
   a critic with broader tools and just "ask nicely."

5. **Record the verdict** (this single call both writes the verdict fields
   AND advances the gate — `submitted -> gated` on PASS/PASS_WITH_EDITS, or
   `submitted -> failed` on FAIL, in one transaction):

   ```
   trialerror gate verdict --id <CR-id> --verdict <PASS|PASS_WITH_EDITS|FAIL> \
     --critic-launch <critic's launch_id> \
     --edits '[{"text":"...","blocking":true}, ...]' \
     [--reproduction-ref <path>]
   ```

   A FAIL is a real, useful outcome — it lands the gate in its terminal
   `failed` state with the reasoning attached, not a thing to argue the
   critic out of.

6. **Applier-verifies each blocking edit — a SEPARATE tool-locked
   `[Read, Edit]` subagent applies edits (never the critic itself, never a
   subagent that can also regenerate the artifact wholesale — "apply the
   edit union" must never become "rewrite the artifact").** After each
   blocking edit is actually applied to the artifact file:

   ```
   trialerror gate verify-edit --id <CR-id> --edit-id <edit-id> --by-launch <applier launch_id> \
     --verified-note "<what changed and where>"
   ```

   Every BLOCKING edit needs this before the next step will succeed — a
   PASS_WITH_EDITS whose edits were never applied+verified is not
   actually done.

7. **Apply the union** (`gated -> union_applied`) — refuses unless the
   verdict was a pass value, every blocking edit is verified, and
   reproduction (if attached) did not mismatch:

   ```
   trialerror gate apply-union --id <CR-id> --by-launch <your launch_id>
   ```

8. **Register.** This is what actually advances `union_applied ->
   registered` for a gated type (do not call `gate advance --to registered`
   directly — `register_artifact` is the entry point that also writes the
   registry row in the same transaction):

   ```
   trialerror artifact register --id <ART-id> --by-launch <your launch_id>
   ```

9. If you need the raw state-machine transition for something the named
   verbs above don't cover, `trialerror gate advance --id <CR-id> --to <state>
   --by-launch <id>` is the generic low-level entry point — it still
   refuses any illegal edge, but reach for the named verb first; it's
   there because most transitions carry a business precondition the raw
   `advance` alone won't check for you.
