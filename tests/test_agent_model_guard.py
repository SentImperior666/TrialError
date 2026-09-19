"""``agent_model_matches_booking`` — the spawn gate's second model check,
its post-hoc doctor twin, and the ``[models]`` purpose table they both hang
off.

The failure this guards against is cheap and invisible: book ``top``,
spawn something cheaper, and every check that existed before this one still
passed. So the tests below are written from the attacker's side — book
top, try to spawn mid and small — and only then confirm the honest path
still works.
"""

from __future__ import annotations

import json
import tomllib

import pytest

from trialerror.budget.checks import check_agent_model_matches_booking
from trialerror.budget.gate import evaluate_spawn
from trialerror.budget.policy import (
    DEFAULT_MODEL_FAMILY_CLASSES,
    classify_model,
    meets_minimum,
    required_class_for_purpose,
)
from trialerror.budget.pools import book_launch, reconcile_launch
from trialerror.cli.program import _TRIALERROR_TOML_TEMPLATE
from trialerror.stores import get, insert
from trialerror.util.doctor import DoctorContext
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

# ---------------------------------------------------------------------------
# the shipped [models] table
# ---------------------------------------------------------------------------

#: The eight purposes the scaffold ships, and their floors. This tuple is
#: the test's own statement of the contract; the assertions below check the
#: shipped example against it, key for key, in both directions.
EXPECTED_MODEL_FLOORS = {
    "keystone": "top",
    "ideation": "top",
    "moderation": "top",
    "room_participant": "top",
    "novelty_judge": "top",
    "gates": "top",
    "consolidation": "mid",
    "screen": "mid",
}


def _uncommented_table(template: str, table: str) -> dict:
    """Pull one commented-out TOML table out of the scaffold template and
    parse it for real. The scaffold ships every table commented so a fresh
    program starts with no policy at all; that is exactly why the example
    needs parsing rather than eyeballing -- a commented block is a block
    nothing has ever checked."""
    lines = template.splitlines()
    start = lines.index(f"# [{table}]")
    body = []
    for line in lines[start:]:
        if not line.startswith("#"):
            break
        body.append(line.lstrip("#").lstrip())
    return tomllib.loads("\n".join(body))[table]


def test_the_scaffold_ships_exactly_the_eight_purposes_with_their_floors():
    models = _uncommented_table(_TRIALERROR_TOML_TEMPLATE, "models")
    assert models == EXPECTED_MODEL_FLOORS


def test_every_shipped_purpose_resolves_through_the_policy_module():
    models = _uncommented_table(_TRIALERROR_TOML_TEMPLATE, "models")
    for purpose, floor in models.items():
        assert required_class_for_purpose(models, purpose) == floor
        assert meets_minimum(floor, floor)
        assert meets_minimum("top", floor)
    # And a purpose the table does not name still has no floor at all --
    # the silent-permission case the table exists to close.
    assert required_class_for_purpose(models, "not_a_purpose") is None
    assert meets_minimum("small", None)


def test_no_research_judgment_purpose_floors_below_mid():
    models = _uncommented_table(_TRIALERROR_TOML_TEMPLATE, "models")
    assert "small" not in set(models.values())


def test_the_scaffold_also_ships_the_model_classes_escape_hatch():
    table = _uncommented_table(_TRIALERROR_TOML_TEMPLATE, "model_classes")
    assert table and set(table.values()) <= {"small", "mid", "top"}


# ---------------------------------------------------------------------------
# classify_model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("haiku", "small"),
        ("sonnet", "mid"),
        ("opus", "top"),
        ("fable", "top"),
        ("Claude Opus 5", "top"),
        ("claude-3-5-haiku-20241022", "small"),
        ("claude-sonnet-5", "mid"),
        ("inherit", None),
        ("", None),
        (None, None),
        ("a-model-nobody-has-heard-of", None),
    ],
)
def test_classify_model_reads_families_not_version_strings(model, expected):
    assert classify_model(model) == expected


def test_a_program_table_extends_and_overrides_the_built_in_families():
    assert classify_model("house-model-1", model_classes={"house-model-1": "top"}) == "top"
    # Override wins over the built-in family map.
    assert classify_model("sonnet", model_classes={"sonnet": "top"}) == "top"
    assert set(DEFAULT_MODEL_FAMILY_CLASSES.values()) <= {"small", "mid", "top"}


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------


