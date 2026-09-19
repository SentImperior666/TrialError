"""Lane FB-7 item 2: ``lens screen --baseline``.

A threshold defined as "the 90th percentile of the earlier round's
record→corpus nearest-neighbour cosine" could not be read out of the
harness. The calibration card's own baseline is over PLANTS and is cut from
bundles that already passed a similarity floor, and an archived round could
not be screened at all -- so the number had to be computed by hand, outside
anything the round could reproduce.

Three claims are tested here. The percentiles are the ones the stated method
produces, on known vectors, so the output's ``percentile_method`` is not a
label over a different cut. The filters select the population they say they
do. And nothing is written: every table's row count is taken before and
after, with the one documented exception -- the per-model idea-vector cache,
which the pass may fill.
"""

from __future__ import annotations

import pytest

from tests._novelty_fixtures import build_round
from trialerror.cli.lens import _parse_where
from trialerror.lens.novelty import IDEA_VECTOR_TABLE, PERCENTILE_METHOD, baseline_distribution, _percentile

pytestmark = pytest.mark.usefixtures("store")

#: Every table the pass could plausibly touch. Counted whole rather than
#: named one at a time so a table added later is covered without this test
#: being edited -- a read-only claim that only checks the tables someone
#: remembered is not a read-only claim.
def _row_counts(store) -> dict[str, int]:
    tables = [
        r["name"]
        for r in store.knowledge.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]
    counts = {}
    for name in tables:
        try:
            counts[name] = int(store.knowledge.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"])
        except Exception:  # a virtual/shadow table that will not COUNT is not state this pass writes
            continue
    return counts


# ---------------------------------------------------------------------------
# the distribution
# ---------------------------------------------------------------------------


def test_baseline_reports_a_neighbour_per_record_and_the_stated_percentiles(store):
    built = build_round(store)
    result = baseline_distribution(store, round_id=built["round_id"], corpus_mode="vector")

    assert result["percentile_method"] == PERCENTILE_METHOD == "index-round(p*(n-1))"
    assert result["n_records"] == len(built["ideas"])
    assert result["n_with_neighbour"] == result["n_records"]
    assert [r["idea_id"] for r in result["per_record"]] == sorted(r["idea_id"] for r in result["per_record"])

    for row in result["per_record"]:
        assert row["nearest_chunk_id"]
        assert row["nearest_doc_id"]
        assert -1.0 <= row["cosine"] <= 1.0
        doc_id = store.knowledge.execute(
            "SELECT doc_id FROM chunk WHERE chunk_id = ?", (row["nearest_chunk_id"],)
        ).fetchone()["doc_id"]
        assert doc_id == row["nearest_doc_id"]

    # The percentiles are exactly what the named method produces over the
    # per-record cosines this same call reported -- not a second cut.
    cosines = sorted(r["cosine"] for r in result["per_record"] if r["cosine"] is not None)
    assert result["min"] == round(cosines[0], 6)
    assert result["max"] == round(cosines[-1], 6)
    assert result["p50"] == _percentile(cosines, 0.50)
    assert result["p90"] == _percentile(cosines, 0.90)
    assert result["p95"] == _percentile(cosines, 0.95)
    assert result["reference_snapshot"]["model_key"] == result["model_key"]


def test_the_nearest_neighbour_has_no_similarity_floor(store):
    """The distinction from ``baseline_cosine_distribution``: a record
    nothing retrieved above the candidate-hit bar still contributes its
    real nearest neighbour here, so ``n_with_neighbour`` is ``n_records``
    on any corpus that has a vector at all."""
    built = build_round(store)
    result = baseline_distribution(store, round_id=built["round_id"], corpus_mode="vector")
    assert result["n_with_neighbour"] == result["n_records"] > 0
    assert all(r["cosine"] is not None for r in result["per_record"])


def test_an_archived_round_can_be_read(store):
    """The round a later one actually wants the number from."""
    built = build_round(store)
    ids = [str(i["idea_id"]) for i in built["ideas"]]
    with store.knowledge as conn:
        conn.execute(
            f"UPDATE idea SET status = 'archived' WHERE idea_id IN ({','.join('?' for _ in ids)})", ids
        )
    result = baseline_distribution(
        store, round_id=built["round_id"], status="archived", corpus_mode="vector"
    )
    assert result["status"] == "archived"
    assert result["n_records"] == len(ids)

    # ...and a status nothing is in selects nothing rather than everything.
    empty = baseline_distribution(store, round_id=built["round_id"], status="raw", corpus_mode="vector")
    assert empty["n_records"] == 0
    assert empty["n_with_neighbour"] == 0
    assert empty["p90"] is None and empty["min"] is None and empty["max"] is None
    assert empty["per_record"] == []


def test_where_filters_on_a_provenance_key_that_never_became_a_column(store):
    """``set_id`` lives inside the provenance JSON object and nowhere else,
    which is exactly the case the ``provenance.`` spelling is for."""
    built = build_round(store)
    whole = baseline_distribution(store, round_id=built["round_id"], corpus_mode="vector")

    filtered = baseline_distribution(
        store, round_id=built["round_id"], where={"set_id": "lens-1"}, corpus_mode="vector"
    )
    assert filtered["where"] == {"set_id": "lens-1"}
    assert 0 < filtered["n_records"] < whole["n_records"]
    ids = {r["idea_id"] for r in filtered["per_record"]}
    assert ids < {r["idea_id"] for r in whole["per_record"]}

    # A value nothing carries selects nothing, never everything.
    none_of_them = baseline_distribution(
        store, round_id=built["round_id"], where={"set_id": "no-such-lens"}, corpus_mode="vector"
    )
    assert none_of_them["n_records"] == 0

    # ...and so does a key nothing carries at all.
    unknown_key = baseline_distribution(
        store, round_id=built["round_id"], where={"no_such_key": "x"}, corpus_mode="vector"
    )
    assert unknown_key["n_records"] == 0


def test_where_also_reads_a_field_that_did_become_a_column(store):
    built = build_round(store)
    whole = baseline_distribution(store, round_id=built["round_id"], corpus_mode="vector")
    by_card = baseline_distribution(
        store, round_id=built["round_id"], where={"recipe_card": "TRANSFER"}, corpus_mode="vector"
    )
    assert by_card["n_records"] == whole["n_records"] > 0


def test_where_terms_are_anded(store):
    built = build_round(store)
    both = baseline_distribution(
        store,
        round_id=built["round_id"],
        where={"set_id": "lens-1", "recipe_card": "TRANSFER"},
        corpus_mode="vector",
    )
    only_set = baseline_distribution(
        store, round_id=built["round_id"], where={"set_id": "lens-1"}, corpus_mode="vector"
    )
    assert both["n_records"] == only_set["n_records"] > 0

    contradiction = baseline_distribution(
        store,
        round_id=built["round_id"],
        where={"set_id": "lens-1", "recipe_card": "NOT-A-CARD"},
        corpus_mode="vector",
    )
    assert contradiction["n_records"] == 0


# ---------------------------------------------------------------------------
# it writes nothing
# ---------------------------------------------------------------------------


def test_baseline_writes_nothing_but_the_idea_vector_cache(store):
    built = build_round(store)
    before = _row_counts(store)
    baseline_distribution(store, round_id=built["round_id"], corpus_mode="vector")
    after = _row_counts(store)

    changed = {name: (before.get(name), after.get(name)) for name in after if before.get(name) != after.get(name)}
    assert set(changed) <= {IDEA_VECTOR_TABLE}, changed

    # And a SECOND read writes nothing at all: the cache it filled is the
    # cache it now reads, so an archived round costs one embedding ever.
    mid = _row_counts(store)
    baseline_distribution(store, round_id=built["round_id"], corpus_mode="vector")
    assert _row_counts(store) == mid


def test_baseline_is_stable_across_reads(store):
    built = build_round(store)
    first = baseline_distribution(store, round_id=built["round_id"], corpus_mode="vector")
    second = baseline_distribution(store, round_id=built["round_id"], corpus_mode="vector")
    assert first["per_record"] == second["per_record"]
    assert (first["min"], first["p50"], first["p90"], first["p95"], first["max"]) == (
        second["min"], second["p50"], second["p90"], second["p95"], second["max"]
    )


# ---------------------------------------------------------------------------
# the CLI's own half
# ---------------------------------------------------------------------------


def test_where_parsing_requires_the_provenance_spelling():
    parsed, refusal = _parse_where(["provenance.set_id=lens-a", "provenance.arm=near"])
    assert refusal is None
    assert parsed == {"set_id": "lens-a", "arm": "near"}

    for bad in (["set_id=lens-a"], ["provenance.set_id"], ["provenance.=x"]):
        parsed, refusal = _parse_where(bad)
        assert parsed is None
        assert refusal["error"]["code"] == "where_malformed"

    assert _parse_where(None) == (None, None)
    assert _parse_where([]) == (None, None)


def test_a_value_containing_an_equals_sign_survives():
    parsed, refusal = _parse_where(["provenance.note=a=b"])
    assert refusal is None
    assert parsed == {"note": "a=b"}


# ---------------------------------------------------------------------------
# end to end through the CLI
# ---------------------------------------------------------------------------


@pytest.fixture()
def cli_program_root(tmp_path, monkeypatch):
    platform_root = tmp_path / "platform_root"
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(platform_root))
    program_root = tmp_path / "program"
    program_root.mkdir(parents=True, exist_ok=True)
    return program_root


