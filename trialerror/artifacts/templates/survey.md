---
artifact_id: SV-000            # registry-assigned, immutable
title: "<short title>"          # used verbatim in the registry line
purpose: "<why the research PROGRAM needs this artifact — plain language, self-contained>"
# C-0014 REQUIRED purpose standard: 1-3 sentences, plain language, SELF-CONTAINED research
# rationale — the question/need in the research PROGRAM this artifact serves and why that
# matters, understandable in isolation (readable WITHOUT following depends_on edges). It
# states the RESEARCH RATIONALE, NEVER merely the DAG position or an "X of Y" cross-reference
# (e.g. "critique of TD-002", "critique B of PB-003 v1"). The author writes it at creation; a
# positional-only purpose is REJECTED at registration (registry_lint purpose rule, C-0014(2)).
supersedes: null               # prior version this replaces (artifact_id or null)
type: survey                   # fixed for this template
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

## Scope + questions
<!-- Exact literature scope and the numbered questions asked of it. Exemplar bar: the gapdr_T# reports. -->

## Per-question state of the art
<!-- One subsection per question: state-of-art verdict + must-reads, EACH with an extraction goal (what a reader must pull from it). -->

## EMPTY verdicts
<!-- Questions the literature does NOT answer — flag each explicitly as a publication opportunity. -->

## Ledger-ready source lines
<!-- Lines formatted for direct paste into sources.md; no rework needed downstream. -->

## What changed for which hypotheses
<!-- Per hypothesis id: how this survey moves its status/evidence. -->

## Registry event
<!-- [SPEC-PASS] Outbox/projector discipline (OPS_IMPROVEMENTS Cluster 3): authors NEVER append to
research/ledgers/ARTIFACTS.md directly. Emit ONE artifact_registered event to your own outbox
(events/<WKP-id>/<agent>.jsonl); the single PROJECTOR folds it into the registry. An artifact not
in the registry does not exist for citation — this event is what puts it there. -->
`{"type":"artifact_registered","ts":"<ISO-8601>","agent":"<agent-id>","artifact_id":"SV-000","artifact_type":"survey","title":"<title>","purpose":"<self-contained research rationale, C-0014 standard>","threads":["T?"],"gate":"ungated","supersedes":null,"path":"research/artifacts/SV-000_<slug>.md","corrections_pin":"v<N>@<YYYY-MM-DD>","attempt":1,"step":"S?"}`
<!-- Projector output (canonical registry-line format, owned by the ARTIFACTS.md header):
`- SV-000 | survey | <title> | T? | gate: ungated | supersedes: - | research/artifacts/SV-000_<slug>.md` -->