@pytest.fixture()
def booked(store):
    """One open session and a booking factory over it."""
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "t", "created_ts": now()})
    session_id = new_id("SESS")
    insert(
        store, "session",
        {"session_id": session_id, "account_id": account_id, "opened_ts": now(), "status": "open"},
    )

    def _book(*, model_class="top", model="opus", purpose="ideation"):
        result = book_launch(
            store, session_id=session_id, program_id="PROG-test", agent_kind="lens",
            model_class=model_class, model=model, purpose=purpose, est_tokens=1000,
        )
        return result.launch_id

    return session_id, _book


@pytest.mark.parametrize("cheap_model", ["sonnet", "haiku", "claude-3-5-haiku-20241022"])
def test_a_top_booking_spawned_cheaper_is_refused(store, booked, cheap_model):
    session_id, book = booked
    launch_id = book(model_class="top", purpose="ideation")
    result = evaluate_spawn(
        store, f"launch_id: {launch_id}", session_id=session_id, agent_model=cheap_model
    )
    assert result.allowed is False
    assert result.code == "agent_model_below_booking"
    assert cheap_model in result.message
    # And the booking is NOT consumed -- a refused spawn must leave the
    # token usable once the caller fixes the model.
    assert get(store, "launch", pk_column="launch_id", pk_value=launch_id)["state"] == "PROVISIONAL"


def test_a_top_booking_spawned_top_is_allowed(store, booked):
    session_id, book = booked
    launch_id = book(model_class="top", purpose="ideation")
    result = evaluate_spawn(store, f"launch_id: {launch_id}", session_id=session_id, agent_model="opus")
    assert result.allowed is True
    assert get(store, "launch", pk_column="launch_id", pk_value=launch_id)["state"] == "RUNNING"


def test_a_mid_booking_spawned_top_is_allowed(store, booked):
    """The guard is a floor, not an equality: spending MORE than booked is
    a budget question, not a policy violation."""
    session_id, book = booked
    launch_id = book(model_class="mid", model="sonnet", purpose="consolidation")
    result = evaluate_spawn(store, f"launch_id: {launch_id}", session_id=session_id, agent_model="opus")
    assert result.allowed is True


def test_naming_no_model_at_all_is_not_a_mismatch(store, booked):
    session_id, book = booked
    launch_id = book(model_class="top")
    assert evaluate_spawn(store, f"launch_id: {launch_id}", session_id=session_id).allowed is True


@pytest.mark.parametrize("value", ["inherit", "", "   "])
def test_a_no_claim_model_value_is_not_a_mismatch(store, booked, value):
    session_id, book = booked
    launch_id = book(model_class="top")
    result = evaluate_spawn(store, f"launch_id: {launch_id}", session_id=session_id, agent_model=value)
    assert result.allowed is True


def test_an_unclassifiable_model_fails_closed_and_names_the_escape_hatch(store, booked):
    session_id, book = booked
    launch_id = book(model_class="top")
    result = evaluate_spawn(
        store, f"launch_id: {launch_id}", session_id=session_id, agent_model="mystery-model-9"
    )
    assert result.allowed is False
    assert result.code == "agent_model_unclassified"
    assert "[model_classes]" in result.message


def test_a_program_declared_model_class_lets_the_same_spawn_through(store, booked):
    session_id, book = booked
    launch_id = book(model_class="top")
    result = evaluate_spawn(
        store, f"launch_id: {launch_id}", session_id=session_id,
        agent_model="mystery-model-9", model_classes={"mystery-model-9": "top"},
    )
    assert result.allowed is True


def test_a_program_declared_model_class_can_also_convict(store, booked):
    session_id, book = booked
    launch_id = book(model_class="top")
    result = evaluate_spawn(
        store, f"launch_id: {launch_id}", session_id=session_id,
        agent_model="mystery-model-9", model_classes={"mystery-model-9": "small"},
    )
    assert result.allowed is False
    assert result.code == "agent_model_below_booking"


