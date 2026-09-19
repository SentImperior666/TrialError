"""The extraction route into the lexicon: the envelope's optional ``term``
key and the accept-time hook that consumes it (design §4; rulings
L-E5/L-E7).

Two routes and one guarantee.

*The envelope route* -- a judgment that named the lemma lands a ``current``
sense, because the reviewer accepting the claim just accepted that naming
along with it. *The heuristic route* -- no lemma in the judgment, so a
substring match over the same chunk's entity candidates proposes at
``proposed`` and waits for someone. The guarantee is that neither route can
break an acceptance: the claim, its anchor and its record stamp are already
written when the hook runs, so every failure it can have comes back as a
stated reason instead of an exception.

Built on ``tests._retrieve_fixtures.build_small_corpus``, the same landed
fixture ``tests/test_ingest_extract.py`` uses, so every ``quote`` here is a
real verbatim substring of a real chunk.
"""

from __future__ import annotations

import json

import pytest

from trialerror.ingest import extract as extract_api
from trialerror.ingest.errors import ExtractError
from trialerror.lexicon import api as lexicon_api
from trialerror.lexicon import policy
from trialerror.stores.writer import get as store_get

from tests._retrieve_fixtures import build_small_corpus

_OPEN_SENTENCE_1 = "Distributed schedulers use retry budgets to bound tail latency during failover."
_OPEN_SENTENCE_2 = "A coordinator arbitrates lock conflicts and records the consequences of worker actions."


@pytest.fixture()
def corpus(store):
    return build_small_corpus(store)


def _chunk_id(corpus):
    return corpus["open_chunk_ids"][0]


def _judge(claims, entities=None):
    def judge(_envelope):
        return {"entities": entities or [], "relations": [], "claims": claims}

    return judge


def _extract(store, corpus, claims, entities=None):
    return extract_api.run_extract_chunk(
        store,
        _chunk_id(corpus),
        judge=_judge(claims, entities),
        created_by_launch=corpus["launch_id"],
    )


def _accept_all(store, corpus, result):
    out = []
    for record_id in result["record_ids"]["entities"] + result["record_ids"]["claims"]:
        out.append(extract_api.accept_candidate(store, record_id, by_launch=corpus["launch_id"]))
    return out


# ---------------------------------------------------------------------------
# the envelope
# ---------------------------------------------------------------------------


def test_the_envelope_asks_for_the_lemma_and_says_what_it_is_not(store, corpus):
    chunk = store_get(store, "chunk", pk_column="chunk_id", pk_value=_chunk_id(corpus))
    envelope = extract_api.build_extraction_judgment_envelope(chunk)
    instructions = envelope["instructions"]
    assert '"term"?' in instructions, "the response shape carries the optional key"
    assert "the lemma being defined" in instructions
    assert "never the sentence" in instructions, "a lemma is a name, not a definition"


def test_a_judgment_written_before_the_key_existed_still_validates(store, corpus):
    """The key is optional in both directions -- an older judgments file must
    not stop extracting."""
    result = _extract(
        store,
        corpus,
        [{"text": "Retry budgets bound tail latency.", "kind": "definition", "quote": _OPEN_SENTENCE_1}],
    )
    candidate = extract_api.get_candidate(store, result["record_ids"]["claims"][0])
    assert candidate["payload"]["term"] is None


def test_a_term_on_a_definition_is_carried_on_the_pending_row(store, corpus):
    """Carried, not acted on: a term proposed at queue time would be a term
    proposed for a candidate that may yet be rejected."""
    result = _extract(
        store,
        corpus,
        [
            {
                "text": "Retry budgets bound tail latency.",
                "kind": "definition",
                "quote": _OPEN_SENTENCE_1,
                "term": "  retry budget  ",
            }
        ],
    )
    candidate = extract_api.get_candidate(store, result["record_ids"]["claims"][0])
    assert candidate["payload"]["term"] == "retry budget"
    assert store.knowledge.execute("SELECT count(*) FROM term").fetchone()[0] == 0


