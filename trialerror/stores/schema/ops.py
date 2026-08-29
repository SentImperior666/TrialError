"""ops.db — the operations store. Design Section 4.2 (ruling/law_digest,
session, template/artifact/gate/gate_transition, event, thread/feed_post/
inbox_item, prereg, lens_roster/lens_assignment) plus Sections 9.7/9.8
(memory_item, room/room_turn/room_score) — in M1 scope per the review
union's corrected scope statement ("§4 + §9.6-9.8 verbatim").

TRIALERROR-DEV-NOTE (synthetic PKs): two tables are specified as append-only
logs with no PK column named in the design prose (``gate_transition``,
mirrored by ``job_event`` in ``schema/jobs.py``). Both get a synthetic
``INTEGER PRIMARY KEY AUTOINCREMENT`` id — the cheapest faithful reading
that satisfies "every table is a table" without inventing meaning the
design didn't specify.
"""

from __future__ import annotations

from trialerror.stores.migrate import Migration

TABLES = (
    "ruling",
    "law_digest",
    "session",
    "template",
    "artifact",
    "gate",
    "gate_transition",
    "event",
    "thread",
    "feed_post",
    "inbox_item",
    "prereg",
    "lens_roster",
    "lens_assignment",
    "memory_item",
    "room",
    "room_turn",
    "room_score",
    "room_link",
    "criterion",
    "feed_post_translation",
)

