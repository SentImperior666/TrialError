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

MIGRATIONS = (
    Migration(version=1, name="knowledge_v1_initial_schema", statements=_V1),
    Migration(version=2, name="knowledge_v2_idea_promoted_columns", statements=_V2),
    Migration(version=3, name="knowledge_v3_summary_table", statements=_V3),
)
