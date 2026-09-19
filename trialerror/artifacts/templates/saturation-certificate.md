---
artifact_id: SC-000            # registry-assigned, immutable ([SPEC-NOTE] SC prefix is NEW with this 12th template; research_artifacts.md §2 prefix table gains the SC row — flagged at WKP-005 S2)
title: "<short title>"          # used verbatim in the registry line
purpose: "<why the research PROGRAM needs this artifact — plain language, self-contained>"
# C-0014 REQUIRED purpose standard: 1-3 sentences, plain language, SELF-CONTAINED research
# rationale — the question/need in the research PROGRAM this artifact serves and why that
# matters, understandable in isolation (readable WITHOUT following depends_on edges). It
# states the RESEARCH RATIONALE, NEVER merely the DAG position or an "X of Y" cross-reference
# (e.g. "critique of TD-002", "critique B of PB-003 v1"). The author writes it at creation; a
# positional-only purpose is REJECTED at registration (registry_lint purpose rule, C-0014(2)).
supersedes: null               # prior version this replaces (artifact_id or null)
type: saturation-certificate   # fixed for this template
version: v1                    # bumped on revision; superseded versions kept
stage: results                 # planning|in-progress|results|reflection|keystone
threads: [T?]                  # program threads touched
hypotheses: []                 # hypotheses the empty verdict bears on
depends_on: []                 # the artifact whose "no literature exists" verdict this certifies
gate: ungated                  # ungated|passed|passed-with-edits|failed
grounding: compliant           # compliant|partial|legacy (curriculum_operations §2)
empty_verdict: "<one sentence: the claim of absence being certified>"
counter_searcher: null         # agent id of the BLIND counter-searcher (MUST differ from author; null until slot filled — certificate is INCOMPLETE while null)
corrections_pin: v0@0000-00-00 # echo the pin injected at spawn
---
<!-- AGENT-TUNED: NO abstract, NO ceremony. OPS_IMPROVEMENTS Cluster 1: an EMPTY
verdict ("no literature exists / no prior art / nothing found") counts ONLY with this
certificate attached: PRISMA-S-lite search manifest + blind counter-search by a
DIFFERENT agent + spot-check of dismissed hits. Dual-screening evidence: 13%->3% miss;
our observed 15% predicts ~5x reduction. -->

> **VERDICT-FIRST** — the absence claim, verbatim: …
> Certified: YES/NO (NO until the counter-search slot and spot-check are filled)
> Downstream dependents: …

## Search manifest (PRISMA-S-lite)
<!-- Reproducible by a stranger. One row per executed query — no summarizing away. -->
| # | database/engine | query (verbatim) | date | hits | screened | dismissed | kept |
|---|---|---|---|---|---|---|---|

### Screens applied
<!-- Inclusion/exclusion criteria, stated BEFORE screening; note any criterion added mid-search (that is a deviation — say so). -->

## Blind counter-search slot
<!-- Filled by a DIFFERENT agent (frontmatter `counter_searcher`), blind to the manifest above: they receive ONLY the absence claim, not the queries. Their manifest goes here in the same table format. Verdict: CONFIRMS-EMPTY | FOUND-SOMETHING (refs). A certificate without this slot filled is INCOMPLETE and the empty verdict does not count. -->
- counter_searcher: <agent id>
- blind to author manifest: YES/NO (NO invalidates the slot)
- verdict: …

## Dismissed-hit spot-check
<!-- Sample of hits dismissed at screening, re-examined against the screens; catches over-eager dismissal. Minimum: 5 hits or 20% of dismissed, whichever is larger (all of them if fewer than 5). -->
| dismissed hit | dismissal reason | spot-check verdict (correct/incorrect dismissal) |
|---|---|---|

## Residual uncertainty
<!-- What the searches CANNOT rule out (paywalled corpora queued per curriculum_operations §4, non-English literature, grey literature, etc.). Enumerate honestly. -->

## Registry event
<!-- [SPEC-PASS] Outbox/projector discipline (OPS_IMPROVEMENTS Cluster 3): authors NEVER append to
research/ledgers/ARTIFACTS.md directly. Emit ONE artifact_registered event to your own outbox
(events/<WKP-id>/<agent>.jsonl); the single PROJECTOR folds it into the registry. An artifact not
in the registry does not exist for citation — this event is what puts it there. `step` is REQUIRED
(outbox_projector.md m7): stepless registrations reject SPINE-5.
[FUTURE WORK, flagged at WKP-005 S3]: a filled-instance validator (checks that a COMPLETED
certificate has a non-empty manifest table, a counter_searcher distinct from the author, and a
spot-check meeting the minimum) is NOT built yet — the registry-event line above is already
schema-complete, but instance completeness is currently enforced by the critique gate, not a lint. -->
`{"type":"artifact_registered","ts":"<ISO-8601>","agent":"<agent-id>","artifact_id":"SC-000","artifact_type":"saturation-certificate","title":"<title>","purpose":"<self-contained research rationale, C-0014 standard>","threads":["T?"],"gate":"ungated","supersedes":null,"path":"research/artifacts/SC-000_<slug>.md","corrections_pin":"v<N>@<YYYY-MM-DD>","attempt":1,"step":"S?"}`
<!-- Projector output (canonical registry-line format, owned by the ARTIFACTS.md header):
`- SC-000 | saturation-certificate | <title> | T? | gate: ungated | supersedes: - | research/artifacts/SC-000_<slug>.md` -->
