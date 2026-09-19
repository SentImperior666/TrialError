"""``trialerror.lexicon.api`` -- the whole write lifecycle, and the reads
its consumers need.

Organized around the three invariants the package docstring states, because
those are what the tests are actually for:

1. *A sense reaches ``current`` only through the evidenced route.* Tested
   from both directions -- the happy path, and every way of trying to get a
   ``current`` sense without live evidence or without a real launch.
2. *A machine judgment can only ever open a pending row.* Including the
   adversarial version: there is no argument to any function here that
   confirms a system-marked relation without a deciding launch.
3. *Nothing is deleted.* Every decision that "removes" something is checked
   for what it left behind -- the rejected sense keeps its evidence, the
   superseded sense stays readable, the merged term keeps its rows and its
   lemma keeps resolving.

The store fixture is ``populate_one_of_everything``: it supplies a real
launch, a real anchored document, a real claim, record and idea, so the
evidence routes are exercised against rows that actually exist rather than
against ids that merely look right. It also inserts one lexicon row per
table directly (not through this API), which is why assertions here scope
themselves by term id rather than counting the whole table.
"""

from __future__ import annotations

import sqlite3

import pytest

from trialerror.lexicon import api, policy
from trialerror.lexicon.errors import (
    GlossTooLongError,
    InvalidDecisionError,
    InvalidEvidenceError,
    InvalidMergeError,
    InvalidTermInputError,
    LaunchRequiredError,
    MissingDisambiguatorError,
    RelationNotFoundError,
    RelationNotPendingError,
    SenseNotDecidableError,
    SenseNotFoundError,
    SenseWithoutEvidenceError,
    TermNotFoundError,
)
from trialerror.stores import insert
from trialerror.stores.errors import XidTargetMissingError
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests._store_fixtures import populate_one_of_everything


@pytest.fixture()
def ids(store):
    return populate_one_of_everything(store)


def _second_source(store, ids) -> tuple[str, str]:
    """A second source with its own document and anchor -- what a genuine
    disjoint-source conflict needs."""
    source_id = new_id("SRC")
    insert(
        store,
        "source",
        {
            "source_id": source_id,
            "kind": "report",
            "title": "another source",
            "license_tier": "open",
            "acquisition_route": "web",
            "request_state": "indexed",
            "registered_ts": now(),
            "registered_by_launch": ids["launch"],
        },
    )
    doc_id = new_id("DOC")
    insert(
        store,
        "document",
        {
            "doc_id": doc_id,
            "source_id": source_id,
            "rel_path": "archive/other.md",
            "media_type": "pdf",
            "normalizer_id": "pdf-text",
            "normalizer_version": "1",
            "sha256": "a" * 64,
            "status": "indexed",
        },
    )
    anchor_id = new_id("ANC")
    insert(
        store,
        "quote_anchor",
        {
            "anchor_id": anchor_id,
            "doc_id": doc_id,
            "char_start": 0,
            "char_end": 5,
            "doc_sha256": "a" * 64,
            "quote_sha256": "b" * 64,
            "quote_text": "other",
            "created_by_launch": ids["launch"],
            "created_ts": now(),
        },
    )
    return source_id, anchor_id


def _propose(store, ids, **overrides):
    kwargs = {
        "lemma": "initiative order",
        "gloss": "the sequence in which actors take their turns",
        "origin_kind": "manual",
        "origin_ref": None,
        "evidence": [f"anchor:{ids['quote_anchor']}"],
        "by_launch": ids["launch"],
        "procedure_version": policy.MANUAL_PROCEDURE_VERSION,
    }
    kwargs.update(overrides)
    return api.propose(store, **kwargs)


def _retract_around_the_api(store, evidence_id: str, *, reason: str = "written around the API") -> None:
    """Retract an evidence row with a direct UPDATE.

    Used ONLY by the tests that exercise a backstop against a state
    ``trialerror.lexicon.api`` refuses to produce (fix pass, finding F1:
    ``retract_evidence`` will not leave a live reading ungrounded). A guard
    whose only reachable input is a row somebody wrote around the API has to
    be tested with a row written around the API."""
    with store.knowledge:
        store.knowledge.execute(
            "UPDATE term_sense_evidence SET retracted_ts = ?, retracted_reason = ? WHERE evidence_id = ?",
            (now(), reason, evidence_id),
        )


def _event_types(store) -> list[str]:
    return [r[0] for r in store.ops.execute("SELECT type FROM event ORDER BY ts, event_id").fetchall()]


def _fts_row_count(store) -> int:
    return store.knowledge.execute("SELECT count(*) FROM term_fts").fetchone()[0]


# ---------------------------------------------------------------------------
# propose
# ---------------------------------------------------------------------------


def test_propose_creates_a_term_a_sense_and_its_evidence(store, ids):
    result = _propose(store, ids)

    assert result["created_term"] is True
    assert result["created_sense"] is True
    assert result["status"] == "proposed"
    assert result["term_status"] == "proposed"
    assert len(result["evidence_ids"]) == 1

    term = api.get_term(store, result["term_id"])
    assert term["lemma"] == "initiative order"
    assert term["lemma_norm"] == "initiative order"
    assert term["created_by_launch"] == ids["launch"]

    sense = api.get_sense(store, result["sense_id"])
    assert sense["status"] == "proposed"
    assert sense["decided_by_launch"] is None
    assert sense["review_after"] is None, "a proposal has no decay window yet -- accepting is what starts it"
    assert sense["created_at"] and sense["valid_at"] and sense["expired_at"] is None


def test_propose_derives_the_source_key_from_the_anchors_document(store, ids):
    result = _propose(store, ids)
    assert api.source_keys_for_sense(store, result["sense_id"]) == [ids["source"]]


@pytest.mark.parametrize(
    "spec,expected_key",
    [
        pytest.param("claim:{claim}", "{source}", id="claim reaches a source through its anchor"),
        pytest.param("record:{record}", "test-register", id="a record carries its register key"),
        pytest.param("idea:{idea}", "{idea}", id="a coinage is its own source"),
    ],
)
def test_every_evidence_kind_can_name_a_source(store, ids, spec, expected_key):
    token = spec.format(claim=ids["claim"], record=ids["record"], idea=ids["idea"])
    expected = expected_key.format(source=ids["source"], idea=ids["idea"])
    result = _propose(store, ids, evidence=[token])
    assert api.source_keys_for_sense(store, result["sense_id"]) == [expected]


def test_two_ideation_coinages_count_as_two_sources(store, ids):
    """Stated as a test because it is a choice with a consequence: folding
    all ideation into one shared key would make two independent rounds read
    as nuance when they are a genuine unreconciled disagreement."""
    second_idea = new_id("IDEA")
    insert(
        store,
        "idea",
        {
            "idea_id": second_idea,
            "author_launch": ids["launch"],
            "body": "a second coinage",
            "status": "raw",
            "created_ts": now(),
        },
    )
    a = _propose(store, ids, lemma="pressure", evidence=[f"idea:{ids['idea']}"])
    b = _propose(store, ids, lemma="tension", evidence=[f"idea:{second_idea}"])
    assert api.source_keys_for_sense(store, a["sense_id"]) != api.source_keys_for_sense(store, b["sense_id"])


def test_a_sense_with_no_evidence_is_refused_before_anything_is_written(store, ids):
    with pytest.raises(SenseWithoutEvidenceError, match="quote-grounding"):
        _propose(store, ids, evidence=[])
    assert api.find_term(store, "initiative order") is None


def test_evidence_that_cannot_name_a_source_is_refused(store, ids):
    with pytest.raises(InvalidEvidenceError, match="source_key"):
        _propose(store, ids, evidence=["record:REC-never-created"])
    assert api.find_term(store, "initiative order") is None


@pytest.mark.parametrize(
    "token,match",
    [
        ("ANC-1", "must be '<kind>:<id>'"),
        ("anchor:", "must be '<kind>:<id>'"),
        ("footnote:X-1", "not one of"),
    ],
)
def test_a_malformed_evidence_token_says_what_the_shape_is(store, ids, token, match):
    with pytest.raises(InvalidEvidenceError, match=match):
        _propose(store, ids, evidence=[token])


def test_a_gloss_over_the_cap_is_refused_as_a_transcription(store, ids):
    long_gloss = " ".join(["word"] * (policy.GLOSS_MAX_WORDS + 1))
    with pytest.raises(GlossTooLongError, match="own reading"):
        _propose(store, ids, gloss=long_gloss)


