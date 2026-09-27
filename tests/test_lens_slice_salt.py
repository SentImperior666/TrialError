"""The slice draw's salt scheme: ``roster-id`` (legacy, the default) and
``lens-name``.

The property under test is the one the legacy scheme does NOT have: a draw
that does not move when the roster's minted ids move. A ``roster_id`` is
minted — deleting and re-adding a roster row, or adding the rows in another
order, mints new ids, and under the legacy scheme both the per-lens salt and
the pool-depletion order read those ids, so the draw moves although nothing a
reader would call the design (round, seed, lens names, seats, weights,
candidates) changed.

Every store-level comparison here is between two FRESH fixture stores holding
the same candidate pool under the same document ids (see
:func:`_shared_doc_pool`) — otherwise a difference between two draws could
just as well be a difference between two corpora, and nothing could be
concluded from it.
"""

from __future__ import annotations

import contextlib
import json
import time

import pytest

from trialerror.ingest.anchors import sha256_hex
from trialerror.ingest.backends import FakeEmbedBackend
from trialerror.lens.assign import (
    LEGACY_SALT_SCHEME,
    LEGACY_SALT_WARNING_CODE,
    LENS_NAME_SALT_SCHEME,
    SALT_SCHEMES,
    SALT_SCHEME_KEY,
    build_assignment_plan,
    legacy_salt_warning,
    list_assignments,
    plan_to_json,
    row_salt_scheme,
    run_assignment,
    slice_distances,
)
from trialerror.lens.errors import DuplicateLensNameError
from trialerror.lens.export import export_launch_bookable, lens_log
from trialerror.lens.roster import add_lens
from trialerror.stores import insert
from trialerror.stores.store import open_store
from trialerror.stores.vecindex import (
    VecBackend,
    ensure_vec_table,
    serialize_vector_fallback,
    vec_table_name,
)
from trialerror.util.timeutil import now
from tests._lens_fixtures import DEFAULT_MODEL_DIMS, bootstrap_launch

ROUND = "round-salt"
SEED = "seed-salt"
SLICES = 5

#: One roster, described once: name -> (vantage, seat, cards). The same four
#: lenses are built in several INSERTION orders below; the design is identical
#: every time, only the minted ids differ.
ROSTER: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "lens-alpha": ("inside", "standard", ("FLIP", "INVERT")),
    "lens-bravo": ("outside", "assumption_buster", ("NEGATE",)),
    "lens-charlie": ("plain", "control", ()),
    "lens-delta": ("across", "standard", ("FLIP", "INVERT")),
}

#: Two insertion orders whose minted ids sort differently, and neither of
#: which is the lens-name order (``sorted(ROSTER)``).
ORDER_A = ("lens-delta", "lens-alpha", "lens-charlie", "lens-bravo")
ORDER_B = ("lens-charlie", "lens-bravo", "lens-delta", "lens-alpha")


# ---------------------------------------------------------------------------
# fixtures: two fresh stores over ONE candidate pool
# ---------------------------------------------------------------------------


