"""Lane F-1 item D: degrade vs refuse, per caller.

The rule, stated once: a search DEGRADES (full-text tier only, a named
reason in ``stats``, a ``warnings`` entry in the envelope), a status-changing
read REFUSES (``query_embed_backend_unrunnable``, the reason, the next
action). ``similar`` by chunk id is unaffected -- it ranks by a STORED
vector and never embeds anything.

Every test here runs against a program configured exactly as the live
two-machine case is: ``[ingest.embed] backend = "offload"``, which is a
backend whose ``embed_batch`` raises by design. Before this lane each of
these paths surfaced that raise as a traceback.
"""

from __future__ import annotations

import pytest

from tests._retrieve_fixtures import bootstrap_launch, build_small_corpus
from trialerror.lens.novelty import NoveltyError
from trialerror.retrieve import engine
from trialerror.retrieve.errors import QUERY_EMBED_UNRUNNABLE_CODE, QueryEmbedBackendUnrunnableError
from trialerror.verify.errors import QueryEmbedBackendUnrunnableError as VerifyUnrunnable
from trialerror.verify.hypothesis import stratified_retrieve

_OFFLOAD_CONFIG = '[program]\nid = "PROG-test"\n\n[ingest.embed]\nbackend = "offload"\nmodel_key = "{key}"\ndims = {dims}\n'


@pytest.fixture()
def offload_corpus(store, program_root):
    """The small fixture corpus, re-keyed so its stored vectors carry the
    model key an offload-backed program declares -- i.e. a corpus that IS
    embedded and searchable, on a program that cannot embed anything more."""
    built = build_small_corpus(store)
    fake_key, dims = built["model_key"], built["dims"]
    store.knowledge.execute("UPDATE emb SET model_key = 'real-key'")
    store.knowledge.execute("UPDATE vec_index_registry SET model_key = 'real-key'")
    store.knowledge.execute(
        f"ALTER TABLE vec_chunks__{fake_key.replace('-', '_')} RENAME TO vec_chunks__real_key"
    )
    store.knowledge.execute("UPDATE vec_chunks__real_key SET model_key = 'real-key'")
    store.knowledge.execute("UPDATE vec_index_registry SET table_name = 'vec_chunks__real_key'")
    store.knowledge.commit()
    (program_root / "trialerror.toml").write_text(
        _OFFLOAD_CONFIG.format(key="real-key", dims=dims), encoding="utf-8"
    )
    return built


def _add_query_backend(program_root, table_body: str) -> None:
    with (program_root / "trialerror.toml").open("a", encoding="utf-8") as fh:
        fh.write("\n[ingest.embed.query]\n" + table_body)


# ---------------------------------------------------------------------------
# the helper
# ---------------------------------------------------------------------------


def test_the_helper_returns_the_offload_reason_rather_than_raising(store, offload_corpus):
    vector, reason = engine.query_vector_or_reason(store, "quorum reconfiguration")
    assert vector is None
    assert reason == "embedding runs on the DEV GPU worker, never in this process"
    assert engine.query_embed_runnable(store) == (False, reason)


def test_the_helper_returns_a_vector_once_a_query_side_backend_is_named(store, offload_corpus, program_root):
    _add_query_backend(program_root, 'backend = "fake"\n')
    vector, reason = engine.query_vector_or_reason(store, "quorum reconfiguration")
    assert reason == ""
    assert vector is not None and len(vector) == offload_corpus["dims"]
    assert engine.query_embed_runnable(store) == (True, "")


def test_a_mismatched_query_table_is_a_reason_not_a_traceback(store, offload_corpus, program_root):
    _add_query_backend(program_root, 'backend = "fake"\nmodel_key = "some-other-key"\n')
    vector, reason = engine.query_vector_or_reason(store, "quorum")
    assert vector is None
    assert "could not be resolved" in reason and "some-other-key" in reason


# ---------------------------------------------------------------------------
# search: degrade
# ---------------------------------------------------------------------------


def test_auto_mode_degrades_to_the_full_text_tier_and_says_why(store, offload_corpus):
    result = engine.search(store, query="quorum reconfiguration", mode="auto")
    assert result["ok"] is True
    assert result["results"], "the full-text tier still answered"
    assert result["tiers_used"] == ["fts"], "tiers_used stays honest"
    assert result["stats"]["vector_skipped_reason"] == (
        "embedding runs on the DEV GPU worker, never in this process"
    )
    assert result["stats"]["vector_scored"] == 0


def test_hybrid_mode_degrades_the_same_way(store, offload_corpus):
    result = engine.search(store, query="leader election timeouts", mode="hybrid")
    assert result["ok"] is True and "vector" not in result["tiers_used"]
    assert result["stats"]["vector_skipped_reason"]


