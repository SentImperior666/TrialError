"""Per-migration proving tests for knowledge-v4 (``web_fetch``) and
jobs-v3 (``job.kind`` gains ``web_fetch``/``web_extract``) — lane a's two
schema numbers, reserved by the orchestrator before the lane started.

Follows ``test_stores_migrate_v7_memory_relation.py``'s pattern, which
follows v4's and v6's before it: fresh-create-vs-step-from-vN schema diff,
per-DDL-change proving tests, failure-path rollback discipline.

Two things here are worth more than the usual additive-migration checks.

The **partial unique index** is the dedup rule of design §5 expressed as a
constraint rather than as a convention the enqueue path is trusted to follow:
at most one LIVE row per canonical URL, unlimited superseded ones behind it.
Every ``refresh`` in the system depends on that index admitting the sequence
it is written for, so the sequence is tested directly.

The **jobs-v3 rebuild** touches a table with a foreign key pointing into it
(``job_event.job_id``), which is the case v2's comment spent thirty lines
explaining. The pre-existing-rows test below is what proves the migration
recipe still works when a program has actually run jobs — the case a
fresh-database test cannot see.
"""

from __future__ import annotations

import sqlite3

import pytest

from trialerror.stores.errors import MigrationError
from trialerror.stores.migrate import Migration, apply_migrations, current_version, latest_version
from trialerror.stores.schema import jobs, knowledge

TS = "2026-09-05T12:00:00.000Z"


