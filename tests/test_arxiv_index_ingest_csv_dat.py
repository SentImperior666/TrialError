"""Unit tests for the ``csv+dat`` zip layout (:mod:`trialerror.arxiv_index.ingest`'s
``_build_index_from_csv_dat`` branch, fix-arxiv-ingest-layout session) -- the
REAL Kaggle ``openai-arxiv-embeddings.zip`` layout confirmed by direct
inspection: ``papers.csv`` (header ``index,id,journal``) + ``vectors.dat``
(raw concatenated little-endian float32, ``dims*4`` bytes/row, row ``i``
aligned to csv row ``i``). Mirrors ``tests/test_arxiv_index_ingest.py``'s own
structure/fixture-style for the jsonl layout, one test file per layout."""

from __future__ import annotations

import json
import sqlite3
import struct

import pytest

from trialerror.arxiv_index.ingest import (
    ArxivIndexIngestError,
    CSV_DAT_LAYOUT_KEY,
    SchemaAssumptionError,
    SimulatedKillError,
    build_index_from_zip,
)
from trialerror.arxiv_index.store import (
    META_TABLE_NAME,
    VEC_TABLE_NAME,
    deserialize_vector_fallback,
    get_build_state,
    row_count,
)
from tests._arxiv_index_fixtures import (
    make_csv_dat_records,
    make_record,
    write_csv_dat_fixture_zip,
    write_small_fixture_zip,
)


@pytest.fixture()
def conn(monkeypatch):
    # force the fallback backend -- deterministic, no dependency on the
    # real sqlite-vec extension being installed (same convention
    # tests/test_arxiv_index_ingest.py's own `conn` fixture uses).
    import trialerror.arxiv_index.store as store_mod

    monkeypatch.setattr(store_mod, "try_load_sqlite_vec", lambda c: False)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def test_csv_dat_layout_ingests_every_row_with_correct_vectors_and_journal(tmp_path, conn):
    zip_path, records = write_csv_dat_fixture_zip(tmp_path / "fixture.zip", n=24, dims=8)
    progress = build_index_from_zip(conn, zip_path, dims=8, batch_size=7)

    assert progress["rows_ingested"] == 24
    assert progress["rows_skipped"] == 0
    assert progress["current_member"] is None
    assert progress["members_done"] == [CSV_DAT_LAYOUT_KEY]
    assert row_count(conn) == 24

    state = get_build_state(conn)
    assert state["status"] == "complete"
    assert state["rows_ingested"] == "24"
    assert state.get("layout") == "csv_dat"

    for rec in records:
        meta_row = conn.execute(f"SELECT * FROM {META_TABLE_NAME} WHERE arxiv_id = ?", (rec["id"],)).fetchone()
        assert meta_row is not None
        expected_journal = rec["journal"] or None
        assert meta_row["journal_ref"] == expected_journal
        # papers.csv carries no title/abstract/categories/authors/doi (module docstring)
        assert meta_row["title"] is None
        assert meta_row["abstract"] is None

        vec_row = conn.execute(f"SELECT * FROM {VEC_TABLE_NAME} WHERE arxiv_id = ?", (rec["id"],)).fetchone()
        assert vec_row["dims"] == 8
        got_vector = deserialize_vector_fallback(vec_row["vector"])
        assert got_vector == pytest.approx(rec["vector"])


def test_csv_dat_kill_mid_dat_then_resume_ingests_every_row_exactly_once(tmp_path, conn):
    zip_path, records = write_csv_dat_fixture_zip(tmp_path / "fixture.zip", n=40, dims=8)

    with pytest.raises(SimulatedKillError):
        build_index_from_zip(conn, zip_path, dims=8, batch_size=5, _raise_after_rows=17)

    state_after_kill = get_build_state(conn)
    assert state_after_kill["status"] == "building"
    partial_ingested = int(state_after_kill["rows_ingested"])
    assert 0 < partial_ingested < 40
    assert row_count(conn) == partial_ingested

    checkpoint = json.loads(state_after_kill["checkpoint_json"])
    assert checkpoint["current_member"] == CSV_DAT_LAYOUT_KEY
    assert checkpoint["records_seen_in_current_member"] == partial_ingested

    final_progress = build_index_from_zip(conn, zip_path, dims=8, batch_size=5, checkpoint=checkpoint)

    assert final_progress["rows_ingested"] == 40
    assert row_count(conn) == 40  # byte-identical final state -- no duplicates, nothing missing

    ids = {r["arxiv_id"] for r in conn.execute(f"SELECT arxiv_id FROM {META_TABLE_NAME}").fetchall()}
    assert ids == {rec["id"] for rec in records}
    vec_ids = [r["arxiv_id"] for r in conn.execute(f"SELECT arxiv_id FROM {VEC_TABLE_NAME}").fetchall()]
    assert sorted(vec_ids) == sorted(ids)

    # every vector byte-exact after the resume, including the ones re-offered mid-batch
    for rec in records:
        vec_row = conn.execute(f"SELECT vector FROM {VEC_TABLE_NAME} WHERE arxiv_id = ?", (rec["id"],)).fetchone()
        assert deserialize_vector_fallback(vec_row["vector"]) == pytest.approx(rec["vector"])


def test_csv_dat_resume_with_no_progress_change_is_a_pure_no_op_replay(tmp_path, conn):
    zip_path, _records = write_csv_dat_fixture_zip(tmp_path / "fixture.zip", n=10, dims=8)
    first = build_index_from_zip(conn, zip_path, dims=8, batch_size=4)
    second = build_index_from_zip(conn, zip_path, dims=8, batch_size=4, checkpoint=first)
    assert second["rows_ingested"] == 10
    assert row_count(conn) == 10


