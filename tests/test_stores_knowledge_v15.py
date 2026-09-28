"""Lane SI part B, item B1: knowledge schema v15 -- ``source_evidence`` (the
source investigator's provider-response cache, live/superseded like
``web_fetch``) and ``source_dossier`` (one row per investigated cited work).

Every DB here is a file under ``tmp_path`` or the conftest's temp program
store; nothing touches a live store.
"""

from __future__ import annotations

import sqlite3

import pytest

from trialerror.stores.errors import ValidationError, XidTargetMissingError
from trialerror.stores.migrate import apply_migrations, is_additive, latest_version, read_provenance
from trialerror.stores.schema import knowledge as knowledge_schema
from trialerror.stores.store import TABLE_DB
from trialerror.stores.writer import insert, update
from trialerror.stores.xid import xid_columns_for_table
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now
from tests._ingest_fixtures import bootstrap_launch

_EVIDENCE_COLUMNS = {
    "evidence_id", "subject_key", "provider", "provider_id", "kind", "params_json", "outcome",
    "payload_json", "fetched_ts", "created_by_launch", "superseded_by",
}
_DOSSIER_COLUMNS = {
    "dossier_id", "list_id", "row_id", "seed_raw", "subject_key", "resolution", "held_source_id",
    "mechanical_state", "verdict", "verdict_detail_json", "verdict_by_launch", "verdict_ts",
    "dossier_path", "dossier_sha256", "stage_version", "created_by_launch", "created_ts",
}


def _v15() -> int:
    return next(m.version for m in knowledge_schema.MIGRATIONS if m.name == "knowledge_v15_source_investigation")


def _evidence_row(launch_id: str, **overrides) -> dict:
    row = {
        "evidence_id": new_id("SEVD"),
        "subject_key": "10.9999/widgets",
        "provider": "openalex",
        "provider_id": "W1",
        "kind": "record",
        "params_json": '{"doi": "10.9999/widgets"}',
        "outcome": "record",
        "payload_json": '{"value": {"title": "A Study of Widgets"}}',
        "fetched_ts": now(),
        "created_by_launch": launch_id,
    }
    row.update(overrides)
    return row


