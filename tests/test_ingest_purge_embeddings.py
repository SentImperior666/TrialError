"""``trialerror ingest purge-embeddings`` -- taking a superseded embedding
model key's rows back out of the record.

The failure this covers: a program embedded its whole corpus under a
placeholder backend's key, was then pointed at a real model, and every
document was re-embedded under the real key. ``emb``'s primary key is
``(chunk_sha256, model_key)``, so the second embedding did not replace the
first -- both sets of rows sit in the table, the ``fake_backend_rows``
doctor check goes on failing with the placeholder count, and ``re-embed``
cannot help because by design it only ADDS rows for the configured key.

Every document here goes through the REAL pipeline (add-source -> add ->
drain every job) so the placeholder rows being purged are the rows the
embed stage actually writes, and the "re-embedded on the real model" state
is then built on top of them the way the offload path builds it: the same
chunk hashes, a second model key, that key's own vector table.
"""

from __future__ import annotations

import argparse
import json

import pytest

from trialerror.cli import ingest as cli_ingest
from trialerror.ingest import pipeline
from trialerror.ingest.backends import DEFAULT_FAKE_EMBED_DIMS
from trialerror.ingest.checks import check_embedding_missing, configured_embed_model_key
from trialerror.ingest.errors import ActiveEmbedKeyPurgeError, DocumentNotFoundError
from trialerror.ingest.purge import PURGE_EVENT_TYPE, purge_embeddings
from trialerror.jobs.worker import run_one
from trialerror.offload.checks import check_fake_backend_rows
from trialerror.stores.errors import XidTargetMissingError
from trialerror.stores.vecindex import ensure_vec_table, serialize_vector_fallback, vec_table_name
from trialerror.util.doctor import DoctorContext
from trialerror.util.timeutil import now
from tests._ingest_fixtures import bootstrap_launch, write_html_fixture, write_markdown_fixture

#: What the default (placeholder) embed backend stamps on its rows.
SUPERSEDED_KEY = f"fake-{DEFAULT_FAKE_EMBED_DIMS}"

#: The key this program is configured to write once it has a real model --
#: a neutral stand-in name, not any particular vendor's.
REAL_KEY = "mk-real-2048"
REAL_DIMS = 2048

REAL_CONFIG = {"ingest": {"embed": {"backend": "offload", "model_key": REAL_KEY, "dims": REAL_DIMS}}}

REAL_TOML = (
    '[program]\nid = "PROG-test"\n\n'
    "[ingest]\nrequire_real_backends = true\n\n"
    f'[ingest.embed]\nbackend = "offload"\nmodel_key = "{REAL_KEY}"\ndims = {REAL_DIMS}\n'
)


class _Args:
    def __init__(self, **kw):
        self.program_root = None
        self.platform_root = None
        self.doc_id = None
        self.dry_run = False
        for k, v in kw.items():
            setattr(self, k, v)


def _drain(store, max_steps=12):
    for i in range(max_steps):
        if run_one(store, worker_id=f"w{i}")["status"] == "idle":
            return


def _ingest(store, program_root, *, launch_id, name="doc.html", writer=write_html_fixture):
    raw_dir = program_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = writer(raw_dir / name)
    source_id = pipeline.register_source(
        store, kind="paper", title=f"Fixture {name}", license_tier="open",
        acquisition_route="web", registered_by_launch=launch_id,
    )["source_id"]
    result = pipeline.add_document(
        store, program_root=program_root, source_id=source_id, raw_path=path,
        created_by_launch=launch_id,
    )
    _drain(store)
    return result["document"]["doc_id"]