def test_the_ideation_floor_and_the_guard_refuse_the_same_spawn_for_different_reasons(store, booked):
    """Booking mid for a top-floor purpose is caught by the policy check;
    booking top and spawning mid is caught by this guard. Both paths have to
    be closed, or the other is trivially walked around."""
    session_id, book = booked
    models = _uncommented_table(_TRIALERROR_TOML_TEMPLATE, "models")

    cheap_booking = book(model_class="mid", model="sonnet", purpose="ideation")
    first = evaluate_spawn(store, f"launch_id: {cheap_booking}", session_id=session_id, policy=models)
    assert first.allowed is False and first.code == "model_policy_violation"

    honest_booking = book(model_class="top", model="opus", purpose="ideation")
    second = evaluate_spawn(
        store, f"launch_id: {honest_booking}", session_id=session_id, policy=models, agent_model="sonnet"
    )
    assert second.allowed is False and second.code == "agent_model_below_booking"


# ---------------------------------------------------------------------------
# reconcile records spawned_model; the doctor check reads it
# ---------------------------------------------------------------------------


def test_reconcile_records_spawned_model_on_the_launch_row(store, booked):
    _session_id, book = booked
    launch_id = book(model_class="top")
    result = reconcile_launch(store, launch_id=launch_id, actual_tokens=900, spawned_model="opus")
    assert result["spawned_model"] == "opus"
    attrs = json.loads(get(store, "launch", pk_column="launch_id", pk_value=launch_id)["attrs"])
    assert attrs["spawned_model"] == "opus"


def test_reconcile_without_a_spawned_model_leaves_existing_attrs_alone(store, booked):
    _session_id, book = booked
    launch_id = book(model_class="top")
    reconcile_launch(store, launch_id=launch_id, actual_tokens=900)
    row = get(store, "launch", pk_column="launch_id", pk_value=launch_id)
    attrs = json.loads(row["attrs"]) if row["attrs"] else {}
    assert "spawned_model" not in attrs


def _ctx(platform_root) -> DoctorContext:
    return DoctorContext(platform_root=platform_root)


def test_the_doctor_check_skips_when_nothing_is_attested(store, booked, platform_root):
    _session_id, book = booked
    launch_id = book(model_class="top")
    reconcile_launch(store, launch_id=launch_id, actual_tokens=900)
    store.close()
    result = check_agent_model_matches_booking(_ctx(platform_root))
    assert result.status == "skip"


def test_the_doctor_check_passes_on_an_honest_history(store, booked, platform_root):
    _session_id, book = booked
    for model in ("opus", "fable"):
        reconcile_launch(store, launch_id=book(model_class="top"), actual_tokens=900, spawned_model=model)
    reconcile_launch(
        store, launch_id=book(model_class="mid", model="sonnet", purpose="consolidation"),
        actual_tokens=100, spawned_model="sonnet",
    )
    store.close()
    result = check_agent_model_matches_booking(_ctx(platform_root))
    assert result.status == "pass", result.details
    assert result.details["attested"] == 3


def test_the_doctor_check_fails_a_top_booking_that_ran_on_a_cheap_model(store, booked, platform_root):
    _session_id, book = booked
    honest = book(model_class="top")
    cheat = book(model_class="top", purpose="novelty_judge")
    reconcile_launch(store, launch_id=honest, actual_tokens=900, spawned_model="opus")
    reconcile_launch(store, launch_id=cheat, actual_tokens=50, spawned_model="haiku")
    store.close()

    result = check_agent_model_matches_booking(_ctx(platform_root))
    assert result.status == "fail"
    offenders = result.details["offenders"]
    assert [o["launch_id"] for o in offenders] == [cheat]
    assert offenders[0]["spawned_class"] == "small"
    assert offenders[0]["booked_model_class"] == "top"


def test_the_doctor_check_flags_an_unclassifiable_spawned_model(store, booked, platform_root):
    """WARN, not FAIL, and in its own words (finding V-1). "I cannot place
    this name" is a different finding from "this ran below its floor", and
    only the second one is a rule that was broken."""
    _session_id, book = booked
    reconcile_launch(
        store, launch_id=book(model_class="top"), actual_tokens=900, spawned_model="mystery-model-9"
    )
    store.close()
    result = check_agent_model_matches_booking(_ctx(platform_root))
    assert result.status == "warn"
    assert result.details["offenders"] == []
    assert result.details["unclassified"][0]["spawned_class"] is None
    assert "below" not in result.message
    assert "[model_classes]" in result.message


