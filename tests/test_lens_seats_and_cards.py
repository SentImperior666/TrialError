"""The control seat, the recipe-card block, and the two roster-level doctor
checks that read them.

Both checks are exercised green AND red: a check that has only ever been
seen passing is a check whose failure path is untested, and these two exist
precisely to catch a round that was assembled wrong.
"""

from __future__ import annotations

import json

import pytest

from trialerror.lens.assign import run_assignment
from trialerror.lens.checks import (
    CARDS_PER_STANDARD_LENS,
    MAX_CARDS_PER_ROUND,
    MIN_FAR_LENSES,
    MIN_LENSES_PER_CARD,
    check_far_lens_floor_honored,
    check_recipe_rotation_honored,
)
from trialerror.lens.export import export_launch_bookable
from trialerror.lens.roster import (
    BUSTER_ONLY_CARD,
    CONTROL_VANTAGE_PREFIX,
    SEATS,
    add_lens,
    list_roster,
    roster_cards,
)
from trialerror.stores import insert
from trialerror.util.doctor import DoctorContext
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now
from tests._lens_fixtures import build_doc_pool

SEED = "seed-seats"


@pytest.fixture()
def ctx(program_root) -> DoctorContext:
    return DoctorContext(program_root=program_root)


# ---------------------------------------------------------------------------
# the roster row
# ---------------------------------------------------------------------------


def test_control_is_a_seat_now():
    assert set(SEATS) == {"standard", "assumption_buster", "control"}


def test_a_control_seat_round_trips_with_the_documented_vantage_convention(store):
    row = add_lens(
        store, round_id="r", lens_name="control-1",
        vantage=f"{CONTROL_VANTAGE_PREFIX}no-recipe", model_class="top", seat="control",
    )
    assert row["seat"] == "control"
    assert list_roster(store, round_id="r")[0]["seat"] == "control"


def test_a_card_block_keeps_its_seeded_order(store):
    row = add_lens(
        store, round_id="r", lens_name="l1", vantage="v", model_class="top",
        recipe_cards=["TRANSFER", "MISMATCH"],
    )
    assert json.loads(row["recipe_cards"]) == ["TRANSFER", "MISMATCH"]
    assert roster_cards(row) == ["TRANSFER", "MISMATCH"]


def test_a_control_seat_carrying_a_card_is_refused(store):
    with pytest.raises(ValueError) as exc:
        add_lens(
            store, round_id="r", lens_name="c", vantage="v", model_class="top",
            seat="control", recipe_cards=["MISMATCH"],
        )
    assert "control" in str(exc.value)


def test_an_unknown_seat_is_still_refused(store):
    with pytest.raises(ValueError):
        add_lens(store, round_id="r", lens_name="l", vantage="v", model_class="top", seat="observer")


def test_roster_cards_never_raises_on_a_malformed_value():
    assert roster_cards({"recipe_cards": None}) == []
    assert roster_cards({"recipe_cards": "not json at all"}) == []
    assert roster_cards({"recipe_cards": '{"not": "a list"}'}) == []
    assert roster_cards({}) == []


# ---------------------------------------------------------------------------
# a round the checks can read
# ---------------------------------------------------------------------------


def _round(
    store, *, round_id, seats_and_cards, slices_per_lens=5, arm_mode="per_lens",
    n_docs=61, far_floor=2,
):
    """Build a real round: roster rows, then a real seeded assignment."""
    pool = build_doc_pool(store, n_docs=n_docs)
    home_id, *candidate_ids = pool["doc_ids"]
    lenses = []
    for i, (seat, cards) in enumerate(seats_and_cards):
        row = add_lens(
            store, round_id=round_id, lens_name=f"lens-{i}", vantage=f"v{i}",
            model_class="top", seat=seat, recipe_cards=cards,
        )
        lenses.append({"roster_id": row["roster_id"], "seat": seat, "recipe_cards": row["recipe_cards"]})
    run_assignment(
        store, round_id=round_id, model_key=pool["model_key"], home_doc_ids=[home_id],
        candidate_doc_ids=candidate_ids, lenses=lenses, slices_per_lens=slices_per_lens,
        seed=SEED, arm_mode=arm_mode, far_floor=far_floor,
    )
    return lenses


