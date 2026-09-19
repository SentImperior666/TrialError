"""Lane FB-7 item 8: the two FB-6 items that were deferred.

(a) ``lens intake`` fills the per-model idea-vector cache. FB-6 item 1 made
the cache fill at SCREEN time, which is the first moment a vector is needed;
intake is the first moment one can EXIST, and it is also the moment the
operator is sitting there. The screen-time fill stays as the fallback, so
this is an optimisation and is treated as one: a parked or absent backend
warns and the records still land.

(b) ``extra`` on a round's OWN records. FB-6 item 6 gave a PLANT a free
block that reaches the judge as one text field; a round's records had
nowhere to put the same keys, so the text was folded into the statement by
hand, in a different place for each record. The envelope's shape is what
keeps a plant indistinguishable from a record, which cuts both ways.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest

from tests._inventory_fixtures import bootstrap_launch, build_corpus_with_inventory
from trialerror.cli import main
from trialerror.lens.ideas import intake_records, read_idea, write_idea
from trialerror.lens.novelty import (
    IDEA_VECTOR_TABLE,
    build_verifier_envelope,
    fetch_idea_vectors,
    fill_idea_vector_cache,
    render_extra_text,
)

ROUND = "round-intake-extra"


def _record(statement, **extra_fields):
    return {
        "statement": statement,
        "probe": "Check the transition against one inventory row.",
        "provenance": {"docs": ["DOC-x"]},
        **extra_fields,
    }


def _cached_ids(store):
    return {
        r["idea_id"]
        for r in store.knowledge.execute(f"SELECT idea_id FROM {IDEA_VECTOR_TABLE}").fetchall()
    }


# ---------------------------------------------------------------------------
# (a) the cache fill
# ---------------------------------------------------------------------------


@pytest.fixture()
def corpus(store):
    built = build_corpus_with_inventory(store)
    return built, bootstrap_launch(store, purpose="ideation")


def test_intake_fills_the_cache_and_the_next_read_embeds_nothing(store, corpus):
    _built, launch = corpus
    rows = intake_records(
        store, round_id=ROUND,
        records=[_record("A first record."), _record("A second record.")],
        author_launch=launch,
    )
    ids = [r["idea_id"] for r in rows]
    assert _cached_ids(store) == set()

    filled = fill_idea_vector_cache(store, idea_ids=ids)
    assert filled["warnings"] == []
    assert filled["n_embedded"] == 2
    assert filled["n_cached"] == 0
    assert _cached_ids(store) == set(ids)

    # The second call is a pure cache hit: nothing is re-embedded.
    again = fill_idea_vector_cache(store, idea_ids=ids)
    assert again["n_embedded"] == 0
    assert again["n_cached"] == 2


def test_the_cached_vector_is_the_one_a_screen_would_have_computed(store, corpus):
    _built, launch = corpus
    rows = intake_records(store, round_id=ROUND, records=[_record("A record.")], author_launch=launch)
    ids = [r["idea_id"] for r in rows]
    filled = fill_idea_vector_cache(store, idea_ids=ids)
    cached = fetch_idea_vectors(store, model_key=filled["model_key"], idea_ids=ids)
    assert set(cached) == set(ids)
    assert cached[ids[0]]["vector"]
    from trialerror.lens.novelty import statement_digest

    assert cached[ids[0]]["statement_sha256"] == statement_digest("A record.")


def test_a_parked_backend_is_a_warning_and_never_a_lost_intake(store, corpus, monkeypatch):
    _built, launch = corpus
    rows = intake_records(store, round_id=ROUND, records=[_record("A record.")], author_launch=launch)
    import trialerror.retrieve.engine as engine_mod

    def _refuse(*a, **k):
        raise RuntimeError("the embedding backend is parked")

    monkeypatch.setattr(engine_mod, "_resolve_embed_backend", _refuse)
    filled = fill_idea_vector_cache(store, idea_ids=[r["idea_id"] for r in rows])
    assert filled["n_embedded"] == 0
    assert len(filled["warnings"]) == 1
    assert "parked" in filled["warnings"][0]
    assert "screen" in filled["warnings"][0]
    # ...and the records are still there.
    assert read_idea(store, idea_id=rows[0]["idea_id"]) is not None


def test_a_backend_that_refuses_mid_embed_is_also_only_a_warning(store, corpus, monkeypatch):
    _built, launch = corpus
    rows = intake_records(store, round_id=ROUND, records=[_record("A record.")], author_launch=launch)
    import trialerror.retrieve.engine as engine_mod

    real = engine_mod._resolve_embed_backend

    def _broken_backend(*a, **k):
        model_key, backend = real(*a, **k)

        class _Refusing:
            def embed_batch(self, *args, **kwargs):
                raise RuntimeError("no GPU here")

        return model_key, _Refusing()

    monkeypatch.setattr(engine_mod, "_resolve_embed_backend", _broken_backend)
    filled = fill_idea_vector_cache(store, idea_ids=[r["idea_id"] for r in rows])
    assert filled["n_embedded"] == 0
    assert "no GPU here" in filled["warnings"][0]
    assert _cached_ids(store) == set()


def test_an_empty_id_list_is_a_no_op(store):
    assert fill_idea_vector_cache(store, idea_ids=[]) == {
        "model_key": None, "n_requested": 0, "n_embedded": 0, "n_cached": 0, "warnings": [],
    }


def test_the_cli_fills_by_default_and_no_embed_skips_it(tmp_path, monkeypatch):
    from trialerror.stores.store import open_store

    platform_root = tmp_path / "platform"
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(platform_root))
    program_root = tmp_path / "program"
    program_root.mkdir()
    opened = open_store(program_root, platform_root=platform_root)
    build_corpus_with_inventory(opened)
    launch = bootstrap_launch(opened, purpose="ideation")
    opened.close()

    records_file = tmp_path / "records.json"
    records_file.write_text(json.dumps([_record("A CLI record.")]), encoding="utf-8")

    def _run(*rest):
        buf = io.StringIO()
        with redirect_stdout(buf):
            main([
                "lens", "--program-root", str(program_root), "intake",
                "--round-id", ROUND, "--records", str(records_file),
                "--author-launch", launch, *rest,
            ])
        return json.loads(buf.getvalue().strip())

    envelope = _run()
    assert envelope["ok"] is True, envelope
    assert envelope["result"]["idea_vectors"]["n_embedded"] == 1
    assert envelope["result"]["idea_vectors"]["warnings"] == []

    skipped = _run("--no-embed")
    assert skipped["result"]["idea_vectors"] == {"skipped": "--no-embed"}


# ---------------------------------------------------------------------------
# (b) extra on a round's own records
# ---------------------------------------------------------------------------


def test_extra_round_trips_through_intake(store, corpus):
    _built, launch = corpus
    extra = {"unlock": "what it unlocks", "literature": "what it sits near", "seeds": ["a", "b"]}
    rows = intake_records(
        store, round_id=ROUND, records=[_record("A record.", extra=extra)], author_launch=launch
    )
    stored = read_idea(store, idea_id=rows[0]["idea_id"])
    assert json.loads(stored["extra"]) == extra


def test_extra_reaches_the_judge_as_one_text_field_exactly_as_a_plants_does(store, corpus):
    _built, launch = corpus
    extra = {"unlock": "what it unlocks", "seeds": ["a", "b"]}
    rows = intake_records(
        store, round_id=ROUND, records=[_record("A record.", extra=extra)], author_launch=launch
    )
    idea = read_idea(store, idea_id=rows[0]["idea_id"])
    envelope = build_verifier_envelope(idea)
    assert envelope["record"]["extra_text"] == render_extra_text(extra)
    assert "unlock: what it unlocks" in envelope["record"]["extra_text"]
    # ...and the RAW block is not in the envelope: one text field, the same
    # one a plant fills, is what keeps the two shapes identical.
    assert "extra" not in envelope["record"]


def test_a_record_with_no_extra_carries_none_and_is_shaped_the_same(store, corpus):
    _built, launch = corpus
    rows = intake_records(store, round_id=ROUND, records=[_record("A record.")], author_launch=launch)
    envelope = build_verifier_envelope(read_idea(store, idea_id=rows[0]["idea_id"]))
    assert envelope["record"]["extra_text"] is None
    assert "extra_text" in envelope["record"]


def test_a_malformed_extra_is_refused_with_the_records_position_on_it(store, corpus):
    _built, launch = corpus
    with pytest.raises(ValueError, match="record 1"):
        intake_records(
            store, round_id=ROUND,
            records=[_record("Fine."), _record("Bad.", extra=["a", "list"])],
            author_launch=launch,
        )
    # ...and nothing was written: the file is all or none.
    assert store.knowledge.execute(
        "SELECT COUNT(*) AS n FROM idea WHERE round_id = ?", (ROUND,)
    ).fetchone()["n"] == 0


def test_a_row_written_before_the_column_existed_still_builds_an_envelope(store, corpus):
    """``extra`` is nullable and absent means nothing, not an error."""
    _built, launch = corpus
    row = write_idea(store, round_id=ROUND, author_launch=launch, body="An older record.", probe="p")
    envelope = build_verifier_envelope(read_idea(store, idea_id=row["idea_id"]))
    assert envelope["record"]["extra_text"] is None
