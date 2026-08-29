"""Unit tests for :mod:`trialerror.arxiv_index.ingest` -- streaming zip ingest,
resumability (kill-mid-build), the ASSUMED-schema loud-failure guard."""

from __future__ import annotations

import json
import sqlite3

import pytest

from trialerror.arxiv_index.ingest import (
    DEFAULT_FIELD_MAP,
    ArxivIndexIngestError,
    SchemaAssumptionError,
    SimulatedKillError,
    build_index_from_zip,
)
from trialerror.arxiv_index.store import META_TABLE_NAME, VEC_TABLE_NAME, get_build_state, row_count
from tests._arxiv_index_fixtures import make_record, write_records_zip, write_small_fixture_zip


@pytest.fixture()
def conn(monkeypatch):
    # force the fallback backend for most tests -- deterministic, no
    # dependency on the real extension being installed; the sqlite-vec
    # path is exercised separately in tests/test_arxiv_index_query.py and
    # test_arxiv_index_store.py::test_ensure_schema_real_sqlite_vec_when_available.
    import trialerror.arxiv_index.store as store_mod

    monkeypatch.setattr(store_mod, "try_load_sqlite_vec", lambda c: False)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def test_build_index_ingests_every_row(tmp_path, conn):
    zip_path = write_small_fixture_zip(tmp_path / "fixture.zip", n=12, dims=8)
    progress = build_index_from_zip(conn, zip_path, dims=8, batch_size=4)

    assert progress["rows_ingested"] == 12
    assert progress["rows_skipped"] == 0
    assert progress["current_member"] is None
    assert row_count(conn) == 12

    row = conn.execute(f"SELECT * FROM {META_TABLE_NAME} WHERE arxiv_id = ?", ("9999.00003",)).fetchone()
    assert row["title"] == "Synthetic Paper 3"
    assert row["categories"] == "cs.AI cs.LG"

    vec_row = conn.execute(f"SELECT * FROM {VEC_TABLE_NAME} WHERE arxiv_id = ?", ("9999.00003",)).fetchone()
    assert vec_row["dims"] == 8

    state = get_build_state(conn)
    assert state["status"] == "complete"
    assert state["rows_ingested"] == "12"


def test_build_index_batches_smaller_than_total_still_flush_final_partial_batch(tmp_path, conn):
    zip_path = write_small_fixture_zip(tmp_path / "fixture.zip", n=10, dims=8)
    progress = build_index_from_zip(conn, zip_path, dims=8, batch_size=7)  # 7 + 3 (partial final batch)
    assert progress["rows_ingested"] == 10


def test_build_index_multiple_members(tmp_path, conn):
    zip_path = tmp_path / "fixture.zip"
    members = {
        "shard-0000.jsonl": [make_record(i, id_prefix="0001") for i in range(5)],
        "shard-0001.jsonl": [make_record(i, id_prefix="0002") for i in range(5)],
    }
    write_records_zip(zip_path, members)
    progress = build_index_from_zip(conn, zip_path, dims=8, batch_size=3)
    assert progress["rows_ingested"] == 10
    assert sorted(progress["members_done"]) == ["shard-0000.jsonl", "shard-0001.jsonl"]


def test_kill_mid_build_then_resume_ingests_every_row_exactly_once(tmp_path, conn):
    zip_path = write_small_fixture_zip(tmp_path / "fixture.zip", n=20, dims=8)

    with pytest.raises(SimulatedKillError):
        build_index_from_zip(conn, zip_path, dims=8, batch_size=4, _raise_after_rows=8)

    state_after_kill = get_build_state(conn)
    assert state_after_kill["status"] == "building"
    partial_ingested = int(state_after_kill["rows_ingested"])
    assert 0 < partial_ingested < 20
    assert row_count(conn) == partial_ingested

    checkpoint = json.loads(state_after_kill["checkpoint_json"])

    # resume from the persisted checkpoint -- no _raise_after_rows this time
    final_progress = build_index_from_zip(conn, zip_path, dims=8, batch_size=4, checkpoint=checkpoint)

    assert final_progress["rows_ingested"] == 20
    assert row_count(conn) == 20  # byte-identical final state -- no duplicates, nothing missing

    # every id 0..19 present exactly once
    ids = {r["arxiv_id"] for r in conn.execute(f"SELECT arxiv_id FROM {META_TABLE_NAME}").fetchall()}
    assert ids == {f"9999.{i:05d}" for i in range(20)}
    vec_ids = [r["arxiv_id"] for r in conn.execute(f"SELECT arxiv_id FROM {VEC_TABLE_NAME}").fetchall()]
    assert sorted(vec_ids) == sorted(ids)  # no duplicate vec rows either