_TWO_CARD_ROUND = [
    ("standard", ["MISMATCH", "TRANSFER"]),
    ("standard", ["TRANSFER", "MISMATCH"]),
    ("standard", ["MISMATCH", "TRANSFER"]),
    ("standard", ["TRANSFER", "MISMATCH"]),
    ("assumption_buster", ["NEGATE"]),
    ("control", None),
]


# ---------------------------------------------------------------------------
# far_lens_floor_honored
# ---------------------------------------------------------------------------


def test_far_lens_floor_skips_a_program_with_no_per_lens_round(store, ctx):
    _round(store, round_id="r-per-slice", seats_and_cards=_TWO_CARD_ROUND, arm_mode="per_slice")
    store.close()
    assert check_far_lens_floor_honored(ctx).status == "skip"


def test_far_lens_floor_passes_for_a_real_per_lens_round(store, ctx):
    _round(store, round_id="r-ok", seats_and_cards=_TWO_CARD_ROUND)
    store.close()
    result = check_far_lens_floor_honored(ctx)
    assert result.status == "pass", result.details


def test_far_lens_floor_fails_when_a_direct_write_leaves_one_far_lens(store, ctx):
    """The seeded draw structurally cannot produce this, so a violation is a
    direct write -- exactly what the check is for. Move every far
    assignment but one lens's into the near arm."""
    _round(store, round_id="r-thin", seats_and_cards=_TWO_CARD_ROUND)
    far_rosters = [
        r["roster_id"]
        for r in store.ops.execute(
            "SELECT DISTINCT roster_id FROM lens_assignment WHERE arm = 'far'"
        ).fetchall()
    ]
    assert len(far_rosters) == 2
    with store.ops:
        store.ops.execute(
            "UPDATE lens_assignment SET arm = 'near' WHERE roster_id = ?", (far_rosters[0],)
        )
    store.close()

    result = check_far_lens_floor_honored(ctx)
    assert result.status == "fail"
    offender = result.details["offenders"][0]
    assert offender["round_id"] == "r-thin"
    assert offender["far_lens_count"] == 1
    assert offender["far_lens_floor"] == 2


def test_far_lens_floor_skips_when_ops_db_is_absent(tmp_path):
    ctx = DoctorContext(program_root=tmp_path / "no-such-program")
    assert check_far_lens_floor_honored(ctx).status == "skip"
    assert check_recipe_rotation_honored(ctx).status == "skip"


def test_a_round_cannot_lower_the_far_lens_floor_below_the_hard_floor(store, ctx):
    """Finding V-2. `--far-floor 1` is a round declaring a smaller bar for
    itself; the amendment states the far arm as a floor of TWO agents, so
    the check judges the harder of the two. Before the fix this round
    seated one far lens, recorded far_lens_floor=1, and passed."""
    _round(
        store, round_id="r-lowered",
        seats_and_cards=[("standard", None)] * 5 + [("assumption_buster", None)],
        far_floor=1,
    )
    recorded = store.ops.execute(
        "SELECT DISTINCT far_lens_floor FROM lens_assignment"
    ).fetchone()[0]
    assert recorded == 1
    far_lenses = store.ops.execute(
        "SELECT COUNT(DISTINCT roster_id) FROM lens_assignment WHERE arm = 'far'"
    ).fetchone()[0]
    assert far_lenses == 1
    store.close()

    result = check_far_lens_floor_honored(ctx)
    assert result.status == "fail", result.details
    offender = result.details["offenders"][0]
    assert offender["far_lens_count"] == 1
    assert offender["far_lens_floor"] == MIN_FAR_LENSES
    assert offender["hard_floor"] == MIN_FAR_LENSES


