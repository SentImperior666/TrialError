"""Lane FB-1 item F5: the real-backend requirement is per stage.

One global flag was too coarse for the case it is most used in: a program
with a real embedder and no OCR stack (or the reverse). Two knobs now, each
defaulting to the global one -- and the load-bearing property is that every
configuration written before these keys existed resolves BYTE-FOR-BYTE as it
did before.
"""

from __future__ import annotations

import pytest

from trialerror.ingest.backends import (
    RealBackendRequiredError,
    assert_real_backends_if_required,
    require_real_key_for,
    stage_requires_real,
)
from trialerror.offload.checks import check_fake_backend_rows
from trialerror.util.doctor import DoctorContext

_REAL_OCR = {"backend": "offload"}
_REAL_EMBED = {"backend": "offload"}


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------


def test_absent_everything_requires_nothing():
    for stage in ("ocr", "embed"):
        assert stage_requires_real(None, stage) is False
        assert stage_requires_real({}, stage) is False
        assert stage_requires_real({"ingest": {}}, stage) is False


def test_the_global_flag_still_governs_both_stages():
    config = {"ingest": {"require_real_backends": True, "ocr": _REAL_OCR, "embed": _REAL_EMBED}}
    assert stage_requires_real(config, "ocr") is True
    assert stage_requires_real(config, "embed") is True
    assert require_real_key_for(config, "ocr") == "[ingest] require_real_backends"


def test_a_stage_key_wins_for_its_own_stage_only():
    config = {
        "ingest": {
            "require_real_backends": True,
            "ocr": {"backend": "fake", "require_real": False},
            "embed": _REAL_EMBED,
        }
    }
    assert stage_requires_real(config, "ocr") is False
    assert stage_requires_real(config, "embed") is True
    assert require_real_key_for(config, "ocr") == "[ingest.ocr] require_real"
    assert require_real_key_for(config, "embed") == "[ingest] require_real_backends"


def test_a_stage_key_can_also_raise_the_bar_a_permissive_program_never_set():
    config = {"ingest": {"embed": {"backend": "fake", "require_real": True}}}
    assert stage_requires_real(config, "embed") is True
    assert stage_requires_real(config, "ocr") is False


@pytest.mark.parametrize(
    "config",
    [
        None,
        {},
        {"ingest": {}},
        {"ingest": {"require_real_backends": False}},
        {"ingest": {"require_real_backends": False, "ocr": {"backend": "fake"}}},
        {"ingest": {"require_real_backends": True, "ocr": _REAL_OCR, "embed": _REAL_EMBED}},
    ],
)
def test_every_pre_f5_configuration_is_accepted_exactly_as_before(config):
    assert_real_backends_if_required(config)  # must not raise


@pytest.mark.parametrize(
    "config",
    [
        {"ingest": {"require_real_backends": True}},
        {"ingest": {"require_real_backends": True, "ocr": {"backend": "fake"}, "embed": _REAL_EMBED}},
        {"ingest": {"require_real_backends": True, "ocr": _REAL_OCR}},
    ],
)
def test_every_pre_f5_refusal_is_still_refused(config):
    with pytest.raises(RealBackendRequiredError):
        assert_real_backends_if_required(config)


# ---------------------------------------------------------------------------
# the loader's refusal
# ---------------------------------------------------------------------------


def test_one_stage_exempted_lets_the_other_still_refuse():
    config = {
        "ingest": {
            "require_real_backends": True,
            "ocr": {"backend": "fake", "require_real": False},
            "embed": {"backend": "fake"},
        }
    }
    with pytest.raises(RealBackendRequiredError) as exc:
        assert_real_backends_if_required(config)
    message = str(exc.value)
    assert "[ingest.embed]" in message
    assert "[ingest] require_real_backends" in message  # the key that decided it
    assert "[ingest.ocr]" not in message


def test_an_exempted_stage_with_a_fake_backend_is_accepted():
    config = {
        "ingest": {
            "require_real_backends": True,
            "ocr": {"backend": "fake", "require_real": False},
            "embed": _REAL_EMBED,
        }
    }
    assert_real_backends_if_required(config)


def test_an_absent_table_still_counts_as_fake_even_under_a_per_stage_flag():
    """The per-stage key lives inside the stage's table, so there is no way
    to exempt a stage whose table is absent -- and an absent table is one of
    the two conditions being refused."""
    config = {"ingest": {"require_real_backends": True, "embed": _REAL_EMBED}}
    with pytest.raises(RealBackendRequiredError) as exc:
        assert_real_backends_if_required(config)
    assert "[ingest.ocr] is absent" in str(exc.value)
    assert "require_real = false" in str(exc.value)  # the way out is named


