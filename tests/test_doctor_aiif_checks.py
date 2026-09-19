"""The four framework doctor checks, each green and red:
``idea_missing_dossier``, ``round_collapse_flag_unacknowledged``,
``lens_brief_contains_verdict_text`` (``trialerror.lens.checks``) and
``gate_without_prereg`` (``trialerror.verify.checks``).

Plus one test that the three checks stage A and stage B landed are still
catalogued, since this lane is the last one in the sequence that reads that
row of the design's integration table.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trialerror.lens.checks import (
    check_idea_missing_dossier,
    check_lens_brief_contains_verdict_text,
    check_round_collapse_flag_unacknowledged,
)
from trialerror.lens.novelty import round_dir
from trialerror.stores import insert, update
from trialerror.util.doctor import DoctorContext, discover_and_register_checks, registered_checks
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now
from trialerror.verify.checks import check_gate_without_prereg

from tests._inventory_fixtures import bootstrap_launch


@pytest.fixture()
def ctx(program_root, platform_root) -> DoctorContext:
    return DoctorContext(program_root=program_root, platform_root=platform_root)


ROUND_ID = "ROUND-fix"


def _idea(store, *, status: str = "consolidated", round_id: str | None = ROUND_ID) -> str:
    idea_id = new_id("IDEA")
    insert(
        store, "idea",
        {
            "idea_id": idea_id, "round_id": round_id, "author_launch": bootstrap_launch(store),
            "body": "a state transition", "status": status, "created_ts": now(),
        },
    )
    return idea_id


def _write_dossier(program_root, idea_id: str, *, round_id: str = ROUND_ID) -> None:
    path = round_dir(program_root, round_id) / "novelty" / f"{idea_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"idea_id": idea_id, "label_inventory": "no-close-neighbour"}), encoding="utf-8")


def _write_batch(program_root, *, batch_id: str, collapse: dict, round_id: str = ROUND_ID) -> None:
    path = round_dir(program_root, round_id) / "batches" / f"{batch_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"round_id": round_id, "batch_id": batch_id, "collapse": collapse}), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# idea_missing_dossier
# ---------------------------------------------------------------------------


def test_idea_missing_dossier_green(store, ctx, program_root):
    ideas = [_idea(store) for _ in range(3)]
    for idea_id in ideas:
        _write_dossier(program_root, idea_id)
    result = check_idea_missing_dossier(ctx)
    assert result.status == "pass"
    assert result.details["ideas_checked"] == 3


def test_idea_missing_dossier_red_names_the_idea(store, ctx, program_root):
    kept, lost = _idea(store), _idea(store)
    _write_dossier(program_root, kept)
    (round_dir(program_root, ROUND_ID) / "novelty").mkdir(parents=True, exist_ok=True)
    result = check_idea_missing_dossier(ctx)
    assert result.status == "fail"
    assert [o["idea_id"] for o in result.details["offenders"]] == [lost]


def test_a_raw_idea_owes_no_dossier(store, ctx, program_root):
    _idea(store, status="raw")
    _write_batch(program_root, batch_id="batch-0", collapse={"mode": "descriptive", "flag": False})
    result = check_idea_missing_dossier(ctx)
    assert result.status == "skip"
    assert "no screened idea" in result.message


def test_a_promoted_idea_still_owes_its_dossier(store, ctx, program_root):
    promoted = _idea(store, status="promoted")
    (round_dir(program_root, ROUND_ID) / "novelty").mkdir(parents=True, exist_ok=True)
    result = check_idea_missing_dossier(ctx)
    assert result.status == "fail"
    assert result.details["offenders"][0]["status"] == "promoted"


def test_a_round_with_no_directory_is_not_judged(store, ctx, program_root):
    """"This round predates the screen" is a different statement from "this
    round lost a dossier"."""
    _idea(store, round_id="ROUND-older")
    _write_dossier(program_root, _idea(store))
    result = check_idea_missing_dossier(ctx)
    assert result.status == "pass"
    assert result.details["ideas_checked"] == 1


def test_idea_missing_dossier_skips_a_program_with_no_rounds(store, ctx):
    _idea(store)
    result = check_idea_missing_dossier(ctx)
    assert result.status == "skip"
    assert "no round directory" in result.message


# ---------------------------------------------------------------------------
# round_collapse_flag_unacknowledged
# ---------------------------------------------------------------------------


def _acknowledge(store, *, batch_id: str, event_type: str = "round_collapse_acknowledged", round_id: str = ROUND_ID) -> None:
    insert(
        store, "event",
        {
            "event_id": new_id("EVT"), "ts": now(), "type": event_type,
            "payload": json.dumps({"round_id": round_id, "batch_id": batch_id}),
        },
    )