def test_a_round_may_still_ask_for_more_far_lenses_than_the_hard_floor(store, ctx):
    """The floor is a floor, not a fixed value: a round declaring three is
    held to three."""
    _round(
        store, round_id="r-raised",
        seats_and_cards=[("standard", None)] * 5 + [("assumption_buster", None)],
        far_floor=3,
    )
    with store.ops:
        store.ops.execute("UPDATE lens_assignment SET arm = 'near' WHERE arm = 'far'")
        store.ops.execute(
            "UPDATE lens_assignment SET arm = 'far' WHERE roster_id IN "
            "(SELECT DISTINCT roster_id FROM lens_assignment LIMIT 2)"
        )
    store.close()

    result = check_far_lens_floor_honored(ctx)
    assert result.status == "fail"
    assert result.details["offenders"][0]["far_lens_floor"] == 3


# ---------------------------------------------------------------------------
# recipe_rotation_honored
# ---------------------------------------------------------------------------


def test_recipe_rotation_skips_a_round_with_no_cards(store, ctx):
    _round(
        store, round_id="r-nocards",
        seats_and_cards=[("standard", None)] * 4 + [("assumption_buster", None), ("control", None)],
    )
    store.close()
    assert check_recipe_rotation_honored(ctx).status == "skip"


def test_recipe_rotation_passes_when_every_card_is_held_twice(store, ctx):
    _round(store, round_id="r-cards-ok", seats_and_cards=_TWO_CARD_ROUND)
    store.close()
    result = check_recipe_rotation_honored(ctx)
    assert result.status == "pass", result.details


def test_recipe_rotation_fails_a_card_held_by_one_lens(store, ctx):
    seats = [
        ("standard", ["MISMATCH", "INVERT"]),   # INVERT held by this lens alone
        ("standard", ["MISMATCH", "TRANSFER"]),
        ("standard", ["TRANSFER", "MISMATCH"]),
        ("standard", ["MISMATCH", "TRANSFER"]),
        ("assumption_buster", ["NEGATE"]),
        ("control", None),
    ]
    _round(store, round_id="r-thin-card", seats_and_cards=seats)
    store.close()

    result = check_recipe_rotation_honored(ctx)
    assert result.status == "fail"
    thin = [o for o in result.details["offenders"] if o["reason"] == "card held by too few lenses"]
    assert thin and thin[0]["cards"] == ["INVERT"]
    assert thin[0]["min_lenses_per_card"] == MIN_LENSES_PER_CARD


def test_recipe_rotation_fails_a_round_that_drew_five_cards(store, ctx):
    seats = [
        ("standard", ["A-CARD", "B-CARD"]),
        ("standard", ["A-CARD", "B-CARD"]),
        ("standard", ["C-CARD", "D-CARD"]),
        ("standard", ["C-CARD", "D-CARD", "E-CARD"]),
        ("standard", ["E-CARD", "A-CARD"]),
        ("assumption_buster", ["NEGATE"]),
    ]
    _round(store, round_id="r-too-many", seats_and_cards=seats, slices_per_lens=5)
    store.close()

    result = check_recipe_rotation_honored(ctx)
    assert result.status == "fail"
    over = [o for o in result.details["offenders"] if o["reason"] == "too many distinct cards in one round"]
    assert over and over[0]["max_cards_per_round"] == MAX_CARDS_PER_ROUND
    assert len(over[0]["cards"]) == 5


def test_the_busters_own_card_does_not_count_against_the_two_lens_bar(store, ctx):
    """NEGATE comes with the seat and is held by one lens by design -- the
    check must not report the buster as a rotation violation."""
    _round(store, round_id="r-buster", seats_and_cards=_TWO_CARD_ROUND)
    store.close()
    result = check_recipe_rotation_honored(ctx)
    assert result.status == "pass"
    assert "NEGATE" not in json.dumps(result.details)


# --- the two block-design rules that used to go unenforced (finding V-3) ---


