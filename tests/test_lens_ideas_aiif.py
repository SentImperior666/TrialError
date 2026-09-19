"""schema-v6: the two new ``idea`` statuses, the ten promoted record
columns, and ``read_idea``'s column-then-``provenance``-JSON fallback.

Kept in its own module rather than appended to ``tests/test_lens_ideas.py``
so the pre-v6 contract that file pins stays legible as exactly that: what
was true before this migration, still true after it.
"""

from __future__ import annotations

import json

import pytest

from trialerror.lens.ideas import (
    AIIF_FIELDS,
    AIIF_JSON_FIELDS,
    IDEA_STATUSES,
    read_idea,
    write_idea,
)
from trialerror.stores import get, table_columns, update
from tests._lens_fixtures import bootstrap_launch


def test_new_statuses_are_declared_and_the_old_three_survive():
    # `archived` joined them at schema-v9 (lane FB-5 item 4): a prior round's
    # candidate or request row, in the reference set and not a candidate.
    assert set(IDEA_STATUSES) == {
        "raw", "consolidated", "promoted", "eliminated", "merged", "archived",
    }


@pytest.mark.parametrize("status", ["eliminated", "merged", "archived"])
def test_the_db_check_constraint_accepts_the_new_statuses(store, status):
    """The caller-side tuple and the DDL CHECK have to agree -- a widened
    tuple over an un-migrated CHECK would fail only at the DB round trip."""
    launch_id = bootstrap_launch(store)
    row = write_idea(store, round_id="round-1", author_launch=launch_id, body="b", status=status)
    assert get(store, "idea", pk_column="idea_id", pk_value=row["idea_id"])["status"] == status


def test_the_db_check_constraint_still_refuses_an_unknown_status(store):
    from trialerror.stores.errors import StoreError

    launch_id = bootstrap_launch(store)
    with pytest.raises((StoreError, ValueError)):
        write_idea(store, round_id="round-1", author_launch=launch_id, body="b", status="shelved")


def test_every_promoted_field_is_a_real_column(store):
    columns = set(table_columns(store.knowledge, "idea"))
    assert set(AIIF_FIELDS) <= columns


def test_write_then_read_round_trips_every_promoted_field(store):
    launch_id = bootstrap_launch(store)
    written = write_idea(
        store,
        round_id="round-1",
        author_launch=launch_id,
        body="full text of the idea, never a summary",
        status="consolidated",
        requirements="two to five lines of requirements stated before the statement",
        recipe_card="MISMATCH",
        operation_declared="replace",
        probe="run the stated check against inventory row R-7",
        surprise="the constraint is satisfiable without a shared clock",
        author_rationale="generator-facing only; never enters a judge envelope",
        parent_ids=["IDEA-parent-1", "IDEA-parent-2"],
        statement_sha256="a" * 64,
        corpus_snapshot_id="SNAP-round-1",
        convergent_with=["IDEA-elsewhere"],
    )
    got = read_idea(store, idea_id=written["idea_id"])

    assert got["status"] == "consolidated"
    assert got["requirements"].startswith("two to five lines")
    assert got["recipe_card"] == "MISMATCH"
    assert got["operation_declared"] == "replace"
    assert got["probe"].startswith("run the stated check")
    assert got["surprise"].startswith("the constraint")
    assert got["author_rationale"].startswith("generator-facing")
    assert got["parent_ids"] == ["IDEA-parent-1", "IDEA-parent-2"]
    assert got["statement_sha256"] == "a" * 64
    assert got["corpus_snapshot_id"] == "SNAP-round-1"
    assert got["convergent_with"] == ["IDEA-elsewhere"]


def test_json_list_fields_are_stored_encoded_and_returned_decoded(store):
    launch_id = bootstrap_launch(store)
    written = write_idea(
        store, round_id="r", author_launch=launch_id, body="b",
        parent_ids=["IDEA-1"], convergent_with=["IDEA-2"],
    )
    raw = get(store, "idea", pk_column="idea_id", pk_value=written["idea_id"])
    for field in AIIF_JSON_FIELDS:
        assert isinstance(raw[field], str)
        assert json.loads(raw[field]) == read_idea(store, idea_id=written["idea_id"])[field]