def _shared_doc_pool(store, *, n_docs: int, prefix: str = "DOCFX") -> dict:
    """``n_docs`` single-chunk documents with DETERMINISTIC ids and
    text-derived vectors.

    ``tests._lens_fixtures.build_doc_pool`` mints a fresh ULID per document,
    so two stores built with it hold different candidate ids and two draws
    over them cannot be compared at all. Everything else (the source row, the
    chunk/emb/vec shape) is that builder's shape."""
    launch_id = bootstrap_launch(store)
    embed_backend = FakeEmbedBackend(dims=DEFAULT_MODEL_DIMS)
    model_key = embed_backend.model_key
    backend = ensure_vec_table(store.knowledge, model_key, DEFAULT_MODEL_DIMS)

    source_id = f"SRC-{prefix}"
    insert(
        store, "source",
        {
            "source_id": source_id, "kind": "paper", "title": "Salt Fixture Corpus",
            "license_tier": "open", "acquisition_route": "web", "request_state": "indexed",
            "registered_ts": now(), "registered_by_launch": launch_id,
        },
    )

    doc_ids: list[str] = []
    for i in range(n_docs):
        doc_id = f"{prefix}-{i:03d}"
        text = f"salt fixture document {i} distinct content marker Q{i}"
        insert(
            store, "document",
            {
                "doc_id": doc_id, "source_id": source_id, "rel_path": f"archive/{doc_id}.md",
                "media_type": "md", "normalizer_id": "fixture", "normalizer_version": "1",
                "sha256": "0" * 64, "status": "registered",
            },
        )
        element_id = f"ELM-{prefix}-{i:03d}"
        insert(
            store, "element",
            {"element_id": element_id, "doc_id": doc_id, "seq": 0, "type": "NarrativeText", "text": text},
        )
        chunk_id = f"CHK-{prefix}-{i:03d}"
        sha = sha256_hex(text)
        insert(
            store, "chunk",
            {
                "chunk_id": chunk_id, "doc_id": doc_id, "seq": 0, "text": text,
                "token_count": len(text.split()), "element_first": element_id,
                "element_last": element_id, "sha256": sha, "chunker_id": "fixture",
                "chunker_version": "1", "created_ts": now(),
            },
        )
        vector = list(embed_backend.embed_batch([text], kind="document")[0])
        blob = serialize_vector_fallback(vector)
        insert(
            store, "emb",
            {"chunk_sha256": sha, "model_key": model_key, "dims": embed_backend.dims,
             "vector": blob, "created_ts": now()},
        )
        table = vec_table_name(model_key)
        with store.knowledge:
            if backend == VecBackend.SQLITE_VEC:
                store.knowledge.execute(
                    f"INSERT INTO {table}(chunk_id, vector) VALUES (?, ?)", (chunk_id, blob)
                )
            else:
                store.knowledge.execute(
                    f"INSERT INTO {table}(chunk_id, model_key, dims, vector) VALUES (?, ?, ?, ?)",
                    (chunk_id, model_key, embed_backend.dims, blob),
                )
        doc_ids.append(doc_id)
    return {"launch_id": launch_id, "model_key": model_key, "doc_ids": doc_ids}


@pytest.fixture()
def fresh_store(tmp_path, platform_root):
    """A factory for independent program stores under one platform root, so a
    test can hold two rounds' worth of fixture stores at once."""
    opened = []

    def _open(name: str):
        root = tmp_path / f"program-{name}"
        root.mkdir(parents=True, exist_ok=True)
        store = open_store(root, platform_root=platform_root)
        opened.append(store)
        return store

    yield _open
    for store in opened:
        store.close()


@contextlib.contextmanager
def _strictly_increasing_mint_times():
    """Mint every id in this block at a strictly later millisecond than the
    last.

    A ULID sorts by its millisecond timestamp first and by ten random bytes
    only within one millisecond -- so four rows inserted inside the same
    millisecond sort in an order nobody chose. :func:`_two_rosters` asserts, as
    the PREMISE of every comparison it sets up, that two rosters inserted in
    different orders sort into different orders; without this that premise held
    only most of the time, and the test flaked.

    This changes nothing about what is asserted, and nothing outside this file:
    it advances the clock ``new_ulid`` reads through that function's OWN
    documented ``_timestamp_ms`` seam ("internal seams for deterministic
    tests"), so the ids are ordinary, well-formed, unique ULIDs. No real sleep,
    and no seeded code is touched.
    """
    from trialerror.util import ids as ids_mod

    real_new_ulid = ids_mod.new_ulid
    state = {"ms": int(time.time() * 1000)}

    def _minted(_timestamp_ms=None, _random_bytes=None):
        if _timestamp_ms is None:
            state["ms"] += 1
            _timestamp_ms = state["ms"]
        return real_new_ulid(_timestamp_ms=_timestamp_ms, _random_bytes=_random_bytes)

    ids_mod.new_ulid = _minted
    try:
        yield
    finally:
        ids_mod.new_ulid = real_new_ulid