def test_fts_mode_is_untouched_and_reports_no_skip(store, offload_corpus):
    result = engine.search(store, query="retry budgets", mode="fts")
    assert result["tiers_used"] == ["fts"]
    assert "vector_skipped_reason" not in result["stats"]


def test_vector_mode_refuses_because_there_is_nothing_to_rank_by(store, offload_corpus):
    with pytest.raises(QueryEmbedBackendUnrunnableError) as exc:
        engine.search(store, query="retry budgets", mode="vector")
    message = str(exc.value)
    assert exc.value.code == QUERY_EMBED_UNRUNNABLE_CODE
    assert engine.QUERY_EMBED_DOCTOR_CHECK in message
    assert engine.QUERY_EMBED_TABLE in message


def test_a_runnable_query_backend_restores_the_vector_tier(store, offload_corpus, program_root):
    _add_query_backend(program_root, 'backend = "fake"\n')
    result = engine.search(store, query="quorum reconfiguration", mode="auto")
    assert "vector" in result["tiers_used"]
    assert "vector_skipped_reason" not in result["stats"]


def test_similar_by_chunk_id_is_unaffected(store, offload_corpus):
    """It ranks by the STORED vector: no embedding, no degrade, no warning."""
    ref = offload_corpus["open_chunk_ids"][0]
    result = engine.similar(store, ref, k=3)
    assert result["ok"] is True
    assert result["results"], "stored vectors still rank"
    assert all(row["chunk_id"] != ref for row in result["results"])


# ---------------------------------------------------------------------------
# the CLI envelope
# ---------------------------------------------------------------------------


def _run_cli(argv: list[str]) -> dict:
    import argparse

    from trialerror.cli import query as query_cli

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="group")
    query_cli.register(subparsers)
    args = parser.parse_args(argv)
    args.platform_root = None
    return args.handler(args)


def test_the_search_envelope_carries_a_warning_naming_the_check_and_the_table(
    store, offload_corpus, program_root, platform_root, monkeypatch
):
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(platform_root))
    store.close()
    envelope = _run_cli(["query", "search", "quorum reconfiguration", "--program-root", str(program_root)])
    assert envelope["ok"] is True
    warnings = envelope["warnings"]
    assert len(warnings) == 1
    warning = warnings[0]
    assert warning["code"] == QUERY_EMBED_UNRUNNABLE_CODE
    assert warning["doctor_check"] == engine.QUERY_EMBED_DOCTOR_CHECK
    assert warning["config_table"] == engine.QUERY_EMBED_TABLE
    assert "DEV GPU worker" in warning["message"]
    assert envelope["nextActions"], "the warning offers the doctor command"


def test_a_healthy_search_envelope_has_no_warnings_key_at_all(
    store, offload_corpus, program_root, platform_root, monkeypatch
):
    """The key is additive: a program with a runnable query backend emits
    exactly the envelope it emitted before this lane."""
    _add_query_backend(program_root, 'backend = "fake"\n')
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(platform_root))
    store.close()
    envelope = _run_cli(["query", "search", "quorum reconfiguration", "--program-root", str(program_root)])
    assert "warnings" not in envelope


def test_vector_mode_is_an_envelope_error_not_a_traceback(
    store, offload_corpus, program_root, platform_root, monkeypatch
):
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(platform_root))
    store.close()
    envelope = _run_cli(
        ["query", "search", "retry budgets", "--mode", "vector", "--program-root", str(program_root)]
    )
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == QUERY_EMBED_UNRUNNABLE_CODE
    assert envelope["nextActions"]


# ---------------------------------------------------------------------------
# verify hypothesis: refuse
# ---------------------------------------------------------------------------


def test_stratified_retrieve_refuses_in_every_mode_but_fts(store, offload_corpus):
    for mode in ("hybrid", "auto", "vector"):
        with pytest.raises(VerifyUnrunnable) as exc:
            stratified_retrieve(store, query="quorum reconfiguration", mode=mode)
        assert exc.value.code == QUERY_EMBED_UNRUNNABLE_CODE
        assert engine.QUERY_EMBED_DOCTOR_CHECK in str(exc.value)
        assert "--corpus-mode fts" in str(exc.value)


def test_stratified_retrieve_with_an_explicit_fts_mode_proceeds_and_records_the_fallback(store, offload_corpus):
    arms = stratified_retrieve(store, query="quorum reconfiguration", mode="fts")
    assert arms["all"], "the full-text tier answered"
    assert arms["stratify_method"] == "rank_fallback", "the record says which instrument ran"
    assert arms["query_vector_reason"] == "embedding runs on the DEV GPU worker, never in this process"


def test_a_healthy_program_still_stratifies_by_distance_and_says_nothing_extra(
    store, offload_corpus, program_root
):
    _add_query_backend(program_root, 'backend = "fake"\n')
    arms = stratified_retrieve(store, query="quorum reconfiguration", mode="hybrid")
    assert arms["stratify_method"] == "distance"
    assert "query_vector_reason" not in arms


