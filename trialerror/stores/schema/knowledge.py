"""knowledge.db — the research content store. Design Section 4.1 verbatim.

Also creates ``chunk_fts`` (an FTS5 virtual table — stdlib SQLite ships
FTS5, so this is unconditional) as part of the base schema migration;
``vec_chunks`` is intentionally NOT created here — Section 4.1 specifies it
"per active model_key" (one virtual table per embedding model, dimensioned
to that model), so it is created on demand by
``trialerror.stores.vecindex.ensure_vec_table`` once a caller (M7's embed
worker) knows which model it's indexing for.
"""

from __future__ import annotations

from trialerror.stores.migrate import Migration

TABLES = (
    "source",
    "document",
    "element",
    "chunk",
    "emb",
    "quote_anchor",
    "claim",
    "entity",
    "relation",
    "merge_proposal",
    "hypothesis",
    "verdict",
    "experiment",
    "idea",
    "record",
    "prov_edge",
    "summary",
    "web_fetch",
    # lane e, knowledge_v5_lexicon_term_store. ``term_fts`` is deliberately
    # absent: it is a virtual table, exactly like ``chunk_fts``, and TABLES
    # feeds ``trialerror.stores.store.TABLE_DB`` (the validated write API's
    # routing map) and the one-row-per-table round-trip fixture, neither of
    # which a contentless FTS5 shadow belongs in.
    "term",
    "term_alias",
    "term_sense",
    "term_sense_evidence",
    "term_relation",
)

