"""jobs.db — durable execution ledger (atomic's pattern, per-program).
Design Section 4.4 verbatim. M2 (Jobs ledger + workers) owns the
claim/lease/heartbeat business logic; M1 ships only the DDL these writes
land in, per the "schema-only, no business logic" spine discipline.
"""

from __future__ import annotations

from trialerror.stores.migrate import Migration

TABLES = ("job", "job_event")

_V1 = (
    """
    CREATE TABLE job (
        job_id           TEXT PRIMARY KEY,
        kind              TEXT NOT NULL CHECK (
            kind IN ('ocr','embed','index','extract','ingest_batch','watch','custom')
        ),
        payload            TEXT NOT NULL,
        state               TEXT NOT NULL CHECK (
            state IN ('pending','claimed','running','complete','failed','abandoned','paused')
        ),
        claimed_by          TEXT,
        lease_expires_ts    TEXT,
        heartbeat_ts        TEXT,
        attempts            INTEGER NOT NULL DEFAULT 0,
        max_attempts        INTEGER NOT NULL DEFAULT 3,
        next_attempt_ts     TEXT,
        failure_class       TEXT CHECK (failure_class IN ('environmental','logic')),
        last_error          TEXT,
        checkpoint          TEXT,
        created_ts          TEXT NOT NULL,
        settled_ts          TEXT
    )
    """,
    """
    CREATE TABLE job_event (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id  TEXT NOT NULL REFERENCES job(job_id),
        ts      TEXT NOT NULL,
        type    TEXT NOT NULL,
        detail  TEXT
    )
    """,
    "CREATE INDEX idx_job_state ON job(state)",
    "CREATE INDEX idx_job_event_job ON job_event(job_id)",
)

# ---- schema-v2 (docs/the migration-plan notes (internal, not in this export) Section 4, item 2;
# docs/INTEGRATION_NOTES.md item 8) -----------------------------------------
#
# job.kind's CHECK constraint gains 'normalize'/'chunk' as first-class kinds
# (previously they rode kind='custom' with payload['handler'] set --
# trialerror.ingest.pipeline.stage_job_kind_and_payload's own TRIALERROR-DEV-NOTE,
# which this migration discharges; see that module's post-v2 update).
# SQLite cannot ALTER a CHECK constraint directly, so this is the same
# documented table-rebuild recipe memory_item's v2 migration uses (new
# table, copy, drop, rename, re-create indexes) -- inside the SAME one
# transaction trialerror.stores.migrate.apply_migrations wraps every migration
# in, per the M1 pattern this build follows.
#
# job_event.job_id REFERENCES job(job_id) is a same-file FK with existing
# rows once a program has run any job -- SQLite refuses a bare
# ``DROP TABLE job`` under ``PRAGMA foreign_keys=ON`` while a referencing
# child row exists (verified empirically against a live repro: DROP TABLE
# on an FK-referenced parent fails with "FOREIGN KEY constraint failed"
# even though the very next statement recreates the table under the
# identical name before commit -- this is NOT the same case SQLite's
# ALTER-TABLE-RENAME auto-rewrite-of-other-tables'-REFERENCES-clauses
# gotcha covers, which is why the temp-name-first/drop-old/rename-into-place
# ORDER alone -- correct as it is -- does not by itself avoid the failure).
# ``trialerror.stores.migrate.apply_migrations`` was updated (this same build) to
# toggle ``PRAGMA foreign_keys`` OFF before each migration's transaction and
# back ON after (SQLite documents the pragma as a no-op if set WHILE a
# transaction is already open, so it cannot live inside ``_V2`` itself) --
# see that function's own TRIALERROR-DEV-NOTE for the full account. job_event's
# own schema/data/index are untouched by this migration; its FK clause
# ("REFERENCES job(job_id)") is never rewritten because ``job`` is DROPped
# (not RENAMEd away) and a table of that exact name exists again by the time
# the transaction commits.
_V2 = (
    """
    CREATE TABLE job__v2new (
        job_id           TEXT PRIMARY KEY,
        kind              TEXT NOT NULL CHECK (
            kind IN ('ocr','embed','index','extract','ingest_batch','watch','custom','normalize','chunk')
        ),
        payload            TEXT NOT NULL,
        state               TEXT NOT NULL CHECK (
            state IN ('pending','claimed','running','complete','failed','abandoned','paused')
        ),
        claimed_by          TEXT,
        lease_expires_ts    TEXT,
        heartbeat_ts        TEXT,
        attempts            INTEGER NOT NULL DEFAULT 0,
        max_attempts        INTEGER NOT NULL DEFAULT 3,
        next_attempt_ts     TEXT,
        failure_class       TEXT CHECK (failure_class IN ('environmental','logic')),
        last_error          TEXT,
        checkpoint          TEXT,
        created_ts          TEXT NOT NULL,
        settled_ts          TEXT
    )
    """,
    """
    INSERT INTO job__v2new (
        job_id, kind, payload, state, claimed_by, lease_expires_ts, heartbeat_ts,
        attempts, max_attempts, next_attempt_ts, failure_class, last_error,
        checkpoint, created_ts, settled_ts
    )
    SELECT
        job_id, kind, payload, state, claimed_by, lease_expires_ts, heartbeat_ts,
        attempts, max_attempts, next_attempt_ts, failure_class, last_error,
        checkpoint, created_ts, settled_ts
    FROM job
    """,
    "DROP TABLE job",
    "ALTER TABLE job__v2new RENAME TO job",
    "CREATE INDEX idx_job_state ON job(state)",
)

