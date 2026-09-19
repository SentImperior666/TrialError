"""``trialerror.obs.semconv``'s pin: literal attribute-name/operation-value
strings must match what ``opentelemetry-semantic-conventions``'s
incubating ``gen_ai_attributes`` module actually defines for the pinned
release -- this is the "no drift between the design's stated pin and
today's resolver" check the module's docstring claims. Skips (doesn't
fail) when the ``obs`` extra isn't installed: semconv.py itself has zero
third-party imports (see its own docstring for why), so this is the one
test that needs the real package, purely to CROSS-CHECK the pin -- absence
of the package is not itself a semconv bug."""

from __future__ import annotations

import pytest

from trialerror.obs import semconv

otel_semconv = pytest.importorskip("opentelemetry.semconv._incubating.attributes.gen_ai_attributes")


def test_gen_ai_attribute_names_match_the_installed_incubating_package():
    assert semconv.GEN_AI_OPERATION_NAME == otel_semconv.GEN_AI_OPERATION_NAME
    assert semconv.GEN_AI_AGENT_NAME == otel_semconv.GEN_AI_AGENT_NAME
    assert semconv.GEN_AI_AGENT_ID == otel_semconv.GEN_AI_AGENT_ID
    assert semconv.GEN_AI_REQUEST_MODEL == otel_semconv.GEN_AI_REQUEST_MODEL
    assert semconv.GEN_AI_RESPONSE_MODEL == otel_semconv.GEN_AI_RESPONSE_MODEL
    assert semconv.GEN_AI_USAGE_INPUT_TOKENS == otel_semconv.GEN_AI_USAGE_INPUT_TOKENS
    assert semconv.GEN_AI_USAGE_OUTPUT_TOKENS == otel_semconv.GEN_AI_USAGE_OUTPUT_TOKENS
    assert semconv.GEN_AI_RETRIEVAL_QUERY_TEXT == otel_semconv.GEN_AI_RETRIEVAL_QUERY_TEXT


def test_operation_name_values_match_the_installed_incubating_package():
    values = otel_semconv.GenAiOperationNameValues
    assert semconv.OP_INVOKE_AGENT == values.INVOKE_AGENT.value
    assert semconv.OP_RETRIEVAL == values.RETRIEVAL.value
    assert semconv.OP_EXECUTE_TOOL == values.EXECUTE_TOOL.value


def test_design_section_4_5_table_mapping_is_exactly_these_three_operations():
    """Design Section 4.5's table names exactly ``invoke_agent`` (launch),
    ``retrieval`` (retrieval call), ``execute_tool`` (verification step) --
    pinned here as a standalone (no-deps-needed) regression guard,
    independent of the cross-check above."""
    assert {semconv.OP_INVOKE_AGENT, semconv.OP_RETRIEVAL, semconv.OP_EXECUTE_TOOL} == {
        "invoke_agent",
        "retrieval",
        "execute_tool",
    }


def test_spec_version_pin_matches_design_section_4_5():
    assert semconv.SPEC_VERSION_PIN == "1.44.0"


def test_trialerror_namespaced_attributes_all_use_the_trialerror_prefix():
    trialerror_attrs = [v for k, v in vars(semconv).items() if k.startswith("TRIALERROR_")]
    assert trialerror_attrs, "expected at least one TRIALERROR_* constant"
    assert all(v.startswith("trialerror.") for v in trialerror_attrs)