def _reembed_under_real_key(store, *, doc_id=None):
    """What the offload path leaves behind once the real model has answered:
    a SECOND ``emb`` row per chunk hash under the real key, and that key's
    own ``vec_chunks__*`` table -- the placeholder rows untouched."""
    conn = store.knowledge
    sql = "SELECT chunk_id, sha256 FROM chunk"
    params: tuple = ()
    if doc_id is not None:
        sql += " WHERE doc_id = ?"
        params = (doc_id,)
    chunks = [dict(r) for r in conn.execute(sql, params).fetchall()]
    blob = serialize_vector_fallback([0.125] * REAL_DIMS)
    ensure_vec_table(conn, REAL_KEY, REAL_DIMS)
    table = vec_table_name(REAL_KEY)
    with conn:
        for c in chunks:
            conn.execute(
                "INSERT OR REPLACE INTO emb(chunk_sha256, model_key, dims, vector, created_ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (c["sha256"], REAL_KEY, REAL_DIMS, blob, now()),
            )
            conn.execute(
                f"INSERT OR REPLACE INTO {table}(chunk_id, model_key, dims, vector) VALUES (?, ?, ?, ?)",
                (c["chunk_id"], REAL_KEY, REAL_DIMS, blob),
            )
    conn.commit()
    return len(chunks)


def _emb_count(store, model_key):
    return int(
        store.knowledge.execute(
            "SELECT COUNT(*) FROM emb WHERE model_key = ?", (model_key,)
        ).fetchone()[0]
    )