# ---- schema-v3 (lane a, docs/reviews/LANE_A_WEB_INGESTION_DESIGN.md Section 5)
# ---------------------------------------------------------------------------
#
# job.kind's CHECK gains 'web_fetch' and 'web_extract': the two stages of web
# ingestion, run by the jobs window's existing loop, with no new worker
# process.
#
# They are first-class kinds rather than kind='custom' riders for the same
# reason normalize/chunk stopped riding custom in v2 -- a real kind is what
# ``jobs list --kind`` filters on, what the ledger's per-kind counts see, and
# what a doctor check can reason about. Riding 'custom' would make every web
# fetch in the ledger indistinguishable from every other custom job.
#
# TWO kinds, not one, because they are separated by a trust boundary and by
# an unbounded wait: 'web_fetch' writes a manifest and parks
# (EnvironmentalFailure, no attempt consumed) until the sidecar answers, and
# may re-enter its handler for hours; 'web_extract' is pure local CPU over
# bytes already on disk, and a failure in it is a LOGIC failure worth an
# attempt. Folding them into one job would mean either an extraction bug
# burning attempts that belong to the network, or a slow network burning
# attempts that belong to the parser.
#
# Same table-rebuild recipe as _V2 (SQLite cannot ALTER a CHECK), inside the
# one transaction apply_migrations wraps every migration in, and relying on
# that function's PRAGMA foreign_keys toggle for the job_event.job_id FK --
# see _V2's comment above for the full account of why that toggle is needed.
_V3 = (
    """
    CREATE TABLE job__v3new (
        job_id           TEXT PRIMARY KEY,
        kind              TEXT NOT NULL CHECK (
            kind IN ('ocr','embed','index','extract','ingest_batch','watch','custom',
                     'normalize','chunk','web_fetch','web_extract')
        ),
        payload            TEXT NOT NULL,
        state               TEXT NOT NULL CHECK (
            state IN ('pending','claimed','running','complete','failed','abandoned','paused')
        ),
        claimed_by          TEXT,
        lease_expires_ts    TEXT,
        heartbeat_ts        TEXT,
        attempts            INTEGER NOT NULL DEFAULT 0,
        max_attempts        INTEGER NOT NULL DEFAULT 3,
        next_attempt_ts     TEXT,
        failure_class       TEXT CHECK (failure_class IN ('environmental','logic')),
        last_error          TEXT,
        checkpoint          TEXT,
        created_ts          TEXT NOT NULL,
        settled_ts          TEXT
    )
    """,
    """
    INSERT INTO job__v3new (
        job_id, kind, payload, state, claimed_by, lease_expires_ts, heartbeat_ts,
        attempts, max_attempts, next_attempt_ts, failure_class, last_error,
        checkpoint, created_ts, settled_ts
    )
    SELECT
        job_id, kind, payload, state, claimed_by, lease_expires_ts, heartbeat_ts,
        attempts, max_attempts, next_attempt_ts, failure_class, last_error,
        checkpoint, created_ts, settled_ts
    FROM job
    """,
    "DROP TABLE job",
    "ALTER TABLE job__v3new RENAME TO job",
    "CREATE INDEX idx_job_state ON job(state)",
)

MIGRATIONS = (
    Migration(version=1, name="jobs_v1_initial_schema", statements=_V1),
    Migration(version=2, name="jobs_v2_kind_check_adds_normalize_and_chunk", statements=_V2),
    Migration(version=3, name="jobs_v3_kind_check_adds_web_fetch_and_web_extract", statements=_V3),
)