_V1 = (
    # ---- 9.1 corrections ledger -----------------------------------------
    """
    CREATE TABLE ruling (
        ruling_id             TEXT PRIMARY KEY,
        ts                    TEXT NOT NULL,
        verbatim_quote        TEXT,
        summary                TEXT NOT NULL,
        standing_clauses        TEXT,
        domains                   TEXT,
        supersedes                  TEXT REFERENCES ruling(ruling_id),
        supersedes_note               TEXT,
        status                          TEXT NOT NULL CHECK (status IN ('active','superseded')),
        ledger_sha256_after               TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE law_digest (
        version         TEXT PRIMARY KEY,
        generated_ts    TEXT NOT NULL,
        content_sha256  TEXT NOT NULL,
        rendered_path   TEXT NOT NULL
    )
    """,
    # ---- 9.3 session lifecycle -------------------------------------------
    # session.account_id is an XID -> platform.account (delta-verify
    # residual #1, applied at M1 kickoff: ADD session.account_id to the
    # cross-store enumeration).
    """
    CREATE TABLE session (
        session_id        TEXT PRIMARY KEY,
        account_id        TEXT NOT NULL,
        opened_ts         TEXT NOT NULL,
        closed_ts         TEXT,
        status            TEXT NOT NULL CHECK (status IN ('open','closed','abandoned')),
        boot_pin_version  TEXT,
        boot_bundle_sha   TEXT,
        queue             TEXT,
        close_report      TEXT,
        course_check      TEXT
    )
    """,
    # ---- 9.4 typed artifacts + gates --------------------------------------
    """
    CREATE TABLE template (
        type_key    TEXT PRIMARY KEY,
        title       TEXT NOT NULL,
        version     TEXT NOT NULL,
        path        TEXT NOT NULL,
        gated       INTEGER NOT NULL CHECK (gated IN (0,1)),
        schema_ref  TEXT
    )
    """,
    # artifact.gate_id and gate.artifact_id are a circular same-file FK
    # pair by design (l.329-335 / l.330); SQLite does not validate that a
    # REFERENCES target table exists at CREATE TABLE time (only at DML
    # time, and only with foreign_keys=ON), so declaring both in either
    # order is safe as long as both tables land in the same migration
    # (they do). registered_by_launch is XID -> platform.launch.
    """
    CREATE TABLE artifact (
        artifact_id        TEXT PRIMARY KEY,
        type                TEXT NOT NULL REFERENCES template(type_key),
        title                 TEXT NOT NULL,
        path                    TEXT NOT NULL,
        sha256                    TEXT NOT NULL,
        status                      TEXT NOT NULL CHECK (
            status IN ('draft','in_gate','registered','superseded')
        ),
        purpose            TEXT,
        domains            TEXT,
        attrs              TEXT,
        gate_id            TEXT REFERENCES gate(gate_id),
        registered_ts      TEXT,
        registered_by_launch  TEXT NOT NULL,
        supersedes            TEXT REFERENCES artifact(artifact_id)
    )
    """,
    """
    CREATE TABLE gate (
        gate_id               TEXT PRIMARY KEY,
        artifact_id           TEXT NOT NULL REFERENCES artifact(artifact_id),
        state                 TEXT NOT NULL CHECK (
            state IN ('draft','submitted','gated','union_applied','registered','failed')
        ),
        verdict               TEXT CHECK (verdict IN ('PASS','PASS_WITH_EDITS','FAIL')),
        critic_launch         TEXT,
        verdict_ts            TEXT,
        edits                 TEXT,
        reproduction_ref      TEXT,
        reproduction_status   TEXT CHECK (reproduction_status IN ('match','mismatch','unrun'))
    )
    """,
    """
    CREATE TABLE gate_transition (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        gate_id     TEXT NOT NULL REFERENCES gate(gate_id),
        from_state  TEXT NOT NULL,
        to_state    TEXT NOT NULL,
        ts          TEXT NOT NULL,
        by_launch   TEXT NOT NULL,
        evidence    TEXT
    )
    """,
    # ---- 9.5 events (session_id is same-file FK; launch_id is XID ->
    # platform.launch; workpackage is a plain scoping string, not an XID) --
    """
    CREATE TABLE event (
        event_id     TEXT PRIMARY KEY,
        ts           TEXT NOT NULL,
        session_id   TEXT REFERENCES session(session_id),
        launch_id    TEXT,
        workpackage  TEXT,
        type         TEXT NOT NULL,
        payload      TEXT NOT NULL,
        redactions   INTEGER NOT NULL DEFAULT 0
    )
    """,
    # ---- 9.9 feed + inbox --------------------------------------------------
    """
    CREATE TABLE thread (
        thread_id           TEXT PRIMARY KEY,
        title                TEXT NOT NULL,
        created_ts             TEXT NOT NULL,
        created_by_launch        TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE feed_post (
        post_id       TEXT PRIMARY KEY,
        thread_id     TEXT NOT NULL REFERENCES thread(thread_id),
        author        TEXT NOT NULL,
        launch_id     TEXT,
        ts            TEXT NOT NULL,
        body          TEXT NOT NULL,
        in_reply_to   TEXT REFERENCES feed_post(post_id)
    )
    """,
    """
    CREATE TABLE inbox_item (
        item_id           TEXT PRIMARY KEY,
        ts                TEXT NOT NULL,
        body              TEXT NOT NULL,
        source            TEXT NOT NULL CHECK (source IN ('user')),
        read_ts           TEXT,
        read_by_session   TEXT REFERENCES session(session_id)
    )
    """,
    # ---- 9.6 (b) blind pre-registration -------------------------------------
    """
    CREATE TABLE prereg (
        prereg_id          TEXT PRIMARY KEY,
        title              TEXT NOT NULL,
        procedure_sha256   TEXT NOT NULL,
        params_sha256      TEXT NOT NULL,
        committed_ts       TEXT NOT NULL,
        escrow_path        TEXT NOT NULL,
        revealed_ts        TEXT,
        status             TEXT NOT NULL CHECK (status IN ('committed','revealed','voided'))
    )
    """,
    # ---- 9.6 ideation: lenses, slices, stratification -----------------------
    """
    CREATE TABLE lens_roster (
        roster_id    TEXT PRIMARY KEY,
        round_id     TEXT NOT NULL,
        lens_name    TEXT NOT NULL,
        vantage      TEXT NOT NULL,
        seat         TEXT NOT NULL CHECK (seat IN ('standard','assumption_buster')),
        model_class  TEXT NOT NULL,
        created_ts   TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE lens_assignment (
        assign_id               TEXT PRIMARY KEY,
        roster_id               TEXT NOT NULL REFERENCES lens_roster(roster_id),
        slice_spec               TEXT NOT NULL,
        arm                         TEXT CHECK (arm IN ('near','moderate','far')),
        weights                       TEXT NOT NULL DEFAULT '[40,40,20]',
        far_floor                       INTEGER NOT NULL DEFAULT 2,
        inter_cluster_mandate              INTEGER NOT NULL DEFAULT 0 CHECK (
            inter_cluster_mandate IN (0,1)
        ),
        seed          TEXT NOT NULL,
        launch_id     TEXT,
        created_ts    TEXT NOT NULL
    )
    """,
    # ---- 9.7 memory sync (account_id is XID -> platform.account,
    # delta-verify residual #1, applied at M1 kickoff) ------------------------
    """
    CREATE TABLE memory_item (
        memory_item_id  TEXT PRIMARY KEY,
        key             TEXT NOT NULL,
        tier            TEXT NOT NULL CHECK (tier IN ('L0','L1','L2')),
        kind            TEXT NOT NULL CHECK (kind IN ('rule','fact','lesson','preference','index')),
        body            TEXT NOT NULL,
        l0_abstract     TEXT,
        updated_ts      TEXT NOT NULL,
        account_id      TEXT NOT NULL,
        status          TEXT NOT NULL DEFAULT 'active'
    )
    """,
    # ---- 9.8 brainstorm rooms (schema now, runtime v1) ----------------------
    """
    CREATE TABLE room (
        room_id  TEXT PRIMARY KEY,
        topic    TEXT NOT NULL,
        dps      TEXT NOT NULL,
        state    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE room_turn (
        room_id        TEXT NOT NULL REFERENCES room(room_id),
        seq            INTEGER NOT NULL,
        author_launch  TEXT NOT NULL,
        dp_ref         TEXT NOT NULL,
        body           TEXT NOT NULL,
        PRIMARY KEY (room_id, seq)
    )
    """,
    """
    CREATE TABLE room_score (
        dp_ref          TEXT PRIMARY KEY,
        agreement_pct   REAL NOT NULL,
        frozen          INTEGER NOT NULL DEFAULT 0 CHECK (frozen IN (0,1))
    )
    """,
    "CREATE INDEX idx_ruling_status ON ruling(status)",
    "CREATE INDEX idx_artifact_type ON artifact(type)",
    "CREATE INDEX idx_artifact_status ON artifact(status)",
    "CREATE INDEX idx_gate_artifact ON gate(artifact_id)",
    "CREATE INDEX idx_gate_state ON gate(state)",
    "CREATE INDEX idx_event_session ON event(session_id)",
    "CREATE INDEX idx_event_type ON event(type)",
    "CREATE INDEX idx_event_workpackage ON event(workpackage)",
    "CREATE INDEX idx_feed_post_thread ON feed_post(thread_id)",
    "CREATE INDEX idx_lens_assignment_roster ON lens_assignment(roster_id)",
    "CREATE INDEX idx_memory_item_key ON memory_item(key)",
    "CREATE INDEX idx_memory_item_tier ON memory_item(tier)",
)

