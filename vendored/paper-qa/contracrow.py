# upstream: https://github.com/Future-House/paper-qa
# commit: 57e89f7223b0960d5ee5ea048c69e3c47e088572
# license: Apache-2.0
# verified-by: build-M9
# date: 2026-08-29
#
# Vendored from src/paperqa/configs/contracrow.json (Apache License 2.0,
# Copyright Future House — see LICENSE below). Design docs/DESIGN_v0.md
# Section 8.2 ("Classify: each evidence chunk scored against the hypothesis
# with the paper-qa contracrow prompt (vendored, Apache-2.0): 11-point
# ordinal scale from `explicit contradiction` ... `lack of evidence` ...
# `explicit agreement`, forced-XML response, every sentence citing its
# anchor") and Section 13 flag F4 (vendoring discipline: header +
# VENDORED.md row, re-verified for this SPECIFIC file).
#
# NOT the whole contracrow.json: only the two pieces design Section 8.2
# actually adopts are carried over — the "qa" prompt template (label
# taxonomy + forced-XML response shape) and its 11 labels, extracted
# verbatim from the upstream JSON's "prompts.qa" string and reformatted as
# Python constants (JSON cannot carry the vendoring header's `key: value`
# comment lines trialerror.util.checks.check_license_audit scans for, so this is
# a same-content re-encoding, not a port — every word of CONTRACROW_QA_PROMPT
# below is copied character-for-character out of the upstream string, only
# the surrounding container changed from JSON to a Python triple-quoted
# string). Upstream's unrelated fields (LLM model names, agent tool-loop
# prompt, summary/citation prompts, temperature, batch_size, ...) belong to
# paper-qa's own agent harness, which TrialError does not adopt — design's own
# scoping: "LLM-judgment steps ... are executed by AGENTS at runtime, not
# by this module" (M9 build brief), so only the CLASSIFICATION prompt and
# its label vocabulary are in scope.
#
# LICENSE (Apache-2.0, upstream vendor_mining/paper-qa/LICENSE — summary;
# full text at the upstream URL above):
#   Licensed under the Apache License, Version 2.0 (the "License"); you may
#   not use this file except in compliance with the License. You may obtain
#   a copy of the License at http://www.apache.org/licenses/LICENSE-2.0.
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
"""The contracrow label taxonomy + classification prompt, vendored from
paper-qa. :mod:`trialerror.verify.hypothesis` builds one LLM-judgment envelope
per evidence chunk using :data:`CONTRACROW_QA_PROMPT`; the caller-supplied
judge (a real subagent at runtime, a deterministic fake in tests — the M9
build brief: "your pipeline SHAPES the work ... and exposes a 'judgment
request' envelope an agent (or a fake judge in tests) fills") must return a
label from :data:`CONTRACROW_LABELS`.
"""

from __future__ import annotations

__all__ = ["CONTRACROW_LABELS", "CONTRACROW_QA_PROMPT", "label_index", "label_polarity"]

#: The 11-point ordinal scale, contradiction end to agreement end — the
#: EXACT label strings and order from upstream's "qa" prompt (design
#: Section 8.2: "11-point ordinal scale from `explicit contradiction` ...
#: `lack of evidence` ... `explicit agreement`"). Index 0..4 = contradiction
#: side, 5 = no evidence, 6..10 = agreement side (see :func:`label_polarity`).
CONTRACROW_LABELS: tuple[str, ...] = (
    "explicit contradiction",
    "strong contradiction",
    "contradiction",
    "nuanced contradiction",
    "possibly a contradiction",
    "lack of evidence",
    "possibly an agreement",
    "nuanced agreement",
    "agreement",
    "strong agreement",
    "explicit agreement",
)

#: Verbatim from upstream ``prompts.qa`` (only ``{context}``/``{question}``/
#: ``{answer_length}`` placeholders retained — upstream's own placeholder
#: names, unchanged so the prompt text stays byte-identical to source).
CONTRACROW_QA_PROMPT = """Determine if the claim below is contradicted by the context below


{context}

----

Claim: {question}


Determine if the claim is contradicted by the context. For each part of your response, indicate which sources most support it via citation keys at the end of sentences, like (pqac-1234abcd). Only cite from the context below and only use the valid keys.

Respond with the following XML format:

<response>
  <reasoning>...</reasoning>
  <label>...</label>
</response>


where `reasoning` is your reasoning ({answer_length}) about if the claim is being contradicted. `label` is one of the following (must match exactly):

explicit contradiction
strong contradiction
contradiction
nuanced contradiction
possibly a contradiction
lack of evidence
possibly an agreement
nuanced agreement
agreement
strong agreement
explicit agreement

Don't worry about other contradictions or agreements in the context, only focus on the specific claim. If there is no evidence for the claim, you should choose lack of evidence."""


def label_index(label: str) -> int:
    """0-based position of ``label`` in :data:`CONTRACROW_LABELS`. Raises
    :class:`ValueError` (via ``tuple.index``) for anything outside the
    fixed 11-word vocabulary — callers use this to fail loudly on a judge
    that returns free text instead of the forced label."""
    return CONTRACROW_LABELS.index(label)


def label_polarity(label: str) -> int:
    """``-1`` for any contradiction-side label, ``0`` for ``"lack of
    evidence"``, ``+1`` for any agreement-side label — the coarse signal
    :mod:`trialerror.verify.hypothesis` aggregates over a whole evidence set to
    propose a hypothesis status (design Section 8.2 step 4: "label
    distribution ... hypothesis status proposal")."""
    idx = label_index(label)
    if idx < 5:
        return -1
    if idx == 5:
        return 0
    return 1
