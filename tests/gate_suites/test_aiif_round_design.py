"""Lane R1-H: the ``aiif_round`` suite reads the round's own design.

A round declares its design in its prereg params -- ``design`` (with
``control_count``), ``rooms``, ``report_p_values`` -- and the four checks
that read those keys judge the round it declared rather than the one design
the suite was first written for. A fourth status, ``not_applicable``, exists
for exactly those declarations and for nothing else.

**Legacy is absolute.** A subject that declares none of the keys is judged
as it was before they existed, check for check and message for message. The
golden fixture (``tests/fixtures/gate_suites/aiif_round_legacy_golden.json``)
was generated from the unmodified suite before this lane touched it; the
first tests below require every entry to come out byte-identical.

No store is opened except in the last test, which writes a real gate row to
prove what ``not_applicable`` does to ``reproduction_status``.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from trialerror.artifacts.gates import apply_union
from trialerror.eval.gate_suites import (
    AIIF_ROUND_SUITE_ID,
    CHECK_FAIL,
    CHECK_PASS,
    NOT_APPLICABLE,
    REPORTED_P_VALUE_TERMS,
    ROUND_DESIGNS,
    SIGNIFICANCE_TERMS,
    MetricResult,
    admission_order_hash_matches,
    check_status,
    control_arm_present,
    get_suite,
    no_significance_language,
    per_arm_n_disclosed,
    run_gate_suite,
    run_gate_suite_for_gate,
)
from trialerror.stores.writer import get

from tests._verify_fixtures import bootstrap_launch
from tests.gate_suites.test_runner import _open_gated_artifact

_GOLDEN_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "gate_suites" / "aiif_round_legacy_golden.json"
_GOLDEN = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))

#: The four checks that read a declaration. Every other check must come out
#: of a declared subject exactly as it comes out of the same subject without
#: the declaration.
_DECLARATION_READERS = {
    "control_arm_present", "per_arm_n_disclosed", "admission_order_hash_matches", "no_significance_language",
}


def _apply_patch(subject: dict, patch: list) -> dict:
    """The golden's patch ops: ``["set", path, value]``, ``["del", path]``,
    ``["append", path, value]``; a path is a list of keys and indices."""
    subject = copy.deepcopy(subject)
    for op in patch:
        kind, path = op[0], op[1]
        parent = subject
        for key in path[:-1]:
            parent = parent[key]
        if kind == "set":
            parent[path[-1]] = copy.deepcopy(op[2])
        elif kind == "del":
            del parent[path[-1]]
        elif kind == "append":
            parent[path[-1]].append(copy.deepcopy(op[2]))
        else:  # pragma: no cover -- a malformed fixture, not a suite result
            raise ValueError(f"unknown patch op {kind!r}")
    return subject


def _run_all(subject: dict) -> dict[str, dict]:
    """Every check's ``to_dict()``, or ``{"raises": <type>}`` -- the shape the
    golden recorded, raising included."""
    suite = get_suite(AIIF_ROUND_SUITE_ID)
    out: dict[str, dict] = {}
    for name in sorted(suite.checks):
        try:
            out[name] = suite.checks[name](copy.deepcopy(subject)).to_dict()
        except Exception as exc:  # noqa: BLE001 -- the golden records what legacy did
            out[name] = {"raises": type(exc).__name__}
    return out


def _golden_cases() -> list[tuple[str, dict, dict]]:
    base = _GOLDEN["base_subject"]
    cases = [("complete_round", base, _GOLDEN["base_checks"])]
    for case in _GOLDEN["cases"]:
        if "patch" in case:
            cases.append((case["name"], _apply_patch(base, case["patch"]), {**_GOLDEN["base_checks"], **case["checks_changed"]}))
        else:
            cases.append((case["name"], case["subject"], case["checks"]))
    return cases


_CASES = _golden_cases()


def _round(**params) -> dict:
    """The golden's complete round (every check passes) with ``params``
    merged into its prereg params. A test builds the design it needs from
    this, so a failure says which bar broke rather than which fixture was
    mis-assembled."""
    subject = copy.deepcopy(_GOLDEN["base_subject"])
    subject["prereg"]["params"].update(params)
    return subject


def _paired_round(**params) -> dict:
    """A complete paired round: two standard lenses each holding a card half
    and a plain half, the buster outside the pairing (its one launch is
    unlabelled), no control seat, and the plain cells reported as the
    control arm."""
    subject = _round(design="paired", **params)
    subject["roster"] = [r for r in subject["roster"] if r["seat"] != "control"]
    subject["lens_launches"] = [
        {"lens_name": "lens_1", "launch_id": "LNCH-1C", "phase": "card"},
        {"lens_name": "lens_1", "launch_id": "LNCH-1P", "phase": "plain"},
        {"lens_name": "lens_2", "launch_id": "LNCH-2C", "phase": "card"},
        {"lens_name": "lens_2", "launch_id": "LNCH-2P", "phase": "plain"},
        {"lens_name": "lens_3", "launch_id": "LNCH-3", "phase": None},
    ]
    subject["outcomes"] = [
        {"cell": "arm=card", "arm": "card", "n": 6},
        {"cell": "arm=plain", "arm": "plain", "n": 6},
    ]
    return subject


def _control_seats_round(count: int = 2) -> dict:
    subject = _round(design="control_seats", control_count=count)
    for i in range(count - 1):
        subject["roster"].append({"lens_name": f"lens_c{i}", "seat": "control", "recipe_cards": []})
        subject["outcomes"].append({"cell": f"control lens_c{i}", "seat": "control", "n": 1})
    return subject


def _none_round() -> dict:
    subject = _round(design="none")
    subject["roster"] = [r for r in subject["roster"] if r["seat"] != "control"]
    subject["outcomes"] = [r for r in subject["outcomes"] if r.get("seat") != "control"]
    return subject


# ---------------------------------------------------------------------------
# the status itself
# ---------------------------------------------------------------------------


def test_a_not_applicable_result_is_never_a_pass():
    with pytest.raises(ValueError, match="not a pass"):
        MetricResult(name="x", passed=True, score=None, message="m", not_applicable=True)
    result = MetricResult(name="x", passed=False, score=None, message="m", not_applicable=True)
    assert result.status == NOT_APPLICABLE
    assert result.to_dict() == {"name": "x", "passed": False, "score": None, "message": "m", "status": NOT_APPLICABLE}
    assert check_status(result.to_dict()) == NOT_APPLICABLE


def test_a_pass_or_a_fail_serialises_exactly_as_it_always_has():
    """No ``status`` key on a pass or a fail: a legacy subject's per-check
    entries -- and the reproduction_ref written from them -- stay
    byte-identical. ``check_status`` reads both shapes."""
    passed = MetricResult(name="x", passed=True, score=1.0, message="m")
    failed = MetricResult(name="x", passed=False, score=None, message="m")
    assert passed.to_dict() == {"name": "x", "passed": True, "score": 1.0, "message": "m"}
    assert failed.to_dict() == {"name": "x", "passed": False, "score": None, "message": "m"}
    assert (passed.status, failed.status) == (CHECK_PASS, CHECK_FAIL)
    assert (check_status(passed.to_dict()), check_status(failed.to_dict())) == (CHECK_PASS, CHECK_FAIL)


# ---------------------------------------------------------------------------
# probe (a): legacy is absolute
# ---------------------------------------------------------------------------


def test_the_golden_was_generated_before_this_lane():
    assert _GOLDEN["generated_from_commit"] == "a1b6140"
    assert len(_CASES) == 26
    assert "round_shaped_undeclared" in {name for name, _, _ in _CASES}


@pytest.mark.parametrize(("name", "subject", "expected"), _CASES, ids=[name for name, _, _ in _CASES])
def test_a_subject_that_declares_nothing_is_judged_exactly_as_before(name, subject, expected):
    """Every check's pass/fail, score and message -- or the exception it
    raised -- byte-identical to the unmodified suite's."""
    assert _run_all(subject) == expected