def test_a_term_on_a_claim_that_is_not_a_definition_is_dropped(store, corpus):
    result = _extract(
        store,
        corpus,
        [
            {
                "text": "Retry budgets bound tail latency.",
                "kind": "mechanism",
                "quote": _OPEN_SENTENCE_1,
                "term": "retry budget",
            }
        ],
    )
    candidate = extract_api.get_candidate(store, result["record_ids"]["claims"][0])
    assert candidate["payload"]["term"] is None


@pytest.mark.parametrize("value", ["", "   ", 17, ["retry budget"]])
def test_a_malformed_term_is_refused_by_name(store, corpus, value):
    """A judge that emitted the key meant to name something; dropping it
    silently would leave an operator wondering why no term appeared."""
    with pytest.raises(ExtractError, match="non-empty lemma string"):
        _extract(
            store,
            corpus,
            [
                {
                    "text": "Retry budgets bound tail latency.",
                    "kind": "definition",
                    "quote": _OPEN_SENTENCE_1,
                    "term": value,
                }
            ],
        )


# ---------------------------------------------------------------------------
# the envelope route at accept time
# ---------------------------------------------------------------------------


def test_a_named_lemma_lands_a_current_sense_anchored_to_the_same_quote(store, corpus):
    result = _extract(
        store,
        corpus,
        [
            {
                "text": "Retry budgets bound tail latency.",
                "kind": "definition",
                "quote": _OPEN_SENTENCE_1,
                "term": "retry budget",
            }
        ],
    )
    accepted = extract_api.accept_candidate(
        store, result["record_ids"]["claims"][0], by_launch=corpus["launch_id"]
    )

    lexicon = accepted["lexicon"]
    assert lexicon["status"] == "ok"
    assert lexicon["route"] == "envelope"
    assert lexicon["sense_status"] == "current"

    sense = lexicon_api.get_sense(store, lexicon["sense_id"])
    assert sense["procedure_version"] == policy.EXTRACT_PROCEDURE_VERSION
    assert sense["origin_kind"] == "extract"
    assert sense["origin_ref"] == accepted["claim_id"]
    assert sense["gloss"] == "Retry budgets bound tail latency."

    evidence = lexicon_api.evidence_for_sense(store, lexicon["sense_id"])
    assert len(evidence) == 1
    assert evidence[0]["evidence_kind"] == "quote_anchor"
    assert evidence[0]["anchor_id"] == accepted["claim"]["anchor_id"]
    assert evidence[0]["excerpt"] == _OPEN_SENTENCE_1
    assert lexicon_api.find_term(store, "Retry Budget")["term_id"] == lexicon["term_id"]


def test_the_accept_records_the_sense_on_the_row_and_in_the_event(store, corpus):
    result = _extract(
        store,
        corpus,
        [
            {
                "text": "Retry budgets bound tail latency.",
                "kind": "definition",
                "quote": _OPEN_SENTENCE_1,
                "term": "retry budget",
            }
        ],
    )
    record_id = result["record_ids"]["claims"][0]
    accepted = extract_api.accept_candidate(store, record_id, by_launch=corpus["launch_id"])

    candidate = extract_api.get_candidate(store, record_id)
    assert candidate["payload"]["lexicon_sense_id"] == accepted["lexicon"]["sense_id"]
    event = [
        json.loads(r["payload"])
        for r in store.ops.execute("SELECT payload FROM event WHERE type = 'kg_candidate_accepted'").fetchall()
    ][-1]
    assert event["term_id"] == accepted["lexicon"]["term_id"]
    assert event["sense_id"] == accepted["lexicon"]["sense_id"]


def test_a_rejected_candidate_proposes_nothing(store, corpus):
    result = _extract(
        store,
        corpus,
        [
            {
                "text": "Retry budgets bound tail latency.",
                "kind": "definition",
                "quote": _OPEN_SENTENCE_1,
                "term": "retry budget",
            }
        ],
    )
    extract_api.reject_candidate(
        store, result["record_ids"]["claims"][0], by_launch=corpus["launch_id"], reason="not a definition"
    )
    assert store.knowledge.execute("SELECT count(*) FROM term").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# the heuristic route
# ---------------------------------------------------------------------------