def _seed_roster(store, order, *, round_id: str = ROUND) -> dict[str, str]:
    """Insert :data:`ROSTER` in ``order``; returns ``{lens_name: roster_id}``.

    Each row is minted a millisecond after the one before it (see
    :func:`_strictly_increasing_mint_times`), so sorting these ids reproduces
    ``order`` exactly rather than usually."""
    out: dict[str, str] = {}
    with _strictly_increasing_mint_times():
        for name in order:
            vantage, seat, cards = ROSTER[name]
            row = add_lens(
                store, round_id=round_id, lens_name=name, vantage=vantage,
                model_class="top", seat=seat, recipe_cards=list(cards) or None,
            )
            out[name] = row["roster_id"]
    return out


def _assign(store, pool, *, salt_scheme: str, seed: str = SEED, arm_mode: str = "per_slice",
            round_id: str = ROUND, roster: dict[str, str] | None = None) -> dict:
    """``run_assignment`` over the whole fixture pool, roster read in
    insertion order exactly as ``trialerror lens assign`` reads it."""
    from trialerror.lens.roster import list_roster

    home_id, *candidate_ids = pool["doc_ids"]
    rows = list_roster(store, round_id=round_id)
    return run_assignment(
        store, round_id=round_id, model_key=pool["model_key"],
        home_doc_ids=[home_id], candidate_doc_ids=candidate_ids,
        lenses=[
            {"roster_id": r["roster_id"], "lens_name": r["lens_name"], "seat": r["seat"],
             "recipe_cards": r["recipe_cards"]}
            for r in rows
        ],
        slices_per_lens=SLICES, seed=seed, arm_mode=arm_mode, salt_scheme=salt_scheme,
    )


def _fingerprint(store, *, round_id: str = ROUND) -> dict[str, list]:
    """What the draw decided, keyed by LENS NAME rather than by minted id:
    per lens, in assignment order, each slice's candidate, arm, rank, distance,
    the lens's floor, its mode and its card block. Two draws that agree here
    agree on every decision the planner made; a comparison keyed by roster_id
    could not be made across two stores at all."""
    out: dict[str, list] = {}
    for row in list_assignments(store, round_id=round_id):
        spec = json.loads(row["slice_spec"])
        out.setdefault(row["lens_name"], []).append(
            [
                spec["candidate_id"], row["arm"], spec["rank"], spec["distance_score"],
                row["arm_mode"], row["far_floor"], row["far_lens_floor"], row["recipe_cards"],
            ]
        )
    return out


def _hash(store, pool, *, round_id: str = ROUND) -> str:
    """The one assignment hash the harness itself computes over a stored draw
    (``lens slice-distances``), which is keyed by lens name and is therefore
    comparable across two stores."""
    home_id = pool["doc_ids"][0]
    return slice_distances(
        store, round_id=round_id, home_doc_ids=[home_id], model_key=pool["model_key"]
    )["canonical_sha256"]


def _two_rosters(fresh_store, *, arm_mode: str, salt_scheme: str, n_docs: int = 46):
    """Store A (insertion :data:`ORDER_A`) and store B (:data:`ORDER_B`), each
    assigned under ``salt_scheme``. Returns both stores, both pools and the
    two roster-id maps."""
    store_a, store_b = fresh_store("a"), fresh_store("b")
    pool_a = _shared_doc_pool(store_a, n_docs=n_docs)
    pool_b = _shared_doc_pool(store_b, n_docs=n_docs)
    assert pool_a["doc_ids"] == pool_b["doc_ids"]  # one pool, two stores
    roster_a = _seed_roster(store_a, ORDER_A)
    roster_b = _seed_roster(store_b, ORDER_B)
    # The premise of every comparison below: the ids really are different, and
    # they really do sort into a different order. _seed_roster mints each row a
    # millisecond after the last, so sorting the ids reproduces the insertion
    # order and this holds every run rather than most runs.
    assert set(roster_a.values()).isdisjoint(roster_b.values())
    assert [n for n, _ in sorted(roster_a.items(), key=lambda kv: kv[1])] == list(ORDER_A)
    assert [n for n, _ in sorted(roster_b.items(), key=lambda kv: kv[1])] == list(ORDER_B)
    assert list(ORDER_A) != list(ORDER_B)
    _assign(store_a, pool_a, salt_scheme=salt_scheme, arm_mode=arm_mode)
    _assign(store_b, pool_b, salt_scheme=salt_scheme, arm_mode=arm_mode)
    return (store_a, pool_a, roster_a), (store_b, pool_b, roster_b)


