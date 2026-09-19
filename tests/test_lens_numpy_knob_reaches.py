"""Fix pass V-1: ``[retrieve] numpy_fastpath = "off"`` reaches the lens.

The knob shipped reading a config MAPPING the caller hands down, and every
lens call site handed ``None`` -- so ``numpy_fastpath_mode(None)`` fell
through the program's own ``trialerror.toml`` and landed on the process
environment or on the code default. ``off`` therefore turned nothing off for
three of the four families ``USER_SETUP`` §3i names under the knob: the lens
stratifier's candidate scoring, the document-vector pooler, and the novelty
screen's corpus nearest-neighbour passes.

The tests here are deliberately NOT unit tests of ``vecmath``. That module's
own suite already proves ``mode="off"`` takes the plain path; what shipped
broken is the PLUMBING between a program's config file and those calls, so
each test below drives a real lens entry point against a program root whose
``trialerror.toml`` says ``off`` and asserts that no scan under it obtained
numpy. Passing ``mode=`` or ``config=`` explicitly -- which is what the
lane's own tests did -- cannot see this failure at all.

The spy wraps :func:`trialerror.util.vecmath.numpy_module`, the single door
every scan in that module goes through, and records what each call was
handed and what it returned. ``numpy_module`` is consulted BEFORE the
row-count threshold on every path, so a fixture-sized scan exercises the
plumbing exactly as a hundred-thousand-row one would.
"""

from __future__ import annotations

import pytest

from tests._lens_fixtures import build_doc_pool
from tests._novelty_fixtures import build_round
from trialerror.lens import assign as lens_assign
from trialerror.lens import vectors as lens_vectors
from trialerror.lens.novelty import baseline_distribution, run_mechanical_screen
from trialerror.lens.roster import add_lens
from trialerror.lens.stratify import score_candidates
from trialerror.util import vecmath

pytestmark = pytest.mark.usefixtures("store")

numpy = pytest.importorskip("numpy")

SEED = "seed-knob"


@pytest.fixture()
def spy(monkeypatch):
    """``[(config_given, numpy_returned), ...]`` for every scan that asked."""
    calls: list[tuple[object, bool]] = []
    real = vecmath.numpy_module

    def _spy(config=None, *, mode=None):
        out = real(config, mode=mode)
        calls.append((config, out is not None))
        return out

    monkeypatch.setattr(vecmath, "numpy_module", _spy)
    return calls


def _knob(store, value: str) -> None:
    """Write the program's own config file -- the surface the operator
    actually has. Nothing else in these tests names the knob."""
    (store.program_root / "trialerror.toml").write_text(
        '[program]\nid = "PROG-knob"\n\n[retrieve]\nnumpy_fastpath = "' + value + '"\n',
        encoding="utf-8",
    )


def _used_numpy(calls) -> bool:
    return any(used for _config, used in calls)


# ---------------------------------------------------------------------------
# the four families USER_SETUP names
# ---------------------------------------------------------------------------


def test_the_document_vector_pooler_honours_the_programs_off(store, spy):
    built = build_round(store)
    _knob(store, "off")
    doc_ids = [
        r["doc_id"]
        for r in store.knowledge.execute("SELECT doc_id FROM document ORDER BY doc_id").fetchall()
    ]
    assert doc_ids
    spy.clear()
    pooled = lens_vectors.fetch_doc_vectors(store, model_key=built["model_key"], doc_ids=doc_ids)
    assert pooled
    assert spy, "the pooler never reached vecmath at all"
    assert not _used_numpy(spy)


def test_the_corpus_nearest_neighbour_pass_honours_the_programs_off(store, spy):
    built = build_round(store)
    _knob(store, "off")
    spy.clear()
    result = baseline_distribution(store, round_id=built["round_id"], corpus_mode="vector")
    assert result["n_with_neighbour"] == result["n_records"] > 0
    assert spy
    assert not _used_numpy(spy)


def test_the_mechanical_screen_honours_the_programs_off(store, spy):
    built = build_round(store)
    _knob(store, "off")
    spy.clear()
    run_mechanical_screen(store, round_id=built["round_id"])
    assert spy
    assert not _used_numpy(spy)