def test_without_a_named_lemma_the_heuristic_proposes_and_waits(store, corpus):
    result = _extract(
        store,
        corpus,
        [{"text": "Retry budgets bound tail latency.", "kind": "definition", "quote": _OPEN_SENTENCE_1}],
        entities=[{"name": "Retry Budget", "entity_type": "mechanism"}],
    )
    _accept_all(store, corpus, result)

    term = lexicon_api.find_term(store, "Retry Budget")
    sense = lexicon_api.senses_for_term(store, term["term_id"])[0]
    assert sense["status"] == "proposed", "a substring match is a guess, so it waits for someone"
    assert sense["procedure_version"] == policy.EXTRACT_HEURISTIC_PROCEDURE_VERSION
    assert sense["decided_by_launch"] is None


def test_the_heuristic_prefers_the_longest_name_that_occurs(store, corpus):
    """"budget" and "retry budget" can both hit, and only one of them is the
    term this sentence defines."""
    result = _extract(
        store,
        corpus,
        [{"text": "Retry budgets bound tail latency.", "kind": "definition", "quote": _OPEN_SENTENCE_1}],
        entities=[
            {"name": "Budget", "entity_type": "mechanism"},
            {"name": "Retry Budget", "entity_type": "mechanism"},
        ],
    )
    accepted = _accept_all(store, corpus, result)
    assert accepted[-1]["lexicon"]["lemma"] == "Retry Budget"


def test_an_entity_that_does_not_occur_in_the_claim_is_not_the_lemma(store, corpus):
    result = _extract(
        store,
        corpus,
        [{"text": "Retry budgets bound tail latency.", "kind": "definition", "quote": _OPEN_SENTENCE_1}],
        entities=[{"name": "Coordinator", "entity_type": "role"}],
    )
    accepted = _accept_all(store, corpus, result)
    lexicon = accepted[-1]["lexicon"]
    assert lexicon["status"] == "skipped"
    assert "no entity candidate" in lexicon["reason"]
    assert store.knowledge.execute("SELECT count(*) FROM term").fetchone()[0] == 0


def test_a_claim_that_is_not_a_definition_feeds_the_lexicon_nothing(store, corpus):
    result = _extract(
        store,
        corpus,
        [{"text": "Retry budgets bound tail latency.", "kind": "mechanism", "quote": _OPEN_SENTENCE_1}],
        entities=[{"name": "Retry Budget", "entity_type": "mechanism"}],
    )
    accepted = _accept_all(store, corpus, result)
    assert accepted[-1]["lexicon"]["status"] == "skipped"
    assert "definition claims only" in accepted[-1]["lexicon"]["reason"]


# ---------------------------------------------------------------------------
# the guarantee: the hook cannot break an acceptance
# ---------------------------------------------------------------------------


def test_a_gloss_the_lexicon_refuses_does_not_fail_the_accept(store, corpus):
    """The claim and its anchor are already written when the hook runs. A
    refusal there is a reported outcome, never a rolled-back acceptance a
    reviewer already made."""
    long_text = " ".join(f"word{i}" for i in range(policy.GLOSS_MAX_WORDS + 5))
    result = _extract(
        store,
        corpus,
        [{"text": long_text, "kind": "definition", "quote": _OPEN_SENTENCE_1, "term": "retry budget"}],
    )
    accepted = extract_api.accept_candidate(
        store, result["record_ids"]["claims"][0], by_launch=corpus["launch_id"]
    )

    assert accepted["claim_id"], "the claim landed"
    assert store_get(store, "claim", pk_column="claim_id", pk_value=accepted["claim_id"]) is not None
    assert accepted["lexicon"]["status"] == "refused"
    assert "cap is" in accepted["lexicon"]["reason"]
    assert store.knowledge.execute("SELECT count(*) FROM term").fetchone()[0] == 0


