"""Lane FB-7 item 7: ``lens slice-distances``.

A round pre-registered a rule of the form "the slice document farthest from
the lens's home medoid (the home medoid nearest to the slice), ties to the
lower id" and then computed it with an outside script that read
``lens_assignment.slice_spec``, pooled document vectors and cosined them by
hand. A pre-registered rule whose answer comes from a script nobody else has
is a rule the round cannot reproduce.

The two tie rules are what this module is mostly about, because they are
what an outside script and the harness would most easily disagree on: they
are exercised on CONSTRUCTED vectors where the tie is exact rather than on
whatever the fixture corpus happens to produce.
"""

from __future__ import annotations

import hashlib
import io
import json
from contextlib import redirect_stdout

import pytest

from tests._lens_fixtures import build_doc_pool
from trialerror.cli import main
from trialerror.lens.assign import _canonical_json, slice_distances
from trialerror.lens.errors import InsufficientCandidatesError
from trialerror.lens.roster import add_lens
from trialerror.stores import insert
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

ROUND = "round-distances"


def _run_cli(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(argv)
    return rc, json.loads(buf.getvalue().strip())


def _assignment(store, *, roster_id, doc_id, arm="near", seed="s"):
    insert(
        store, "lens_assignment",
        {
            "assign_id": new_id("ASGN"),
            "roster_id": roster_id,
            "slice_spec": json.dumps({"round_id": ROUND, "candidate_id": doc_id, "arm": arm, "rank": 0}),
            "arm": arm, "seed": seed, "created_ts": now(),
        },
    )


@pytest.fixture()
def round_with_slices(store):
    """Two lenses with two slice documents each, and two home documents."""
    pool = build_doc_pool(store, n_docs=6, dims=8)
    docs = sorted(pool["doc_ids"])
    rosters = {}
    for name in ("lens-a", "lens-b"):
        row = add_lens(
            store, round_id=ROUND, lens_name=name, vantage="v", model_class="mid", seat="standard"
        )
        rosters[name] = row["roster_id"]
    _assignment(store, roster_id=rosters["lens-a"], doc_id=docs[2])
    _assignment(store, roster_id=rosters["lens-a"], doc_id=docs[3])
    _assignment(store, roster_id=rosters["lens-b"], doc_id=docs[4])
    _assignment(store, roster_id=rosters["lens-b"], doc_id=docs[5])
    return {"pool": pool, "docs": docs, "home": [docs[0], docs[1]], "rosters": rosters}


# ---------------------------------------------------------------------------
# the shape
# ---------------------------------------------------------------------------


def test_every_lens_gets_its_slice_its_home_means_and_its_farthest(store, round_with_slices):
    fixture = round_with_slices
    result = slice_distances(
        store, round_id=ROUND, home_doc_ids=fixture["home"], model_key=fixture["pool"]["model_key"]
    )
    assert set(result["lenses"]) == {"lens-a", "lens-b"}
    assert result["home"] == fixture["home"]
    assert result["unvectorized"] == []

    lens_a = result["lenses"]["lens-a"]
    assert lens_a["slice_doc_ids"] == sorted([fixture["docs"][2], fixture["docs"][3]])
    assert set(lens_a["home_mean_distance"]) == set(fixture["home"])
    assert lens_a["nearest_home"] in fixture["home"]
    assert set(lens_a["distance_to_nearest_home"]) == set(lens_a["slice_doc_ids"])
    assert lens_a["farthest"] in lens_a["slice_doc_ids"]

    # `farthest` really is the largest of the reported distances, to the
    # reported home -- the two halves of the rule, checked against each
    # other rather than against a recomputation.
    distances = lens_a["distance_to_nearest_home"]
    assert distances[lens_a["farthest"]] == max(distances.values())
    # ...and the nearest home really is the smallest reported mean.
    means = lens_a["home_mean_distance"]
    assert means[lens_a["nearest_home"]] == min(means.values())


def test_lens_filter_narrows_the_answer(store, round_with_slices):
    fixture = round_with_slices
    only_b = slice_distances(
        store, round_id=ROUND, home_doc_ids=fixture["home"],
        model_key=fixture["pool"]["model_key"], lens_names=["lens-b"],
    )
    assert set(only_b["lenses"]) == {"lens-b"}
    # A name nothing matches yields no lens rather than every lens.
    none = slice_distances(
        store, round_id=ROUND, home_doc_ids=fixture["home"],
        model_key=fixture["pool"]["model_key"], lens_names=["lens-zzz"],
    )
    assert none["lenses"] == {}


def test_no_home_is_refused(store, round_with_slices):
    with pytest.raises(InsufficientCandidatesError, match="--home"):
        slice_distances(
            store, round_id=ROUND, home_doc_ids=[], model_key=round_with_slices["pool"]["model_key"]
        )


def test_a_document_with_no_vector_is_reported_and_not_averaged_over(store, round_with_slices):
    fixture = round_with_slices
    absent = "DOC-not-in-this-corpus"
    result = slice_distances(
        store, round_id=ROUND, home_doc_ids=[*fixture["home"], absent],
        model_key=fixture["pool"]["model_key"],
    )
    assert absent in result["unvectorized"]
    for lens in result["lenses"].values():
        assert lens["home_mean_distance"][absent] is None
        assert lens["nearest_home"] != absent


def test_an_unknown_model_key_leaves_everything_unvectorized(store, round_with_slices):
    fixture = round_with_slices
    result = slice_distances(
        store, round_id=ROUND, home_doc_ids=fixture["home"], model_key="no-such-model"
    )
    assert len(result["unvectorized"]) == 6
    for lens in result["lenses"].values():
        assert lens["nearest_home"] is None
        assert lens["farthest"] is None


# ---------------------------------------------------------------------------
# the two tie rules, on constructed vectors
# ---------------------------------------------------------------------------


def test_a_tie_on_the_nearest_home_breaks_to_the_lower_id(store, monkeypatch):
    """Two homes exactly equidistant from the slice: the lower id wins."""
    import trialerror.lens.assign as assign_mod

    vectors = {
        "DOC-home-b": [1.0, 0.0],
        "DOC-home-a": [1.0, 0.0],   # identical to home-b -> exactly tied means
        "DOC-slice-1": [0.0, 1.0],
        "DOC-slice-2": [0.0, 1.0],
    }
    monkeypatch.setattr(assign_mod, "fetch_doc_vectors", lambda *a, **k: dict(vectors))
    monkeypatch.setattr(
        assign_mod, "list_assignments",
        lambda *a, **k: [
            {"lens_name": "lens-a", "slice_spec": json.dumps({"candidate_id": "DOC-slice-1"})},
            {"lens_name": "lens-a", "slice_spec": json.dumps({"candidate_id": "DOC-slice-2"})},
        ],
    )
    result = slice_distances(
        store, round_id=ROUND, home_doc_ids=["DOC-home-b", "DOC-home-a"], model_key="k"
    )
    lens = result["lenses"]["lens-a"]
    assert lens["home_mean_distance"]["DOC-home-a"] == lens["home_mean_distance"]["DOC-home-b"]
    assert lens["nearest_home"] == "DOC-home-a"  # lower id, not the one passed first


def test_a_tie_on_the_farthest_slice_doc_breaks_to_the_lower_id(store, monkeypatch):
    import trialerror.lens.assign as assign_mod

    vectors = {
        "DOC-home": [1.0, 0.0],
        "DOC-slice-b": [0.0, 1.0],
        "DOC-slice-a": [0.0, 1.0],  # identical -> exactly tied distances
    }
    monkeypatch.setattr(assign_mod, "fetch_doc_vectors", lambda *a, **k: dict(vectors))
    monkeypatch.setattr(
        assign_mod, "list_assignments",
        lambda *a, **k: [
            {"lens_name": "lens-a", "slice_spec": json.dumps({"candidate_id": "DOC-slice-b"})},
            {"lens_name": "lens-a", "slice_spec": json.dumps({"candidate_id": "DOC-slice-a"})},
        ],
    )
    result = slice_distances(store, round_id=ROUND, home_doc_ids=["DOC-home"], model_key="k")
    lens = result["lenses"]["lens-a"]
    assert lens["distance_to_nearest_home"]["DOC-slice-a"] == lens["distance_to_nearest_home"]["DOC-slice-b"]
    assert lens["farthest"] == "DOC-slice-a"


# ---------------------------------------------------------------------------
# the hash
# ---------------------------------------------------------------------------


def test_the_hash_is_stable_across_runs_and_is_over_the_stated_canonical_form(store, round_with_slices):
    fixture = round_with_slices
    first = slice_distances(
        store, round_id=ROUND, home_doc_ids=fixture["home"], model_key=fixture["pool"]["model_key"]
    )
    second = slice_distances(
        store, round_id=ROUND, home_doc_ids=list(reversed(fixture["home"])),
        model_key=fixture["pool"]["model_key"],
    )
    assert first["canonical_sha256"] == second["canonical_sha256"]

    expected = hashlib.sha256(
        json.dumps(first["canonical"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert first["canonical_sha256"] == expected
    assert set(first["canonical"]) == set(first["lenses"])
    for lens_name, entry in first["canonical"].items():
        assert set(entry) == {"home", "farthest"}
        assert entry["home"] == first["lenses"][lens_name]["nearest_home"]
        assert entry["farthest"] == first["lenses"][lens_name]["farthest"]


def test_the_canonical_form_has_no_incidental_whitespace():
    assert _canonical_json({"b": 1, "a": {"y": 2, "x": 3}}) == '{"a":{"x":3,"y":2},"b":1}'


def test_the_hash_moves_when_a_pick_moves(store, round_with_slices):
    fixture = round_with_slices
    first = slice_distances(
        store, round_id=ROUND, home_doc_ids=fixture["home"], model_key=fixture["pool"]["model_key"]
    )
    narrowed = slice_distances(
        store, round_id=ROUND, home_doc_ids=fixture["home"],
        model_key=fixture["pool"]["model_key"], lens_names=["lens-a"],
    )
    assert narrowed["canonical_sha256"] != first["canonical_sha256"]


# ---------------------------------------------------------------------------
# read-only, and through the CLI
# ---------------------------------------------------------------------------


def test_it_writes_nothing(store, round_with_slices):
    fixture = round_with_slices

    def counts():
        return {
            name: int(store.ops.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"])
            for name in ("lens_assignment", "lens_roster", "event")
        }

    before = counts()
    slice_distances(
        store, round_id=ROUND, home_doc_ids=fixture["home"], model_key=fixture["pool"]["model_key"]
    )
    assert counts() == before


def test_the_cli_accepts_a_comma_separated_home(tmp_path, monkeypatch):
    from trialerror.stores.store import open_store

    platform_root = tmp_path / "platform"
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(platform_root))
    program_root = tmp_path / "program"
    program_root.mkdir()
    opened = open_store(program_root, platform_root=platform_root)
    pool = build_doc_pool(opened, n_docs=4, dims=8)
    docs = sorted(pool["doc_ids"])
    roster = add_lens(
        opened, round_id=ROUND, lens_name="lens-a", vantage="v", model_class="mid", seat="standard"
    )
    _assignment(opened, roster_id=roster["roster_id"], doc_id=docs[2])
    _assignment(opened, roster_id=roster["roster_id"], doc_id=docs[3])
    opened.close()

    common = ["lens", "--program-root", str(program_root)]
    rc, env = _run_cli([
        *common, "slice-distances", "--round-id", ROUND,
        "--home", f"{docs[0]},{docs[1]}", "--model-key", pool["model_key"],
    ])
    assert rc == 0, env
    assert env["ok"] is True
    assert env["result"]["home"] == [docs[0], docs[1]]
    assert env["result"]["canonical_sha256"]

    # ...and repeating the flag says the same thing.
    rc, repeated = _run_cli([
        *common, "slice-distances", "--round-id", ROUND,
        "--home", docs[0], "--home", docs[1], "--model-key", pool["model_key"],
    ])
    assert repeated["result"]["canonical_sha256"] == env["result"]["canonical_sha256"]


def test_the_cli_refuses_a_blank_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(tmp_path / "platform"))
    program_root = tmp_path / "program"
    program_root.mkdir()
    rc, env = _run_cli([
        "lens", "--program-root", str(program_root),
        "slice-distances", "--round-id", ROUND, "--home", " , ", "--model-key", "k",
    ])
    assert env["ok"] is False
    assert env["error"]["code"] == "no_home"


# ---------------------------------------------------------------------------
# fix pass V-7: the pick is decided on what the command PRINTS
# ---------------------------------------------------------------------------


def _drive(store, monkeypatch, vectors, homes):
    import trialerror.lens.assign as assign_mod

    monkeypatch.setattr(assign_mod, "fetch_doc_vectors", lambda *a, **k: dict(vectors))
    monkeypatch.setattr(
        assign_mod, "list_assignments",
        lambda *a, **k: [
            {"lens_name": "lens-a", "slice_spec": json.dumps({"candidate_id": "DOC-slice-1"})},
            {"lens_name": "lens-a", "slice_spec": json.dumps({"candidate_id": "DOC-slice-2"})},
        ],
    )
    return slice_distances(store, round_id=ROUND, home_doc_ids=homes, model_key="k")


def test_a_perturbation_below_the_reported_precision_does_not_flip_the_pick(store, monkeypatch):
    """Both picks used to be decided on unrounded float64 while every
    distance was reported to nine places, and the inputs come from
    ``mean_pool_l2``, whose two paths diverge by ~1.1e-16 for a document of
    256 chunks or more. So a change far below anything the command prints
    could flip ``nearest_home`` and the hash while every printed number
    stayed identical -- a difference a reader comparing two runs by the hash
    could not explain. The pick is now a function of the reported value."""
    base = {
        "DOC-home-a": [1.0, 0.0],
        "DOC-home-b": [1.0, 0.0],
        "DOC-slice-1": [0.0, 1.0],
        "DOC-slice-2": [0.0, 1.0],
    }
    before = _drive(store, monkeypatch, base, ["DOC-home-a", "DOC-home-b"])

    nudged = dict(base)
    nudged["DOC-home-b"] = [1.0 - 1.2e-16, 0.0 + 1.2e-16]
    after = _drive(store, monkeypatch, nudged, ["DOC-home-a", "DOC-home-b"])

    assert before["lenses"]["lens-a"]["home_mean_distance"] == \
        after["lenses"]["lens-a"]["home_mean_distance"]
    assert before["lenses"]["lens-a"]["nearest_home"] == after["lenses"]["lens-a"]["nearest_home"]
    assert before["canonical_sha256"] == after["canonical_sha256"]


def test_a_perturbation_above_the_reported_precision_does_move_the_pick(store, monkeypatch):
    """The mirror image: rounding is not a way of ignoring real
    differences. A gap the output can express is still decided on."""
    base = {
        "DOC-home-a": [1.0, 0.0],
        "DOC-home-b": [1.0, 0.0],
        "DOC-slice-1": [0.0, 1.0],
        "DOC-slice-2": [0.0, 1.0],
    }
    before = _drive(store, monkeypatch, base, ["DOC-home-a", "DOC-home-b"])
    assert before["lenses"]["lens-a"]["nearest_home"] == "DOC-home-a"

    moved = dict(base)
    moved["DOC-home-b"] = [1.0, 1e-4]  # a difference nine places can hold
    after = _drive(store, monkeypatch, moved, ["DOC-home-a", "DOC-home-b"])
    lens = after["lenses"]["lens-a"]
    assert lens["home_mean_distance"]["DOC-home-a"] != lens["home_mean_distance"]["DOC-home-b"]
    assert lens["nearest_home"] == min(
        lens["home_mean_distance"], key=lambda h: (lens["home_mean_distance"][h], h)
    )
    assert before["canonical_sha256"] != after["canonical_sha256"]


def test_every_reported_distance_is_at_the_stated_precision(store, round_with_slices):
    """The decided value and the printed value are the same object now, so
    nothing can be reported to one precision and decided at another."""
    from trialerror.lens.assign import SLICE_DISTANCE_DP

    fixture = round_with_slices
    result = slice_distances(
        store, round_id=ROUND, home_doc_ids=fixture["home"], model_key=fixture["pool"]["model_key"]
    )
    for lens in result["lenses"].values():
        for value in list(lens["home_mean_distance"].values()) + list(
            lens["distance_to_nearest_home"].values()
        ):
            if value is not None:
                assert value == round(value, SLICE_DISTANCE_DP)