# ---- schema-v2 (docs/the migration-plan notes (internal, not in this export) Section 4, items 1 + 4;
# docs/INTEGRATION_NOTES.md item 14 is knowledge.db, not this file) --------
#
# Item 1: memory_item.account_id NOT NULL -> nullable. origin-project's repo memory
# (memory/*.md, per this same repo's own CLAUDE.md "Memory sync" section) is
# deliberately cross-account -- a NULL here means "shared across every
# account on this machine," the source's actual semantics, not a synthetic
# single-account fiction forced by a NOT NULL constraint. SQLite cannot
# ALTER a column's NOT NULL off directly, so this is the documented
# table-rebuild recipe (new table, copy, drop, rename, re-create indexes) --
# see trialerror.stores.migrate.apply_migrations' TRIALERROR-DEV-NOTE for why the
# runner now toggles PRAGMA foreign_keys around the whole migration
# transaction (this table itself has no same-file FK children, so that
# toggle is a no-op for memory_item specifically, but the same runner change
# is what makes jobs.py's v2 migration, below, safe). The XID_REGISTRY entry
# for ("memory_item", "account_id") is UNCHANGED -- trialerror.stores.writer's
# ``_validate_xids`` already skips XID validation for a NULL column value
# (see its own "if col not in row or row[col] is None: continue"), so "null
# allowed, non-null still validated" falls out of the existing write-API
# code with no further change once the DDL itself allows NULL.
#
# Item 4 (optional, quality-of-life): thread gains status (default
# 'active', CHECK 'active'|'archived') + refs (JSON). Plain ADD COLUMN --
# no existing NOT NULL to remove, so no table-rebuild needed here.
_V2 = (
    """
    CREATE TABLE memory_item__v2new (
        memory_item_id  TEXT PRIMARY KEY,
        key             TEXT NOT NULL,
        tier            TEXT NOT NULL CHECK (tier IN ('L0','L1','L2')),
        kind            TEXT NOT NULL CHECK (kind IN ('rule','fact','lesson','preference','index')),
        body            TEXT NOT NULL,
        l0_abstract     TEXT,
        updated_ts      TEXT NOT NULL,
        account_id      TEXT,
        status          TEXT NOT NULL DEFAULT 'active'
    )
    """,
    """
    INSERT INTO memory_item__v2new (
        memory_item_id, key, tier, kind, body, l0_abstract, updated_ts, account_id, status
    )
    SELECT memory_item_id, key, tier, kind, body, l0_abstract, updated_ts, account_id, status
    FROM memory_item
    """,
    "DROP TABLE memory_item",
    "ALTER TABLE memory_item__v2new RENAME TO memory_item",
    "CREATE INDEX idx_memory_item_key ON memory_item(key)",
    "CREATE INDEX idx_memory_item_tier ON memory_item(tier)",
    "ALTER TABLE thread ADD COLUMN status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived'))",
    "ALTER TABLE thread ADD COLUMN refs TEXT",
)