@pytest.mark.parametrize(("name", "subject", "expected"), _CASES, ids=[name for name, _, _ in _CASES])
def test_no_check_is_not_applicable_without_a_declaration(name, subject, expected):
    statuses = {k: v.get("status") for k, v in _run_all(subject).items() if "raises" not in v}
    assert NOT_APPLICABLE not in statuses.values()


def test_the_round_shaped_legacy_subject_still_fails_the_four_bars_it_failed():
    """A round that ran the paired contrast with no control seat and no
    rooms, and states its randomization p, fails four checks by construction
    when it declares nothing -- and must keep failing them, with the same
    words, until it does declare."""
    (_, subject, expected), = [c for c in _CASES if c[0] == "round_shaped_undeclared"]
    failed = sorted(name for name, entry in expected.items() if not entry["passed"])
    assert failed == ["admission_order_hash_matches", "control_arm_present", "no_significance_language", "per_arm_n_disclosed"]
    assert _run_all(subject) == expected


def test_the_legacy_subject_runs_through_the_real_runner_unchanged():
    (_, subject, expected), = [c for c in _CASES if c[0] == "round_shaped_undeclared"]
    result = run_gate_suite(AIIF_ROUND_SUITE_ID, subject, timeout=180.0)
    assert (result["overall"], result["returncode"]) == ("FAIL", 1)
    assert {c["name"]: c for c in result["checks"]} == expected
    summary = result["stdout_tail"].strip().splitlines()[-1]
    assert summary.startswith("4 failed, 10 passed") and "skipped" not in summary


