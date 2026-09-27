"""Lane FB-acq item 5 (F17), the read half: the doctor says WHICH WAY a
schema-version mismatch points.

``store_schema_version`` used to compute ``current == expected`` and fail on
anything else. "This store has not been migrated yet" and "this store was
migrated by a newer client than the one reading it" therefore arrived as the
same flat failure with the same two numbers -- although they call for opposite
actions, and only one of them is dangerous.

This module pins the three directions: older store (fail, migrate it), newer
store whose extra migrations are all additive and all accounted for (warn,
upgrade the client), and newer store with a non-additive or unaccounted-for
step (fail, do not write to it). Plus the one thing that must not move: a
matching DB's details dict still holds exactly its three keys.

Every DB here is a file under ``tmp_path``, via the suite's own
``program_root`` / ``platform_root`` fixtures.
"""

from __future__ import annotations

import sqlite3

import pytest

from trialerror.stores import paths
from trialerror.stores.migrate import PROVENANCE_TABLE, latest_version
from trialerror.stores.schema import knowledge as knowledge_schema
from trialerror.stores.store import SCHEMA_MODULES, open_store
from trialerror.util.doctor import DoctorContext, discover_and_register_checks, run_checks

_DB_KINDS = ("platform", "ops", "knowledge", "jobs")


def _run(program_root, platform_root):
    discover_and_register_checks()
    ctx = DoctorContext(program_root=program_root, platform_root=platform_root)
    results = run_checks(ctx, only=["store_schema_version"])
    return {r.name: r for r in results}["store_schema_version"]


@pytest.fixture()
def migrated(program_root, platform_root):
    """A program whose four DBs are all on their expected versions."""
    store = open_store(program_root, platform_root=platform_root)
    store.close()
    return program_root


def _knowledge_path(program_root):
    return paths.knowledge_db_path(program_root)


def _set_version(path, version: int) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"PRAGMA user_version = {version:d}")
        conn.commit()
    finally:
        conn.close()


def _plant_provenance(path, *, version: int, additive: int, name: str = "future_migration") -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            f"INSERT OR REPLACE INTO {PROVENANCE_TABLE} "
            "(version, name, additive, applied_ts, client_version, client_schema_latest, provenance) "
            "VALUES (?, ?, ?, '2020-01-01T00:00:00.000Z', 'newer-client', ?, 'recorded')",
            (version, name, additive, version),
        )
        conn.commit()
    finally:
        conn.close()


def _latest(db_kind: str) -> int:
    return latest_version(SCHEMA_MODULES[db_kind].MIGRATIONS)


# ---------------------------------------------------------------------------
# the matching case must not move
# ---------------------------------------------------------------------------


def test_a_matching_store_reports_exactly_the_three_keys_it_always_did(migrated, platform_root):
    r = _run(migrated, platform_root)
    assert r.status == "pass"
    for db_kind in _DB_KINDS:
        expected = _latest(db_kind)
        assert r.details[db_kind] == {
            "current_version": expected,
            "expected_version": expected,
            "match": True,
        }, db_kind
    assert r.message == "all present DB(s) on their expected schema version"


# ---------------------------------------------------------------------------
# store older than the client
# ---------------------------------------------------------------------------


def test_a_store_behind_the_client_fails_and_says_to_migrate_it(migrated, platform_root):
    path = _knowledge_path(migrated)
    _set_version(path, _latest("knowledge") - 1)
    r = _run(migrated, platform_root)
    assert r.status == "fail"
    details = r.details["knowledge"]
    assert details["direction"] == "store_older"
    assert details["match"] is False
    assert "additive_only" not in details
    assert "this store is older than the client" in r.message
    assert "to migrate it" in r.message


# ---------------------------------------------------------------------------
# store ahead of the client
# ---------------------------------------------------------------------------


def test_a_store_ahead_of_the_client_by_an_additive_migration_only_warns(migrated, platform_root):
    path = _knowledge_path(migrated)
    future = _latest("knowledge") + 1
    _plant_provenance(path, version=future, additive=1)
    _set_version(path, future)

    r = _run(migrated, platform_root)
    assert r.status == "warn"
    details = r.details["knowledge"]
    assert details["direction"] == "store_newer"
    assert details["additive_only"] is True
    assert details["newer_migrations"] == [
        {"version": future, "name": "future_migration", "additive": 1}
    ]
    assert "this client is older than the store: upgrade the client" in r.message


def test_a_non_additive_newer_migration_is_a_failure(migrated, platform_root):
    path = _knowledge_path(migrated)
    future = _latest("knowledge") + 1
    _plant_provenance(path, version=future, additive=0)
    _set_version(path, future)

    r = _run(migrated, platform_root)
    assert r.status == "fail"
    details = r.details["knowledge"]
    assert details["direction"] == "store_newer"
    assert details["additive_only"] is False
    assert "are not known to be additive" in r.message


def test_a_newer_store_with_no_provenance_row_is_a_failure(migrated, platform_root):
    """The store is ahead and nothing says what changed -- the runner that
    applied it predates the history table, or a newer client holds a migration
    this one has no name for. Either way this client must not write."""
    path = _knowledge_path(migrated)
    _set_version(path, _latest("knowledge") + 1)

    r = _run(migrated, platform_root)
    assert r.status == "fail"
    details = r.details["knowledge"]
    assert details["direction"] == "store_newer"
    assert details["additive_only"] is None
    assert details["newer_migrations"] == []
    assert "are not known to be additive" in r.message


def test_a_gap_in_the_newer_history_is_a_failure_even_when_the_rows_present_are_additive(
    migrated, platform_root
):
    """Two versions ahead with only the SECOND one recorded: the first is
    unaccounted for, so "all additive" is a claim about a history with a hole
    in it."""
    path = _knowledge_path(migrated)
    latest = _latest("knowledge")
    _plant_provenance(path, version=latest + 2, additive=1, name="second_future")
    _set_version(path, latest + 2)

    r = _run(migrated, platform_root)
    assert r.status == "fail"
    assert r.details["knowledge"]["additive_only"] is None


def test_the_rest_of_the_dbs_are_unaffected_by_one_drifting_db(migrated, platform_root):
    path = _knowledge_path(migrated)
    future = _latest("knowledge") + 1
    _plant_provenance(path, version=future, additive=1)
    _set_version(path, future)

    r = _run(migrated, platform_root)
    for db_kind in ("platform", "ops", "jobs"):
        expected = _latest(db_kind)
        assert r.details[db_kind] == {
            "current_version": expected,
            "expected_version": expected,
            "match": True,
        }, db_kind


def test_a_fail_anywhere_outranks_a_warn_elsewhere(migrated, platform_root):
    knowledge_path = _knowledge_path(migrated)
    future = _latest("knowledge") + 1
    _plant_provenance(knowledge_path, version=future, additive=1)
    _set_version(knowledge_path, future)
    _set_version(paths.jobs_db_path(migrated), _latest("jobs") - 1)

    r = _run(migrated, platform_root)
    assert r.status == "fail"
    assert r.details["knowledge"]["additive_only"] is True
    assert r.details["jobs"]["direction"] == "store_older"


def test_the_expected_version_is_read_from_the_schema_module_not_hardcoded(migrated, platform_root):
    """Guards this module itself: every number above comes from
    ``latest_version(MIGRATIONS)``, so a future schema bump does not silently
    turn these tests into assertions about a version nobody is on."""
    r = _run(migrated, platform_root)
    assert r.details["knowledge"]["expected_version"] == latest_version(
        knowledge_schema.MIGRATIONS
    )
