# PURPOSE — /verify-hypothesis

**Problem this skill exists for.** A hypothesis "verified" by the agent that
proposed it, against the nearest neighbours of its own wording, with the
procedure chosen after the results were seen, is not verified. This skill is
the only sanctioned path to a `hypothesis` verdict row: pre-register the
procedure and parameters blind (escrowed outside the program tree), retrieve
stratified so far-arm evidence is forced in, classify every retrieved chunk
with a contradiction-detection judgment supplied per chunk, aggregate the label
distribution, and write a typed verdict artifact whose `prereg_compliant` flag
is stamped from the link, never asserted.

**Mined patterns that motivated it.** The per-chunk contradiction judgment is
paper-qa's `contracrow` prompt (11-point ordinal scale, forced structured
output), adopted near drop-in (`docs/mining/S1-scilit-1__paper-qa.md`). The
mechanical-first, deterministically-sampled LLM-escalation shape shared with
citecheck is hyperresearch's (`docs/mining/G23-search__hyperresearch.md`).
Stratified, breadth-forcing retrieval is the same machinery `/ideation-round`
uses, with Lacuna Deep Research's independent-worker evidence gathering as the
reference (`docs/mining/G17-papers__arxiv-2606.26246-research-structured-knowledge.md`).
From the 2026-09 round: Harness-of-Harness's budget-controlled ablation
(a matched-compute "vanilla continuation" control) is the standing method for
the next bake-off of this pipeline's own settings
(`docs/mining/G25-operator-2026-09__harness-of-harness.md`, F5;
arXiv:2609.01481), and "AI Finds A Way" is why the verdict is a discrete label
rather than a score anything could optimise against
(`docs/mining/G25-operator-2026-09__ai-finds-a-way.md`, F3, rejected as a
build item because the design already does this).

**Evolution.**
- Created with the plugin (design Sections 5.3, 8.2; M9).
- 2026-09-05 (launch `LNCH-01M1R8J1H9QN0RZVCE1MKR6W6G`): added this sidecar and
  a "When NOT to apply" section (wikiskill-F4).

*Mining reports live under `docs/mining/` in the harness repository and are
not part of the public export; primary sources are named inline.*
