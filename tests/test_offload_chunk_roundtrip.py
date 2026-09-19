"""``offload/<job>/chunks.jsonl`` must survive the characters real prose
contains -- control characters AND the Unicode line-break class.

THE LIVE DEFECT. Three documents failed to embed on the DEV worker on every
attempt with ``JSONDecodeError: Unterminated string starting at: line 1
column 69 (char 68)`` -- column 69 being exactly where the ``"text"`` value
of a ``chunks.jsonl`` line opens. The root cause is a disagreement about
what a line is, not malformed JSON:

* the writer serialized with ``json.dumps(..., ensure_ascii=False)``, which
  escapes every C0 control (U+0000-U+001F) but leaves U+0085 NEL, U+2028
  LINE SEPARATOR and U+2029 PARAGRAPH SEPARATOR raw -- they are legal,
  ordinary content inside a JSON string;
* the reader split the file with ``str.splitlines()``, which DOES treat all
  three as line terminators.

So one record became two physical lines and the worker handed ``json.loads``
a fragment that ended mid-string. ``test_the_old_writer_reader_pair_*``
below pins that mechanism with the live error message; everything else pins
the fix. Note which characters were never the problem: NUL, unit separator
and a lone CR round-trip through the OLD pair too (they are C0, so
``json.dumps`` escaped them) -- they are in this file as the regression
guard the brief asked for, not as the cause.
"""

from __future__ import annotations

import json

import pytest

from trialerror.offload import protocol, stage
from trialerror.offload.stage import (
    EMBED_INPUT_NAME,
    ChunkPayloadError,
    build_chunks_payload,
    embeddable_text,
    read_chunks_payload,
)
from trialerror.offload.worker import _run_embed
from tests._offload_fixtures import StubEmbedBackend

CHUNK_ID = "CHNK-01JQZ8X4A7N6MCTVB9KDWF2HRY"

#: The C0 controls named in the brief -- escaped by ``json.dumps`` all
#: along, so these are a guard, not a reproduction.
C0_CASES = {
    "nul": "\x00",
    "unit_separator": "\x1f",
    "lone_carriage_return": "\r",
}

#: The characters that actually broke the live jobs.
LINE_BREAK_CASES = {
    "next_line": "\u0085",
    "line_separator": "\u2028",
    "paragraph_separator": "\u2029",
}

ALL_CASES = {**C0_CASES, **LINE_BREAK_CASES}


def _one(text: str, *, chunk_id: str = CHUNK_ID, seq: int = 0) -> list[dict]:
    return [{"chunk_id": chunk_id, "seq": seq, "text": text}]


# ---------------------------------------------------------------------------
# the root cause, pinned
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(LINE_BREAK_CASES))
def test_the_old_writer_reader_pair_produced_the_live_error(name):
    """``ensure_ascii=False`` + ``str.splitlines()``, i.e. exactly the code
    this lane replaced, on exactly one character. The column in the message
    is where the ``"text"`` value opens -- the live symptom, reproduced."""
    text = f"alpha{LINE_BREAK_CASES[name]}beta"
    old_line = json.dumps({"chunk_id": CHUNK_ID, "seq": 0, "text": text}, ensure_ascii=False)
    assert len(old_line.splitlines()) == 2, "splitlines cut a single valid JSON line in two"

    with pytest.raises(json.JSONDecodeError) as exc:
        for physical in old_line.splitlines():
            json.loads(physical)

    assert "Unterminated string starting at" in str(exc.value)
    # The reported position is always where the "text" VALUE opens, which is
    # what made the live column look like a fixed number: the preceding
    # fields are the same shape on every line.
    assert exc.value.pos == old_line.index('"text": ') + len('"text": ')


def test_the_old_pair_reproduces_the_live_message_exactly():
    """``line 1 column 69 (char 68)``, verbatim, from a line whose
    ``chunk_id`` is the real 31-character shape and whose ``seq`` runs into
    three digits -- the offset is just where ``"text"``'s value opens (see
    above), so this is the live message rather than a coincidence."""
    old_line = json.dumps(
        {"chunk_id": CHUNK_ID, "seq": 123, "text": "alpha\u2028beta"}, ensure_ascii=False
    )
    with pytest.raises(json.JSONDecodeError) as exc:
        json.loads(old_line.splitlines()[0])
    assert "Unterminated string starting at: line 1 column 69 (char 68)" in str(exc.value)


@pytest.mark.parametrize("name", sorted(C0_CASES))
def test_the_c0_controls_were_never_the_cause(name):
    """Measured, against the brief's hypothesis: ``json.dumps`` escapes
    every C0 control even with ``ensure_ascii=False``, so NUL / unit
    separator / lone CR survived the OLD pair unharmed. The three documents
    that failed did not fail on these."""
    text = f"alpha{C0_CASES[name]}beta"
    old_line = json.dumps({"chunk_id": CHUNK_ID, "seq": 0, "text": text}, ensure_ascii=False)
    assert len(old_line.splitlines()) == 1
    assert json.loads(old_line)["text"] == text