def _ctx_with_program(platform_root, program_root) -> DoctorContext:
    return DoctorContext(platform_root=platform_root, program_root=program_root)


def _write_model_classes(program_root, table: str) -> None:
    (program_root / "trialerror.toml").write_text(
        '[program]\nid = "PROG-test"\n\n' + table, encoding="utf-8"
    )


def test_the_doctor_check_reads_the_same_model_classes_table_the_gate_reads(
    store, booked, platform_root, program_root
):
    """The V-1 regression, end to end. The gate ALLOWS a spawn whose model
    the program's own ``[model_classes]`` table places at ``top`` -- that
    table is the one-line fix its refusal message advertises. The post-hoc
    check must therefore resolve the same name the same way; before the fix
    it classified with the built-in families only and reported that very
    launch as an offender, at FAIL, under the below-the-floor headline."""
    _write_model_classes(program_root, '[model_classes]\n"house-model-9" = "top"\n')
    session_id, book = booked

    allowed = evaluate_spawn(
        store, f"launch_id: {book(model_class='top')}", session_id=session_id,
        agent_model="house-model-9", model_classes={"house-model-9": "top"},
    )
    assert allowed.allowed is True

    reconcile_launch(
        store, launch_id=book(model_class="top"), actual_tokens=900, spawned_model="house-model-9"
    )
    store.close()

    result = check_agent_model_matches_booking(_ctx_with_program(platform_root, program_root))
    assert result.status == "pass", result.details
    assert result.details["unclassified"] == []


def test_a_program_table_can_also_expose_a_floor_crossing_the_families_would_miss(
    store, booked, platform_root, program_root
):
    """The same table read in the other direction: a house model the
    program itself classes ``small`` is a real floor crossing under a
    ``top`` booking, and reading the table is what makes it visible."""
    _write_model_classes(program_root, '[model_classes]\n"house-model-9" = "small"\n')
    _session_id, book = booked
    cheat = book(model_class="top")
    reconcile_launch(store, launch_id=cheat, actual_tokens=900, spawned_model="house-model-9")
    store.close()

    result = check_agent_model_matches_booking(_ctx_with_program(platform_root, program_root))
    assert result.status == "fail"
    assert [o["launch_id"] for o in result.details["offenders"]] == [cheat]
    assert result.details["offenders"][0]["spawned_class"] == "small"


def test_no_program_root_and_unreadable_config_both_mean_defaults_only(
    store, booked, platform_root, program_root
):
    """platform.db is cross-program: a doctor run may have no program root,
    or one with no (or a broken) ``trialerror.toml``. None of those is an
    error here -- they all mean "defaults only", and the built-in families
    still resolve ``opus``."""
    _session_id, book = booked
    reconcile_launch(store, launch_id=book(model_class="top"), actual_tokens=900, spawned_model="opus")
    store.close()

    assert check_agent_model_matches_booking(_ctx(platform_root)).status == "pass"
    assert (
        check_agent_model_matches_booking(_ctx_with_program(platform_root, program_root)).status
        == "pass"
    )
    (program_root / "trialerror.toml").write_text("this is not toml = = =", encoding="utf-8")
    assert (
        check_agent_model_matches_booking(_ctx_with_program(platform_root, program_root)).status
        == "pass"
    )


def test_a_floor_crossing_outranks_an_unclassifiable_name_in_the_headline(
    store, booked, platform_root
):
    """Both kinds present: the status is the fail the crossing earns, and
    the unresolvable name is still counted in the message rather than
    disappearing behind it."""
    _session_id, book = booked
    reconcile_launch(store, launch_id=book(model_class="top"), actual_tokens=50, spawned_model="haiku")
    reconcile_launch(
        store, launch_id=book(model_class="top"), actual_tokens=900, spawned_model="mystery-model-9"
    )
    store.close()

    result = check_agent_model_matches_booking(_ctx(platform_root))
    assert result.status == "fail"
    assert len(result.details["offenders"]) == 1
    assert len(result.details["unclassified"]) == 1
    assert "1 more ran on a model no [model_classes] entry resolves" in result.message