def test_the_gloss_cap_is_configurable_per_program(store, ids):
    config = {"lexicon": {"gloss_max_words": 3}}
    with pytest.raises(GlossTooLongError, match="cap is 3"):
        _propose(store, ids, config=config)
    assert _propose(store, ids, gloss="turn sequence", config=config)["created_sense"] is True


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"lemma": "   "}, "non-empty name"),
        ({"gloss": "  "}, "non-empty own-words"),
        ({"origin_kind": "guess"}, "origin_kind"),
        ({"granularity": "level-1"}, "granularity"),
        ({"status": "retired"}, "propose may land"),
    ],
)
def test_every_out_of_vocabulary_field_refuses_by_name(store, ids, overrides, match):
    with pytest.raises(InvalidTermInputError, match=match):
        _propose(store, ids, **overrides)


def test_propose_is_idempotent_on_its_origin(store, ids):
    """The partial unique index is what guarantees this; the early return is
    what makes a re-run report rather than raise. A backfill re-run inserts
    nothing at all."""
    first = _propose(store, ids, origin_kind="record_import", origin_ref=ids["record"],
                     evidence=[f"record:{ids['record']}"],
                     procedure_version=policy.RECORD_IMPORT_PROCEDURE_VERSION)
    before = store.knowledge.execute("SELECT count(*) FROM term_sense").fetchone()[0]

    second = _propose(store, ids, origin_kind="record_import", origin_ref=ids["record"],
                      evidence=[f"record:{ids['record']}"],
                      procedure_version=policy.RECORD_IMPORT_PROCEDURE_VERSION)

    assert second["sense_id"] == first["sense_id"]
    assert second["created_sense"] is False
    assert second["candidates"]["status"] == "skipped"
    assert store.knowledge.execute("SELECT count(*) FROM term_sense").fetchone()[0] == before


def test_a_second_reading_attaches_to_the_existing_term(store, ids):
    first = _propose(store, ids)
    second = _propose(store, ids, gloss="who acts, in what order", evidence=[f"claim:{ids['claim']}"])
    assert second["term_id"] == first["term_id"]
    assert second["created_term"] is False
    assert len(api.senses_for_term(store, first["term_id"])) == 2


def test_a_proposal_against_an_alias_finds_the_term_that_owns_it(store, ids):
    first = _propose(store, ids, aliases=[("turn order", "variant")])
    second = _propose(store, ids, lemma="Turn  Order", gloss="the same thing, named differently",
                      evidence=[f"claim:{ids['claim']}"])
    assert second["term_id"] == first["term_id"]
    assert second["created_term"] is False


def test_an_existing_term_learns_what_it_did_not_know_and_nothing_else(store, ids):
    first = _propose(store, ids, granularity="instance", tags=["f-a"])
    api.propose(
        store,
        lemma="initiative order",
        gloss="a later reading with different metadata",
        origin_kind="manual",
        origin_ref=None,
        evidence=[f"claim:{ids['claim']}"],
        by_launch=ids["launch"],
        procedure_version=policy.MANUAL_PROCEDURE_VERSION,
        granularity="family",
        tags=["f-b"],
    )
    term = api.get_term(store, first["term_id"])
    assert term["granularity"] == "instance", "a later proposal never overwrites a decided level"
    assert term["tags"] == '["f-a"]'


def test_an_alias_that_duplicates_the_lemma_or_another_alias_is_not_an_error(store, ids):
    result = _propose(store, ids, aliases=["Initiative Order", "turn order", "TURN ORDER"])
    assert len(result["alias_ids"]) == 1, "the lemma itself and the repeated spelling add no key"


# ---------------------------------------------------------------------------
# invariant 1: current only through the evidenced route
# ---------------------------------------------------------------------------


def test_accept_promotes_the_sense_the_term_and_the_preference(store, ids):
    proposed = _propose(store, ids)
    accepted = api.accept_sense(store, proposed["sense_id"], by_launch=ids["launch"])

    assert accepted["status"] == "current"
    assert accepted["term_status"] == "active"
    sense = api.get_sense(store, proposed["sense_id"])
    assert sense["decided_by_launch"] == ids["launch"] and sense["decided_ts"]
    assert sense["review_after"] > now()
    term = api.get_term(store, proposed["term_id"])
    assert term["preferred_sense_id"] == proposed["sense_id"]


def test_accept_stamps_the_decay_window_for_the_senses_own_origin(store, ids):
    """engram-F5's type-keyed decay: an imported register row ages slower
    than a round's coinage, because the two are provisional to different
    degrees."""
    imported = _propose(store, ids, lemma="a", origin_kind="record_import", origin_ref=ids["record"],
                        evidence=[f"record:{ids['record']}"],
                        procedure_version=policy.RECORD_IMPORT_PROCEDURE_VERSION)
    coined = _propose(store, ids, lemma="b", origin_kind="ideation", origin_ref=ids["idea"],
                      evidence=[f"idea:{ids['idea']}"], procedure_version="round-v1")
    a = api.accept_sense(store, imported["sense_id"], by_launch=ids["launch"])
    b = api.accept_sense(store, coined["sense_id"], by_launch=ids["launch"])
    assert a["review_after"] > b["review_after"]


def test_accept_writes_the_derived_from_provenance_edge(store, ids):
    """Ruling L-E5: the lexicon is the provenance graph's first writer. Lane
    c's Evidence panel reads exactly this table."""
    proposed = _propose(store, ids, origin_kind="extract", origin_ref=ids["claim"],
                        evidence=[f"claim:{ids['claim']}"],
                        procedure_version=policy.EXTRACT_PROCEDURE_VERSION)
    accepted = api.accept_sense(store, proposed["sense_id"], by_launch=ids["launch"])

    edge = store.knowledge.execute(
        "SELECT * FROM prov_edge WHERE edge_id = ?", (accepted["prov_edge"],)
    ).fetchone()
    assert (edge["role"], edge["src_kind"], edge["src_id"], edge["dst_kind"], edge["dst_id"]) == (
        "derived_from", "term_sense", proposed["sense_id"], "claim", ids["claim"],
    )
    assert edge["launch_id"] == ids["launch"]


def test_a_manual_sense_with_no_origin_writes_no_edge_rather_than_a_dangling_one(store, ids):
    proposed = _propose(store, ids)
    accepted = api.accept_sense(store, proposed["sense_id"], by_launch=ids["launch"])
    assert accepted["prov_edge"] is None


def test_propose_current_runs_the_accept_path_rather_than_setting_the_column(store, ids):
    """Invariant 1 as one code path: the two routes design §4 lets land
    already-accepted get the identical evidence check, decay stamp,
    provenance edge and event pair a later ``accept_sense`` would."""
    result = _propose(store, ids, status="current", origin_kind="record_import",
                      origin_ref=ids["record"], evidence=[f"record:{ids['record']}"],
                      procedure_version=policy.RECORD_IMPORT_PROCEDURE_VERSION)
    assert result["status"] == "current"
    assert result["term_status"] == "active"
    assert result["review_after"]

    sense = api.get_sense(store, result["sense_id"])
    assert sense["decided_by_launch"] == ids["launch"]
    types = _event_types(store)
    assert types.count("term_sense_proposed") == 1
    assert types.count("term_sense_accepted") == 1


def test_a_sense_whose_evidence_was_all_retracted_cannot_become_current(store, ids):
    """``accept_sense``'s own grounding check, exercised against a state the
    API now refuses to create (fix pass, finding F1) -- so the state is
    written around the API here on purpose. The check stays because it is the
    second of two guards on one invariant, and this pins that it still
    refuses if a row ever reaches the store some other way."""
    proposed = _propose(store, ids)
    _retract_around_the_api(store, proposed["evidence_ids"][0])
    with pytest.raises(SenseWithoutEvidenceError, match="no live evidence"):
        api.accept_sense(store, proposed["sense_id"], by_launch=ids["launch"])
    assert api.get_sense(store, proposed["sense_id"])["status"] == "proposed"


def test_retraction_keeps_the_row_and_demands_a_reason(store, ids):
    second_source, second_anchor = _second_source(store, ids)
    proposed = _propose(
        store, ids, evidence=[f"anchor:{ids['quote_anchor']}", f"anchor:{second_anchor}"]
    )
    evidence_id = proposed["evidence_ids"][0]
    with pytest.raises(InvalidEvidenceError, match="requires a reason"):
        api.retract_evidence(store, evidence_id, by_launch=ids["launch"], reason="")

    api.retract_evidence(store, evidence_id, by_launch=ids["launch"], reason="wrong page")
    live = api.evidence_for_sense(store, proposed["sense_id"])
    assert [e["evidence_id"] for e in live] == [proposed["evidence_ids"][1]]
    kept = api.evidence_for_sense(store, proposed["sense_id"], include_retracted=True)
    assert len(kept) == 2
    assert [e["retracted_reason"] for e in kept if e["evidence_id"] == evidence_id] == ["wrong page"]


def test_a_second_retraction_is_reported_not_repeated(store, ids):
    _second_source_id, second_anchor = _second_source(store, ids)
    proposed = _propose(
        store, ids, evidence=[f"anchor:{ids['quote_anchor']}", f"anchor:{second_anchor}"]
    )
    evidence_id = proposed["evidence_ids"][0]
    api.retract_evidence(store, evidence_id, by_launch=ids["launch"], reason="wrong page")
    again = api.retract_evidence(store, evidence_id, by_launch=ids["launch"], reason="again")
    assert again["retracted"] is False


