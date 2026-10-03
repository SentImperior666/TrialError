"""platform.db — money and accounts. Design Section 4.3 verbatim.

Lives at ``~/.trialerror/platform.db`` (one file per machine account setup,
shared across every program — see ``trialerror.stores.paths.platform_db_path``).
"""

from __future__ import annotations

from trialerror.stores.migrate import Migration

TABLES = (
    "account",
    "budget_pool",
    "launch",
    "quota_snapshot",
    "calibration",
    "unit",
    "unit_msg",
    "probe_run",
    "quota_capture",
    "quota_rate",
    "quota_notice",
)

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

# ---- platform-v3 (lane L3, F1/F5: unit cost from transcripts, probes) -----
#
# Three brand-new, additive tables -- no existing table is touched, so this
# migration is nothing but CREATE TABLE/CREATE INDEX (design Section 2.1;
# trap 3: fresh stores replay every migration, _V1/_V2 stay untouched).
#
# ``unit`` is one row per Claude Code "unit of work" this host has ever
# scanned out of its own transcripts: a main session, a subagent, or a
# workflow agent. ``unit_key`` is a computed TEXT primary key
# (``"<host>/<session_id>/<agent_id or '-'>"``) rather than a tuple of NOT
# NULL columns, because SQLite's own UNIQUE/PRIMARY KEY machinery treats
# NULLs as pairwise-distinct -- a main unit's NULL ``agent_id`` would then
# never collide with a second scan of the same session, and the row would
# duplicate every time ``units scan`` re-reads that file. F2's verdict
# columns and the lanes/launch attribution columns are left NULL here on
# purpose (design Section 0: "verdicts per unit (F2 provides them later;
# the columns are left empty)").
#
# ``unit_msg`` is the cross-file message-id ledger ``units scan`` uses to
# dedupe a resumed or forked session's copied records (design Section 2.2
# step 2: "a message id already owned by another unit ... is skipped").
#
# ``probe_run`` is F5's conformance/canary/drill result ledger (design
# Section 3.1) -- kept in platform.db, not a program's ops.db, because a
# conformance probe is a fact about THIS MACHINE's Claude Code install, not
# about any one program (mirrors ``unit`` and the pre-existing
# ``quota_snapshot``, both already per-machine here for the same reason).
_V3 = (
    """
    CREATE TABLE unit (
        unit_key                TEXT PRIMARY KEY,
        host                     TEXT NOT NULL,
        kind                      TEXT NOT NULL CHECK (kind IN ('main','subagent','workflow_agent')),
        session_id                TEXT NOT NULL,
        agent_id                   TEXT,
        workflow_run_id             TEXT,
        parent_unit_key               TEXT,
        spawn_tool_use_id              TEXT,
        agent_type                       TEXT,
        project_slug                      TEXT NOT NULL,
        entrypoint                         TEXT,
        cc_version                          TEXT,
        models                                TEXT,
        first_ts                              TEXT,
        last_ts                                TEXT,
        conversation_last_ts                    TEXT,
        n_messages                               INTEGER NOT NULL DEFAULT 0,
        usage_input                               INTEGER NOT NULL DEFAULT 0,
        usage_cache_write                          INTEGER NOT NULL DEFAULT 0,
        usage_cache_read                            INTEGER NOT NULL DEFAULT 0,
        usage_output                                 INTEGER NOT NULL DEFAULT 0,
        usage_cache_write_1h                          INTEGER NOT NULL DEFAULT 0,
        usage_cache_write_5m                           INTEGER NOT NULL DEFAULT 0,
        usage_source                                    TEXT NOT NULL CHECK (
            usage_source IN ('transcript','transcript_partial','statusline_total','otel','none')
        ),
        statusline_cost         TEXT,
        transcript_path          TEXT,
        transcript_sha256         TEXT,
        transcript_size            INTEGER,
        transcript_mtime_ns          INTEGER,
        launch_id                     TEXT,
        lane                            TEXT,
        round                             TEXT,
        verdict_status                     TEXT,
        verdict_reason                       TEXT,
        extractor_version                     TEXT NOT NULL,
        scanned_ts                             TEXT NOT NULL
    )
    """,
    "CREATE INDEX ix_unit_session ON unit(host, session_id)",
    """
    CREATE TABLE unit_msg (
        msg_id    TEXT PRIMARY KEY,
        unit_key  TEXT NOT NULL,
        ts        TEXT
    )
    """,
    """
    CREATE TABLE probe_run (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT NOT NULL,
        kind           TEXT NOT NULL CHECK (kind IN ('conformance','canary','drill')),
        host            TEXT NOT NULL,
        program_id       TEXT,
        cc_version        TEXT,
        build              TEXT,
        started_ts          TEXT NOT NULL,
        finished_ts          TEXT,
        status                TEXT NOT NULL CHECK (status IN ('pass','warn','fail','skip','error')),
        detail                 TEXT
    )
    """,
    "CREATE INDEX ix_probe_run_name ON probe_run(name, started_ts)",
)

