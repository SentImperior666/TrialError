"""The scheduled convergent-discovery re-check: the ``convergent_recheck``
job handler, the ``trialerror.lens.novelty`` seam it writes through, and the
``trialerror lens recheck`` verb that enqueues it.

The property under test throughout is the one the design states: a
convergence found after a round closed is LOGGED, never applied to the
original label. Every test here that writes a link also asserts what did not
move.

Why an idea statement is sometimes a corpus chunk's text verbatim: the
zero-setup embed backend is hash-derived, so identical text is the one case
where the cosine is known (exactly 1.0). Same reason ``tests/_novelty_fixtures.py``
gives for its own plants -- this is not pretending two paraphrases embed near
each other.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest

from trialerror.cli import main
from trialerror.jobs import ledger
from trialerror.jobs.worker import run_one
from trialerror.lens.ideas import read_idea, write_idea
from trialerror.lens.novelty import (
    CONVERGENT_RECHECK_VERSION,
    NoveltyError,
    StaticExternalProvider,
    known_neighbour_keys,
    read_dossier,
    recheck_idea_convergence,
    record_convergent_links,
    round_dir,
    run_mechanical_screen,
)
from trialerror.stores import insert, update
from trialerror.stores.store import open_store
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests._inventory_fixtures import bootstrap_launch, build_corpus_with_inventory

ROUND_ID = "round-recheck"


def _corpus_chunk_texts(store, *, limit: int = 2) -> list[str]:
    """Real chunk texts from the fixture corpus (never the inventory source,
    which the engine excludes from R4 by default anyway)."""
    rows = store.knowledge.execute(
        "SELECT c.text FROM chunk c JOIN document d ON c.doc_id = d.doc_id "
        "JOIN source s ON d.source_id = s.source_id WHERE s.kind = 'paper' ORDER BY c.chunk_id LIMIT ?",
        (limit,),
    ).fetchall()
    return [r["text"] for r in rows]


def _idea(store, *, statement: str, status: str = "consolidated", home: str = "family-a/row-1", docs=()) -> str:
    row = write_idea(
        store,
        round_id=ROUND_ID,
        author_launch=bootstrap_launch(store, attrs={"lens_name": "lens-1"}, purpose="ideation"),
        body=statement,
        home=home,
        tier="near",
        provenance={"docs": list(docs), "set_id": "lens-1"},
        operation_declared="bridge/synthesis-unify",
        recipe_card="TRANSFER",
    )
    if status != "raw":
        update(store, "idea", pk_column="idea_id", pk_value=row["idea_id"], changes={"status": status})
    return row["idea_id"]


def _dossier_on_file(store, idea_id: str, *, hits: dict) -> None:
    path = round_dir(store.program_root, ROUND_ID) / "novelty" / f"{idea_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "idea_id": idea_id, "round_id": ROUND_ID, "label_inventory": "no-close-neighbour",
                "label_corpus": None, "judged": False, "candidate_hits": hits,
            }
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# the seam: known keys, links, and what never moves
# ---------------------------------------------------------------------------


def test_known_neighbour_keys_reads_both_reference_sets():
    dossier = {
        "candidate_hits": {
            "R4": [{"chunk_id": "CHK-1", "similarity": 0.7}],
            "R5": [{"provider": "static", "id": "paper-9"}],
        }
    }
    assert known_neighbour_keys(dossier) == {"R4:CHK-1", "R5:static:paper-9"}
    assert known_neighbour_keys(None) == set()
    assert known_neighbour_keys({}) == set()


def test_record_convergent_links_writes_one_column_and_nothing_else(store):
    build_corpus_with_inventory(store)
    idea_id = _idea(store, statement="a resource flow with two clocks")
    before = read_idea(store, idea_id=idea_id)
    result = record_convergent_links(
        store, idea_id=idea_id,
        links=[{"key": "R5:static:paper-9", "reference_set": "R5", "similarity": 0.81}],
        round_id=ROUND_ID,
    )
    after = read_idea(store, idea_id=idea_id)
    assert result["n_links"] == 1
    assert after["convergent_with"][0]["key"] == "R5:static:paper-9"
    assert after["convergent_with"][0]["procedure_version"] == CONVERGENT_RECHECK_VERSION
    for field in ("status", "tier", "recipe_card", "operation_declared", "body", "home", "requirements"):
        assert after.get(field) == before.get(field), field
    assert store.knowledge.execute("SELECT COUNT(*) AS n FROM verdict").fetchone()["n"] == 0


def test_a_link_carries_only_the_whitelisted_fields(store):
    """A link is an id, a reference set and a number. A caller handing a
    status or a label through used to have it copied onto
    ``idea.convergent_with`` -- judge-shaped keys on the one record this pass
    may never re-score."""
    build_corpus_with_inventory(store)
    idea_id = _idea(store, statement="a resource flow with two clocks")
    record_convergent_links(
        store, idea_id=idea_id,
        links=[{
            "key": "R4:CHK-1", "reference_set": "R4", "chunk_id": "CHK-1", "source_id": "SRC-1",
            "similarity": 0.77, "provider": None,
            # none of these belong on a link
            "status": "eliminated", "label_inventory": "same", "judged": True, "text": "the chunk prose",
        }],
    )
    link = read_idea(store, idea_id=idea_id)["convergent_with"][0]
    assert set(link) == {
        "key", "reference_set", "found_ts", "procedure_version",
        "chunk_id", "source_id", "similarity", "provider",
    }
    assert read_idea(store, idea_id=idea_id)["status"] == "consolidated"


def test_a_second_pass_adds_nothing_and_keeps_the_first_sighting(store):
    build_corpus_with_inventory(store)
    idea_id = _idea(store, statement="s")
    record_convergent_links(store, idea_id=idea_id, links=[{"key": "R4:CHK-1", "reference_set": "R4"}])
    first = read_idea(store, idea_id=idea_id)["convergent_with"][0]["found_ts"]
    result = record_convergent_links(store, idea_id=idea_id, links=[{"key": "R4:CHK-1", "reference_set": "R4"}])
    assert result["added"] == []
    assert read_idea(store, idea_id=idea_id)["convergent_with"][0]["found_ts"] == first


def test_linking_emits_one_audit_event(store):
    build_corpus_with_inventory(store)
    idea_id = _idea(store, statement="s")
    record_convergent_links(
        store, idea_id=idea_id, links=[{"key": "R4:CHK-1", "reference_set": "R4"}], round_id=ROUND_ID
    )
    rows = store.ops.execute("SELECT payload FROM event WHERE type = 'idea_convergent_linked'").fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload["idea_id"] == idea_id and payload["n_new"] == 1 and payload["round_id"] == ROUND_ID


# ---------------------------------------------------------------------------
# the re-check itself
# ---------------------------------------------------------------------------


def test_a_neighbour_the_screen_already_saw_is_not_a_discovery(store):
    build_corpus_with_inventory(store)
    text = _corpus_chunk_texts(store)[0]
    idea_id = _idea(store, statement=text)

    first = recheck_idea_convergence(store, idea_id=idea_id, allow_unscreened=True)
    assert first["n_found"] >= 1, "a statement that IS a chunk should retrieve that chunk"
    assert first["new"], "with no dossier on file, every hit is new -- the unscreened reading"

    _dossier_on_file(store, idea_id, hits={"R4": first["new"], "R5": []})
    second = recheck_idea_convergence(store, idea_id=idea_id)
    assert second["had_dossier"] is True
    assert second["new"] == []
    assert second["n_known"] >= 1


def test_a_document_ingested_later_is_found_as_a_convergence(store):
    """The pass runs at a threshold of -1.0 here, so every retrieved
    neighbour counts as a hit.

    That is not a shortcut around the threshold, it is the only honest way to
    stage this case on a hash-derived embed backend: the later document has to
    carry DIFFERENT text (the embedding cache is keyed on the text's own hash,
    so re-ingesting identical text is refused at the `emb` table), and
    different text under a hash backend has an unrelated — unpredictable —
    cosine. Holding the threshold out of the way makes the test about what it
    says it is about: a neighbour that did not exist when the dossier was
    written becomes a convergent link when it does.
    """
    corpus = build_corpus_with_inventory(store)
    text = _corpus_chunk_texts(store)[0]
    idea_id = _idea(store, statement=text)
    first = recheck_idea_convergence(
        store, idea_id=idea_id, write=False, candidate_hit_similarity=-1.0, allow_unscreened=True
    )
    _dossier_on_file(store, idea_id, hits={"R4": first["new"], "R5": []})
    assert read_idea(store, idea_id=idea_id)["convergent_with"] in (None, [])

    from tests._inventory_fixtures import add_document
    from trialerror.retrieve import engine as retrieve_engine
    from trialerror.stores.vecindex import ensure_vec_table

    model_key, _backend = retrieve_engine._resolve_embed_backend(store)
    vec_backend = ensure_vec_table(store.knowledge, model_key, corpus["dims"])
    add_document(
        store, source_id=corpus["corpus_source_id"], rel_path="later/arrival.md",
        paragraphs=["A later study derives the same coordination track from a queueing argument."],
        launch_id=corpus["launch_id"], model_key=model_key, embed_backend=corpus["embed_backend"],
        backend=vec_backend,
    )

    later = recheck_idea_convergence(store, idea_id=idea_id, candidate_hit_similarity=-1.0)
    assert later["new"], "the later arrival is a neighbour the dossier never saw"
    assert {link["reference_set"] for link in later["new"]} == {"R4"}
    linked = read_idea(store, idea_id=idea_id)
    assert len(linked["convergent_with"]) == len(later["new"])
    assert linked["status"] == "consolidated", "a convergence is logged, never applied"


def test_an_external_hit_is_linked_under_its_providers_own_key(store):
    build_corpus_with_inventory(store)
    idea_id = _idea(store, statement="a state model for upkeep")
    provider = StaticExternalProvider(default=[{"id": "paper-9", "title": "Two clocks", "provider": "static"}])
    result = recheck_idea_convergence(
        store, idea_id=idea_id, external=provider, external_query_mode="neutral_abstract",
        allow_unscreened=True,
    )
    assert "R5:static:paper-9" in [link["key"] for link in result["new"]]
    assert provider.calls and provider.calls[0].mode == "neutral_abstract"
    assert store.ops.execute(
        "SELECT COUNT(*) AS n FROM event WHERE type = 'novelty_external_query'"
    ).fetchone()["n"] == 1


def test_a_query_mode_with_no_provider_is_refused(store):
    build_corpus_with_inventory(store)
    idea_id = _idea(store, statement="s")
    with pytest.raises(NoveltyError, match="no provider to issue"):
        recheck_idea_convergence(store, idea_id=idea_id, external_query_mode="neutral_abstract")


def test_write_false_reports_without_writing(store):
    build_corpus_with_inventory(store)
    idea_id = _idea(store, statement=_corpus_chunk_texts(store)[0])
    result = recheck_idea_convergence(store, idea_id=idea_id, write=False, allow_unscreened=True)
    assert result["new"]
    assert result["written"] is False
    assert read_idea(store, idea_id=idea_id)["convergent_with"] in (None, [])


def test_the_screens_own_dossier_is_what_the_recheck_reads(store):
    """Not a fixture of a dossier -- the real one, written by the screen, so
    the key shapes the two halves use cannot drift apart."""
    build_corpus_with_inventory(store)
    text = _corpus_chunk_texts(store)[0]
    idea_id = _idea(store, statement=text, status="raw")
    run_mechanical_screen(store, round_id=ROUND_ID)
    update(store, "idea", pk_column="idea_id", pk_value=idea_id, changes={"status": "consolidated"})

    dossier = read_dossier(store, round_id=ROUND_ID, idea_id=idea_id)
    assert dossier is not None
    result = recheck_idea_convergence(store, idea_id=idea_id)
    assert result["had_dossier"] is True
    assert result["n_known"] == len(known_neighbour_keys(dossier)) >= 1
    assert result["new"] == [], "the screen just measured these same neighbours"


def test_an_unknown_idea_is_a_typed_refusal(store):
    build_corpus_with_inventory(store)
    with pytest.raises(NoveltyError, match="no idea"):
        recheck_idea_convergence(store, idea_id="IDEA-nope")


def test_a_record_with_no_dossier_is_refused_by_default(store):
    """New is measured against what the screen recorded, so with nothing
    recorded every neighbour is new -- which is the screen run late under
    another name, not a round of convergent discoveries."""
    build_corpus_with_inventory(store)
    idea_id = _idea(store, statement=_corpus_chunk_texts(store)[0])
    with pytest.raises(NoveltyError, match="no novelty dossier on file"):
        recheck_idea_convergence(store, idea_id=idea_id)
    assert read_idea(store, idea_id=idea_id)["convergent_with"] in (None, [])


def test_a_caller_supplied_dossier_satisfies_the_precondition(store):
    build_corpus_with_inventory(store)
    idea_id = _idea(store, statement=_corpus_chunk_texts(store)[0])
    result = recheck_idea_convergence(store, idea_id=idea_id, dossier={"candidate_hits": {"R4": [], "R5": []}})
    assert result["had_dossier"] is True


# ---------------------------------------------------------------------------
# the handler on the ledger
# ---------------------------------------------------------------------------


def _enqueue(store, **payload) -> str:
    job = ledger.enqueue(store, kind="custom", payload={"handler": "convergent_recheck", **payload})
    return job["job_id"]


def _checkpoint(store, job_id: str) -> dict:
    raw = ledger.get_job(store, job_id)["checkpoint"]
    return json.loads(raw) if raw else {}


def test_the_handler_runs_a_whole_round_and_checkpoints_what_it_did(store):
    build_corpus_with_inventory(store)
    texts = _corpus_chunk_texts(store, limit=2)
    ideas = [_idea(store, statement=t, home=f"family-a/row-{i}") for i, t in enumerate(texts)]
    for idea_id in ideas:
        _dossier_on_file(store, idea_id, hits={"R4": [], "R5": []})
    job_id = _enqueue(store, round_id=ROUND_ID)
    result = run_one(store, job_id=job_id)
    assert result["status"] == "complete", result
    checkpoint = _checkpoint(store, job_id)
    assert checkpoint["complete"] is True
    assert sorted(checkpoint["done"]) == sorted(ideas)
    assert checkpoint["n_ideas"] == 2
    assert checkpoint["n_linked"] >= 2
    for idea_id in ideas:
        assert read_idea(store, idea_id=idea_id)["status"] == "consolidated"


def test_the_handler_never_rescores(store):
    build_corpus_with_inventory(store)
    idea_id = _idea(store, statement=_corpus_chunk_texts(store)[0])
    _dossier_on_file(store, idea_id, hits={"R4": [], "R5": []})
    job_id = _enqueue(store, round_id=ROUND_ID)
    assert run_one(store, job_id=job_id)["status"] == "complete"
    row = read_idea(store, idea_id=idea_id)
    assert row["status"] == "consolidated"
    assert row["convergent_with"]
    assert store.knowledge.execute("SELECT COUNT(*) AS n FROM verdict").fetchone()["n"] == 0


def test_an_already_done_idea_is_not_rechecked_on_resume(store):
    """A pending job carrying a checkpoint from an interrupted attempt: the
    ids already done are not re-retrieved, which is what keeps a paused
    external pass from re-issuing queries it has already made."""
    build_corpus_with_inventory(store)
    texts = _corpus_chunk_texts(store, limit=2)
    ideas = [_idea(store, statement=t, home=f"family-a/row-{i}") for i, t in enumerate(texts)]
    for idea_id in ideas:
        _dossier_on_file(store, idea_id, hits={"R4": [], "R5": []})
    job_id = new_id("JOB")
    insert(
        store, "job",
        {
            "job_id": job_id, "kind": "custom",
            "payload": json.dumps({"handler": "convergent_recheck", "round_id": ROUND_ID}),
            "state": "pending", "attempts": 0, "max_attempts": 3,
            "checkpoint": json.dumps({"round_id": ROUND_ID, "done": [ideas[0]], "linked": {}}),
            "created_ts": now(),
        },
    )
    assert run_one(store, job_id=job_id)["status"] == "complete"
    checkpoint = _checkpoint(store, job_id)
    assert sorted(checkpoint["done"]) == sorted(ideas)
    assert read_idea(store, idea_id=ideas[0])["convergent_with"] in (None, [])
    assert read_idea(store, idea_id=ideas[1])["convergent_with"]


def test_raw_records_are_not_rechecked_by_default(store):
    build_corpus_with_inventory(store)
    texts = _corpus_chunk_texts(store, limit=2)
    raw = _idea(store, statement=texts[0], status="raw", home="family-a/row-0")
    consolidated = _idea(store, statement=texts[1], home="family-a/row-1")
    _dossier_on_file(store, consolidated, hits={"R4": [], "R5": []})
    job_id = _enqueue(store, round_id=ROUND_ID)
    run_one(store, job_id=job_id)
    checkpoint = _checkpoint(store, job_id)
    assert checkpoint["done"] == [consolidated]
    assert raw not in checkpoint["done"]


def test_eliminated_records_are_rechecked(store):
    """An eliminated idea whose twin shows up in later work is exactly the
    finding this pass exists to log, and those rows stay in the reference sets
    forever."""
    build_corpus_with_inventory(store)
    idea_id = _idea(store, statement=_corpus_chunk_texts(store)[0], status="eliminated")
    _dossier_on_file(store, idea_id, hits={"R4": [], "R5": []})
    job_id = _enqueue(store, round_id=ROUND_ID)
    run_one(store, job_id=job_id)
    assert read_idea(store, idea_id=idea_id)["convergent_with"]
    assert read_idea(store, idea_id=idea_id)["status"] == "eliminated"


def test_an_explicit_idea_list_still_honours_the_status_filter(store):
    """Naming a record does not screen it. The status filter used to apply
    only to the default path, so ``--idea-id <raw record>`` re-checked a row
    with no dossier and reported every neighbour as a new discovery."""
    build_corpus_with_inventory(store)
    texts = _corpus_chunk_texts(store, limit=2)
    named_raw = _idea(store, statement=texts[0], status="raw", home="family-a/row-0")
    named_screened = _idea(store, statement=texts[1], home="family-a/row-1")
    _dossier_on_file(store, named_screened, hits={"R4": [], "R5": []})
    job_id = _enqueue(store, round_id=ROUND_ID, idea_ids=[named_raw, named_screened])
    run_one(store, job_id=job_id)
    assert _checkpoint(store, job_id)["done"] == [named_screened]
    assert read_idea(store, idea_id=named_raw)["convergent_with"] in (None, [])


def test_allow_unscreened_lifts_both_the_filter_and_the_precondition(store):
    """The deliberate reading, taken in the payload where it is on the record."""
    build_corpus_with_inventory(store)
    named_raw = _idea(store, statement=_corpus_chunk_texts(store)[0], status="raw")
    job_id = _enqueue(store, round_id=ROUND_ID, idea_ids=[named_raw], allow_unscreened=True)
    assert run_one(store, job_id=job_id)["status"] == "complete"
    assert _checkpoint(store, job_id)["done"] == [named_raw]
    assert read_idea(store, idea_id=named_raw)["convergent_with"]
    assert read_idea(store, idea_id=named_raw)["status"] == "raw", "a convergence is logged, never applied"


def test_a_consolidated_record_whose_dossier_is_missing_is_skipped_with_its_reason(store):
    """A status says the screen ran; a missing dossier says it cannot be read.
    The refusal is carried in the checkpoint and the rest of the round still
    runs -- it is not a failed job, and it is not a round of invented
    discoveries either."""
    build_corpus_with_inventory(store)
    texts = _corpus_chunk_texts(store, limit=2)
    lost = _idea(store, statement=texts[0], home="family-a/row-0")
    kept = _idea(store, statement=texts[1], home="family-a/row-1")
    _dossier_on_file(store, kept, hits={"R4": [], "R5": []})
    job_id = _enqueue(store, round_id=ROUND_ID)
    assert run_one(store, job_id=job_id)["status"] == "complete"
    checkpoint = _checkpoint(store, job_id)
    assert sorted(checkpoint["done"]) == sorted([lost, kept])
    refused = checkpoint["linked"]["_refused"]
    assert len(refused) == 1 and refused[0].startswith(lost)
    assert "no novelty dossier on file" in refused[0]
    assert read_idea(store, idea_id=lost)["convergent_with"] in (None, [])
    assert read_idea(store, idea_id=kept)["convergent_with"]


def test_a_payload_with_no_round_id_fails_the_job(store):
    job_id = ledger.enqueue(store, kind="custom", payload={"handler": "convergent_recheck"})["job_id"]
    result = run_one(store, job_id=job_id)
    assert result["status"] in ("failed", "abandoned")
    assert "round_id" in ledger.get_job(store, job_id)["last_error"]


def test_a_mode_without_a_provider_fails_the_job(store):
    job_id = _enqueue(store, round_id=ROUND_ID, external_query_mode="statement")
    result = run_one(store, job_id=job_id)
    assert result["status"] in ("failed", "abandoned")
    assert "nothing to issue it to" in ledger.get_job(store, job_id)["last_error"]


def test_a_provider_without_a_mode_fails_the_job(store):
    job_id = _enqueue(store, round_id=ROUND_ID, external_provider="litapi")
    result = run_one(store, job_id=job_id)
    assert result["status"] in ("failed", "abandoned")
    assert "silently idle" in ledger.get_job(store, job_id)["last_error"]


def test_the_handler_reaches_r5_through_the_shared_builder(store, monkeypatch):
    """The provider builder is the screen's own
    (``cli.lens._build_external_provider``), which is also the seam a test
    injects a static provider through -- so no network and no index on disk
    are needed to drive the external path."""
    import trialerror.cli.lens as cli_lens

    build_corpus_with_inventory(store)
    idea_id = _idea(store, statement="a state model for upkeep")
    _dossier_on_file(store, idea_id, hits={"R4": [], "R5": []})
    closed: list[bool] = []
    provider = StaticExternalProvider(default=[{"id": "paper-42", "provider": "static"}])
    monkeypatch.setattr(
        cli_lens, "_build_external_provider", lambda kind, root: (provider, lambda: closed.append(True))
    )
    job_id = _enqueue(
        store, round_id=ROUND_ID, external_query_mode="neutral_abstract", external_provider="arxiv-index"
    )
    assert run_one(store, job_id=job_id)["status"] == "complete"
    keys = [link["key"] for link in read_idea(store, idea_id=idea_id)["convergent_with"]]
    assert "R5:static:paper-42" in keys
    assert closed == [True], "whatever the builder opened is closed again"


# ---------------------------------------------------------------------------
# the CLI that enqueues it
# ---------------------------------------------------------------------------


def _run_cli(argv: list[str]) -> tuple[int, dict]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(argv)
    return rc, json.loads(buf.getvalue().strip())


def test_the_cli_enqueues_a_custom_job_naming_the_handler(program_root, platform_root):
    open_store(program_root, platform_root=platform_root).close()
    rc, env = _run_cli(
        ["lens", "--program-root", str(program_root), "recheck", "--round-id", ROUND_ID, "--status", "promoted"]
    )
    assert rc == 0, env
    payload = env["result"]["payload"]
    assert payload["handler"] == "convergent_recheck"
    assert payload["round_id"] == ROUND_ID and payload["statuses"] == ["promoted"]
    assert env["result"]["job"]["kind"] == "custom"
    assert env["result"]["job"]["state"] == "pending"
    assert env["nextActions"][0]["argv"][:3] == ["trialerror", "jobs", "start-worker"]


def test_the_cli_refuses_a_mode_with_no_provider(program_root, platform_root):
    open_store(program_root, platform_root=platform_root).close()
    rc, env = _run_cli(
        ["lens", "--program-root", str(program_root), "recheck", "--round-id", ROUND_ID,
         "--external-query-mode", "statement"]
    )
    assert rc == 1
    assert env["error"]["code"] == "external_provider_required"


def test_the_cli_refuses_a_provider_with_no_mode(program_root, platform_root):
    open_store(program_root, platform_root=platform_root).close()
    rc, env = _run_cli(
        ["lens", "--program-root", str(program_root), "recheck", "--round-id", ROUND_ID,
         "--external-provider", "litapi"]
    )
    assert rc == 1
    assert env["error"]["code"] == "external_mode_required"