@pytest.mark.parametrize("status", ["proposed", "current"])
def test_retracting_the_last_row_under_a_live_reading_is_refused(store, ids, status):
    """Finding F1. Before the fix this succeeded silently: the sense stayed
    ``current``, the term stayed ``active`` with ``preferred_sense_id``
    pointing at it, and the fail-level ``term_sense_without_evidence`` check
    went from 0 to 1 in one public-API call -- on an invariant the package
    docstring calls structurally unreachable."""
    proposed = _propose(store, ids, status=status)
    evidence_id = proposed["evidence_ids"][0]

    with pytest.raises(SenseWithoutEvidenceError, match="no live evidence"):
        api.retract_evidence(store, evidence_id, by_launch=ids["launch"], reason="wrong page")

    sense = api.get_sense(store, proposed["sense_id"])
    assert sense["status"] == status
    assert api.evidence_for_sense(store, proposed["sense_id"])[0]["evidence_id"] == evidence_id
    assert api.get_term(store, proposed["term_id"])["status"] == (
        "active" if status == "current" else "proposed"
    )


def test_the_refusal_names_the_verb_that_ends_a_reading(store, ids):
    """A refusal that does not say what to do instead is a wall. Retiring the
    reading first is the sanctioned route, and it leaves the retraction
    recorded against a reading that no longer claims to be grounded."""
    proposed = _propose(store, ids, status="current")
    evidence_id = proposed["evidence_ids"][0]
    with pytest.raises(SenseWithoutEvidenceError, match="retire_sense"):
        api.retract_evidence(store, evidence_id, by_launch=ids["launch"], reason="wrong page")

    api.retire_sense(store, proposed["sense_id"], by_launch=ids["launch"], reason="the reading lapsed")
    result = api.retract_evidence(store, evidence_id, by_launch=ids["launch"], reason="wrong page")
    assert result["retracted"] is True
    assert api.get_sense(store, proposed["sense_id"])["status"] == "retired"
    assert api.get_term(store, proposed["term_id"])["preferred_sense_id"] is None


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda store, ids, s: api.propose(
            store, lemma="x", gloss="y", origin_kind="manual", origin_ref=None,
            evidence=[f"anchor:{ids['quote_anchor']}"], by_launch=None,
            procedure_version="manual-v1"), id="propose"),
        pytest.param(lambda store, ids, s: api.accept_sense(store, s, by_launch=None), id="accept"),
        pytest.param(lambda store, ids, s: api.reject_sense(store, s, by_launch=None), id="reject"),
        pytest.param(lambda store, ids, s: api.supersede_sense(store, s, gloss="z", by_launch=None),
                     id="supersede"),
        pytest.param(lambda store, ids, s: api.retire_sense(store, s, by_launch=None), id="retire"),
        pytest.param(lambda store, ids, s: api.mark_reviewed(store, s, by_launch=None), id="mark_reviewed"),
        pytest.param(lambda store, ids, s: api.merge_terms(store, "TERM-a", "TERM-b", by_launch=None),
                     id="merge"),
    ],
)
def test_every_deciding_verb_refuses_without_a_launch(store, ids, call):
    proposed = _propose(store, ids)
    with pytest.raises(LaunchRequiredError, match="by_launch"):
        call(store, ids, proposed["sense_id"])


def test_a_launch_id_that_names_no_row_is_refused_before_any_write(store, ids):
    """Ruling L-E4's guard, and the reason it is a pre-flight: the refusal
    must arrive before the mutation, not after it."""
    proposed = _propose(store, ids)
    with pytest.raises(XidTargetMissingError, match="platform.launch"):
        api.accept_sense(store, proposed["sense_id"], by_launch="LNCH-does-not-exist")
    assert api.get_sense(store, proposed["sense_id"])["status"] == "proposed"


@pytest.mark.parametrize("status", ["current", "rejected"])
def test_a_decided_sense_is_not_re_decided(store, ids, status):
    proposed = _propose(store, ids)
    if status == "current":
        api.accept_sense(store, proposed["sense_id"], by_launch=ids["launch"])
    else:
        api.reject_sense(store, proposed["sense_id"], by_launch=ids["launch"])
    with pytest.raises(SenseNotDecidableError, match="not 'proposed'"):
        api.accept_sense(store, proposed["sense_id"], by_launch=ids["launch"])


def test_an_unknown_sense_or_relation_says_so(store, ids):
    with pytest.raises(SenseNotFoundError):
        api.accept_sense(store, "SENSE-ghost", by_launch=ids["launch"])
    with pytest.raises(RelationNotFoundError):
        api.decide_relation(store, "TREL-ghost", decision="rejected", by_launch=ids["launch"])


def test_rejecting_a_sense_keeps_the_evidence_it_was_rejected_on(store, ids):
    proposed = _propose(store, ids)
    result = api.reject_sense(store, proposed["sense_id"], by_launch=ids["launch"], reason="not a term")
    assert result["status"] == "rejected"
    assert len(api.evidence_for_sense(store, proposed["sense_id"])) == 1
    assert api.get_term(store, proposed["term_id"])["status"] == "proposed"


# ---------------------------------------------------------------------------
# supersede / retire / review
# ---------------------------------------------------------------------------


def test_superseding_asserts_a_replacement_and_keeps_the_old_reading_readable(store, ids):
    proposed = _propose(store, ids)
    api.accept_sense(store, proposed["sense_id"], by_launch=ids["launch"])

    result = api.supersede_sense(
        store, proposed["sense_id"], gloss="the order in which actors act each round",
        by_launch=ids["launch"], reason="sharper wording",
    )
    old = api.get_sense(store, proposed["sense_id"])
    new = api.get_sense(store, result["sense_id"])

    assert old["status"] == "superseded" and old["expired_at"] and old["superseded_by"] == result["sense_id"]
    assert old["gloss"] == "the sequence in which actors take their turns", "the old wording survives"
    assert new["status"] == "current" and new["expired_at"] is None
    assert api.get_term(store, proposed["term_id"])["preferred_sense_id"] == result["sense_id"]


def test_the_replacement_carries_no_origin_ref_and_says_so_through_provenance(store, ids):
    """A consequence of the partial unique index, not a choice made here: an
    origin belongs to the one row actually derived from it. The correction's
    lineage runs through ``superseded_by`` and a ``supersedes`` edge."""
    proposed = _propose(store, ids, origin_kind="extract", origin_ref=ids["claim"],
                        evidence=[f"claim:{ids['claim']}"],
                        procedure_version=policy.EXTRACT_PROCEDURE_VERSION)
    api.accept_sense(store, proposed["sense_id"], by_launch=ids["launch"])
    result = api.supersede_sense(store, proposed["sense_id"], gloss="a corrected reading",
                                 by_launch=ids["launch"])

    assert api.get_sense(store, result["sense_id"])["origin_ref"] is None
    edge = store.knowledge.execute(
        "SELECT * FROM prov_edge WHERE edge_id = ?", (result["prov_edge"],)
    ).fetchone()
    assert (edge["role"], edge["src_id"], edge["dst_id"]) == (
        "supersedes", result["sense_id"], proposed["sense_id"],
    )


def test_superseding_copies_the_evidence_forward_and_moves_none_of_it(store, ids):
    proposed = _propose(store, ids)
    api.accept_sense(store, proposed["sense_id"], by_launch=ids["launch"])
    result = api.supersede_sense(store, proposed["sense_id"], gloss="a corrected reading",
                                 by_launch=ids["launch"])

    old_evidence = api.evidence_for_sense(store, proposed["sense_id"])
    new_evidence = api.evidence_for_sense(store, result["sense_id"])
    assert len(old_evidence) == 1, "the raw layer never moves"
    assert len(new_evidence) == 1
    assert old_evidence[0]["evidence_id"] != new_evidence[0]["evidence_id"]
    assert old_evidence[0]["anchor_id"] == new_evidence[0]["anchor_id"]
    assert old_evidence[0]["source_key"] == new_evidence[0]["source_key"]


def test_only_a_current_reading_can_be_superseded(store, ids):
    proposed = _propose(store, ids)
    with pytest.raises(SenseNotDecidableError, match="only a 'current' reading"):
        api.supersede_sense(store, proposed["sense_id"], gloss="x", by_launch=ids["launch"])


def test_retiring_closes_the_event_time_window_without_deleting(store, ids):
    proposed = _propose(store, ids)
    api.accept_sense(store, proposed["sense_id"], by_launch=ids["launch"])
    api.retire_sense(store, proposed["sense_id"], by_launch=ids["launch"], reason="no longer used")

    sense = api.get_sense(store, proposed["sense_id"])
    assert sense["status"] == "retired"
    assert sense["invalid_at"], "the fact stopped being true, and that is the event-time axis"
    assert sense["expired_at"] is None, "the DB still believes it recorded this correctly"
    assert api.get_term(store, proposed["term_id"])["preferred_sense_id"] is None


