# PURPOSE — /lit-review

**Problem this skill exists for.** The three ways a literature answer goes
wrong are answering from general knowledge instead of the corpus, citing an
anchor that is "close enough" to the claim, and reconstructing a fenced
passage from repeated partial reads. The loop — search, gather evidence beyond
the top hit, draft with an inline marker after every claim sentence, re-gather
on any gap instead of softening the claim, quote-check byte-exact before
finalizing — exists so that every sentence in the answer can be bound back to
an anchor by the verification tools that run after it.

**Mined patterns that motivated it.** The loop is paper-qa-shaped: its
three-tool agentic search / gather-evidence / answer cycle and its
"re-search after new evidence" discipline
(`docs/mining/S1-scilit-1__paper-qa.md`; design Section 5.3 says so by name),
including the keyword-prefilter-then-rerank retrieval it sits on. The
deterministic claim-support pre-pass that the citecheck hand-off relies on
converges on claude-deep-research-skill's token-Jaccard scoring
(`docs/mining/G23-search__claude-deep-research-skill.md`, reference only — no
license) and hyperresearch's persistent research vault with citation
verification (`docs/mining/G23-search__hyperresearch.md`). From the 2026-09
round: the bounded-but-locally-complete plan block for a review that is larger
than one question is Harness-of-Harness §3.4.1 / Appendix A.2
(`docs/mining/G25-operator-2026-09__harness-of-harness.md`, F4;
arXiv:2609.01481); its claim-evidence record with a strict verified/gap
partition (F2) is the adopt-later shape for the evidence table.

**Evolution.**
- Created with the plugin (design Sections 5.1, 5.3, 7).
- 2026-09-05 (launch `LNCH-01M1R8J1H9QN0RZVCE1MKR6W6G`): added this sidecar and
  a "When NOT to apply" section (wikiskill-F4); the review-plan block (HoH-F4).

*Mining reports live under `docs/mining/` in the harness repository and are
not part of the public export; primary sources are named inline.*
