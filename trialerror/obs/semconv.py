"""Pinned OTel GenAI semantic-conventions vocabulary. Design Section 4.5:
"Adopt the OTel GenAI semantic conventions vocabulary wholesale, spec
version **pinned** (v1.44.0 attribute set; re-check at each minor release
-- the spec is Status: Development)."

Every attribute-name string below was copied verbatim from
``opentelemetry.semconv._incubating.attributes.gen_ai_attributes`` /
``GenAiOperationNameValues`` in ``opentelemetry-semantic-conventions==0.65b0``
(the release ``pip`` resolves alongside ``opentelemetry-sdk==1.44.0`` --
i.e. exactly the pin this module names) -- NOT imported from that package.
That is deliberate: this module has zero third-party imports, so
``trialerror.obs.spans``' attribute-shape contract (which attributes a given
span kind carries, and their names) stays importable, type-checkable, and
unit-testable with no OTel/Phoenix deps installed at all -- the "obs seed"
degrades to a no-op emitter (see ``trialerror.obs.tracer``), never to an
ImportError. A value here is a promise about what gets emitted WHEN
emission is live, not a live import from the incubating package (whose own
module path starts with an underscore for a reason -- it is explicitly
unstable upstream).

Design Section 4.5's mapping table (TrialError object -> OTel span -> key
attributes), reproduced as the constants below:

| TrialError object                  | OTel span (``gen_ai.operation.name``) | Key attributes |
|---|---|---|
| launch (booked->reconciled)   | ``invoke_agent``  | agent.name=agent_kind, request.model, usage.{input,output}_tokens, trialerror.launch_id, trialerror.parent_launch |
| retrieval call                | ``retrieval``     | query hash, tiers used, k, result chunk_ids (bounded), trialerror.program |
| verification step             | ``execute_tool``  | procedure, subject_id, verdict label |
| job lifecycle                 | (one span per attempt) | trialerror.job_id, kind, failure_class, checkpoint progress |
"""

from __future__ import annotations

__all__ = [
    "SPEC_VERSION_PIN",
    "GEN_AI_OPERATION_NAME",
    "GEN_AI_AGENT_NAME",
    "GEN_AI_AGENT_ID",
    "GEN_AI_REQUEST_MODEL",
    "GEN_AI_RESPONSE_MODEL",
    "GEN_AI_USAGE_INPUT_TOKENS",
    "GEN_AI_USAGE_OUTPUT_TOKENS",
    "GEN_AI_RETRIEVAL_QUERY_TEXT",
    "OP_INVOKE_AGENT",
    "OP_RETRIEVAL",
    "OP_EXECUTE_TOOL",
    "TRIALERROR_LAUNCH_ID",
    "TRIALERROR_PARENT_LAUNCH",
    "TRIALERROR_PROGRAM",
    "TRIALERROR_JOB_ID",
    "TRIALERROR_JOB_KIND",
    "TRIALERROR_JOB_ATTEMPT",
    "TRIALERROR_FAILURE_CLASS",
    "TRIALERROR_CHECKPOINT_PROGRESS",
    "TRIALERROR_PROCEDURE",
    "TRIALERROR_SUBJECT_ID",
    "TRIALERROR_VERDICT",
    "TRIALERROR_QUERY_HASH",
    "TRIALERROR_TIERS",
    "TRIALERROR_K",
    "TRIALERROR_RESULT_CHUNK_IDS",
]

#: design Section 4.5's literal pin. Cross-checked live at M12 build time:
#: ``opentelemetry-sdk==1.44.0`` (the current PyPI release) pulls in
#: ``opentelemetry-semantic-conventions==0.65b0``, whose incubating
#: ``gen_ai_attributes`` module defines exactly the attribute names below --
#: no drift between the design's stated pin and what today's resolver hands
#: back. Re-check this at each minor ``opentelemetry-sdk`` release, per the
#: design comment above.
SPEC_VERSION_PIN = "1.44.0"

# ---- gen_ai.* attribute keys (OTel GenAI semantic conventions) -----------
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_AGENT_NAME = "gen_ai.agent.name"
GEN_AI_AGENT_ID = "gen_ai.agent.id"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_RETRIEVAL_QUERY_TEXT = "gen_ai.retrieval.query.text"

# ---- gen_ai.operation.name values (GenAiOperationNameValues, same pin) ---
OP_INVOKE_AGENT = "invoke_agent"
OP_RETRIEVAL = "retrieval"
OP_EXECUTE_TOOL = "execute_tool"

# ---- trialerror.*-namespaced attributes (the table's non-GenAI columns) -------
TRIALERROR_LAUNCH_ID = "trialerror.launch_id"
TRIALERROR_PARENT_LAUNCH = "trialerror.parent_launch"
TRIALERROR_PROGRAM = "trialerror.program"
TRIALERROR_JOB_ID = "trialerror.job_id"
TRIALERROR_JOB_KIND = "trialerror.job_kind"
TRIALERROR_JOB_ATTEMPT = "trialerror.job_attempt"
TRIALERROR_FAILURE_CLASS = "trialerror.failure_class"
TRIALERROR_CHECKPOINT_PROGRESS = "trialerror.checkpoint_progress"
TRIALERROR_PROCEDURE = "trialerror.procedure"
TRIALERROR_SUBJECT_ID = "trialerror.subject_id"
TRIALERROR_VERDICT = "trialerror.verdict"
TRIALERROR_QUERY_HASH = "trialerror.query_hash"
TRIALERROR_TIERS = "trialerror.tiers"
TRIALERROR_K = "trialerror.k"
TRIALERROR_RESULT_CHUNK_IDS = "trialerror.result_chunk_ids"
