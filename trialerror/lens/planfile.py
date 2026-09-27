"""``lens assign --plan-file``: write a round's ``lens_assignment`` rows from a
plan somebody else drew, and prove they landed as planned.

``lens assign``'s own mode DRAWS the slices (stratify, seeded quota draw,
write). This mode draws nothing. A round whose design is decided by a planner
of its own -- which lens reads which documents, in which arm, in which order --
hands the harness that decision as a plan file, and this module turns it into
the same rows ``lens assign`` writes, in exactly the same shape
(:func:`trialerror.lens.assign.run_assignment`), so every doctor check, ``lens
log``, ``lens export`` and the retrieval scope read them without knowing which
mode wrote them.

Three steps, in this order, and nothing is written until the first is done:

1. **Validate everything first** (:func:`validate_plan`, checks V1-V8 below).
   Each refusal names the check and the items that failed it.
2. **Write in one transaction** (:func:`write_plan`): lenses in name order,
   rows in rank order, one ``BEGIN IMMEDIATE`` for the lot.
3. **Read the rows back** inside the same transaction and compare them with
   the file, column by column and as the projection below. Any difference
   rolls the whole transaction back and refuses (``plan_readback_mismatch``):
   a round never holds rows that differ from the plan it was given.

The plan file (``trialerror-plan-file/1``), every key always present::

    {"format": "trialerror-plan-file/1", "round_id", "seed", "weights",
     "far_lens_floor", "arm_mode": "per_lens", "inter_cluster_mandate",
     "salt_scheme",
     "lenses": [{"lens_name", "arm", "far_floor",
                 "rows": [{"candidate_id", "cluster_id", "rank",
                           "distance_score", "extra": {...}}]}],
     "annex": {...} | null,
     "plan_sha256"}

``annex`` is the planner's own record. It is covered by ``plan_sha256`` and
never interpreted here. ``extra`` rides into each row's ``slice_spec`` beside
the keys this module writes itself (:data:`RESERVED_EXTRA_KEYS`), which it may
not reuse.

The checks, all before any write:

- **V1** ``format`` is :data:`PLAN_FILE_FORMAT` and ``round_id`` is the round
  the caller named.
- **V2** the plan's lens names are exactly the round's roster names: no roster
  lens without rows, no rows for a name the roster does not hold.
- **V3** every ``candidate_id`` names a document whose source is not the
  inventory kind, no id appears twice, and the round has no assignment row yet
  (a plan is written once).
- **V4** arms: the assumption-buster seat is far; a control seat sits in the
  modal arm of the non-control quota; the non-control arm counts equal
  ``compute_quota_counts(n_non_control, weights, far_floor=far_lens_floor)``;
  and at least ``max(far_lens_floor, 2)`` non-control lenses are far.
- **V5** a far lens's ``far_floor`` is its row count; every other lens's is 0.
- **V6** each lens's ranks are exactly ``0 .. n-1``.
- **V7** no ``extra`` uses a reserved key.
- **V8** ``plan_sha256`` is the hash of the plan without that key, and equals
  the caller's ``--expect-plan-sha256`` when one is given.

Hashes are lower-case hex SHA-256 over the canonical JSON the rest of the
lens package hashes (sorted keys, ``,``/``:`` separators, UTF-8, no trailing
newline -- :func:`trialerror.lens.assign._canonical_json`).
``projection_sha256`` is the hash of the rows as read back, projected to
``[{lens_name, arm, candidate_id, cluster_id, rank, distance_score, extra}]``
sorted by ``(lens_name, rank)``; a planner that records the same projection's
hash can compare it with the one this module returns.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

from trialerror.ingest.pipeline import INVENTORY_SOURCE_KIND
from trialerror.lens.assign import SALT_SCHEME_KEY, SALT_SCHEMES, _canonical_json, modal_arm
from trialerror.lens.checks import MIN_FAR_LENSES
from trialerror.lens.errors import LensError
from trialerror.lens.quota import compute_quota_counts
from trialerror.lens.roster import list_roster
from trialerror.lens.stratify import ARMS
from trialerror.stores.errors import XidTargetMissingError
from trialerror.stores.store import Store
from trialerror.stores.writer import require_xid_targets, table_columns
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

__all__ = [
    "PLAN_FILE_FORMAT",
    "PLAN_KEYS",
    "LENS_KEYS",
    "ROW_KEYS",
    "RESERVED_EXTRA_KEYS",
    "PROJECTION_KEYS",
    "PlanFileRefusedError",
    "canonical_bytes",
    "sha256_of",
    "plan_sha256",
    "plan_projection",
    "load_plan_file",
    "validate_plan",
    "write_plan",
]

#: The one format string this module reads.
PLAN_FILE_FORMAT = "trialerror-plan-file/1"

#: The plan's top-level keys, every one required, no other accepted.
PLAN_KEYS: tuple[str, ...] = (
    "format", "round_id", "seed", "weights", "far_lens_floor", "arm_mode",
    "inter_cluster_mandate", "salt_scheme", "lenses", "annex", "plan_sha256",
)

#: One lens entry's keys.
LENS_KEYS: tuple[str, ...] = ("lens_name", "arm", "far_floor", "rows")

#: One row entry's keys.
ROW_KEYS: tuple[str, ...] = ("candidate_id", "cluster_id", "rank", "distance_score", "extra")

#: The ``slice_spec`` keys this module writes itself, which a row's ``extra``
#: may therefore not use: a key in both places would be overwritten by one of
#: them, and the read-back could not tell which the plan meant.
RESERVED_EXTRA_KEYS: tuple[str, ...] = (
    "round_id", "candidate_id", "distance_score", "cluster_id", "rank", SALT_SCHEME_KEY, "plan_sha256",
)

#: The fields of one projected row, the unit the read-back compares and
#: ``projection_sha256`` hashes.
PROJECTION_KEYS: tuple[str, ...] = (
    "lens_name", "arm", "candidate_id", "cluster_id", "rank", "distance_score", "extra",
)

#: The only arm mode a plan file can carry: V4 and V5 are the per-lens rules,
#: and a per-slice plan has no per-lens arm for them to judge.
_PLAN_ARM_MODE = "per_lens"

#: How many offending items a refusal's message spells out; ``items`` in the
#: error details always carries all of them.
_MESSAGE_ITEMS = 12

#: SQLite's default host-parameter ceiling is 999 on older builds; stay under it.
_IN_CHUNK = 500


class PlanFileRefusedError(LensError):
    """A plan file refused, before any write or by its read-back.

    ``code`` is the envelope's error code, ``check`` names the rule (``V1`` ..
    ``V8``, ``shape`` for the file's structure, ``read`` for a file that
    cannot be read, ``launch`` for an unknown ``--launch-id``, ``read-back``
    for a write that did not read back as planned), and ``items`` lists every
    offending item, each naming where it sits."""

    def __init__(self, code: str, check: str, message: str, items: Sequence[str] = ()) -> None:
        self.code = code
        self.check = check
        self.items = list(items)
        shown = self.items[:_MESSAGE_ITEMS]
        more = len(self.items) - len(shown)
        text = f"{check}: {message}"
        if shown:
            text += ": " + "; ".join(shown) + (f"; and {more} more" if more > 0 else "")
        super().__init__(text)

    def details(self) -> dict[str, Any]:
        return {"check": self.check, "items": list(self.items)}


# ---------------------------------------------------------------------------
# hashes and the projection
# ---------------------------------------------------------------------------


def canonical_bytes(obj: Any) -> bytes:
    """The bytes every hash here is taken over."""
    return _canonical_json(obj).encode("utf-8")


def sha256_of(obj: Any) -> str:
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()


def plan_sha256(plan: Mapping[str, Any]) -> str:
    """The hash a plan's ``plan_sha256`` must carry: the plan without that key."""
    return sha256_of({k: v for k, v in plan.items() if k != "plan_sha256"})


