"""``trialerror.law.digest`` — pure rendering. ``render_digest`` must be a
deterministic function of its inputs (that's what makes ``content_sha256``
a meaningful lockstep signal downstream in ``trialerror.law.service`` /
``trialerror.law.checks``)."""

from __future__ import annotations

from trialerror.law.digest import digest_sha256, render_digest


def _ruling(**overrides) -> dict:
    row = {
        "ruling_id": "C-0001",
        "ts": "2026-08-29T00:00:00.000Z",
        "verbatim_quote": None,
        "summary": "test ruling",
        "standing_clauses": "[]",
        "domains": "[]",
        "supersedes": None,
        "supersedes_note": None,
        "status": "active",
    }
    row.update(overrides)
    return row


def test_render_digest_is_deterministic():
    rulings = [_ruling()]
    a = render_digest(rulings, version="v1", generated_ts="2026-08-29T00:00:00.000Z")
    b = render_digest(rulings, version="v1", generated_ts="2026-08-29T00:00:00.000Z")
    assert a == b
    assert digest_sha256(a) == digest_sha256(b)


def test_render_digest_empty_ledger_still_renders():
    text = render_digest([], version="v1", generated_ts="2026-08-29T00:00:00.000Z")
    assert "v1" in text
    assert "no active rulings" in text


def test_render_digest_includes_ruling_content():
    rulings = [
        _ruling(
            ruling_id="C-0007",
            summary="GPU-only OCR",
            standing_clauses='["never run OCR on CPU"]',
            domains='["ingest", "ocr"]',
            verbatim_quote="use the GPU",
        )
    ]
    text = render_digest(rulings, version="v3", generated_ts="2026-08-29T00:00:00.000Z")
    assert "C-0007" in text
    assert "GPU-only OCR" in text
    assert "never run OCR on CPU" in text
    assert "ingest" in text and "ocr" in text
    assert "use the GPU" in text


def test_render_digest_shows_supersedes_and_note():
    rulings = [_ruling(supersedes="C-0002", supersedes_note="all '18-game' corpus figures")]
    text = render_digest(rulings, version="v2", generated_ts="2026-08-29T00:00:00.000Z")
    assert "C-0002" in text
    assert "18-game" in text


def test_render_digest_content_changes_when_ruling_set_changes():
    base = render_digest([_ruling()], version="v1", generated_ts="2026-08-29T00:00:00.000Z")
    changed = render_digest([_ruling(summary="different")], version="v1", generated_ts="2026-08-29T00:00:00.000Z")
    assert digest_sha256(base) != digest_sha256(changed)


def test_render_digest_does_not_embed_its_own_hash():
    text = render_digest([_ruling()], version="v1", generated_ts="2026-08-29T00:00:00.000Z")
    computed = digest_sha256(text)
    assert computed not in text
