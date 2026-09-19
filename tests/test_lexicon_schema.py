"""Per-migration proving tests for knowledge-v5, the lexicon term store
(``docs/reviews/LANE_E_TERM_STORE_DESIGN.md`` §2; ruling L-E1).

Follows ``test_stores_migrate_v4_web_fetch.py``'s shape -- fresh-create vs
step-from-vN, per-DDL-change proving tests, failure-path rollback -- and
adds two things that shape is not usually asked for.

**The vocabularies are checked against the mirror, not just against
themselves.** ``trialerror.lexicon.policy`` re-declares every CHECK
constraint in this migration as a Python tuple, because the write API
refuses a bad value with a named error before SQLite refuses it with an
integrity violation. A mirror that can drift is worse than no mirror, so
each pair is asserted to agree here: if a later migration widens a CHECK
and forgets the tuple, this file is what says so.

**The three index shapes that are load-bearing get their own tests.** The
partial unique on ``(origin_kind, origin_ref)`` is what makes every
proposal route idempotent by construction; the expression unique on
``COALESCE(anchor_id, ref_id)`` is what stops one anchor being attached to
a sense twice through two different columns; and the DELIBERATE ABSENCE of
a unique on ``term_relation(src, dst)`` is what keeps two actors'
disagreement representable. All three are the kind of thing a later
"tidy-up" removes, so all three say why they are there.
"""

from __future__ import annotations

import sqlite3

import pytest

from trialerror.lexicon import policy
from trialerror.stores.bitemporal import BITEMPORAL_TABLES
from trialerror.stores.errors import MigrationError
from trialerror.stores.migrate import Migration, apply_migrations, current_version, latest_version
from trialerror.stores.schema import knowledge
from trialerror.stores.store import TABLE_DB
from trialerror.stores.xid import XID_REGISTRY, XidTarget, xid_columns_for_table

TS = "2026-09-06T12:00:00.000Z"

LEXICON_TABLES = ("term", "term_alias", "term_sense", "term_sense_evidence", "term_relation")


def _upto(migrations, max_version: int):
    return tuple(m for m in migrations if m.version <= max_version)


def _knowledge_at(version: int) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migrations(conn, _upto(knowledge.MIGRATIONS, version))
    return conn


def _schema_snapshot(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def _insert(conn: sqlite3.Connection, table: str, **row) -> None:
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))


def _term(conn: sqlite3.Connection, term_id: str = "TERM-1", **overrides) -> str:
    row = {
        "term_id": term_id,
        "lemma": term_id.lower(),
        "lemma_norm": term_id.lower(),
        "status": "proposed",
        "created_by_launch": "LNCH-1",
        "created_at": TS,
    }
    row.update(overrides)
    _insert(conn, "term", **row)
    return str(row["term_id"])


def _sense(conn: sqlite3.Connection, sense_id: str = "SENSE-1", term_id: str = "TERM-1", **overrides) -> str:
    row = {
        "sense_id": sense_id,
        "term_id": term_id,
        "gloss": "a reading",
        "origin_kind": "manual",
        "procedure_version": "manual-v1",
        "status": "proposed",
        "created_at": TS,
        "proposed_by_launch": "LNCH-1",
    }
    row.update(overrides)
    _insert(conn, "term_sense", **row)
    return str(row["sense_id"])


def _evidence(conn: sqlite3.Connection, evidence_id: str = "TSE-1", sense_id: str = "SENSE-1", **overrides) -> str:
    row = {
        "evidence_id": evidence_id,
        "sense_id": sense_id,
        "evidence_kind": "record",
        "ref_id": "REC-1",
        "source_key": "register-a",
        "created_by_launch": "LNCH-1",
        "created_ts": TS,
    }
    row.update(overrides)
    _insert(conn, "term_sense_evidence", **row)
    return str(row["evidence_id"])


def _relation(conn: sqlite3.Connection, rel_id: str = "TREL-1", **overrides) -> str:
    row = {
        "rel_id": rel_id,
        "src_kind": "term",
        "src_id": "TERM-1",
        "dst_kind": "term",
        "dst_id": "TERM-2",
        "verb": "same_as",
        "status": "pending",
        "marked_by_kind": "system",
        "marked_ts": TS,
    }
    row.update(overrides)
    _insert(conn, "term_relation", **row)
    return str(row["rel_id"])


# ---------------------------------------------------------------------------
# the migration list itself
# ---------------------------------------------------------------------------


