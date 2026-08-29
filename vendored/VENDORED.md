# Vendored code manifest

TrialError adopts select third-party code verbatim under `vendored/<item>/` (one
directory per adopted item — see design docs/DESIGN_v0.md Section 3.1 and
Appendix A/D4-D6, Section 13 flag F4). Every file inside an item's directory
MUST carry a header block (convention below) recording where it came from,
at what revision, and under what license — re-verified for that SPECIFIC
file/module, never inherited from the upstream repo's top-level LICENSE
alone (STACK_DECISIONS standing rule: module-level license traps like
Langfuse's restrictively-licensed `ee/` subtree or cognee's Postgres-graph
extras living inside an otherwise-permissive repo).

`trialerror doctor --license-audit` (framework shipped in M0, `trialerror/util/checks.py`)
walks every file under every `vendored/<item>/` directory and FAILS the
check if:
- any file is missing the header block below, or
- this manifest is missing while at least one item directory exists.

## Header convention

Every vendored file's first ~40 lines must contain five `key: value` lines —
`upstream`, `commit`, `license`, `verified-by`, `date` — in ANY comment
style. The audit strips leading comment punctuation (`#`, `//`, `/*`, `<!--`,
`-->`, ...) before matching keys, so the same five lines work verbatim
whether the file is Python, TypeScript, or Markdown:

```
upstream: <source repo/page URL the file was taken from>
commit: <commit sha or version tag the file was taken from>
license: <SPDX identifier or license name>
verified-by: <person/agent who re-verified THIS file's license at vendor time>
date: <ISO-8601 date the file was vendored / last re-verified>
```

Example (Python file):

```python
# upstream: https://github.com/example/project
# commit: 1a2b3c4
# license: MIT
# verified-by: build-M0
# date: 2026-08-29
```

## Manifest

| Item | Upstream | Commit | License | Verified by | Date | Local path |
|---|---|---|---|---|---|---|
| MegaMemory 2-way merge (classification algorithm, ported TS->Python) | https://github.com/0xK3vin/MegaMemory | e0bb3c270d7fb4f6f280ae4685e0c538eb225d93 | MIT | build-M11 | 2026-08-29 | `vendored/MegaMemory/merge_port.py` |
| book-to-skill sanitizer (injection-defense: Trojan-Source/invisible codepoints) | https://github.com/virgiliojr94/book-to-skill | 9c207f870adebe20ade4f7d2f11bc3d759c2fd88 | MIT | build-M7 | 2026-08-29 | `vendored/book-to-skill-sanitizer/sanitize.py` |
| paper-qa contracrow verification prompt (11-point ordinal label taxonomy + forced-XML classification prompt, `prompts.qa` only) | https://github.com/Future-House/paper-qa | 57e89f7223b0960d5ee5ea048c69e3c47e088572 | Apache-2.0 | build-M9 | 2026-08-29 | `vendored/paper-qa/contracrow.py` |

M0 ships the framework and this (empty) manifest only.

**TRIALERROR-DEV-NOTE (M9, JSON->Python re-encoding, not a byte-for-byte file copy):** upstream's `contracrow.json` is JSON, which cannot carry the five `key: value` header lines this manifest's own convention requires (JSON has no comment syntax) — so `vendored/paper-qa/contracrow.py` re-encodes the two pieces design Section 8.2 actually adopts (the `prompts.qa` string and its embedded 11-label vocabulary) as Python string/tuple constants, copied character-for-character out of the upstream JSON value, inside a file that CAN carry the header. Upstream's other fields (LLM model names, agent tool-loop prompt, summary/citation prompts, batch/temperature config) belong to paper-qa's own agent harness and are not adopted — see the file's own module docstring for the full scoping rationale. Loaded via `importlib.util.spec_from_file_location` with `sys.dont_write_bytecode` held during the import (`trialerror/verify/labels.py`), the same pattern `trialerror/ingest/sanitizer.py` and `trialerror/memory/merge.py` already use for a hyphenated vendored directory name / to dodge the pre-existing `__pycache__`-one-level-in license-audit gap those two files already flag (out of this build's lane — `trialerror/util/checks.py` is M0's).

**TRIALERROR-DEV-NOTE (M11, TS->Python port, not a byte-for-byte copy):** the
source language (TypeScript) cannot be vendored verbatim into a Python
package and executed, so `vendored/MegaMemory/merge_port.py` is a close,
attributed PORT of upstream `src/merge.ts`'s per-id classification pass
(the `left_only`/`right_only`/`identical`/`conflict` decision — see the
file's own module docstring for exactly what was and was not carried
over: the node/edge knowledge-graph machinery was dropped as inapplicable
to `memory_item`, a flat non-graph table). This is the "adapt actual code
... follow the vendoring discipline" path named in the M11 build brief,
not the "reimplement thin from the report's description" path — the
control-flow and naming (`::left`/`::right` suffixes, per-id classify
loop, group-id minting on divergence) are a direct line-by-line
translation, checked against the upstream source at vendor time, not an
independent reimplementation from the mining report alone.