def test_control_count_without_a_design_changes_nothing():
    """``control_count`` is read under design ``control_seats`` only; on its
    own it is not a declaration, and a two-control round without a design is
    refused under today's rule, in today's words."""
    (_, two_controls, expected), = [c for c in _CASES if c[0] == "two_controls"]
    subject = copy.deepcopy(two_controls)
    subject["prereg"]["params"]["control_count"] = 2
    assert _run_all(subject) == expected


@pytest.mark.parametrize("key", ["design", "rooms", "report_p_values"])
def test_a_null_declaration_is_no_declaration(key):
    (_, subject, expected), = [c for c in _CASES if c[0] == "round_shaped_undeclared"]
    subject = copy.deepcopy(subject)
    subject["prereg"]["params"][key] = None
    assert _run_all(subject) == expected


def test_explicit_rooms_true_and_report_p_values_false_are_todays_rules():
    (_, subject, expected), = [c for c in _CASES if c[0] == "round_shaped_undeclared"]
    subject = copy.deepcopy(subject)
    subject["prereg"]["params"].update({"rooms": True, "report_p_values": False})
    assert _run_all(subject) == expected


# ---------------------------------------------------------------------------
# a complete round under each design passes every check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("builder", "not_applicable"),
    [
        (_control_seats_round, set()),
        (_paired_round, set()),
        (_none_round, {"control_arm_present", "per_arm_n_disclosed"}),
    ],
    ids=["control_seats", "paired", "none"],
)
def test_a_complete_round_under_each_design_passes(builder, not_applicable):
    results = _run_all(builder())
    statuses = {name: check_status(entry) for name, entry in results.items()}
    assert {n for n, s in statuses.items() if s == NOT_APPLICABLE} == not_applicable
    assert {n for n, s in statuses.items() if s == CHECK_FAIL} == set(), results


def test_a_declaration_moves_only_the_checks_that_read_it():
    """Every check outside the four readers comes out of a declared subject
    exactly as it comes out of the same subject undeclared."""
    for builder in (_control_seats_round, _paired_round, _none_round):
        declared = builder()
        undeclared = copy.deepcopy(declared)
        for key in ("design", "control_count"):
            undeclared["prereg"]["params"].pop(key, None)
        a, b = _run_all(declared), _run_all(undeclared)
        assert {k: v for k, v in a.items() if k not in _DECLARATION_READERS} == {
            k: v for k, v in b.items() if k not in _DECLARATION_READERS
        }


def test_the_design_vocabulary_is_the_three_the_brief_names():
    assert ROUND_DESIGNS == ("control_seats", "paired", "none")