def test_short_dat_stream_raises_loudly(tmp_path, conn):
    """dat content missing exactly one full row's worth of bytes off the
    end (a legitimately-CRC-correct-for-its-own-content zip member, so the
    guard under test is ingest.py's OWN short-read check, not zipfile's
    unrelated CRC check) -- the last csv row's dat read comes up 0 bytes
    short, and must fail loudly rather than silently truncating the index."""
    row_bytes = 8 * 4
    zip_path, _records = write_csv_dat_fixture_zip(
        tmp_path / "fixture.zip", n=12, dims=8, truncate_dat_bytes=row_bytes
    )
    with pytest.raises(ArxivIndexIngestError, match="short read"):
        build_index_from_zip(conn, zip_path, dims=8)
    # no partial index left behind as "complete"
    state = get_build_state(conn)
    assert state.get("status") != "complete"


def test_dat_size_not_a_multiple_of_row_bytes_raises_immediately(tmp_path, conn):
    """A partial-row truncation (a few stray bytes, not a whole row) is
    caught by the UPFRONT size%row_bytes check before any streaming even
    starts -- distinct code path from the mid-stream short-read guard."""
    zip_path, _records = write_csv_dat_fixture_zip(tmp_path / "fixture.zip", n=12, dims=8, truncate_dat_bytes=3)
    with pytest.raises(ArxivIndexIngestError, match="not a multiple"):
        build_index_from_zip(conn, zip_path, dims=8)
    assert row_count(conn) == 0


def test_dat_has_extra_full_row_raises_row_count_mismatch(tmp_path, conn):
    """dat is byte-aligned (every individual read succeeds) but implies ONE
    MORE row than papers.csv actually has -- must be caught by the final
    row-count integrity assertion, not silently accepted as 'complete'."""
    zip_path, records = write_csv_dat_fixture_zip(tmp_path / "fixture.zip", n=12, dims=8, extra_dat_rows=1)
    with pytest.raises(ArxivIndexIngestError, match="row-count integrity check failed"):
        build_index_from_zip(conn, zip_path, dims=8)
    # every real row was still fully readable/insertable before the final check fired
    assert row_count(conn) == len(records)
    state = get_build_state(conn)
    assert state.get("status") != "complete"


def test_csv_dat_layout_wins_over_jsonl_when_both_present(tmp_path, conn):
    """Auto-detection priority: when a zip contains BOTH a jsonl member
    (which would normally satisfy the default member_glob) AND the
    papers.csv+vectors.dat pair, the csv+dat layout is used exclusively --
    the jsonl member is never read at all."""
    zip_path = tmp_path / "fixture.zip"
    csv_records = make_csv_dat_records(6, dims=8, id_prefix="9999")
    jsonl_only_records = [make_record(i, dims=8, id_prefix="0001") for i in range(4)]  # distinct prefix, no overlap

    # write the jsonl member (would normally satisfy the default member_glob)
    # and the papers.csv+vectors.dat pair into the SAME zip.
    import zipfile

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        body = "\n".join(json.dumps(r) for r in jsonl_only_records) + "\n"
        zf.writestr("extra.jsonl", body)
        csv_lines = ["index,id,journal"] + [f"{r['index']},{r['id']},{r['journal']}" for r in csv_records]
        zf.writestr("papers.csv", "\r\n".join(csv_lines) + "\r\n")
        zf.writestr("vectors.dat", b"".join(struct.pack("<8f", *r["vector"]) for r in csv_records))

    progress = build_index_from_zip(conn, zip_path, dims=8, batch_size=3, member_glob="*.jsonl")

    assert progress["rows_ingested"] == 6
    assert progress["members_done"] == [CSV_DAT_LAYOUT_KEY]

    ids = {r["arxiv_id"] for r in conn.execute(f"SELECT arxiv_id FROM {META_TABLE_NAME}").fetchall()}
    assert ids == {r["id"] for r in csv_records}
    # the jsonl-only ids (distinct "0001.*" prefix) must NOT be present --
    # proof the jsonl member was never read at all once csv+dat was detected.
    assert not any(i.startswith("0001.") for i in ids)


def test_csv_dat_missing_id_column_raises_schema_error(tmp_path, conn):
    zip_path = tmp_path / "fixture.zip"
    import zipfile

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("papers.csv", "index,journal\r\n0,arxiv\r\n")
        zf.writestr("vectors.dat", struct.pack("<8f", *([0.1] * 8)))
    with pytest.raises(SchemaAssumptionError, match="no 'id' column"):
        build_index_from_zip(conn, zip_path, dims=8)


def test_csv_dat_empty_id_value_raises_schema_error(tmp_path, conn):
    zip_path = tmp_path / "fixture.zip"
    import zipfile

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("papers.csv", "index,id,journal\r\n0,,arxiv\r\n")
        zf.writestr("vectors.dat", struct.pack("<8f", *([0.1] * 8)))
    with pytest.raises(SchemaAssumptionError, match="empty/missing id"):
        build_index_from_zip(conn, zip_path, dims=8)


def test_jsonl_only_zip_still_uses_jsonl_path_unaffected(tmp_path, conn):
    """Back-compat sanity check (build brief item 1: 'keeping the jsonl
    path for tests/back-compat') -- a zip with no csv+dat members at all
    behaves exactly as before this session's change."""
    zip_path = write_small_fixture_zip(tmp_path / "fixture.zip", n=5, dims=8)
    progress = build_index_from_zip(conn, zip_path, dims=8, batch_size=2)
    assert progress["rows_ingested"] == 5
    assert progress["members_done"] == ["shard-0000.jsonl"]
    state = get_build_state(conn)
    assert state.get("layout") != "csv_dat"
