"""Lane FB-6 item 1: the R2 archive is embedded once per statement per model.

Before this, ``_archive_rows`` handed every ``idea`` row to the backend on
every ``--judged-prep`` and every ``--calibration``. The archive is never
reset, so that was the one cost in the screen that grows for the rest of a
programme's life -- a live round measured 76 rows at minutes per build, and
a calibration paid it twice over (``build_plants`` reads R2, and so does
``build_judged_batch``).

What these tests pin is not "it is faster": it is that the cache can never
serve a vector for text nobody embedded. A row whose statement changed
hashes differently and is re-embedded; a run under another model key misses
every row rather than mixing two vector spaces in one ranking;
``--reembed-archive`` rebuilds the lot.
"""

from __future__ import annotations

import pytest

from trialerror.lens import novelty
from trialerror.lens.ideas import write_idea
from trialerror.lens.novelty import (
    IDEA_VECTOR_TABLE,
    _archive_rows,
    build_calibration_batch,
    fetch_idea_vectors,
    run_mechanical_screen,
    statement_digest,
    store_idea_vectors,
)
from trialerror.retrieve import engine as retrieve_engine
from trialerror.stores import update as store_update

from tests._inventory_fixtures import bootstrap_launch, build_corpus_with_inventory
from tests._novelty_fixtures import build_round

SEED = "seed-archive-vectors"


class CountingBackend:
    """Every ``embed_batch`` the screen makes, with the texts it made it
    with -- the only way to assert "zero embed calls" that a cache returning
    the right answer for the wrong reason could not pass."""

    def __init__(self, inner):
        self.inner = inner
        self.calls: list[list[str]] = []

    def embed_batch(self, texts, kind="document"):
        self.calls.append([str(t) for t in texts])
        return self.inner.embed_batch(texts, kind=kind)

    def __getattr__(self, name):
        return getattr(self.inner, name)

    @property
    def n_texts(self) -> int:
        return sum(len(call) for call in self.calls)

    @property
    def texts(self) -> list[str]:
        return [text for call in self.calls for text in call]


@pytest.fixture()
def backend(store):
    model_key, inner = retrieve_engine._resolve_embed_backend(store, side="query")
    return model_key, CountingBackend(inner)


def _archive(store, backend_pair, **kwargs):
    model_key, counting = backend_pair
    return _archive_rows(store, backend=counting, model_key=model_key, **kwargs)


# ---------------------------------------------------------------------------
# the cache itself
# ---------------------------------------------------------------------------


def test_a_second_read_of_the_archive_makes_no_embed_call_at_all(store, backend):
    fixture = build_round(store)
    first = _archive(store, backend)
    model_key, counting = backend
    assert counting.n_texts == len(fixture["ideas"])
    assert all(row["vector"] for row in first)

    counting.calls.clear()
    second = _archive(store, backend)
    assert counting.calls == [], "a cached archive must reach the backend zero times"
    assert [row["idea_id"] for row in second] == [row["idea_id"] for row in first]
    for a, b in zip(first, second):
        assert [round(v, 6) for v in a["vector"]] == [round(v, 6) for v in b["vector"]]


def test_the_cache_is_keyed_by_the_text_that_was_embedded(store, backend):
    fixture = build_round(store)
    _archive(store, backend)
    model_key, counting = backend
    cached = fetch_idea_vectors(store, model_key=model_key)
    assert set(cached) == {row["idea_id"] for row in fixture["ideas"]}
    row = fixture["ideas"][0]
    assert cached[row["idea_id"]]["statement_sha256"] == statement_digest(row["body"])


def test_a_changed_statement_is_re_embedded_and_nothing_else_is(store, backend):
    fixture = build_round(store)
    _archive(store, backend)
    model_key, counting = backend

    changed = fixture["ideas"][2]
    store_update(
        store, "idea", pk_column="idea_id", pk_value=changed["idea_id"],
        changes={"body": "A wholly rewritten statement for the same archived row."},
    )
    counting.calls.clear()
    rows = _archive(store, backend)
    assert counting.texts == ["A wholly rewritten statement for the same archived row."]
    fresh = next(r for r in rows if r["idea_id"] == changed["idea_id"])
    assert fresh["vector"]
    assert fetch_idea_vectors(store, model_key=model_key)[changed["idea_id"]][
        "statement_sha256"
    ] == statement_digest("A wholly rewritten statement for the same archived row.")