def test_run_hypothesis_verification_refuses_before_it_writes_anything(store, offload_corpus):
    from trialerror.verify.hypothesis import run_hypothesis_verification

    launch_id = bootstrap_launch(store)
    calls: list[object] = []

    with pytest.raises(VerifyUnrunnable):
        run_hypothesis_verification(
            store,
            hypothesis_text="Retry budgets bound tail latency during failover.",
            judge=lambda envelope: calls.append(envelope),
            issued_by_launch=launch_id,
            prereg=True,
        )
    assert calls == [], "the judge was never called"
    assert store.knowledge.execute("SELECT COUNT(*) AS n FROM verdict").fetchone()["n"] == 0
    assert store.ops.execute("SELECT COUNT(*) AS n FROM prereg").fetchone()["n"] == 0


def test_run_hypothesis_verification_refuses_even_in_fts_mode(store, offload_corpus):
    """The screen has a carve-out; a verdict does not. C-0096: the status
    change IS the output here."""
    from trialerror.verify.hypothesis import run_hypothesis_verification

    launch_id = bootstrap_launch(store)
    with pytest.raises(VerifyUnrunnable):
        run_hypothesis_verification(
            store,
            hypothesis_text="Retry budgets bound tail latency during failover.",
            judge=lambda envelope: "supported",
            issued_by_launch=launch_id,
            mode="fts",
        )


# ---------------------------------------------------------------------------
# lens screen: refuse
# ---------------------------------------------------------------------------


def test_the_mechanical_screen_refuses_and_names_the_check(store, offload_corpus):
    from trialerror.lens.novelty import run_mechanical_screen

    with pytest.raises(NoveltyError) as exc:
        run_mechanical_screen(store, round_id="RND-1")
    message = str(exc.value)
    assert engine.QUERY_EMBED_DOCTOR_CHECK in message
    assert engine.QUERY_EMBED_TABLE in message
    assert "--corpus-mode" in message, "the message says what that flag does and does not lift"


def test_the_screen_refuses_under_corpus_mode_fts_too_and_says_why(store, offload_corpus):
    from trialerror.lens.novelty import run_mechanical_screen

    with pytest.raises(NoveltyError) as exc:
        run_mechanical_screen(store, round_id="RND-1", corpus_mode="fts")
    assert "R1/R2/R3" in str(exc.value)


def test_the_screen_refusal_is_a_typed_error_carrying_the_envelope_code(store, offload_corpus):
    """V-5: the CLI used to choose this envelope's code by looking for the
    doctor check's NAME inside the exception message, so any reword of
    ``query_embed_refusal_message`` -- or a future refusal quoting the check
    name for another reason -- silently changed what an agent parsing the
    envelope saw. The code now comes off the exception type, as it does for
    ``verify hypothesis`` and ``query search``."""
    from trialerror.lens.novelty import QueryEmbedBackendUnrunnableError as ScreenUnrunnable
    from trialerror.lens.novelty import run_mechanical_screen

    with pytest.raises(ScreenUnrunnable) as exc:
        run_mechanical_screen(store, round_id="RND-1")
    assert isinstance(exc.value, NoveltyError), "still a structural screen refusal"
    assert exc.value.code == QUERY_EMBED_UNRUNNABLE_CODE
    # and a refusal that is NOT about the backend carries no code at all
    assert NoveltyError("some other structural refusal").code is None


def test_the_screen_cli_envelope_takes_its_code_from_the_type_not_the_message(
    store, offload_corpus, program_root, capsys, monkeypatch
):
    """Driven through the real parser, with the refusal MESSAGE replaced by a
    string that mentions neither the doctor check nor the config table: the
    envelope still carries the code, the check and the table, because they
    come off the exception type rather than out of the prose."""
    import json as _json

    from trialerror.cli import main

    monkeypatch.setattr(
        engine, "query_embed_refusal_message", lambda _reason, *, action: "no query-side encoder here"
    )
    store.close()

    exit_code = main(
        ["lens", "--program-root", str(program_root), "screen", "--round-id", "RND-1", "--mechanical"]
    )
    env = _json.loads(capsys.readouterr().out.strip())
    assert exit_code != 0
    assert env["ok"] is False
    assert env["error"]["code"] == QUERY_EMBED_UNRUNNABLE_CODE
    assert engine.QUERY_EMBED_DOCTOR_CHECK not in env["error"]["message"], "the prose is gone"
    assert env["error"]["details"]["doctor_check"] == engine.QUERY_EMBED_DOCTOR_CHECK
    assert env["error"]["details"]["config_table"] == engine.QUERY_EMBED_TABLE