def test_the_refusal_names_the_stage_key_when_the_stage_key_decided_it():
    config = {"ingest": {"ocr": {"backend": "fake", "require_real": True}}}
    with pytest.raises(RealBackendRequiredError) as exc:
        assert_real_backends_if_required(config)
    assert "[ingest.ocr] require_real = true" in str(exc.value)


def test_the_query_side_follows_the_embed_stages_own_requirement():
    exempted = {
        "ingest": {
            "require_real_backends": True,
            "ocr": _REAL_OCR,
            "embed": {"backend": "fake", "require_real": False, "query": {"backend": "fake"}},
        }
    }
    assert_real_backends_if_required(exempted)  # the exemption it just granted

    required = {
        "ingest": {
            "ocr": _REAL_OCR,
            "embed": {"backend": "offload", "require_real": True, "query": {"backend": "fake"}},
        }
    }
    with pytest.raises(RealBackendRequiredError) as exc:
        assert_real_backends_if_required(required)
    assert "query" in str(exc.value)


# ---------------------------------------------------------------------------
# the doctor check
# ---------------------------------------------------------------------------


def _ingest_through_the_fake_backends(store, program_root) -> str:
    """One document taken through the real pipeline on the fake backends --
    the state this check exists to notice. Same path
    ``tests/test_offload_checks.py`` uses, so the two files cannot disagree
    about what a fake-backend row looks like."""
    from trialerror.ingest import pipeline
    from trialerror.jobs.worker import run_one
    from tests._ingest_fixtures import bootstrap_launch, write_scanned_pdf_fixture

    raw = program_root / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    launch_id = bootstrap_launch(store)
    source = pipeline.register_source(
        store, kind="paper", title="t", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id,
    )
    added = pipeline.add_document(
        store,
        program_root=program_root,
        source_id=source["source_id"],
        raw_path=write_scanned_pdf_fixture(raw / "scan.pdf"),
        created_by_launch=launch_id,
        media_type="pdf-scan",
    )
    for i in range(8):
        if run_one(store, worker_id=f"w{i}")["status"] == "idle":
            break
    return added["document"]["doc_id"]


def _write_config(program_root, body: str) -> None:
    (program_root / "trialerror.toml").write_text(f'[program]\nid = "p"\n\n{body}', encoding="utf-8")


def test_fake_embeddings_fail_only_where_the_embed_stage_is_required_to_be_real(
    store, program_root, platform_root
):
    doc_id = _ingest_through_the_fake_backends(store, program_root)
    # isolate the embed half: this document's OCR backend is not the subject
    store.knowledge.execute("UPDATE document SET ocr_backend = 'offload' WHERE doc_id = ?", (doc_id,))
    store.knowledge.commit()
    store.close()

    _write_config(
        program_root,
        "[ingest]\nrequire_real_backends = true\n\n[ingest.ocr]\nbackend = 'offload'\n\n"
        "[ingest.embed]\nbackend = 'offload'\n",
    )
    result = check_fake_backend_rows(DoctorContext(program_root=program_root, platform_root=platform_root))
    assert result.status == "fail"
    assert "[ingest] require_real_backends = true" in result.message

    _write_config(
        program_root,
        "[ingest]\nrequire_real_backends = true\n\n[ingest.ocr]\nbackend = 'offload'\n\n"
        "[ingest.embed]\nbackend = 'fake'\nrequire_real = false\n",
    )
    result = check_fake_backend_rows(DoctorContext(program_root=program_root, platform_root=platform_root))
    assert result.status == "warn"
    assert "not required to be real" in result.message
    assert result.details["require_real"]["embed"]["require_real"] is False
    assert result.details["require_real"]["embed"]["key"] == "[ingest.embed] require_real"