def test_a_store_error_from_the_lexicon_does_not_fail_the_accept_either(store, corpus, monkeypatch):
    """Finding F3, the hook's half. The docstring promises "never raises",
    but the arms caught ``LexiconError`` and ``sqlite3.OperationalError``
    only -- and a UNIQUE violation arrives as ``ValidationError`` while a
    launch naming no row arrives as ``XidTargetMissingError``, neither of
    which is a subclass of either. A concurrent accept would have failed an
    acceptance a human had already made."""
    from trialerror.lexicon import api as lexicon_api_module
    from trialerror.stores.errors import ValidationError

    def _raced(store_, **kwargs):
        raise ValidationError(
            "term: integrity violation on insert: UNIQUE constraint failed: term.lemma_norm"
        )

    monkeypatch.setattr(lexicon_api_module, "propose", _raced)

    result = _extract(
        store,
        corpus,
        [{"text": "a retry budget is a cap on retries", "kind": "definition",
          "quote": _OPEN_SENTENCE_1, "term": "retry budget"}],
    )
    accepted = extract_api.accept_candidate(
        store, result["record_ids"]["claims"][0], by_launch=corpus["launch_id"]
    )

    assert accepted["claim_id"], "the acceptance stood"
    assert store_get(store, "claim", pk_column="claim_id", pk_value=accepted["claim_id"]) is not None
    assert accepted["lexicon"]["status"] == "refused"
    assert "ValidationError" in accepted["lexicon"]["reason"]


def test_a_launch_the_lexicon_cannot_resolve_is_reported_not_raised(store, corpus, monkeypatch):
    from trialerror.lexicon import api as lexicon_api_module
    from trialerror.stores.errors import XidTargetMissingError

    def _missing(store_, **kwargs):
        raise XidTargetMissingError(
            "event.launch_id = 'LNCH-gone' has no matching row in platform.launch.launch_id (XID refused)"
        )

    monkeypatch.setattr(lexicon_api_module, "propose", _missing)
    result = _extract(
        store,
        corpus,
        [{"text": "a retry budget is a cap on retries", "kind": "definition",
          "quote": _OPEN_SENTENCE_1, "term": "retry budget"}],
    )
    accepted = extract_api.accept_candidate(
        store, result["record_ids"]["claims"][0], by_launch=corpus["launch_id"]
    )
    assert accepted["lexicon"]["status"] == "refused"
    assert "XidTargetMissingError" in accepted["lexicon"]["reason"]


def test_two_definitions_from_one_source_are_two_readings_and_no_conflict(store, corpus):
    """Polysemy is not a defect and a shared source is not a disagreement --
    two readings out of one document are nuance, and the store says so by
    opening nothing."""
    result = _extract(
        store,
        corpus,
        [
            {"text": "Retry budgets bound tail latency.", "kind": "definition",
             "quote": _OPEN_SENTENCE_1, "term": "retry budget"},
            {"text": "A retry budget is what a coordinator arbitrates against.", "kind": "definition",
             "quote": _OPEN_SENTENCE_2, "term": "retry budget"},
        ],
    )
    _accept_all(store, corpus, result)

    term = lexicon_api.find_term(store, "retry budget")
    assert len(lexicon_api.senses_for_term(store, term["term_id"], statuses=("current",))) == 2
    assert store.knowledge.execute(
        "SELECT count(*) FROM term_relation WHERE verb = 'conflicts_with'"
    ).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# the demo emits the key it asks judges for
# ---------------------------------------------------------------------------


def test_the_demo_judge_names_the_lemma_on_definitions_only(store):
    """``trialerror demo seed`` is the only end-to-end run most people see,
    so it takes the route a real judge takes rather than leaning on the
    accept-time heuristic. Run against the demo's own document text, so a
    corpus edit that stops containing a term fails here rather than
    quietly emitting fewer claims."""
    from trialerror.demo import content
    from trialerror.demo.seed import _extraction_judge

    body = "\n\n".join(document_text for _name, _meta, document_text in content.DOCUMENTS)
    response = _extraction_judge({"text": body})

    definitions = [c for c in response["claims"] if c["kind"] == "definition"]
    assert definitions, "the demo corpus defines something"
    named = {term for term, _type, kind in content.EXTRACTION_TERMS if kind == "definition"}
    for claim in definitions:
        assert claim["term"] in named
        assert claim["term"] in claim["text"], "the lemma is a name lifted from its own sentence"
    for claim in response["claims"]:
        if claim["kind"] != "definition":
            assert "term" not in claim