def _schema_snapshot(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name"
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def _upto(migrations, max_version: int):
    return tuple(m for m in migrations if m.version <= max_version)


def _knowledge_at(version: int) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migrations(conn, _upto(knowledge.MIGRATIONS, version))
    return conn


def _jobs_at(version: int) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migrations(conn, _upto(jobs.MIGRATIONS, version))
    return conn


def _insert_fetch(conn: sqlite3.Connection, fetch_id: str, **overrides) -> None:
    row: dict[str, object] = {
        "fetch_id": fetch_id,
        "launch_id": "LNCH-1",
        "url": "https://example.org/a",
        "url_norm": "https://example.org/a",
        "kind": "page",
        "origin": "operator_list",
        "state": "queued",
        "created_ts": TS,
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO web_fetch ({cols}) VALUES ({marks})", tuple(row.values()))


# ---------------------------------------------------------------------------
# the migration lists themselves
# ---------------------------------------------------------------------------


def test_knowledge_migrations_are_contiguous_one_through_four():
    versions = [m.version for m in knowledge.MIGRATIONS]
    assert versions == list(range(1, len(versions) + 1))
    assert versions[-1] == 4
    assert len({m.name for m in knowledge.MIGRATIONS}) == len(knowledge.MIGRATIONS)


def test_jobs_migrations_are_contiguous_one_through_three():
    versions = [m.version for m in jobs.MIGRATIONS]
    assert versions == list(range(1, len(versions) + 1))
    assert versions[-1] == 3
    assert len({m.name for m in jobs.MIGRATIONS}) == len(jobs.MIGRATIONS)


def test_the_two_lane_a_migrations_wear_their_reserved_numbers():
    """Reserved by the orchestrator before the lane started, so lanes d/e
    could take theirs. A renumber is exactly the edit that leaves a hole."""
    v4 = next(m for m in knowledge.MIGRATIONS if m.version == 4)
    assert v4.name == "knowledge_v4_web_fetch_table"
    assert v4.statements is knowledge._V4
    v3 = next(m for m in jobs.MIGRATIONS if m.version == 3)
    assert v3.name == "jobs_v3_kind_check_adds_web_fetch_and_web_extract"
    assert v3.statements is jobs._V3


def test_web_fetch_is_declared_in_the_schema_modules_table_list():
    """``TABLE_DB`` is built from these tuples, and the validated write API
    routes on it — a table missing from here is a table ``insert`` cannot
    reach."""
    from trialerror.stores.store import TABLE_DB

    assert "web_fetch" in knowledge.TABLES
    assert TABLE_DB["web_fetch"] == "knowledge"


# ---------------------------------------------------------------------------
# fresh-create vs. step-from-vN
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", [1, 2, 3])
def test_knowledge_stepping_from_any_earlier_version_lands_the_same_schema(prefix: int):
    fresh = sqlite3.connect(":memory:")
    apply_migrations(fresh, knowledge.MIGRATIONS)

    stepped = sqlite3.connect(":memory:")
    apply_migrations(stepped, _upto(knowledge.MIGRATIONS, prefix))
    apply_migrations(stepped, knowledge.MIGRATIONS)

    assert current_version(stepped) == latest_version(knowledge.MIGRATIONS)
    assert _schema_snapshot(stepped) == _schema_snapshot(fresh)


@pytest.mark.parametrize("prefix", [1, 2])
def test_jobs_stepping_from_any_earlier_version_lands_the_same_schema(prefix: int):
    fresh = sqlite3.connect(":memory:")
    apply_migrations(fresh, jobs.MIGRATIONS)

    stepped = sqlite3.connect(":memory:")
    apply_migrations(stepped, _upto(jobs.MIGRATIONS, prefix))
    apply_migrations(stepped, jobs.MIGRATIONS)

    assert current_version(stepped) == latest_version(jobs.MIGRATIONS)
    assert _schema_snapshot(stepped) == _schema_snapshot(fresh)


def test_both_migrations_are_idempotent_reapply_is_noop():
    for module in (knowledge, jobs):
        conn = sqlite3.connect(":memory:")
        apply_migrations(conn, module.MIGRATIONS)
        assert apply_migrations(conn, module.MIGRATIONS) == []
        assert current_version(conn) == latest_version(module.MIGRATIONS)


def test_knowledge_v4_costs_pre_existing_rows_nothing():
    """The case a fresh-database test cannot see: an operator's store with
    real content in it arrives at v3 and must come out the other side with
    every row intact."""
    conn = _knowledge_at(3)
    conn.execute(
        "INSERT INTO source (source_id, kind, title, license_tier, acquisition_route, "
        "request_state, registered_ts, registered_by_launch) "
        "VALUES ('SRC-1','web','a title','open','web','delivered',?,'LNCH-1')",
        (TS,),
    )
    conn.commit()  # close the implicit DML transaction before the migration's BEGIN IMMEDIATE

    before = _schema_snapshot(conn)
    apply_migrations(conn, knowledge.MIGRATIONS)

    assert conn.execute("SELECT title FROM source WHERE source_id='SRC-1'").fetchone()[0] == "a title"
    # Purely additive: every pre-existing object still there, plus the new ones.
    assert set(name for _kind, name, _sql in before) < set(
        name for _kind, name, _sql in _schema_snapshot(conn)
    )


# ---------------------------------------------------------------------------
# knowledge v4: the web_fetch DDL
# ---------------------------------------------------------------------------


def test_web_fetch_carries_every_declared_column():
    conn = _knowledge_at(4)
    cols = {r[1]: r for r in conn.execute("PRAGMA table_info(web_fetch)")}
    assert set(cols) == {
        # identity and the manifest's own fields
        "fetch_id", "job_id", "launch_id", "program_id", "url", "url_norm",
        "final_url", "redirect_chain", "kind", "origin", "list_ref", "state",
        # the result's provenance, flattened
        "outcome", "reason", "http_status", "content_type", "content_class",
        "bytes", "content_sha256", "extracted_sha256", "resolved_ips", "bytes_out",
        "headers_subset", "robots_verdict", "robots_crawl_delay_s",
        "policy_host_rule", "query_stripped", "fetched_ts", "elapsed_ms",
        "sidecar_version", "git_head", "git_ref", "git_path",
        # what it became
        "source_id", "doc_id", "superseded_by", "extractor_version",
        # the extraction signals of design §2.2
        "title", "author", "published", "canonical_link", "license_detected",
        "lang", "extracted_words", "thin_content", "js_markers", "links_json",
        "tdm_signals_json",
        # local files
        "raw_path", "clean_path", "markdown_path", "provenance_path",
        "created_ts", "updated_ts",
    }
    assert cols["fetch_id"][5] == 1  # primary key
    for required in ("launch_id", "url", "url_norm", "kind", "origin", "state", "created_ts"):
        assert cols[required][3] == 1, required


def test_a_refusal_before_any_socket_opened_is_a_storable_row():
    """Everything but identity, the URL pair, the two vocabularies and the
    timestamp is nullable — on purpose. A URL refused on its shape has no
    status, no bytes and no signals, and a schema that could not hold that
    row would force the refusal path to invent values."""
    conn = _knowledge_at(4)
    _insert_fetch(conn, "WF-1", state="refused", outcome="refused", reason="ip_literal")
    row = conn.execute(
        "SELECT http_status, bytes, content_sha256, source_id, doc_id FROM web_fetch"
    ).fetchone()
    assert row == (None, None, None, None, None)


@pytest.mark.parametrize(
    "column,good,bad",
    [
        ("kind", "git", "video"),
        ("origin", "agent", "cron"),
        ("state", "extracted", "done"),
        ("outcome", "unchanged", "maybe"),
        ("content_class", "pdf", "epub"),
        ("robots_verdict", "disallow", "probably"),
    ],
)
def test_every_closed_vocabulary_is_checked_at_the_schema_layer(column, good, bad):
    conn = _knowledge_at(4)
    _insert_fetch(conn, f"WF-good-{column}", **{column: good})
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_fetch(conn, f"WF-bad-{column}", **{column: bad})


def test_the_reason_column_is_deliberately_not_a_ddl_vocabulary():
    """The closed reason list lives in ``trialerror.webfetch.REASONS`` and is
    enforced by ``check_reason`` on every construction. Duplicating it as a
    CHECK would mean a migration every time a reason is added, and two lists
    that can disagree — which is the failure the single vocabulary exists to
    prevent."""
    conn = _knowledge_at(4)
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='web_fetch'").fetchone()[0]
    assert "reason" in sql
    assert "robots_disallow" not in sql


def test_at_most_one_live_row_per_canonical_url():
    conn = _knowledge_at(4)
    _insert_fetch(conn, "WF-1")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        _insert_fetch(conn, "WF-2")


def test_superseding_frees_the_url_and_keeps_the_history():
    """The exact sequence ``refresh`` performs, and the reason the index
    predicate is partial: the old row stops being live BEFORE the new one is
    inserted, and both rows survive."""
    conn = _knowledge_at(4)
    _insert_fetch(conn, "WF-1", state="extracted")
    conn.execute("UPDATE web_fetch SET superseded_by='WF-2' WHERE fetch_id='WF-1'")
    _insert_fetch(conn, "WF-2", state="queued")

    assert conn.execute("SELECT count(*) FROM web_fetch").fetchone()[0] == 2
    live = conn.execute(
        "SELECT fetch_id FROM web_fetch WHERE url_norm='https://example.org/a' "
        "AND superseded_by IS NULL"
    ).fetchall()
    assert [r[0] for r in live] == ["WF-2"]


def test_many_superseded_rows_may_share_one_url():
    conn = _knowledge_at(4)
    for i in range(4):
        _insert_fetch(conn, f"WF-{i}", superseded_by=f"WF-{i + 1}")
    _insert_fetch(conn, "WF-live")
    assert conn.execute("SELECT count(*) FROM web_fetch").fetchone()[0] == 5


def test_superseded_by_is_deliberately_not_a_foreign_key():
    """Stated as a test because it is a departure: SQLite checks same-file FKs
    immediately, and superseding is two statements whose order the partial
    index fixes, so the pointer would name a row that does not exist yet. The
    house precedent is ``verdict.subject_id`` and ``summary.subject_id``,
    both non-FK id columns checked by doctor rather than by the DDL."""
    conn = _knowledge_at(4)
    _insert_fetch(conn, "WF-1", superseded_by="WF-never-created")
    assert (
        conn.execute("SELECT superseded_by FROM web_fetch").fetchone()[0] == "WF-never-created"
    )


def test_source_and_doc_pointers_are_enforced_foreign_keys():
    """These two ARE FKs: they are written once, after the row they point at
    exists, so there is no ordering problem to trade away."""
    conn = _knowledge_at(4)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        _insert_fetch(conn, "WF-1", source_id="SRC-ghost")


def test_v4_creates_its_lookup_indexes():
    conn = _knowledge_at(4)
    names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='web_fetch'"
        )
    }
    assert {
        "idx_web_fetch_url_norm_live",
        "idx_web_fetch_state",
        "idx_web_fetch_source",
        "idx_web_fetch_superseded",
    } <= names