def test_slice_distances_honours_the_programs_off(store, spy):
    pool = build_doc_pool(store, n_docs=6)
    lens_row = add_lens(
        store, round_id="round-knob", lens_name="lens-a", vantage="adversarial", model_class="top"
    )
    home_id, *candidate_ids = sorted(pool["doc_ids"])
    lens_assign.run_assignment(
        store, round_id="round-knob", model_key=pool["model_key"],
        home_doc_ids=[home_id], candidate_doc_ids=candidate_ids,
        lenses=[{"roster_id": lens_row["roster_id"]}], slices_per_lens=2, seed=SEED,
        far_floor=0, launch_id=pool["launch_id"],
    )
    _knob(store, "off")
    spy.clear()
    out = lens_assign.slice_distances(
        store, round_id="round-knob", home_doc_ids=[home_id], model_key=pool["model_key"]
    )
    assert out["lenses"]
    assert spy
    assert not _used_numpy(spy)


def test_run_assignment_scores_candidates_under_the_programs_off(store, spy):
    pool = build_doc_pool(store, n_docs=6)
    lens_row = add_lens(
        store, round_id="round-knob", lens_name="lens-a", vantage="adversarial", model_class="top"
    )
    home_id, *candidate_ids = sorted(pool["doc_ids"])
    _knob(store, "off")
    spy.clear()
    lens_assign.run_assignment(
        store, round_id="round-knob", model_key=pool["model_key"],
        home_doc_ids=[home_id], candidate_doc_ids=candidate_ids,
        lenses=[{"roster_id": lens_row["roster_id"]}], slices_per_lens=2, seed=SEED,
        far_floor=0, launch_id=pool["launch_id"],
    )
    assert spy
    assert not _used_numpy(spy)


# ---------------------------------------------------------------------------
# and the knob is a knob, not a switch that is always off
# ---------------------------------------------------------------------------


def test_auto_still_reaches_numpy_through_the_same_plumbing(store, spy):
    """The mirror image, so a fix that simply stopped calling numpy would
    fail here. Same entry point, same fixture, the knob the other way."""
    built = build_round(store)
    _knob(store, "auto")
    spy.clear()
    baseline_distribution(store, round_id=built["round_id"], corpus_mode="vector")
    assert _used_numpy(spy)


def test_the_config_handed_to_a_scan_is_the_programs_own_file(store, spy):
    """Not just "numpy was not used" -- the mapping that decided it is the
    one read off this program's root. A scan handed ``None`` that happened
    to be off for another reason would pass the tests above and fail here."""
    built = build_round(store)
    _knob(store, "off")
    spy.clear()
    lens_vectors.fetch_doc_vectors(
        store,
        model_key=built["model_key"],
        doc_ids=[
            r["doc_id"]
            for r in store.knowledge.execute("SELECT doc_id FROM document ORDER BY doc_id").fetchall()
        ],
    )
    seen = [config for config, _used in spy]
    assert seen
    assert all(
        isinstance(c, dict) and c.get("retrieve", {}).get("numpy_fastpath") == "off" for c in seen
    ), seen


def test_an_explicit_config_still_wins_over_the_file(store):
    """``program_config`` resolves the file only when the caller passed
    nothing. A caller that has already resolved a config -- or that wants
    the code default -- is not overruled by the program's file."""
    _knob(store, "off")
    assert lens_vectors.program_config(store)["retrieve"]["numpy_fastpath"] == "off"
    assert lens_vectors.program_config(store, {"retrieve": {"numpy_fastpath": "auto"}}) == {
        "retrieve": {"numpy_fastpath": "auto"}
    }
    assert lens_vectors.program_config(store, {}) == {}


def test_score_candidates_passes_its_config_through(store):
    """The stratifier's own seam, asserted directly: the ``config`` keyword
    reaches :func:`trialerror.util.vecmath.score_candidates` rather than
    being accepted and dropped."""
    candidates = {f"c{i}": [float(i), 1.0, 0.0] for i in range(300)}
    home = {"h": [1.0, 0.0, 0.0]}
    off = score_candidates(candidates, home, config={"retrieve": {"numpy_fastpath": "off"}})
    auto = score_candidates(candidates, home, config={"retrieve": {"numpy_fastpath": "auto"}})
    assert set(off) == set(auto)
    for cid in off:
        assert off[cid] == pytest.approx(auto[cid], abs=1e-9)