def test_knowledge_migrations_are_contiguous_and_include_lane_es_fifth():
    """Contiguity and unique names are permanent claims. "v5 is the LAST
    one" was not: the ideation lane's knowledge v6 followed. What this test
    still has to hold is that lane e's migration is there, at 5, with no
    hole in front of it -- pinning the tail number instead would only ever
    have failed on the next lane, which is not what it was protecting."""
    versions = [m.version for m in knowledge.MIGRATIONS]
    assert versions == list(range(1, len(versions) + 1))
    assert versions[-1] >= 5
    assert len({m.name for m in knowledge.MIGRATIONS}) == len(knowledge.MIGRATIONS)
    v5 = next(m for m in knowledge.MIGRATIONS if m.version == 5)
    assert v5.name == "knowledge_v5_lexicon_term_store"


def test_lane_e_wears_the_number_ruling_l_e1_gave_it():
    """L-E1: knowledge v5, following lane a's REAL v4. The ordering rule the
    ruling attached to it ("if lane a is still unmerged, lane e lands as v4
    and lane a renumbers") is discharged by v4 existing and being lane a's."""
    v4 = next(m for m in knowledge.MIGRATIONS if m.version == 4)
    assert v4.name == "knowledge_v4_web_fetch_table"
    v5 = next(m for m in knowledge.MIGRATIONS if m.version == 5)
    assert v5.name == "knowledge_v5_lexicon_term_store"
    assert v5.statements is knowledge._V5


def test_the_five_base_tables_are_declared_and_routed():
    for table in LEXICON_TABLES:
        assert table in knowledge.TABLES, table
        assert TABLE_DB[table] == "knowledge", table


def test_term_fts_is_a_virtual_table_and_deliberately_not_in_tables():
    """Same treatment ``chunk_fts`` gets. ``TABLES`` feeds ``TABLE_DB``, the
    validated write API's routing map, and the one-row-per-table round-trip
    fixture -- an FTS5 shadow with no row of its own belongs in neither."""
    assert "term_fts" not in knowledge.TABLES
    assert "term_fts" not in TABLE_DB
    conn = _knowledge_at(5)
    kinds = {
        r[0]
        for r in conn.execute("SELECT type FROM sqlite_master WHERE name = 'term_fts'").fetchall()
    }
    assert kinds == {"table"}  # FTS5 registers as a table with a module


# ---------------------------------------------------------------------------
# fresh-create vs step-from-vN, idempotency, pre-existing rows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", [1, 2, 3, 4])
def test_stepping_from_any_earlier_version_lands_the_same_schema(prefix: int):
    fresh = sqlite3.connect(":memory:")
    apply_migrations(fresh, knowledge.MIGRATIONS)

    stepped = sqlite3.connect(":memory:")
    apply_migrations(stepped, _upto(knowledge.MIGRATIONS, prefix))
    apply_migrations(stepped, knowledge.MIGRATIONS)

    # Written against latest_version rather than the literal 5 it started
    # at: the claim is "stepping up lands where a fresh create lands", and
    # that claim outlives the number.
    assert current_version(stepped) == latest_version(knowledge.MIGRATIONS)
    assert _schema_snapshot(stepped) == _schema_snapshot(fresh)


def test_reapplying_the_migration_list_is_a_noop():
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, knowledge.MIGRATIONS)
    assert apply_migrations(conn, knowledge.MIGRATIONS) == []
    assert current_version(conn) == latest_version(knowledge.MIGRATIONS)


def test_v5_costs_pre_existing_rows_nothing():
    """The case a fresh-database test cannot see: a store with real content
    in it arrives at v4 and comes out with every row and every object still
    there, plus the new ones. Purely additive is the whole claim."""
    conn = _knowledge_at(4)
    conn.execute(
        "INSERT INTO source (source_id, kind, title, license_tier, acquisition_route, "
        "request_state, registered_ts, registered_by_launch) "
        "VALUES ('SRC-1','book','a title','open','web','indexed',?,'LNCH-1')",
        (TS,),
    )
    conn.commit()

    before = _schema_snapshot(conn)
    apply_migrations(conn, knowledge.MIGRATIONS)

    assert conn.execute("SELECT title FROM source WHERE source_id='SRC-1'").fetchone()[0] == "a title"
    assert {name for _k, name, _s in before} < {name for _k, name, _s in _schema_snapshot(conn)}