def test_web_fetch_launch_id_is_a_registered_xid():
    """Design §4 T7 as a write-API refusal rather than a convention."""
    from trialerror.stores.xid import xid_columns_for_table

    columns = xid_columns_for_table("web_fetch")
    assert set(columns) == {"launch_id"}
    assert columns["launch_id"].table == "launch"
    assert "job_id" not in columns, "jobs.db rows are swept; a fetch record outlives its job"


# ---------------------------------------------------------------------------
# jobs v3: the kind CHECK
# ---------------------------------------------------------------------------


def test_jobs_v3_accepts_the_two_web_kinds():
    conn = _jobs_at(3)
    for kind in ("web_fetch", "web_extract"):
        conn.execute(
            "INSERT INTO job (job_id, kind, payload, state, created_ts) VALUES (?,?,'{}','pending',?)",
            (f"JOB-{kind}", kind, TS),
        )
    assert conn.execute("SELECT count(*) FROM job").fetchone()[0] == 2


def test_jobs_v3_keeps_every_kind_v2_allowed():
    conn = _jobs_at(3)
    for kind in (
        "ocr", "embed", "index", "extract", "ingest_batch", "watch", "custom", "normalize", "chunk"
    ):
        conn.execute(
            "INSERT INTO job (job_id, kind, payload, state, created_ts) VALUES (?,?,'{}','pending',?)",
            (f"JOB-{kind}", kind, TS),
        )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        conn.execute(
            "INSERT INTO job (job_id, kind, payload, state, created_ts) "
            "VALUES ('JOB-x','web_crawl','{}','pending',?)",
            (TS,),
        )


