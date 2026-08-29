# TrialError

TrialError is a research-operations harness for Claude Code. Iteration is the
method: a long-running research system that churns through tasks in rounds,
built for one researcher running long, multi-session AI-agent research
programs: literature review, hypothesis testing, and structured write-ups
over weeks or months, not a single afternoon chat.

Plain Claude Code gives you an agent loop. It does not give you a spend
ledger that actually blocks an unbooked subagent spawn, a corpus where every
quoted sentence resolves to a real page and license tier, a critique-gate
workflow for artifacts, or a local dashboard to see what your agents have
been doing while you were away. TrialError adds that layer on top of Claude Code
without becoming a separate agent runtime itself: it is a CLI, two MCP
servers, and a set of hooks that Claude Code drives.

It targets a single operator working locally on Windows, with SQLite as the
only storage engine and no required external services.

## Who it is for

TrialError is for a researcher who already uses Claude Code (or a similar
coding-agent CLI) to run research work, and who has been burned by at least
one of: an agent spawning more subagents than the budget allowed, a citation
that turned out not to say what the summary claimed, or a week of work with
no record of what actually happened. It is not a chatbot product, a hosted
service, or a general-purpose RAG framework. If your research program is a
single question answered in one sitting, a plain deep-research tool will
serve you better and cost less setup time.

## What it does

- Budget-gated agent spawning: a `PreToolUse:Task` hook refuses an unbooked
  subagent spawn with exit code 2, not a warning. The booking is consumed
  atomically on spawn and a replayed token is refused.
- A quote-anchored corpus: every ingested source is chunked, embedded, and
  indexed with a citation anchor. A source's license tier is enforced at
  the retrieval layer, not just recorded as metadata: a
  `commercial_restricted` source returns no verbatim run over 20 words.
- Critique gates: typed artifacts move through a state machine (submit,
  review, verdict) with edit-union verification, so a fix in response to a
  reviewer's finding is checked, not just claimed.
- Deliberation rooms: moderated multi-agent convergence in an append-only
  room document, with a fixed agreement bar and a freeze-and-escalate path
  when agents do not converge.
- A local, read-only dashboard: spend, jobs, gates, and corpus state,
  served over Server-Sent Events on localhost, no external hosting.
- Local semantic search over a public arXiv embeddings dataset (about 3.6
  million papers, confirmed against the real download), queryable from
  your own machine once you build the index.

## How it compares

These are the differences that hold up against the alternatives a
researcher would actually consider, checked against this codebase on the
TrialError side and against each project's own documented architecture on the
other side.

| | TrialError | LangChain / LlamaIndex | AutoGen / CrewAI | paper-qa | gpt-researcher | Plain Claude Code |
|---|---|---|---|---|---|---|
| Subagent spend enforcement | A hook refuses an unbooked spawn (exit 2), verified live in this build's own acceptance run | No built-in spawn gate; cost callbacks log spend after the fact | Agent/turn counts are configurable, not enforced against a ledger | No multi-agent spawning to gate | Sub-queries run without a budget gate | Task spawns are ungated by default |
| Citation grounding | Every result resolves to an anchor; a restricted source is capped at a 20-word verbatim quote, server-side | Chunks carry metadata; no license-tier fence on quote length | No retrieval layer of its own | Cites source and page, no license-tier fence | Cites live web URLs; no fixed corpus | No built-in citation system |
| Storage | SQLite (WAL) only, four local stores, no external services | Usually needs a separate vector database | No built-in persistence layer | In-memory or pickle-based index | No persistent corpus; each run is largely disposable | No data layer |
| Operator visibility | A local read-only dashboard over spend, jobs, gates, and corpus | Observability is a separate hosted product | Console and log output | None | A web UI for running reports, not for spend or gate oversight | None |
| Runtime model | Not a runtime: a CLI plus two MCP servers and hooks that Claude Code drives | A framework you import and run inside your own agent loop | Owns its own multi-agent loop | A library with its own agentic loop | An agent with its own orchestration loop | The exoskeleton TrialError attaches to |
| Deliberation protocol | A rooms runtime: fixed agreement bar, freeze-and-escalate on non-convergence | None built in | Multi-agent conversation exists; no enforced agreement threshold | Single-perspective synthesis | Single-perspective report | None |

## What it is not

