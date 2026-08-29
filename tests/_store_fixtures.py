"""Not a test module (pytest only collects ``test_*.py``) — a shared
builder of one minimal, fully-valid row per table, inserted in dependency
order. Used by the schema round-trip test and reused wherever another test
needs "a store that already has one of everything" rather than hand-rolling
the same 42-table dependency chain again.
"""

from __future__ import annotations

from trialerror.stores import insert
from trialerror.stores.store import Store
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now


def populate_one_of_everything(store: Store) -> dict[str, str]:
    """Insert exactly one valid row into every base table across all four
    DBs, respecting same-file ``FK`` and cross-file ``XID`` dependency
    order. Returns a dict of the ids created, keyed by table name, so
    callers can assert against them."""
    ids: dict[str, str] = {}

    # ---- platform.db -----------------------------------------------------
    ids["account"] = new_id("ACC")
    insert(store, "account", {"account_id": ids["account"], "label": "test account", "created_ts": now()})

    # session (ops.db) needs account (platform.db) via XID — created before
    # launch, which needs BOTH account (same-file FK) and session (XID).
    ids["session"] = new_id("SESS")
    insert(
        store,
        "session",
        {
            "session_id": ids["session"],
            "account_id": ids["account"],
            "opened_ts": now(),
            "status": "open",
        },
    )

    ids["launch"] = new_id("LNCH")
    insert(
        store,
        "launch",
        {
            "launch_id": ids["launch"],
            "account_id": ids["account"],
            "program_id": "PROG-test",
            "session_id": ids["session"],
            "agent_kind": "tester",
            "model_class": "top",
            "model": "sonnet",
            "purpose": "fixture",
            "est_tokens": 100,
            "booked_ts": now(),
            "state": "PROVISIONAL",
        },
    )

    ids["budget_pool"] = new_id("POOL")
    insert(
        store,
        "budget_pool",
        {
            "pool_id": ids["budget_pool"],
            "account_id": ids["account"],
            "model_class": "top",
            "period": "weekly",
            "period_start": now(),
            "cap_tokens": 1_000_000,
            "updated_ts": now(),
        },
    )

    ids["quota_snapshot"] = new_id("QSNAP")
    insert(
        store,
        "quota_snapshot",
        {
            "snap_id": ids["quota_snapshot"],
            "account_id": ids["account"],
            "ts": now(),
            "source": "screenshot",
            "payload": "{}",
        },
    )

    ids["calibration"] = new_id("CALIB")
    insert(
        store,
        "calibration",
        {
            "calib_id": ids["calibration"],
            "account_id": ids["account"],
            "model_class": "top",
            "window": "7d",
            "multiplier": 2.75,
            "derived_from": "[]",
            "ts": now(),
        },
    )

    # ---- ops.db ------------------------------------------------------------
    ids["ruling"] = "C-0001"
    insert(
        store,
        "ruling",
        {
            "ruling_id": ids["ruling"],
            "ts": now(),
            "summary": "test ruling",
            "status": "active",
            "ledger_sha256_after": "0" * 64,
        },
    )

    ids["law_digest"] = "v1"
    insert(
        store,
        "law_digest",
        {
            "version": ids["law_digest"],
            "generated_ts": now(),
            "content_sha256": "1" * 64,
            "rendered_path": "law/LAW_DIGEST.md",
        },
    )

    ids["template"] = "note"
    insert(
        store,
        "template",
        {
            "type_key": ids["template"],
            "title": "Note",
            "version": "1",
            "path": "templates/note.md",
            "gated": 0,
        },
    )

    ids["artifact"] = new_id("ART")
    insert(
        store,
        "artifact",
        {
            "artifact_id": ids["artifact"],
            "type": ids["template"],
            "title": "test artifact",
            "path": "artifacts/test.md",
            "sha256": "2" * 64,
            "status": "draft",
            "registered_by_launch": ids["launch"],
        },
    )

    ids["gate"] = new_id("CR")
    insert(
        store,
        "gate",
        {
            "gate_id": ids["gate"],
            "artifact_id": ids["artifact"],
            "state": "draft",
        },
    )

    ids["gate_transition"] = str(
        insert(
            store,
            "gate_transition",
            {
                "gate_id": ids["gate"],
                "from_state": "draft",
                "to_state": "submitted",
                "ts": now(),
                "by_launch": ids["launch"],
            },
        )
    )

    ids["event"] = new_id("EVT")
    insert(
        store,
        "event",
        {
            "event_id": ids["event"],
            "ts": now(),
            "session_id": ids["session"],
            "launch_id": ids["launch"],
            "workpackage": "WKP-000_test",
            "type": "test_event",
            "payload": "{}",
        },
    )

    ids["thread"] = new_id("THR")
    insert(
        store,
        "thread",
        {
            "thread_id": ids["thread"],
            "title": "test thread",
            "created_ts": now(),
            "created_by_launch": ids["launch"],
        },
    )

    ids["feed_post"] = new_id("POST")
    insert(
        store,
        "feed_post",
        {
            "post_id": ids["feed_post"],
            "thread_id": ids["thread"],
            "author": f"launch:{ids['launch']}",
            "launch_id": ids["launch"],
            "ts": now(),
            "body": "test post body",
        },
    )

    ids["inbox_item"] = new_id("INBX")
    insert(
        store,
        "inbox_item",
        {
            "item_id": ids["inbox_item"],
            "ts": now(),
            "body": "test inbox item",
            "source": "user",
        },
    )

    ids["prereg"] = new_id("PREG")
    insert(
        store,
        "prereg",
        {
            "prereg_id": ids["prereg"],
            "title": "test prereg",
            "procedure_sha256": "3" * 64,
            "params_sha256": "4" * 64,
            "committed_ts": now(),
            "escrow_path": "escrow/test",
            "status": "committed",
        },
    )

    ids["lens_roster"] = new_id("ROST")
    insert(
        store,
        "lens_roster",
        {
            "roster_id": ids["lens_roster"],
            "round_id": "round-1",
            "lens_name": "skeptic",
            "vantage": "adversarial",
            "seat": "standard",
            "model_class": "top",
            "created_ts": now(),
        },
    )

    ids["lens_assignment"] = new_id("ASGN")
    insert(
        store,
        "lens_assignment",
        {
            "assign_id": ids["lens_assignment"],
            "roster_id": ids["lens_roster"],
            "slice_spec": "{}",
            "arm": "near",
            "seed": "seed-1",
            "launch_id": ids["launch"],
            "created_ts": now(),
        },
    )

    ids["memory_item"] = new_id("MEM")
    insert(
        store,
        "memory_item",
        {
            "memory_item_id": ids["memory_item"],
            "key": "origin-project-orchestrator-working-rules",
            "tier": "L0",
            "kind": "rule",
            "body": "test memory body",
            "updated_ts": now(),
            "account_id": ids["account"],
        },
    )

    ids["room"] = new_id("ROOM")
    insert(
        store,
        "room",
        {
            "room_id": ids["room"],
            "topic": "test room",
            "dps": "[]",
            "state": "open",
        },
    )

    ids["room_turn"] = ids["room"]  # composite PK (room_id, seq); keep room_id for reference
    insert(
        store,
        "room_turn",
        {
            "room_id": ids["room"],
            "seq": 1,
            "author_launch": ids["launch"],
            "dp_ref": "dp-1",
            "body": "test turn body",
        },
    )

    ids["room_score"] = ids["room"]  # composite PK (room_id, dp_id); keep room_id for reference
    insert(
        store,
        "room_score",
        {
            "room_id": ids["room"],
            "dp_id": "dp-1",
            "agreement_pct": 92.5,
        },
    )

    # ---- ops.db, ops_v4 (build-v2dash-data) -------------------------------
    ids["criterion"] = "G-01"
    insert(
        store,
        "criterion",
        {
            "criterion_id": ids["criterion"],
            "label": "test criterion",
            "phase": "test-phase",
            "state": "discharged",
            "discharged_by_artifact": ids["artifact"],
        },
    )

    ids["feed_post_translation"] = new_id("XLAT")
    insert(
        store,
        "feed_post_translation",
        {
            "translation_id": ids["feed_post_translation"],
            "post_id": ids["feed_post"],
            "translator_version": "1",
            "style_mode": "flavored",
            "body": "test translation body",
            "original_sha256": "8" * 64,
            "status": "current",
            "created_by_launch": ids["launch"],
            "created_ts": now(),
        },
    )

    # ---- knowledge.db --------------------------------------------------------
    ids["source"] = new_id("SRC")
    insert(
        store,
        "source",
        {
            "source_id": ids["source"],
            "kind": "paper",
            "title": "test source",
            "license_tier": "open",
            "acquisition_route": "web",
            "request_state": "indexed",
            "registered_ts": now(),
            "registered_by_launch": ids["launch"],
        },
    )

    ids["document"] = new_id("DOC")
    insert(
        store,
        "document",
        {
            "doc_id": ids["document"],
            "source_id": ids["source"],
            "rel_path": "archive/test.md",
            "media_type": "pdf",
            "normalizer_id": "pdf-text",
            "normalizer_version": "1",
            "sha256": "5" * 64,
            "status": "indexed",
        },
    )

    ids["element"] = new_id("ELM")
    insert(
        store,
        "element",
        {
            "element_id": ids["element"],
            "doc_id": ids["document"],
            "seq": 1,
            "type": "NarrativeText",
            "text": "hello world",
        },
    )

    ids["chunk"] = new_id("CHK")
    insert(
        store,
        "chunk",
        {
            "chunk_id": ids["chunk"],
            "doc_id": ids["document"],
            "seq": 1,
            "text": "hello world",
            "token_count": 2,
            "element_first": ids["element"],
            "element_last": ids["element"],
            "sha256": "6" * 64,
            "chunker_id": "two-pass",
            "chunker_version": "1",
            "created_ts": now(),
        },
    )

    ids["emb"] = "6" * 64
    insert(
        store,
        "emb",
        {
            "chunk_sha256": ids["emb"],
            "model_key": "qwen3-embedding-4b",
            "dims": 8,
            "vector": bytes(32),
            "created_ts": now(),
        },
    )

    ids["quote_anchor"] = new_id("ANC")
    insert(
        store,
        "quote_anchor",
        {
            "anchor_id": ids["quote_anchor"],
            "doc_id": ids["document"],
            "chunk_id": ids["chunk"],
            "page_number": 1,
            "char_start": 0,
            "char_end": 11,
            "doc_sha256": "5" * 64,
            "quote_sha256": "7" * 64,
            "quote_text": "hello world",
            "created_by_launch": ids["launch"],
            "created_ts": now(),
        },
    )

    ids["claim"] = new_id("CLM")
    insert(
        store,
        "claim",
        {
            "claim_id": ids["claim"],
            "text": "test claim",
            "kind": "finding",
            "anchor_id": ids["quote_anchor"],
            "created_at": now(),
            "created_by_launch": ids["launch"],
        },
    )

    ids["entity"] = new_id("ENT")
    insert(
        store,
        "entity",
        {
            "entity_id": ids["entity"],
            "name": "Test Entity",
            "entity_type": "concept",
            "resolution": "draft",
            "created_by_launch": ids["launch"],
            "created_at": now(),
        },
    )

    entity2 = new_id("ENT")
    insert(
        store,
        "entity",
        {
            "entity_id": entity2,
            "name": "Test Entity 2",
            "entity_type": "concept",
            "resolution": "draft",
            "created_by_launch": ids["launch"],
            "created_at": now(),
        },
    )

    ids["relation"] = new_id("REL")
    insert(
        store,
        "relation",
        {
            "rel_id": ids["relation"],
            "src_entity": ids["entity"],
            "dst_entity": entity2,
            "rel_type": "relates_to",
            "fact_text": "Test Entity relates_to Test Entity 2",
            "evidence_anchor": ids["quote_anchor"],
            "created_at": now(),
        },
    )

    ids["merge_proposal"] = new_id("MRG")
    insert(
        store,
        "merge_proposal",
        {
            "prop_id": ids["merge_proposal"],
            "canonical_entity": ids["entity"],
            "members": f'["{ids["entity"]}", "{entity2}"]',
            "reason": "test merge",
            "status": "draft",
            "proposed_by_launch": ids["launch"],
        },
    )

    ids["hypothesis"] = new_id("HYP")
    insert(
        store,
        "hypothesis",
        {
            "hyp_id": ids["hypothesis"],
            "text": "test hypothesis",
            "status": "open",
            "prereg_id": ids["prereg"],
            "created_ts": now(),
            "created_by_launch": ids["launch"],
        },
    )

    ids["verdict"] = new_id("VRD")
    insert(
        store,
        "verdict",
        {
            "verdict_id": ids["verdict"],
            "subject_kind": "hypothesis",
            "subject_id": ids["hypothesis"],
            "procedure": "citecheck",
            "procedure_version": "1",
            "label": "PASS",
            "evidence": "[]",
            "prereg_id": ids["prereg"],
            "ts": now(),
            "issued_by_launch": ids["launch"],
        },
    )

    ids["experiment"] = new_id("EXP")
    insert(
        store,
        "experiment",
        {
            "exp_id": ids["experiment"],
            "hyp_id": ids["hypothesis"],
            "prereg_id": ids["prereg"],
            "procedure_ref": "verify/citecheck.py",
            "params": "{}",
            "status": "planned",
            "created_ts": now(),
            "created_by_launch": ids["launch"],
        },
    )

    ids["idea"] = new_id("IDEA")
    insert(
        store,
        "idea",
        {
            "idea_id": ids["idea"],
            "round_id": "round-1",
            "author_launch": ids["launch"],
            "body": "test idea",
            "feed_post_ref": ids["feed_post"],
            "status": "raw",
            "created_ts": now(),
        },
    )

    # ops_v3 room_link: XID room_link.idea_id -> knowledge.idea, so this
    # must land AFTER idea exists (dependency order) even though room_link
    # otherwise belongs alongside room/room_turn/room_score in ops.db.
    ids["room_link"] = ids["room"]  # composite PK (room_id, dp_id); keep room_id for reference
    insert(
        store,
        "room_link",
        {
            "room_id": ids["room"],
            "dp_id": "dp-1",
            "idea_id": ids["idea"],
        },
    )

    ids["record"] = new_id("REC")
    insert(
        store,
        "record",
        {
            "record_id": ids["record"],
            "register_key": "test-register",
            "artifact_id": ids["artifact"],
            "seq": 1,
            "payload": "{}",
            "created_ts": now(),
        },
    )

    ids["prov_edge"] = new_id("EDGE")
    insert(
        store,
        "prov_edge",
        {
            "edge_id": ids["prov_edge"],
            "src_kind": "claim",
            "src_id": ids["claim"],
            "dst_kind": "artifact",
            "dst_id": ids["artifact"],
            "role": "supports",
            "launch_id": ids["launch"],
            "ts": now(),
        },
    )

    ids["summary"] = new_id("SUM")
    insert(
        store,
        "summary",
        {
            "summary_id": ids["summary"],
            "subject_kind": "document",
            "subject_id": ids["document"],
            "tier": "L1",
            "body": "test summary body",
            "word_count": 3,
            "word_cap": 150,
            "source_doc_ids": f'["{ids["document"]}"]',
            "subject_sha256": "5" * 64,
            "fenced": 0,
            "status": "current",
            "procedure_version": "1",
            "created_by_launch": ids["launch"],
            "created_ts": now(),
        },
    )

    # ---- jobs.db ----------------------------------------------------------
    ids["job"] = new_id("JOB")
    insert(
        store,
        "job",
        {
            "job_id": ids["job"],
            "kind": "embed",
            "payload": "{}",
            "state": "pending",
            "created_ts": now(),
        },
    )

    ids["job_event"] = str(
        insert(
            store,
            "job_event",
            {
                "job_id": ids["job"],
                "ts": now(),
                "type": "created",
            },
        )
    )

    return ids