def test_jobs_v3_rebuild_preserves_rows_and_the_referencing_job_event_table():
    """The case v2's comment is about: ``job_event.job_id`` references
    ``job``, and the rebuild DROPs ``job``. A program that has run any job at
    all has rows on both sides."""
    conn = _jobs_at(2)
    conn.execute(
        "INSERT INTO job (job_id, kind, payload, state, attempts, created_ts) "
        "VALUES ('JOB-old','embed','{\"a\":1}','complete',2,?)",
        (TS,),
    )
    conn.execute(
        "INSERT INTO job_event (job_id, ts, type, detail) VALUES ('JOB-old',?,'enqueued','x')",
        (TS,),
    )
    conn.commit()

    apply_migrations(conn, jobs.MIGRATIONS)

    job = conn.execute("SELECT kind, payload, state, attempts FROM job").fetchone()
    assert job == ("embed", '{"a":1}', "complete", 2)
    assert conn.execute("SELECT count(*) FROM job_event").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1  # toggled back ON


def test_jobs_v3_leaves_the_state_index_in_place():
    conn = _jobs_at(3)
    names = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='job'")
    }
    assert "idx_job_state" in names
    assert "job__v3new" not in {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


# ---------------------------------------------------------------------------
# failure-path rollback discipline
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module,previous",
    [(knowledge, 3), (jobs, 2)],
    ids=["knowledge_v4", "jobs_v3"],
)
def test_a_failed_migration_does_not_advance_the_version_or_partially_apply(module, previous):
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, _upto(module.MIGRATIONS, previous))
    before = _schema_snapshot(conn)

    real = next(m for m in module.MIGRATIONS if m.version == previous + 1)
    broken = Migration(
        version=previous + 1,
        name=real.name + "_broken_for_test",
        statements=real.statements + ("THIS IS NOT SQL",),
    )
    with pytest.raises(MigrationError, match="failed"):
        apply_migrations(conn, [broken])

    assert current_version(conn) == previous
    assert _schema_snapshot(conn) == before
    conn.execute("BEGIN")
    conn.execute("ROLLBACK")  # would raise if a transaction leaked
