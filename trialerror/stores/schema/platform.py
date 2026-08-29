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

MIGRATIONS = (Migration(version=1, name="platform_v1_initial_schema", statements=_V1),)