def test_a_failed_v5_does_not_advance_the_version_or_partially_apply():
    conn = _knowledge_at(4)
    before = _schema_snapshot(conn)
    real = next(m for m in knowledge.MIGRATIONS if m.version == 5)
    broken = Migration(version=5, name=real.name + "_broken_for_test", statements=real.statements + ("NOT SQL",))
    with pytest.raises(MigrationError, match="failed"):
        apply_migrations(conn, [broken])
    assert current_version(conn) == 4
    assert _schema_snapshot(conn) == before
    conn.execute("BEGIN")
    conn.execute("ROLLBACK")  # would raise if a transaction leaked


# ---------------------------------------------------------------------------
# column sets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "table,columns,required",
    [
        (
            "term",
            {
                "term_id", "lemma", "lemma_norm", "granularity", "tags", "entity_id", "status",
                "preferred_sense_id", "merged_into", "created_by_launch", "created_at", "updated_ts",
            },
            ("lemma", "lemma_norm", "status", "created_by_launch", "created_at"),
        ),
        (
            "term_alias",
            {"alias_id", "term_id", "alias", "alias_norm", "kind", "created_by_launch", "created_ts"},
            ("term_id", "alias", "alias_norm", "kind", "created_by_launch", "created_ts"),
        ),
        (
            "term_sense",
            {
                "sense_id", "term_id", "gloss", "disambiguator", "origin_kind", "origin_ref",
                "confidence", "procedure_version", "status", "created_at", "expired_at", "valid_at",
                "invalid_at", "superseded_by", "proposed_by_launch", "decided_by_launch",
                "decided_ts", "review_after", "reviewed_ts",
            },
            ("term_id", "gloss", "origin_kind", "procedure_version", "status", "created_at", "proposed_by_launch"),
        ),
        (
            "term_sense_evidence",
            {
                "evidence_id", "sense_id", "evidence_kind", "anchor_id", "ref_id", "source_key",
                "cite_raw", "excerpt", "created_by_launch", "created_ts", "retracted_ts",
                "retracted_reason",
            },
            ("sense_id", "evidence_kind", "source_key", "created_by_launch", "created_ts"),
        ),
        (
            "term_relation",
            {
                "rel_id", "src_kind", "src_id", "dst_kind", "dst_id", "verb", "decided_verb",
                "status", "reason", "evidence", "confidence", "marked_by_kind", "marked_by_launch",
                "marked_by_model", "marked_ts", "decided_by_launch", "decided_ts", "superseded_by",
            },
            ("src_kind", "src_id", "dst_kind", "dst_id", "verb", "status", "marked_by_kind", "marked_ts"),
        ),
    ],
)
def test_each_table_carries_exactly_its_declared_columns(table, columns, required):
    conn = _knowledge_at(5)
    info = {r[1]: r for r in conn.execute(f"PRAGMA table_info({table})")}
    assert set(info) == columns
    for name in required:
        assert info[name][3] == 1, f"{table}.{name} should be NOT NULL"


def test_term_sense_carries_the_full_bitemporal_column_set_and_is_registered():
    """The same four columns ``claim`` and ``relation`` carry, and the same
    module drives them -- a corrected gloss is a transaction-time supersede,
    a reading that stopped being used is an event-time end."""
    conn = _knowledge_at(5)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(term_sense)")}
    assert {"created_at", "expired_at", "valid_at", "invalid_at", "superseded_by"} <= cols
    assert BITEMPORAL_TABLES["term_sense"] == "sense_id"