def test_retiring_the_preferred_sense_hands_the_preference_to_a_survivor(store, ids):
    first = _propose(store, ids)
    api.accept_sense(store, first["sense_id"], by_launch=ids["launch"])
    second = _propose(store, ids, gloss="a second live reading", evidence=[f"claim:{ids['claim']}"])
    api.accept_sense(store, second["sense_id"], by_launch=ids["launch"])

    api.retire_sense(store, first["sense_id"], by_launch=ids["launch"])
    assert api.get_term(store, first["term_id"])["preferred_sense_id"] == second["sense_id"]


def test_review_is_a_read_time_flag_and_never_a_status(store, ids):
    """MINING §5.7: staleness surfaces a row for a human; it decides
    nothing. ``needs_review`` is computed from ``review_after`` and no
    column anywhere records it."""
    proposed = _propose(store, ids)
    api.accept_sense(store, proposed["sense_id"], by_launch=ids["launch"])
    sense = api.get_sense(store, proposed["sense_id"])
    assert api.needs_review(sense) is False

    store.knowledge.execute(
        "UPDATE term_sense SET review_after = '2000-01-01T00:00:00.000Z' WHERE sense_id = ?",
        (proposed["sense_id"],),
    )
    store.knowledge.commit()
    stale = api.get_sense(store, proposed["sense_id"])
    assert api.needs_review(stale) is True
    assert stale["status"] == "current", "the decay flag did not move the status"

    api.mark_reviewed(store, proposed["sense_id"], by_launch=ids["launch"])
    reviewed = api.get_sense(store, proposed["sense_id"])
    assert reviewed["reviewed_ts"] and api.needs_review(reviewed) is False


@pytest.mark.parametrize("status", ["proposed", "rejected", "superseded", "retired"])
def test_only_a_current_reading_is_ever_stale(store, ids, status):
    assert api.needs_review({"status": status, "review_after": "2000-01-01T00:00:00.000Z"}) is False


def test_marking_a_non_current_sense_reviewed_is_refused(store, ids):
    proposed = _propose(store, ids)
    with pytest.raises(SenseNotDecidableError, match="review window"):
        api.mark_reviewed(store, proposed["sense_id"], by_launch=ids["launch"])


# ---------------------------------------------------------------------------
# invariant 2: a machine judgment only ever opens a pending row
# ---------------------------------------------------------------------------


def test_a_system_relation_carries_no_launch_and_names_the_scan_that_opened_it(store, ids):
    first = _propose(store, ids, lemma="save")
    second = _propose(store, ids, lemma="saving throw", evidence=[f"claim:{ids['claim']}"])
    opened = api.open_relation(
        store,
        src_kind="term", src_id=second["term_id"],
        dst_kind="term", dst_id=first["term_id"],
        verb="same_as", marked_by_kind="system", confidence=0.7,
    )
    rel = api.get_relation(store, opened["rel_id"])
    assert rel["status"] == "pending"
    assert rel["marked_by_launch"] is None
    assert rel["marked_by_model"] == policy.SYSTEM_SCAN_MODEL


def test_attributing_a_machine_judgment_to_a_launch_is_refused(store, ids):
    with pytest.raises(InvalidTermInputError, match="advisory"):
        api.open_relation(
            store, src_kind="term", src_id="TERM-a", dst_kind="term", dst_id="TERM-b",
            verb="same_as", marked_by_kind="system", marked_by_launch=ids["launch"],
        )


def test_a_launch_marked_relation_must_carry_one(store, ids):
    with pytest.raises(LaunchRequiredError):
        api.open_relation(
            store, src_kind="term", src_id="TERM-a", dst_kind="term", dst_id="TERM-b",
            verb="same_as", marked_by_kind="launch",
        )


def test_there_is_no_argument_that_opens_a_confirmed_relation(store, ids):
    """The adversarial version of invariant 2. ``open_relation`` has no
    ``status`` parameter at all, so a machine judgment cannot be born
    decided -- proposing and deciding are different calls even when one
    operator makes them a second apart."""
    import inspect

    assert "status" not in inspect.signature(api.open_relation).parameters
    opened = api.open_relation(
        store, src_kind="term", src_id="TERM-a", dst_kind="term", dst_id="TERM-b",
        verb="same_as", marked_by_kind="system",
    )
    assert api.get_relation(store, opened["rel_id"])["status"] == "pending"


def test_confirming_a_system_relation_still_takes_a_real_launch(store, ids):
    first = _propose(store, ids, lemma="save")
    second = _propose(store, ids, lemma="saving throw", evidence=[f"claim:{ids['claim']}"])
    opened = api.open_relation(
        store, src_kind="term", src_id=second["term_id"], dst_kind="term",
        dst_id=first["term_id"], verb="same_as", marked_by_kind="system",
    )
    with pytest.raises(LaunchRequiredError):
        api.decide_relation(store, opened["rel_id"], decision="same_as", by_launch=None)
    with pytest.raises(XidTargetMissingError):
        api.decide_relation(store, opened["rel_id"], decision="same_as", by_launch="LNCH-ghost")

    assert api.get_relation(store, opened["rel_id"])["status"] == "pending"
    assert api.get_term(store, second["term_id"])["status"] != "merged"


def test_a_decided_relation_is_not_decided_again(store, ids):
    opened = api.open_relation(
        store, src_kind="term", src_id="TERM-a", dst_kind="term", dst_id="TERM-b",
        verb="unrelated", marked_by_kind="system",
    )
    api.decide_relation(store, opened["rel_id"], decision="rejected", by_launch=ids["launch"])
    with pytest.raises(RelationNotPendingError, match="never silently redone"):
        api.decide_relation(store, opened["rel_id"], decision="rejected", by_launch=ids["launch"])


@pytest.mark.parametrize("decision", ["conflicts_with", "supersedes", "merge", "yes"])
def test_a_decision_outside_the_closed_set_is_refused(store, ids, decision):
    opened = api.open_relation(
        store, src_kind="term", src_id="TERM-a", dst_kind="term", dst_id="TERM-b",
        verb="conflicts_with", marked_by_kind="system",
    )
    with pytest.raises(InvalidDecisionError, match="not one of"):
        api.decide_relation(store, opened["rel_id"], decision=decision, by_launch=ids["launch"])


def test_rejecting_a_candidate_moves_nothing_but_the_relation(store, ids):
    first = _propose(store, ids, lemma="save")
    second = _propose(store, ids, lemma="saving throw", evidence=[f"claim:{ids['claim']}"])
    opened = api.open_relation(
        store, src_kind="term", src_id=second["term_id"], dst_kind="term",
        dst_id=first["term_id"], verb="same_as", marked_by_kind="system",
    )
    api.decide_relation(store, opened["rel_id"], decision="rejected", by_launch=ids["launch"],
                        reason="different concepts")

    rel = api.get_relation(store, opened["rel_id"])
    assert rel["status"] == "rejected"
    assert rel["decided_verb"] is None, "a rejection confirms no verb"
    assert rel["decided_by_launch"] == ids["launch"]
    assert api.get_term(store, second["term_id"])["status"] == "proposed"
    assert api.get_term(store, second["term_id"])["merged_into"] is None


# ---------------------------------------------------------------------------
# decisions that move rows: merge, scope, reconcile
# ---------------------------------------------------------------------------


def _two_terms(store, ids):
    keep = _propose(store, ids, lemma="saving throw", aliases=["save vs."])
    api.accept_sense(store, keep["sense_id"], by_launch=ids["launch"])
    other = _propose(store, ids, lemma="save", gloss="a roll to avoid an effect",
                     evidence=[f"claim:{ids['claim']}"], aliases=["saves"])
    api.accept_sense(store, other["sense_id"], by_launch=ids["launch"])
    return keep, other


def test_same_as_folds_one_term_into_the_other_and_deletes_nothing(store, ids):
    keep, other = _two_terms(store, ids)
    opened = api.open_relation(
        store, src_kind="term", src_id=other["term_id"], dst_kind="term",
        dst_id=keep["term_id"], verb="same_as", marked_by_kind="system",
    )
    result = api.decide_relation(store, opened["rel_id"], decision="same_as", by_launch=ids["launch"])

    merged = api.get_term(store, other["term_id"])
    assert merged is not None, "nothing is deleted"
    assert merged["status"] == "merged" and merged["merged_into"] == keep["term_id"]
    assert api.get_sense(store, other["sense_id"])["term_id"] == keep["term_id"]
    assert result["merge"]["senses_moved"] == [other["sense_id"]]