# ---------------------------------------------------------------------------
# control_arm_present
# ---------------------------------------------------------------------------


def test_control_seats_passes_with_exactly_the_declared_count_and_names_it():
    result = control_arm_present(_control_seats_round(2))
    assert result.passed
    assert "design 'control_seats'" in result.message and "2 control lens(es)" in result.message
    assert "excluded from the arm mix and from the far floor" in result.message


def test_control_seats_with_one_seat_fewer_than_declared_fails_naming_design_and_count():
    subject = _control_seats_round(2)
    subject["prereg"]["params"]["control_count"] = 3
    result = control_arm_present(subject)
    assert not result.passed
    assert "design 'control_seats' (control_count 3)" in result.message
    assert "seats 2 control lens(es)" in result.message and "not the 3 declared" in result.message


def test_control_seats_one_is_the_single_control_under_a_declared_name():
    subject = _round(design="control_seats", control_count=1)
    assert control_arm_present(subject).passed
    subject["roster"].append({"lens_name": "lens_5", "seat": "control", "recipe_cards": []})
    assert not control_arm_present(subject).passed


def test_a_carded_control_is_not_a_control_under_control_seats_either():
    subject = _control_seats_round(2)
    subject["roster"][3]["recipe_cards"] = ["MISMATCH"]
    result = control_arm_present(subject)
    assert not result.passed
    assert "a control with a card is not a control" in result.message


@pytest.mark.parametrize("count", [None, 0, -1, True, "2", 2.0, [2]])
def test_control_seats_without_a_readable_count_fails(count):
    subject = _control_seats_round(2)
    subject["prereg"]["params"]["control_count"] = count
    result = control_arm_present(subject)
    assert not result.passed
    assert f"control_count={count!r}" in result.message


def test_control_seats_with_an_empty_roster_fails_closed():
    subject = _control_seats_round(2)
    subject["roster"] = []
    assert not control_arm_present(subject).passed


def test_paired_passes_when_every_standard_lens_holds_both_halves():
    result = control_arm_present(_paired_round())
    assert result.passed
    assert "design 'paired'" in result.message
    assert "2 card and 2 plain launch(es)" in result.message


def test_probe_b_a_lens_missing_its_plain_launch_fails_and_is_named():
    subject = _paired_round()
    subject["lens_launches"] = [r for r in subject["lens_launches"] if r["launch_id"] != "LNCH-2P"]
    result = control_arm_present(subject)
    assert not result.passed
    assert "lens_2 (standard): card launch(es) ['LNCH-2C'], plain launch(es) none" in result.message
    assert "lens_1 (" not in result.message
    assert "2 card and 1 plain launch(es)" in result.message


def test_a_plain_launch_without_a_card_launch_fails_too():
    subject = _paired_round()
    subject["lens_launches"] = [r for r in subject["lens_launches"] if r["launch_id"] != "LNCH-1C"]
    result = control_arm_present(subject)
    assert not result.passed
    assert "lens_1 (standard): card launch(es) none, plain launch(es) ['LNCH-1P']" in result.message


def test_no_lens_of_any_seat_may_hold_one_half_alone():
    """The buster is outside the requirement but inside the pairing rule: a
    buster launch labelled card with no plain half is refused."""
    subject = _paired_round()
    subject["lens_launches"][-1]["phase"] = "card"
    result = control_arm_present(subject)
    assert not result.passed
    assert "lens_3 (assumption_buster)" in result.message
    subject["lens_launches"].append({"lens_name": "lens_3", "launch_id": "LNCH-3P", "phase": "plain"})
    assert control_arm_present(subject).passed


def test_a_standard_lens_with_no_launches_at_all_fails():
    subject = _paired_round()
    subject["roster"].append({"lens_name": "lens_9", "seat": "standard", "recipe_cards": ["TRANSFER", "MISMATCH"]})
    result = control_arm_present(subject)
    assert not result.passed
    assert "lens_9 (standard): card launch(es) none, plain launch(es) none" in result.message