# ---------------------------------------------------------------------------
# 1 · the legacy path is untouched
# ---------------------------------------------------------------------------


_PURE_HOME = {"H": [1.0, 0.0]}


def _pure_candidates(n: int) -> dict[str, list[float]]:
    import math

    denom = max(n - 1, 1)
    return {f"C{i}": [math.cos(i * math.pi / denom), math.sin(i * math.pi / denom)] for i in range(n)}


@pytest.mark.parametrize("arm_mode", ["per_slice", "per_lens"])
def test_an_explicit_roster_id_scheme_is_byte_identical_to_no_flag(arm_mode):
    candidates = _pure_candidates(30)
    lenses = [
        {"roster_id": "ROST-3", "lens_name": "lens-c", "seat": "standard"},
        {"roster_id": "ROST-1", "lens_name": "lens-a", "seat": "assumption_buster"},
        {"roster_id": "ROST-2", "lens_name": "lens-b", "seat": "standard"},
    ]
    kwargs = dict(
        candidates=candidates, home=_PURE_HOME, lenses=lenses, slices_per_lens=3,
        seed="seed-A", arm_mode=arm_mode, far_floor=1,
    )
    default = build_assignment_plan(**kwargs)
    explicit = build_assignment_plan(**kwargs, salt_scheme=LEGACY_SALT_SCHEME, round_id="round-x")
    assert plan_to_json(default) == plan_to_json(explicit)
    assert default["salt_scheme"] == LEGACY_SALT_SCHEME


def test_the_legacy_scheme_ignores_round_id_and_lens_name_entirely():
    """Under ``roster-id`` the salt is the minted id and nothing else: a plan
    drawn with different names, in a different round, is the same plan."""
    candidates = _pure_candidates(30)
    a = build_assignment_plan(
        candidates=candidates, home=_PURE_HOME, slices_per_lens=3, seed="seed-A",
        lenses=[{"roster_id": "ROST-1", "lens_name": "zeta"}, {"roster_id": "ROST-2", "lens_name": "alpha"}],
        round_id="round-1",
    )
    b = build_assignment_plan(
        candidates=candidates, home=_PURE_HOME, slices_per_lens=3, seed="seed-A",
        lenses=[{"roster_id": "ROST-1"}, {"roster_id": "ROST-2"}],
    )
    assert plan_to_json(a) == plan_to_json(b)


def test_an_unknown_salt_scheme_is_refused():
    with pytest.raises(ValueError, match="salt_scheme must be one of"):
        build_assignment_plan(
            candidates=_pure_candidates(9), home=_PURE_HOME, lenses=[{"roster_id": "R"}],
            slices_per_lens=1, seed="s", far_floor=0, salt_scheme="by-vibes",
        )


def test_the_schemes_are_the_two_named_ones():
    assert SALT_SCHEMES == (LEGACY_SALT_SCHEME, LENS_NAME_SALT_SCHEME) == ("roster-id", "lens-name")


# ---------------------------------------------------------------------------
# 2 · roster-id independence — the acceptance test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("arm_mode", ["per_slice", "per_lens"])
def test_a_lens_name_draw_is_identical_across_two_differently_ordered_rosters(fresh_store, arm_mode):
    (store_a, pool_a, _), (store_b, pool_b, _) = _two_rosters(
        fresh_store, arm_mode=arm_mode, salt_scheme=LENS_NAME_SALT_SCHEME
    )
    assert _fingerprint(store_a) == _fingerprint(store_b)
    assert _hash(store_a, pool_a) == _hash(store_b, pool_b)


@pytest.mark.parametrize("arm_mode", ["per_slice", "per_lens"])
def test_the_same_two_rosters_draw_differently_under_the_legacy_scheme(fresh_store, arm_mode):
    """The defect itself, pinned: nobody may "fix" the legacy path by
    accident. Under ``roster-id`` the same design over two differently-ordered
    rosters is two different draws."""
    (store_a, pool_a, _), (store_b, pool_b, _) = _two_rosters(
        fresh_store, arm_mode=arm_mode, salt_scheme=LEGACY_SALT_SCHEME
    )
    assert _fingerprint(store_a) != _fingerprint(store_b)
    assert _hash(store_a, pool_a) != _hash(store_b, pool_b)