# ---- platform-v4 (lane L4, F6 first slice: quota policy) ------------------
#
# Three brand-new, additive tables (nothing existing is touched -- pure
# CREATE TABLE/CREATE INDEX, the same additive shape v3 used):
#
# ``quota_capture`` is the imported form of each host's
# ``rate_limits.jsonl`` (design L4 Section 2.2): one row per statusLine
# capture, deduplicated by ``UNIQUE (host, epoch, session_id)`` so
# ``trialerror quota import`` can be re-run against the same history file (or
# a re-pulled copy of it) without inserting the same row twice.
# ``quota_snapshot.source`` already has a CHECK constraint
# (``'screenshot','api','estimate'``) that a fourth value would need to
# widen -- SQLite cannot ALTER a CHECK, so the design chooses a new table
# over that table rebuild (design L4 Section 1).
#
# ``quota_rate`` holds the fitted exchange rate (design L4 Section 2.3):
# one row per (account_label, window) fit, with the bootstrap interval, the
# window counts behind it and the free-text ``detail`` the CLI's plain-words
# summary reads from. A later fit does not overwrite an earlier one -- there
# is no UNIQUE constraint here on purpose, so ``quota_rate show`` reading
# "the latest row for this (account_label, window)" is a real history, not
# a single mutable cell no auditor can see the trend of.
#
# ``quota_notice`` is every 50/80/95 % monthly-spend notice, ``limit_hit``
# StopFailure detection, ``new_account`` guard state and ``rate_stale``
# warning, each written at most once per (account_label, kind, period,
# level) by the code that computes it -- see
# :mod:`trialerror.quota.monthly` and :mod:`trialerror.hooks.stop_failure`.
_V4 = (
    """
    CREATE TABLE quota_capture (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        host              TEXT NOT NULL,
        account_label     TEXT NOT NULL DEFAULT '',
        epoch             REAL NOT NULL,
        captured_ts       TEXT NOT NULL,
        session_id        TEXT,
        session_cost_usd  REAL,
        five_pct          REAL,
        five_resets       INTEGER,
        seven_pct         REAL,
        seven_resets      INTEGER,
        cc_version        TEXT,
        model             TEXT,
        UNIQUE (host, epoch, session_id)
    )
    """,
    "CREATE INDEX ix_quota_capture_time ON quota_capture(account_label, epoch)",
    """
    CREATE TABLE quota_rate (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        account_label      TEXT NOT NULL,
        window             TEXT NOT NULL CHECK (window IN ('five_hour','seven_day')),
        points_per_usd     REAL NOT NULL,
        ci_low             REAL,
        ci_high            REAL,
        n_windows          INTEGER NOT NULL,
        excluded_windows   INTEGER NOT NULL,
        fit_from_ts        TEXT NOT NULL,
        fit_to_ts          TEXT NOT NULL,
        fitted_ts          TEXT NOT NULL,
        method             TEXT NOT NULL,
        detail             TEXT
    )
    """,
    """
    CREATE TABLE quota_notice (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        account_label  TEXT NOT NULL,
        kind           TEXT NOT NULL CHECK (kind IN ('monthly_level','limit_hit','new_account','rate_stale')),
        period         TEXT,
        level          INTEGER,
        created_ts     TEXT NOT NULL,
        sent_ts        TEXT,
        detail         TEXT
    )
    """,
)

# v5 (spawn identity): the spawn gate records WHICH tool call consumed a
# booking, and when, so a failed spawn that never started a subagent can
# give its booking back and a spawn that did start one never can.
# ``spawn_ts`` is the gate's own moment and is never moved (``booked_ts`` is
# moved by ``budget heartbeat``, so it cannot say when a spawn happened).
# ``spawn_transcript_dir`` is absolute: ``<dirname(transcript_path)>/<session_id>``.
# ``agent_id`` is set at PostToolUse from ``tool_response.agentId``.
# ALTER ... ADD COLUMN and CREATE INDEX only, so ADDITIVE.
_V5 = (
    "ALTER TABLE launch ADD COLUMN spawn_tool_use_id TEXT",
    "ALTER TABLE launch ADD COLUMN spawn_ts TEXT",
    "ALTER TABLE launch ADD COLUMN spawn_transcript_dir TEXT",
    "ALTER TABLE launch ADD COLUMN agent_id TEXT",
    "CREATE INDEX ix_launch_spawn_tool_use ON launch(spawn_tool_use_id)",
)

MIGRATIONS = (
    Migration(version=1, name="platform_v1_initial_schema", statements=_V1),
    Migration(version=2, name="platform_v2_launch_usage_split_pool_id_and_event_source", statements=_V2),
    Migration(version=3, name="platform_v3_unit_cost_and_probes", statements=_V3),
    Migration(version=4, name="platform_v4_quota_policy", statements=_V4),
    Migration(version=5, name="platform_v5_launch_spawn_identity", statements=_V5),
)