# ---------------------------------------------------------------------------
# the write side
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(ALL_CASES))
def test_one_chunk_is_always_one_physical_line(name):
    data = build_chunks_payload(_one(f"alpha{ALL_CASES[name]}beta"))
    text = data.decode("utf-8")
    assert len(text.splitlines()) == 1, f"{name} split the line"
    assert text.count("\n") == 1, "exactly the one terminating newline"


def test_every_chunk_of_a_document_is_one_line_each():
    chunks = [
        {"chunk_id": f"CHNK-{i:028d}", "seq": i, "text": f"page {i}{ch}body"}
        for i, ch in enumerate(ALL_CASES.values())
    ]
    data = build_chunks_payload(chunks)
    assert len(data.decode("utf-8").splitlines()) == len(chunks)
    assert [c["chunk_id"] for c in read_chunks_payload(data)] == [c["chunk_id"] for c in chunks]


@pytest.mark.parametrize("name", sorted(ALL_CASES))
def test_the_text_round_trips_byte_exactly(name):
    text = f"alpha{ALL_CASES[name]}beta"
    record = read_chunks_payload(build_chunks_payload(_one(text)))[0]
    assert record["text"] == text
    assert record["chunk_id"] == CHUNK_ID
    assert record["seq"] == 0


def test_all_six_characters_in_one_chunk_round_trip():
    text = "a\x00b\x1fc\rd\u0085e\u2028f\u2029g\nh"
    assert read_chunks_payload(build_chunks_payload(_one(text)))[0]["text"] == text


def test_non_ascii_prose_is_still_not_escaped():
    """``ensure_ascii=False`` is kept on purpose (the wire has a size cap):
    only the three line-break characters are escaped, not every accent."""
    data = build_chunks_payload(_one("éè 中文 \u2028 tail")).decode("utf-8")
    assert "éè" in data and "中文" in data
    assert "\\u2028" in data and "\u2028" not in data


def test_the_payload_survives_the_tar_transit_both_stages_use():
    """The queue does not hand the file over directly -- it goes through
    ``pack_dir``/``unpack_into``. Proven end to end on the hard characters."""
    text = "before\u2028middle\x00after\u0085end"
    data = build_chunks_payload(_one(text))
    assert read_chunks_payload(
        _through_tar(data)
    )[0]["text"] == text


def test_the_writer_refuses_rather_than_queueing_a_splittable_line(monkeypatch):
    """The law, not the comment: if a future character class stops being
    escaped, the failure happens HERE -- in the writer, on the machine that
    owns the record, naming the chunk -- rather than 20 minutes later as an
    unattributable decode error on the GPU host. Driven by disabling the
    escape table, which is the only way to reach the guard now."""
    monkeypatch.setattr(stage, "_RAW_LINE_BREAKS", {})

    with pytest.raises(ChunkPayloadError) as exc:
        build_chunks_payload(
            _one(f"alpha{LINE_BREAK_CASES['line_separator']}beta", chunk_id="CHNK-offender")
        )

    message = str(exc.value)
    assert "CHNK-offender" in message
    assert "U+2028 at offset" in message


def test_the_line_break_diagnostic_names_every_offending_codepoint():
    line = f"a{LINE_BREAK_CASES['next_line']}b{LINE_BREAK_CASES['paragraph_separator']}c"
    described = stage._describe_line_breaks(line)
    assert "U+0085 at offset 1" in described
    assert "U+2029 at offset 3" in described
    assert stage._describe_line_breaks("plain") == "no line-break character found"


def _through_tar(data: bytes, tmp=None):
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "src"
        dst = Path(td) / "dst"
        src.mkdir()
        (src / EMBED_INPUT_NAME).write_bytes(data)
        protocol.unpack_into(protocol.pack_dir(src), dst)
        return (dst / EMBED_INPUT_NAME).read_bytes()


# ---------------------------------------------------------------------------
# the read side
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(LINE_BREAK_CASES))
def test_a_payload_the_old_writer_produced_still_decodes(name):
    """The operational half of the fix: three documents are already parked
    with a raw line-break character in their ``chunks.jsonl``. A raw U+2028
    inside a JSON string is LEGAL JSON, so splitting on ``"\\n"`` alone is
    enough to decode them -- the worker's next run embeds them without the
    queue being rebuilt."""
    text = f"alpha{LINE_BREAK_CASES[name]}beta"
    legacy = (
        json.dumps({"chunk_id": CHUNK_ID, "seq": 0, "text": text}, ensure_ascii=False) + "\n"
    ).encode("utf-8")

    records = read_chunks_payload(legacy)

    assert len(records) == 1
    assert records[0]["text"] == text


