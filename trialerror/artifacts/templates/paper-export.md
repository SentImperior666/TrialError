---
artifact_id: PX-000            # registry-assigned, immutable; [SPEC-PASS] PE→PX: canonical prefix per research_artifacts.md §2
title: "<short title>"          # used verbatim in the registry line
purpose: "<why the research PROGRAM needs this artifact — plain language, self-contained>"
# C-0014 REQUIRED purpose standard: 1-3 sentences, plain language, SELF-CONTAINED research
# rationale — the question/need in the research PROGRAM this artifact serves and why that
# matters, understandable in isolation (readable WITHOUT following depends_on edges). It
# states the RESEARCH RATIONALE, NEVER merely the DAG position or an "X of Y" cross-reference
# (e.g. "critique of TD-002", "critique B of PB-003 v1"). The author writes it at creation; a
# positional-only purpose is REJECTED at registration (registry_lint purpose rule, C-0014(2)).
supersedes: null               # prior version this replaces (artifact_id or null)
type: paper-export             # fixed for this template
version: v1                    # bumped on revision; superseded versions kept
stage: keystone                # planning|in-progress|results|reflection|keystone
threads: [T?]                  # program threads touched
hypotheses: []                 # every hypothesis the exported claims rest on
depends_on: []                 # the registered artifacts this export derives from
gate: ungated                  # ungated|passed|passed-with-edits|failed
grounding: compliant           # compliant|partial|legacy (curriculum_operations §2)
---
<!-- HUMAN-TUNED, VENUE-FORMATTED. Derivation of EXISTING registered artifacts for external publication (the 13 registered opportunities). Same no-new-claims rule as white-paper: if the venue version needs a new claim, write the intermediate artifact first. Authorship/disclosure decided by the user at export time — do not fill without that call. -->

## Abstract
<!-- Human-tuned abstract per the target venue's conventions. -->

## Venue + opportunity
<!-- Target venue, format constraints, and which of the 13 registered publication opportunities this is. -->

## Source artifacts
<!-- The registered artifacts each section derives from — every claim traceable; NO new claims introduced at export. -->

## Body (venue format)
<!-- The venue-formatted paper body; structure follows the venue, not the internal template. -->

## Authorship + disclosure
<!-- Per the user's call at export time; leave blank until made. -->

## Registry event
<!-- [SPEC-PASS] PE→PX fixed (canonical prefix per research_artifacts.md §2). Outbox/projector
discipline (OPS_IMPROVEMENTS Cluster 3): authors NEVER append to research/ledgers/ARTIFACTS.md
directly. Emit ONE artifact_registered event to your own outbox (events/<WKP-id>/<agent>.jsonl);
the single PROJECTOR folds it into the registry. An artifact not in the registry does not exist
for citation — this event is what puts it there. -->
`{"type":"artifact_registered","ts":"<ISO-8601>","agent":"<agent-id>","artifact_id":"PX-000","artifact_type":"paper-export","title":"<title>","purpose":"<self-contained research rationale, C-0014 standard>","threads":["T?"],"gate":"ungated","supersedes":null,"path":"research/artifacts/PX-000_<slug>.md","corrections_pin":"v<N>@<YYYY-MM-DD>","attempt":1,"step":"S?"}`
<!-- Projector output (canonical registry-line format, owned by the ARTIFACTS.md header):
`- PX-000 | paper-export | <title> | T? | gate: ungated | supersedes: - | research/artifacts/PX-000_<slug>.md` -->