def test_kill_mid_build_across_two_members_resumes_correctly(tmp_path, conn):
    zip_path = tmp_path / "fixture.zip"
    members = {
        "shard-0000.jsonl": [make_record(i, id_prefix="0001") for i in range(6)],
        "shard-0001.jsonl": [make_record(i, id_prefix="0002") for i in range(6)],
    }
    write_records_zip(zip_path, members)

    with pytest.raises(SimulatedKillError):
        build_index_from_zip(conn, zip_path, dims=8, batch_size=3, _raise_after_rows=9)  # kills partway into shard-0001

    state = get_build_state(conn)
    checkpoint = json.loads(state["checkpoint_json"])
    assert "shard-0000.jsonl" in checkpoint["members_done"]
    assert checkpoint["current_member"] == "shard-0001.jsonl"

    final_progress = build_index_from_zip(conn, zip_path, dims=8, batch_size=3, checkpoint=checkpoint)
    assert final_progress["rows_ingested"] == 12
    assert row_count(conn) == 12


def test_resume_with_no_progress_change_is_a_pure_no_op_replay(tmp_path, conn):
    """Calling build_index_from_zip AGAIN with a checkpoint from a run that
    already finished must not double-insert anything (idempotent replay)."""
    zip_path = write_small_fixture_zip(tmp_path / "fixture.zip", n=8, dims=8)
    first = build_index_from_zip(conn, zip_path, dims=8, batch_size=3)
    second = build_index_from_zip(conn, zip_path, dims=8, batch_size=3, checkpoint=first)
    assert second["rows_ingested"] == 8
    assert row_count(conn) == 8


def test_on_progress_callback_invoked_with_incrementing_totals(tmp_path, conn):
    zip_path = write_small_fixture_zip(tmp_path / "fixture.zip", n=9, dims=8)
    seen = []
    build_index_from_zip(conn, zip_path, dims=8, batch_size=3, on_progress=lambda p: seen.append(p["rows_ingested"]))
    assert seen == sorted(seen)
    assert seen[-1] == 9


def test_missing_member_glob_match_raises_clear_error(tmp_path, conn):
    zip_path = write_small_fixture_zip(tmp_path / "fixture.zip", n=3, dims=8, member_name="data.csv")
    with pytest.raises(ArxivIndexIngestError, match="no zip members matched"):
        build_index_from_zip(conn, zip_path, dims=8, member_glob="*.jsonl")


def test_first_record_missing_required_field_raises_schema_assumption_error(tmp_path, conn):
    zip_path = tmp_path / "fixture.zip"
    bad_records = [{"title": "no id or embedding here"}]
    write_records_zip(zip_path, {"shard-0000.jsonl": bad_records})
    with pytest.raises(SchemaAssumptionError):
        build_index_from_zip(conn, zip_path, dims=8)


def test_later_record_missing_required_field_is_skipped_not_fatal(tmp_path, conn):
    zip_path = tmp_path / "fixture.zip"
    good = [make_record(0, dims=8), make_record(1, dims=8)]
    bad = [{"title": "malformed, no id/embedding"}]
    write_records_zip(zip_path, {"shard-0000.jsonl": good + bad})
    progress = build_index_from_zip(conn, zip_path, dims=8)
    assert progress["rows_ingested"] == 2
    assert progress["rows_skipped"] == 1


def test_malformed_json_line_is_skipped_not_fatal(tmp_path, conn):
    zip_path = tmp_path / "fixture.zip"
    good = [make_record(0, dims=8)]
    body = json.dumps(good[0]) + "\n" + "{not valid json,,," + "\n"
    import zipfile

    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("shard-0000.jsonl", body)
    progress = build_index_from_zip(conn, zip_path, dims=8)
    assert progress["rows_ingested"] == 1
    assert progress["rows_skipped"] == 1


def test_dims_mismatch_row_is_skipped(tmp_path, conn):
    zip_path = tmp_path / "fixture.zip"
    rec_ok = make_record(0, dims=8)
    rec_wrong_dims = make_record(1, dims=8)
    rec_wrong_dims["embedding"] = rec_wrong_dims["embedding"][:4]  # wrong width
    write_records_zip(zip_path, {"shard-0000.jsonl": [rec_ok, rec_wrong_dims]})
    progress = build_index_from_zip(conn, zip_path, dims=8)
    assert progress["rows_ingested"] == 1
    assert progress["rows_skipped"] == 1


def test_field_map_override_supports_alternate_key_names(tmp_path, conn):
    zip_path = tmp_path / "fixture.zip"
    records = [{"paper_id": "alt.0001", "vec": [0.1] * 8}]
    write_records_zip(zip_path, {"shard-0000.jsonl": records})
    field_map = dict(DEFAULT_FIELD_MAP)
    field_map["arxiv_id"] = ("paper_id",)
    field_map["embedding"] = ("vec",)
    progress = build_index_from_zip(conn, zip_path, dims=8, field_map=field_map)
    assert progress["rows_ingested"] == 1
    row = conn.execute(f"SELECT arxiv_id FROM {META_TABLE_NAME}").fetchone()
    assert row["arxiv_id"] == "alt.0001"