def test_a_standard_lens_with_a_short_block_is_a_violation(store, ctx):
    """Finding V-3, first half. Every standard lens writes under exactly
    two cards in a seeded order; that is what makes the block a block. A
    round with one lens holding a single card and another holding none used
    to pass, because only the per-CARD bars were counted."""
    seats = [
        ("standard", ["MISMATCH", "TRANSFER"]),
        ("standard", ["TRANSFER", "MISMATCH"]),
        ("standard", ["MISMATCH"]),
        ("standard", None),
        ("assumption_buster", ["NEGATE"]),
        ("control", None),
    ]
    lenses = _round(store, round_id="r-short-block", seats_and_cards=seats)
    store.close()

    result = check_recipe_rotation_honored(ctx)
    assert result.status == "fail"
    short = [o for o in result.details["offenders"] if o["reason"].startswith("standard lens not")]
    assert short and short[0]["cards_per_standard_lens"] == CARDS_PER_STANDARD_LENS
    assert set(short[0]["lenses"]) == {lenses[2]["roster_id"], lenses[3]["roster_id"]}
    assert sorted(short[0]["block_sizes"].values()) == [0, 1]


def test_the_full_block_round_reports_no_block_size_offender(store, ctx):
    _round(store, round_id="r-blocks-ok", seats_and_cards=_TWO_CARD_ROUND)
    store.close()
    result = check_recipe_rotation_honored(ctx)
    assert result.status == "pass"
    assert f"{CARDS_PER_STANDARD_LENS} cards" in result.message


def test_negate_on_a_standard_seat_is_refused_at_the_write(store):
    """Finding V-3, second half, at the writer: NEGATE presumes the
    buster's stake, so no other seat may hold it."""
    with pytest.raises(ValueError) as exc:
        add_lens(
            store, round_id="r", lens_name="l", vantage="v", model_class="top",
            seat="standard", recipe_cards=["NEGATE", "TRANSFER"],
        )
    assert BUSTER_ONLY_CARD in str(exc.value)
    # The buster itself is of course still allowed to hold it.
    assert add_lens(
        store, round_id="r", lens_name="b", vantage="v", model_class="top",
        seat="assumption_buster", recipe_cards=["NEGATE"],
    )["seat"] == "assumption_buster"


def test_negate_written_onto_standard_seats_directly_is_still_caught(store, ctx):
    """The audit half of the same rule, over rows the writer never saw
    (a direct write, or a round assembled before the refusal existed)."""
    _round(store, round_id="r-negate", seats_and_cards=_TWO_CARD_ROUND)
    victim = store.ops.execute(
        "SELECT roster_id FROM lens_roster WHERE seat = 'standard' ORDER BY rowid LIMIT 1"
    ).fetchone()[0]
    with store.ops:
        # The check reads the block off the ASSIGNMENT rows (that is where
        # run_assignment copies it to), so that is where a direct write has
        # to land for this to be the round the doctor would actually see.
        for table in ("lens_roster", "lens_assignment"):
            store.ops.execute(
                f"UPDATE {table} SET recipe_cards = ? WHERE roster_id = ?",
                (json.dumps(["NEGATE", "TRANSFER"]), victim),
            )
    store.close()

    result = check_recipe_rotation_honored(ctx)
    assert result.status == "fail"
    stolen = [o for o in result.details["offenders"] if o.get("card") == BUSTER_ONLY_CARD]
    assert stolen and stolen[0]["lenses"] == [victim]


def test_a_fifth_card_parked_on_the_buster_cannot_hide_from_the_ceiling(store, ctx):
    """Finding V-3, the hole the two bars left between them: the buster's
    block was excluded from BOTH, so a fifth catalogue card sitting there
    was invisible to the four-card ceiling. Non-NEGATE cards now count
    toward the ceiling wherever they sit."""
    seats = [
        ("standard", ["A-CARD", "B-CARD"]),
        ("standard", ["A-CARD", "B-CARD"]),
        ("standard", ["C-CARD", "D-CARD"]),
        ("standard", ["C-CARD", "D-CARD"]),
        ("assumption_buster", ["E-CARD"]),
    ]
    _round(store, round_id="r-hidden-fifth", seats_and_cards=seats)
    store.close()

    result = check_recipe_rotation_honored(ctx)
    assert result.status == "fail"
    over = [o for o in result.details["offenders"] if o["reason"].startswith("too many")]
    assert over and over[0]["cards"] == ["A-CARD", "B-CARD", "C-CARD", "D-CARD", "E-CARD"]
    # And it is reported as thin too: no standard lens holds it at all.
    thin = [o for o in result.details["offenders"] if o["reason"] == "card held by too few lenses"]
    assert thin and thin[0]["holders"]["E-CARD"] == 0