def test_a_deleted_and_re_added_roster_row_does_not_move_a_lens_name_draw(fresh_store):
    """Probe (a)'s second half, and the prohibition this lane retires: one
    roster, built in one order, with one row deleted and re-added mid-way — a
    new minted id under the same name — draws exactly what the untouched
    roster draws."""
    store_a, store_c = fresh_store("a"), fresh_store("c")
    pool_a = _shared_doc_pool(store_a, n_docs=46)
    pool_c = _shared_doc_pool(store_c, n_docs=46)
    _seed_roster(store_a, ORDER_A)

    roster_c = _seed_roster(store_c, ORDER_A[:2])
    with store_c.ops:
        store_c.ops.execute("DELETE FROM lens_roster WHERE roster_id = ?", (roster_c[ORDER_A[1]],))
    _seed_roster(store_c, ORDER_A[2:])
    re_added = add_lens(
        store_c, round_id=ROUND, lens_name=ORDER_A[1], vantage=ROSTER[ORDER_A[1]][0],
        model_class="top", seat=ROSTER[ORDER_A[1]][1],
        recipe_cards=list(ROSTER[ORDER_A[1]][2]) or None,
    )
    assert re_added["roster_id"] != roster_c[ORDER_A[1]]

    _assign(store_a, pool_a, salt_scheme=LENS_NAME_SALT_SCHEME)
    _assign(store_c, pool_c, salt_scheme=LENS_NAME_SALT_SCHEME)
    assert _fingerprint(store_a) == _fingerprint(store_c)
    assert _hash(store_a, pool_a) == _hash(store_c, pool_c)


def test_the_lens_name_scheme_processes_the_roster_in_name_order(fresh_store):
    """The ordering half, visible on its own: whatever order the rows went in,
    the plan's lenses come back in ascending lens_name order — which is the
    order the shared arm pools deplete in."""
    store = fresh_store("order")
    pool = _shared_doc_pool(store, n_docs=46)
    roster = _seed_roster(store, ORDER_A)
    result = _assign(store, pool, salt_scheme=LENS_NAME_SALT_SCHEME)
    name_of = {rid: name for name, rid in roster.items()}
    assert [name_of[lp["roster_id"]] for lp in result["plan"]["lenses"]] == sorted(ROSTER)


# ---------------------------------------------------------------------------
# 3 · seed and name sensitivity under lens-name
# ---------------------------------------------------------------------------


def test_another_seed_draws_another_plan_under_lens_name(fresh_store):
    store_a, store_b = fresh_store("a"), fresh_store("b")
    pool_a = _shared_doc_pool(store_a, n_docs=46)
    pool_b = _shared_doc_pool(store_b, n_docs=46)
    _seed_roster(store_a, ORDER_A)
    _seed_roster(store_b, ORDER_A)
    _assign(store_a, pool_a, salt_scheme=LENS_NAME_SALT_SCHEME, seed=SEED)
    _assign(store_b, pool_b, salt_scheme=LENS_NAME_SALT_SCHEME, seed=SEED + "-other")
    assert _fingerprint(store_a) != _fingerprint(store_b)