@pytest.mark.parametrize("label", ["derivation", None, "Plain", "plain ", "PLAIN", ["plain"], {"phase": "plain"}])
def test_only_the_exact_labels_pair(label):
    subject = _paired_round()
    for row in subject["lens_launches"]:
        if row["launch_id"] == "LNCH-2P":
            row["phase"] = label
    assert not control_arm_present(subject).passed


def test_a_launch_listed_once_per_assignment_row_counts_once():
    subject = _paired_round()
    subject["lens_launches"] += copy.deepcopy(subject["lens_launches"])
    result = control_arm_present(subject)
    assert result.passed
    assert "2 card and 2 plain launch(es)" in result.message


def test_a_later_phase_label_beside_both_halves_changes_nothing():
    subject = _paired_round()
    subject["lens_launches"].append({"lens_name": "lens_1", "launch_id": "LNCH-1D", "phase": "derivation"})
    assert control_arm_present(subject).passed


def test_paired_without_the_launch_block_fails_closed():
    subject = _paired_round()
    del subject["lens_launches"]
    result = control_arm_present(subject)
    assert not result.passed
    assert "subject['lens_launches'] is absent" in result.message


@pytest.mark.parametrize("block", [{"lens_1": ["card", "plain"]}, "card,plain"])
def test_paired_with_an_unrecognised_block_shape_fails(block):
    subject = _paired_round()
    subject["lens_launches"] = block
    result = control_arm_present(subject)
    assert not result.passed
    assert "shape unrecognised" in result.message


def test_paired_rows_that_name_no_lens_fail():
    subject = _paired_round()
    subject["lens_launches"].append({"launch_id": "LNCH-X", "phase": "plain"})
    result = control_arm_present(subject)
    assert not result.passed
    assert "row(s) [5] name no lens" in result.message


def test_paired_with_an_empty_block_names_every_standard_lens():
    subject = _paired_round()
    subject["lens_launches"] = []
    result = control_arm_present(subject)
    assert not result.passed
    assert "lens_1 (standard)" in result.message and "lens_2 (standard)" in result.message


def test_paired_with_no_standard_lens_fails():
    subject = _paired_round()
    subject["roster"] = [r for r in subject["roster"] if r["seat"] != "standard"]
    assert not control_arm_present(subject).passed


def test_design_none_is_not_applicable_and_says_so():
    result = control_arm_present(_none_round())
    assert result.not_applicable and not result.passed
    assert result.message.startswith("not applicable: design 'none'")
    assert "seats 0 control lens(es)" in result.message


@pytest.mark.parametrize("value", ["pairs", "Paired", "", "single", 3, True, ["paired"]])
def test_an_unreadable_design_fails_both_checks_that_read_it(value):
    subject = _round(design=value)
    for check in (control_arm_present, per_arm_n_disclosed):
        result = check(subject)
        assert not result.passed and not result.not_applicable
        assert f"design={value!r}" in result.message


# ---------------------------------------------------------------------------
# per_arm_n_disclosed
# ---------------------------------------------------------------------------


def test_control_seats_keeps_the_seat_rule_and_names_the_design():
    subject = _control_seats_round(2)
    result = per_arm_n_disclosed(subject)
    assert result.passed
    assert result.message.startswith("design 'control_seats': ")
    subject["outcomes"] = [r for r in subject["outcomes"] if r.get("seat") != "control"]
    result = per_arm_n_disclosed(subject)
    assert not result.passed and "no cell carries seat='control'" in result.message


def test_paired_reads_the_plain_cells_as_the_control_arm():
    result = per_arm_n_disclosed(_paired_round())
    assert result.passed
    assert "the plain half (1 arm='plain' cell(s))" in result.message


def test_paired_without_a_plain_cell_fails():
    subject = _paired_round()
    subject["outcomes"] = [r for r in subject["outcomes"] if r["arm"] != "plain"]
    result = per_arm_n_disclosed(subject)
    assert not result.passed
    assert "no cell carries arm='plain'" in result.message