def test_a_row_written_since_the_last_build_is_embedded_for_the_first_time(store, backend):
    fixture = build_round(store)
    _archive(store, backend)
    model_key, counting = backend
    write_idea(
        store, round_id=fixture["round_id"], author_launch=fixture["launches"]["lens-1"],
        body="An archived row that arrived after the first batch was built.", status="archived",
    )
    counting.calls.clear()
    _archive(store, backend)
    assert counting.texts == ["An archived row that arrived after the first batch was built."]


def test_reembed_rebuilds_every_row(store, backend):
    fixture = build_round(store)
    _archive(store, backend)
    model_key, counting = backend
    counting.calls.clear()
    _archive(store, backend, reembed=True)
    assert counting.n_texts == len(fixture["ideas"])


def test_another_model_key_misses_every_row_rather_than_mixing_vector_spaces(store, backend):
    build_round(store)
    _archive(store, backend)
    model_key, counting = backend
    assert fetch_idea_vectors(store, model_key="some-other-model") == {}
    counting.calls.clear()
    _archive_rows(store, backend=counting, model_key="some-other-model")
    assert counting.n_texts > 0


def test_an_empty_archive_reaches_neither_the_backend_nor_the_cache(store, backend):
    build_corpus_with_inventory(store)
    assert _archive(store, backend) == []
    assert backend[1].calls == []