def test_a_raised_flag_with_no_acknowledgement_fails(store, ctx, program_root):
    _write_batch(program_root, batch_id="batch-0", collapse={"mode": "armed", "flag": True, "reasons": [{"alarm": "x"}]})
    result = check_round_collapse_flag_unacknowledged(ctx)
    assert result.status == "fail"
    assert result.details["offenders"] == [{"round_id": ROUND_ID, "batch_id": "batch-0"}]


def test_an_acknowledged_flag_passes(store, ctx, program_root):
    _write_batch(program_root, batch_id="batch-0", collapse={"mode": "armed", "flag": True, "reasons": []})
    _acknowledge(store, batch_id="batch-0")
    assert check_round_collapse_flag_unacknowledged(ctx).status == "pass"


def test_the_contingency_firing_is_also_an_acknowledgement(store, ctx, program_root):
    _write_batch(program_root, batch_id="batch-1", collapse={"mode": "armed", "flag": True, "reasons": []})
    _acknowledge(store, batch_id="batch-1", event_type="round_collapse_rerun")
    assert check_round_collapse_flag_unacknowledged(ctx).status == "pass"


def test_acknowledging_one_batch_does_not_cover_another(store, ctx, program_root):
    _write_batch(program_root, batch_id="batch-0", collapse={"mode": "armed", "flag": True, "reasons": []})
    _write_batch(program_root, batch_id="batch-3", collapse={"mode": "armed", "flag": True, "reasons": []})
    _acknowledge(store, batch_id="batch-0")
    result = check_round_collapse_flag_unacknowledged(ctx)
    assert result.status == "fail"
    assert [o["batch_id"] for o in result.details["offenders"]] == ["batch-3"]


def test_armed_alarms_that_raised_nothing_pass(store, ctx, program_root):
    _write_batch(program_root, batch_id="batch-0", collapse={"mode": "armed", "flag": False, "reasons": []})
    result = check_round_collapse_flag_unacknowledged(ctx)
    assert result.status == "pass"
    assert result.details["batches_with_armed_alarms"] == 1


def test_descriptive_alarms_are_not_judged(store, ctx, program_root):
    """With no pre-registered alarm values there is no flag to raise, and the
    screen refuses to invent a threshold."""
    _write_batch(program_root, batch_id="batch-0", collapse={"mode": "descriptive", "flag": False, "reasons": []})
    result = check_round_collapse_flag_unacknowledged(ctx)
    assert result.status == "skip"
    assert "descriptive" in result.message


# ---------------------------------------------------------------------------
# lens_brief_contains_verdict_text
# ---------------------------------------------------------------------------


_CLEAN_BRIEF = (
    "Your slice is DOC-1 and DOC-2. Your vantage is bookkeeping load. Write 15 records under "
    "MISMATCH then TRANSFER, each naming the register row and the check that would test it."
)
_LEAKY_BRIEF = _CLEAN_BRIEF + " For reference, the screen labelled the last round's best idea new-mechanism."


def _lens_brief_launch(store, text: str) -> str:
    return bootstrap_launch(store, attrs={"lens_name": "lens_1", "brief": text}, purpose="ideation")


def _round_plan_block() -> str:
    """The round-plan template out of the ideation-round skill -- the one block
    that file says goes into every lens's prompt verbatim. Read from the file
    rather than copied, so a future edit to the template is audited by this
    check rather than by nobody."""
    path = Path(__file__).resolve().parent.parent / "plugin" / "skills" / "ideation-round" / "SKILL.md"
    lines = [line[1:].strip() for line in path.read_text(encoding="utf-8").splitlines() if line.startswith(">")]
    assert lines, "the ideation-round skill carries no round-plan blockquote"
    return chr(10).join(lines)


def test_a_clean_lens_brief_passes(store, ctx):
    _lens_brief_launch(store, _CLEAN_BRIEF)
    result = check_lens_brief_contains_verdict_text(ctx)
    assert result.status == "pass"
    assert result.details["briefs_checked"] == 1


def test_a_brief_carrying_a_dossier_label_fails(store, ctx):
    _lens_brief_launch(store, _LEAKY_BRIEF)
    result = check_lens_brief_contains_verdict_text(ctx)
    assert result.status == "fail"
    assert result.details["offenders"][0]["markers"] == ["new-mechanism"]


@pytest.mark.parametrize(
    "leak", ["label_inventory: variant", "the dossier says so", "a scoring rubric", "agreement_pct 94", "MEETS-BOTH"]
)
def test_each_marker_class_is_caught(store, ctx, leak):
    _lens_brief_launch(store, f"{_CLEAN_BRIEF} {leak}")
    assert check_lens_brief_contains_verdict_text(ctx).status == "fail"