def test_the_folded_lemma_keeps_resolving_through_its_former_lemma_alias(store, ids):
    keep, other = _two_terms(store, ids)
    api.merge_terms(store, keep["term_id"], other["term_id"], by_launch=ids["launch"])
    assert api.find_term(store, "save")["term_id"] == keep["term_id"]
    assert api.find_term(store, "saves")["term_id"] == keep["term_id"], "its aliases come too"


def test_a_lookup_follows_the_merge_but_an_audit_can_see_the_row_as_stored(store, ids):
    """A merge deletes nothing, so the folded row keeps its own
    ``lemma_norm`` -- and a raw lookup would keep returning a term nobody
    should be writing to. Following ``merged_into`` is what makes "what does
    this name mean" answerable; ``follow_merges=False`` is what makes the
    merge itself auditable."""
    keep, other = _two_terms(store, ids)
    api.merge_terms(store, keep["term_id"], other["term_id"], by_launch=ids["launch"])

    assert api.find_term(store, "save")["term_id"] == keep["term_id"]
    stored = api.find_term(store, "save", follow_merges=False)
    assert stored["term_id"] == other["term_id"] and stored["status"] == "merged"


def test_a_later_proposal_against_a_folded_lemma_lands_on_the_canonical_term(store, ids):
    keep, other = _two_terms(store, ids)
    api.merge_terms(store, keep["term_id"], other["term_id"], by_launch=ids["launch"])
    later = _propose(store, ids, lemma="save", gloss="a third reading, arriving after the merge",
                     evidence=[f"record:{ids['record']}"])
    assert later["term_id"] == keep["term_id"]
    assert later["created_term"] is False


def test_variant_of_is_the_same_fold_under_a_different_alias_kind(store, ids):
    keep, other = _two_terms(store, ids)
    opened = api.open_relation(
        store, src_kind="term", src_id=other["term_id"], dst_kind="term",
        dst_id=keep["term_id"], verb="same_as", marked_by_kind="system",
    )
    api.decide_relation(store, opened["rel_id"], decision="variant_of", by_launch=ids["launch"])
    kinds = {
        r[0]
        for r in store.knowledge.execute(
            "SELECT kind FROM term_alias WHERE term_id = ? AND alias_norm = 'save'", (keep["term_id"],)
        )
    }
    assert kinds == {"variant"}


def test_the_destination_term_is_canonical_by_default_and_the_default_is_overridable(store, ids):
    keep, other = _two_terms(store, ids)
    opened = api.open_relation(
        store, src_kind="term", src_id=other["term_id"], dst_kind="term",
        dst_id=keep["term_id"], verb="same_as", marked_by_kind="system",
    )
    result = api.decide_relation(
        store, opened["rel_id"], decision="same_as", by_launch=ids["launch"],
        canonical=other["term_id"],
    )
    assert result["merge"]["canonical_term_id"] == other["term_id"]
    assert api.get_term(store, keep["term_id"])["status"] == "merged"


def test_a_canonical_that_is_neither_side_of_the_relation_is_refused(store, ids):
    keep, other = _two_terms(store, ids)
    opened = api.open_relation(
        store, src_kind="term", src_id=other["term_id"], dst_kind="term",
        dst_id=keep["term_id"], verb="same_as", marked_by_kind="system",
    )
    with pytest.raises(InvalidDecisionError, match="neither side"):
        api.decide_relation(store, opened["rel_id"], decision="same_as",
                            by_launch=ids["launch"], canonical="TERM-elsewhere")


@pytest.mark.parametrize("decision", ["same_as", "variant_of"])
def test_a_duplicate_decision_on_a_sense_scoped_relation_is_refused(store, ids, decision):
    proposed = _propose(store, ids)
    opened = api.open_relation(
        store, src_kind="sense", src_id=proposed["sense_id"], dst_kind="sense",
        dst_id=proposed["sense_id"], verb="same_as", marked_by_kind="system",
    )
    with pytest.raises(InvalidDecisionError, match="term-to-term"):
        api.decide_relation(store, opened["rel_id"], decision=decision, by_launch=ids["launch"])


def _conflicted_term(store, ids):
    """One term, two current senses, disjoint sources -- the shape a
    conflict item is about."""
    _source_b, anchor_b = _second_source(store, ids)
    first = _propose(store, ids, lemma="pressure")
    api.accept_sense(store, first["sense_id"], by_launch=ids["launch"])
    second = _propose(store, ids, lemma="pressure", gloss="a countdown that forces a decision",
                      evidence=[f"anchor:{anchor_b}"])
    api.accept_sense(store, second["sense_id"], by_launch=ids["launch"])
    opened = api.open_relation(
        store, src_kind="term", src_id=first["term_id"], dst_kind="term", dst_id=first["term_id"],
        verb="conflicts_with", marked_by_kind="system",
        evidence={"sense_ids": [first["sense_id"], second["sense_id"]], "shared_sources": []},
    )
    return first, second, opened["rel_id"]


def test_scoping_keeps_both_readings_names_them_apart_and_records_the_contradiction(store, ids):
    first, second, rel_id = _conflicted_term(store, ids)
    result = api.decide_relation(
        store, rel_id, decision="scoped", by_launch=ids["launch"],
        disambiguators={first["sense_id"]: "resource sense", second["sense_id"]: "timer sense"},
    )

    assert api.get_term(store, first["term_id"])["status"] == "split"
    assert api.get_sense(store, first["sense_id"])["disambiguator"] == "resource sense"
    assert api.get_sense(store, second["sense_id"])["disambiguator"] == "timer sense"
    for sense_id in (first["sense_id"], second["sense_id"]):
        assert api.get_sense(store, sense_id)["status"] == "current", "both readings are kept"

    edges = store.knowledge.execute(
        "SELECT src_id, dst_id FROM prov_edge WHERE role = 'contradicts'"
    ).fetchall()
    assert [(r[0], r[1]) for r in edges] == [(first["sense_id"], second["sense_id"])]
    assert len(result["prov_edges"]) == 1


def test_scoping_without_a_name_for_every_kept_reading_is_refused(store, ids):
    first, second, rel_id = _conflicted_term(store, ids)
    with pytest.raises(MissingDisambiguatorError, match="name of its own"):
        api.decide_relation(store, rel_id, decision="scoped", by_launch=ids["launch"],
                            disambiguators={first["sense_id"]: "resource sense"})
    assert api.get_term(store, first["term_id"])["status"] == "active", "nothing moved"


def test_not_conflict_supersedes_the_others_into_the_kept_reading(store, ids):
    first, second, rel_id = _conflicted_term(store, ids)
    result = api.decide_relation(
        store, rel_id, decision="not_conflict", by_launch=ids["launch"], into=first["sense_id"]
    )

    kept = api.get_sense(store, first["sense_id"])
    folded = api.get_sense(store, second["sense_id"])
    assert kept["status"] == "current"
    assert folded["status"] == "superseded"
    assert folded["superseded_by"] == first["sense_id"] and folded["expired_at"]
    assert result["superseded_sense_ids"] == [second["sense_id"]]
    assert api.get_term(store, first["term_id"])["status"] == "active"
    assert api.get_term(store, first["term_id"])["preferred_sense_id"] == first["sense_id"]


def test_not_conflict_leaves_the_kept_reading_standing_on_the_union_of_the_sources(store, ids):
    first, second, rel_id = _conflicted_term(store, ids)
    before = set(api.source_keys_for_sense(store, first["sense_id"]))
    other = set(api.source_keys_for_sense(store, second["sense_id"]))
    assert not (before & other), "the fixture really is disjoint"

    api.decide_relation(store, rel_id, decision="not_conflict", by_launch=ids["launch"],
                        into=first["sense_id"])

    assert set(api.source_keys_for_sense(store, first["sense_id"])) == before | other
    assert len(api.evidence_for_sense(store, second["sense_id"])) == 1, "no evidence row was lost"


def test_not_conflict_does_not_duplicate_evidence_the_kept_reading_already_had(store, ids):
    first = _propose(store, ids, lemma="pressure")
    api.accept_sense(store, first["sense_id"], by_launch=ids["launch"])
    second = _propose(store, ids, lemma="pressure", gloss="the same source, read again",
                      evidence=[f"claim:{ids['claim']}"])
    api.accept_sense(store, second["sense_id"], by_launch=ids["launch"])
    opened = api.open_relation(
        store, src_kind="term", src_id=first["term_id"], dst_kind="term", dst_id=first["term_id"],
        verb="conflicts_with", marked_by_kind="system",
        evidence={"sense_ids": [first["sense_id"], second["sense_id"]]},
    )
    api.decide_relation(store, opened["rel_id"], decision="not_conflict",
                        by_launch=ids["launch"], into=first["sense_id"])
    kinds = [(e["evidence_kind"], e["anchor_id"] or e["ref_id"])
             for e in api.evidence_for_sense(store, first["sense_id"])]
    assert len(kinds) == len(set(kinds))


