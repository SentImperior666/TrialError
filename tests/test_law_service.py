"""``trialerror.law.service`` — the public API. Covers the M4 acceptance
criteria at the unit level (design Section 12, M4 row):

    | Acceptance criterion                          | Test(s) here |
    |------------------------------------------------|--------------|
    | append w/o digest impossible (single API)       | test_append_ruling_creates_exactly_one_ruling_and_one_digest_row, test_append_ruling_rolls_back_ruling_insert_when_supersedes_target_missing |
    | stale pin fails `law verify`                    | test_verify_pin_stale_after_a_second_append |
    | diff-foreign lists post-pin rows                | test_diff_foreign_lists_rows_appended_after_the_pin |

Chain tampering is covered in ``test_law_chain.py`` (unit) and
``test_m4_acceptance.py`` (through ``verify_pin``).
"""

from __future__ import annotations

import json

import pytest

from trialerror.law.service import (
    RENDERED_PATH,
    append_ruling,
    current_pin,
    diff_foreign,
    format_pin,
    get_current_digest,
    lookup_rulings,
    parse_pin,
    render_current_digest_to_disk,
    verify_pin,
)
from trialerror.stores.errors import ValidationError


# ---- append_ruling: the atomic single API -------------------------------


def test_append_ruling_creates_exactly_one_ruling_and_one_digest_row(store):
    result = append_ruling(store, summary="first ruling")

    assert result.ruling_id == "C-0001"
    assert result.digest_version == "v1"
    n_rulings = store.ops.execute("SELECT COUNT(*) FROM ruling").fetchone()[0]
    n_digests = store.ops.execute("SELECT COUNT(*) FROM law_digest").fetchone()[0]
    assert n_rulings == 1
    assert n_digests == 1


def test_append_ruling_sequential_ids_and_versions_increment(store):
    r1 = append_ruling(store, summary="one")
    r2 = append_ruling(store, summary="two")
    r3 = append_ruling(store, summary="three")
    assert [r1.ruling_id, r2.ruling_id, r3.ruling_id] == ["C-0001", "C-0002", "C-0003"]
    assert [r1.digest_version, r2.digest_version, r3.digest_version] == ["v1", "v2", "v3"]


def test_append_ruling_requires_nonempty_summary(store):
    with pytest.raises(ValueError):
        append_ruling(store, summary="")
    with pytest.raises(ValueError):
        append_ruling(store, summary="   ")


def test_append_ruling_rolls_back_ruling_insert_when_supersedes_target_missing(store):
    """Proves real transactional atomicity, not just "one function did two
    things": a mid-transaction failure (bad `supersedes`) leaves ZERO
    trace, never a ruling with no matching digest bump."""
    with pytest.raises(ValidationError):
        append_ruling(store, summary="x", supersedes="C-9999")
    n_rulings = store.ops.execute("SELECT COUNT(*) FROM ruling").fetchone()[0]
    n_digests = store.ops.execute("SELECT COUNT(*) FROM law_digest").fetchone()[0]
    assert n_rulings == 0
    assert n_digests == 0


def test_append_ruling_supersedes_flips_status(store):
    r1 = append_ruling(store, summary="original rule")
    r2 = append_ruling(store, summary="revised rule", supersedes=r1.ruling_id, supersedes_note="tightened scope")

    old = store.ops.execute("SELECT status FROM ruling WHERE ruling_id = ?", (r1.ruling_id,)).fetchone()
    new = store.ops.execute("SELECT status, supersedes, supersedes_note FROM ruling WHERE ruling_id = ?", (r2.ruling_id,)).fetchone()
    assert old["status"] == "superseded"
    assert new["status"] == "active"
    assert new["supersedes"] == r1.ruling_id
    assert new["supersedes_note"] == "tightened scope"

    # the digest only lists the still-active ruling
    active_ids = [lr["ruling_id"] for lr in lookup_rulings(store, status="active")]
    assert active_ids == [r2.ruling_id]


def test_append_ruling_supersedes_note_without_supersedes_id(store):
    """F20(c): real supersession targets are often prose, not a ruling FK
    (e.g. "all '18-game' corpus figures")."""
    r = append_ruling(store, summary="new figures stand", supersedes_note="all '18-game' corpus figures")
    row = store.ops.execute("SELECT supersedes, supersedes_note FROM ruling WHERE ruling_id = ?", (r.ruling_id,)).fetchone()
    assert row["supersedes"] is None
    assert row["supersedes_note"] == "all '18-game' corpus figures"