def test_an_undecodable_line_names_the_chunk_and_the_line_number():
    good = build_chunks_payload(_one("fine", chunk_id="CHNK-good")).decode("utf-8").strip()
    broken = '{"chunk_id": "CHNK-broken", "seq": 1, "text": "no closing quote'
    data = (good + "\n" + broken + "\n").encode("utf-8")

    with pytest.raises(ChunkPayloadError) as exc:
        read_chunks_payload(data)

    message = str(exc.value)
    assert "line 2" in message
    assert "CHNK-broken" in message


def test_an_undecodable_line_with_no_readable_id_says_so():
    with pytest.raises(ChunkPayloadError) as exc:
        read_chunks_payload(b'{"seq": 1, "text": "unterminated\n')
    assert "id unreadable" in str(exc.value)
    assert "line 1" in str(exc.value)


def test_a_line_that_is_not_an_object_is_refused_by_name():
    with pytest.raises(ChunkPayloadError) as exc:
        read_chunks_payload(b'["chunk_id", "text"]\n')
    assert "expected an" in str(exc.value)


def test_blank_lines_and_a_crlf_transport_are_tolerated():
    text = "alpha\u2028beta"
    body = build_chunks_payload(_one(text)).decode("utf-8")
    crlf = (body.replace("\n", "\r\n") + "\r\n").encode("utf-8")
    assert read_chunks_payload(crlf)[0]["text"] == text


# ---------------------------------------------------------------------------
# the model boundary
# ---------------------------------------------------------------------------


def test_control_only_text_becomes_the_empty_string():
    assert embeddable_text("\x00\x1f\r") == ""
    assert embeddable_text("   ") == ""
    assert embeddable_text("") == ""


def test_control_characters_are_stripped_out_of_ordinary_text():
    assert embeddable_text("head\x00 tail") == "head tail"
    assert embeddable_text("keep\ta\nline") == "keep\ta\nline"


def test_the_worker_embeds_a_control_only_chunk_instead_of_failing_the_job(tmp_path):
    """A chunk of nothing but control characters must not cost the document
    its whole job: the vector COUNT is what the sandbox verifies against
    ``expect.chunk_count``, so the chunk is embedded as an empty string
    rather than dropped."""
    in_dir, out_dir = tmp_path / "in", tmp_path / "out"
    in_dir.mkdir()
    out_dir.mkdir()
    chunks = [
        {"chunk_id": "CHNK-a", "seq": 0, "text": "ordinary prose"},
        {"chunk_id": "CHNK-b", "seq": 1, "text": "\x00\x1f\r"},
        {"chunk_id": "CHNK-c", "seq": 2, "text": "tail\u2028prose"},
    ]
    (in_dir / EMBED_INPUT_NAME).write_bytes(build_chunks_payload(chunks))
    backend = StubEmbedBackend()
    manifest = {"job_id": "JOB-t", "stage": "embed", "expect": {"input_name": EMBED_INPUT_NAME}}

    # C-0097 D2: the stage returns a StageOutcome now, because it has to be
    # able to say whether a stop landed between two of its batches.
    outcome = _run_embed(backend, manifest, in_dir, out_dir, batch_size=8)

    assert outcome.stopped is False and outcome.complete is True
    assert (outcome.units_done, outcome.units_total, outcome.unit) == (3, 3, "chunk")
    assert outcome.fields["chunk_ids"] == ["CHNK-a", "CHNK-b", "CHNK-c"]
    vectors = [
        json.loads(line)
        for line in (out_dir / "vectors.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(vectors) == len(chunks)
    assert all(len(v) == backend.dims for v in vectors)


def test_the_worker_never_hands_a_nul_byte_to_the_model(tmp_path):
    in_dir, out_dir = tmp_path / "in", tmp_path / "out"
    in_dir.mkdir()
    out_dir.mkdir()
    seen: list[str] = []

    class _Recording(StubEmbedBackend):
        def embed_batch(self, texts, *, kind="document"):
            seen.extend(texts)
            return super().embed_batch(texts, kind=kind)

    (in_dir / EMBED_INPUT_NAME).write_bytes(
        build_chunks_payload(_one("head\x00\x1f tail\u2028more"))
    )
    _run_embed(_Recording(), {"job_id": "J", "stage": "embed", "expect": {}}, in_dir, out_dir, batch_size=4)

    assert seen == ["head tail\u2028more"]
    assert not any("\x00" in t for t in seen)


def test_the_worker_names_the_chunk_when_a_payload_cannot_be_decoded(tmp_path):
    in_dir, out_dir = tmp_path / "in", tmp_path / "out"
    in_dir.mkdir()
    out_dir.mkdir()
    (in_dir / EMBED_INPUT_NAME).write_bytes(
        b'{"chunk_id": "CHNK-broken", "seq": 0, "text": "unterminated\n'
    )

    with pytest.raises(ChunkPayloadError) as exc:
        _run_embed(StubEmbedBackend(), {"job_id": "J", "stage": "embed", "expect": {}}, in_dir, out_dir, batch_size=4)

    assert "CHNK-broken" in str(exc.value)