def test_a_brief_recorded_as_an_event_is_audited_too(store, ctx):
    insert(
        store, "event",
        {
            "event_id": new_id("EVT"), "ts": now(), "type": "lens_brief",
            "payload": json.dumps({"round_id": ROUND_ID, "lens_name": "lens_2", "text": _LEAKY_BRIEF}),
        },
    )
    result = check_lens_brief_contains_verdict_text(ctx)
    assert result.status == "fail"
    assert result.details["offenders"][0]["source"] == "event:lens_brief"


def test_a_critics_brief_is_not_a_lens_brief(store, ctx):
    """A critic's brief is SUPPOSED to carry the pre-mortem and the rubric;
    flagging it would teach an operator to ignore this check."""
    bootstrap_launch(store, attrs={"brief": "Apply the rubric and the dossier."}, purpose="gates")
    result = check_lens_brief_contains_verdict_text(ctx)
    assert result.status == "skip"


def test_a_room_turn_booking_is_not_a_lens_brief(store, ctx):
    """Phase 5 books lenses too, and a room-turn envelope legitimately carries
    the dossier LABELS -- the room barrier allows exactly those. Reading its
    prompt as a generation brief would fail a correctly run round, which is
    the same reason the critic's brief is out of scope.

    The scope is the ideation booking: the purpose the lens export writes, or
    an assignment's own roster_id/slice_doc_ids.
    """
    bootstrap_launch(
        store,
        attrs={"lens_name": "lens_1", "room_id": "ROOM-1",
               "prompt": "DP1 carries dossier_labels {'label_inventory': 'variant'}"},
        purpose="room_participant",
    )
    result = check_lens_brief_contains_verdict_text(ctx)
    assert result.status == "skip"
    assert "room-turn booking" in result.message


def test_an_ideation_booking_is_in_scope_by_its_assignment_attrs(store, ctx):
    """A booking made by hand carries no ideation purpose but does carry what
    only an assignment can produce."""
    bootstrap_launch(
        store,
        attrs={"lens_name": "lens_1", "roster_id": "ROSTER-1", "slice_doc_ids": ["DOC-1"], "brief": _LEAKY_BRIEF},
        purpose="fixture",
    )
    assert check_lens_brief_contains_verdict_text(ctx).status == "fail"


def test_the_skills_own_round_plan_block_passes_the_check(store, ctx):
    """The block the skill says goes into every lens's prompt VERBATIM must
    pass the check that audits lens prompts.

    It did not: the acceptance gate read "every consolidated record
    dossiered", and the marker list carries the bare word -- so a brief
    assembled exactly as instructed failed with markers ['dossier'].
    """
    _lens_brief_launch(store, _CLEAN_BRIEF + chr(10) * 2 + _round_plan_block())
    result = check_lens_brief_contains_verdict_text(ctx)
    assert result.status == "pass", result.details["offenders"]


def test_no_recorded_brief_is_a_skip_that_names_both_places(store, ctx):
    result = check_lens_brief_contains_verdict_text(ctx)
    assert result.status == "skip"
    assert "launch's attrs" in result.message and "lens_brief" in result.message


# ---------------------------------------------------------------------------
# gate_without_prereg
# ---------------------------------------------------------------------------


def _gated_artifact(store, *, attrs: dict | None, type_key: str = "round-synthesis") -> tuple[str, str]:
    launch_id = bootstrap_launch(store)
    store.ops.execute(
        "INSERT OR IGNORE INTO template (type_key, title, version, path, gated) VALUES (?, ?, ?, ?, 1)",
        (type_key, type_key, "1", f"templates/{type_key}.md"),
    )
    store.ops.commit()
    artifact_id = new_id("ART")
    insert(
        store, "artifact",
        {
            "artifact_id": artifact_id, "type": type_key, "title": "round synthesis", "path": "s.md",
            "sha256": "0" * 64, "status": "draft", "registered_by_launch": launch_id,
            "attrs": json.dumps(attrs) if attrs is not None else None,
        },
    )
    gate_id = new_id("CR")
    insert(store, "gate", {"gate_id": gate_id, "artifact_id": artifact_id, "state": "submitted"})
    return artifact_id, gate_id


def _prereg(store, *, status: str = "committed") -> str:
    prereg_id = new_id("PREREG")
    insert(
        store, "prereg",
        {
            "prereg_id": prereg_id, "title": "round", "procedure_sha256": "a" * 64, "params_sha256": "b" * 64,
            "committed_ts": now(), "escrow_path": "escrow/p.json", "status": status,
        },
    )
    return prereg_id


def test_a_round_gate_naming_a_committed_prereg_passes(store, ctx):
    prereg_id = _prereg(store)
    _gated_artifact(store, attrs={"round_id": ROUND_ID, "prereg_id": prereg_id})
    result = check_gate_without_prereg(ctx)
    assert result.status == "pass"
    assert result.details["round_gates"] == 1