def plan_projection(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The rows a plan asks for, as the read-back will see them, sorted by
    ``(lens_name, rank)``."""
    out = [
        {
            "lens_name": lens["lens_name"],
            "arm": lens["arm"],
            "candidate_id": row["candidate_id"],
            "cluster_id": row["cluster_id"],
            "rank": row["rank"],
            "distance_score": row["distance_score"],
            "extra": row["extra"],
        }
        for lens in plan["lenses"]
        for row in lens["rows"]
    ]
    out.sort(key=lambda r: (r["lens_name"], r["rank"]))
    return out


# ---------------------------------------------------------------------------
# reading the file
# ---------------------------------------------------------------------------


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"key {key!r} appears twice in one object")
        seen[key] = value
    return seen


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not a JSON number")


def load_plan_file(path: Path | str) -> tuple[dict[str, Any], bytes]:
    """``(plan, raw bytes)``. Strict JSON: UTF-8, one object, no key twice in
    any object (a repeated key would be silently resolved to its last value),
    no ``NaN``/``Infinity``, no number too large for a float, no lone
    surrogate."""
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError as exc:
        raise PlanFileRefusedError(
            "plan_file_unreadable", "read", f"cannot read the plan file {str(p)!r} ({exc.strerror or exc})"
        ) from exc
    try:
        plan = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys, parse_constant=_no_constant
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise PlanFileRefusedError(
            "plan_file_unreadable", "read", f"the plan file {str(p)!r} is not strict UTF-8 JSON ({exc})"
        ) from exc
    # A literal such as 1e400 parses to inf without reaching parse_constant, and "\ud800" loads into a
    # str that UTF-8 cannot carry: re-encode strictly, so neither reaches slice_spec or crashes at V8.
    try:
        json.dumps(plan, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PlanFileRefusedError(
            "plan_file_unreadable", "read",
            f"the plan file {str(p)!r} holds a lone surrogate, which UTF-8 cannot carry ({exc.reason})",
        ) from exc
    except ValueError as exc:
        raise PlanFileRefusedError(
            "plan_file_unreadable", "read", f"the plan file {str(p)!r} holds a number too large for a float ({exc})"
        ) from exc
    if not isinstance(plan, dict):
        raise PlanFileRefusedError(
            "plan_malformed", "shape", f"the plan file holds a {type(plan).__name__}, not a JSON object"
        )
    return plan, raw


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _key_items(where: str, obj: Mapping[str, Any], keys: Sequence[str]) -> list[str]:
    missing = [k for k in keys if k not in obj]
    unknown = sorted(k for k in obj if k not in keys)
    items = [f"{where}: missing key {k!r}" for k in missing]
    items += [f"{where}: unknown key {k!r}" for k in unknown]
    return items


def _check_shape(plan: Mapping[str, Any]) -> None:
    """Every key present with a value of the right kind, before any rule
    reads one. Refuses with ``plan_malformed``."""
    items = _key_items("plan", plan, PLAN_KEYS)
    if items:
        raise PlanFileRefusedError("plan_malformed", "shape", "the plan's top-level keys", items)

    if not isinstance(plan["format"], str):
        items.append("format: not a string")
    if not _nonempty_str(plan["round_id"]):
        items.append("round_id: not a non-empty string")
    seed = plan["seed"]
    if not (_nonempty_str(seed) or _is_int(seed)):
        items.append("seed: not a non-empty string or an integer")
    weights = plan["weights"]
    if not (
        isinstance(weights, list) and len(weights) == 3
        and all(_is_int(w) and w >= 0 for w in weights) and sum(weights) > 0
    ):
        items.append("weights: not three non-negative integers (near, moderate, far) with a positive sum")
    if not (_is_int(plan["far_lens_floor"]) and plan["far_lens_floor"] >= 0):
        items.append("far_lens_floor: not a non-negative integer")
    if plan["arm_mode"] != _PLAN_ARM_MODE:
        items.append(f"arm_mode: {plan['arm_mode']!r}, and a plan file is {_PLAN_ARM_MODE!r} only")
    if not isinstance(plan["inter_cluster_mandate"], bool):
        items.append("inter_cluster_mandate: not true or false")
    if plan["salt_scheme"] not in SALT_SCHEMES:
        items.append(f"salt_scheme: {plan['salt_scheme']!r} is not one of {list(SALT_SCHEMES)!r}")
    if not (plan["annex"] is None or isinstance(plan["annex"], dict)):
        items.append("annex: not an object or null")
    if not isinstance(plan["plan_sha256"], str):
        items.append("plan_sha256: not a string")

    lenses = plan["lenses"]
    if not isinstance(lenses, list) or not lenses:
        items.append("lenses: not a non-empty list")
        lenses = []
    for i, lens in enumerate(lenses):
        where = f"lenses[{i}]"
        if not isinstance(lens, dict):
            items.append(f"{where}: not an object")
            continue
        key_items = _key_items(where, lens, LENS_KEYS)
        if key_items:
            items.extend(key_items)
            continue
        if not _nonempty_str(lens["lens_name"]):
            items.append(f"{where}.lens_name: not a non-empty string")
        else:
            where = f"lens {lens['lens_name']!r}"
        if lens["arm"] not in ARMS:
            items.append(f"{where}.arm: {lens['arm']!r} is not one of {list(ARMS)!r}")
        if not (_is_int(lens["far_floor"]) and lens["far_floor"] >= 0):
            items.append(f"{where}.far_floor: not a non-negative integer")
        rows = lens["rows"]
        if not isinstance(rows, list):
            items.append(f"{where}.rows: not a list")
            continue
        for j, row in enumerate(rows):
            rwhere = f"{where}.rows[{j}]"
            if not isinstance(row, dict):
                items.append(f"{rwhere}: not an object")
                continue
            key_items = _key_items(rwhere, row, ROW_KEYS)
            if key_items:
                items.extend(key_items)
                continue
            if not _nonempty_str(row["candidate_id"]):
                items.append(f"{rwhere}.candidate_id: not a non-empty string")
            if not (row["cluster_id"] is None or isinstance(row["cluster_id"], str)):
                items.append(f"{rwhere}.cluster_id: not a string or null")
            if not (_is_int(row["rank"]) and row["rank"] >= 0):
                items.append(f"{rwhere}.rank: not a non-negative integer")
            score = row["distance_score"]
            if not (score is None or isinstance(score, str) or _is_number(score)):
                items.append(f"{rwhere}.distance_score: not a finite number, a string or null")
            if not isinstance(row["extra"], dict):
                items.append(f"{rwhere}.extra: not an object")
    if items:
        raise PlanFileRefusedError("plan_malformed", "shape", "the plan file's structure", items)


def _documents(store: Store, doc_ids: Sequence[str]) -> dict[str, str | None]:
    """``doc_id -> its source's kind`` for every id that names a document."""
    out: dict[str, str | None] = {}
    ids = list(doc_ids)
    for start in range(0, len(ids), _IN_CHUNK):
        chunk = ids[start:start + _IN_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        rows = store.knowledge.execute(
            f"SELECT d.doc_id AS doc_id, s.kind AS kind FROM document d "
            f"LEFT JOIN source s ON s.source_id = d.source_id WHERE d.doc_id IN ({placeholders})",
            chunk,
        ).fetchall()
        for row in rows:
            out[row["doc_id"]] = row["kind"]
    return out


def _round_assignment_count(conn: sqlite3.Connection, round_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM lens_assignment a JOIN lens_roster r ON a.roster_id = r.roster_id "
        "WHERE r.round_id = ?",
        (round_id,),
    ).fetchone()["n"]


def _refuse_if_assigned(conn: sqlite3.Connection, round_id: str) -> None:
    n = _round_assignment_count(conn, round_id)
    if n:
        raise PlanFileRefusedError(
            "plan_round_already_assigned", "V3",
            f"round {round_id!r} already holds {n} lens_assignment row(s), and a plan is written once",
            [f"round {round_id!r}: {n} existing row(s)"],
        )


def validate_plan(
    store: Store,
    plan: Mapping[str, Any],
    *,
    round_id: str,
    expect_plan_sha256: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Run the shape check and V1-V8 against ``store``, in that order; raise
    :class:`PlanFileRefusedError` on the first check that fails. Returns the
    round's roster rows keyed by ``lens_name`` -- what :func:`write_plan`
    writes against. Reads only."""
    # V1's format half comes before the shape check: another format version has another key set, and is
    # refused as "not this format", not as an unknown key.
    if isinstance(plan, Mapping) and plan.get("format") != PLAN_FILE_FORMAT:
        raise PlanFileRefusedError(
            "plan_format_mismatch", "V1", "the plan is not this format or not this round",
            [f"format: {plan.get('format')!r}, expected {PLAN_FILE_FORMAT!r}"],
        )
    _check_shape(plan)
    lenses: list[dict[str, Any]] = list(plan["lenses"])

    # V1 -- the format, and the round the caller named.
    items = []
    if plan["format"] != PLAN_FILE_FORMAT:
        items.append(f"format: {plan['format']!r}, expected {PLAN_FILE_FORMAT!r}")
    if plan["round_id"] != round_id:
        items.append(f"round_id: the plan is for {plan['round_id']!r}, the command names {round_id!r}")
    if items:
        raise PlanFileRefusedError("plan_format_mismatch", "V1", "the plan is not this format or not this round", items)

    # V2 -- the plan's lenses are exactly the round's roster.
    roster = list_roster(store, round_id=round_id)
    roster_counts = Counter(str(r["lens_name"]) for r in roster)
    plan_counts = Counter(lens["lens_name"] for lens in lenses)
    items = [f"roster: lens name {n!r} is held by {c} roster rows" for n, c in sorted(roster_counts.items()) if c > 1]
    items += [f"plan: lens name {n!r} appears {c} times" for n, c in sorted(plan_counts.items()) if c > 1]
    items += [f"lens {n!r}: on the roster, not in the plan" for n in sorted(set(roster_counts) - set(plan_counts))]
    items += [f"lens {n!r}: in the plan, not on the roster" for n in sorted(set(plan_counts) - set(roster_counts))]
    items += [f"lens {lens['lens_name']!r}: no rows" for lens in lenses if not lens["rows"]]
    if items:
        raise PlanFileRefusedError(
            "plan_roster_mismatch", "V2",
            f"the plan's lenses must be exactly round {round_id!r}'s roster, each with rows", items,
        )
    roster_by_name = {str(r["lens_name"]): r for r in roster}

    # V3 -- documents that exist, none of the inventory kind, none twice; a
    # round assigned once.
    placements: dict[str, list[str]] = {}
    for lens in lenses:
        for row in lens["rows"]:
            placements.setdefault(row["candidate_id"], []).append(f"{lens['lens_name']}#{row['rank']}")
    items = [
        f"candidate {cid!r}: appears {len(at)} times ({', '.join(at)})"
        for cid, at in sorted(placements.items()) if len(at) > 1
    ]
    kinds = _documents(store, sorted(placements))
    items += [
        f"candidate {cid!r} ({placements[cid][0]}): names no document"
        for cid in sorted(placements) if cid not in kinds
    ]
    items += [
        f"candidate {cid!r} ({placements[cid][0]}): a document of source kind {INVENTORY_SOURCE_KIND!r}"
        for cid in sorted(placements) if kinds.get(cid) == INVENTORY_SOURCE_KIND
    ]
    if items:
        raise PlanFileRefusedError(
            "plan_candidate_refused", "V3",
            "every candidate must name one non-inventory document, once", items,
        )
    _refuse_if_assigned(store.ops, round_id)

    # V4 -- the arms: seats first, then the quota over the non-control seats.
    weights = tuple(plan["weights"])
    far_lens_floor = plan["far_lens_floor"]
    seat_of = {name: str(row.get("seat") or "standard") for name, row in roster_by_name.items()}
    non_control = [lens for lens in lenses if seat_of[lens["lens_name"]] != "control"]
    quota = compute_quota_counts(len(non_control), weights=weights, far_floor=far_lens_floor)
    counts = Counter(lens["arm"] for lens in non_control)
    modal = modal_arm(quota)
    items = []
    for lens in sorted(lenses, key=lambda l: l["lens_name"]):
        seat = seat_of[lens["lens_name"]]
        if seat == "assumption_buster" and lens["arm"] != "far":
            items.append(f"lens {lens['lens_name']!r}: the assumption-buster seat must be far, the plan says {lens['arm']!r}")
        if seat == "control" and lens["arm"] != modal:
            items.append(
                f"lens {lens['lens_name']!r}: a control seat sits in the modal arm of the non-control quota "
                f"({modal!r}), the plan says {lens['arm']!r}"
            )
    for arm in ARMS:
        if counts.get(arm, 0) != quota.get(arm, 0):
            items.append(
                f"arm {arm!r}: {counts.get(arm, 0)} non-control lens(es), the quota "
                f"compute_quota_counts({len(non_control)}, weights={list(weights)}, far_floor={far_lens_floor}) "
                f"gives {quota.get(arm, 0)}"
            )
    far_needed = max(far_lens_floor, MIN_FAR_LENSES)
    if counts.get("far", 0) < far_needed:
        items.append(
            f"far lenses: {counts.get('far', 0)} non-control lens(es) are far, at least {far_needed} must be "
            f"(the harder of far_lens_floor={far_lens_floor} and the hard floor {MIN_FAR_LENSES})"
        )
    if items:
        raise PlanFileRefusedError("plan_arms_refused", "V4", "the plan's arms break the seat or quota rules", items)

    # V5 -- the per-lens far floor the far_arm_floor_honored check reads.
    items = []
    for lens in sorted(lenses, key=lambda l: l["lens_name"]):
        expected = len(lens["rows"]) if lens["arm"] == "far" else 0
        if lens["far_floor"] != expected:
            items.append(
                f"lens {lens['lens_name']!r} ({lens['arm']}): far_floor {lens['far_floor']}, expected {expected}"
            )
    if items:
        raise PlanFileRefusedError(
            "plan_far_floor_mismatch", "V5",
            "a far lens's far_floor is its row count, every other lens's is 0", items,
        )

    # V6 -- ranks 0 .. n-1 per lens.
    items = []
    for lens in sorted(lenses, key=lambda l: l["lens_name"]):
        ranks = sorted(row["rank"] for row in lens["rows"])
        if ranks != list(range(len(ranks))):
            items.append(f"lens {lens['lens_name']!r}: ranks {ranks}, expected 0..{len(ranks) - 1}")
    if items:
        raise PlanFileRefusedError("plan_ranks_refused", "V6", "each lens's ranks must be exactly 0..n-1", items)

    # V7 -- extra keys the verb writes itself.
    items = []
    for lens in sorted(lenses, key=lambda l: l["lens_name"]):
        for row in sorted(lens["rows"], key=lambda r: r["rank"]):
            clash = sorted(set(row["extra"]) & set(RESERVED_EXTRA_KEYS))
            if clash:
                items.append(f"lens {lens['lens_name']!r} rank {row['rank']}: extra uses {clash}")
    if items:
        raise PlanFileRefusedError(
            "plan_extra_key_reserved", "V7",
            f"extra may not use the keys the verb writes itself {list(RESERVED_EXTRA_KEYS)}", items,
        )

    # V8 -- the plan's own hash, and the caller's expectation.
    computed = plan_sha256(plan)
    items = []
    if plan["plan_sha256"] != computed:
        items.append(f"plan_sha256: the file says {plan['plan_sha256']!r}, the plan hashes to {computed!r}")
    if expect_plan_sha256 is not None and expect_plan_sha256.strip().lower() != computed:
        items.append(f"--expect-plan-sha256: {expect_plan_sha256!r}, the plan hashes to {computed!r}")
    if items:
        raise PlanFileRefusedError("plan_sha256_mismatch", "V8", "the plan's hash does not match", items)

    return roster_by_name


# ---------------------------------------------------------------------------
# write and read back
# ---------------------------------------------------------------------------


def _raw_insert(conn: sqlite3.Connection, table: str, row: Mapping[str, Any]) -> None:
    """``trialerror.stores.writer.insert``'s unknown-column check, minus its
    per-call commit -- the rows here land in the caller's one transaction
    (the pattern ``trialerror.law.service`` and ``trialerror.artifacts._txn``
    use for the same reason)."""
    columns = table_columns(conn, table)
    unknown = set(row) - columns
    if unknown:
        raise PlanFileRefusedError(
            "plan_write_refused", "write", f"{table}: unknown column(s) {sorted(unknown)!r}"
        )
    cols = list(row)
    conn.execute(
        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
        [row[c] for c in cols],
    )


def _row_for(
    plan: Mapping[str, Any],
    lens: Mapping[str, Any],
    row: Mapping[str, Any],
    *,
    roster_row: Mapping[str, Any],
    round_id: str,
    launch_id: str,
    ts: str,
) -> dict[str, Any]:
    """One ``lens_assignment`` row in ``run_assignment``'s own shape."""
    spec = {
        "round_id": round_id,
        "candidate_id": row["candidate_id"],
        "distance_score": row["distance_score"],
        "cluster_id": row["cluster_id"],
        "rank": row["rank"],
        SALT_SCHEME_KEY: plan["salt_scheme"],
        "plan_sha256": plan["plan_sha256"],
        **row["extra"],
    }
    return {
        "assign_id": new_id("ASGN"),
        "roster_id": roster_row["roster_id"],
        "slice_spec": json.dumps(spec, ensure_ascii=False),
        "arm": lens["arm"],
        "weights": json.dumps(list(plan["weights"])),
        "far_floor": lens["far_floor"],
        "arm_mode": plan["arm_mode"],
        "far_lens_floor": plan["far_lens_floor"],
        "recipe_cards": roster_row.get("recipe_cards"),
        "inter_cluster_mandate": int(plan["inter_cluster_mandate"]),
        "seed": str(plan["seed"]),
        "launch_id": launch_id,
        "created_ts": ts,
    }


def _read_back(conn: sqlite3.Connection, round_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT a.*, r.lens_name AS lens_name FROM lens_assignment a "
        "JOIN lens_roster r ON a.roster_id = r.roster_id WHERE r.round_id = ?",
        (round_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _project_read_back(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        try:
            spec = json.loads(row["slice_spec"])
        except (TypeError, ValueError):
            spec = None
        if not isinstance(spec, dict):
            spec = {}
        out.append(
            {
                "lens_name": row["lens_name"],
                "arm": row["arm"],
                "candidate_id": spec.get("candidate_id"),
                "cluster_id": spec.get("cluster_id"),
                "rank": spec.get("rank"),
                "distance_score": spec.get("distance_score"),
                "extra": {k: v for k, v in spec.items() if k not in RESERVED_EXTRA_KEYS},
            }
        )
    out.sort(key=lambda r: (str(r["lens_name"]), r["rank"] if _is_int(r["rank"]) else -1))
    return out


def _compare_read_back(
    written: Sequence[Mapping[str, Any]],
    read: Sequence[Mapping[str, Any]],
    *,
    expected_projection: Sequence[Mapping[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]]:
    """``(mismatch items, projection of what was read)``. Two comparisons:
    every column of every written row, by ``assign_id``; and the projection,
    byte for byte in canonical form."""
    items: list[str] = []
    by_id = {r["assign_id"]: r for r in read}
    if len(read) != len(written):
        items.append(f"rows: {len(written)} written, {len(read)} read back for the round")
    for row in written:
        got = by_id.get(row["assign_id"])
        if got is None:
            items.append(f"row {row['assign_id']}: not read back")
            continue
        for column, value in row.items():
            if got.get(column) != value:
                items.append(f"row {row['assign_id']}.{column}: wrote {value!r}, read {got.get(column)!r}")
    projection = _project_read_back(read)
    if canonical_bytes(projection) != canonical_bytes(list(expected_projection)):
        items.append(
            f"projection: the file's hashes to {sha256_of(list(expected_projection))}, "
            f"the read-back's to {sha256_of(projection)}"
        )
    return items, projection


def write_plan(
    store: Store,
    *,
    plan_path: Path | str,
    round_id: str,
    launch_id: str,
    expect_plan_sha256: str | None = None,
    now_ts: str | None = None,
) -> dict[str, Any]:
    """Validate the plan file at ``plan_path``, write its rows in one
    transaction, read them back, and commit only if they read back as
    planned. Returns ``{"n_rows", "n_lenses", "projection_sha256",
    "plan_file_sha256", "assign_ids": {lens_name: [assign ids by rank]}}``.

    ``launch_id`` is the launch doing the writing (``lens_assignment.
    launch_id``), and must name a ``platform.launch`` row: it is checked
    before the transaction opens, so an unknown id writes nothing.
    ``plan_file_sha256`` is over the file's raw bytes; a plan written in
    canonical form hashes the same as its object."""
    plan, raw = load_plan_file(plan_path)
    roster_by_name = validate_plan(store, plan, round_id=round_id, expect_plan_sha256=expect_plan_sha256)
    try:
        require_xid_targets(store, "lens_assignment", {"launch_id": launch_id})
    except XidTargetMissingError as exc:
        raise PlanFileRefusedError(
            "plan_launch_unknown", "launch", f"--launch-id {launch_id!r} names no launch", [str(exc)]
        ) from exc

    expected_projection = plan_projection(plan)
    ts = now_ts or now()
    conn = store.ops
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Re-read under the write lock what the checks above read without it:
        # a roster row or an assignment written in between would otherwise
        # land beside this plan unseen.
        _refuse_if_assigned(conn, round_id)
        current = {str(r["lens_name"]): r for r in list_roster(store, round_id=round_id)}
        if {n: dict(r) for n, r in current.items()} != {n: dict(r) for n, r in roster_by_name.items()}:
            raise PlanFileRefusedError(
                "plan_roster_mismatch", "V2",
                f"round {round_id!r}'s roster changed between the checks and the write",
                [f"lens {n!r}" for n in sorted(set(current) ^ set(roster_by_name))] or ["roster rows edited"],
            )
        written: list[dict[str, Any]] = []
        assign_ids: dict[str, list[str]] = {}
        for lens in sorted(plan["lenses"], key=lambda l: l["lens_name"]):
            roster_row = roster_by_name[lens["lens_name"]]
            for row in sorted(lens["rows"], key=lambda r: r["rank"]):
                record = _row_for(
                    plan, lens, row, roster_row=roster_row, round_id=round_id, launch_id=launch_id, ts=ts
                )
                try:
                    _raw_insert(conn, "lens_assignment", record)
                except sqlite3.IntegrityError as exc:
                    raise PlanFileRefusedError(
                        "plan_write_refused", "write", "lens_assignment refused a row; nothing was written",
                        [f"lens {lens['lens_name']!r} rank {row['rank']}: {exc}"],
                    ) from exc
                written.append(record)
                assign_ids.setdefault(lens["lens_name"], []).append(record["assign_id"])

        items, projection = _compare_read_back(
            written, _read_back(conn, round_id), expected_projection=expected_projection
        )
        if items:
            raise PlanFileRefusedError(
                "plan_readback_mismatch", "read-back",
                "the rows did not read back as the plan asked; nothing was written", items,
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    return {
        "n_rows": len(written),
        "n_lenses": len(assign_ids),
        "projection_sha256": sha256_of(projection),
        "plan_file_sha256": hashlib.sha256(raw).hexdigest(),
        "assign_ids": assign_ids,
    }