_V1 = (
    # registered_by_launch is XID -> platform.launch. Dedup: UNIQUE index on
    # content_sha256 (partial: NULLs don't collide) is what turns a
    # duplicate registration into "return the existing row with dedup_of
    # set" at the write-API layer -- the index just makes a silent second
    # row structurally impossible.
    """
    CREATE TABLE source (
        source_id             TEXT PRIMARY KEY,
        kind                  TEXT NOT NULL CHECK (
            kind IN ('paper','book','web','rulebook','dataset','report','other')
        ),
        title                 TEXT NOT NULL,
        authors               TEXT,
        year                  INTEGER,
        venue                 TEXT,
        url                   TEXT,
        doi                   TEXT,
        arxiv_id              TEXT,
        isbn                  TEXT,
        content_sha256        TEXT,
        license_tier          TEXT NOT NULL CHECK (
            license_tier IN ('open','academic_oa','user_owned_scan','commercial_restricted','unknown')
        ),
        acquisition_route     TEXT NOT NULL CHECK (
            acquisition_route IN (
                'author_posted','institutional','publisher_oa','user_scan','user_delivered','api','web'
            )
        ),
        rights_notes          TEXT,
        request_state         TEXT NOT NULL CHECK (
            request_state IN (
                'wanted','requested','delivered','verifying','archived','indexed','rejected','failed'
            )
        ),
        requested_ts          TEXT,
        delivered_ts          TEXT,
        registered_ts         TEXT NOT NULL,
        registered_by_launch  TEXT NOT NULL,
        dedup_of              TEXT REFERENCES source(source_id)
    )
    """,
    "CREATE UNIQUE INDEX idx_source_content_sha256 ON source(content_sha256) WHERE content_sha256 IS NOT NULL",
    """
    CREATE TABLE document (
        doc_id              TEXT PRIMARY KEY,
        source_id           TEXT NOT NULL REFERENCES source(source_id),
        rel_path            TEXT NOT NULL,
        raw_path            TEXT,
        media_type          TEXT NOT NULL,
        page_count          INTEGER,
        ocr_backend         TEXT,
        ocr_version         TEXT,
        normalizer_id       TEXT NOT NULL,
        normalizer_version  TEXT NOT NULL,
        sha256              TEXT NOT NULL,
        status               TEXT NOT NULL CHECK (
            status IN ('registered','normalized','parsed','chunked','embedded','indexed','failed')
        ),
        sanitizer_version   TEXT
    )
    """,
    """
    CREATE TABLE element (
        element_id        TEXT PRIMARY KEY,
        doc_id            TEXT NOT NULL REFERENCES document(doc_id),
        seq               INTEGER NOT NULL,
        type              TEXT NOT NULL,
        text              TEXT,
        text_as_html      TEXT,
        page_number       INTEGER,
        bbox              TEXT,
        parent_element    TEXT REFERENCES element(element_id),
        category_depth    INTEGER,
        detection_origin  TEXT
    )
    """,
    """
    CREATE TABLE chunk (
        chunk_id          TEXT PRIMARY KEY,
        doc_id            TEXT NOT NULL REFERENCES document(doc_id),
        seq               INTEGER NOT NULL,
        text              TEXT NOT NULL,
        token_count       INTEGER NOT NULL CHECK (token_count <= 1024),
        element_first     TEXT NOT NULL REFERENCES element(element_id),
        element_last      TEXT NOT NULL REFERENCES element(element_id),
        page_start        INTEGER,
        page_end          INTEGER,
        sha256            TEXT NOT NULL,
        chunker_id        TEXT NOT NULL,
        chunker_version   TEXT NOT NULL,
        created_ts        TEXT NOT NULL
    )
    """,
    # embedding cache: model-keyed, chunk-hash-addressed -- survives
    # rechunks of identical text (Section 4.1: "emb: chunk_sha256 PK-part |
    # model_key PK-part | ..."). Deliberately no FK to chunk: the whole
    # point of hash-addressing is that this row outlives any one chunk row
    # that happened to produce that text.
    """
    CREATE TABLE emb (
        chunk_sha256  TEXT NOT NULL,
        model_key     TEXT NOT NULL,
        dims          INTEGER NOT NULL,
        vector        BLOB NOT NULL,
        created_ts    TEXT NOT NULL,
        PRIMARY KEY (chunk_sha256, model_key)
    )
    """,
    # created_by_launch is XID -> platform.launch.
    """
    CREATE TABLE quote_anchor (
        anchor_id          TEXT PRIMARY KEY,
        doc_id             TEXT NOT NULL REFERENCES document(doc_id),
        chunk_id           TEXT REFERENCES chunk(chunk_id),
        page_number        INTEGER,
        char_start         INTEGER NOT NULL,
        char_end           INTEGER NOT NULL,
        stream_fn          TEXT NOT NULL DEFAULT 'stream_v1',
        doc_sha256         TEXT NOT NULL,
        quote_sha256       TEXT NOT NULL,
        quote_text         TEXT,
        created_by_launch  TEXT NOT NULL,
        created_ts         TEXT NOT NULL
    )
    """,
    # bi-temporal (Graphiti 4-timestamp pattern): created_at/expired_at are
    # transaction-time, valid_at/invalid_at are event-time. created_by_launch
    # is XID -> platform.launch.
    """
    CREATE TABLE claim (
        claim_id           TEXT PRIMARY KEY,
        text               TEXT NOT NULL,
        kind               TEXT NOT NULL CHECK (
            kind IN ('finding','definition','number','mechanism','opinion')
        ),
        confidence         REAL,
        anchor_id          TEXT NOT NULL REFERENCES quote_anchor(anchor_id),
        extra_anchors      TEXT,
        created_at         TEXT NOT NULL,
        expired_at         TEXT,
        valid_at           TEXT,
        invalid_at         TEXT,
        superseded_by      TEXT REFERENCES claim(claim_id),
        created_by_launch  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE entity (
        entity_id          TEXT PRIMARY KEY,
        name               TEXT NOT NULL,
        entity_type        TEXT NOT NULL,
        aliases            TEXT,
        summary            TEXT,
        attributes         TEXT,
        resolution         TEXT NOT NULL CHECK (resolution IN ('draft','confirmed','rejected')),
        merge_group        TEXT,
        created_by_launch  TEXT NOT NULL,
        created_at         TEXT NOT NULL
    )
    """,
    # bi-temporal, same 4-column pattern as claim.
    """
    CREATE TABLE relation (
        rel_id            TEXT PRIMARY KEY,
        src_entity        TEXT NOT NULL REFERENCES entity(entity_id),
        dst_entity        TEXT NOT NULL REFERENCES entity(entity_id),
        rel_type          TEXT NOT NULL,
        fact_text         TEXT NOT NULL,
        evidence_anchor   TEXT NOT NULL REFERENCES quote_anchor(anchor_id),
        extra_anchors     TEXT,
        confidence        REAL,
        created_at        TEXT NOT NULL,
        expired_at        TEXT,
        valid_at          TEXT,
        invalid_at        TEXT,
        superseded_by     TEXT REFERENCES relation(rel_id)
    )
    """,
    """
    CREATE TABLE merge_proposal (
        prop_id             TEXT PRIMARY KEY,
        canonical_entity    TEXT NOT NULL REFERENCES entity(entity_id),
        members             TEXT NOT NULL,
        reason              TEXT NOT NULL,
        status              TEXT NOT NULL CHECK (status IN ('draft','confirmed','rejected')),
        proposed_by_launch  TEXT NOT NULL,
        decided_by          TEXT,
        decided_ts          TEXT
    )
    """,
    # prereg_id is XID -> ops.prereg (nullable); created_by_launch is XID ->
    # platform.launch.
    """
    CREATE TABLE hypothesis (
        hyp_id             TEXT PRIMARY KEY,
        text               TEXT NOT NULL,
        status             TEXT NOT NULL CHECK (
            status IN ('open','supported','contradicted','mixed','retired')
        ),
        prereg_id          TEXT,
        created_ts         TEXT NOT NULL,
        created_by_launch  TEXT NOT NULL
    )
    """,
    # prereg_id is XID -> ops.prereg (nullable); issued_by_launch is XID ->
    # platform.launch.
    """
    CREATE TABLE verdict (
        verdict_id          TEXT PRIMARY KEY,
        subject_kind        TEXT NOT NULL CHECK (
            subject_kind IN ('hypothesis','claim','citation','artifact')
        ),
        subject_id          TEXT NOT NULL,
        procedure           TEXT NOT NULL CHECK (
            procedure IN ('citecheck','contracrow','gate','reproduction','custom')
        ),
        procedure_version   TEXT NOT NULL,
        label               TEXT NOT NULL,
        evidence            TEXT NOT NULL,
        prereg_id           TEXT,
        prereg_compliant    INTEGER CHECK (prereg_compliant IN (0,1)),
        reproduction_ref    TEXT,
        ts                  TEXT NOT NULL,
        issued_by_launch    TEXT NOT NULL
    )
    """,
    # prereg_id is XID -> ops.prereg (nullable); created_by_launch is XID ->
    # platform.launch.
    """
    CREATE TABLE experiment (
        exp_id             TEXT PRIMARY KEY,
        hyp_id             TEXT REFERENCES hypothesis(hyp_id),
        prereg_id          TEXT,
        procedure_ref      TEXT NOT NULL,
        params             TEXT NOT NULL,
        status             TEXT NOT NULL CHECK (
            status IN ('planned','running','complete','abandoned')
        ),
        result_refs        TEXT,
        created_ts         TEXT NOT NULL,
        created_by_launch  TEXT NOT NULL
    )
    """,
    # author_launch is XID -> platform.launch; feed_post_ref is XID ->
    # ops.feed_post (nullable).
    """
    CREATE TABLE idea (
        idea_id         TEXT PRIMARY KEY,
        round_id        TEXT,
        author_launch   TEXT NOT NULL,
        body            TEXT NOT NULL,
        slice_ref       TEXT,
        feed_post_ref   TEXT,
        status          TEXT NOT NULL CHECK (status IN ('raw','consolidated','promoted')),
        created_ts      TEXT NOT NULL
    )
    """,
    # artifact_id is XID -> ops.artifact (nullable, "owning register
    # artifact").
    """
    CREATE TABLE record (
        record_id      TEXT PRIMARY KEY,
        register_key   TEXT NOT NULL,
        artifact_id    TEXT,
        seq            INTEGER NOT NULL,
        payload        TEXT NOT NULL,
        anchors        TEXT,
        created_ts     TEXT NOT NULL
    )
    """,
    # launch_id is XID -> platform.launch (nullable).
    """
    CREATE TABLE prov_edge (
        edge_id    TEXT PRIMARY KEY,
        src_kind   TEXT NOT NULL,
        src_id     TEXT NOT NULL,
        dst_kind   TEXT NOT NULL,
        dst_id     TEXT NOT NULL,
        role       TEXT NOT NULL CHECK (
            role IN (
                'derived_from','supports','contradicts','cites','supersedes',
                'extracted_from','verified_by','registered_as'
            )
        ),
        run_id     TEXT,
        launch_id  TEXT,
        ts         TEXT NOT NULL
    )
    """,
    # FTS5 prefilter index (Section 7 pipeline step 1). Porter-stemmed,
    # unicode61-tokenized per Section 4.1. chunk_id is UNINDEXED (it's a
    # lookup key returned in results, never itself the subject of a MATCH).
    """
    CREATE VIRTUAL TABLE chunk_fts USING fts5(
        chunk_id UNINDEXED,
        text,
        tokenize = 'porter unicode61'
    )
    """,
    "CREATE INDEX idx_document_source ON document(source_id)",
    "CREATE INDEX idx_element_doc ON element(doc_id)",
    "CREATE INDEX idx_chunk_doc ON chunk(doc_id)",
    "CREATE INDEX idx_quote_anchor_doc ON quote_anchor(doc_id)",
    "CREATE INDEX idx_quote_anchor_chunk ON quote_anchor(chunk_id)",
    "CREATE INDEX idx_claim_anchor ON claim(anchor_id)",
    "CREATE INDEX idx_claim_expired ON claim(expired_at)",
    "CREATE INDEX idx_relation_src ON relation(src_entity)",
    "CREATE INDEX idx_relation_dst ON relation(dst_entity)",
    "CREATE INDEX idx_relation_expired ON relation(expired_at)",
    "CREATE INDEX idx_verdict_subject ON verdict(subject_kind, subject_id)",
    "CREATE INDEX idx_record_register_key ON record(register_key)",
    "CREATE INDEX idx_prov_edge_src ON prov_edge(src_kind, src_id)",
    "CREATE INDEX idx_prov_edge_dst ON prov_edge(dst_kind, dst_id)",
)