def test_a_round_gate_with_no_prereg_fails(store, ctx):
    _gated_artifact(store, attrs={"round_id": ROUND_ID})
    result = check_gate_without_prereg(ctx)
    assert result.status == "fail"
    assert "name no pre-registration" in result.message
    assert result.details["missing"][0]["round_id"] == ROUND_ID


def test_a_voided_prereg_is_its_own_offender_kind(store, ctx):
    prereg_id = _prereg(store, status="voided")
    _gated_artifact(store, attrs={"round_id": ROUND_ID, "prereg_id": prereg_id})
    result = check_gate_without_prereg(ctx)
    assert result.status == "fail"
    assert result.details["missing"] == []
    assert result.details["unusable"][0]["statuses"] == ["voided"]
    assert "does not resolve or is voided" in result.message


def test_a_prereg_linked_through_a_verdict_about_the_artifact_counts(store, ctx):
    """A gate verdict recorded against the artifact itself is one of the two
    verdict shapes read."""
    prereg_id = _prereg(store)
    artifact_id, _gate_id = _gated_artifact(store, attrs={"round_id": ROUND_ID})
    insert(
        store, "verdict",
        {
            "verdict_id": new_id("VRD"), "subject_kind": "artifact", "subject_id": artifact_id,
            "procedure": "gate", "procedure_version": "gate-v1", "label": "PASS",
            "evidence": json.dumps([]), "prereg_id": prereg_id, "ts": now(),
            "issued_by_launch": bootstrap_launch(store),
        },
    )
    assert check_gate_without_prereg(ctx).status == "pass"


def test_a_prereg_linked_only_through_the_screens_own_verdict_rows_counts(store, ctx):
    """The shape the novelty screen actually writes: one verdict per IDEA per
    reference set, subject_kind='claim', carrying the round's prereg_id.

    The fallback used to filter subject_kind='artifact' only, so the linkage
    the docstring and the guide both documented could never be satisfied --
    and the fixture that tested it wrote a shape nothing in the tree produces.
    The round is on the idea row, so that is where the join goes.
    """
    prereg_id = _prereg(store)
    _artifact_id, _gate_id = _gated_artifact(store, attrs={"round_id": ROUND_ID})
    idea_id = _idea(store)
    insert(
        store, "verdict",
        {
            "verdict_id": new_id("VRD"), "subject_kind": "claim", "subject_id": idea_id,
            "procedure": "custom", "procedure_version": "novelty-v2", "label": "new-mechanism",
            "evidence": json.dumps([]), "prereg_id": prereg_id, "ts": now(),
            "issued_by_launch": bootstrap_launch(store),
        },
    )
    assert check_gate_without_prereg(ctx).status == "pass"


def test_a_screen_verdict_from_another_round_does_not_cover_this_gate(store, ctx):
    prereg_id = _prereg(store)
    _artifact_id, _gate_id = _gated_artifact(store, attrs={"round_id": ROUND_ID})
    other = _idea(store, round_id="ROUND-other")
    insert(
        store, "verdict",
        {
            "verdict_id": new_id("VRD"), "subject_kind": "claim", "subject_id": other,
            "procedure": "custom", "procedure_version": "novelty-v2", "label": "new-mechanism",
            "evidence": json.dumps([]), "prereg_id": prereg_id, "ts": now(),
            "issued_by_launch": bootstrap_launch(store),
        },
    )
    result = check_gate_without_prereg(ctx)
    assert result.status == "fail"
    assert result.details["missing"][0]["round_id"] == ROUND_ID


def test_a_gate_on_a_non_round_artifact_is_out_of_scope(store, ctx):
    """Widening this to every gate would make it noise an operator learns to
    skip."""
    _gated_artifact(store, attrs={"purpose": "keystone"})
    result = check_gate_without_prereg(ctx)
    assert result.status == "skip"
    assert "declare a round_id" in result.message


def test_a_program_with_no_gates_is_skipped(store, ctx):
    assert check_gate_without_prereg(ctx).status == "skip"


# ---------------------------------------------------------------------------
# the integration table's checks row, end to end
# ---------------------------------------------------------------------------


def test_all_seven_checks_the_design_names_are_registered():
    discover_and_register_checks()
    registry = registered_checks()
    named = {
        # stage A / stage B
        "far_lens_floor_honored": "lens",
        "lens_citations_within_slice": "lens",
        "agent_model_matches_booking": "budget",
        # stage C
        "idea_missing_dossier": "lens",
        "round_collapse_flag_unacknowledged": "lens",
        "lens_brief_contains_verdict_text": "lens",
        "gate_without_prereg": "verify",
    }
    for name, category in named.items():
        assert name in registry, f"{name} is not registered"
        assert registry[name][0] == category, f"{name} is in category {registry[name][0]!r}, expected {category!r}"
