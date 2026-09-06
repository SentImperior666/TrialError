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

## Frozen candidate: pin the body before the critic reads it (HoH-F3)

Nothing in the gate state machine re-reads the artifact file between
`submitted` and `union_applied`: the `artifact.sha256` column is whatever the
author declared at `artifact create`, and `register_artifact` does not
recompute it. So an artifact can be edited while it sits `submitted`, and a
verdict can be recorded against text the critic never saw. The rule that
closes that gap, in four digests:

1. **At submit.** Immediately before `gate submit`, compute the digest of the
   file at the artifact's `path` (`sha256sum <path>`; on Windows
   `Get-FileHash -Algorithm SHA256 <path>`) and record it in the submit
   evidence:

   ```
   trialerror gate submit --id <CR-id> --by-launch <your launch_id> \
     --evidence '{"body_sha256":"<hex>","body_path":"<path>"}'
   ```

   If it differs from the row's `sha256`, stop — the body changed after
   creation. Say so in the evidence (`"declared_sha256_mismatch":true`) or
   re-create the row; never submit a body whose digest is unknown.

2. **At spawn.** The critic reads the frozen copy, not "whatever is on disk
   now": hand it the artifact text inside its spawn prompt, or copy the file
   to a scratch path that no applier will ever touch and hand it that path,
   together with the pinned digest. The critic states in its verdict which
   digest it reviewed.

3. **At verdict.** Recompute the digest before `gate verdict`. A mismatch
   means the candidate moved under review: the verdict is void. Append an
   event (`trialerror events append --type gate_candidate_moved --payload
   '{"gate_id":"<CR-id>","pinned":"<hex>","found":"<hex>"}'`), fix whatever
   edited it, and re-submit with the new digest before re-spawning a critic.
   Never quietly re-hash and carry on.

4. **At apply-union.** After the applier has applied the blocking edits and
   each one is `verify-edit`-ed, recompute once more and record it:
   `--evidence '{"body_sha256_after_edits":"<hex>"}'` on `gate apply-union`.
   The pair (pre-review digest, post-edit digest) is the audit trail that the
   only change between verdict and registration was the verified edit union.

Source: Harness-of-Harness §3.4.3 — "Freezing separates artifact production
from artifact assessment: the implementation cannot change while its evidence
is being collected" (arXiv:2609.01481, CC BY 4.0). The code half of the same
finding — a black-box/white-box check-type axis on `eval/gate_suites.py` — is a
separate lane; this section is the rule half.

## Black-box verdict first, white-box notes second (HoH-F3)

Ask the critic for two labelled parts, in this order, and keep them apart in
what you transcribe into `gate verdict`:

- **Black-box** — judged from the artifact as its reader receives it: does
  each claim follow from what it cites; would the stated procedure reproduce;
  are the required sections present with content rather than placeholders.
  The PASS / PASS_WITH_EDITS / FAIL verdict is decided HERE and does not move
  afterwards.
- **White-box** — judged from the inside: anchor-pairing quality, evidence
  selection, method choices, edits that improve the artifact without changing
  whether it passes. These land as non-blocking edits — unless one reveals a
  black-box failure, in which case the critic says so explicitly and restates
  the verdict with the reason.

Mixing the two lets a well-argued internal note rescue an artifact whose
central claim does not hold, or a stylistic complaint sink one that does.

## Pre-mortem before a judge spawns: letter vs spirit (ai-finds-a-way-F1) — REQUIRED

Before any critic, rubric or scoring instruction ships for this gate, answer
three questions for the judged step and record the answers in an event row
(`trialerror events append --type gate_premortem --payload '{"gate_id":"<CR-id>",
"proxy_gameable":..., "harness_escapable":..., "judge_steerable":...,
"mitigations":[...]}'`) and, for a pre-registered artifact, in the prereg
text. Each "yes" needs a named mitigation or the critic does not spawn:

- **(i) Is the proxy itself gameable?** Can the artifact satisfy the letter of
  the rubric — sections present, citation markers resolving, agreement
  numbers high — while missing its spirit (the central claim is not actually
  supported)?
- **(ii) Is the harness escapable?** Can the author or the applier change what
  is being judged while it is being judged (the frozen-candidate rule above),
  rewrite the artifact instead of applying the edit union, or reach the
  critic's inputs?
- **(iii) Can the judge be steered by the content it judges?** Self-assessment
  inside the artifact ("novel", "rigorously verified"), framing addressed to
  the reviewer, persuasive prose standing in for evidence, or the generator's
  own prompt being visible to the critic (it never is)?

Standing mitigations this skill already carries: the critic is Read-only and
never sees the generator prompt; the applier applies the edit union and
nothing else; the candidate is frozen by digest; the verdict is a discrete
label, not a score anything can climb; this pre-mortem sits in the critic
brief. The three questions are the same ones AIIF v2 §5.1 asks of every judged
step (that document's table carries the worked answers for AIIF's own
instruments). Paper: "AI Finds A Way" (arXiv:2608.23875) §4.1/§4.2 and §7 —
the exploited proxy, the exploited environment, and the judge that becomes a
target the moment it enters the optimisation loop.

## When NOT to apply

- The artifact's template type is not `gated=1` — `trialerror artifact register`
  alone is the right tool; opening a gate on an ungated type is noise.
- The artifact is a half-written draft — a gate is not a place in line.
- You would be spawning the critic with tools beyond `Read`, or letting the
  critic, the author, or a subagent that can regenerate the artifact apply
  the edits. Then it is not this skill; do not run it and call it a gate.
- To re-argue a FAIL. `failed` is terminal and useful; a revised artifact is a
  new row with a new gate.
- As a substitute for `trialerror verify citecheck` — Tier 1 is not optional.