# ---- schema-v2 (docs/the migration-plan notes (internal, not in this export) Section 4, item 3;
# docs/INTEGRATION_NOTES.md item 14) -----------------------------------------
#
# Promotes home/assumed_circle/provenance/tier/set_distance from
# ``trialerror.lens.ideas``'s JSON-packed-into-``slice_ref`` convention (see that
# module's own TRIALERROR-DEV-NOTE, which this migration discharges) to real
# columns. None of the five had a prior NOT NULL/value to preserve (they
# only ever lived inside slice_ref's JSON blob, never as bare columns), so
# this is a plain ``ALTER TABLE ADD COLUMN`` pass -- no table-rebuild
# recipe needed (nothing here removes a NOT NULL or narrows a CHECK).
# ``tier`` reuses the exact near/moderate/far vocabulary
# ``trialerror.lens.stratify``/``trialerror.lens.assign`` already write into
# slice_ref's JSON (a CHECK constraint on a freshly-added, all-NULL column
# is satisfied trivially -- SQLite treats NULL as satisfying a CHECK unless
# the constraint says otherwise, verified against a live repro). ``home``/
# ``assumed_circle``/``provenance`` are left unconstrained TEXT (provenance
# is JSON-shaped, per ``trialerror.lens.ideas.build_slice_ref``'s own
# ``provenance: Any`` parameter -- no CHECK to transcribe from a column that
# never had one). ``idea.slice_ref`` itself is UNCHANGED and kept populated
# by ``trialerror.lens.ideas.write_idea`` for one version, marked deprecated (its
# own module docstring says so post-v2), so nothing already reading
# slice_ref's JSON breaks.
_V2 = (
    "ALTER TABLE idea ADD COLUMN home TEXT",
    "ALTER TABLE idea ADD COLUMN assumed_circle TEXT",
    "ALTER TABLE idea ADD COLUMN provenance TEXT",
    "ALTER TABLE idea ADD COLUMN tier TEXT CHECK (tier IN ('near','moderate','far'))",
    "ALTER TABLE idea ADD COLUMN set_distance REAL",
)