def test_append_ruling_verbatim_quote_is_nullable(store):
    """F20(c): real ledgers hold summary-only entries with no verbatim
    quote (e.g. origin-project's C-0005)."""
    r = append_ruling(store, summary="summary-only entry")
    row = store.ops.execute("SELECT verbatim_quote FROM ruling WHERE ruling_id = ?", (r.ruling_id,)).fetchone()
    assert row["verbatim_quote"] is None


def test_append_ruling_standing_clauses_and_domains_round_trip(store):
    append_ruling(
        store,
        summary="GPU-only OCR",
        standing_clauses=["never run OCR on CPU", "batch-chunked"],
        domains=["ingest", "ocr"],
    )
    rows = lookup_rulings(store)
    assert json.loads(rows[0]["standing_clauses"]) == ["never run OCR on CPU", "batch-chunked"]
    assert json.loads(rows[0]["domains"]) == ["ingest", "ocr"]


def test_append_ruling_writes_the_rendered_file(store):
    from trialerror.law.digest import digest_sha256

    result = append_ruling(store, summary="renders to disk")
    path = store.program_root / RENDERED_PATH
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert result.ruling_id in text
    assert digest_sha256(text) == result.digest["content_sha256"]


def test_append_ruling_ledger_sha256_after_is_a_64_char_hex_string(store):
    r = append_ruling(store, summary="hash shape")
    assert len(r.ledger_sha256_after) == 64
    int(r.ledger_sha256_after, 16)  # does not raise


# ---- lookup_rulings -------------------------------------------------------


def test_lookup_rulings_filters(store):
    append_ruling(store, summary="alpha rule", domains=["budget"])
    append_ruling(store, summary="beta rule", domains=["law"])
    by_domain = lookup_rulings(store, domain="law")
    assert [r["summary"] for r in by_domain] == ["beta rule"]

    by_query = lookup_rulings(store, query="alpha")
    assert [r["summary"] for r in by_query] == ["alpha rule"]

    by_id = lookup_rulings(store, ruling_id="C-0001")
    assert [r["ruling_id"] for r in by_id] == ["C-0001"]


def test_lookup_rulings_status_filter_after_supersede(store):
    r1 = append_ruling(store, summary="v1")
    append_ruling(store, summary="v2", supersedes=r1.ruling_id)
    active = lookup_rulings(store, status="active")
    superseded = lookup_rulings(store, status="superseded")
    assert [r["ruling_id"] for r in superseded] == [r1.ruling_id]
    assert len(active) == 1


# ---- digest read/render ----------------------------------------------------


def test_get_current_digest_none_before_any_append(store):
    assert get_current_digest(store) is None
    assert current_pin(store) is None


def test_get_current_digest_tracks_latest_version(store):
    append_ruling(store, summary="one")
    r2 = append_ruling(store, summary="two")
    digest = get_current_digest(store)
    assert digest["version"] == r2.digest_version == "v2"


def test_render_current_digest_to_disk_raises_before_any_append(store):
    with pytest.raises(ValueError):
        render_current_digest_to_disk(store)


def test_render_current_digest_to_disk_recreates_a_deleted_file(store):
    result = append_ruling(store, summary="recoverable")
    path = store.program_root / RENDERED_PATH
    path.unlink()
    assert not path.exists()

    rendered = render_current_digest_to_disk(store)
    assert path.is_file()
    assert rendered.matches_stored_hash is True
    assert rendered.content_sha256 == result.digest["content_sha256"]


# ---- [paths].law_digest_path knob (the import-design notes (internal, not in this export) Sec 5 knob #2) -----


def test_append_ruling_default_config_matches_unconfigured_behavior(store):
    """``config={}`` (a program with a trialerror.toml but no ``[paths]``
    table) must render byte-identically to ``config=None`` -- zero
    behavior change for a program that hasn't touched this knob."""
    result = append_ruling(store, summary="empty config dict", config={})
    assert result.rendered_path == RENDERED_PATH
    assert (store.program_root / RENDERED_PATH).is_file()


def test_append_ruling_respects_configured_relative_law_digest_path(store):
    config = {"paths": {"law_digest_path": "governance/DIGEST.md"}}
    result = append_ruling(store, summary="relocated digest", config=config)

    assert result.rendered_path == "governance/DIGEST.md"
    relocated = store.program_root / "governance" / "DIGEST.md"
    assert relocated.is_file()
    assert result.ruling_id in relocated.read_text(encoding="utf-8")
    assert not (store.program_root / "law").exists()


