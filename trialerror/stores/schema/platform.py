"""platform.db — money and accounts. Design Section 4.3 verbatim.

Lives at ``~/.trialerror/platform.db`` (one file per machine account setup,
shared across every program — see ``trialerror.stores.paths.platform_db_path``).
"""

from __future__ import annotations

from trialerror.stores.migrate import Migration

TABLES = ("account", "budget_pool", "launch", "quota_snapshot", "calibration")

_V1 = (
    """
    CREATE TABLE account (
        account_id  TEXT PRIMARY KEY,
        label       TEXT NOT NULL,
        created_ts  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE budget_pool (
        pool_id               TEXT PRIMARY KEY,
        account_id            TEXT NOT NULL REFERENCES account(account_id),
        model_class           TEXT NOT NULL CHECK (model_class IN ('top','mid','small')),
        period                TEXT NOT NULL CHECK (period IN ('weekly','monthly')),
        period_start          TEXT NOT NULL,
        cap_tokens            INTEGER NOT NULL,
        spent_visible_tokens  INTEGER NOT NULL DEFAULT 0,
        billed_multiplier     REAL NOT NULL DEFAULT 2.75,
        soft_pct              REAL NOT NULL DEFAULT 95,
        hard_pct              REAL NOT NULL DEFAULT 100,
        updated_ts            TEXT NOT NULL
    )
    """,
    # launch.session_id is an XID -> ops.session (session rows live in
    # ops.db, a different file); launch.workpackage is a plain free-form
    # scoping string with NO target table -- not an XID, not an FK (design
    # Section 4 cross-store rule, delta-verify residual applied at M1
    # kickoff). launch.account_id IS a same-file FK (both in platform.db).
    """
    CREATE TABLE launch (
        launch_id         TEXT PRIMARY KEY,
        account_id         TEXT NOT NULL REFERENCES account(account_id),
        program_id          TEXT NOT NULL,
        session_id            TEXT NOT NULL,
        parent_launch          TEXT REFERENCES launch(launch_id),
        agent_kind               TEXT NOT NULL,
        model_class                TEXT NOT NULL,
        model                        TEXT NOT NULL,
        purpose                       TEXT NOT NULL,
        est_tokens                     INTEGER NOT NULL,
        booked_ts                       TEXT NOT NULL,
        booking_ttl_s                     INTEGER NOT NULL DEFAULT 3600,
        state                              TEXT NOT NULL CHECK (
            state IN ('PROVISIONAL','RUNNING','RECONCILED','ABANDONED','REFUSED','DEFERRED')
        ),
        actual_tokens      INTEGER,
        reconciled_ts      TEXT,
        reconcile_source   TEXT CHECK (reconcile_source IN ('transcript','estimate','manual')),
        workpackage        TEXT,
        attrs              TEXT
    )
    """,
    """
    CREATE TABLE quota_snapshot (
        snap_id     TEXT PRIMARY KEY,
        account_id  TEXT NOT NULL REFERENCES account(account_id),
        ts          TEXT NOT NULL,
        source      TEXT NOT NULL CHECK (source IN ('screenshot','api','estimate')),
        payload     TEXT NOT NULL
    )
    """,
    # design text omits the "FK" marker on calibration.account_id (unlike
    # its sibling rows budget_pool/quota_snapshot in the same table block);
    # TRIALERROR-DEV-NOTE: treated as a same-file FK for consistency with those
    # siblings -- both tables live in platform.db, and an unenforced
    # dangling account_id here would be the exact class of silent bug the
    # rest of Section 4.3 is designed to make loud. Faithful-closest-reading.
    """
    CREATE TABLE calibration (
        calib_id      TEXT PRIMARY KEY,
        account_id    TEXT NOT NULL REFERENCES account(account_id),
        model_class   TEXT NOT NULL,
        window        TEXT NOT NULL,
        multiplier    REAL NOT NULL,
        derived_from  TEXT NOT NULL,
        ts            TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_launch_state ON launch(state)",
    "CREATE INDEX idx_launch_account ON launch(account_id)",
    "CREATE INDEX idx_budget_pool_account ON budget_pool(account_id)",
)

# ---- platform-v2 (lane FB-3, dispositions D-FB-13 (c) / D-FB-14 (1)) ------
#
# Two changes to one table, which is why they share one migration:
#
# 1. ``launch.reconcile_source`` gains ``'event'``. A reconciliation whose
#    number came off a recorded ``subagent_return`` event (the host's own
#    ``usage`` object, captured by ``trialerror.hooks.post_task``) is a
#    DIFFERENT provenance from a human typing ``--actual-tokens``, and the
#    whole point of the ``reconcile_provenance`` doctor check is that the
#    two can be told apart. Only ``budget reconcile --from-event`` sets it:
#    ``--reconcile-source`` does not offer it as a choice, and
#    ``reconcile_launch`` refuses it from any other caller, so the value
#    cannot be asserted by hand about a launch nobody measured.
#
# 2. The launch row gains the usage SPLIT (``usage_input_tokens``,
#    ``usage_cache_creation_tokens``, ``usage_cache_read_tokens``,
#    ``usage_output_tokens``) and ``pool_id``. All five are nullable and
#    stay null for every pre-v2 row and for every ``--actual-tokens``
#    reconciliation: a null here means "nobody measured this", which is the
#    one thing a fabricated zero could not say. ``pool_id`` is the pool the
#    booking was actually judged against, so a per-pool committed sum stops
#    being an inference from (account_id, model_class) the moment a period
#    rolls over -- see ``trialerror.budget.pools.pool_report``.
#
# SQLite cannot ALTER a CHECK constraint, so this is the documented
# table-rebuild recipe (new table, copy, drop, rename, re-create indexes)
# that ops-v2 and jobs-v2 already use, inside the one transaction
# ``trialerror.stores.migrate.apply_migrations`` wraps every migration in.
# ``launch.parent_launch`` REFERENCES ``launch(launch_id)`` -- a SELF-FK with
# existing rows -- so the same ``PRAGMA foreign_keys`` toggle that made
# jobs-v2's parent/child rebuild safe is what makes this one safe: during the
# window between ``DROP TABLE launch`` and the RENAME, the new table's own FK
# clause names a table that does not exist. Nothing else in platform.db
# references ``launch``, and the FK clause is written against the FINAL name
# (``launch``), so the rename needs no rewrite of it.
_V2 = (
    """
    CREATE TABLE launch__v2new (
        launch_id         TEXT PRIMARY KEY,
        account_id         TEXT NOT NULL REFERENCES account(account_id),
        program_id          TEXT NOT NULL,
        session_id            TEXT NOT NULL,
        parent_launch          TEXT REFERENCES launch(launch_id),
        agent_kind               TEXT NOT NULL,
        model_class                TEXT NOT NULL,
        model                        TEXT NOT NULL,
        purpose                       TEXT NOT NULL,
        est_tokens                     INTEGER NOT NULL,
        booked_ts                       TEXT NOT NULL,
        booking_ttl_s                     INTEGER NOT NULL DEFAULT 3600,
        state                              TEXT NOT NULL CHECK (
            state IN ('PROVISIONAL','RUNNING','RECONCILED','ABANDONED','REFUSED','DEFERRED')
        ),
        actual_tokens      INTEGER,
        reconciled_ts      TEXT,
        reconcile_source   TEXT CHECK (reconcile_source IN ('transcript','estimate','manual','event')),
        workpackage        TEXT,
        attrs              TEXT,
        usage_input_tokens           INTEGER,
        usage_cache_creation_tokens  INTEGER,
        usage_cache_read_tokens      INTEGER,
        usage_output_tokens          INTEGER,
        pool_id                      TEXT REFERENCES budget_pool(pool_id)
    )
    """,
    """
    INSERT INTO launch__v2new (
        launch_id, account_id, program_id, session_id, parent_launch, agent_kind,
        model_class, model, purpose, est_tokens, booked_ts, booking_ttl_s, state,
        actual_tokens, reconciled_ts, reconcile_source, workpackage, attrs
    )
    SELECT
        launch_id, account_id, program_id, session_id, parent_launch, agent_kind,
        model_class, model, purpose, est_tokens, booked_ts, booking_ttl_s, state,
        actual_tokens, reconciled_ts, reconcile_source, workpackage, attrs
    FROM launch
    """,
    "DROP TABLE launch",
    "ALTER TABLE launch__v2new RENAME TO launch",
    "CREATE INDEX idx_launch_state ON launch(state)",
    "CREATE INDEX idx_launch_account ON launch(account_id)",
    "CREATE INDEX idx_launch_pool ON launch(pool_id)",
)

MIGRATIONS = (
    Migration(version=1, name="platform_v1_initial_schema", statements=_V1),
    Migration(version=2, name="platform_v2_launch_usage_split_pool_id_and_event_source", statements=_V2),
)