def test_the_cache_replaces_rather_than_appends(store, backend):
    fixture = build_round(store)
    model_key, _counting = backend
    idea_id = fixture["ideas"][0]["idea_id"]
    store_idea_vectors(store, model_key=model_key, vectors={idea_id: ("sha-one", [0.1, 0.2])})
    store_idea_vectors(store, model_key=model_key, vectors={idea_id: ("sha-two", [0.3, 0.4])})
    rows = store.knowledge.execute(
        f"SELECT statement_sha256, dim FROM {IDEA_VECTOR_TABLE} WHERE idea_id = ? AND model_key = ?",
        (idea_id, model_key),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["statement_sha256"] == "sha-two"
    assert rows[0]["dim"] == 2


# ---------------------------------------------------------------------------
# through a batch build
# ---------------------------------------------------------------------------


CALIBRATION_PLANTS = [
    {"plant_id": "C-1", "kind": "area", "statement": "Calibration plant one, a rewritten reference row.",
     "expected_labels": {"R3": ["same", "variant"]}},
    {"plant_id": "C-2", "kind": "custom", "statement": "Calibration plant two, unrelated to everything.",
     "expected_labels": {"R3": ["new-mechanism"]}},
]


@pytest.fixture()
def counted_screen(store, monkeypatch):
    """The screen's own backend resolution, wrapped -- so a batch build is
    measured through the code path the CLI actually takes."""
    real = retrieve_engine._resolve_embed_backend
    holder: dict[str, CountingBackend] = {}

    def _resolve(target_store, *, side="document"):
        model_key, inner = real(target_store, side=side)
        counting = holder.get(model_key)
        if counting is None:
            counting = holder[model_key] = CountingBackend(inner)
        return model_key, counting

    monkeypatch.setattr(novelty.retrieve_engine, "_resolve_embed_backend", _resolve)
    return holder


def test_a_second_batch_build_embeds_no_archive_statement(store, counted_screen, tmp_path):
    fixture = build_round(store)
    bodies = {row["body"] for row in fixture["ideas"]}

    def _build(**kwargs):
        return build_calibration_batch(
            store, round_id=fixture["round_id"], external_plants=CALIBRATION_PLANTS, seed=SEED,
            judged_sets="R2,R3", batch_fail_on="area", out_dir=tmp_path, **kwargs,
        )

    _build()
    counting = next(iter(counted_screen.values()))
    assert bodies & set(counting.texts), "the first build must embed the archive"

    counting.calls.clear()
    _build()
    assert not (bodies & set(counting.texts)), "a cached archive is not re-embedded on the next build"


def test_reembed_archive_puts_the_statements_back_through_the_backend(store, counted_screen, tmp_path):
    fixture = build_round(store)
    bodies = {row["body"] for row in fixture["ideas"]}

    def _build(**kwargs):
        return build_calibration_batch(
            store, round_id=fixture["round_id"], external_plants=CALIBRATION_PLANTS, seed=SEED,
            judged_sets="R2,R3", batch_fail_on="area", out_dir=tmp_path, **kwargs,
        )

    _build()
    counting = next(iter(counted_screen.values()))
    counting.calls.clear()
    _build(reembed_archive=True)
    assert bodies <= set(counting.texts)


def test_the_mechanical_screen_fills_the_cache_it_will_later_read(store, counted_screen):
    """"Filled at screen time": the mechanical half already embeds exactly
    these statements under exactly this model key."""
    fixture = build_round(store)
    run_mechanical_screen(
        store, round_id=fixture["round_id"], launch_id=fixture["launches"]["lens-1"]
    )
    counting = next(iter(counted_screen.values()))
    model_key = next(iter(counted_screen))
    cached = fetch_idea_vectors(store, model_key=model_key)
    assert set(cached) == {row["idea_id"] for row in fixture["ideas"]}

    counting.calls.clear()
    rows = _archive_rows(store, backend=counting, model_key=model_key)
    assert counting.calls == []
    assert all(row["vector"] for row in rows)


# ---------------------------------------------------------------------------
# the one failure mode that grows with the archive (stage-3, finding N1)
# ---------------------------------------------------------------------------


SQLITE_VARIABLE_CEILING = 32766


def test_the_cache_lookup_survives_an_archive_past_sqlites_variable_ceiling(store, backend):
    """``_archive_rows`` names EVERY archived idea in the lookup, so an
    archive above SQLite's parameter ceiling raised ``too many SQL
    variables`` -- an uncaught ``OperationalError`` out of ``--judged-prep``,
    not a refusal and not a cache miss. That is the one failure mode in this
    cache that grows with the archive, which is the growth the cache exists
    to bound.

    Proved on ids alone rather than on 32767 real ``idea`` rows: the ceiling
    is a property of the BIND list, so the row count that would trip it is
    irrelevant to the fix."""
    fixture = build_round(store)
    model_key, _counting = backend
    _archive(store, backend)
    real = [row["idea_id"] for row in fixture["ideas"]]

    ids = [*real, *(f"IDEA-absent-{n:06d}" for n in range(SQLITE_VARIABLE_CEILING + 2))]
    assert len(ids) > SQLITE_VARIABLE_CEILING
    got = fetch_idea_vectors(store, model_key=model_key, idea_ids=ids)

    assert set(got) == set(real), "a chunked lookup returns the cached rows and invents none"
    whole = fetch_idea_vectors(store, model_key=model_key)
    assert {k: v["statement_sha256"] for k, v in got.items()} == {
        k: v["statement_sha256"] for k, v in whole.items()
    }
    assert all(got[k]["vector"] == whole[k]["vector"] for k in got)


def test_a_chunk_boundary_does_not_drop_or_duplicate_a_row(store, backend):
    """The chunking is only safe if it partitions: every id asked for is
    asked for exactly once, whichever chunk it lands in."""
    model_key, _counting = backend
    build_round(store)
    ids = [f"IDEA-chunk-{n:05d}" for n in range(novelty._ID_BIND_CHUNK * 2 + 3)]
    store_idea_vectors(
        store, model_key=model_key,
        vectors={idea_id: (f"sha-{idea_id}", [float(n), 0.5]) for n, idea_id in enumerate(ids)},
    )
    got = fetch_idea_vectors(store, model_key=model_key, idea_ids=ids)
    assert set(got) == set(ids)
    assert [got[idea_id]["vector"][0] for idea_id in ids] == [float(n) for n in range(len(ids))]