# ---------------------------------------------------------------------------
# closed vocabularies: DDL and the policy mirror must agree
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "table,column,mirror,good,bad",
    [
        ("term", "status", policy.TERM_STATUSES, "merged", "archived"),
        ("term", "granularity", policy.GRANULARITIES, "family", "level-1"),
        ("term_alias", "kind", policy.ALIAS_KINDS, "abbreviation", "nickname"),
        ("term_sense", "origin_kind", policy.ORIGIN_KINDS, "record_import", "import"),
        ("term_sense", "status", policy.SENSE_STATUSES, "superseded", "stale"),
        ("term_sense_evidence", "evidence_kind", policy.EVIDENCE_KINDS, "idea", "note"),
        ("term_relation", "verb", policy.RELATION_VERBS, "not_conflict", "maybe"),
        ("term_relation", "decided_verb", policy.RELATION_VERBS, "scoped", "maybe"),
        ("term_relation", "status", policy.RELATION_STATUSES, "confirmed", "open"),
        ("term_relation", "marked_by_kind", policy.MARKED_BY_KINDS, "launch", "operator"),
    ],
)
def test_every_closed_vocabulary_is_checked_by_the_ddl_and_mirrored_in_policy(
    table, column, mirror, good, bad
):
    conn = _knowledge_at(5)
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
    for value in mirror:
        assert f"'{value}'" in sql, f"{table}.{column}: policy lists {value!r}, the DDL does not"
    assert good in mirror

    builder = {
        "term": lambda **kw: _term(conn, "TERM-good", **kw),
        "term_alias": lambda **kw: _insert(
            conn, "term_alias", alias_id="ALIAS-good", term_id="TERM-1", alias="a",
            alias_norm="a", created_by_launch="LNCH-1", created_ts=TS,
            **({"kind": "variant"} | kw),
        ),
        "term_sense": lambda **kw: _sense(conn, "SENSE-good", **kw),
        "term_sense_evidence": lambda **kw: _evidence(conn, "TSE-good", **kw),
        "term_relation": lambda **kw: _relation(conn, "TREL-good", **kw),
    }[table]
    _term(conn)
    if table in ("term_sense_evidence",):
        _sense(conn)

    builder(**{column: good})
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        builder(**{column: bad})


def test_relation_decisions_are_a_subset_of_the_verbs_plus_rejected():
    """``decide_relation`` accepts less than the DDL allows, on purpose:
    ``conflicts_with`` is the question a conflict item asks and
    ``supersedes`` is a sense-level operation with its own verb, so neither
    is offered as a resolution."""
    decisions = set(policy.RELATION_DECISIONS)
    assert decisions - {"rejected"} < set(policy.RELATION_VERBS)
    assert "conflicts_with" not in decisions
    assert "supersedes" not in decisions
    assert "rejected" in decisions and "rejected" not in policy.RELATION_VERBS


# ---------------------------------------------------------------------------
# the three load-bearing index shapes
# ---------------------------------------------------------------------------


def test_one_origin_can_back_only_one_sense():
    """The partial unique index IS the idempotency of every proposal route:
    a re-run of the register import inserts nothing, without the backfill
    having to remember to check first."""
    conn = _knowledge_at(5)
    _term(conn)
    _sense(conn, "SENSE-1", origin_kind="record_import", origin_ref="REC-1")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        _sense(conn, "SENSE-2", origin_kind="record_import", origin_ref="REC-1")


def test_the_same_ref_under_a_different_origin_kind_is_a_different_key():
    conn = _knowledge_at(5)
    _term(conn)
    _sense(conn, "SENSE-1", origin_kind="record_import", origin_ref="X-1")
    _sense(conn, "SENSE-2", origin_kind="extract", origin_ref="X-1")
    assert conn.execute("SELECT count(*) FROM term_sense").fetchone()[0] == 2


def test_manual_senses_have_no_origin_and_therefore_never_collide():
    """The predicate is partial for exactly this: the hand-written route has
    no origin to be idempotent about, and any number of its rows must
    coexist."""
    conn = _knowledge_at(5)
    _term(conn)
    for i in range(4):
        _sense(conn, f"SENSE-{i}", origin_kind="manual", origin_ref=None)
    assert conn.execute("SELECT count(*) FROM term_sense").fetchone()[0] == 4


def test_one_anchor_cannot_back_one_sense_twice():
    conn = _knowledge_at(5)
    _term(conn)
    _sense(conn)
    _evidence(conn, "TSE-1", evidence_kind="claim", ref_id="CLM-1", anchor_id=None)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        _evidence(conn, "TSE-2", evidence_kind="claim", ref_id="CLM-1", anchor_id=None)


def test_the_evidence_identity_index_spans_both_id_columns():
    """``COALESCE(anchor_id, ref_id)``: the identifying column differs by
    kind, and a plain three-column UNIQUE would let one id be attached twice
    -- once through each column."""
    conn = _knowledge_at(5)
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'idx_term_sense_evidence_identity'"
    ).fetchone()[0]
    assert "COALESCE(anchor_id, ref_id)" in sql


