"""Lane F-1 items A + B: query-side embed backend resolution
(``[ingest.embed.query]``) and ``runnable()`` on the embed backend protocol.

The defect these two items close: with ``[ingest.embed] backend = "offload"``
every call that embeds FRESH TEXT raises
``trialerror.offload.marker.OffloadNotRunnable`` in the calling process, and a
retrieval surface is full of them (the query itself, a hypothesis statement,
an idea statement). The document side is right to refuse -- its model runs on
another machine -- so the query side gets its own backend choice, and every
backend can now be ASKED whether it can compute here before being told to.

Nothing in this module needs a GPU, a model file or a network.
"""

from __future__ import annotations

import pytest

from trialerror.ingest.backends import (
    QUERY_EMBED_BACKEND_INVALID,
    QUERY_EMBED_TABLE,
    SAME_AS_DOCUMENT,
    FakeEmbedBackend,
    QueryEmbedBackendMismatchError,
    RealQwenEmbedBackend,
    embed_backend_runnable,
    load_embed_backend,
    load_query_embed_backend,
    query_embed_backend_name,
)
from trialerror.offload.marker import OffloadMarker, OffloadNotRunnable
from trialerror.retrieve import engine

_OFFLOAD_EMBED = {"backend": "offload", "model_key": "real-key", "dims": 2048}