def _dossier_row(launch_id: str, **overrides) -> dict:
    row = {
        "dossier_id": new_id("DOSS"),
        "list_id": "list-1",
        "row_id": "row-1",
        "seed_raw": "A. Author, A Study of Widgets, 1986, doi:10.9999/widgets",
        "subject_key": "10.9999/widgets",
        "resolution": "exact",
        "mechanical_state": "open",
        "dossier_path": "dossiers/list-1/row-1/a.json",
        "dossier_sha256": "0" * 64,
        "stage_version": "source-investigator-1",
        "created_by_launch": launch_id,
        "created_ts": now(),
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# the migration
# ---------------------------------------------------------------------------


def test_v15_creates_evidence_and_dossier_tables_and_is_additive(tmp_path):
    """FAILS BEFORE lane SI part B: knowledge stopped at v14 and neither table
    existed. A store one version short, with a source row already in it, gains
    both tables and nothing else about it moves; the migration is CREATE-only,
    classifies as additive, and is recorded as such."""
    v15 = _v15()
    assert v15 == 15
    migration = next(m for m in knowledge_schema.MIGRATIONS if m.version == v15)
    assert is_additive(migration) is True

    conn = sqlite3.connect(tmp_path / "knowledge.db")
    conn.row_factory = sqlite3.Row
    try:
        apply_migrations(conn, tuple(m for m in knowledge_schema.MIGRATIONS if m.version < v15))
        names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert not {"source_evidence", "source_dossier"} & names
        conn.execute(
            "INSERT INTO source (source_id, kind, title, license_tier, acquisition_route, request_state, "
            "registered_ts, registered_by_launch) VALUES ('SRC-example', 'paper', 'Example Paper', 'open', "
            "'author_posted', 'indexed', '2026-01-01T00:00:00Z', 'LNCH-example')"
        )
        conn.commit()

        assert apply_migrations(conn, knowledge_schema.MIGRATIONS) == [
            m.version for m in knowledge_schema.MIGRATIONS if m.version >= v15
        ]
        assert conn.execute("PRAGMA user_version").fetchone()[0] == latest_version(knowledge_schema.MIGRATIONS)
        assert {r["name"] for r in conn.execute("PRAGMA table_info(source_evidence)")} == _EVIDENCE_COLUMNS
        assert {r["name"] for r in conn.execute("PRAGMA table_info(source_dossier)")} == _DOSSIER_COLUMNS
        indexes = {r["name"] for r in conn.execute("PRAGMA index_list(source_evidence)")}
        assert "idx_source_evidence_live" in indexes
        # the existing row is untouched
        assert conn.execute("SELECT title FROM source WHERE source_id = 'SRC-example'").fetchone()[0] == "Example Paper"

        recorded = {row["version"]: row for row in read_provenance(conn)}[v15]
        assert recorded["name"] == "knowledge_v15_source_investigation"
        assert recorded["additive"] == 1
        assert recorded["provenance"] == "recorded"
    finally:
        conn.close()


def test_both_tables_route_to_knowledge_and_register_their_launch_columns():
    assert TABLE_DB["source_evidence"] == "knowledge"
    assert TABLE_DB["source_dossier"] == "knowledge"
    assert set(xid_columns_for_table("source_evidence")) == {"created_by_launch"}
    assert set(xid_columns_for_table("source_dossier")) == {"created_by_launch", "verdict_by_launch"}
    for target in list(xid_columns_for_table("source_evidence").values()) + list(
        xid_columns_for_table("source_dossier").values()
    ):
        assert (target.db, target.table, target.pk_column) == ("platform", "launch", "launch_id")


# ---------------------------------------------------------------------------
# source_dossier
# ---------------------------------------------------------------------------


def test_dossier_unique_per_list_row_seed(store):
    launch_id = bootstrap_launch(store)
    insert(store, "source_dossier", _dossier_row(launch_id))

    with pytest.raises(ValidationError, match="integrity"):
        insert(store, "source_dossier", _dossier_row(launch_id))

    # the same citation in another row, or another list, is another dossier
    insert(store, "source_dossier", _dossier_row(launch_id, row_id="row-2"))
    insert(store, "source_dossier", _dossier_row(launch_id, list_id="list-2"))
    count = store.knowledge.execute("SELECT COUNT(*) FROM source_dossier").fetchone()[0]
    assert count == 3


def test_dossier_verdict_check_rejects_unknown_word(store):
    launch_id = bootstrap_launch(store)
    row = _dossier_row(launch_id)
    insert(store, "source_dossier", row)

    with pytest.raises(ValidationError, match="integrity"):
        update(
            store, "source_dossier", pk_column="dossier_id", pk_value=row["dossier_id"],
            changes={"verdict": "FETCH-IT", "verdict_by_launch": launch_id},
        )
    for word in ("REQUEST", "REQUEST-AS-FOUNDATIONAL", "SUBSTITUTE-WITH", "HELD", "DROP", "NEED-INFO"):
        update(
            store, "source_dossier", pk_column="dossier_id", pk_value=row["dossier_id"],
            changes={"verdict": word, "verdict_by_launch": launch_id},
        )

    with pytest.raises(ValidationError, match="integrity"):
        insert(store, "source_dossier", _dossier_row(launch_id, seed_raw="other", resolution="maybe"))
    with pytest.raises(ValidationError, match="integrity"):
        insert(store, "source_dossier", _dossier_row(launch_id, seed_raw="other", mechanical_state="unknown"))


def test_dossier_launch_columns_are_xid_checked_and_held_source_is_an_fk(store):
    launch_id = bootstrap_launch(store)
    with pytest.raises(XidTargetMissingError):
        insert(store, "source_dossier", _dossier_row("LNCH-nobody"))

    row = _dossier_row(launch_id)
    insert(store, "source_dossier", row)
    with pytest.raises(XidTargetMissingError):
        update(
            store, "source_dossier", pk_column="dossier_id", pk_value=row["dossier_id"],
            changes={"verdict": "DROP", "verdict_by_launch": "LNCH-nobody"},
        )

    with pytest.raises(ValidationError, match="integrity"):
        insert(store, "source_dossier", _dossier_row(launch_id, seed_raw="held", held_source_id="SRC-missing"))


# ---------------------------------------------------------------------------
# source_evidence
# ---------------------------------------------------------------------------


def test_evidence_one_live_row_per_call_any_number_superseded(store):
    """The web_fetch rule: at most one LIVE answer per (subject, provider, kind,
    params); a re-ask retires the live row first, then inserts."""
    launch_id = bootstrap_launch(store)
    first = _evidence_row(launch_id, outcome="rate_limited", payload_json=None)
    insert(store, "source_evidence", first)

    with pytest.raises(ValidationError, match="integrity"):
        insert(store, "source_evidence", _evidence_row(launch_id))

    second = _evidence_row(launch_id)
    update(
        store, "source_evidence", pk_column="evidence_id", pk_value=first["evidence_id"],
        changes={"superseded_by": second["evidence_id"]},
    )
    insert(store, "source_evidence", second)

    # other params, other provider, other kind: other calls
    insert(store, "source_evidence", _evidence_row(launch_id, params_json='{"doi": "10.9999/widgets", "x": 1}'))
    insert(store, "source_evidence", _evidence_row(launch_id, provider="semanticscholar"))
    insert(store, "source_evidence", _evidence_row(launch_id, kind="citing"))

    live = store.knowledge.execute(
        "SELECT COUNT(*) FROM source_evidence WHERE superseded_by IS NULL"
    ).fetchone()[0]
    assert live == 4
    total = store.knowledge.execute("SELECT COUNT(*) FROM source_evidence").fetchone()[0]
    assert total == 5


def test_evidence_kind_is_checked_and_launch_is_xid_checked(store):
    launch_id = bootstrap_launch(store)
    with pytest.raises(ValidationError, match="integrity"):
        insert(store, "source_evidence", _evidence_row(launch_id, kind="fulltext"))
    with pytest.raises(XidTargetMissingError):
        insert(store, "source_evidence", _evidence_row("LNCH-nobody"))