def test_two_senses_may_cite_the_same_source_and_even_the_same_row():
    """Uniqueness is per-sense. Shared evidence between two senses is not a
    duplicate -- it is precisely the signal the conflict rule reads as
    "nuance, not conflict"."""
    conn = _knowledge_at(5)
    _term(conn)
    _sense(conn, "SENSE-1")
    _sense(conn, "SENSE-2")
    _evidence(conn, "TSE-1", sense_id="SENSE-1", ref_id="REC-1")
    _evidence(conn, "TSE-2", sense_id="SENSE-2", ref_id="REC-1")
    assert conn.execute("SELECT count(*) FROM term_sense_evidence").fetchone()[0] == 2


def test_term_relation_deliberately_has_no_unique_on_src_dst():
    """Two actors disagreeing about the same pair is a fact about the
    program, and a unique constraint would make it unrepresentable (the
    engram schema's own comment). Written as a test because a later
    "obviously missing index" cleanup is exactly how it would be lost."""
    conn = _knowledge_at(5)
    _relation(conn, "TREL-1", verb="same_as", marked_by_kind="system")
    _relation(conn, "TREL-2", verb="same_as", marked_by_kind="launch", marked_by_launch="LNCH-1")
    _relation(conn, "TREL-3", verb="unrelated", marked_by_kind="launch", marked_by_launch="LNCH-2")
    assert conn.execute("SELECT count(*) FROM term_relation").fetchone()[0] == 3


def test_one_alias_key_per_term_but_the_same_spelling_may_serve_two_terms():
    conn = _knowledge_at(5)
    _term(conn, "TERM-1")
    _term(conn, "TERM-2")
    _insert(
        conn, "term_alias", alias_id="ALIAS-1", term_id="TERM-1", alias="Save",
        alias_norm="save", kind="variant", created_by_launch="LNCH-1", created_ts=TS,
    )
    _insert(
        conn, "term_alias", alias_id="ALIAS-2", term_id="TERM-2", alias="save",
        alias_norm="save", kind="variant", created_by_launch="LNCH-1", created_ts=TS,
    )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        _insert(
            conn, "term_alias", alias_id="ALIAS-3", term_id="TERM-1", alias="SAVE",
            alias_norm="save", kind="plural", created_by_launch="LNCH-1", created_ts=TS,
        )


def test_lemma_norm_is_unique_and_that_uniqueness_is_the_lemma_index():
    """The design asks for an index on ``lemma_norm``; the UNIQUE constraint
    already materializes one. A second explicit CREATE INDEX would be a
    duplicate B-tree maintained on every write for no read that could not
    use the first."""
    conn = _knowledge_at(5)
    _term(conn, "TERM-1", lemma_norm="save")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        _term(conn, "TERM-2", lemma_norm="save")
    autoindexes = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='term' "
            "AND name LIKE 'sqlite_autoindex%'"
        )
    ]
    assert autoindexes, "the UNIQUE(lemma_norm) constraint must carry its own index"
    assert "idx_term_lemma_norm" not in {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }


def test_v5_creates_its_lookup_indexes():
    conn = _knowledge_at(5)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {
        "idx_term_status",
        "idx_term_granularity",
        "idx_term_alias_norm",
        "idx_term_sense_origin",
        "idx_term_sense_term_status",
        "idx_term_sense_expired",
        "idx_term_sense_review_after",
        "idx_term_sense_evidence_identity",
        "idx_term_sense_evidence_sense",
        "idx_term_sense_evidence_source",
        "idx_term_relation_status_verb",
        "idx_term_relation_src",
        "idx_term_relation_dst",
    } <= names


# ---------------------------------------------------------------------------
# foreign keys -- which are enforced and which deliberately are not
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda c: _sense(c, "SENSE-x", term_id="TERM-ghost"), id="term_sense.term_id"),
        pytest.param(
            lambda c: _insert(
                c, "term_alias", alias_id="ALIAS-x", term_id="TERM-ghost", alias="a",
                alias_norm="a", kind="variant", created_by_launch="LNCH-1", created_ts=TS,
            ),
            id="term_alias.term_id",
        ),
        pytest.param(
            lambda c: _evidence(c, "TSE-x", sense_id="SENSE-ghost"), id="term_sense_evidence.sense_id"
        ),
        pytest.param(
            lambda c: _evidence(c, "TSE-y", evidence_kind="quote_anchor", anchor_id="ANC-ghost", ref_id=None),
            id="term_sense_evidence.anchor_id",
        ),
        pytest.param(lambda c: _term(c, "TERM-x", entity_id="ENT-ghost"), id="term.entity_id"),
        pytest.param(lambda c: _term(c, "TERM-y", merged_into="TERM-ghost"), id="term.merged_into"),
    ],
)
def test_the_same_file_pointers_that_are_foreign_keys_are_enforced(call):
    conn = _knowledge_at(5)
    _term(conn)
    _sense(conn)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        call(conn)