# ---- schema-v3 (build-v2-summary, design Section 11 "summary tier (L1
# overviews)" / Section 7 pipeline step 5) --------------------------------
#
# The L1 summary tier's durable landing zone: v0/v1 shipped NO summary or
# overview column anywhere in knowledge.db (checked against this build's
# own read of Section 4.1 -- ``document`` carries ``status`` and OCR/
# normalizer stamps only, no body-summary field), so this is a genuinely
# NEW table, not a promoted column the way schema-v2 promoted
# ``idea.slice_ref``'s JSON fields.
#
# Shape mirrors ``verdict``'s established ``subject_kind``/``subject_id``
# polymorphic-subject pattern (design Section 4.1) rather than inventing a
# parallel one: ``subject_kind`` is ``document`` (``subject_id`` = a real
# ``doc_id``) or ``collection`` (``subject_id`` = a caller-chosen grouping
# key -- a ``source_id`` when every document under one source is being
# summarized together, or a free-form label for an arbitrary caller-given
# ``doc_ids`` set). Like ``verdict.subject_id``, ``summary.subject_id`` is
# deliberately NOT a same-file ``FK`` -- a real ``FK`` cannot point at "one
# of two different tables depending on a sibling column," and enforcing it
# only for the ``document`` case while leaving ``collection`` unconstrained
# would be a half-truth worse than stating the real contract in prose.
#
# Versioning ("a re-summarize supersedes, never overwrites" -- the build
# brief, verbatim): modeled as a same-table versioned-row chain, the same
# spirit ``trialerror.stores.bitemporal.supersede_fact`` uses for ``claim``/
# ``relation`` (assert the replacement, then flag the old row superseded
# and link it) -- but WITHOUT that module's four bi-temporal timestamp
# columns, because a summary has no independent "event-time" axis to speak
# of (unlike a claim, a summary IS the DB's transaction-time belief about
# what a document currently says; there is no second, independent
# "when did this become true in the world" question to ask of it). A plain
# ``status`` + ``supersedes`` pair (the ``artifact``/``source.dedup_of``
# convention) says exactly what's needed: at most one ``status='current'``
# row is the live answer for a given ``(subject_kind, subject_id)``, and
# every prior generation is retained, chained via ``supersedes``, for
# audit/history. Deliberately NO partial-unique index enforcing
# "exactly one current row" at the DDL layer: ``trialerror.stores.bitemporal``'s
# own precedent (``claim``/``relation``) relies on the write API's
# supersede-before-insert ordering, not a DB constraint, for this same
# single-current-row convention, and this table follows that same
# established house pattern rather than a stricter one invented just for
# itself.
#
# ``subject_sha256`` is the staleness key ("summaries_stale (docs newer
# than their summary)" -- the build brief) -- for ``subject_kind=
# 'document'`` this is literally ``document.sha256`` at generation time
# (the exact same value ``quote_anchor.doc_sha256`` already stamps for the
# identical staleness purpose, design Section 4.1's own anchors_dangling
# convention); for ``subject_kind='collection'`` it is a combined hash over
# every member doc's ``(doc_id, sha256)`` pair (``trialerror.summarize.api.
# compute_subject_sha256`` -- the ONE function both the write path and the
# doctor check call, so the two can never independently drift on what
# "stale" means).
_V3 = (
    """
    CREATE TABLE summary (
        summary_id          TEXT PRIMARY KEY,
        subject_kind         TEXT NOT NULL CHECK (subject_kind IN ('document','collection')),
        subject_id            TEXT NOT NULL,
        tier                    TEXT NOT NULL DEFAULT 'L1' CHECK (tier IN ('L1')),
        body                      TEXT NOT NULL,
        word_count                 INTEGER NOT NULL,
        word_cap                     INTEGER NOT NULL,
        source_doc_ids                 TEXT NOT NULL,
        subject_sha256                   TEXT NOT NULL,
        fenced                             INTEGER NOT NULL DEFAULT 0 CHECK (fenced IN (0,1)),
        status                                TEXT NOT NULL CHECK (status IN ('current','superseded')),
        supersedes                             TEXT REFERENCES summary(summary_id),
        procedure_version                        TEXT NOT NULL,
        created_by_launch                          TEXT NOT NULL,
        created_ts                                   TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_summary_subject ON summary(subject_kind, subject_id, status)",
    "CREATE INDEX idx_summary_supersedes ON summary(supersedes)",
)

# ---- schema-v4 (lane a, docs/reviews/LANE_A_WEB_INGESTION_DESIGN.md §2.3)
# -------------------------------------------------------------------------
#
# ``web_fetch``: one row per URL the harness has been asked to fetch, from
# the moment it is enqueued to whatever it finally became. Additive — not one
# existing column moves — and deliberately a NEW table rather than columns on
# ``source``: a fetch is not a source. Most fetches become one (``source_id``/
# ``doc_id`` point at it once extraction lands), but a refused one never
# does, an unchanged re-fetch produces no new source at all, and a superseded
# one has to stay readable beside its replacement. Hanging all of that off
# ``source`` would mean a source row for every 404.
#
# The columns are, in order: the identity and provenance the manifest carried
# out (§2.3's ``pending/<job>.json``), the provenance the sidecar carried back
# (§2.3's ``result.json``, flattened — SQLite has no JSON columns worth the
# name here, and every field a query filters on wants to be a real column),
# the extraction signals of §2.2, and the local paths of the four files a
# fetch leaves on disk.
#
# ``UNIQUE(url_norm) WHERE superseded_by IS NULL`` is the dedup rule of §5,
# enforced by the DDL rather than by the enqueue path alone: at most one LIVE
# row per canonical URL, any number of superseded ones behind it. That is what
# makes ``refresh`` safe — the new row is written, the old row's
# ``superseded_by`` is set, and the index has held throughout. The partial
# predicate is what keeps history: without it, superseding would mean deleting.
#
# ``launch_id`` is NOT NULL and is registered in ``trialerror.stores.xid``
# against ``platform.launch``, so T7's attribution rule ("every fetch carries
# a launch id that exists") is a write-API refusal rather than a convention.
# ``job_id`` is deliberately NOT an XID: jobs.db rows are swept, and a fetch
# record must outlive the job that produced it.
#
# ``superseded_by`` is deliberately NOT a same-file FK, which is a departure
# worth stating rather than leaving to be discovered. SQLite checks same-file
# FKs immediately, and superseding is inherently two statements whose order
# the partial unique index above already fixes: the old row must stop being
# live BEFORE the new one is inserted, so the pointer it would carry names a
# row that does not exist yet. The alternatives were a self-referencing
# placeholder, a deferred-FK transaction that bypasses the validated write
# API for the one insert that most needs it, or an extra ``live`` column that
# says the same thing twice. Leaving the column unconstrained follows the
# house precedent this schema already sets twice -- ``verdict.subject_id``
# and ``summary.subject_id`` are both deliberately non-FK id columns, checked
# by doctor's referential scan rather than by the DDL -- and it keeps the
# design's index predicate verbatim. The crash window is readable rather than
# wrong: an interrupted supersede leaves the old row retired with a pointer
# to a row that never landed, which says exactly what happened.
#
# ``state`` and ``outcome`` are two different questions and both are kept.
# ``outcome`` is what the SIDECAR reported (§2.3's closed three-value set);
# ``state`` is where the research side has got to with it. A row can be
# ``outcome='fetched'`` and ``state='fetched'`` for the minute between the
# fetch handler completing and the extract handler running, and telling those
# apart is exactly what ``webfetch status`` exists for.
_V4 = (
    """
    CREATE TABLE web_fetch (
        fetch_id              TEXT PRIMARY KEY,
        job_id                TEXT,
        launch_id             TEXT NOT NULL,
        program_id            TEXT,
        url                   TEXT NOT NULL,
        url_norm              TEXT NOT NULL,
        final_url             TEXT,
        redirect_chain        TEXT,
        kind                  TEXT NOT NULL CHECK (kind IN ('page','pdf','git')),
        origin                TEXT NOT NULL CHECK (origin IN ('operator_list','agent')),
        list_ref              TEXT,
        state                 TEXT NOT NULL CHECK (
            state IN ('queued','pending','fetched','unchanged','refused','extracted','failed')
        ),
        outcome               TEXT CHECK (outcome IN ('fetched','unchanged','refused')),
        reason                TEXT,
        http_status           INTEGER,
        content_type          TEXT,
        content_class         TEXT CHECK (content_class IN ('html','pdf','text','git')),
        bytes                 INTEGER,
        content_sha256        TEXT,
        extracted_sha256      TEXT,
        resolved_ips          TEXT,
        bytes_out             INTEGER,
        headers_subset        TEXT,
        robots_verdict        TEXT CHECK (
            robots_verdict IN ('allow','disallow','unavailable','n/a')
        ),
        robots_crawl_delay_s  REAL,
        policy_host_rule      TEXT,
        query_stripped        INTEGER CHECK (query_stripped IN (0,1)),
        fetched_ts            TEXT,
        elapsed_ms            INTEGER,
        sidecar_version       TEXT,
        git_head              TEXT,
        git_ref               TEXT,
        git_path              TEXT,
        source_id             TEXT REFERENCES source(source_id),
        doc_id                TEXT REFERENCES document(doc_id),
        superseded_by         TEXT,
        extractor_version     TEXT,
        title                 TEXT,
        author                TEXT,
        published             TEXT,
        canonical_link        TEXT,
        license_detected      TEXT,
        lang                  TEXT,
        extracted_words       INTEGER,
        thin_content          INTEGER CHECK (thin_content IN (0,1)),
        js_markers            TEXT,
        links_json            TEXT,
        tdm_signals_json      TEXT,
        raw_path              TEXT,
        clean_path            TEXT,
        markdown_path         TEXT,
        provenance_path       TEXT,
        created_ts            TEXT NOT NULL,
        updated_ts            TEXT
    )
    """,
    "CREATE UNIQUE INDEX idx_web_fetch_url_norm_live ON web_fetch(url_norm) WHERE superseded_by IS NULL",
    "CREATE INDEX idx_web_fetch_state ON web_fetch(state)",
    "CREATE INDEX idx_web_fetch_source ON web_fetch(source_id)",
    "CREATE INDEX idx_web_fetch_superseded ON web_fetch(superseded_by)",
)

# ---- schema-v5 (lane e, docs/reviews/LANE_E_TERM_STORE_DESIGN.md §2;
# orchestrator ruling L-E1: knowledge v5, following lane a's real v4)
# -------------------------------------------------------------------------
#
# The lexicon term store: the five tables that replace the Lexicon page's
# honest proxy (``entity`` + ``claim WHERE kind='definition'`` + draft
# ``merge_proposal`` rows). Purely additive -- five CREATE TABLEs, one
# virtual table, ten indexes, not one existing column touched, no table
# rebuild.
#
# The design's three-layer mutation contract (§1) is what the DDL is
# shaped around, and it is worth reading the five tables in that order
# rather than alphabetically:
#
#   raw, write-once   ``term_sense_evidence`` -- append-only. A wrong row
#                     gets ``retracted_ts`` + a reason; the row stays.
#                     Nothing in the write API deletes from it.
#   compounding       ``term_sense`` (bi-temporal, the same four timestamp
#                     columns ``claim`` carries) and ``term_relation``
#                     (every judgment ever made, including the rejected and
#                     superseded ones). New rows only.
#   gated head        ``term`` and ``term_alias`` -- the only rows an
#                     accept/merge/scope decision updates in place, and
#                     every such update appends one ``event``, so the head
#                     can always be rebuilt from the two layers under it.
#
# Four DDL decisions that are choices rather than transcription:
#
# 1. ``term.lemma_norm`` is UNIQUE, and that UNIQUE constraint is also the
#    lemma_norm INDEX the design asks for -- SQLite materializes one
#    (``sqlite_autoindex_term_1``) for every UNIQUE column, so a second
#    explicit ``CREATE INDEX`` on the same column would be a duplicate
#    B-tree maintained on every write for no read that could not use the
#    first. Stated here because "indexes: lemma_norm, status, granularity"
#    reads, at a glance, like three CREATE INDEX statements are missing one.
#
# 2. ``term.preferred_sense_id`` is deliberately NOT a foreign key, and
#    ``term.merged_into`` deliberately IS one. The difference is ordering:
#    a term's preferred sense is written in the same breath as the term
#    itself (the sense's ``term_id`` points back, so one of the two
#    pointers must name a row that does not exist yet), while
#    ``merged_into`` is set long afterwards, against a canonical term that
#    has existed for as long as the merge has been a candidate. This is the
#    house precedent (``verdict.subject_id``, ``summary.subject_id``,
#    ``web_fetch.superseded_by``) applied to the one column that needs it,
#    not to both.
#
# 3. ``term_sense``'s ``UNIQUE(origin_kind, origin_ref) WHERE origin_ref IS
#    NOT NULL`` is what makes every proposal route idempotent BY
#    CONSTRUCTION rather than by the backfill remembering to check first.
#    Re-running the record import over the same rows inserts nothing; the
#    partial predicate is what keeps the manual route (``origin_ref IS
#    NULL``, any number of them) working beside it. It also has a
#    consequence the write API has to honor and does: a superseding sense
#    cannot re-use its predecessor's ``origin_ref``, because the raw origin
#    belongs to the row that was actually derived from it. The correction's
#    lineage is the ``superseded_by`` chain plus a ``prov_edge``, not a
#    second claim on the same origin.
#
# 4. ``term_sense_evidence``'s uniqueness is an EXPRESSION index --
#    ``UNIQUE(sense_id, evidence_kind, COALESCE(anchor_id, ref_id))`` --
#    because the identifying column differs by kind (``anchor_id`` for
#    quote-anchor evidence, ``ref_id`` for record/claim/idea evidence) and
#    a plain three-column UNIQUE over both would let the same anchor be
#    attached twice, once through each column. ``source_key`` is
#    deliberately NOT part of it: the same source may legitimately back one
#    sense through several distinct anchors.
#
# ``term_relation`` carries no ``UNIQUE(src, dst)`` on purpose (the engram
# schema's own comment, ``docs/mining/G25-operator-2026-09__engram.md``):
# two actors disagreeing about the same pair is a fact about the program,
# and a unique constraint would make it unrepresentable. ``marked_by_kind``
# is the machine/human boundary MINING §5.3 draws -- a ``'system'`` row can
# only ever be opened ``'pending'``, and the API (not the DDL, which cannot
# see across a status change) is what refuses to confirm one without a
# ``decided_by_launch``. The doctor check ``term_system_relation_decided``
# audits the same invariant from the other side.
#
# ``verdict`` was considered for the decision rows and rejected: its
# ``subject_kind`` CHECK is a closed four-value vocabulary, so adding
# ``term`` would mean the v2 table-rebuild recipe on a table three other
# subsystems write. ``term_relation`` carries the same provenance fields.
#
# ``term_fts`` is a trigram FTS5 index over lemma + aliases + current
# glosses, maintained by ``trialerror.lexicon.api`` on every write and
# parity-checked by the ``term_fts_in_sync`` doctor check. Trigram
# tokenization needs SQLite >= 3.34; Python 3.12's bundled build is well
# past that, and ``trialerror.lexicon`` asserts it at import with a named
# error rather than letting a CREATE TABLE fail inside a migration.
_V5 = (
    # created_by_launch is XID -> platform.launch.
    """
    CREATE TABLE term (
        term_id             TEXT PRIMARY KEY,
        lemma               TEXT NOT NULL,
        lemma_norm          TEXT NOT NULL UNIQUE,
        granularity         TEXT CHECK (granularity IN ('family','instance')),
        tags                TEXT,
        entity_id           TEXT REFERENCES entity(entity_id),
        status              TEXT NOT NULL CHECK (
            status IN ('proposed','active','split','merged','retired')
        ),
        preferred_sense_id  TEXT,
        merged_into         TEXT REFERENCES term(term_id),
        created_by_launch   TEXT NOT NULL,
        created_at          TEXT NOT NULL,
        updated_ts          TEXT
    )
    """,
    "CREATE INDEX idx_term_status ON term(status)",
    "CREATE INDEX idx_term_granularity ON term(granularity)",
    # created_by_launch is XID -> platform.launch. Lookup across the lexicon
    # is `term.lemma_norm UNION term_alias.alias_norm` -- plurals, spellings
    # and abbreviations are aliases, never folded by a stemmer.
    """
    CREATE TABLE term_alias (
        alias_id           TEXT PRIMARY KEY,
        term_id            TEXT NOT NULL REFERENCES term(term_id),
        alias              TEXT NOT NULL,
        alias_norm         TEXT NOT NULL,
        kind               TEXT NOT NULL CHECK (
            kind IN ('variant','abbreviation','plural','former_lemma','other')
        ),
        created_by_launch  TEXT NOT NULL,
        created_ts         TEXT NOT NULL,
        UNIQUE (term_id, alias_norm)
    )
    """,
    "CREATE INDEX idx_term_alias_norm ON term_alias(alias_norm)",
    # bi-temporal (the claim/relation 4-timestamp set; registered in
    # trialerror.stores.bitemporal.BITEMPORAL_TABLES). proposed_by_launch and
    # decided_by_launch are XIDs -> platform.launch. origin_ref is
    # polymorphic by origin_kind -- claim_id / record_id / idea_id / NULL --
    # and therefore deliberately not a FK, the verdict.subject_id precedent.
    """
    CREATE TABLE term_sense (
        sense_id            TEXT PRIMARY KEY,
        term_id             TEXT NOT NULL REFERENCES term(term_id),
        gloss               TEXT NOT NULL,
        disambiguator       TEXT,
        origin_kind         TEXT NOT NULL CHECK (
            origin_kind IN ('extract','record_import','ideation','manual')
        ),
        origin_ref          TEXT,
        confidence          REAL,
        procedure_version   TEXT NOT NULL,
        status              TEXT NOT NULL CHECK (
            status IN ('proposed','current','superseded','rejected','retired')
        ),
        created_at          TEXT NOT NULL,
        expired_at          TEXT,
        valid_at            TEXT,
        invalid_at          TEXT,
        superseded_by       TEXT REFERENCES term_sense(sense_id),
        proposed_by_launch  TEXT NOT NULL,
        decided_by_launch   TEXT,
        decided_ts          TEXT,
        review_after        TEXT,
        reviewed_ts         TEXT
    )
    """,
    "CREATE UNIQUE INDEX idx_term_sense_origin ON term_sense(origin_kind, origin_ref) "
    "WHERE origin_ref IS NOT NULL",
    "CREATE INDEX idx_term_sense_term_status ON term_sense(term_id, status)",
    "CREATE INDEX idx_term_sense_expired ON term_sense(expired_at)",
    "CREATE INDEX idx_term_sense_review_after ON term_sense(review_after)",
    # created_by_launch is XID -> platform.launch. `source_key` is the
    # identity the disjoint-source conflict rule counts: a real
    # source.source_id where one exists (anchor and claim evidence reach one
    # through quote_anchor.doc_id -> document.source_id), else the record's
    # register_key. `trialerror term relink` rewrites the latter into the
    # former once those sources are ingested; the doctor check
    # term_evidence_source_unlinked counts what is still waiting.
    """
    CREATE TABLE term_sense_evidence (
        evidence_id        TEXT PRIMARY KEY,
        sense_id           TEXT NOT NULL REFERENCES term_sense(sense_id),
        evidence_kind      TEXT NOT NULL CHECK (
            evidence_kind IN ('quote_anchor','record','claim','idea')
        ),
        anchor_id          TEXT REFERENCES quote_anchor(anchor_id),
        ref_id             TEXT,
        source_key         TEXT NOT NULL,
        cite_raw           TEXT,
        excerpt            TEXT,
        created_by_launch  TEXT NOT NULL,
        created_ts         TEXT NOT NULL,
        retracted_ts       TEXT,
        retracted_reason   TEXT
    )
    """,
    "CREATE UNIQUE INDEX idx_term_sense_evidence_identity ON term_sense_evidence("
    "sense_id, evidence_kind, COALESCE(anchor_id, ref_id))",
    "CREATE INDEX idx_term_sense_evidence_sense ON term_sense_evidence(sense_id)",
    "CREATE INDEX idx_term_sense_evidence_source ON term_sense_evidence(source_key)",
    # marked_by_launch and decided_by_launch are XIDs -> platform.launch.
    # src_id/dst_id are polymorphic by src_kind/dst_kind (a term_id or a
    # sense_id) and therefore not FKs -- the verdict.subject_id precedent
    # again. No UNIQUE(src, dst): see the block comment above.
    """
    CREATE TABLE term_relation (
        rel_id             TEXT PRIMARY KEY,
        src_kind           TEXT NOT NULL CHECK (src_kind IN ('term','sense')),
        src_id             TEXT NOT NULL,
        dst_kind           TEXT NOT NULL CHECK (dst_kind IN ('term','sense')),
        dst_id             TEXT NOT NULL,
        verb               TEXT NOT NULL CHECK (
            verb IN ('same_as','variant_of','conflicts_with','scoped','supersedes',
                     'not_conflict','unrelated')
        ),
        decided_verb       TEXT CHECK (
            decided_verb IN ('same_as','variant_of','conflicts_with','scoped','supersedes',
                             'not_conflict','unrelated')
        ),
        status             TEXT NOT NULL CHECK (
            status IN ('pending','confirmed','rejected','superseded')
        ),
        reason             TEXT,
        evidence           TEXT,
        confidence         REAL,
        marked_by_kind     TEXT NOT NULL CHECK (marked_by_kind IN ('system','launch')),
        marked_by_launch   TEXT,
        marked_by_model    TEXT,
        marked_ts          TEXT NOT NULL,
        decided_by_launch  TEXT,
        decided_ts         TEXT,
        superseded_by      TEXT REFERENCES term_relation(rel_id)
    )
    """,
    "CREATE INDEX idx_term_relation_status_verb ON term_relation(status, verb)",
    "CREATE INDEX idx_term_relation_src ON term_relation(src_kind, src_id)",
    "CREATE INDEX idx_term_relation_dst ON term_relation(dst_kind, dst_id)",
    # Trigram (not porter): the lexicon's queries are substring and
    # near-miss duplicate detection over short lemmas, which porter stemming
    # actively hurts. Tiny (~10k rows) and separate from chunk_fts, so a
    # later swap of the chunk index does not touch it.
    """
    CREATE VIRTUAL TABLE term_fts USING fts5(
        term_id UNINDEXED,
        text,
        tokenize = 'trigram'
    )
    """,
)

# ---- schema-v6 (the ideation idea record: two new statuses + ten promoted
# columns; docs/AIIF_DESIGN.md Section 5, the idea record) ------------------
#
# Two changes, both on ``idea``:
#
# 1. ``status`` gains ``eliminated`` and ``merged``. A round's convergence
#    phase either kills an idea (the both-or-eliminated rule) or folds a
#    near-duplicate into another record; before this migration both outcomes
#    had to masquerade as one of raw/consolidated/promoted, which loses the
#    distinction the archive exists FOR (an eliminated idea stays in the
#    never-reset reference set and is revivable by ruling -- it is not
#    "raw", and it is certainly not "promoted"). SQLite cannot widen a CHECK
#    in place, so this is the documented table-rebuild recipe (new table,
#    copy, DROP, RENAME) -- ``trialerror.stores.migrate.apply_migrations``
#    toggles ``PRAGMA foreign_keys`` around the whole migration transaction
#    for exactly this case. ``idea`` has no same-file FK child
#    (``room_link.idea_id`` is a cross-DB XID in ops.db, validated by
#    ``trialerror.stores.writer``, never a SQLite REFERENCES clause) and no
#    indexes, so nothing needs re-creating after the rename.
#
# 2. Ten record fields are promoted from the ``provenance`` JSON blob to
#    real columns: ``requirements``, ``recipe_card``, ``operation_declared``,
#    ``probe``, ``surprise``, ``author_rationale``, ``parent_ids`` (JSON
#    array), ``statement_sha256``, ``corpus_snapshot_id``,
#    ``convergent_with`` (JSON array). Same posture schema-v2 took for the
#    first five promoted fields: the columns land all-NULL, the interim JSON
#    convention is NOT deleted, and ``trialerror.lens.ideas.read_idea`` reads
#    column-then-``provenance``-JSON so every row written before this
#    migration keeps answering with the values it always did.
#
# Deliberately no CHECK on ``recipe_card``/``operation_declared``: the card
# catalogue rotates between rounds and the declared-operation vocabulary is
# a measured distribution, not a closed enum the schema may freeze (a CHECK
# here would make adding one card a migration).
_V6 = (
    """
    CREATE TABLE idea__v6new (
        idea_id             TEXT PRIMARY KEY,
        round_id            TEXT,
        author_launch       TEXT NOT NULL,
        body                TEXT NOT NULL,
        slice_ref           TEXT,
        feed_post_ref       TEXT,
        status              TEXT NOT NULL CHECK (
            status IN ('raw','consolidated','promoted','eliminated','merged')
        ),
        created_ts          TEXT NOT NULL,
        home                TEXT,
        assumed_circle      TEXT,
        provenance          TEXT,
        tier                TEXT CHECK (tier IN ('near','moderate','far')),
        set_distance        REAL,
        requirements        TEXT,
        recipe_card         TEXT,
        operation_declared  TEXT,
        probe               TEXT,
        surprise            TEXT,
        author_rationale    TEXT,
        parent_ids          TEXT,
        statement_sha256    TEXT,
        corpus_snapshot_id  TEXT,
        convergent_with     TEXT
    )
    """,
    """
    INSERT INTO idea__v6new (
        idea_id, round_id, author_launch, body, slice_ref, feed_post_ref, status,
        created_ts, home, assumed_circle, provenance, tier, set_distance
    )
    SELECT idea_id, round_id, author_launch, body, slice_ref, feed_post_ref, status,
           created_ts, home, assumed_circle, provenance, tier, set_distance
    FROM idea
    """,
    "DROP TABLE idea",
    "ALTER TABLE idea__v6new RENAME TO idea",
)

# ---- schema-v7 (framework stage B): ``source.kind`` gains ``inventory`` --
#
# The novelty screen measures every idea against named reference sets, and
# one of them is the program's own structured inventory -- one row per
# entry, ingested as ordinary documents so the retrieval engine, the
# license fence and the embedding pipeline all cover it with no second code
# path (design Sections 3 and 4). It needs its own ``source.kind`` for two
# reasons that are not cosmetic:
#
# 1. The kind is the key the SERVER-SIDE default exclusion filters on
#    (``trialerror.retrieve.engine.DEFAULT_EXCLUDED_KINDS``). A generator
#    that can retrieve the inventory is reading the reference set of the
#    screen that judges it, so the barrier has to be a value the engine can
#    filter by -- not a sentence in a prompt.
# 2. The kind is what tells the chunk stage to cut one chunk per ROW rather
#    than run the prose chunker over a table
#    (``trialerror.ingest.chunker.build_row_chunks``). An inventory row is
#    the unit of comparison; a chunk spanning six of them would make every
#    distance measured against it mean something other than what it says.
#
# SQLite cannot widen a CHECK in place, so this is the same documented
# table-rebuild recipe schema-v6 used for ``idea`` (new table, copy, DROP,
# RENAME) under ``trialerror.stores.migrate.apply_migrations``' own ``PRAGMA
# foreign_keys`` bracketing -- which is what makes dropping a table two
# others reference (``document.source_id``, ``web_fetch.source_id``) legal
# mid-migration: both name ``source``, so they resolve again the moment the
# rename lands. Unlike ``idea``, ``source`` carries an index of its own --
# the partial UNIQUE on ``content_sha256`` that makes a silent duplicate
# registration structurally impossible -- and an index dies with its table,
# so it is re-created below. Losing it would fail no test; it would quietly
# stop deduplicating, which is exactly the class of bug a rebuild invites.
_V7 = (
    """
    CREATE TABLE source__v7new (
        source_id             TEXT PRIMARY KEY,
        kind                  TEXT NOT NULL CHECK (
            kind IN ('paper','book','web','rulebook','dataset','report','inventory','other')
        ),
        title                 TEXT NOT NULL,
        authors               TEXT,
        year                  INTEGER,
        venue                 TEXT,
        url                   TEXT,
        doi                   TEXT,
        arxiv_id              TEXT,
        isbn                  TEXT,
        content_sha256        TEXT,
        license_tier          TEXT NOT NULL CHECK (
            license_tier IN ('open','academic_oa','user_owned_scan','commercial_restricted','unknown')
        ),
        acquisition_route     TEXT NOT NULL CHECK (
            acquisition_route IN (
                'author_posted','institutional','publisher_oa','user_scan','user_delivered','api','web'
            )
        ),
        rights_notes          TEXT,
        request_state         TEXT NOT NULL CHECK (
            request_state IN (
                'wanted','requested','delivered','verifying','archived','indexed','rejected','failed'
            )
        ),
        requested_ts          TEXT,
        delivered_ts          TEXT,
        registered_ts         TEXT NOT NULL,
        registered_by_launch  TEXT NOT NULL,
        dedup_of              TEXT REFERENCES source(source_id)
    )
    """,
    """
    INSERT INTO source__v7new (
        source_id, kind, title, authors, year, venue, url, doi, arxiv_id, isbn,
        content_sha256, license_tier, acquisition_route, rights_notes, request_state,
        requested_ts, delivered_ts, registered_ts, registered_by_launch, dedup_of
    )
    SELECT source_id, kind, title, authors, year, venue, url, doi, arxiv_id, isbn,
           content_sha256, license_tier, acquisition_route, rights_notes, request_state,
           requested_ts, delivered_ts, registered_ts, registered_by_launch, dedup_of
    FROM source
    """,
    "DROP TABLE source",
    "ALTER TABLE source__v7new RENAME TO source",
    "CREATE UNIQUE INDEX idx_source_content_sha256 ON source(content_sha256) WHERE content_sha256 IS NOT NULL",
)

# ---- schema-v8 (lane FB-5 item 2): ``verdict.label_canonical`` ----------
#
# The judged novelty screen lets a round spell its own label vocabulary and
# declare a mapping onto the design's fixed one (``requested`` -> ``same``,
# ``new`` -> ``new-mechanism``, ...). Both halves have to be on the ROW:
# ``label`` so the round can read back what its judge actually returned, and
# ``label_canonical`` so every count downstream of the screen -- the
# adjudication draft, the gate suite's bundle check, any report that says
# "three records were labelled same" -- keeps reading one vocabulary however
# many rounds spell it differently.
#
# A plain ADD COLUMN rather than the table-rebuild recipe v6/v7 needed: no
# CHECK is being widened, so SQLite can do this in place. The column is
# nullable on purpose -- every verdict written by a procedure with ONE
# vocabulary (citecheck, contracrow, gate, reproduction) leaves it NULL,
# which says "``label`` is already canonical" rather than duplicating it.
_V8 = (
    "ALTER TABLE verdict ADD COLUMN label_canonical TEXT",
)

# ---- schema-v9 (lane FB-5 item 4): ``idea.status`` gains ``archived`` ----
#
# A round that judges against its ARCHIVE (reference set R2) needs the prior
# rounds' candidates and request rows to BE idea rows -- that is what makes
# them retrievable as archive rows, embedded by the same path and shown to a
# judge in the same envelope shape. They are not this round's records
# though, and every existing status says something false about them:
# ``raw`` would put them in the judged scope and consolidate them at the end
# of a round they did not take part in; ``eliminated`` asserts a convergence
# ruling nobody made; ``merged`` asserts a near-duplicate fold.
#
# ``archived`` says the one true thing: this row is in the reference set and
# is not a candidate. The screen reads it as exactly that -- an archived row
# is never consolidated, is never the survivor of a near-duplicate merge,
# and a live record that lands on one carries an ``archive_hit`` flag rather
# than being folded into it.
#
# SQLite cannot widen a CHECK in place, so this is the same documented
# table-rebuild recipe v6 used for this very table (new table, copy, DROP,
# RENAME) under ``trialerror.stores.migrate.apply_migrations``' own ``PRAGMA
# foreign_keys`` bracketing. ``idea`` carries no index of its own, so unlike
# v7's ``source`` rebuild there is none to re-create.
_V9 = (
    """
    CREATE TABLE idea__v9new (
        idea_id             TEXT PRIMARY KEY,
        round_id            TEXT,
        author_launch       TEXT NOT NULL,
        body                TEXT NOT NULL,
        slice_ref           TEXT,
        feed_post_ref       TEXT,
        status              TEXT NOT NULL CHECK (
            status IN ('raw','consolidated','promoted','eliminated','merged','archived')
        ),
        created_ts          TEXT NOT NULL,
        home                TEXT,
        assumed_circle      TEXT,
        provenance          TEXT,
        tier                TEXT CHECK (tier IN ('near','moderate','far')),
        set_distance        REAL,
        requirements        TEXT,
        recipe_card         TEXT,
        operation_declared  TEXT,
        probe               TEXT,
        surprise            TEXT,
        author_rationale    TEXT,
        parent_ids          TEXT,
        statement_sha256    TEXT,
        corpus_snapshot_id  TEXT,
        convergent_with     TEXT
    )
    """,
    """
    INSERT INTO idea__v9new (
        idea_id, round_id, author_launch, body, slice_ref, feed_post_ref, status,
        created_ts, home, assumed_circle, provenance, tier, set_distance,
        requirements, recipe_card, operation_declared, probe, surprise,
        author_rationale, parent_ids, statement_sha256, corpus_snapshot_id, convergent_with
    )
    SELECT idea_id, round_id, author_launch, body, slice_ref, feed_post_ref, status,
           created_ts, home, assumed_circle, provenance, tier, set_distance,
           requirements, recipe_card, operation_declared, probe, surprise,
           author_rationale, parent_ids, statement_sha256, corpus_snapshot_id, convergent_with
    FROM idea
    """,
    "DROP TABLE idea",
    "ALTER TABLE idea__v9new RENAME TO idea",
)

# ---- schema-v10 (lane FB-6 item 1): ``vec_ideas``, the idea-vector cache --
#
# R2 -- the archive of ``idea`` rows -- is a reference set of RECORDS, not of
# chunks, so no ``vec_chunks__<model>`` table holds it and
# ``trialerror.lens.novelty._archive_rows`` embedded every row through the
# backend on every batch build. On a live programme that was 76 rows re-
# embedded per ``--judged-prep`` and per ``--calibration`` (minutes per build,
# and growing with the archive for the rest of the programme's life), for
# vectors that are a pure function of the statement and the model.
#
# The cache is keyed by ``(idea_id, model_key)`` and carries the SHA-256 of
# the exact text that was embedded: a row whose statement changed hashes
# differently and is re-embedded, and a program that switches embedding
# models simply misses every row rather than silently mixing two vector
# spaces in one ranking. ``vector`` is little-endian float64 rather than the
# float32 ``vec_chunks`` carries (which mirrors sqlite-vec's own fixed-width
# column): this table is a cache, and a cached vector that differed from a
# freshly embedded one in the seventh decimal would rank the archive
# minutely differently depending on whether the cache was warm.
#
# No FOREIGN KEY onto ``idea``: v6 and v9 both rebuilt that table with the
# DROP/RENAME recipe SQLite requires for a widened CHECK, and a child table
# would have to be rebuilt with it every time. A cache row for a deleted
# idea is harmless -- nothing reads it without naming the id -- and the
# statement hash is what decides whether a hit is usable anyway.
_V10 = (
    """
    CREATE TABLE vec_ideas (
        idea_id             TEXT NOT NULL,
        model_key           TEXT NOT NULL,
        statement_sha256    TEXT NOT NULL,
        dim                 INTEGER NOT NULL,
        vector              BLOB NOT NULL,
        created_ts          TEXT NOT NULL,
        PRIMARY KEY (idea_id, model_key)
    )
    """,
)

# ---- schema-v11 (lane FB-7 item 8b): ``idea.extra`` ---------------------
#
# FB-6 item 6 gave a PLANT an ``extra`` block: any keys at all, rendered into
# one ``key: value`` text field the judge sees. A round writing its plants by
# hand wanted ``literature``, ``unlock`` and ``seeds`` on each of them -- the
# same three keys its own records carry -- and the envelope's shape is what
# keeps a plant indistinguishable from a record, so a plant carrying three
# keys no record has would be pickable on its shape alone.
#
# That cuts both ways, and this is the other half: a ROUND's own records had
# nowhere to put the same three keys either, so the text was folded into the
# statement by hand, in a different place for each record. One nullable
# column, holding the raw JSON object exactly as the intake file declared it;
# the rendered one-block form is derived on read by the same
# ``render_extra_text`` a plant's is, so the two can never drift.
#
# ADD COLUMN rather than the DROP/RENAME rebuild v6 and v9 needed: nothing
# about ``idea``'s CHECK constraints changes, and a rebuild of a table three
# other migrations already rebuilt is risk for nothing.
_V11 = (
    "ALTER TABLE idea ADD COLUMN extra TEXT",
)

# ---- schema-v12 (lane FB-7 item 9): ``verdict.round_id``/``batch_id`` ----
#
# The novelty recorder's one-submission rule -- design 5.2(3), "one
# submission per idea per judge" -- was keyed by ``(subject_id,
# reference_set, procedure_version)`` and nothing else. That is right for a
# RECORD, whose id is unique across the programme. It is wrong for a PLANT,
# whose ``plant_id`` is whatever the round's plants file called it: a live
# supplement battery was refused because twelve of its plants re-used the
# ids of an earlier battery's ("already carry a novelty-v2-calibration
# verdict"), and the only offered way through, ``--supersede``, would have
# marked the EARLIER round's rows as replaced -- rewriting settled history
# to record a new battery.
#
# The key wanted is ``(round_id, batch_id, subject_id, reference_set,
# procedure_version)``, and no existing column carried the first two.
# ``prereg_id`` is optional and usually NULL; ``evidence`` is free text a
# guard must not parse; there is no ``attrs``. So: two nullable columns,
# ADD COLUMN (nothing about ``verdict``'s CHECK constraints changes).
#
# ``issued_by`` is deliberately NOT in that key (lane FB-7 fix pass, V-9).
# The lane's own docs listed it; the guard never selected, compared or
# returned ``issued_by_launch``, which makes the implemented rule STRICTER
# than the documented one -- a second launch re-recording the same subject
# is refused, which is what design 5.2(3) wants -- so the code was right and
# the sentence was not. Keying on the launch would let one round record the
# same idea twice by booking a second launch, which is the hole the rule
# exists to close.
#
# EXISTING ROWS ARE NULL, and that is load-bearing rather than incidental. A
# migration cannot read a round's batch files, so "backfilled where
# derivable" is derivable from nowhere in SQL. A NULL round is UNKNOWN, not
# "no round" -- so the guard treats a NULL-round row as possibly this
# round's and still blocks on it, which is exactly the behaviour every
# pre-migration row had. Rows written from here on carry their round and are
# scoped by it.
_V12 = (
    "ALTER TABLE verdict ADD COLUMN round_id TEXT",
    "ALTER TABLE verdict ADD COLUMN batch_id TEXT",
    "CREATE INDEX idx_verdict_round ON verdict(round_id, batch_id, subject_id)",
)

MIGRATIONS = (
    Migration(version=1, name="knowledge_v1_initial_schema", statements=_V1),
    Migration(version=2, name="knowledge_v2_idea_promoted_columns", statements=_V2),
    Migration(version=3, name="knowledge_v3_summary_table", statements=_V3),
    Migration(version=4, name="knowledge_v4_web_fetch_table", statements=_V4),
    Migration(version=5, name="knowledge_v5_lexicon_term_store", statements=_V5),
    Migration(version=6, name="knowledge_v6_idea_aiif_columns_and_statuses", statements=_V6),
    Migration(version=7, name="knowledge_v7_source_kind_inventory", statements=_V7),
    Migration(version=8, name="knowledge_v8_verdict_label_canonical", statements=_V8),
    Migration(version=9, name="knowledge_v9_idea_status_archived", statements=_V9),
    Migration(version=10, name="knowledge_v10_vec_ideas", statements=_V10),
    Migration(version=11, name="knowledge_v11_idea_extra", statements=_V11),
    Migration(version=12, name="knowledge_v12_verdict_round_scope", statements=_V12),
)