@pytest.fixture()
def seeded_round(cli_program_root, tmp_path):
    from trialerror.stores.store import open_store

    opened = open_store(cli_program_root, platform_root=tmp_path / "platform_root")
    fixture = build_round(opened)
    opened.close()
    return fixture


def _run(capsys, argv):
    import json

    from trialerror.cli import main

    exit_code = main(argv)
    envelope = json.loads(capsys.readouterr().out.strip())
    envelope["_exit_code"] = exit_code
    return envelope


def test_cli_baseline_returns_the_distribution(capsys, cli_program_root, seeded_round):
    envelope = _run(
        capsys,
        ["lens", "--program-root", str(cli_program_root), "screen",
         "--round-id", seeded_round["round_id"], "--baseline", "--corpus-mode", "vector"],
    )
    assert envelope["ok"] is True, envelope
    baseline = envelope["result"]["baseline"]
    assert baseline["percentile_method"] == "index-round(p*(n-1))"
    assert baseline["corpus_mode"] == "vector"
    assert baseline["n_records"] == len(seeded_round["ideas"])
    assert baseline["p90"] is not None
    assert len(baseline["per_record"]) == baseline["n_records"]


def test_cli_baseline_takes_status_and_where(capsys, cli_program_root, seeded_round):
    envelope = _run(
        capsys,
        ["lens", "--program-root", str(cli_program_root), "screen",
         "--round-id", seeded_round["round_id"], "--baseline", "--corpus-mode", "vector",
         "--where", "provenance.set_id=lens-1"],
    )
    assert envelope["ok"] is True, envelope
    assert envelope["result"]["baseline"]["where"] == {"set_id": "lens-1"}
    assert 0 < envelope["result"]["baseline"]["n_records"] < len(seeded_round["ideas"])