def test_paired_still_requires_every_cells_n():
    subject = _paired_round()
    del subject["outcomes"][1]["n"]
    result = per_arm_n_disclosed(subject)
    assert not result.passed
    assert "arm=plain" in result.message


def test_a_plain_cell_that_calls_itself_the_control_is_keyed_by_its_arm():
    subject = _paired_round()
    subject["outcomes"][1]["cell"] = "control (plain half)"
    assert per_arm_n_disclosed(subject).passed
    subject["outcomes"].append({"cell": "control, unkeyed", "n": 2})
    result = per_arm_n_disclosed(subject)
    assert not result.passed
    assert "control, unkeyed" in result.message


def test_design_none_leaves_per_arm_n_not_applicable():
    result = per_arm_n_disclosed(_none_round())
    assert result.not_applicable and not result.passed
    assert "design 'none'" in result.message


# ---------------------------------------------------------------------------
# admission_order_hash_matches -- and probe (d)
# ---------------------------------------------------------------------------


def test_rooms_false_is_not_applicable():
    subject = _round(rooms=False)
    del subject["admission_order"], subject["admission_escrow"]
    result = admission_order_hash_matches(subject)
    assert result.not_applicable and not result.passed
    assert "rooms=false" in result.message


def _has_params(subject: dict) -> bool:
    prereg = subject.get("prereg")
    return isinstance(prereg, dict) and isinstance(prereg.get("params"), dict)


_CASES_WITH_PARAMS = [c for c in _CASES if _has_params(c[1])]


@pytest.mark.parametrize(
    ("name", "subject", "expected"), _CASES_WITH_PARAMS, ids=[c[0] for c in _CASES_WITH_PARAMS],
)
def test_probe_d_rooms_false_without_a_design_moves_only_the_admission_check(name, subject, expected):
    """Over every legacy subject whose params can carry the key: the
    admission check becomes not_applicable, and nothing else moves."""
    subject = copy.deepcopy(subject)
    subject["prereg"]["params"]["rooms"] = False
    got = _run_all(subject)
    changed = sorted(check for check in got if got[check] != expected[check])
    assert changed == ["admission_order_hash_matches"]
    assert check_status(got["admission_order_hash_matches"]) == NOT_APPLICABLE


@pytest.mark.parametrize("value", ["false", 0, 1, "no", []])
def test_an_unreadable_rooms_declaration_fails(value):
    result = admission_order_hash_matches(_round(rooms=value))
    assert not result.passed and not result.not_applicable
    assert f"rooms={value!r}" in result.message


# ---------------------------------------------------------------------------
# no_significance_language -- and probe (c)
# ---------------------------------------------------------------------------


def test_the_significance_list_is_unchanged():
    """Add none, drop none: the declaration changes what a round may use,
    not the list."""
    assert SIGNIFICANCE_TERMS == (
        "statistically significant", "statistical significance", "significantly", "significant", "significance",
        "p-value", "p value", "confidence interval", "null hypothesis",
    )
    assert set(REPORTED_P_VALUE_TERMS) <= set(SIGNIFICANCE_TERMS)


def test_probe_c_the_word_still_fails_and_the_number_does_not():
    subject = _round(report_p_values=True)
    subject["report_text"] = "The card half was significant (p = 0.03)."
    result = no_significance_language(subject)
    assert not result.passed
    assert "['significant']" in result.message
    assert "p = 0" not in result.message
    subject["report_text"] = "The card half survived more often (p = 0.03)."
    assert no_significance_language(subject).passed


@pytest.mark.parametrize(
    "text",
    [
        "randomization p = 0.031 over the lens pairs",
        "the contrast held at p ≤ 0.05",
        "p = 2/64, exact",
        "p<0.05 on the judged half",
        "a 95% confidence interval of 0.41 to 0.77",
        "a Clopper–Pearson interval for the share",
        "the 95 % interval excludes one half",
        "the randomization p-value is 2/64",
    ],
)
def test_report_p_values_allows_numbers_and_interval_wording(text):
    assert no_significance_language({**_round(report_p_values=True), "report_text": text}).passed