# ---------------------------------------------------------------------------
# export attrs
# ---------------------------------------------------------------------------


def test_export_attrs_carry_arm_mode_arm_and_the_card_block(store):
    _round(store, round_id="r-export", seats_and_cards=_TWO_CARD_ROUND)
    rows = export_launch_bookable(store, round_id="r-export")
    assert len(rows) == 6
    for row in rows:
        attrs = row["attrs"]
        assert attrs["arm_mode"] == "per_lens"
        assert attrs["arm"] in ("near", "moderate", "far")
        # The lens's whole slice sits in the arm the attrs name.
        assert attrs["arms"][attrs["arm"]] == attrs["slice_count"]
    buster = next(r for r in rows if r["attrs"]["seat"] == "assumption_buster")
    assert buster["attrs"]["arm"] == "far"
    assert buster["attrs"]["recipe_cards"] == ["NEGATE"]
    control = next(r for r in rows if r["attrs"]["seat"] == "control")
    assert control["attrs"]["recipe_cards"] == []


def test_export_names_no_single_arm_under_per_slice(store):
    _round(store, round_id="r-export-ps", seats_and_cards=_TWO_CARD_ROUND, arm_mode="per_slice")
    rows = export_launch_bookable(store, round_id="r-export-ps")
    for row in rows:
        assert row["attrs"]["arm_mode"] == "per_slice"
        assert row["attrs"]["arm"] is None


def test_a_pre_v9_assignment_row_reads_as_no_mode_no_cards(store):
    """Rows written before the mode columns existed: NULL everywhere, and
    every reader treats that as 'the per-slice default, no cards' rather
    than crashing on a missing key."""
    roster = add_lens(store, round_id="r-legacy", lens_name="old", vantage="v", model_class="top")
    insert(
        store, "lens_assignment",
        {
            "assign_id": new_id("ASGN"), "roster_id": roster["roster_id"],
            "slice_spec": json.dumps({"round_id": "r-legacy", "candidate_id": "DOC-1"}),
            "arm": "near", "seed": "s", "created_ts": now(),
        },
    )
    rows = export_launch_bookable(store, round_id="r-legacy")
    assert rows[0]["attrs"]["arm_mode"] is None
    assert rows[0]["attrs"]["arm"] is None
    assert rows[0]["attrs"]["recipe_cards"] == []


def test_both_new_checks_skip_an_ops_db_that_predates_the_columns(tmp_path):
    """Doctor reads read-only and never migrates, so a program still at
    ops v8 must not turn "your store is behind" into a lens FAIL --
    store_schema_version already reports that, precisely."""
    import sqlite3

    from trialerror.stores import paths
    from trialerror.stores.migrate import apply_migrations
    from trialerror.stores.schema import ops as ops_schema

    program_root = tmp_path / "old-program"
    db_path = paths.ops_db_path(program_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    # WAL to match what open_store leaves behind: the read-only connection
    # doctor opens issues a journal_mode pragma, which a non-WAL file
    # refuses -- an artefact of building this store by hand, not of the
    # thing under test.
    conn.execute("PRAGMA journal_mode = WAL")
    apply_migrations(conn, tuple(m for m in ops_schema.MIGRATIONS if m.version <= 8))
    conn.commit()
    conn.close()

    old_ctx = DoctorContext(program_root=program_root)
    for check in (check_far_lens_floor_honored, check_recipe_rotation_honored):
        result = check(old_ctx)
        assert result.status == "skip", result.message
        assert "schema-v9" in result.message
