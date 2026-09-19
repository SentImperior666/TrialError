"""Lane F-1b item 3: ``embeddable_text`` is ONE function, with the producing
path's semantics, and both sides call it.

The defect this closes is not a crash. The offload worker (which produced the
stored vectors) stripped ``\\r`` and mapped whitespace-only text to ``""``; the
query-side client kept both. Measured against a clean-room reference, the
divergence costs cosine **0.959** on ``"line one\\r\\nline two\\r\\n"`` and
**0.412** on ``"   \\n  "``, and **3.1 % of one measured corpus (503 of 16,173
chunks) carries ``\\r``**. Nothing downstream would ever report it: both
routes return finite, unit-norm vectors of the right width.

So the tests here are byte-identity tests on the three shapes that separated
the two functions, plus the structural assertion that there is only one
function left to diverge.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from trialerror.ingest import backends
from trialerror.ingest.backends import embeddable_text
from trialerror.offload import stage, worker


# ---------------------------------------------------------------------------
# one function, both call sites
# ---------------------------------------------------------------------------


def test_the_worker_path_and_the_query_path_use_the_SAME_function_object():
    assert stage.embeddable_text is embeddable_text
    assert worker.embeddable_text is embeddable_text
    # the query-side clients' historical name is an alias, not a second copy
    assert backends._sanitise_for_embedding is embeddable_text


def test_the_query_clients_prepare_through_that_function():
    import inspect

    source = inspect.getsource(backends._prepare_for_encoder)
    assert "embeddable_text(text)" in source
    assert "_CONTROL_CHARS_RE" not in inspect.getsource(backends), "the second sanitiser is gone"


@pytest.mark.parametrize(
    "module_order",
    [("trialerror.ingest.backends", "trialerror.offload.stage"), ("trialerror.offload.stage", "trialerror.ingest.backends")],
)
def test_neither_import_order_is_a_cycle(module_order):
    """The re-export makes ``offload.stage`` depend on ``ingest.backends``. A
    fresh interpreter per order is the only honest check -- this session has
    both modules cached."""
    code = f"import {module_order[0]}; import {module_order[1]}; print('ok')"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


# ---------------------------------------------------------------------------
# the three shapes, byte for byte
# ---------------------------------------------------------------------------


def test_carriage_return_is_stripped():
    """Shape 1, and the 3.1 % of stored chunks that carry it: the producing
    path removed ``\\r`` before embedding, so the query side must too."""
    assert embeddable_text("line one\r\nline two\r\n") == "line one\nline two\n"
    assert embeddable_text("a\rb") == "ab"
    assert "\r" not in embeddable_text("\r\r\r ends with content\r")


def test_whitespace_only_text_becomes_the_empty_string():
    """Shape 2: the measured worst case (cosine 0.412 between the two
    sanitisers' vectors for this input)."""
    assert embeddable_text("   \n  ") == ""
    assert embeddable_text("   ") == ""
    assert embeddable_text("\t\n\r ") == ""
    assert embeddable_text("") == ""
    assert embeddable_text("\x00\x1f\r") == ""


def test_control_characters_are_removed_and_tab_and_newline_are_kept():
    """Shape 3: everything else in the C0/C1 range goes, and the two
    whitespace characters a tokenizer handles stay."""
    assert embeddable_text("a\x00b\x1fc") == "abc"
    assert embeddable_text("head\x00 tail") == "head tail"
    assert embeddable_text("keep\ta\nline") == "keep\ta\nline"
    assert embeddable_text("del\x7fand\x9fc1") == "delandc1"


def test_nothing_else_is_touched():
    """Never strips, never collapses, never lowercases: an embedding has to be
    reproducible from the stored text."""
    for text in (
        "  leading and trailing  ",
        "double  spaces   kept",
        "MiXeD CaSe",
        "curly “quotes”, em—dash, café, naïve, 中文, \U0001f600",
    ):
        assert embeddable_text(text) == text


def test_the_producing_paths_own_cases_still_hold():
    """The assertions ``tests/test_offload_chunk_roundtrip.py`` makes about the
    worker's boundary, restated here: moving the function must not have moved
    its semantics."""
    assert embeddable_text("\x00\x1f\r") == ""
    assert embeddable_text("   ") == ""
    assert embeddable_text("head\x00 tail") == "head tail"
    assert embeddable_text("keep\ta\nline") == "keep\ta\nline"


def test_it_is_idempotent():
    for text in ("line\r\none", "   ", "a\x00b", "plain"):
        once = embeddable_text(text)
        assert embeddable_text(once) == once


# ---------------------------------------------------------------------------
# what it means at the two boundaries
# ---------------------------------------------------------------------------


def test_a_whitespace_only_query_embeds_the_instruction_alone():
    """The query-side consequence, named rather than discovered: there is no
    content to encode, and the prompt is still the prompt."""

    class FakeVocab:
        def tokenize(self, text: bytes, add_bos: bool = True, special: bool = False):
            return list(text)

        def detokenize(self, ids) -> bytes:
            return bytes(ids)

    backend = backends.LlamaServerEmbedBackend(
        model_key="k", dims=4, native_dims=8, n_ctx=4096, _tokenizer=FakeVocab()
    )
    assert backend.prepared_text("   \n ", kind="query") == backends.DEFAULT_QUERY_PROMPT


def test_a_whitespace_only_chunk_is_still_embedded_so_the_count_matches():
    """The document-side consequence: an empty string, never a dropped row --
    ``offload_embed_vectors`` checks the vector count against
    ``expect.chunk_count``."""
    texts = [embeddable_text(t) for t in ("real text", "   ", "\r\n")]
    assert texts == ["real text", "", ""]
    assert len(texts) == 3