def test_write_idea_refuses_a_non_list_for_a_json_list_field(store):
    launch_id = bootstrap_launch(store)
    with pytest.raises(ValueError):
        write_idea(store, round_id="r", author_launch=launch_id, body="b", parent_ids={"nope": 1})


def test_interim_provenance_json_is_the_fallback_for_a_pre_migration_row(store):
    """A row written under the interim convention -- every AIIF field packed
    into the ``provenance`` JSON object, no columns at all -- must read back
    exactly like one written since the migration."""
    launch_id = bootstrap_launch(store)
    interim = {
        "set_id": "SET-9",
        "docs": ["DOC-1"],
        "requirements": "stated before the statement",
        "recipe_card": "TRANSFER",
        "operation_declared": "decouple",
        "probe": "check the two separated flows against inventory row R-3",
        "surprise": "one economy was really two",
        "author_rationale": "kept out of every judge envelope",
        "parent_ids": ["IDEA-old"],
        "statement_sha256": "b" * 64,
        "corpus_snapshot_id": "SNAP-0",
        "convergent_with": [],
    }
    written = write_idea(
        store, round_id="round-0", author_launch=launch_id, body="b", provenance=interim
    )
    # Nothing landed in the columns themselves -- this row predates them.
    raw = get(store, "idea", pk_column="idea_id", pk_value=written["idea_id"])
    assert all(raw[field] is None for field in AIIF_FIELDS)

    got = read_idea(store, idea_id=written["idea_id"])
    assert got["requirements"] == "stated before the statement"
    assert got["recipe_card"] == "TRANSFER"
    assert got["operation_declared"] == "decouple"
    assert got["surprise"] == "one economy was really two"
    assert got["parent_ids"] == ["IDEA-old"]
    assert got["convergent_with"] == []
    assert got["statement_sha256"] == "b" * 64


def test_a_column_value_wins_over_the_provenance_fallback(store):
    launch_id = bootstrap_launch(store)
    written = write_idea(
        store, round_id="r", author_launch=launch_id, body="b",
        recipe_card="SCALE-SHIFT",
        provenance={"recipe_card": "STALE-INTERIM-VALUE", "docs": ["DOC-1"]},
    )
    assert read_idea(store, idea_id=written["idea_id"])["recipe_card"] == "SCALE-SHIFT"


def test_a_plain_string_provenance_is_refused_at_the_writer(store):
    """It used to be stored verbatim. Nothing can read a doc id out of a
    sentence, so the record reached the judge declaring no source at all --
    which the envelope reports as provenance_docs [] and nobody notices."""
    launch_id = bootstrap_launch(store)
    with pytest.raises(ValueError) as excinfo:
        write_idea(store, round_id="r", author_launch=launch_id, body="b", provenance="free-form note")
    assert "provenance" in str(excinfo.value) and "docs" in str(excinfo.value)


def test_a_historical_string_provenance_still_reads_back_without_a_fallback(store):
    """Rows written before that refusal are still in the store, and
    ``read_idea`` answers for them exactly as it did: no fallback values,
    no exception."""
    launch_id = bootstrap_launch(store)
    written = write_idea(
        store, round_id="r", author_launch=launch_id, body="b", provenance={"docs": []}
    )
    update(
        store, "idea", pk_column="idea_id", pk_value=written["idea_id"],
        changes={"provenance": "free-form note"},
    )
    got = read_idea(store, idea_id=written["idea_id"])
    assert got["provenance"] == "free-form note"
    assert all(got[field] is None for field in AIIF_FIELDS)


def test_read_idea_returns_none_for_an_unknown_id(store):
    assert read_idea(store, idea_id="IDEA-nope") is None


def test_a_pre_migration_row_still_writes_and_reads_with_no_aiif_fields_at_all(store):
    """The whole pre-v6 call shape, unchanged: no new keyword, no new
    column value, and every promoted field simply ``None``."""
    launch_id = bootstrap_launch(store)
    written = write_idea(
        store, round_id="round-1", author_launch=launch_id, body="b",
        home="cell-4", assumed_circle="skeptics", tier="far", set_distance=0.81,
    )
    got = read_idea(store, idea_id=written["idea_id"])
    assert got["tier"] == "far"
    assert got["home"] == "cell-4"
    assert all(got[field] is None for field in AIIF_FIELDS)