TrialError does not run its own language-model calls, and it ships no general
web crawler. Web research is the agents' job: Claude Code agents carry
their own web search, and a fetched page enters the corpus as markdown
through the normal ingest path, anchors and license tier included. What
TrialError builds in is the scholarly layer: OpenAlex, Semantic Scholar, arXiv,
and Unpaywall clients, plus a local semantic index over the full arXiv
corpus. It does not manage API keys for you beyond reading a path you
configure. If you want a framework that owns the whole agent loop for you,
use AutoGen, CrewAI, or a LangChain agent instead.

## Quick start

These commands were run against a fresh scaffold to confirm they work as
written. Windows PowerShell, from the repo root:

```powershell
pip install -e .
trialerror program init demo --dir C:\research\demo-program
cd C:\research\demo-program

trialerror session boot --create-account "my-account"
# copy result.session_id from the output, then:
trialerror budget book --session-id <SESS-id> --program-id demo --agent-kind orchestrator --model-class top --model claude-opus --purpose "first run" --est-tokens 5000
# copy result.launch_id, then ingest a document:
trialerror ingest add-source --kind web --title "example" --license-tier open --acquisition-route web --launch-id <launch_id>
trialerror ingest add --source-id <source_id> --path raw\your-file.md --launch-id <launch_id>
trialerror jobs start-worker --mode loop --foreground

trialerror query search "your search terms here"
trialerror dashboard serve
```

Each command prints a JSON envelope with the field you need for the next
step (`result.session_id`, `result.launch_id`, and so on), plus a
`nextActions` list naming the exact next command when one is expected.

For the full walkthrough, including citation-checking a draft and closing a
session with a rendered handoff, read `docs/GETTING_STARTED.md`. For
account setup, local model configuration, and the optional literature-API
integrations, read `docs/USER_SETUP.md`. For the complete command
reference and the enforcement model in full, read `docs/OPERATOR_GUIDE.md`.

## Acknowledgments

TrialError leans on a handful of other open-source projects, either as vendored
code or as a design pattern read and re-implemented. One line each, credit
where a specific piece of this codebase actually traces back to it:

- **paper-qa** (Apache-2.0) — vendored the 11-label contradiction-judgment
  taxonomy and its forced-XML classification prompt.
- **book-to-skill** (MIT) — vendored the ingestion sanitizer that defends
  against Trojan-Source and invisible-codepoint injection.
- **MegaMemory** (MIT) — vendored (ported TypeScript to Python) the two-way,
  conflict-surfacing memory-merge classification algorithm.
- **sift-kg** (MIT) — the never-silent-auto-merge review queue: extraction
  candidates land as PENDING rows, never straight into the knowledge graph.
- **paper-search-mcp** (MIT) — the content-type-header + `%PDF`-magic-bytes +
  extension triple-check before a downloaded file is trusted as a real PDF.
- **Graphiti** (Apache-2.0, the open-source engine behind Zep) — the
  bi-temporal, 4-timestamp edge schema.
- **Unstructured** (Apache-2.0) — the two-pass boundary-aware chunking
  algorithm and the element-type taxonomy documents get normalized onto.
- **pdf-brain** (MIT) — the AgentEnvelope output shape, ported close to
  verbatim, including its HATEOAS-style `nextActions` array.
- **Ragas** (Apache-2.0) — the statement-decomposition-then-NLI-verdict
  pattern behind faithfulness scoring.
- **DeepEval** (Apache-2.0) — the pytest-native gate-acceptance-suite
  pattern (the pattern only — no dependency on the library itself).
- **langfuse** (MIT, core) — idle-gap time compression, now how the
  dashboard's Console timeline stays readable across long waits.
- **sigma.js** (MIT) — grid-cell label decimation and barycentre cluster
  labels, now the dashboard's Atlas graph view.
- **k9s** (Apache-2.0) — the two-layer status colorer and per-cell delta
  indicators, now the dashboard's Console jobs table.
- **btop** (Apache-2.0) — the 101-step, three-stop gradient behind the
  dashboard's meter rule.
- **arxiv-sanity-lite** (MIT) — the search-first page shell and explicit
  RANK BY control.
- **taste-skill** and **ui-ux-pro-max-skill** — design-process skills (not
  shipped code) that shaped the dashboard's visual system: palette,
  contrast floors, and type scale.
- **tomtum/openai-arxiv-embeddings** (MIT, Kaggle dataset) — the
  precomputed embeddings behind the local, 3.57-million-paper all-arXiv
  semantic index.

Licenses are respected as stated above; where this list says a project was
"ported" or "vendored" the code was adopted with attribution (see
`vendored/VENDORED.md`), everywhere else a design or algorithm was read and
reimplemented rather than copied.

## License

MIT. See `LICENSE`.