@pytest.mark.parametrize(
    "text",
    [
        "the far arm was significantly better",
        "this reaches statistical significance",
        "a statistically significant difference (p = 0.01)",
        "the significance of the card half",
        "we reject the null hypothesis at p = 0.02",
    ],
)
def test_report_p_values_keeps_the_claim_words_barred(text):
    result = no_significance_language({**_round(report_p_values=True), "report_text": text})
    assert not result.passed
    assert "stay barred in every mode" in result.message


def test_the_same_text_fails_without_the_declaration():
    text = "randomization p = 2/64 with a 95% confidence interval"
    assert not no_significance_language({**_round(), "report_text": text}).passed
    assert not no_significance_language({**_round(report_p_values=False), "report_text": text}).passed
    assert no_significance_language({**_round(report_p_values=True), "report_text": text}).passed


@pytest.mark.parametrize("value", ["true", 1, "yes"])
def test_an_unreadable_report_p_values_declaration_fails(value):
    result = no_significance_language(_round(report_p_values=value))
    assert not result.passed
    assert f"report_p_values={value!r}" in result.message


# ---------------------------------------------------------------------------
# the real runner and the gate row
# ---------------------------------------------------------------------------


def _declared_round() -> dict:
    """Paired, no rooms, a stated randomization p: the round the suite used
    to fail by construction, now declared."""
    subject = _paired_round(rooms=False, report_p_values=True)
    del subject["admission_order"], subject["admission_escrow"]
    subject["report_text"] = (
        "Card halves survived more often than plain halves (randomization p = 2/64; Clopper–Pearson 95 % "
        "interval 0.41 to 0.77), at n=6 per half."
    )
    return subject


def test_a_not_applicable_check_is_a_skip_and_the_run_passes():
    result = run_gate_suite(AIIF_ROUND_SUITE_ID, _declared_round(), timeout=180.0)
    assert (result["overall"], result["returncode"]) == ("PASS", 0), result["stdout_tail"]
    by_name = {c["name"]: c for c in result["checks"]}
    assert len(by_name) == 14
    admission = by_name["admission_order_hash_matches"]
    assert admission["status"] == NOT_APPLICABLE and admission["passed"] is False
    assert [n for n, c in by_name.items() if check_status(c) == NOT_APPLICABLE] == ["admission_order_hash_matches"]
    assert result["stdout_tail"].strip().splitlines()[-1].startswith("13 passed, 1 skipped")


def test_not_applicable_never_masks_a_failure():
    subject = _none_round()
    subject["report_text"] = "The far arm was significantly better."
    result = run_gate_suite(AIIF_ROUND_SUITE_ID, subject, timeout=180.0)
    assert (result["overall"], result["returncode"]) == ("FAIL", 1)
    statuses = {c["name"]: check_status(c) for c in result["checks"]}
    assert statuses["no_significance_language"] == CHECK_FAIL
    assert statuses["control_arm_present"] == statuses["per_arm_n_disclosed"] == NOT_APPLICABLE


def test_a_declared_round_writes_match_and_its_verdict_names_the_status(store):
    launch_id = bootstrap_launch(store)
    gate = _open_gated_artifact(store, launch_id=launch_id)
    result = run_gate_suite_for_gate(
        store, gate_id=gate["gate_id"], suite_id=AIIF_ROUND_SUITE_ID, subject=_declared_round(),
        issued_by_launch=launch_id, timeout=180.0,
    )
    assert result["reproduction_status"] == "match"
    refreshed = get(store, "gate", pk_column="gate_id", pk_value=gate["gate_id"])
    ref = json.loads(refreshed["reproduction_ref"])
    assert {c["name"]: check_status(c) for c in ref["checks"]}["admission_order_hash_matches"] == NOT_APPLICABLE
    stances = {e["note"].split(":", 1)[0]: e["stance"] for e in json.loads(result["verdict"]["evidence"])}
    assert stances["admission_order_hash_matches"] == "NOT_APPLICABLE"
    assert stances["control_arm_present"] == "PASS"
    assert apply_union(store, gate_id=gate["gate_id"], by_launch=launch_id)["state"] == "union_applied"
