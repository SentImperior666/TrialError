---
artifact_id: PR-000            # registry-assigned, immutable; PR = protocol (canonical prefix table in research_artifacts.md §2)
title: "<short title>"          # used verbatim in the registry line
purpose: "<why the research PROGRAM needs this artifact — plain language, self-contained>"
# C-0014 REQUIRED purpose standard: 1-3 sentences, plain language, SELF-CONTAINED research
# rationale — the question/need in the research PROGRAM this artifact serves and why that
# matters, understandable in isolation (readable WITHOUT following depends_on edges). It
# states the RESEARCH RATIONALE, NEVER merely the DAG position or an "X of Y" cross-reference
# (e.g. "critique of TD-002", "critique B of PB-003 v1"). The author writes it at creation; a
# positional-only purpose is REJECTED at registration (registry_lint purpose rule, C-0014(2)).
supersedes: null               # prior version this replaces (artifact_id or null)
type: protocol                 # fixed for this template
version: v1                    # bumped on revision; superseded versions kept
stage: planning                # planning|in-progress|results|reflection|keystone
threads: [T?]                  # program threads touched
hypotheses: [H???]             # every hypothesis touched
depends_on: []                 # artifact DAG — provenance of OUR knowledge
gate: ungated                  # ungated|passed|passed-with-edits|failed
grounding: compliant           # compliant|partial|legacy (curriculum_operations §2)
---
<!-- AGENT-TUNED: NO abstract, NO author list, no ceremony. Claims quote-grounded ⟦S###:p###⟧ or probe-grounded (artifact refs); numbers carry uncertainty; hedge-words banned — use ledger statuses. Length = whatever density requires. -->

> **VERDICT-FIRST** — what was claimed/found/decided: …
> Ledger delta: …
> Downstream dependents: …

## Question
<!-- The single question this design answers. One sentence; exemplar bar: a2_design, t4_h1_tost_design. -->

## Hypothesis refs
<!-- Exact hypothesis ids under test, matching frontmatter `hypotheses`. -->

## Method
<!-- Full procedure, reproducible by a fresh agent without this conversation. -->

## Sample / power
<!-- Sizes and power reasoning; be HONEST about noise floors — do not claim resolution the sample cannot deliver. -->

## FROZEN predictions
<!-- Predictions fixed BEFORE data; immutable after pre-registration date. -->

## Outcome map
<!-- Which result patterns → which ledger updates. Every branch pre-committed. -->

## Budget
<!-- Time/compute/source budget. -->

## Pre-registration date
<!-- Date frozen. NO results section — results go in a PROBE-REPORT that depends_on this protocol. -->

## Registry event
<!-- [SPEC-PASS] PT→PR fixed (canonical prefix per research_artifacts.md §2). Outbox/projector
discipline (OPS_IMPROVEMENTS Cluster 3): authors NEVER append to research/ledgers/ARTIFACTS.md
directly. Emit ONE artifact_registered event to your own outbox (events/<WKP-id>/<agent>.jsonl);
the single PROJECTOR folds it into the registry. An artifact not in the registry does not exist
for citation — this event is what puts it there. -->
`{"type":"artifact_registered","ts":"<ISO-8601>","agent":"<agent-id>","artifact_id":"PR-000","artifact_type":"protocol","title":"<title>","purpose":"<self-contained research rationale, C-0014 standard>","threads":["T?"],"gate":"ungated","supersedes":null,"path":"research/artifacts/PR-000_<slug>.md","corrections_pin":"v<N>@<YYYY-MM-DD>","attempt":1,"step":"S?"}`
<!-- Projector output (canonical registry-line format, owned by the ARTIFACTS.md header):
`- PR-000 | protocol | <title> | T? | gate: ungated | supersedes: - | research/artifacts/PR-000_<slug>.md` -->