def _vec_count(store, model_key):
    table = vec_table_name(model_key)
    exists = store.knowledge.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (table,)
    ).fetchone()
    if exists is None:
        return None
    return int(store.knowledge.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


@pytest.fixture()
def two_docs(store, program_root):
    """Two documents, both embedded under the placeholder key and both
    re-embedded under the real key -- the live state this verb exists for."""
    launch_id = bootstrap_launch(store)
    first = _ingest(store, program_root, launch_id=launch_id, name="first.html")
    second = _ingest(
        store, program_root, launch_id=launch_id, name="second.md", writer=write_markdown_fixture
    )
    _reembed_under_real_key(store)
    return launch_id, first, second


# ---------------------------------------------------------------------------
# the core subtraction
# ---------------------------------------------------------------------------


def test_purge_removes_only_the_named_keys_rows(store, program_root, two_docs):
    launch_id, _first, _second = two_docs
    superseded_before = _emb_count(store, SUPERSEDED_KEY)
    real_before = _emb_count(store, REAL_KEY)
    assert superseded_before >= 2 and real_before >= 2

    result = purge_embeddings(
        store, model_key=SUPERSEDED_KEY, launch_id=launch_id, config=REAL_CONFIG
    )

    assert result["model_key"] == SUPERSEDED_KEY
    assert result["dry_run"] is False
    assert result["rows_deleted"] == superseded_before
    assert result["documents"] == 2
    assert _emb_count(store, SUPERSEDED_KEY) == 0
    assert _emb_count(store, REAL_KEY) == real_before, "the active key's rows are not this verb's"


def test_purge_reports_the_documented_envelope_shape(store, program_root, two_docs):
    launch_id, _first, _second = two_docs
    result = purge_embeddings(
        store, model_key=SUPERSEDED_KEY, launch_id=launch_id, config=REAL_CONFIG
    )
    assert set(result) == {
        "model_key",
        "documents",
        "rows_deleted",
        "index_entries_deleted",
        "chunks_now_without_any_embedding",
        "dry_run",
    }


def test_purge_empties_the_superseded_vector_table_and_leaves_the_active_one(
    store, program_root, two_docs
):
    launch_id, _first, _second = two_docs
    superseded_vec_before = _vec_count(store, SUPERSEDED_KEY)
    real_vec_before = _vec_count(store, REAL_KEY)
    assert superseded_vec_before, "the index stage should have filled the placeholder key's table"
    assert real_vec_before

    result = purge_embeddings(
        store, model_key=SUPERSEDED_KEY, launch_id=launch_id, config=REAL_CONFIG
    )

    assert result["index_entries_deleted"] == superseded_vec_before
    assert _vec_count(store, SUPERSEDED_KEY) == 0
    assert _vec_count(store, REAL_KEY) == real_vec_before
    # Emptied, not dropped: the table is schema, its rows were the data.
    assert _vec_count(store, SUPERSEDED_KEY) is not None


def test_purge_leaves_no_chunk_unembedded_when_the_real_key_is_complete(
    store, program_root, two_docs
):
    launch_id, _first, _second = two_docs
    result = purge_embeddings(
        store, model_key=SUPERSEDED_KEY, launch_id=launch_id, config=REAL_CONFIG
    )
    assert result["chunks_now_without_any_embedding"] == 0


def test_purge_counts_the_chunks_it_leaves_with_no_embedding_at_all(store, program_root):
    """The number that tells an operator a re-embed still has to run: a
    corpus purged BEFORE the real key was complete."""
    launch_id = bootstrap_launch(store)
    doc_id = _ingest(store, program_root, launch_id=launch_id)
    chunks = int(store.knowledge.execute("SELECT COUNT(*) FROM chunk").fetchone()[0])
    assert chunks >= 1

    result = purge_embeddings(
        store, model_key=SUPERSEDED_KEY, launch_id=launch_id, config=REAL_CONFIG
    )

    assert result["chunks_now_without_any_embedding"] == chunks
    assert result["documents"] == 1
    assert doc_id


def test_purge_also_removes_rows_whose_chunk_no_longer_exists(store, program_root, two_docs):
    """``emb`` is hash-addressed with no FK to ``chunk``, so a rechunk leaves
    rows behind that belong to no document. ``fake_backend_rows`` counts
    them, so a purge that skipped them could never clear the check."""
    launch_id, _first, _second = two_docs
    with store.knowledge:
        store.knowledge.execute(
            "INSERT INTO emb(chunk_sha256, model_key, dims, vector, created_ts) VALUES (?,?,?,?,?)",
            ("sha-of-a-chunk-that-was-rechunked-away", SUPERSEDED_KEY, DEFAULT_FAKE_EMBED_DIMS, b"\x00" * 4, now()),
        )
    store.knowledge.commit()
    before = _emb_count(store, SUPERSEDED_KEY)

    result = purge_embeddings(
        store, model_key=SUPERSEDED_KEY, launch_id=launch_id, config=REAL_CONFIG
    )

    assert result["rows_deleted"] == before
    assert _emb_count(store, SUPERSEDED_KEY) == 0


# ---------------------------------------------------------------------------
# --doc-id scoping
# ---------------------------------------------------------------------------


def test_purge_scoped_to_one_document_leaves_the_other_alone(store, program_root, two_docs):
    launch_id, first, second = two_docs

    def fake_rows_of(doc_id):
        return int(
            store.knowledge.execute(
                """
                SELECT COUNT(*) FROM emb e JOIN chunk c ON c.sha256 = e.chunk_sha256
                WHERE e.model_key = ? AND c.doc_id = ?
                """,
                (SUPERSEDED_KEY, doc_id),
            ).fetchone()[0]
        )

    first_before, second_before = fake_rows_of(first), fake_rows_of(second)
    assert first_before and second_before

    result = purge_embeddings(
        store, model_key=SUPERSEDED_KEY, launch_id=launch_id, doc_id=first, config=REAL_CONFIG
    )

    assert result["documents"] == 1
    assert result["rows_deleted"] == first_before
    assert fake_rows_of(first) == 0
    assert fake_rows_of(second) == second_before
    assert _emb_count(store, SUPERSEDED_KEY) == second_before


def test_purge_scoped_to_one_document_leaves_its_vector_entries_only(store, program_root, two_docs):
    launch_id, first, _second = two_docs
    table = vec_table_name(SUPERSEDED_KEY)
    first_chunk_ids = [
        r[0] for r in store.knowledge.execute("SELECT chunk_id FROM chunk WHERE doc_id = ?", (first,))
    ]
    total_before = _vec_count(store, SUPERSEDED_KEY)

    purge_embeddings(
        store, model_key=SUPERSEDED_KEY, launch_id=launch_id, doc_id=first, config=REAL_CONFIG
    )

    remaining = [r[0] for r in store.knowledge.execute(f"SELECT chunk_id FROM {table}")]
    assert total_before == len(remaining) + len(first_chunk_ids)
    assert not set(remaining) & set(first_chunk_ids)


def test_purge_refuses_an_unknown_document(store, program_root, two_docs):
    launch_id, _first, _second = two_docs
    before = _emb_count(store, SUPERSEDED_KEY)
    with pytest.raises(DocumentNotFoundError):
        purge_embeddings(
            store, model_key=SUPERSEDED_KEY, launch_id=launch_id, doc_id="DOC-nope",
            config=REAL_CONFIG,
        )
    assert _emb_count(store, SUPERSEDED_KEY) == before


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------


def test_purge_refuses_the_configured_real_embed_key(store, program_root, two_docs):
    """Purging the active key would delete the live search surface in one
    transaction -- the outcome the additive-embed design exists to prevent."""
    launch_id, _first, _second = two_docs
    assert configured_embed_model_key(REAL_CONFIG) == REAL_KEY
    before = _emb_count(store, REAL_KEY)

    with pytest.raises(ActiveEmbedKeyPurgeError) as exc:
        purge_embeddings(store, model_key=REAL_KEY, launch_id=launch_id, config=REAL_CONFIG)

    assert REAL_KEY in str(exc.value)
    assert _emb_count(store, REAL_KEY) == before, "a refused purge must remove nothing"
    assert _vec_count(store, REAL_KEY)


def test_purge_refuses_the_default_placeholder_key_when_that_is_what_is_configured(
    store, program_root
):
    """The refusal follows the CONFIG, not a hardcoded name: in a program
    that never moved off the placeholder backend, the placeholder key IS the
    active key and purging it is the same mistake."""
    launch_id = bootstrap_launch(store)
    _ingest(store, program_root, launch_id=launch_id)
    with pytest.raises(ActiveEmbedKeyPurgeError):
        purge_embeddings(store, model_key=SUPERSEDED_KEY, launch_id=launch_id, config={})


def test_purge_refuses_a_launch_that_names_no_platform_row(store, program_root, two_docs):
    """The L-E4 posture: a destructive act needs an existing
    ``platform.launch`` row, checked before anything is touched."""
    _launch_id, _first, _second = two_docs
    before = _emb_count(store, SUPERSEDED_KEY)

    with pytest.raises(XidTargetMissingError):
        purge_embeddings(
            store, model_key=SUPERSEDED_KEY, launch_id="LNCH-nope", config=REAL_CONFIG
        )

    assert _emb_count(store, SUPERSEDED_KEY) == before


def test_purge_refuses_a_launch_that_names_no_platform_row_even_on_a_dry_run(
    store, program_root, two_docs
):
    with pytest.raises(XidTargetMissingError):
        purge_embeddings(
            store, model_key=SUPERSEDED_KEY, launch_id="LNCH-nope", dry_run=True,
            config=REAL_CONFIG,
        )


def test_the_cli_parser_requires_a_model_key_and_a_launch_id():
    parser = argparse.ArgumentParser()
    cli_ingest.register(parser.add_subparsers(dest="group"))
    with pytest.raises(SystemExit):  # no --launch-id
        parser.parse_args(["ingest", "purge-embeddings", "--model-key", SUPERSEDED_KEY])
    with pytest.raises(SystemExit):  # no --model-key
        parser.parse_args(["ingest", "purge-embeddings", "--launch-id", "LNCH-1"])


def test_purge_embeddings_is_registered_as_an_ingest_subcommand():
    parser = argparse.ArgumentParser()
    cli_ingest.register(parser.add_subparsers(dest="group"))
    args = parser.parse_args(
        ["ingest", "purge-embeddings", "--model-key", SUPERSEDED_KEY, "--launch-id", "LNCH-1",
         "--doc-id", "DOC-1", "--dry-run"]
    )
    assert args.handler is cli_ingest._cmd_purge_embeddings
    assert (args.model_key, args.doc_id, args.dry_run) == (SUPERSEDED_KEY, "DOC-1", True)


# ---------------------------------------------------------------------------
# --dry-run and idempotence
# ---------------------------------------------------------------------------


def test_dry_run_reports_the_counts_and_touches_nothing(store, program_root, two_docs):
    launch_id, _first, _second = two_docs
    emb_before = _emb_count(store, SUPERSEDED_KEY)
    vec_before = _vec_count(store, SUPERSEDED_KEY)
    real_before = _emb_count(store, REAL_KEY)

    preview = purge_embeddings(
        store, model_key=SUPERSEDED_KEY, launch_id=launch_id, dry_run=True, config=REAL_CONFIG
    )

    assert preview["dry_run"] is True
    assert preview["rows_deleted"] == emb_before
    assert preview["index_entries_deleted"] == vec_before
    assert preview["documents"] == 2
    assert _emb_count(store, SUPERSEDED_KEY) == emb_before
    assert _vec_count(store, SUPERSEDED_KEY) == vec_before
    assert _emb_count(store, REAL_KEY) == real_before
    assert store.ops.execute(
        "SELECT COUNT(*) FROM event WHERE type = ?", (PURGE_EVENT_TYPE,)
    ).fetchone()[0] == 0

    real = purge_embeddings(
        store, model_key=SUPERSEDED_KEY, launch_id=launch_id, config=REAL_CONFIG
    )
    for key in ("documents", "rows_deleted", "index_entries_deleted", "chunks_now_without_any_embedding"):
        assert real[key] == preview[key], key


def test_dry_run_predicts_the_chunks_left_without_any_embedding(store, program_root):
    launch_id = bootstrap_launch(store)
    _ingest(store, program_root, launch_id=launch_id)
    chunks = int(store.knowledge.execute("SELECT COUNT(*) FROM chunk").fetchone()[0])

    preview = purge_embeddings(
        store, model_key=SUPERSEDED_KEY, launch_id=launch_id, dry_run=True, config=REAL_CONFIG
    )
    assert preview["chunks_now_without_any_embedding"] == chunks

    real = purge_embeddings(
        store, model_key=SUPERSEDED_KEY, launch_id=launch_id, config=REAL_CONFIG
    )
    assert real["chunks_now_without_any_embedding"] == chunks


def test_purge_is_idempotent(store, program_root, two_docs):
    launch_id, _first, _second = two_docs
    first = purge_embeddings(
        store, model_key=SUPERSEDED_KEY, launch_id=launch_id, config=REAL_CONFIG
    )
    assert first["rows_deleted"]

    second = purge_embeddings(
        store, model_key=SUPERSEDED_KEY, launch_id=launch_id, config=REAL_CONFIG
    )

    assert second["rows_deleted"] == 0
    assert second["index_entries_deleted"] == 0
    assert second["documents"] == 0
    # One change, one event: a no-op purge is not a change to the record.
    assert store.ops.execute(
        "SELECT COUNT(*) FROM event WHERE type = ?", (PURGE_EVENT_TYPE,)
    ).fetchone()[0] == 1


# ---------------------------------------------------------------------------
# the event
# ---------------------------------------------------------------------------


def test_purge_writes_one_embeddings_purged_event_with_the_counts_and_the_launch(
    store, program_root, two_docs
):
    launch_id, _first, _second = two_docs
    result = purge_embeddings(
        store, model_key=SUPERSEDED_KEY, launch_id=launch_id, config=REAL_CONFIG
    )

    rows = store.ops.execute(
        "SELECT * FROM event WHERE type = ? ORDER BY ts DESC", (PURGE_EVENT_TYPE,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["launch_id"] == launch_id
    payload = json.loads(rows[0]["payload"])
    assert payload["model_key"] == SUPERSEDED_KEY
    assert payload["doc_id"] is None
    assert payload["rows_deleted"] == result["rows_deleted"]
    assert payload["index_entries_deleted"] == result["index_entries_deleted"]
    assert payload["documents"] == result["documents"]
    assert payload["chunks_now_without_any_embedding"] == 0
    assert payload["vec_table"] == vec_table_name(SUPERSEDED_KEY)


def test_a_scoped_purges_event_names_the_document(store, program_root, two_docs):
    launch_id, first, _second = two_docs
    purge_embeddings(
        store, model_key=SUPERSEDED_KEY, launch_id=launch_id, doc_id=first, config=REAL_CONFIG
    )
    row = store.ops.execute(
        "SELECT * FROM event WHERE type = ? ORDER BY ts DESC LIMIT 1", (PURGE_EVENT_TYPE,)
    ).fetchone()
    assert json.loads(row["payload"])["doc_id"] == first


# ---------------------------------------------------------------------------
# the doctor checks this verb exists to clear
# ---------------------------------------------------------------------------


def test_fake_backend_rows_fails_before_the_purge_and_passes_after(store, program_root, two_docs):
    """Item 2's whole point: ``fake_backend_rows`` is UNCHANGED and has to
    clear on its own once the placeholder rows are gone."""
    launch_id, _first, _second = two_docs
    (program_root / "trialerror.toml").write_text(REAL_TOML, encoding="utf-8")
    ctx = DoctorContext(program_root=program_root)
    store.knowledge.commit()

    before = check_fake_backend_rows(ctx)
    assert before.status == "fail"
    assert before.details["fake_emb_model_keys"] == {SUPERSEDED_KEY: _emb_count(store, SUPERSEDED_KEY)}
    assert before.details["fake_ocr_documents"] == 0, "these documents never went through OCR"

    purge_embeddings(store, model_key=SUPERSEDED_KEY, launch_id=launch_id, config=REAL_CONFIG)
    store.knowledge.commit()

    after = check_fake_backend_rows(ctx)
    assert after.status == "pass"
    assert after.message == "no fake-backend rows in the record"


def test_embedding_missing_keeps_its_semantics_across_the_purge(store, program_root, two_docs):
    """``embedding_missing`` counts chunks with no ``emb`` row under the
    CONFIGURED key. The purge removes a different key's rows, so the count
    must not move -- and must not be confused by the rows that went."""
    launch_id, _first, _second = two_docs
    (program_root / "trialerror.toml").write_text(REAL_TOML, encoding="utf-8")
    ctx = DoctorContext(program_root=program_root)
    store.knowledge.commit()

    before = check_embedding_missing(ctx)
    assert before.details["model_key"] == REAL_KEY
    assert before.details["count"] == 0

    purge_embeddings(store, model_key=SUPERSEDED_KEY, launch_id=launch_id, config=REAL_CONFIG)
    store.knowledge.commit()

    after = check_embedding_missing(ctx)
    assert after.details["model_key"] == REAL_KEY
    assert after.details["count"] == 0
    assert after.status == "pass"


# ---------------------------------------------------------------------------
# the CLI surface
# ---------------------------------------------------------------------------


def test_cmd_purge_embeddings_end_to_end(store, program_root, platform_root, two_docs):
    launch_id, _first, _second = two_docs
    (program_root / "trialerror.toml").write_text(REAL_TOML, encoding="utf-8")
    superseded_before = _emb_count(store, SUPERSEDED_KEY)
    real_before = _emb_count(store, REAL_KEY)
    store.knowledge.commit()
    store.close()

    env = cli_ingest._cmd_purge_embeddings(
        _Args(
            program_root=str(program_root), platform_root=str(platform_root),
            model_key=SUPERSEDED_KEY, launch_id=launch_id,
        )
    )

    assert env["ok"] is True, env
    assert env["result"] == {
        "model_key": SUPERSEDED_KEY,
        "documents": 2,
        "rows_deleted": superseded_before,
        "index_entries_deleted": env["result"]["index_entries_deleted"],
        "chunks_now_without_any_embedding": 0,
        "dry_run": False,
    }
    assert env["result"]["index_entries_deleted"] >= 2

    from trialerror.stores.store import open_store

    reopened = open_store(program_root, platform_root=platform_root)
    try:
        assert _emb_count(reopened, SUPERSEDED_KEY) == 0
        assert _emb_count(reopened, REAL_KEY) == real_before
    finally:
        reopened.close()


def test_cmd_purge_embeddings_dry_run_envelope_touches_nothing(
    store, program_root, platform_root, two_docs
):
    launch_id, _first, _second = two_docs
    (program_root / "trialerror.toml").write_text(REAL_TOML, encoding="utf-8")
    before = _emb_count(store, SUPERSEDED_KEY)
    store.knowledge.commit()
    store.close()

    env = cli_ingest._cmd_purge_embeddings(
        _Args(
            program_root=str(program_root), platform_root=str(platform_root),
            model_key=SUPERSEDED_KEY, launch_id=launch_id, dry_run=True,
        )
    )

    assert env["ok"] is True
    assert env["result"]["dry_run"] is True
    assert env["result"]["rows_deleted"] == before
    argv = env["nextActions"][0]["argv"]
    assert "purge-embeddings" in argv and "--dry-run" not in argv, argv

    from trialerror.stores.store import open_store

    reopened = open_store(program_root, platform_root=platform_root)
    try:
        assert _emb_count(reopened, SUPERSEDED_KEY) == before
    finally:
        reopened.close()


def test_cmd_purge_embeddings_refuses_the_active_key_as_an_envelope_not_a_traceback(
    store, program_root, platform_root, two_docs
):
    launch_id, _first, _second = two_docs
    (program_root / "trialerror.toml").write_text(REAL_TOML, encoding="utf-8")
    store.knowledge.commit()
    store.close()

    env = cli_ingest._cmd_purge_embeddings(
        _Args(
            program_root=str(program_root), platform_root=str(platform_root),
            model_key=REAL_KEY, launch_id=launch_id,
        )
    )

    assert env["ok"] is False
    assert env["error"]["code"] == "purge_refused_active_model_key"
    assert REAL_KEY in env["error"]["message"]


def test_cmd_purge_embeddings_refuses_an_unregistered_launch_as_an_envelope(
    store, program_root, platform_root, two_docs
):
    (program_root / "trialerror.toml").write_text(REAL_TOML, encoding="utf-8")
    store.knowledge.commit()
    store.close()

    env = cli_ingest._cmd_purge_embeddings(
        _Args(
            program_root=str(program_root), platform_root=str(platform_root),
            model_key=SUPERSEDED_KEY, launch_id="LNCH-nope",
        )
    )

    assert env["ok"] is False
    assert env["error"]["code"] == "XidTargetMissingError"


def test_cmd_purge_embeddings_reports_an_unknown_document_as_an_envelope(
    store, program_root, platform_root, two_docs
):
    launch_id, _first, _second = two_docs
    (program_root / "trialerror.toml").write_text(REAL_TOML, encoding="utf-8")
    store.knowledge.commit()
    store.close()

    env = cli_ingest._cmd_purge_embeddings(
        _Args(
            program_root=str(program_root), platform_root=str(platform_root),
            model_key=SUPERSEDED_KEY, launch_id=launch_id, doc_id="DOC-nope",
        )
    )

    assert env["ok"] is False
    assert env["error"]["code"] == "DocumentNotFoundError"
