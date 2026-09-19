---
artifact_id: WP-000            # registry-assigned, immutable
title: "<short title>"          # used verbatim in the registry line
purpose: "<why the research PROGRAM needs this artifact — plain language, self-contained>"
# C-0014 REQUIRED purpose standard: 1-3 sentences, plain language, SELF-CONTAINED research
# rationale — the question/need in the research PROGRAM this artifact serves and why that
# matters, understandable in isolation (readable WITHOUT following depends_on edges). It
# states the RESEARCH RATIONALE, NEVER merely the DAG position or an "X of Y" cross-reference
# (e.g. "critique of TD-002", "critique B of PB-003 v1"). The author writes it at creation; a
# positional-only purpose is REJECTED at registration (registry_lint purpose rule, C-0014(2)).
supersedes: null               # prior version this replaces (artifact_id or null)
type: white-paper              # fixed for this template
version: v1                    # bumped on revision; superseded versions kept
stage: keystone                # planning|in-progress|results|reflection|keystone
threads: [T?]                  # program threads touched
hypotheses: []                 # every hypothesis the finding rests on
depends_on: []                 # the registered artifacts this paper is written from
gate: ungated                  # ungated|passed|passed-with-edits|failed
grounding: compliant           # compliant|partial|legacy (curriculum_operations §2)
---
<!-- HUMAN-TUNED KEYSTONE ARTIFACT. Pre-flight checklist — author must affirm ALL FOUR keystone criteria (research_artifacts.md §4) before writing:
  [ ] 1. Gate-passed with no open BROKEN items.
  [ ] 2. Kill-test escrow clear for every claim it rests on (no preferred-on-untested dimensions).
  [ ] 3. Reproducible from registered artifacts by a fresh agent (the next-consumer test, executable form).
  [ ] 4. Materially moves the origin-project critical path (phase gates Gα/Gβ/Gγ are the natural keystone moments).
White papers are FEW by design. Written FROM depends_on artifacts; adds NO new claims — a white paper that needs a new claim is missing an intermediate artifact; write that first. -->

## Abstract
<!-- Human-tuned: motivation and story ARE wanted here. A real abstract for a human reader. -->

## Narrative arc
<!-- The motivation and story of the finding — problem, approach, result — readable start to finish. -->

## Figures
<!-- Figures with captions; each traceable to a registered artifact. -->

## Related-work positioning
<!-- Where this sits against the surveyed literature (cite the SURVEY artifacts). -->

## Honest limits
<!-- The limitations section, carried over undiluted from the underlying probe-reports. -->

## Registry event
<!-- [SPEC-PASS] Note: WP-### is the WHITE-PAPER artifact prefix ONLY; work packages are WKP-###
(rename per spec-pass — no lexical collision). Outbox/projector discipline (OPS_IMPROVEMENTS
Cluster 3): authors NEVER append to research/ledgers/ARTIFACTS.md directly. Emit ONE
artifact_registered event to your own outbox (events/<WKP-id>/<agent>.jsonl); the single
PROJECTOR folds it into the registry. An artifact not in the registry does not exist for
citation — this event is what puts it there. -->
`{"type":"artifact_registered","ts":"<ISO-8601>","agent":"<agent-id>","artifact_id":"WP-000","artifact_type":"white-paper","title":"<title>","purpose":"<self-contained research rationale, C-0014 standard>","threads":["T?"],"gate":"ungated","supersedes":null,"path":"research/artifacts/WP-000_<slug>.md","corrections_pin":"v<N>@<YYYY-MM-DD>","attempt":1,"step":"S?"}`
<!-- Projector output (canonical registry-line format, owned by the ARTIFACTS.md header):
`- WP-000 | white-paper | <title> | T? | gate: ungated | supersedes: - | research/artifacts/WP-000_<slug>.md` -->
