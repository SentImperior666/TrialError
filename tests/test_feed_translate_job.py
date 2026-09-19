"""lane-b-translator: the translator's STORAGE and JOB paths end to end
(:mod:`trialerror.feed_translate.api` / ``.handlers`` / ``.backends``),
driven through ``trialerror.jobs.worker.run_one`` -- the same
claim-run-settle loop a real detached worker uses, exactly as
``tests/test_summarize_handlers.py`` drives the summarize handler.

Nothing here reaches a network: the only backend that produces text is
:class:`~trialerror.feed_translate.backends.FakeTranslatorBackend`, and the
default backend produces none at all.
"""

from __future__ import annotations

import json

import pytest

from trialerror.feed_translate.api import (
    TRANSLATION_INSTRUCTION,
    build_translation_envelope,
    count_gate_failures,
    find_untranslated_posts,
    get_translation,
    list_translations,
    store_translation,
)
from trialerror.feed_translate.backends import (
    FakeTranslatorBackend,
    ModelTranslatorBackend,
    PendingTranslatorBackend,
    load_translator_backend,
)
from trialerror.feed_translate.errors import PostNotFoundError, TranslatorBackendError
from trialerror.feed_translate.gate import run_translation_gate
from trialerror.jobs import ledger
from trialerror.jobs.worker import run_one

from tests._feed_translate_fixtures import (
    DENSE_POST,
    FAITHFUL_TRANSLATION,
    UNFAITHFUL_TRANSLATION,
    build_feed,
)


def _run(store, job_id: str, payload: dict) -> dict:
    result = run_one(store, job_id=job_id, kind="custom", payload=payload)
    job = ledger.get_job(store, job_id)
    return {"result": result, "job": job, "checkpoint": json.loads(job["checkpoint"] or "{}")}