def test_append_ruling_respects_configured_absolute_law_digest_path(store, tmp_path):
    external = tmp_path / "external-law" / "DIGEST.md"
    config = {"paths": {"law_digest_path": str(external)}}
    result = append_ruling(store, summary="absolute digest path", config=config)

    assert result.rendered_path == str(external)
    assert external.is_file()
    assert not (store.program_root / "law").exists()


def test_render_current_digest_to_disk_reads_back_the_configured_path(store):
    """No ``config`` argument at all on the read side -- the resolved path
    from ``append_ruling`` time is what's stored in ``law_digest.
    rendered_path``, so a later re-render needs no config of its own."""
    config = {"paths": {"law_digest_path": "governance/DIGEST.md"}}
    result = append_ruling(store, summary="recoverable, relocated", config=config)
    relocated = store.program_root / "governance" / "DIGEST.md"
    relocated.unlink()
    assert not relocated.exists()

    rendered = render_current_digest_to_disk(store)
    assert relocated.is_file()
    assert rendered.matches_stored_hash is True
    assert rendered.content_sha256 == result.digest["content_sha256"]


# ---- pin format -------------------------------------------------------------


def test_format_pin_and_parse_pin_round_trip():
    pin = format_pin("v12", "2026-08-29T03:20:11.123Z")
    assert pin == "v12@2026-08-29"
    version, date = parse_pin(pin)
    assert version == "v12"
    assert date == "2026-08-29"


def test_parse_pin_rejects_malformed_input():
    for bad in ["v12", "2026-08-29", "v12@2026-8-29", "", None]:
        with pytest.raises(ValueError):
            parse_pin(bad)


# ---- verify_pin: freshness ---------------------------------------------------


def test_verify_pin_no_digest_yet(store):
    result = verify_pin(store, "v1@2026-01-01")
    assert result.valid is False
    assert result.pin_stale is True
    assert "no_law_digest_exists" in result.reason


def test_verify_pin_valid_immediately_after_append(store):
    result = append_ruling(store, summary="fresh")
    verify = verify_pin(store, result.pin)
    assert verify.valid is True
    assert verify.pin_stale is False
    assert verify.chain_ok is True
    assert verify.current_pin == result.pin


def test_verify_pin_stale_after_a_second_append(store):
    """The core spawn-gate refusal scenario: a booked pin goes stale the
    moment another append lands."""
    first = append_ruling(store, summary="one", ts="2026-01-01T00:00:00.000Z")
    append_ruling(store, summary="two", ts="2026-01-02T00:00:00.000Z")

    stale = verify_pin(store, first.pin)
    assert stale.valid is False
    assert stale.pin_stale is True
    assert stale.current_pin != first.pin


def test_verify_pin_rejects_malformed_pin(store):
    append_ruling(store, summary="x")
    result = verify_pin(store, "not-a-pin")
    assert result.valid is False
    assert "malformed_pin" in result.reason


def test_verify_pin_rejects_missing_pin(store):
    append_ruling(store, summary="x")
    result = verify_pin(store, None)
    assert result.valid is False
    assert result.reason == "no_pin_given"


def test_verify_pin_catches_tampered_chain_even_with_a_fresh_pin(store):
    """Freshness alone is not enough -- the SAME pin can be "current" while
    the ledger underneath it has been tampered with (Design Sec 12: the
    build must catch stale-pin AND tampered-chain, both via `law verify`,
    since that's the one function hooks call)."""
    result = append_ruling(store, summary="one")
    with store.ops:
        store.ops.execute(
            "UPDATE ruling SET summary = ? WHERE ruling_id = ?", ("TAMPERED", result.ruling_id)
        )
    verify = verify_pin(store, result.pin)
    assert verify.pin_stale is False  # the digest row itself wasn't touched
    assert verify.chain_ok is False
    assert verify.valid is False


# ---- diff_foreign -------------------------------------------------------------


def test_diff_foreign_lists_rows_appended_after_the_pin(store):
    r1 = append_ruling(store, summary="one", ts="2026-01-01T00:00:00.000Z")
    r2 = append_ruling(store, summary="two", ts="2026-01-02T00:00:00.000Z")
    r3 = append_ruling(store, summary="three", ts="2026-01-03T00:00:00.000Z")

    foreign = diff_foreign(store, r1.pin)
    assert [r["ruling_id"] for r in foreign] == [r2.ruling_id, r3.ruling_id]

    foreign_after_latest = diff_foreign(store, r3.pin)
    assert foreign_after_latest == []


def test_diff_foreign_unknown_pin_version_raises(store):
    append_ruling(store, summary="one")
    with pytest.raises(ValueError):
        diff_foreign(store, "v999@2026-01-01")