def _write_config(program_root, body: str) -> None:
    (program_root / "trialerror.toml").write_text(
        '[program]\nid = "PROG-test"\n\n' + body, encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# A. resolution
# ---------------------------------------------------------------------------


def test_an_unconfigured_program_resolves_the_document_backend_object_itself():
    """``backend = "same"`` is the default and it is not a COPY -- the very
    same object, so a program that never writes the table cannot have
    acquired a second model load, a second cache or a second key."""
    config = {"backend": "fake", "dims": 8}
    document = load_embed_backend(config)
    query = load_query_embed_backend(config)
    assert query_embed_backend_name(config) == SAME_AS_DOCUMENT
    assert isinstance(query, FakeEmbedBackend)
    assert (query.model_key, query.dims) == (document.model_key, document.dims)


def test_same_spelled_explicitly_is_the_document_backend_too():
    config = {"backend": "fake", "dims": 8, "query": {"backend": "same"}}
    assert query_embed_backend_name(config) == "same"
    assert load_query_embed_backend(config).model_key == load_embed_backend(config).model_key


def test_an_offload_document_side_can_name_a_fake_query_side_under_the_same_key():
    """The shape that makes an offload-backed program searchable at all: the
    document side keeps the key the remote worker stamps, the query side
    inherits it."""
    config = {**_OFFLOAD_EMBED, "query": {"backend": "fake"}}
    document = load_embed_backend(config)
    query = load_query_embed_backend(config)
    assert isinstance(document, OffloadMarker)
    assert isinstance(query, FakeEmbedBackend)
    assert query.model_key == "real-key" and query.dims == 2048
    assert len(query.embed_batch(["a query"], kind="query")[0]) == 2048


def test_the_fake_document_side_never_reads_model_key_from_its_own_table():
    """The inherited key is a QUERY-side affordance only. A stale
    ``model_key`` line beside ``backend = "fake"`` must keep steering
    nothing (OPERATOR_GUIDE's ``embedding_stale`` note)."""
    document = load_embed_backend({"backend": "fake", "dims": 8, "model_key": "real-key"})
    assert document.model_key == "fake-8"


def test_a_query_side_model_key_that_disagrees_is_refused_naming_both_keys():
    config = {**_OFFLOAD_EMBED, "query": {"backend": "fake", "model_key": "other-key", "dims": 2048}}
    with pytest.raises(QueryEmbedBackendMismatchError) as exc:
        load_query_embed_backend(config)
    message = str(exc.value)
    assert "other-key" in message and "real-key" in message
    assert QUERY_EMBED_TABLE in message and "ingest.embed" in message


def test_a_query_side_dims_that_disagrees_is_refused_too():
    config = {**_OFFLOAD_EMBED, "query": {"backend": "fake", "dims": 16}}
    with pytest.raises(QueryEmbedBackendMismatchError) as exc:
        load_query_embed_backend(config)
    assert "2048" in str(exc.value) and "16" in str(exc.value)


def test_a_non_table_query_value_is_refused_rather_than_ignored():
    with pytest.raises(QueryEmbedBackendMismatchError):
        load_query_embed_backend({"backend": "fake", "query": "llama_cpp"})


def test_a_non_table_query_value_is_not_reported_as_the_document_backend():
    """V-7's smaller half: the reporter used to say ``"same"`` for a table
    that resolution refuses, so the doctor's ``details.query_backend`` named a
    backend nothing would ever build."""
    assert query_embed_backend_name({"backend": "fake", "query": "llama_cpp"}) == QUERY_EMBED_BACKEND_INVALID
    assert QUERY_EMBED_BACKEND_INVALID != SAME_AS_DOCUMENT


def test_a_query_side_refusal_names_the_query_table_not_the_document_one():
    """V-7: a query-side table naming a backend that is none of
    same/fake/llama_cpp/offload falls through to the real-driver branch,
    whose message was written for the document side -- it pointed the
    operator at ``[ingest.embed]``, a table they had not edited."""
    with pytest.raises(ValueError) as exc:
        load_query_embed_backend({**_OFFLOAD_EMBED, "query": {"backend": "nonsense"}})
    message = str(exc.value)
    assert f"{QUERY_EMBED_TABLE}.backend = 'nonsense'" in message
    assert f"{QUERY_EMBED_TABLE}.python_exe" in message
    assert "ingest.embed.python_exe" not in message

    # the same for the llama_cpp branch's two required keys
    with pytest.raises(ValueError) as exc:
        load_query_embed_backend({**_OFFLOAD_EMBED, "query": {"backend": "llama_cpp"}})
    assert f"{QUERY_EMBED_TABLE}.backend" in str(exc.value)

    # and the DOCUMENT side's own messages are unchanged
    with pytest.raises(ValueError) as exc:
        load_embed_backend({"backend": "nonsense"})
    assert "ingest.embed.backend = 'nonsense' requires ingest.embed.python_exe" in str(exc.value)


def test_the_engine_resolves_both_sides_and_rejects_any_other_side(store, program_root):
    _write_config(
        program_root,
        '[ingest.embed]\nbackend = "offload"\nmodel_key = "real-key"\ndims = 2048\n\n'
        f"[{QUERY_EMBED_TABLE}]\nbackend = \"fake\"\n",
    )
    doc_key, doc_backend = engine._resolve_embed_backend(store)
    query_key, query_backend = engine._resolve_embed_backend(store, side="query")
    assert doc_key == query_key == "real-key"
    assert isinstance(doc_backend, OffloadMarker)
    assert isinstance(query_backend, FakeEmbedBackend)
    with pytest.raises(ValueError):
        engine._resolve_embed_backend(store, side="both")


def test_the_engines_query_side_default_is_still_the_document_backend(store, program_root):
    _write_config(program_root, '[ingest.embed]\nbackend = "fake"\ndims = 8\n')
    assert engine._resolve_embed_backend(store, side="query")[0] == "fake-8"


# ---------------------------------------------------------------------------
# B. runnable()
# ---------------------------------------------------------------------------


def test_the_offload_marker_is_not_runnable_and_says_why():
    marker = OffloadMarker("embed", {"model_key": "real-key", "dims": 2048})
    ok, reason = marker.runnable()
    assert ok is False
    assert reason == "embedding runs on the DEV GPU worker, never in this process"
    # the raise is still there for the caller that computes anyway
    with pytest.raises(OffloadNotRunnable):
        marker.embed_batch(["x"])


def test_the_ocr_marker_names_ocr_in_its_reason():
    ok, reason = OffloadMarker("ocr", {}).runnable()
    assert ok is False and "OCR" in reason


def test_the_fake_and_real_backends_are_runnable():
    assert FakeEmbedBackend(dims=4).runnable() == (True, "")
    real = RealQwenEmbedBackend(python_exe="/nonexistent/python", module_dir="/nonexistent")
    assert real.runnable() == (True, "")


def test_the_helper_defaults_a_backend_without_the_method_to_runnable():
    class Legacy:
        model_key = "legacy"
        dims = 4

        def embed_batch(self, texts, *, kind="document"):
            return [[0.0] * 4 for _ in texts]

    assert embed_backend_runnable(Legacy()) == (True, "")


def test_a_runnable_probe_that_raises_is_a_no_carrying_its_own_text():
    class Exploding:
        def runnable(self):
            raise OSError("libc too old")

    ok, reason = embed_backend_runnable(Exploding())
    assert ok is False
    assert "OSError" in reason and "libc too old" in reason