# ---- schema-v3 (build-v2-polish): the rooms lane's own five v3-migration
# candidates, ``trialerror/rooms/api.py``'s module TRIALERROR-DEV-NOTE -- items 2, 3,
# 4, 5 (item 1, promoting ``participants``/``rounds_per_dp`` off ``room.
# dps`` JSON, is left for a future migration; out of this build's scope). --
#
# item 2: room.created_ts + room_turn.ts. Plain ADD COLUMN, nullable --
# a pre-v3 row has no timestamp to backfill FROM at the DDL level (the
# room_created/room_turn companion events ARE that history, per
# TRIALERROR-DEV-NOTE item 2 itself: "mirrored into a companion trialerror.events.
# append_event row ... rather than inventing a column this lane has no
# license to add" -- now that this lane has the license, NEW rows get both
# a column and the event; a pre-existing row keeps relying on the event
# trail trialerror.rooms.checks already reads for staleness). trialerror.rooms.api.
# create_room/post_message are updated to populate both going forward.
#
# item 3: room_score gains real room_id + dp_id columns and a composite PK
# (room_id, dp_id), retiring the "<room_id>::<dp_id>" dp_ref namespacing
# convention FOR THIS TABLE (room_turn.dp_ref is untouched by this
# migration -- room_turn already has room_id in its own composite PK
# (room_id, seq); dp_ref there is a same-row filter column, never a PK, so
# item 3's "GLOBAL primary key with no room_id column" problem never
# applied to it). Table-rebuild recipe (trialerror.stores.migrate.
# apply_migrations' FK-toggle, same recipe ops_v2's memory_item rebuild
# used) -- backfills room_id/dp_id for every pre-existing row by splitting
# its old dp_ref on the first "::" separator trialerror.rooms.api._dp_ref always
# wrote (a room_id can never itself contain "::" -- trialerror.util.ids.new_id's
# alphabet is Crockford-Base32 + a dash, no colons -- so splitting on the
# FIRST occurrence always correctly isolates room_id, with everything after
# it, "::" included, reassembled as dp_id verbatim; no data lost even if a
# caller-supplied dp_id itself happens to contain "::").
#
# item 4: room_link -- a per-discussion-point child table promoting the
# OPTIONAL idea_id a ``dps`` JSON entry may carry (module TRIALERROR-DEV-NOTE
# item 1's own convention) to a real, queryable, XID-validated
# (trialerror.stores.xid.XID_REGISTRY: room_link.idea_id -> knowledge.idea)
# column. A per-DP table, not a single room-level column, because a room's
# discussion points may each vet a DIFFERENT idea -- a room-level column
# would be lossy for exactly the multi-DP case this schema already
# supports. trialerror.rooms.api.create_room now writes one row per discussion
# point that carries an idea_id, ALONGSIDE (not instead of) the existing
# dps JSON entry -- the NEITHER-ownership check itself keeps reading dps
# JSON (module's existing, already-working convention); room_link is the
# new queryable audit surface, not a new source of truth for that check.
#
# item 5: room.deliverable_artifact_id -- a same-file FK straight to
# artifact(artifact_id) (both tables live in ops.db, so this is NOT an XID
# -- see trialerror.stores.xid's own module docstring on same-file FKs being
# non-members of that registry). trialerror.rooms.api.
# register_room_deliverable now sets this column ALONGSIDE (not instead of)
# the existing artifact.attrs.room_id / room_deliverable_registered event
# mirrors.
_V3 = (
    "ALTER TABLE room ADD COLUMN created_ts TEXT",
    "ALTER TABLE room ADD COLUMN deliverable_artifact_id TEXT REFERENCES artifact(artifact_id)",
    "ALTER TABLE room_turn ADD COLUMN ts TEXT",
    """
    CREATE TABLE room_score__v3new (
        room_id         TEXT NOT NULL REFERENCES room(room_id),
        dp_id           TEXT NOT NULL,
        agreement_pct   REAL NOT NULL,
        frozen          INTEGER NOT NULL DEFAULT 0 CHECK (frozen IN (0,1)),
        PRIMARY KEY (room_id, dp_id)
    )
    """,
    """
    INSERT INTO room_score__v3new (room_id, dp_id, agreement_pct, frozen)
    SELECT
        substr(dp_ref, 1, instr(dp_ref, '::') - 1),
        substr(dp_ref, instr(dp_ref, '::') + 2),
        agreement_pct,
        frozen
    FROM room_score
    """,
    "DROP TABLE room_score",
    "ALTER TABLE room_score__v3new RENAME TO room_score",
    "CREATE INDEX idx_room_score_room ON room_score(room_id)",
    """
    CREATE TABLE room_link (
        room_id  TEXT NOT NULL REFERENCES room(room_id),
        dp_id    TEXT NOT NULL,
        idea_id  TEXT NOT NULL,
        PRIMARY KEY (room_id, dp_id)
    )
    """,
    "CREATE INDEX idx_room_link_idea ON room_link(idea_id)",
)