def _write_config(program_root, table: str) -> None:
    (program_root / "trialerror.toml").write_text(
        '[program]\nid = "PROG-test"\n\n' + table, encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# envelope + storage
# ---------------------------------------------------------------------------


def test_envelope_carries_the_body_the_mode_and_the_style_contract(store):
    feed = build_feed(store)
    envelope = build_translation_envelope(store, post_id=feed["post_ids"][0])
    assert envelope["kind"] == "feed_translate"
    assert envelope["original_body"] == DENSE_POST
    assert envelope["style_mode"] == "flavored"
    assert envelope["glossary_hint_terms"] == []  # no glossary exists yet (design 4.3.4)
    assert envelope["instruction"] == TRANSLATION_INSTRUCTION
    # the fidelity half of the contract travels with every envelope
    assert "Never promote a hedge to a fact" in envelope["instruction"]


def test_envelope_refuses_an_unknown_post(store):
    build_feed(store)
    with pytest.raises(PostNotFoundError):
        build_translation_envelope(store, post_id="POST-nope")


def test_store_translation_never_touches_the_original_post(store):
    feed = build_feed(store)
    post_id = feed["post_ids"][0]
    before = dict(store.ops.execute("SELECT * FROM feed_post WHERE post_id = ?", (post_id,)).fetchone())

    envelope = build_translation_envelope(store, post_id=post_id)
    gate = run_translation_gate(
        None, post_id=post_id, original_body=DENSE_POST, translation_body=FAITHFUL_TRANSLATION
    )
    store_translation(store, envelope=envelope, body=FAITHFUL_TRANSLATION, gate=gate.as_row())

    after = dict(store.ops.execute("SELECT * FROM feed_post WHERE post_id = ?", (post_id,)).fetchone())
    assert after == before  # author, body, launch_id, ts -- all untouched


def test_a_retranslation_supersedes_rather_than_overwriting(store):
    feed = build_feed(store)
    post_id = feed["post_ids"][0]
    envelope = build_translation_envelope(store, post_id=post_id)
    first = store_translation(store, envelope=envelope, body=FAITHFUL_TRANSLATION, gate={"gate_status": "pass"})
    second = store_translation(
        store, envelope=envelope, body=FAITHFUL_TRANSLATION + " Extra line.", gate={"gate_status": "pass"}
    )

    assert second["supersedes"] == first["translation_id"]
    current = get_translation(store, post_id=post_id)
    assert current["translation_id"] == second["translation_id"]
    rows = {r["translation_id"]: r["status"] for r in list_translations(store, post_id=post_id)}
    assert rows[first["translation_id"]] == "superseded"
    assert rows[second["translation_id"]] == "current"


def test_store_translation_defaults_to_ungated_when_no_verdict_is_supplied(store):
    feed = build_feed(store)
    envelope = build_translation_envelope(store, post_id=feed["post_ids"][0])
    row = store_translation(store, envelope=envelope, body=FAITHFUL_TRANSLATION)
    assert row["gate_status"] == "ungated"
    assert row["gate_reasons"] is None


def test_find_untranslated_posts_scopes_by_thread_and_by_version(store):
    feed = build_feed(store, bodies=[DENSE_POST, "A second, shorter post with no ids."])
    a, b = feed["post_ids"]
    assert find_untranslated_posts(store, thread_id=feed["thread_id"]) == [a, b]

    envelope = build_translation_envelope(store, post_id=a)
    store_translation(store, envelope=envelope, body=FAITHFUL_TRANSLATION, gate={"gate_status": "pass"})
    assert find_untranslated_posts(store, thread_id=feed["thread_id"]) == [b]
    # a version bump makes every existing translation invisible to the sweep
    assert find_untranslated_posts(store, thread_id=feed["thread_id"], translator_version="2") == [a, b]


# ---------------------------------------------------------------------------
# backends
# ---------------------------------------------------------------------------


def test_load_translator_backend_defaults_to_pending():
    assert isinstance(load_translator_backend(None), PendingTranslatorBackend)
    assert isinstance(load_translator_backend({}), PendingTranslatorBackend)


def test_load_translator_backend_reads_the_configured_name_and_model():
    assert isinstance(load_translator_backend({"backend": "fake"}), FakeTranslatorBackend)
    model = load_translator_backend({"backend": "model", "model": "claude-opus-5"})
    assert isinstance(model, ModelTranslatorBackend)
    assert model.model == "claude-opus-5"
    assert model.requires_booking is True


def test_load_translator_backend_refuses_an_unknown_name():
    with pytest.raises(TranslatorBackendError, match="unknown"):
        load_translator_backend({"backend": "gpt-in-a-box"})


def test_the_model_backend_refuses_rather_than_silently_parking():
    with pytest.raises(TranslatorBackendError, match="no generation driver"):
        ModelTranslatorBackend().translate({"original_body": "x"})


def test_the_fake_backend_output_passes_the_style_contract():
    body = FakeTranslatorBackend().translate({"original_body": DENSE_POST})
    gate = run_translation_gate(None, post_id="P", original_body=DENSE_POST, translation_body=body)
    assert gate.passed, gate.reasons
    assert ";" not in body  # rule 4: the fake splits semicolons


# ---------------------------------------------------------------------------
# the job path
# ---------------------------------------------------------------------------


def test_the_default_backend_parks_a_pending_envelope_and_stores_nothing(store):
    feed = build_feed(store)
    out = _run(store, "JOB-xl-1", {"handler": "feed_translate", "thread_id": feed["thread_id"]})
    assert out["result"]["status"] == "complete"
    assert get_translation(store, post_id=feed["post_ids"][0]) is None
    assert out["checkpoint"]["written"] == {}
    assert len(out["checkpoint"]["pending_envelopes"]) == 1
    assert out["checkpoint"]["pending_envelopes"][0]["post_id"] == feed["post_ids"][0]


def test_the_fake_backend_translates_the_whole_thread_end_to_end(store, program_root):
    feed = build_feed(store, bodies=[DENSE_POST, "Gate G-2 is pending review."])
    _write_config(program_root, '[feed.translator]\nbackend = "fake"\n')

    out = _run(store, "JOB-xl-2", {"handler": "feed_translate", "thread_id": feed["thread_id"]})
    assert out["job"]["state"] == "complete"
    assert set(out["checkpoint"]["written"]) == set(feed["post_ids"])
    assert out["checkpoint"]["withheld"] == {}

    for post_id in feed["post_ids"]:
        row = get_translation(store, post_id=post_id)
        assert row is not None
        assert row["gate_status"] == "pass"
        assert row["body"]
    # idempotent: a second sweep finds nothing left to do
    again = _run(store, "JOB-xl-3", {"handler": "feed_translate", "thread_id": feed["thread_id"]})
    assert again["checkpoint"]["written"] == {}


def test_a_judgment_supplied_out_of_band_is_still_gated_and_withheld_when_it_fails(store):
    feed = build_feed(store)
    post_id = feed["post_ids"][0]
    out = _run(
        store,
        "JOB-xl-4",
        {
            "handler": "feed_translate",
            "post_ids": [post_id],
            "judgments": {post_id: UNFAITHFUL_TRANSLATION},
        },
    )
    assert out["job"]["state"] == "complete"
    assert out["checkpoint"]["written"] == {}
    assert post_id in out["checkpoint"]["withheld"]

    row = get_translation(store, post_id=post_id)
    assert row["gate_status"] == "fail"  # stored, so it can be counted and read...
    reasons = json.loads(row["gate_reasons"])
    assert reasons["passed"] is False
    assert reasons["reasons"]
    assert count_gate_failures(store) == 1


def test_a_faithful_judgment_supplied_out_of_band_passes(store):
    feed = build_feed(store)
    post_id = feed["post_ids"][0]
    out = _run(
        store,
        "JOB-xl-5",
        {
            "handler": "feed_translate",
            "post_ids": [post_id],
            "judgments": {post_id: FAITHFUL_TRANSLATION},
            "created_by_launch": feed["launch_id"],
        },
    )
    assert post_id in out["checkpoint"]["written"]
    row = get_translation(store, post_id=post_id)
    assert row["gate_status"] == "pass"
    assert row["body"] == FAITHFUL_TRANSLATION
    assert row["created_by_launch"] == feed["launch_id"]
    assert count_gate_failures(store) == 0


def test_a_budget_spending_backend_refuses_to_run_without_a_booked_launch(store, program_root):
    feed = build_feed(store)
    _write_config(program_root, '[feed.translator]\nbackend = "model"\n')
    out = _run(store, "JOB-xl-6", {"handler": "feed_translate", "post_ids": feed["post_ids"]})
    assert out["result"]["status"] == "failed"
    assert "created_by_launch" in (out["job"]["last_error"] or "")
    assert out["job"]["failure_class"] == "logic"  # a wrong payload, not a flaky environment
    assert get_translation(store, post_id=feed["post_ids"][0]) is None


# ---------------------------------------------------------------------------
# FT-2 (fix pass): the budget guard consumes the booking, not just checks
# that the launch_id names SOME row.
# ---------------------------------------------------------------------------


def _launch_state(store, launch_id: str) -> str:
    row = store.platform.execute("SELECT state FROM launch WHERE launch_id = ?", (launch_id,)).fetchone()
    return row["state"]


def test_an_unknown_launch_id_is_refused(store, program_root):
    feed = build_feed(store)
    _write_config(program_root, '[feed.translator]\nbackend = "model"\n')
    out = _run(
        store, "JOB-xl-ft2-1",
        {"handler": "feed_translate", "post_ids": feed["post_ids"], "created_by_launch": "LNCH-does-not-exist"},
    )
    assert out["result"]["status"] == "failed"
    assert "does not name a row" in (out["job"]["last_error"] or "")


def test_a_refused_booking_is_rejected_even_though_the_row_exists(store, program_root):
    """``book_launch`` inserts a row for a REFUSED (over-cap) booking too --
    row-existence alone was never evidence of an affordable booking."""
    feed = build_feed(store)
    with store.platform:
        store.platform.execute("UPDATE launch SET state = 'REFUSED' WHERE launch_id = ?", (feed["launch_id"],))
    _write_config(program_root, '[feed.translator]\nbackend = "model"\n')
    out = _run(
        store, "JOB-xl-ft2-2",
        {"handler": "feed_translate", "post_ids": feed["post_ids"], "created_by_launch": feed["launch_id"]},
    )
    assert out["result"]["status"] == "failed"
    error = out["job"]["last_error"] or ""
    assert "not PROVISIONAL" in error
    assert "no generation driver" not in error  # refused before the backend was ever called
    assert _launch_state(store, feed["launch_id"]) == "REFUSED"  # untouched, not silently flipped


def test_a_provisional_booking_is_consumed_before_the_backend_is_called(store, program_root):
    feed = build_feed(store)
    assert _launch_state(store, feed["launch_id"]) == "PROVISIONAL"
    _write_config(program_root, '[feed.translator]\nbackend = "model"\n')
    out = _run(
        store, "JOB-xl-ft2-3",
        {"handler": "feed_translate", "post_ids": feed["post_ids"], "created_by_launch": feed["launch_id"]},
    )
    # the booking check now PASSES (it reaches the backend, which then
    # fails for the documented reason: no generation driver ships) --
    # proof the earlier refusal came from the booking check, not the backend.
    assert out["result"]["status"] == "failed"
    assert "no generation driver" in (out["job"]["last_error"] or "")
    assert _launch_state(store, feed["launch_id"]) == "RUNNING"  # consumed


def test_a_consumed_booking_cannot_be_reused_by_a_second_job_run(store, program_root):
    feed = build_feed(store)
    _write_config(program_root, '[feed.translator]\nbackend = "model"\n')
    first = _run(
        store, "JOB-xl-ft2-4a",
        {"handler": "feed_translate", "post_ids": feed["post_ids"], "created_by_launch": feed["launch_id"]},
    )
    assert "no generation driver" in (first["job"]["last_error"] or "")
    assert _launch_state(store, feed["launch_id"]) == "RUNNING"

    second = _run(
        store, "JOB-xl-ft2-4b",
        {"handler": "feed_translate", "post_ids": feed["post_ids"], "created_by_launch": feed["launch_id"]},
    )
    error = second["job"]["last_error"] or ""
    assert "one booking = one job run" in error
    assert "no generation driver" not in error  # refused before the backend was tried again


def test_an_expired_booking_ttl_is_refused(store, program_root):
    feed = build_feed(store)
    with store.platform:
        store.platform.execute(
            "UPDATE launch SET booked_ts = '2000-01-01T00:00:00.000Z', booking_ttl_s = 1 WHERE launch_id = ?",
            (feed["launch_id"],),
        )
    _write_config(program_root, '[feed.translator]\nbackend = "model"\n')
    out = _run(
        store, "JOB-xl-ft2-5",
        {"handler": "feed_translate", "post_ids": feed["post_ids"], "created_by_launch": feed["launch_id"]},
    )
    assert "TTL" in (out["job"]["last_error"] or "") and "expired" in out["job"]["last_error"]
    assert _launch_state(store, feed["launch_id"]) == "PROVISIONAL"  # never consumed


# ---------------------------------------------------------------------------
# FT-1 (fix pass): the judged (meaning-level) faithfulness tier is
# reachable from the JOB path via claim_decomposition/claim_judgments.
# ---------------------------------------------------------------------------

#: Keeps every id/number/date/hedge the fidelity tier checks -- but
#: NEGATES the claim. Tier 1 alone cannot see this (nothing numeric was
#: dropped or invented); this is exactly the gap FT-1 demonstrated.
NEGATED_TRANSLATION = (
    "We did NOT book LNCH-01JXYZ4 against pool P-2 on 2026-09-05. "
    "The match-up is still pending. "
    "So the 3 gates after it stay deferred."
)


def test_the_negation_passes_tier_1_alone_the_documented_gap(store):
    """Sanity check for the scenario below: without any judge, the fail-
    closed gate's lexical tier does not catch a meaning-inverting rewrite
    that keeps every id/number/date/hedge verbatim."""
    feed = build_feed(store)
    post_id = feed["post_ids"][0]
    out = _run(
        store, "JOB-xl-ft1-0",
        {"handler": "feed_translate", "post_ids": [post_id], "judgments": {post_id: NEGATED_TRANSLATION}},
    )
    assert post_id in out["checkpoint"]["written"]
    assert get_translation(store, post_id=post_id)["gate_status"] == "pass"


def test_claim_tables_in_the_payload_reach_the_judged_tier_and_catch_the_negation(store):
    """The same negation, this time with claim_decomposition/claim_judgments
    supplied in the job payload -- reconstituted into judge callables
    INSIDE the worker (judge_from_claim_table), never carried through as
    callables themselves. The judged tier says the negation is
    unsupported, and the gate now fails closed."""
    feed = build_feed(store)
    post_id = feed["post_ids"][0]
    sentences = [
        "We did NOT book LNCH-01JXYZ4 against pool P-2 on 2026-09-05.",
        "The match-up is still pending.",
        "So the 3 gates after it stay deferred.",
    ]
    claim_decomposition = {
        f"{post_id}::S-{i + 1}": {"claims": [sentence]} for i, sentence in enumerate(sentences)
    }
    claim_judgments = {
        f"{post_id}::S-1::CLM-1": {"label": "unsupported", "note": "original says the booking DID happen"},
        f"{post_id}::S-2::CLM-1": {"label": "supported"},
        f"{post_id}::S-3::CLM-1": {"label": "supported"},
    }
    out = _run(
        store, "JOB-xl-ft1-1",
        {
            "handler": "feed_translate",
            "post_ids": [post_id],
            "judgments": {post_id: NEGATED_TRANSLATION},
            "claim_decomposition": claim_decomposition,
            "claim_judgments": claim_judgments,
            "created_by_launch": feed["launch_id"],
        },
    )
    assert post_id in out["checkpoint"]["withheld"]
    row = get_translation(store, post_id=post_id)
    assert row["gate_status"] == "fail"
    assert row["faithfulness_score"] < 1.0
    reasons = json.loads(row["gate_reasons"])
    assert reasons["judged"] is True
    assert count_gate_failures(store) == 1


def test_a_missing_claim_judgment_is_a_named_refusal_not_a_silent_pass(store):
    feed = build_feed(store)
    post_id = feed["post_ids"][0]
    claim_decomposition = {f"{post_id}::S-1": {"claims": ["a claim"]}}
    out = _run(
        store, "JOB-xl-ft1-2",
        {
            "handler": "feed_translate",
            "post_ids": [post_id],
            "judgments": {post_id: "A short claim."},
            "claim_decomposition": claim_decomposition,
            # non-empty, but no entry for the one claim pair id above -- an
            # EMPTY table would be falsy and just turn the judged tier off
            # entirely (see judge_from_claim_table's "if claim_judgments"
            # guard in the handler), which is a different case from this one.
            "claim_judgments": {"POST-unrelated::S-1::CLM-1": {"label": "supported"}},
        },
    )
    # a missing judgment is a named LOGIC failure of the whole job run --
    # never a silent pass, and never a crashed worker process.
    assert out["result"]["status"] == "failed"
    assert out["job"]["failure_class"] == "logic"
    assert "ClaimJudgmentMissingError" in (out["job"]["last_error"] or "")
    assert get_translation(store, post_id=post_id) is None  # nothing stored either


def test_a_missing_post_is_skipped_not_fatal(store, program_root):
    feed = build_feed(store)
    _write_config(program_root, '[feed.translator]\nbackend = "fake"\n')
    out = _run(
        store, "JOB-xl-7", {"handler": "feed_translate", "post_ids": ["POST-nope", feed["post_ids"][0]]}
    )
    assert out["job"]["state"] == "complete"
    assert [s["post_id"] for s in out["checkpoint"]["skipped"]] == ["POST-nope"]
    assert feed["post_ids"][0] in out["checkpoint"]["written"]


def test_strict_style_config_withholds_a_register_level_break(store, program_root):
    feed = build_feed(store, bodies=["The job completed."])
    _write_config(program_root, '[feed.translator]\nbackend = "pending"\nstrict_style = true\n')
    post_id = feed["post_ids"][0]
    out = _run(
        store,
        "JOB-xl-8",
        {
            "handler": "feed_translate",
            "post_ids": [post_id],
            "judgments": {post_id: "The job completed. Hope this helps."},
        },
    )
    assert post_id in out["checkpoint"]["withheld"]
    reasons = json.loads(get_translation(store, post_id=post_id)["gate_reasons"])
    assert any(r.startswith("[strict_style]") for r in reasons["reasons"])