def test_renaming_one_lens_changes_that_lenss_own_draw(fresh_store):
    """Renaming a lens changes ITS salt, so its own slice changes — that is
    the guaranteed half and the only half asserted.

    The pools deplete in name order, so a rename can also move where that lens
    sits in the order and therefore what is left for the others; and a rename
    that does not change the name's position may leave some later lens drawing
    exactly what it drew before. Neither is asserted, because neither is
    guaranteed: this scheme reproduces a roster's draw, not a draw across two
    different rosters."""
    store_a, store_b = fresh_store("a"), fresh_store("b")
    pool_a = _shared_doc_pool(store_a, n_docs=46)
    pool_b = _shared_doc_pool(store_b, n_docs=46)
    _seed_roster(store_a, ORDER_A)

    # The same roster with ONE lens renamed, keeping its position in the name
    # order (alpha -> alpha-2 still sorts first) so the rename is the only
    # difference the draw can see.
    for name in ORDER_A:
        vantage, seat, cards = ROSTER[name]
        add_lens(
            store_b, round_id=ROUND, lens_name=("lens-alpha-2" if name == "lens-alpha" else name),
            vantage=vantage, model_class="top", seat=seat, recipe_cards=list(cards) or None,
        )

    _assign(store_a, pool_a, salt_scheme=LENS_NAME_SALT_SCHEME)
    _assign(store_b, pool_b, salt_scheme=LENS_NAME_SALT_SCHEME)
    fp_a, fp_b = _fingerprint(store_a), _fingerprint(store_b)
    assert [s[0] for s in fp_a["lens-alpha"]] != [s[0] for s in fp_b["lens-alpha-2"]]


def test_the_round_id_is_part_of_the_lens_name_salt(fresh_store):
    """One lens name means one draw stream WITHIN a round, not across every
    round the program runs."""
    candidates = _pure_candidates(30)
    lenses = [{"roster_id": "ROST-1", "lens_name": "lens-a"}, {"roster_id": "ROST-2", "lens_name": "lens-b"}]
    one = build_assignment_plan(
        candidates=candidates, home=_PURE_HOME, lenses=lenses, slices_per_lens=3, seed="s",
        far_floor=1, salt_scheme=LENS_NAME_SALT_SCHEME, round_id="round-1",
    )
    two = build_assignment_plan(
        candidates=candidates, home=_PURE_HOME, lenses=lenses, slices_per_lens=3, seed="s",
        far_floor=1, salt_scheme=LENS_NAME_SALT_SCHEME, round_id="round-2",
    )
    assert plan_to_json(one) != plan_to_json(two)


# ---------------------------------------------------------------------------
# 4 · the refusals
# ---------------------------------------------------------------------------


def test_duplicate_lens_names_are_refused_before_any_row_is_written(fresh_store):
    store = fresh_store("dup")
    pool = _shared_doc_pool(store, n_docs=46)
    add_lens(store, round_id=ROUND, lens_name="twin", vantage="one", model_class="top")
    add_lens(store, round_id=ROUND, lens_name="twin", vantage="two", model_class="top")
    with pytest.raises(DuplicateLensNameError, match="must be unique"):
        _assign(store, pool, salt_scheme=LENS_NAME_SALT_SCHEME)
    assert store.ops.execute("SELECT COUNT(*) AS n FROM lens_assignment").fetchone()["n"] == 0


def test_the_legacy_scheme_still_accepts_a_repeated_lens_name(fresh_store):
    """Nothing new is refused under ``roster-id``: there the minted id is the
    identity, and two lenses may share a name."""
    store = fresh_store("dup-legacy")
    pool = _shared_doc_pool(store, n_docs=46)
    add_lens(store, round_id=ROUND, lens_name="twin", vantage="one", model_class="top")
    add_lens(store, round_id=ROUND, lens_name="twin", vantage="two", model_class="top")
    result = _assign(store, pool, salt_scheme=LEGACY_SALT_SCHEME)
    assert len(result["rows"]) == 2 * SLICES


def test_a_lens_with_no_name_is_refused_under_lens_name():
    with pytest.raises(ValueError, match="carries none"):
        build_assignment_plan(
            candidates=_pure_candidates(9), home=_PURE_HOME, lenses=[{"roster_id": "ROST-1"}],
            slices_per_lens=1, seed="s", far_floor=0, salt_scheme=LENS_NAME_SALT_SCHEME,
            round_id="round-1",
        )


def test_lens_name_needs_a_round_id():
    with pytest.raises(ValueError, match="needs round_id"):
        build_assignment_plan(
            candidates=_pure_candidates(9), home=_PURE_HOME,
            lenses=[{"roster_id": "ROST-1", "lens_name": "a"}], slices_per_lens=1, seed="s",
            far_floor=0, salt_scheme=LENS_NAME_SALT_SCHEME,
        )