@pytest.mark.parametrize("into", [None, "SENSE-not-a-member"])
def test_not_conflict_needs_a_kept_reading_that_is_actually_a_member(store, ids, into):
    _first, _second, rel_id = _conflicted_term(store, ids)
    with pytest.raises(InvalidDecisionError, match="into=<sense_id>"):
        api.decide_relation(store, rel_id, decision="not_conflict", by_launch=ids["launch"], into=into)


# ---------------------------------------------------------------------------
# fix pass F5 -- a decision is refused when its member set has gone stale
# ---------------------------------------------------------------------------


def test_not_conflict_into_a_retired_reading_is_refused(store, ids):
    """Finding F5's headline. A conflict item's members are a snapshot the
    scan took; nothing used to re-read them. Deciding ``not_conflict --into``
    a member retired in between left the term ``active`` with ZERO current
    senses, ``preferred_sense_id`` naming the dead reading, and the loser's
    ``superseded_by`` chain terminating on it -- after which
    ``conflicts_for_term`` said "fewer than two grounded current readings"
    and never raised it again."""
    first, second, rel_id = _conflicted_term(store, ids)
    api.retire_sense(store, first["sense_id"], by_launch=ids["launch"], reason="the reading lapsed")

    with pytest.raises(SenseNotDecidableError, match="no longer current"):
        api.decide_relation(
            store, rel_id, decision="not_conflict", by_launch=ids["launch"], into=first["sense_id"]
        )

    assert api.get_relation(store, rel_id)["status"] == "pending", "the item is still there to decide"
    assert api.get_sense(store, second["sense_id"])["status"] == "current", "nothing was superseded"
    term = api.get_term(store, first["term_id"])
    assert term["status"] == "active"
    assert term["preferred_sense_id"] == second["sense_id"]
    assert [s["sense_id"] for s in api.senses_for_term(store, term["term_id"], statuses=("current",))] == [
        second["sense_id"]
    ]


def test_not_conflict_with_a_stale_loser_is_refused_too(store, ids):
    """The same hole from the other side: ``into`` is fine, a member being
    folded into it is not."""
    first, second, rel_id = _conflicted_term(store, ids)
    api.retire_sense(store, second["sense_id"], by_launch=ids["launch"], reason="the reading lapsed")
    with pytest.raises(SenseNotDecidableError, match=second["sense_id"]):
        api.decide_relation(
            store, rel_id, decision="not_conflict", by_launch=ids["launch"], into=first["sense_id"]
        )
    assert api.get_sense(store, first["sense_id"])["status"] == "current"


def test_scoping_a_member_that_is_no_longer_current_is_refused(store, ids):
    """``scoped`` had the hole from the other end: a member rejected or
    retired between scan and decision was given a disambiguator anyway and
    its term was marked ``split``."""
    first, second, rel_id = _conflicted_term(store, ids)
    api.retire_sense(store, second["sense_id"], by_launch=ids["launch"], reason="the reading lapsed")

    with pytest.raises(SenseNotDecidableError, match="term scan"):
        api.decide_relation(
            store, rel_id, decision="scoped", by_launch=ids["launch"],
            disambiguators={first["sense_id"]: "resource sense", second["sense_id"]: "timer sense"},
        )

    assert api.get_term(store, first["term_id"])["status"] == "active", "the term was not split"
    assert api.get_sense(store, first["sense_id"])["disambiguator"] is None, "and nothing was named"
    assert api.get_relation(store, rel_id)["status"] == "pending"


def test_the_stale_member_refusal_names_the_ids_and_the_way_out(store, ids):
    first, second, rel_id = _conflicted_term(store, ids)
    api.retire_sense(store, second["sense_id"], by_launch=ids["launch"], reason="the reading lapsed")
    with pytest.raises(SenseNotDecidableError) as excinfo:
        api.decide_relation(
            store, rel_id, decision="not_conflict", by_launch=ids["launch"], into=first["sense_id"]
        )
    message = str(excinfo.value)
    assert second["sense_id"] in message and "retired" in message
    assert "term scan" in message


# ---------------------------------------------------------------------------
# fix pass F2 -- a refusal means nothing was written
# ---------------------------------------------------------------------------


def test_a_bad_alias_kind_is_refused_before_the_first_write(store, ids):
    """Finding F2. The alias vocabulary used to be checked inside
    ``_add_alias``, which runs after the term, the sense and every evidence
    row are committed by their own transactions and before the reindex and
    both events -- so this refusal, which means "nothing happened", used to
    leave three rows behind that no ``event`` recorded and no ``term_fts``
    row covered."""
    before = _event_types(store)
    fts_before = _fts_row_count(store)

    with pytest.raises(InvalidTermInputError, match="alias kind"):
        _propose(store, ids, lemma="quorum", aliases=[{"alias": "quora", "kind": "plurals"}])

    assert api.find_term(store, "quorum") is None, "no term"
    assert _event_types(store) == before, "no term_* event"
    assert _fts_row_count(store) == fts_before, "no index drift"


def test_the_same_evidence_named_twice_is_one_row_not_a_half_written_term(store, ids):
    """The other reachable non-race way ``propose`` could refuse after its
    first write: ``UNIQUE(sense_id, evidence_kind, COALESCE(anchor_id,
    ref_id))`` would have refused the second INSERT with the term and the
    sense already committed. Naming one anchor twice is a caller saying one
    thing twice, so the specs are collapsed before anything is written."""
    token = f"anchor:{ids['quote_anchor']}"
    result = _propose(store, ids, lemma="quorum", evidence=[token, token])
    assert len(result["evidence_ids"]) == 1
    assert len(api.evidence_for_sense(store, result["sense_id"])) == 1
    assert api.get_term(store, result["term_id"])["status"] == "proposed"


def test_a_scoped_decision_naming_a_missing_sense_writes_no_disambiguator(store, ids):
    """The same shape inside ``decide_relation``: the members are loaded up
    front now, so an unresolvable id refuses before the first update instead
    of after the ones it reached."""
    first, second, _rel_id = _conflicted_term(store, ids)
    opened = api.open_relation(
        store, src_kind="term", src_id=first["term_id"], dst_kind="term", dst_id=first["term_id"],
        verb="conflicts_with", marked_by_kind="system",
        evidence={"sense_ids": [first["sense_id"], second["sense_id"], "SENSE-does-not-exist"]},
    )
    with pytest.raises(SenseNotFoundError):
        api.decide_relation(
            store, opened["rel_id"], decision="scoped", by_launch=ids["launch"],
            disambiguators={
                first["sense_id"]: "x", second["sense_id"]: "y", "SENSE-does-not-exist": "z",
            },
        )
    assert api.get_sense(store, first["sense_id"])["disambiguator"] is None
    assert api.get_sense(store, second["sense_id"])["disambiguator"] is None
    assert api.get_term(store, first["term_id"])["status"] == "active"
    assert api.get_relation(store, opened["rel_id"])["status"] == "pending"


@pytest.mark.parametrize("decision", ["scoped", "not_conflict"])
def test_a_conflict_resolution_on_a_duplicate_candidate_is_refused(store, ids, decision):
    keep, other = _two_terms(store, ids)
    opened = api.open_relation(
        store, src_kind="term", src_id=other["term_id"], dst_kind="term",
        dst_id=keep["term_id"], verb="same_as", marked_by_kind="system",
    )
    with pytest.raises(InvalidDecisionError, match="resolves a conflict"):
        api.decide_relation(store, opened["rel_id"], decision=decision, by_launch=ids["launch"],
                            into="SENSE-x", disambiguators={})


def test_unrelated_confirms_the_judgment_and_moves_nothing_else(store, ids):
    keep, other = _two_terms(store, ids)
    opened = api.open_relation(
        store, src_kind="term", src_id=other["term_id"], dst_kind="term",
        dst_id=keep["term_id"], verb="same_as", marked_by_kind="system",
    )
    api.decide_relation(store, opened["rel_id"], decision="unrelated", by_launch=ids["launch"])
    rel = api.get_relation(store, opened["rel_id"])
    assert (rel["status"], rel["decided_verb"]) == ("confirmed", "unrelated")
    assert api.get_term(store, other["term_id"])["status"] == "active"


# ---------------------------------------------------------------------------
# merge_terms directly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case,match",
    [
        ("self", "into itself"),
        ("already_merged", "already merged"),
        ("into_merged", "merging into it"),
        ("unknown", "no such term"),
    ],
)
def test_a_merge_that_cannot_mean_anything_is_refused(store, ids, case, match):
    keep, other = _two_terms(store, ids)
    third = _propose(store, ids, lemma="save vs magic", gloss="a third reading",
                     evidence=[f"claim:{ids['claim']}"])

    if case == "self":
        args = (keep["term_id"], keep["term_id"])
    elif case == "already_merged":
        api.merge_terms(store, keep["term_id"], other["term_id"], by_launch=ids["launch"])
        args = (third["term_id"], other["term_id"])
    elif case == "into_merged":
        api.merge_terms(store, keep["term_id"], other["term_id"], by_launch=ids["launch"])
        args = (other["term_id"], third["term_id"])
    else:
        args = ("TERM-ghost", keep["term_id"])

    with pytest.raises((InvalidMergeError, TermNotFoundError), match=match):
        api.merge_terms(store, *args, by_launch=ids["launch"])