def test_cli_refuses_a_bare_column_in_where(capsys, cli_program_root, seeded_round):
    envelope = _run(
        capsys,
        ["lens", "--program-root", str(cli_program_root), "screen",
         "--round-id", seeded_round["round_id"], "--baseline", "--where", "set_id=lens-1"],
    )
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "where_malformed"


def test_cli_refuses_a_filter_no_other_phase_reads(capsys, cli_program_root, seeded_round):
    envelope = _run(
        capsys,
        ["lens", "--program-root", str(cli_program_root), "screen",
         "--round-id", seeded_round["round_id"], "--mechanical", "--where", "provenance.set_id=lens-1"],
    )
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "baseline_only_flag"


def test_cli_baseline_is_a_phase_of_its_own(capsys, cli_program_root, seeded_round):
    """No phase at all still refuses, and names --baseline among them."""
    envelope = _run(
        capsys,
        ["lens", "--program-root", str(cli_program_root), "screen", "--round-id", seeded_round["round_id"]],
    )
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "no_phase"
    assert "--baseline" in envelope["error"]["message"]


# ---------------------------------------------------------------------------
# fix pass V-6: the reported name IS the cut
# ---------------------------------------------------------------------------


def test_the_reported_percentile_name_reproduces_the_cut():
    """The field exists so that "the 90th percentile of the earlier round's
    distribution" means ONE thing. It shipped reading ``nearest-rank``,
    which is the name of a different cut, so a reader who recomputed the
    threshold under the named convention got a different number for 6 of 24
    probed (n, p) pairs. The name now spells the formula out, and this test
    is the formula, recomputed from the name."""
    import math

    assert PERCENTILE_METHOD == "index-round(p*(n-1))"

    def _named(values, p):
        """What the NAME says to do, written out from the string above."""
        return round(values[min(len(values) - 1, int(round(p * (len(values) - 1))))], 6)

    for n in (1, 2, 3, 4, 5, 6, 7, 8, 11, 20, 37):
        series = [float(i) for i in range(1, n + 1)]
        for p in (0.5, 0.9, 0.95):
            assert _percentile(series, p) == _named(series, p), (n, p)


def test_the_cut_is_not_textbook_nearest_rank_and_the_name_no_longer_claims_it():
    """The reproduction from the verify report, kept: the two conventions
    genuinely disagree, so the old label was not a harmless synonym."""
    import math

    assert "nearest-rank" not in PERCENTILE_METHOD

    def _textbook(values, p):
        return round(values[min(len(values) - 1, max(0, math.ceil(p * len(values)) - 1))], 6)

    disagreements = [
        (n, p)
        for n in (3, 4, 5, 6, 7, 8, 11, 20)
        for p in (0.5, 0.9, 0.95)
        if _percentile([float(i) for i in range(1, n + 1)], p)
        != _textbook([float(i) for i in range(1, n + 1)], p)
    ]
    assert (4, 0.5) in disagreements
    assert (6, 0.9) in disagreements
    assert (20, 0.5) in disagreements