# ---------------------------------------------------------------------------
# 5 · the scheme round-trips
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scheme", list(SALT_SCHEMES))
def test_every_row_records_its_scheme_and_the_readers_report_it(fresh_store, scheme):
    store = fresh_store("trip")
    pool = _shared_doc_pool(store, n_docs=46)
    _seed_roster(store, ORDER_A)
    result = _assign(store, pool, salt_scheme=scheme)

    assert result["salt_scheme"] == scheme
    assert result["plan"]["salt_scheme"] == scheme
    for row in result["rows"]:
        assert json.loads(row["slice_spec"])[SALT_SCHEME_KEY] == scheme
        assert row_salt_scheme(row) == scheme

    assert {r["salt_scheme"] for r in lens_log(store, round_id=ROUND)["rows"]} == {scheme}
    assert {r["attrs"]["salt_scheme"] for r in export_launch_bookable(store, round_id=ROUND)} == {scheme}
    distances = slice_distances(
        store, round_id=ROUND, home_doc_ids=[pool["doc_ids"][0]], model_key=pool["model_key"]
    )
    assert {lens["salt_scheme"] for lens in distances["lenses"].values()} == {scheme}


def test_a_stored_row_with_no_recorded_scheme_reads_as_the_legacy_one():
    """Every round that ran before the key existed is in exactly this state."""
    assert row_salt_scheme({"slice_spec": json.dumps({"candidate_id": "DOC-1"})}) == LEGACY_SALT_SCHEME
    assert row_salt_scheme({"slice_spec": "{}"}) == LEGACY_SALT_SCHEME
    assert row_salt_scheme({"slice_spec": None}) == LEGACY_SALT_SCHEME
    assert row_salt_scheme({}) == LEGACY_SALT_SCHEME
    # An unreadable spec is not evidence of a different scheme.
    assert row_salt_scheme({"slice_spec": "{not json"}) == LEGACY_SALT_SCHEME
    # A spec already decoded into a dict is read without re-parsing.
    assert row_salt_scheme({"slice_spec": {SALT_SCHEME_KEY: LENS_NAME_SALT_SCHEME}}) == LENS_NAME_SALT_SCHEME


def test_a_legacy_draw_warns_and_a_lens_name_draw_does_not(fresh_store):
    store = fresh_store("warn")
    pool = _shared_doc_pool(store, n_docs=46)
    _seed_roster(store, ORDER_A)
    legacy = _assign(store, pool, salt_scheme=LEGACY_SALT_SCHEME)
    assert [w["code"] for w in legacy["warnings"]] == [LEGACY_SALT_WARNING_CODE]
    assert "--slice-salt lens-name" in legacy["warnings"][0]["message"]
    assert legacy_salt_warning()["code"] == LEGACY_SALT_WARNING_CODE

    store_b = fresh_store("warn-b")
    pool_b = _shared_doc_pool(store_b, n_docs=46)
    _seed_roster(store_b, ORDER_A)
    assert _assign(store_b, pool_b, salt_scheme=LENS_NAME_SALT_SCHEME)["warnings"] == []


def test_the_recorded_scheme_is_not_an_input_to_the_assignment_hash(fresh_store):
    """``slice_distances`` reports the scheme BESIDE its hash, never inside
    it: folding it in would move the recorded hash of every round that
    pre-registered one, which is the opposite of what this lane is for."""
    store = fresh_store("hash")
    pool = _shared_doc_pool(store, n_docs=46)
    _seed_roster(store, ORDER_A)
    _assign(store, pool, salt_scheme=LENS_NAME_SALT_SCHEME)
    before = slice_distances(
        store, round_id=ROUND, home_doc_ids=[pool["doc_ids"][0]], model_key=pool["model_key"]
    )
    assert SALT_SCHEME_KEY not in json.dumps(before["canonical"])
    # Strip the key off every stored row (the pre-key state) -- the hash is
    # unchanged, only the reported scheme moves.
    with store.ops:
        for row in list_assignments(store, round_id=ROUND):
            spec = json.loads(row["slice_spec"])
            spec.pop(SALT_SCHEME_KEY)
            store.ops.execute(
                "UPDATE lens_assignment SET slice_spec = ? WHERE assign_id = ?",
                (json.dumps(spec), row["assign_id"]),
            )
    after = slice_distances(
        store, round_id=ROUND, home_doc_ids=[pool["doc_ids"][0]], model_key=pool["model_key"]
    )
    assert after["canonical_sha256"] == before["canonical_sha256"]
    assert {lens["salt_scheme"] for lens in after["lenses"].values()} == {LEGACY_SALT_SCHEME}