def test_an_alias_key_the_canonical_term_already_has_stays_where_it_is(store, ids):
    """A collision means the canonical side already has that lookup key, so
    nothing is lost by leaving the duplicate parented to the merged term --
    and deleting it would be the one destructive act a merge avoids."""
    keep = _propose(store, ids, lemma="saving throw", aliases=["save"])
    other = _propose(store, ids, lemma="defence roll", gloss="a roll to avoid an effect",
                     evidence=[f"claim:{ids['claim']}"], aliases=["save"])
    result = api.merge_terms(store, keep["term_id"], other["term_id"], by_launch=ids["launch"])

    assert result["aliases_moved"] == []
    assert len(result["aliases_left_behind"]) == 1
    left = store.knowledge.execute(
        "SELECT term_id FROM term_alias WHERE alias_id = ?", (result["aliases_left_behind"][0],)
    ).fetchone()[0]
    assert left == other["term_id"]


# ---------------------------------------------------------------------------
# conflicts_for_claim -- ruling L-C5's read
# ---------------------------------------------------------------------------


def test_conflicts_for_claim_reports_the_open_conflict_on_a_term_the_claim_backs(store, ids):
    """The conflict is not opened by hand here. Since step E2 landed
    ``lexicon.scan``, accepting the second reading raises it; a hand-opened
    one on top would be a second queue item for the same member set."""
    source_b, anchor_b = _second_source(store, ids)
    first = _propose(store, ids, lemma="pressure", origin_kind="extract", origin_ref=ids["claim"],
                     evidence=[f"claim:{ids['claim']}"],
                     procedure_version=policy.EXTRACT_PROCEDURE_VERSION)
    api.accept_sense(store, first["sense_id"], by_launch=ids["launch"])
    second = _propose(store, ids, lemma="pressure", gloss="a countdown that forces a decision",
                      evidence=[f"anchor:{anchor_b}"])
    accepted = api.accept_sense(store, second["sense_id"], by_launch=ids["launch"])
    assert accepted["conflicts"]["opened"], "the scan raises it on the second current reading"

    out = api.conflicts_for_claim(store, ids["claim"])
    assert len(out) == 1
    entry = out[0]
    assert entry["term_id"] == first["term_id"] and entry["lemma"] == "pressure"
    assert entry["status"] == "pending"
    assert entry["member_sense_ids"] == [first["sense_id"], second["sense_id"]]
    assert entry["senses"][0]["source_keys"] == [ids["source"]]
    assert entry["senses"][1]["source_keys"] == [source_b]


def test_conflicts_for_claim_is_empty_for_a_claim_no_term_stands_on(store, ids):
    _propose(store, ids)
    assert api.conflicts_for_claim(store, ids["claim"]) == []


def test_conflicts_for_claim_writes_nothing(store, ids):
    _propose(store, ids, origin_kind="extract", origin_ref=ids["claim"],
             evidence=[f"claim:{ids['claim']}"], procedure_version=policy.EXTRACT_PROCEDURE_VERSION)
    before = _event_types(store)
    api.conflicts_for_claim(store, ids["claim"])
    assert _event_types(store) == before


def test_conflicts_for_claim_raises_rather_than_reporting_no_conflicts_on_an_unmigrated_store():
    """Ruling L-C5's contract with the caller. On a store that never ran the
    v5 migration the tables are genuinely absent; the Evidence panel's own
    guard catches ``OperationalError`` and OMITS the region with a stated
    reason. Swallowing it here would return ``[]``, which renders as
    "nothing argues with this claim" -- a different, and false, statement."""
    import sqlite3 as _sqlite3

    from trialerror.stores.migrate import apply_migrations
    from trialerror.stores.schema import knowledge

    conn = _sqlite3.connect(":memory:")
    conn.row_factory = _sqlite3.Row
    apply_migrations(conn, tuple(m for m in knowledge.MIGRATIONS if m.version <= 4))

    class _PreV5Store:
        def conn_for_table(self, table):
            return conn

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        api.conflicts_for_claim(_PreV5Store(), "CLM-1")


# ---------------------------------------------------------------------------
# the E2 seams, the FTS index, and the event ledger
# ---------------------------------------------------------------------------


def test_the_candidate_and_conflict_hooks_answer_rather_than_no_op(store, ids):
    """Not silently no-ops: a caller reading the result can tell "no
    candidates were found" from "nothing looked", which is what the
    never-silent-auto-merge posture is about.

    Step E2 landed ``lexicon.candidates`` and ``lexicon.scan``, so both
    hooks now report ``ok`` with an answer in it -- and an answer of "I
    looked and found nothing", which is the state that used to be
    indistinguishable from not looking."""
    proposed = _propose(store, ids)
    assert proposed["candidates"]["status"] == "ok"
    assert proposed["candidates"]["candidates"] == [], "nothing else in the store looks like it"
    accepted = api.accept_sense(store, proposed["sense_id"], by_launch=ids["launch"])
    assert accepted["conflicts"]["status"] == "ok"
    assert accepted["conflicts"]["opened"] is None, "one reading is not a disagreement"


def test_the_index_carries_one_row_per_term_plus_one_per_alias(store, ids):
    """The shape that makes ``term_fts_in_sync`` a plain count comparison
    rather than a text diff."""
    api.reindex_all(store)  # the fixture inserts its own rows without the API
    _propose(store, ids, aliases=["turn order", "init order"])

    terms = store.knowledge.execute("SELECT count(*) FROM term").fetchone()[0]
    aliases = store.knowledge.execute("SELECT count(*) FROM term_alias").fetchone()[0]
    assert _fts_row_count(store) == terms + aliases


def test_the_index_finds_a_term_by_a_fragment_of_its_gloss_once_it_is_current(store, ids):
    proposed = _propose(store, ids)
    api.accept_sense(store, proposed["sense_id"], by_launch=ids["launch"])
    hits = store.knowledge.execute(
        "SELECT term_id FROM term_fts WHERE term_fts MATCH 'sequen'"
    ).fetchall()
    assert proposed["term_id"] in {r[0] for r in hits}


def test_a_rejected_reading_leaves_the_index_showing_only_the_lemma(store, ids):
    proposed = _propose(store, ids)
    api.accept_sense(store, proposed["sense_id"], by_launch=ids["launch"])
    api.supersede_sense(store, proposed["sense_id"], gloss="a wholly different wording here",
                        by_launch=ids["launch"])
    text = store.knowledge.execute(
        "SELECT text FROM term_fts WHERE term_id = ? LIMIT 1", (proposed["term_id"],)
    ).fetchone()[0]
    assert "wholly different wording" in text
    assert "sequence in which actors" not in text, "only CURRENT glosses are indexed"


def test_reindex_all_rebuilds_from_scratch_and_reports_what_it_wrote(store, ids):
    _propose(store, ids, aliases=["turn order"])
    store.knowledge.execute("DELETE FROM term_fts")
    store.knowledge.commit()
    assert _fts_row_count(store) == 0

    report = api.reindex_all(store)
    terms = store.knowledge.execute("SELECT count(*) FROM term").fetchone()[0]
    aliases = store.knowledge.execute("SELECT count(*) FROM term_alias").fetchone()[0]
    assert report == {"terms": terms, "rows": terms + aliases}
    assert _fts_row_count(store) == terms + aliases


def test_every_head_change_leaves_an_event_behind(store, ids):
    """The head layer is the only thing that updates in place, so it has to
    be rebuildable from the log. One event per change, type-keyed."""
    first, second, rel_id = _conflicted_term(store, ids)
    api.decide_relation(
        store, rel_id, decision="scoped", by_launch=ids["launch"],
        disambiguators={first["sense_id"]: "a", second["sense_id"]: "b"},
    )
    api.mark_reviewed(store, first["sense_id"], by_launch=ids["launch"])
    api.supersede_sense(store, second["sense_id"], gloss="a corrected reading", by_launch=ids["launch"])
    keep, other = _two_terms(store, ids)
    api.merge_terms(store, keep["term_id"], other["term_id"], by_launch=ids["launch"])
    lone = _propose(store, ids, lemma="tempo", gloss="how fast play moves",
                    evidence=[f"record:{ids['record']}"])
    api.accept_sense(store, lone["sense_id"], by_launch=ids["launch"])
    api.retire_sense(store, lone["sense_id"], by_launch=ids["launch"])
    rejected = _propose(store, ids, lemma="beat", gloss="a unit of pacing",
                        evidence=[f"idea:{ids['idea']}"])
    api.reject_sense(store, rejected["sense_id"], by_launch=ids["launch"])

    types = set(_event_types(store))
    assert {
        "term_proposed",
        "term_sense_proposed",
        "term_sense_accepted",
        "term_sense_rejected",
        "term_sense_superseded",
        "term_sense_retired",
        "term_relation_opened",
        "term_relation_decided",
        "term_merged",
        "term_split",
        "term_reviewed",
    } <= types