def test_preferred_sense_id_is_deliberately_not_a_foreign_key():
    """A term's preferred sense is written in the same breath as the term,
    and the sense's ``term_id`` points back -- so one of the two pointers
    must name a row that does not exist yet. Same house precedent as
    ``verdict.subject_id`` and ``web_fetch.superseded_by``; validated in
    code and by doctor, not by the DDL."""
    conn = _knowledge_at(5)
    _term(conn, "TERM-1", preferred_sense_id="SENSE-not-yet")
    assert (
        conn.execute("SELECT preferred_sense_id FROM term").fetchone()[0] == "SENSE-not-yet"
    )


def test_origin_ref_is_deliberately_not_a_foreign_key():
    """Polymorphic by ``origin_kind`` -- a claim_id, a record_id or an
    idea_id. A real FK cannot point at one of three tables depending on a
    sibling column, and enforcing it for one case only would be a half-truth
    worse than stating the contract."""
    conn = _knowledge_at(5)
    _term(conn)
    _sense(conn, "SENSE-1", origin_kind="extract", origin_ref="CLM-never-created")
    assert conn.execute("SELECT origin_ref FROM term_sense").fetchone()[0] == "CLM-never-created"


# ---------------------------------------------------------------------------
# XID registry
# ---------------------------------------------------------------------------


def test_the_seven_launch_columns_are_registered_xids():
    """Ruling L-E4 as a write-API refusal rather than a convention: every
    column that attributes a proposal or a decision to a launch is validated
    against a real ``platform.launch`` row at write time, and re-checked by
    doctor's ``xid_dangling`` scan."""
    expected = {
        ("term", "created_by_launch"),
        ("term_alias", "created_by_launch"),
        ("term_sense", "proposed_by_launch"),
        ("term_sense", "decided_by_launch"),
        ("term_sense_evidence", "created_by_launch"),
        ("term_relation", "marked_by_launch"),
        ("term_relation", "decided_by_launch"),
    }
    registered = {(t, c) for (t, c) in XID_REGISTRY if t in LEXICON_TABLES}
    assert registered == expected
    for key in expected:
        assert XID_REGISTRY[key] == XidTarget("platform", "launch", "launch_id")


def test_the_polymorphic_id_columns_are_not_registered_as_xids():
    """``term_sense.origin_ref`` and ``term_relation.src_id``/``dst_id`` name
    rows in the SAME database, and which table depends on a sibling column.
    An XID entry means "this crosses a .db boundary and has one target
    table"; neither is that."""
    assert set(xid_columns_for_table("term_sense")) == {"proposed_by_launch", "decided_by_launch"}
    assert set(xid_columns_for_table("term_relation")) == {"marked_by_launch", "decided_by_launch"}


# ---------------------------------------------------------------------------
# term_fts
# ---------------------------------------------------------------------------


def test_term_fts_is_trigram_tokenized_and_matches_substrings():
    """Trigram, not porter: the lexicon's queries are substring and
    near-miss duplicate detection over short lemmas, which stemming hurts.
    A porter index would not match a three-character fragment at all."""
    conn = _knowledge_at(5)
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='term_fts'").fetchone()[0]
    assert "trigram" in sql
    conn.execute("INSERT INTO term_fts(term_id, text) VALUES ('TERM-1', 'initiative order')")
    hits = conn.execute("SELECT term_id FROM term_fts WHERE term_fts MATCH 'tiat'").fetchall()
    assert [r[0] for r in hits] == ["TERM-1"]


def test_term_fts_scores_are_available_for_the_duplicate_floor():
    """``policy.DUPLICATE_BM25_FLOOR`` is an UPPER bound on a negative
    score. Asserted here because a positive reading of the name is the
    natural one and would invert E2's filter."""
    conn = _knowledge_at(5)
    conn.execute("INSERT INTO term_fts(term_id, text) VALUES ('TERM-1', 'initiative order')")
    score = conn.execute(
        "SELECT bm25(term_fts) FROM term_fts WHERE term_fts MATCH 'initiative'"
    ).fetchone()[0]
    assert score < 0
    assert policy.DUPLICATE_BM25_FLOOR < 0