def test_fake_ocr_documents_fail_only_where_the_ocr_stage_is_required_to_be_real(
    store, program_root, platform_root
):
    _ingest_through_the_fake_backends(store, program_root)
    # isolate the OCR half: no fake embedding rows left to judge
    store.knowledge.execute("UPDATE emb SET model_key = 'real-key-1' WHERE model_key LIKE 'fake-%'")
    store.knowledge.commit()
    store.close()

    _write_config(program_root, "[ingest]\nrequire_real_backends = false\n\n[ingest.ocr]\nbackend = 'fake'\nrequire_real = true\n")
    result = check_fake_backend_rows(DoctorContext(program_root=program_root, platform_root=platform_root))
    assert result.status == "fail"
    assert "ocr_backend = 'fake'" in result.message
    assert "[ingest.ocr] require_real = true" in result.message

    _write_config(program_root, "[ingest]\nrequire_real_backends = false\n")
    result = check_fake_backend_rows(DoctorContext(program_root=program_root, platform_root=platform_root))
    assert result.status == "warn"


def test_one_stage_fails_while_the_other_is_only_reported(store, program_root, platform_root):
    _ingest_through_the_fake_backends(store, program_root)  # fake OCR AND fake embeddings
    store.close()

    _write_config(
        program_root,
        "[ingest]\nrequire_real_backends = true\n\n[ingest.ocr]\nbackend = 'fake'\nrequire_real = false\n\n"
        "[ingest.embed]\nbackend = 'offload'\n",
    )
    result = check_fake_backend_rows(DoctorContext(program_root=program_root, platform_root=platform_root))
    assert result.status == "fail"
    assert "emb row(s)" in result.message and "document(s)" in result.message
    assert "the OCR stage is not required to be real here" in result.message
    assert result.details["require_real"]["ocr"]["require_real"] is False
    assert result.details["require_real"]["embed"]["require_real"] is True


def test_a_clean_program_passes_and_still_reports_both_requirements(store, program_root, platform_root):
    store.close()
    _write_config(program_root, "[ingest]\nrequire_real_backends = true\n")
    result = check_fake_backend_rows(DoctorContext(program_root=program_root, platform_root=platform_root))
    assert result.status == "pass"
    assert set(result.details["require_real"]) == {"embed", "ocr"}


# ---------------------------------------------------------------------------
# backlog item (d): the message names the configured key and the purge verb
# ---------------------------------------------------------------------------


def test_the_failure_names_the_configured_key_calls_the_others_superseded_and_points_at_purge(
    store, program_root, platform_root
):
    _ingest_through_the_fake_backends(store, program_root)
    fake_keys = [
        r["model_key"]
        for r in store.knowledge.execute("SELECT DISTINCT model_key FROM emb WHERE model_key LIKE 'fake-%'")
    ]
    store.close()
    assert fake_keys, "the fixture should have written fake emb rows"

    _write_config(
        program_root,
        "[ingest]\nrequire_real_backends = true\n\n[ingest.ocr]\nbackend = 'offload'\n"
        "require_real = false\n\n[ingest.embed]\nbackend = 'offload'\nmodel_key = 'real-encoder-1'\n",
    )
    result = check_fake_backend_rows(DoctorContext(program_root=program_root, platform_root=platform_root))

    assert result.status == "fail"
    assert "'real-encoder-1'" in result.message  # the key actually configured now
    assert "SUPERSEDED, not in use" in result.message
    assert "ingest purge-embeddings" in result.message
    assert f"--model-key {fake_keys[0]}" in result.message  # the one superseded key, named
    assert "chunks_now_without_any_embedding" in result.message
    assert result.details["configured_embed_model_key"] == "real-encoder-1"
    assert result.details["superseded_fake_model_keys"] == sorted(fake_keys)


def test_a_configured_key_that_is_itself_fake_is_not_called_superseded(
    store, program_root, platform_root
):
    """Nothing supersedes a key the program is still embedding under -- and
    telling an operator to purge the key their live search reads from would
    be telling them to empty their corpus."""
    _ingest_through_the_fake_backends(store, program_root)
    store.close()
    _write_config(program_root, "[ingest]\n\n[ingest.embed]\nrequire_real = true\n")
    result = check_fake_backend_rows(DoctorContext(program_root=program_root, platform_root=platform_root))

    assert result.status == "fail"
    assert "is itself fake" in result.message
    assert "purge-embeddings" not in result.message
    assert result.details["superseded_fake_model_keys"] == []


def test_the_permissive_message_still_names_the_configured_key(store, program_root, platform_root):
    _ingest_through_the_fake_backends(store, program_root)
    store.close()
    _write_config(program_root, "[ingest]\nrequire_real_backends = false\n")
    result = check_fake_backend_rows(DoctorContext(program_root=program_root, platform_root=platform_root))
    assert result.status == "warn"
    assert "this program embeds under" in result.message