# ---------------------------------------------------------------------------
# 6 · the CLI
# ---------------------------------------------------------------------------


def _cli(capsys, argv: list[str]) -> dict:
    from trialerror.cli import main

    code = main(argv)
    envelope = json.loads(capsys.readouterr().out.strip())
    envelope["_exit_code"] = code
    return envelope


@pytest.fixture()
def cli_root(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(tmp_path / "platform_root"))
    root = tmp_path / "cli-program"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _assign_argv(root, pool, *, extra: list[str] | None = None) -> list[str]:
    home_id, *candidate_ids = pool["doc_ids"]
    argv = [
        "lens", "--program-root", str(root), "assign", "--model-key", pool["model_key"],
        "--home", home_id, "--round-id", ROUND, "--slices-per-lens", str(SLICES), "--seed", SEED,
    ]
    for cid in candidate_ids:
        argv += ["--candidate", cid]
    return argv + list(extra or [])


def test_the_cli_defaults_to_the_legacy_scheme_and_says_so(cli_root, capsys):
    store = open_store(cli_root)
    pool = _shared_doc_pool(store, n_docs=46)
    _seed_roster(store, ORDER_A)
    store.close()

    env = _cli(capsys, _assign_argv(cli_root, pool))
    assert env["ok"] is True
    assert env["result"]["salt_scheme"] == LEGACY_SALT_SCHEME
    assert [w["code"] for w in env["warnings"]] == [LEGACY_SALT_WARNING_CODE]

    env = _cli(capsys, ["lens", "--program-root", str(cli_root), "log", "--round-id", ROUND])
    assert {r["salt_scheme"] for r in env["result"]["rows"]} == {LEGACY_SALT_SCHEME}


def test_the_cli_draws_under_lens_name_when_asked(cli_root, capsys):
    store = open_store(cli_root)
    pool = _shared_doc_pool(store, n_docs=46)
    _seed_roster(store, ORDER_A)
    store.close()

    env = _cli(capsys, _assign_argv(cli_root, pool, extra=["--slice-salt", LENS_NAME_SALT_SCHEME]))
    assert env["ok"] is True
    assert env["result"]["salt_scheme"] == LENS_NAME_SALT_SCHEME
    assert "warnings" not in env

    env = _cli(capsys, ["lens", "--program-root", str(cli_root), "export", "--round-id", ROUND])
    assert {r["attrs"]["salt_scheme"] for r in env["result"]["bookable"]} == {LENS_NAME_SALT_SCHEME}


def test_the_cli_refuses_a_duplicate_lens_name_under_lens_name(cli_root, capsys):
    store = open_store(cli_root)
    pool = _shared_doc_pool(store, n_docs=46)
    add_lens(store, round_id=ROUND, lens_name="twin", vantage="one", model_class="top")
    add_lens(store, round_id=ROUND, lens_name="twin", vantage="two", model_class="top")
    store.close()

    env = _cli(capsys, _assign_argv(cli_root, pool, extra=["--slice-salt", LENS_NAME_SALT_SCHEME]))
    assert env["ok"] is False
    assert env["error"]["code"] == "assign_refused"
    assert "must be unique" in env["error"]["message"]

    store = open_store(cli_root)
    assert store.ops.execute("SELECT COUNT(*) AS n FROM lens_assignment").fetchone()["n"] == 0
    store.close()


def test_the_cli_rejects_an_unknown_scheme_at_the_flag(cli_root, capsys):
    store = open_store(cli_root)
    pool = _shared_doc_pool(store, n_docs=6)
    store.close()
    with pytest.raises(SystemExit) as excinfo:
        _cli(capsys, _assign_argv(cli_root, pool, extra=["--slice-salt", "by-vibes"]))
    assert excinfo.value.code == 2