def test_every_event_a_launch_made_is_attributed_to_it(store, ids):
    proposed = _propose(store, ids)
    api.accept_sense(store, proposed["sense_id"], by_launch=ids["launch"])
    rows = store.ops.execute(
        "SELECT type, launch_id FROM event WHERE type LIKE 'term%'"
    ).fetchall()
    assert rows
    for row in rows:
        assert row["launch_id"] == ids["launch"], row["type"]


def test_a_machine_opened_relation_leaves_an_unattributed_event(store, ids):
    """``event.launch_id`` is nullable, and a system scan genuinely has no
    launch to name -- an event that borrowed one would misreport who
    judged."""
    api.open_relation(
        store, src_kind="term", src_id="TERM-a", dst_kind="term", dst_id="TERM-b",
        verb="same_as", marked_by_kind="system",
    )
    row = store.ops.execute(
        "SELECT launch_id FROM event WHERE type = 'term_relation_opened'"
    ).fetchone()
    assert row["launch_id"] is None


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def test_relations_for_term_finds_both_sides_and_filters_by_status(store, ids):
    keep, other = _two_terms(store, ids)
    pending = api.open_relation(
        store, src_kind="term", src_id=other["term_id"], dst_kind="term",
        dst_id=keep["term_id"], verb="same_as", marked_by_kind="system",
    )
    decided = api.open_relation(
        store, src_kind="term", src_id=keep["term_id"], dst_kind="term",
        dst_id=other["term_id"], verb="unrelated", marked_by_kind="system",
    )
    api.decide_relation(store, decided["rel_id"], decision="rejected", by_launch=ids["launch"])

    both = api.relations_for_term(store, keep["term_id"])
    assert {r["rel_id"] for r in both} == {pending["rel_id"], decided["rel_id"]}
    only_pending = api.relations_for_term(store, keep["term_id"], statuses=("pending",))
    assert [r["rel_id"] for r in only_pending] == [pending["rel_id"]]


def test_senses_for_term_filters_by_status(store, ids):
    first = _propose(store, ids)
    api.accept_sense(store, first["sense_id"], by_launch=ids["launch"])
    second = _propose(store, ids, gloss="a second reading", evidence=[f"claim:{ids['claim']}"])

    assert len(api.senses_for_term(store, first["term_id"])) == 2
    current = api.senses_for_term(store, first["term_id"], statuses=("current",))
    assert [s["sense_id"] for s in current] == [first["sense_id"]]
    assert second["sense_id"] not in {s["sense_id"] for s in current}


def test_find_term_normalizes_before_it_looks(store, ids):
    result = _propose(store, ids, lemma="Re–Roll", gloss="rolling a die again")
    assert api.find_term(store, "re-roll")["term_id"] == result["term_id"]
    assert api.find_term(store, "  RE-ROLL  ")["term_id"] == result["term_id"]
    assert api.find_term(store, "rerolls") is None, "a plural is an alias decision, not a lookup"
    assert api.find_term(store, "   ") is None


def test_a_typeset_spelling_attaches_to_the_term_the_typed_one_made(store, ids):
    """Fix pass, finding F4, measured at the store rather than in the
    normalizer: before the fix these were two terms whose lemmas render
    identically, ``find_term`` bridged neither, and the duplicate scan opened
    nothing to queue -- so the split was permanent, unqueued and invisible."""
    typed = _propose(store, ids, lemma="player's turn", gloss="the window in which one actor acts")
    typeset = _propose(
        store,
        ids,
        # exactly what a PDF extractor emits: a curly possessive and a soft
        # hyphen left behind by a line break.
        lemma="player\u2019s tu\u00adrn",
        gloss="a second reading of the same name, written as a typesetter writes it",
    )
    assert typeset["term_id"] == typed["term_id"]
    assert typeset["created_term"] is False
    assert api.find_term(store, "player\u2019s turn")["term_id"] == typed["term_id"]
    assert api.find_term(store, "player\u200bs turn") is None, (
        "a zero-width space is invisible, not a word break: it disappears, and "
        "'players turn' is a different name"
    )


def test_a_lemma_of_nothing_but_invisible_characters_is_refused(store, ids):
    """The rider on F4. ``norm_lemma`` used to return a truthy string of
    zero-width characters, so this guard passed and a term whose every
    rendered cell is blank could be created through the public API."""
    invisible = "\u200b\u200c\ufeff"
    with pytest.raises(InvalidTermInputError, match="empty key"):
        _propose(store, ids, lemma=invisible, gloss="a reading of nothing")
    assert api.find_term(store, invisible) is None


# ---------------------------------------------------------------------------
# the gloss cap, per route (build step 1c, decision D5)
# ---------------------------------------------------------------------------


def _long_gloss(words: int) -> str:
    return " ".join(["word"] * words)


def test_the_import_route_keeps_a_longer_reading_whole(store, ids):
    """The first real register import refused 271 of 7,475 rows for being
    82-93 words long -- rows dropped for faithfully carrying what the
    register said. An imported gloss is somebody else's reading and the
    program has no business rewriting it, so the import route measures
    against its own cap while everything else keeps the strict one."""
    long_gloss = _long_gloss(policy.GLOSS_MAX_WORDS + 20)

    with pytest.raises(GlossTooLongError, match="cap is 80"):
        _propose(store, ids, gloss=long_gloss)

    imported = _propose(
        store,
        ids,
        lemma="an imported reading",
        gloss=long_gloss,
        origin_kind="record_import",
        origin_ref=ids["record"],
        evidence=[f"record:{ids['record']}"],
        procedure_version=policy.RECORD_IMPORT_PROCEDURE_VERSION,
    )
    assert imported["created_sense"] is True


def test_the_import_cap_still_refuses_a_section(store, ids):
    """160 words, not "no cap": the cap keeps the job it was written for on
    this route too -- catching the payload that is not a gloss at all."""
    with pytest.raises(GlossTooLongError, match="cap is 160"):
        _propose(
            store,
            ids,
            lemma="a whole section",
            gloss=_long_gloss(policy.GLOSS_MAX_WORDS_IMPORT + 1),
            origin_kind="record_import",
            origin_ref=ids["record"],
            evidence=[f"record:{ids['record']}"],
            procedure_version=policy.RECORD_IMPORT_PROCEDURE_VERSION,
        )


def test_a_correction_to_an_imported_reading_is_the_programs_own_words(store, ids):
    """The cap keys on the ROUTE, not on the row's history. Superseding an
    imported sense means a launch is writing a gloss, and a gloss somebody
    writes is capped at 80 however the reading it replaces arrived."""
    imported = _propose(
        store,
        ids,
        lemma="an imported reading",
        gloss=_long_gloss(policy.GLOSS_MAX_WORDS + 20),
        origin_kind="record_import",
        origin_ref=ids["record"],
        evidence=[f"record:{ids['record']}"],
        procedure_version=policy.RECORD_IMPORT_PROCEDURE_VERSION,
        status="current",
    )
    with pytest.raises(GlossTooLongError, match="cap is 80"):
        api.supersede_sense(
            store,
            imported["sense_id"],
            gloss=_long_gloss(policy.GLOSS_MAX_WORDS + 1),
            by_launch=ids["launch"],
        )


def test_the_import_cap_is_configurable_but_never_stricter_than_the_other(store, ids):
    """A program that tightens the hand-written cap must not thereby start
    refusing register rows that a hand-written gloss of the same length
    would sail through -- so the import cap is the larger of the two, and
    configuring it downward alone is a no-op."""
    assert policy.load_policy({"lexicon": {"gloss_max_words_import": 10}})["gloss_max_words_import"] == 80
    assert policy.load_policy({"lexicon": {"gloss_max_words": 200}})["gloss_max_words_import"] == 200
    assert policy.load_policy({"lexicon": {"gloss_max_words_import": 240}})["gloss_max_words_import"] == 240

    tight = {"lexicon": {"gloss_max_words_import": 90}}
    with pytest.raises(GlossTooLongError, match="cap is 90"):
        _propose(
            store,
            ids,
            lemma="an imported reading",
            gloss=_long_gloss(91),
            origin_kind="record_import",
            origin_ref=ids["record"],
            evidence=[f"record:{ids['record']}"],
            procedure_version=policy.RECORD_IMPORT_PROCEDURE_VERSION,
            config=tight,
        )
