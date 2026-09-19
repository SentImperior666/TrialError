"""Lane FB-7 item 10: every behaviour the lane shipped is findable.

Deliberately narrow, like ``tests/test_docs_small_items.py``: each assertion
pins that a claim is present where the brief names it, not its prose. A
behaviour nobody can find is a behaviour that surprises somebody later, and
this lane is almost entirely made of distinctions -- which percentile
convention, which of ``kind`` and ``class``, which round a verdict belongs
to, which session a booking chose -- that an operator gets wrong precisely
when nothing written down says otherwise.
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


def _group_row(group: str) -> str:
    text = _read("OPERATOR_GUIDE.md")
    rows = [line for line in text.splitlines() if line.startswith(f"| `{group}` |")]
    assert rows, f"OPERATOR_GUIDE.md's command table has no `{group}` row"
    return rows[0]


# ---------------------------------------------------------------------------
# item 1 -- the numpy fast path
# ---------------------------------------------------------------------------


def test_the_fast_extra_and_its_knob_are_documented_with_both_values():
    block = _setup_section("## 3i.")
    assert '".[fast]"' in block or "[fast]" in block
    assert "numpy_fastpath" in block
    assert '"auto"' in block and '"off"' in block
    assert "TRIALERROR_NUMPY_FASTPATH" in block


def test_the_doc_says_numpy_is_optional_and_the_answer_does_not_change():
    block = _setup_section("## 3i.")
    assert "not a dependency" in block
    assert "byte for byte" in block
    assert "NARROW" in block


def test_the_measured_speed_up_and_the_memory_bound_are_stated():
    block = _setup_section("## 3i.")
    assert "20,000 × 256" in block
    assert "17×" in block
    assert "20,000 rows" in block and "32 MiB" in block


def test_the_17x_figure_names_the_path_that_takes_it(tmp_path):
    """Fix pass V-4: ``fetch_vector_matrix`` -- the frombuffer route the
    17x figure is measured on -- had no production caller at all, so the
    number described a path nothing in the tree took. It has one now
    (``--baseline``), and the doc says which scans get which number rather
    than leaving a reader to assume the headline applies to all of them."""
    block = _setup_section("## 3i.")
    assert "--baseline" in block
    assert "3×" in block

    source = (_DOCS.parent / "trialerror").rglob("*.py")
    callers = sorted(
        path.relative_to(_DOCS.parent).as_posix()
        for path in source
        if "fetch_vector_matrix" in path.read_text(encoding="utf-8")
        and path.name not in ("vecsearch.py",)
    )
    assert callers, "fetch_vector_matrix still has no caller inside trialerror/"


def test_pyproject_carries_the_extra_and_no_core_numpy_dependency():
    text = (_DOCS.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert 'fast = ["numpy' in text
    dependencies = text.split("dependencies = [", 1)[1].split("]", 1)[0]
    assert "numpy" not in dependencies


# ---------------------------------------------------------------------------
# item 2 -- --baseline
# ---------------------------------------------------------------------------


def test_the_baseline_pass_is_documented_with_its_percentile_method():
    block = _guide_section("### Reading a round's baseline")
    assert "--baseline" in block
    assert "index-round(p*(n-1))" in block
    assert "percentile_method" in block
    # Fix pass V-6: the guide says why the borrowed name was wrong.
    assert "ceil(p" in block
    assert "--status" in block and "--where" in block
    assert "Read-only" in block
    assert "no similarity floor" in block


def test_the_provenance_spelling_is_explained_rather_than_just_required():
    block = _guide_section("### Reading a round's baseline")
    assert "provenance.<key>" in block
    assert "schema column first" in block


# ---------------------------------------------------------------------------
# item 3 -- the calibration card's new tables
# ---------------------------------------------------------------------------


def test_the_cards_confusion_disagreements_and_by_expected_are_documented():
    block = _guide_section("### Calibration — measuring the instrument before the round")
    for field in ("`confusion`", "`disagreements`", "`by_expected`", "`by_class`"):
        assert field in block, field
    assert "n_unpaired" in block
    assert "unscreenable" in block


# ---------------------------------------------------------------------------
# item 4 + 5 -- kind, class, batch
# ---------------------------------------------------------------------------


def test_kind_class_and_batch_are_kept_apart_in_the_plant_model():
    block = _guide_section("### The plant model")
    assert "`class`" in block and "`batch`" in block
    assert "40 characters" in block
    assert "never shown to a judge" in block
    assert "plants_injected" in block
    for kind in ("area", "paraphrase", "inventory", "custom"):
        assert kind in block


def test_the_plants_file_row_lists_the_two_new_keys():
    text = _read("OPERATOR_GUIDE.md")
    rows = [
        line for line in text.splitlines()
        if line.startswith("| the round's plants |") and "--plants-file FILE" in line
    ]
    assert rows, "the four-files table has no plants row"
    assert "class?" in rows[0] and "batch?" in rows[0]


def test_user_setup_warns_that_class_is_not_kind():
    block = _setup_section("## 3g.")
    assert "`class`" in block and "`batch`" in block
    assert "NOT `kind`" in block


# ---------------------------------------------------------------------------
# item 6 -- budget book's defaults
# ---------------------------------------------------------------------------


def test_budget_book_defaults_are_documented_including_both_refusals():
    row = _group_row("budget")
    assert "resolved_from" in row
    assert "multiple_open_sessions" in row
    assert "program_id_unresolved" in row
    assert "Explicit flags always win" in row
    assert "book_launch" in row


# ---------------------------------------------------------------------------
# item 7 -- slice-distances
# ---------------------------------------------------------------------------


def test_slice_distances_is_in_the_group_row_and_has_its_own_section():
    assert "`slice-distances`" in _group_row("lens")
    block = _guide_section("### Evaluating a rule-defined pick")
    assert "canonical_sha256" in block
    assert "ties → the lower id" in block
    assert "unvectorized" in block
    assert "Read-only" in block


# ---------------------------------------------------------------------------
# item 8 -- the two deferred FB-6 items
# ---------------------------------------------------------------------------


def test_intake_time_embedding_and_its_opt_out_are_documented():
    block = _guide_section("### The record schema, enforced at intake")
    assert "--no-embed" in block
    assert "idea_vectors" in block
    assert "warns" in block


def test_extra_on_a_round_s_own_records_is_documented_in_both_docs():
    block = _guide_section("### The record schema, enforced at intake")
    assert "`extra`" in block
    assert "record.extra_text" in block
    setup = _setup_section("## 3g.")
    assert "intake records take the same key" in setup


# ---------------------------------------------------------------------------
# item 9 -- the round-scoped duplicate check
# ---------------------------------------------------------------------------


def test_the_duplicate_rule_says_it_is_scoped_to_the_round_and_batch():
    block = _guide_section("### Calibration — measuring the instrument before the round")
    assert "scoped to the round and the batch" in block
    assert "never blocks and is never superseded" in block
    assert "round_id" in block
    # The NULL case is the one that could have quietly loosened the rule.
    assert "UNKNOWN" in block


def test_the_records_half_of_the_rule_says_the_batch_is_not_in_its_key():
    """Fix pass V-3. Two recorders, two grains, and the guide has to say
    which is which: a calibration's subjects are plant ids (batch in the
    key), a round's own records are idea ids (batch NOT in the key, or one
    round could record two contradicting labels for one idea)."""
    block = _guide_section("### Calibration — measuring the instrument before the round")
    assert "`--record-verdicts` is scoped to the round and NOT to the batch" in block
    assert "one submission per idea per judge" in block
    assert "two judged batches" in block


# ---------------------------------------------------------------------------
# fix pass V-9 -- the documented key is the implemented key
# ---------------------------------------------------------------------------


def test_the_documented_duplicate_key_is_the_one_the_code_filters_on():
    """The guide listed `issued_by` in the lookup key. The guard never
    selected, compared or returned `issued_by_launch`, which makes the
    implemented rule STRICTER than the documented one -- so there was no
    correctness hole, only a sentence that was not true of the code. Keying
    on the launch would let one round record the same idea twice by booking
    a second launch, which is the hole the rule exists to close, so the
    sentence went rather than the behaviour."""
    import inspect

    from trialerror.lens.novelty import _existing_novelty_verdicts

    source = inspect.getsource(_existing_novelty_verdicts)
    assert "issued_by" not in source

    guide = _read("OPERATOR_GUIDE.md")
    key_line = [ln for ln in guide.splitlines() if "is keyed by `(round_id" in ln]
    assert key_line, "the guide no longer states the lookup key"
    assert "issued_by" not in key_line[0]
    for column in ("round_id", "batch_id", "subject_id", "procedure_version"):
        assert column in key_line[0]
        assert column in source

    schema = (_DOCS.parent / "trialerror" / "stores" / "schema" / "knowledge.py").read_text(
        encoding="utf-8"
    )
    assert "``issued_by`` is deliberately NOT in that key" in schema
