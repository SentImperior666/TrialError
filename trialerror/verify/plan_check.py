"""The plan-time check at ``prereg commit`` (F3 step 1).

A round's gate suite (:data:`trialerror.eval.gate_suites.AIIF_ROUND_SUITE_ID`)
runs its 14 checks only AFTER the round has produced its data. Some of those
requirements can already be decided from the parameters the round commits, and
a round that finds out at the gate that a requirement was never planned has
lost its registration. This module runs that decidable part at commit time, so
``trialerror.verify.prereg.commit_prereg`` can refuse before the escrow file or
the row exists.

**What is projected, and what is not.** Each item of the ``aiif_round`` plan
suite mirrors one gate-time check, restricted to what the params (and the
program's ``[models]`` table) can decide:

=============================  ===============================
plan item                      gate check it projects
=============================  ===============================
``declarations_readable``      every check that reads a declaration
``control_plan_consistent``    ``control_arm_present``
``admission_escrow_planned``   ``admission_order_hash_matches``
``models_floors_met``          ``models_table_present``
``arm_mode_declared``          ``arm_mode_declared``
``card_cells_feasible``        ``card_cells_ge_2``
``reference_sets_named``       ``novelty_bundle_complete`` (the reference sets)
``plants_declared``            ``plants_caught`` (the plan for plants)
=============================  ===============================

Gate checks that decide only from run or report data are NOT projected:
``prereg_present`` (a prereg exists once this commit lands),
``self_assessment_absent`` (reads the report), ``distribution_card_present``
(reads the report), ``lens_log_reconciled`` (reads the lens log),
``per_arm_n_disclosed`` (reads the report's cells), ``no_significance_language``
(reads the report text), ``consolidation_completeness`` (reads the pool after
the screen), and the label half of ``novelty_bundle_complete`` (reads the
dossiers).

The second escrow of a round that runs rooms (the room order, committed after
the judged screen) is checked by the ``aiif_round_admission`` plan suite.

The check reads the params and never alters them: the params hash of the
commit is unchanged. Helpers that the gate suite keeps private are re-derived
here (the harness convention for cross-lane private helpers); only its public
constants are imported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from trialerror.budget.policy import meets_minimum
from trialerror.eval.gate_suites import (
    AIIF_MODEL_FLOORS,
    BUSTER_ONLY_CARD,
    DESIGN_CONTROL_SEATS,
    DESIGN_NONE,
    DESIGN_PAIRED,
    MIN_LENS_CELLS_PER_CARD,
    PREREG_USABLE_STATUSES,
    ROUND_DESIGNS,
)
from trialerror.util.timeutil import now
from trialerror.verify.errors import UnknownPlanSuiteError

__all__ = [
    "SEVERITY_MUST",
    "SEVERITY_WARN",
    "PLAN_SUITE_ROUND",
    "PLAN_SUITE_ADMISSION",
    "PlanCheckItem",
    "PlanCheckResult",
    "PlanContext",
    "PLAN_SUITES",
    "register_plan_suite",
    "run_plan_check",
    "room_seed_named",
]

SEVERITY_MUST = "must"  # a failure refuses the commit unless it is an accepted deviation
SEVERITY_WARN = "warn"  # recorded, never refuses

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_NOT_APPLICABLE = "not_applicable"

PLAN_SUITE_ROUND = "aiif_round"
PLAN_SUITE_ADMISSION = "aiif_round_admission"

# lower case only: the room order's hash is printed in lower case and the gate compares it as a string
_HEX64 = re.compile(r"[0-9a-f]{64}")
_REFERENCE_SET_KEYS = ("R1", "R2", "R3", "R4", "R5")


@dataclass(frozen=True)
class PlanCheckItem:
    check_id: str
    severity: str  # SEVERITY_MUST | SEVERITY_WARN
    status: str  # "pass" | "fail" | "not_applicable"
    message: str  # plain words: what was found and what the gate will need
    gate_check: str | None  # the aiif_round gate check this item projects, or None

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "severity": self.severity,
            "status": self.status,
            "message": self.message,
            "gate_check": self.gate_check,
        }


@dataclass(frozen=True)
class PlanCheckResult:
    suite_id: str
    items: tuple[PlanCheckItem, ...]
    checked_ts: str

    @property
    def must_failures(self) -> list[str]:
        return [i.check_id for i in self.items if i.severity == SEVERITY_MUST and i.status == STATUS_FAIL]

    @property
    def warnings(self) -> list[str]:
        return [i.check_id for i in self.items if i.severity == SEVERITY_WARN and i.status == STATUS_FAIL]

    @property
    def overall(self) -> str:
        if self.must_failures:
            return "fail"
        if self.warnings:
            return "pass_with_warnings"
        return "pass"

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite_id": self.suite_id,
            "overall": self.overall,
            "must_failures": self.must_failures,
            "warnings": self.warnings,
            "checked_ts": self.checked_ts,
            "items": [i.to_dict() for i in self.items],
        }


@dataclass(frozen=True)
class PlanContext:
    params: Mapping[str, Any]
    models_table: Mapping[str, Any]  # the program's [models] table, read at check time
    round_id: str | None
    parent: Mapping[str, Any] | None  # the parent prereg row plus its plan_check JSON, else None
    store: Any | None  # for the admission suite's optional recompute; None in pure tests


PlanSuite = Callable[[PlanContext], "tuple[PlanCheckItem, ...]"]
PLAN_SUITES: dict[str, PlanSuite] = {}


def register_plan_suite(suite_id: str, fn: PlanSuite) -> None:
    PLAN_SUITES[suite_id] = fn


def run_plan_check(suite_id: str, ctx: PlanContext) -> PlanCheckResult:
    fn = PLAN_SUITES.get(suite_id)
    if fn is None:
        raise UnknownPlanSuiteError(f"unknown plan suite {suite_id!r}; known: {sorted(PLAN_SUITES)}")
    return PlanCheckResult(suite_id=suite_id, items=tuple(fn(ctx)), checked_ts=now())


# ---------------------------------------------------------------------------
# small parsing helpers (re-derived; the gate's own are private)
# ---------------------------------------------------------------------------


def _item(check_id: str, severity: str, status: str, message: str, gate_check: str | None) -> PlanCheckItem:
    return PlanCheckItem(check_id, severity, status, message, gate_check)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _design_of(params: Mapping[str, Any]) -> tuple[str | None, bool]:
    """``(design, readable)``: ``(None, True)`` when nothing is declared."""
    design = params.get("design")
    if design is None:
        return None, True
    if isinstance(design, str) and design in ROUND_DESIGNS:
        return design, True
    return None, False


def room_seed_named(params: Mapping[str, Any]) -> str | None:
    """The room seed the params name (``seeds.rooms`` or ``admission_seed``),
    or None."""
    seeds = params.get("seeds")
    if isinstance(seeds, Mapping) and _nonempty_str(seeds.get("rooms")):
        return str(seeds["rooms"])
    if _nonempty_str(params.get("admission_seed")):
        return str(params["admission_seed"])
    return None


# ---------------------------------------------------------------------------
# the aiif_round plan suite
# ---------------------------------------------------------------------------


def _declarations_readable(ctx: PlanContext) -> PlanCheckItem:
    params = ctx.params
    problems: list[str] = []
    design = params.get("design")
    if design is not None and not (isinstance(design, str) and design in ROUND_DESIGNS):
        problems.append(f"design={design!r} (must be one of {list(ROUND_DESIGNS)} or left out)")
    for key in ("rooms", "report_p_values"):
        value = params.get(key)
        if value is not None and not isinstance(value, bool):
            problems.append(f"{key}={value!r} (must be a JSON true or false, or left out)")
    count = params.get("control_count")
    if count is not None and (not _is_int(count) or count < 0):
        problems.append(f"control_count={count!r} (must be a whole number >= 0, or left out)")
    if problems:
        return _item(
            "declarations_readable", SEVERITY_MUST, STATUS_FAIL,
            "the params declare values the gate cannot read, and a declaration it cannot read fails every gate "
            "check that reads it: " + "; ".join(problems),
            None,
        )
    return _item(
        "declarations_readable", SEVERITY_MUST, STATUS_PASS,
        "every declaration in the params (design, rooms, report_p_values, control_count) is readable", None,
    )


def _control_plan_consistent(ctx: PlanContext) -> PlanCheckItem:
    """``control_lens_names`` names what DENOTES the control, not the seated
    controls: it may hold spare or reserved names. The gate counts roster rows
    seated as control and never reads the list, so this item never refuses a
    list the gate would accept; it checks what the gate WILL decide."""
    gate = "control_arm_present"
    params = ctx.params
    design, readable = _design_of(params)
    if not readable:
        return _item(
            "control_plan_consistent", SEVERITY_MUST, STATUS_NOT_APPLICABLE,
            "the design declaration is unreadable; see declarations_readable", gate,
        )
    names = params.get("control_lens_names")
    if design == DESIGN_NONE:
        return _item(
            "control_plan_consistent", SEVERITY_MUST, STATUS_NOT_APPLICABLE,
            "design 'none': the round makes no control comparison", gate,
        )
    if design == DESIGN_PAIRED:
        return _item(
            "control_plan_consistent", SEVERITY_MUST, STATUS_PASS,
            "design 'paired': pairing is decided from the launches at the gate, not from the params", gate,
        )
    if names is not None and not isinstance(names, (list, tuple)):
        return _item(
            "control_plan_consistent", SEVERITY_MUST, STATUS_FAIL,
            f"control_lens_names={names!r} is not a list of lens names", gate,
        )
    if design == DESIGN_CONTROL_SEATS:
        count = params.get("control_count")
        if not _is_int(count) or count < 1:
            return _item(
                "control_plan_consistent", SEVERITY_MUST, STATUS_FAIL,
                f"design 'control_seats' declares control_count={count!r}; the gate needs a whole number >= 1, "
                "the number of control seats the params escrow", gate,
            )
        if names is not None and len(names) < count:
            return _item(
                "control_plan_consistent", SEVERITY_MUST, STATUS_FAIL,
                f"design 'control_seats' declares control_count={count} but control_lens_names lists only "
                f"{len(names)} name(s); name at least {count}", gate,
            )
        spare = names is not None and len(names) > count
        return _item(
            "control_plan_consistent", SEVERITY_MUST, STATUS_PASS,
            f"design 'control_seats': {count} control seat(s) planned"
            + (f"; {len(names)} names are reserved for the control, spare names are fine and the gate will "
               f"require exactly {count} control seats in the roster" if spare else ""),
            gate,
        )
    # design absent: the gate's default rule is exactly one seated control
    count = params.get("control_count")
    if _is_int(count) and count != 1:
        return _item(
            "control_plan_consistent", SEVERITY_MUST, STATUS_FAIL,
            f"no design is declared, so the gate will require exactly one seated control, but control_count="
            f"{count}. Declare design 'control_seats' (with control_count), or drop control_count or set it to 1",
            gate,
        )
    if names is None:
        return _item(
            "control_plan_consistent", SEVERITY_MUST, STATUS_PASS,
            "no design is declared and no control lens is named; the gate will require exactly one seated control",
            gate,
        )
    if len(names) == 0:
        return _item(
            "control_plan_consistent", SEVERITY_MUST, STATUS_FAIL,
            "no control lens is named, and the gate will require exactly one seated control", gate,
        )
    if len(names) == 1:
        return _item(
            "control_plan_consistent", SEVERITY_MUST, STATUS_PASS,
            "no design is declared; one control lens is named, and the gate will require exactly one seated control",
            gate,
        )
    return _item(
        "control_plan_consistent", SEVERITY_MUST, STATUS_PASS,
        f"no design is declared; {len(names)} names are reserved for the control; spare names are fine, but the "
        "gate will require exactly one seated control in the roster, so only one may be seated", gate,
    )


def _admission_escrow_planned(ctx: PlanContext) -> PlanCheckItem:
    """The known trap: a round that runs rooms must plan the escrow of the
    room order, or the gate's ``admission_order_hash_matches`` fails after the
    data exists."""
    gate = "admission_order_hash_matches"
    params = ctx.params
    if params.get("rooms") is False:
        return _item(
            "admission_escrow_planned", SEVERITY_MUST, STATUS_NOT_APPLICABLE,
            "rooms=false is declared: no room order is drawn, so none needs escrowing", gate,
        )
    problems: list[str] = []
    if room_seed_named(params) is None:
        problems.append(
            "no room seed is named (neither seeds.rooms nor admission_seed), so the order cannot be reproduced"
        )
    order_hash = params.get("admission_order_hash")
    escrow = params.get("admission_escrow")
    has_hash = isinstance(order_hash, str) and bool(_HEX64.fullmatch(order_hash))
    has_second = isinstance(escrow, Mapping) and escrow.get("by") == "second_prereg_commit"
    if not (has_hash or has_second):
        if order_hash is not None:
            problems.append(
                f"admission_order_hash={order_hash!r} is not a 64-character lower-case hex string, and no second escrow is declared; "
                "the gate's `admission_order_hash_matches` will fail after the data exists. Give the 64-character lower-case hex hash "
                "`trialerror room admission-order` prints, declare `rooms: false`, or declare "
                "`admission_escrow: {by: second_prereg_commit}`"
            )
        else:
            problems.append(
                "the round runs rooms, but the params neither carry `admission_order_hash` nor declare "
                "`admission_escrow: {by: second_prereg_commit}`; the gate's `admission_order_hash_matches` will "
                "fail after the data exists. Declare `rooms: false`, or declare the second escrow"
            )
    if problems:
        return _item("admission_escrow_planned", SEVERITY_MUST, STATUS_FAIL, "; ".join(problems), gate)
    how = "a 64-hex admission_order_hash in the params" if has_hash else "a declared second prereg commit"
    return _item(
        "admission_escrow_planned", SEVERITY_MUST, STATUS_PASS,
        f"the room order is planned to be escrowed by {how}, with a named room seed", gate,
    )


def _models_floors_met(ctx: PlanContext) -> PlanCheckItem:
    gate = "models_table_present"
    table = ctx.models_table or {}
    missing = sorted(p for p in AIIF_MODEL_FLOORS if p not in table)
    below = sorted(
        f"{p}={table[p]!r}<{floor}"
        for p, floor in AIIF_MODEL_FLOORS.items()
        if p in table and not meets_minimum(str(table[p]), floor)
    )
    if missing or below:
        return _item(
            "models_floors_met", SEVERITY_MUST, STATUS_FAIL,
            f"the program's [models] table is incomplete or below the framework floors -- missing: "
            f"{missing or 'nothing'}; below floor: {below or 'nothing'}. The gate will refuse a round booked "
            "against it",
            gate,
        )
    return _item(
        "models_floors_met", SEVERITY_MUST, STATUS_PASS,
        f"all {len(AIIF_MODEL_FLOORS)} purposes carry their floor or better", gate,
    )


def _arm_mode_declared(ctx: PlanContext) -> PlanCheckItem:
    gate = "arm_mode_declared"
    mode = ctx.params.get("arm_mode")
    if not _nonempty_str(mode):
        return _item(
            "arm_mode_declared", SEVERITY_MUST, STATUS_FAIL,
            f"the params carry arm_mode={mode!r}; the gate compares the arm mode the round ran with the one it "
            "pre-registered, so a non-empty arm_mode must be in the params", gate,
        )
    return _item("arm_mode_declared", SEVERITY_MUST, STATUS_PASS, f"arm_mode={mode!r} is declared", gate)


def _card_cells_feasible(ctx: PlanContext) -> PlanCheckItem:
    gate = "card_cells_ge_2"
    cards = ctx.params.get("cards")
    if not isinstance(cards, Mapping):
        return _item(
            "card_cells_feasible", SEVERITY_WARN, STATUS_FAIL,
            "cards not escrowed; the gate will decide", gate,
        )
    holders: dict[str, set[str]] = {}
    unreadable: list[str] = []
    for lens, held in cards.items():
        if not isinstance(held, (list, tuple)):
            unreadable.append(str(lens))
            continue
        for card in held:
            holders.setdefault(str(card), set()).add(str(lens))
    if unreadable:
        return _item(
            "card_cells_feasible", SEVERITY_MUST, STATUS_FAIL,
            f"cards lists a non-list value for lens(es) {sorted(unreadable)}; each lens maps to a list of card names",
            gate,
        )
    counted = {c: len(ls) for c, ls in holders.items() if c != BUSTER_ONLY_CARD}
    if not counted:
        return _item(
            "card_cells_feasible", SEVERITY_MUST, STATUS_FAIL,
            "cards names no card for any lens other than the buster's; the gate's card_cells_ge_2 fails a round "
            "whose standard lenses hold no recipe card", gate,
        )
    thin = sorted(f"{c}={n}" for c, n in counted.items() if n < MIN_LENS_CELLS_PER_CARD)
    if thin:
        return _item(
            "card_cells_feasible", SEVERITY_MUST, STATUS_FAIL,
            f"card cells below {MIN_LENS_CELLS_PER_CARD} lenses: {thin}; the gate's card_cells_ge_2 needs every "
            f"card except {BUSTER_ONLY_CARD} held by at least {MIN_LENS_CELLS_PER_CARD} lenses", gate,
        )
    return _item(
        "card_cells_feasible", SEVERITY_MUST, STATUS_PASS,
        f"every card except {BUSTER_ONLY_CARD} is held by >= {MIN_LENS_CELLS_PER_CARD} lenses ({counted})", gate,
    )


def _reference_sets_named(ctx: PlanContext) -> PlanCheckItem:
    gate = "novelty_bundle_complete"
    refs = ctx.params.get("reference_set_hashes")
    if isinstance(refs, Mapping):
        absent = [k for k in _REFERENCE_SET_KEYS if not refs.get(k)]
        if not absent:
            return _item("reference_sets_named", SEVERITY_WARN, STATUS_PASS, "reference sets R1..R5 are named", gate)
        detail = f"reference_set_hashes lacks a value for {absent}"
    else:
        detail = "the params carry no reference_set_hashes mapping"
    return _item(
        "reference_sets_named", SEVERITY_WARN, STATUS_FAIL,
        f"{detail}; the novelty bundle names its reference sets R1..R5, and naming them after the data exists "
        "is choosing them afterwards", gate,
    )


def _plants_declared(ctx: PlanContext) -> PlanCheckItem:
    gate = "plants_caught"
    plants = ctx.params.get("plants")
    if isinstance(plants, Mapping) and plants:
        return _item("plants_declared", SEVERITY_WARN, STATUS_PASS, "a plant plan is declared", gate)
    return _item(
        "plants_declared", SEVERITY_WARN, STATUS_FAIL,
        "the params declare no plants; the gate's plants_caught will have no planned plants to count", gate,
    )


def _aiif_round_suite(ctx: PlanContext) -> tuple[PlanCheckItem, ...]:
    return (
        _declarations_readable(ctx),
        _control_plan_consistent(ctx),
        _admission_escrow_planned(ctx),
        _models_floors_met(ctx),
        _arm_mode_declared(ctx),
        _card_cells_feasible(ctx),
        _reference_sets_named(ctx),
        _plants_declared(ctx),
    )


# ---------------------------------------------------------------------------
# the aiif_round_admission plan suite (the second escrow)
# ---------------------------------------------------------------------------


def _parent_plan_check(parent: Mapping[str, Any]) -> Mapping[str, Any]:
    """The parent's recorded plan check as a mapping. The ``plan_check``
    column holds JSON text; a caller may pass it already parsed."""
    import json

    raw = parent.get("plan_check")
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return {}


def _parent_is_round_prereg(ctx: PlanContext) -> PlanCheckItem:
    parent = ctx.parent
    if parent is None:
        return _item(
            "parent_is_round_prereg", SEVERITY_MUST, STATUS_FAIL,
            "no parent prereg was given; the second escrow points at the round's first prereg "
            "(--parent-prereg)", None,
        )
    problems: list[str] = []
    status = parent.get("status")
    if status not in PREREG_USABLE_STATUSES:
        problems.append(f"the parent prereg is {status!r}; only {sorted(PREREG_USABLE_STATUSES)} can be built on")
    if parent.get("plan_suite") != PLAN_SUITE_ROUND:
        problems.append(
            f"the parent prereg was not committed under the {PLAN_SUITE_ROUND!r} plan suite "
            f"(it has {parent.get('plan_suite')!r})"
        )
    else:
        if _parent_plan_check(parent).get("rooms_declared") is False:
            problems.append("the round declared no rooms (rooms: false), so it has no room order to escrow")
    if ctx.round_id is None or ctx.round_id != parent.get("round_id"):
        problems.append(
            f"this commit is for round {ctx.round_id!r} but the parent prereg is for round {parent.get('round_id')!r}"
        )
    if problems:
        return _item("parent_is_round_prereg", SEVERITY_MUST, STATUS_FAIL, "; ".join(problems), None)
    return _item(
        "parent_is_round_prereg", SEVERITY_MUST, STATUS_PASS,
        f"parent prereg {parent.get('prereg_id')} is the round's first prereg and the round runs rooms", None,
    )


def _order_hash_present(ctx: PlanContext) -> PlanCheckItem:
    value = ctx.params.get("admission_order_hash")
    if isinstance(value, str) and _HEX64.fullmatch(value):
        return _item(
            "order_hash_present", SEVERITY_MUST, STATUS_PASS, "admission_order_hash is a 64-hex string",
            "admission_order_hash_matches",
        )
    return _item(
        "order_hash_present", SEVERITY_MUST, STATUS_FAIL,
        f"params['admission_order_hash']={value!r} is not a 64-character lower-case hex string; the second escrow exists to carry the "
        "room order's hash, which `trialerror room admission-order` prints, and the gate's "
        "`admission_order_hash_matches` will fail after the data exists without it. Two ways forward: commit again "
        "with the hash `trialerror room admission-order` printed, or accept a deviation with an operator decision",
        "admission_order_hash_matches",
    )


def _order_recomputed(ctx: PlanContext) -> PlanCheckItem:
    """Optional recompute of the order from the round's consolidated pool and
    the seed the parent escrowed. Reading the seed does not break the blind:
    the seed is not what is hidden."""
    cid = "order_recomputed"
    gate = "admission_order_hash_matches"
    seed = _parent_plan_check(ctx.parent).get("room_seed") if ctx.parent is not None else None
    if ctx.store is None or not _nonempty_str(seed) or not ctx.round_id:
        return _item(cid, SEVERITY_WARN, STATUS_PASS, "not recomputed", gate)
    given = ctx.params.get("admission_order_hash")
    try:
        # Imported at call time: the rooms module pulls in far more than the
        # plan check otherwise needs.
        from trialerror.rooms.api import build_admission_order, consolidated_ideas_for_admission

        ideas = consolidated_ideas_for_admission(ctx.store, round_id=ctx.round_id)
        if not ideas:
            return _item(cid, SEVERITY_WARN, STATUS_PASS, "not recomputed (the round has no consolidated pool)", gate)
        recomputed = build_admission_order(ideas, seed=str(seed), enforce_batch_band=False)["hash"]
    except Exception as exc:  # the pool is optional context; an unreadable one is "not recomputed"
        return _item(cid, SEVERITY_WARN, STATUS_PASS, f"not recomputed ({type(exc).__name__}: {exc})", gate)
    if str(recomputed) != str(given):
        return _item(
            cid, SEVERITY_WARN, STATUS_FAIL,
            f"THE HASH DOES NOT MATCH: recomputing the room order from the round's consolidated pool with the "
            f"escrowed seed gives {str(recomputed)[:16]}..., but this commit carries {str(given)[:16]}.... The "
            "gate's admission_order_hash_matches compares the order the round runs with this hash",
            gate,
        )
    return _item(cid, SEVERITY_WARN, STATUS_PASS, "the order recomputed from the pool matches the hash", gate)


def _aiif_round_admission_suite(ctx: PlanContext) -> tuple[PlanCheckItem, ...]:
    return (_parent_is_round_prereg(ctx), _order_hash_present(ctx), _order_recomputed(ctx))


register_plan_suite(PLAN_SUITE_ROUND, _aiif_round_suite)
register_plan_suite(PLAN_SUITE_ADMISSION, _aiif_round_admission_suite)
