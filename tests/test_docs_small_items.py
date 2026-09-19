"""Lane FB-6 item 9: the claims an operator has to be able to find.

Deliberately narrow, like ``tests/test_docs_judged_instrument.py``: each
assertion pins that a claim is present at the place the brief names, not its
prose. Every knob and every changed behaviour the lane shipped is named in a
doc here, because a behaviour nobody can find is a behaviour that surprises
somebody later -- and the distinctions these sections carry (the batch is the
authority, the plants' baseline, unscreenable is about the record, the
full-text pass does not advance the status) are exactly the ones a round or
an operator gets wrong when nothing written down says otherwise.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_DOCS = Path(__file__).resolve().parents[1] / "docs"


def _read(name: str) -> str:
    path = _DOCS / name
    if not path.is_file():
        pytest.skip(f"{name} not present in this tree")
    return path.read_text(encoding="utf-8")


def _guide_section(heading: str) -> str:
    text = _read("OPERATOR_GUIDE.md")
    assert heading in text, f"OPERATOR_GUIDE.md has no {heading!r} section"
    after = text.split(heading, 1)[1]
    return after.split("\n### ", 1)[0].split("\n## ", 1)[0]


def _setup_section(heading: str) -> str:
    text = _read("USER_SETUP.md")
    assert heading in text, f"USER_SETUP.md has no {heading!r} section"
    after = text.split(heading, 1)[1]
    return after.split("\n### ", 1)[0].split("\n## ", 1)[0]


# ---------------------------------------------------------------------------
# item 1 -- the idea-vector cache
# ---------------------------------------------------------------------------


def test_the_archive_cache_and_its_rebuild_flag_are_documented():
    block = _guide_section("### The archive round")
    assert "vec_ideas" in block
    assert "--reembed-archive" in block
    assert "model_key" in block and "SHA-256" in block
    assert "mixing two" in block or "two\nvector spaces" in block


def test_user_setup_names_the_rebuild_flag_too():
    block = _setup_section("## 3g. Optional: the judged novelty screen as a configurable instrument")
    assert "--reembed-archive" in block
    assert "vec_ideas" in block


# ---------------------------------------------------------------------------
# item 2 -- stdout
# ---------------------------------------------------------------------------


def test_the_stdout_contract_is_written_down_where_the_tokenizer_is():
    text = _read("OPERATOR_GUIDE.md")
    assert "n_ctx_seq" in text
    assert "exactly one JSON object" in text
    assert "stderr" in text


# ---------------------------------------------------------------------------
# item 3 -- the batch is the authority
# ---------------------------------------------------------------------------


def test_the_instrument_section_says_the_batch_is_the_authority():
    block = _guide_section("### The instrument — what a round declares")
    assert "--record-calibration" in block
    assert "judged_sets_disagree" in block
    assert "needs no `--judged-sets`" in block


def test_user_setup_says_the_same():
    block = _setup_section("## 3g. Optional: the judged novelty screen as a configurable instrument")
    assert "--batch-id" in block
    assert "authority" in block


# ---------------------------------------------------------------------------
# item 4 -- unscreenable
# ---------------------------------------------------------------------------


def test_unscreenable_is_documented_as_an_answer_for_every_set():
    block = _guide_section("### Seed work behind an `unscreenable`")
    assert "EVERY declared set" in block
    assert "non-catch" in block
    assert "label_canonical = unscreenable" in block
    # the FB-5 rule it does NOT overturn
    assert "still has to offer the judge that\nspelling" in block or (
        "still has to offer the judge that spelling" in block
    )


def test_user_setup_carries_the_unscreenable_rule():
    block = _setup_section("## 3g. Optional: the judged novelty screen as a configurable instrument")
    assert "every declared" in block


# ---------------------------------------------------------------------------
# item 5 -- the baseline's population
# ---------------------------------------------------------------------------


def test_the_calibration_card_says_which_population_the_baseline_is_over():
    block = _guide_section("### Calibration — measuring the instrument before the round")
    assert "`over`" in block
    assert "`plants` in calibration mode" in block
    assert "n_with_neighbour" in block
    assert "`warnings`" in block


# ---------------------------------------------------------------------------
# item 6 -- the lenient shapes, side by side
# ---------------------------------------------------------------------------


def test_the_four_files_are_documented_side_by_side():
    block = _guide_section("### The four files a round writes by hand, side by side")
    for flag in ("--records", "--plants-file", "--record-verdicts", "--pair-ratings"):
        assert flag in block, f"the table does not name {flag}"
    assert "extra" in block
    assert "label_archive" in block and "label_inventory" in block and "label_corpus" in block
    assert "lenient about what a person ADDS" in block.replace("**", "")


def test_the_plants_file_table_names_extra():
    block = _guide_section("### The instrument — what a round declares")
    assert "`extra`" in block
    assert "record.extra_text" in block


def test_the_intake_schema_says_archived_may_omit_its_probe():
    block = _guide_section("### The record schema, enforced at intake")
    assert "archived" in block
    assert "`null`" in block
    assert "still refused" in block


# ---------------------------------------------------------------------------
# items 7 and 8 -- ingest
# ---------------------------------------------------------------------------


def test_the_route_keys_and_the_no_orphan_row_rule_are_documented():
    block = _guide_section("### `ingest add` — the route is checked before anything is written")
    assert "text/markdown" in block
    assert "before the document row is inserted" in block
    for key in ("`djvu`", "`epub`", "`html`", "`image`", "`md`", "`pdf-scan`", "`pdf-text`"):
        assert key in block, f"the route key list does not name {key}"
    assert ".md" in block


def test_the_fulltext_before_embed_behaviour_is_documented_in_both_docs():
    block = _guide_section("### Full-text search does not wait for the GPU")
    assert "[ingest] fulltext_before_embed" in block
    assert "JOB-ingest-<doc>-index-fulltext" in block
    assert "does not advance `document.status`" in block
    assert "fulltext_index_stale" in block

    setup = _setup_section("### `[ingest] fulltext_before_embed` — search before the GPU run")
    assert "fulltext_before_embed = true" in setup
    assert "reindex-fulltext" in setup
    assert "fulltext_index_stale" in setup