# ---- schema-v4 (build-v2dash-data): two independently-designed seams the
# V2 dashboard redesign (docs/reviews/REDESIGN_V2_RATIONALE.md Section 5.3
# items 6/8) both name, landing in the same migration since both are
# additive, both are TABLE-ONLY seams (no runtime/producer code lands in
# this build), and both slot into ops.db.
#
# 1. ``criterion`` -- the Course surface's MINIMAL designed seam (REDESIGN
#    Section 5.3 item 6: "Course tables"), deliberately narrower than that
#    item's full three-table wishlist (``charter_criterion``/
#    ``course_dimension``/``course_phase``): one table carries a label, the
#    mission ``phase`` it belongs to (a free-form scoping string -- there is
#    no ``course_phase`` table to be a same-file FK against, matching
#    ``launch.workpackage``'s own "plain scoping string, no target table"
#    precedent, ``trialerror/stores/schema/platform.py``), its own state, and
#    which artifact (if any) discharged it. ``trialerror.dashboard.data.
#    build_course_panel`` derives mission phases and per-phase rollups by
#    grouping on ``phase`` -- there is deliberately no separate phase table
#    to keep in sync.
# 2. ``feed_post_translation`` -- the AI-Speak -> plain-English translator's
#    storage design (docs/reviews/AISPEAK_TRANSLATOR_DESIGN.md Section 4.2,
#    verbatim shape), landing here as the TABLE seam ONLY: this migration
#    creates the sidecar row shape the future ``trialerror/feed_translate/``
#    package (Section 5, steps 2-4/7-8) will write into and
#    ``trialerror.dashboard.data.build_feed_panel`` will read from -- no job
#    handler, no CLI verb, no translation logic lands with it, per that
#    design doc's own step ordering. ``post_id`` is a same-file FK (both
#    tables live in ops.db, ``trialerror.stores.xid``'s own "same-file FK
#    columns are not XID members" rule); ``created_by_launch`` and
#    ``faithfulness_verdict_id`` DO cross a file boundary (platform.launch,
#    knowledge.verdict) and are registered in ``trialerror/stores/xid.py``
#    accordingly. ``status``/``supersedes`` mirror ``knowledge.summary``'s
#    own versioned-row-chain shape (AISPEAK doc Section 4.2: "modeled on
#    summary's versioned-row shape ... for the same reason summary itself
#    isn't update-in-place").
_V4 = (
    """
    CREATE TABLE criterion (
        criterion_id            TEXT PRIMARY KEY,
        label                   TEXT NOT NULL,
        phase                   TEXT NOT NULL,
        state                   TEXT NOT NULL CHECK (state IN ('open','blocked','discharged')),
        discharged_by_artifact  TEXT REFERENCES artifact(artifact_id)
    )
    """,
    "CREATE INDEX idx_criterion_phase ON criterion(phase)",
    "CREATE INDEX idx_criterion_state ON criterion(state)",
    """
    CREATE TABLE feed_post_translation (
        translation_id           TEXT PRIMARY KEY,
        post_id                  TEXT NOT NULL REFERENCES feed_post(post_id),
        translator_version       TEXT NOT NULL,
        style_mode                TEXT NOT NULL CHECK (style_mode IN ('strict','flavored')),
        body                        TEXT NOT NULL,
        original_sha256               TEXT NOT NULL,
        faithfulness_score               REAL,
        faithfulness_verdict_id             TEXT,
        glossary_links                         TEXT,
        status                                    TEXT NOT NULL CHECK (status IN ('current','superseded')),
        supersedes                                  TEXT REFERENCES feed_post_translation(translation_id),
        created_by_launch                              TEXT,
        created_ts                                        TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_feed_post_translation_post ON feed_post_translation(post_id, translator_version, status)",
)

MIGRATIONS = (
    Migration(version=1, name="ops_v1_initial_schema", statements=_V1),
    Migration(version=2, name="ops_v2_memory_item_account_id_nullable_and_thread_status_refs", statements=_V2),
    Migration(version=3, name="ops_v3_rooms_created_ts_scored_link_deliverable", statements=_V3),
    Migration(version=4, name="ops_v4_criterion_and_feed_post_translation", statements=_V4),
)
