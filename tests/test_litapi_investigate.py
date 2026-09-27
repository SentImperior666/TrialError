"""Lane SI part B: ``trialerror.litapi.investigate`` -- the source investigator.

Items B2 (the run), B3 (the held check) and B4 (verdicts and the operator
line), against REAL ``OpenAlexProvider``/``SemanticScholarProvider`` over one
``FakeTransport`` (``tests/_investigate_fixtures.py``) and a temp program store
(the conftest's ``store``). No network, no live store.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from trialerror.ingest import pipeline
from trialerror.litapi.citeparse import parse_citation
from trialerror.litapi.config import InvestigateConfig
from trialerror.litapi.investigate import (
    InvestigateError,
    Seed,
    held_match,
    load_delivered_manifest,
    load_seeds,
    record_verdict,
    render_list,
    run_investigation,
)
from trialerror.litapi.models import WorkRecord
from trialerror.litapi.transport import FakeTransport
from trialerror.stores.errors import XidTargetMissingError
from tests import _investigate_fixtures as fx
from tests._ingest_fixtures import bootstrap_launch
from tests._litapi_fixtures import FIXTURES_DIR, load_fixture

#: Under the brief's default cut-off (2000) the 1986 widgets study is itself
#: foundational; the tests that need an ordinary REQUEST move the cut-off so
#: only the 1969 gadgets book is.
_CUTOFF_1980 = InvestigateConfig(foundational_before=1980)


def _run(store, transport, seeds, tmp_path, *, launch_id, config=None, **kw):
    return run_investigation(
        store, fx.providers(transport), seeds, config=config or InvestigateConfig(), out_dir=tmp_path / "dossiers",
        launch_id=launch_id, current_year=fx.YEAR, **kw,
    )


def _seed(raw, *, list_id="list-1", row_id="row-1", question=None, line=1, position=0):
    return Seed(list_id=list_id, row_id=row_id, seed_raw=raw, question=question, line=line, position=position)


def _dossier_path(result, seed_raw) -> Path:
    return Path(next(d["dossier_path"] for d in result["dossiers"] if d["seed_raw"] == seed_raw))


def _dossier(result, seed_raw) -> dict:
    return json.loads(_dossier_path(result, seed_raw).read_text(encoding="utf-8"))


def _row(store, seed_raw, list_id="list-1") -> dict:
    row = store.knowledge.execute(
        "SELECT * FROM source_dossier WHERE list_id = ? AND seed_raw = ?", (list_id, seed_raw)
    ).fetchone()
    return dict(row) if row is not None else None


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ===========================================================================
# B2 -- the run
# ===========================================================================


def test_investigate_flags_wrong_identifier(store, tmp_path):
    """FAILS BEFORE lane SI part B: no investigator existed. The brief's A4
    pair through the whole run: the cited DOI resolves to another work by
    another author from an adjacent year, so the seed is ``wrong_identifier``,
    its dossier is written, its row says ``mismatch``, and nothing is gathered
    about the wrong work."""
    launch = bootstrap_launch(store)
    transport = FakeTransport()
    fx.route_wrong_doi(transport)

    result = _run(store, transport, [_seed(fx.WRONG_DOI_SEED)], tmp_path, launch_id=launch)

    assert result["states"]["wrong_identifier"] == 1
    dossier = _dossier(result, fx.WRONG_DOI_SEED)
    assert dossier["mechanical_state"] == "wrong_identifier"
    assert dossier["resolution"]["status"] == "mismatch"
    assert dossier["resolution"]["via"] == "doi"
    assert dossier["resolution"]["record"]["title"] == "An Unrelated Evaluation of Boards"
    assert dossier["resolution"]["match"]["notes"] == ["surname_mismatch"]
    assert dossier["flags"]["wrong_identifier"] is True
    assert dossier["evidence"]["calls"] == [] and dossier["evidence"]["citing"] == []
    assert len(transport.calls) == 2  # the two DOI lookups, and nothing about the wrong work

    row = _row(store, fx.WRONG_DOI_SEED)
    assert row["resolution"] == "mismatch"
    assert row["mechanical_state"] == "wrong_identifier"
    assert row["verdict"] is None
    assert row["dossier_sha256"] == _sha(_dossier_path(result, fx.WRONG_DOI_SEED))
    assert row["created_by_launch"] == launch


def test_investigate_rate_limit_is_retry_not_none(store, tmp_path):
    """A 429 on both providers' DOI routes (and on the title search that
    follows) is ``retry`` -- "could not ask" -- never ``need_info``. Nothing
    rate-limited is served from the cache, and ``resume`` re-asks that seed and
    only that seed."""
    launch = bootstrap_launch(store)
    transport = FakeTransport()
    fx.route_widgets(transport)
    # the client retries a search that met an HTTP error once, with the query
    # normalised -- so both spellings are rate-limited here
    for url in (
        fx.oa_doi_url(fx.GADGETS_DOI), fx.s2_paper_url(f"DOI:{fx.GADGETS_DOI}"),
        fx.oa_search_url("A Theory of Gadgets"), fx.s2_search_url("A Theory of Gadgets"),
        fx.oa_search_url("a theory of gadgets"), fx.s2_search_url("a theory of gadgets"),
    ):
        transport.add_response(url, fx.status(429))
    seeds = [_seed(fx.WIDGETS_SEED, line=1), _seed(fx.GADGETS_SEED, line=2)]

    first = _run(store, transport, seeds, tmp_path, launch_id=launch)

    assert first["states"]["retry"] == 1 and first["states"]["need_info"] == 0
    gadgets = _dossier(first, fx.GADGETS_SEED)
    assert gadgets["mechanical_state"] == "retry"
    assert gadgets["resolution"]["status"] == "retry"
    assert [t["call"] for t in gadgets["resolution"]["tried"]] == ["lookup_doi", "search"]
    words = {o["outcome"] for t in gadgets["resolution"]["tried"] for o in t["provider_outcomes"].values()}
    assert words == {"rate_limited"}
    assert _row(store, fx.GADGETS_SEED)["resolution"] == "retry"
    assert first["providers"]["openalex"]["rate_limited"] == 2

    # the providers answer now; resume skips the settled widgets seed
    fx.route_gadgets(transport)
    before = len(transport.calls)
    second = _run(store, transport, seeds, tmp_path, launch_id=launch, resume=True)

    assert second["skipped"]["resume"] == 1 and second["investigated"] == 1
    asked = {c["url"] for c in transport.calls[before:]}
    assert asked == {
        fx.oa_doi_url(fx.GADGETS_DOI), fx.s2_paper_url(f"DOI:{fx.GADGETS_DOI}"),
        fx.oa_citing_url(fx.GADGETS_W), fx.oa_reviews_url(fx.GADGETS_W), fx.oa_references_url(fx.GADGETS_W),
        fx.oa_author_works_url("A9400000001"),
    }
    assert _dossier(second, fx.GADGETS_SEED)["mechanical_state"] == "open"
    assert _row(store, fx.GADGETS_SEED)["resolution"] == "exact"
    # the rate-limited answers are kept, retired behind the new ones
    rows = store.knowledge.execute(
        "SELECT provider, outcome, superseded_by FROM source_evidence WHERE subject_key = ? AND kind = 'record' "
        "ORDER BY fetched_ts", (fx.GADGETS_DOI,),
    ).fetchall()
    retired = [(r["provider"], r["outcome"]) for r in rows if r["superseded_by"] is not None]
    live = {(r["provider"], r["outcome"]) for r in rows if r["superseded_by"] is None}
    assert sorted(retired) == [("openalex", "rate_limited"), ("semanticscholar", "rate_limited")]
    assert live == {("openalex", "record"), ("semanticscholar", "not_found")}


def test_investigate_cache_serves_second_run_without_calls(store, tmp_path):
    launch = bootstrap_launch(store)
    transport = FakeTransport()
    fx.route_widgets(transport)

    first = _run(store, transport, [_seed(fx.WIDGETS_SEED)], tmp_path, launch_id=launch)
    asked = len(transport.calls)
    one = _dossier(first, fx.WIDGETS_SEED)
    dossier_id = _row(store, fx.WIDGETS_SEED)["dossier_id"]

    second = _run(store, transport, [_seed(fx.WIDGETS_SEED)], tmp_path, launch_id=launch)

    assert len(transport.calls) == asked  # not one request
    assert second["cache"]["misses"] == 0
    assert second["cache"]["hits"] == first["cache"]["misses"] == 7
    two = _dossier(second, fx.WIDGETS_SEED)
    assert two["evidence"]["citing"] == one["evidence"]["citing"]
    assert two["evidence"]["author_works"] == one["evidence"]["author_works"]
    assert two["resolution"]["record"] == one["resolution"]["record"]
    assert two["calls_used"] == one["calls_used"] == 5
    cache_words = {c["cache"] for t in two["resolution"]["tried"] + two["evidence"]["calls"] for c in t["provider_calls"]}
    assert cache_words == {"hit"}
    assert _row(store, fx.WIDGETS_SEED)["dossier_id"] == dossier_id  # upserted, not duplicated


def test_investigate_no_cache_asks_again_and_retires_the_cached_answers(store, tmp_path):
    launch = bootstrap_launch(store)
    transport = FakeTransport()
    fx.route_widgets(transport)
    _run(store, transport, [_seed(fx.WIDGETS_SEED)], tmp_path, launch_id=launch)
    asked = len(transport.calls)

    again = _run(store, transport, [_seed(fx.WIDGETS_SEED)], tmp_path, launch_id=launch, use_cache=False)

    assert len(transport.calls) == 2 * asked
    assert again["cache"] == {"enabled": False, "hits": 0, "misses": 7}
    live = store.knowledge.execute("SELECT COUNT(*) FROM source_evidence WHERE superseded_by IS NULL").fetchone()[0]
    total = store.knowledge.execute("SELECT COUNT(*) FROM source_evidence").fetchone()[0]
    assert (live, total) == (7, 14)


def test_investigate_respects_max_calls_per_seed(store, tmp_path):
    launch = bootstrap_launch(store)
    transport = FakeTransport()
    fx.route_widgets(transport)

    result = _run(store, transport, [_seed(fx.WIDGETS_SEED)], tmp_path, launch_id=launch, max_calls_per_seed=2)

    dossier = _dossier(result, fx.WIDGETS_SEED)
    assert dossier["calls_used"] == 2 and result["calls_used"] == 2
    assert [t["call"] for t in dossier["resolution"]["tried"]] == ["lookup_doi"]
    assert [c["call"] for c in dossier["evidence"]["calls"]] == ["get_citations"]
    assert [s["call"] for s in dossier["evidence"]["skipped"]] == ["get_citations", "get_references", "get_author_works"]
    assert dossier["flags"]["call_cap_reached"] is True
    assert dossier["thresholds"]["max_calls_per_seed"] == 2
    assert len(dossier["evidence"]["citing"]) == 2 and dossier["evidence"]["citing_reviews"] == []
    # two lookups, then OpenAlex's DOI-to-work-id step and the citing listing
    assert len(transport.calls) == 4

    # a cap that cuts the resolve leaves the seed unresolved and says so
    lost = 'A. Author, "A Lost Work", 1990, doi:10.9999/lost'
    transport.add_json(fx.oa_doi_url("10.9999/lost"), json_body={"error": "Not Found"}, status_code=404)
    transport.add_json(fx.s2_paper_url("DOI:10.9999/lost"), json_body={"error": "Paper not found"}, status_code=404)
    cut = _run(store, transport, [_seed(lost)], tmp_path, launch_id=launch, max_calls_per_seed=1)
    cut_dossier = _dossier(cut, lost)
    assert cut_dossier["mechanical_state"] == "need_info"
    assert [s["call"] for s in cut_dossier["resolution"]["skipped"]] == ["search"]
    assert cut_dossier["flags"]["call_cap_reached"] is True

    with pytest.raises(InvestigateError) as refused:
        _run(store, transport, [_seed(lost)], tmp_path, launch_id=launch, max_calls_per_seed=0)
    assert refused.value.code == "max_calls_invalid"


def test_investigate_title_only_seed_resolves_by_search(store, tmp_path):
    launch = bootstrap_launch(store)
    transport = FakeTransport()
    fx.route_widgets_search(transport)
    fx.route_widgets(transport)

    result = _run(store, transport, [_seed(fx.TITLE_ONLY_SEED)], tmp_path, launch_id=launch)

    dossier = _dossier(result, fx.TITLE_ONLY_SEED)
    assert [t["call"] for t in dossier["resolution"]["tried"]] == ["search"]
    search = dossier["resolution"]["tried"][0]
    assert search["params"] == {"query": "A Study of Widgets", "limit": 5}
    assert len(search["candidates"]) == 1  # both providers' hits reconciled into one record
    assert dossier["resolution"]["status"] == "exact"
    assert dossier["resolution"]["via"] == "title"
    assert dossier["resolution"]["record"]["doi"] == fx.WIDGETS_DOI
    assert dossier["subject_key"] == fx.WIDGETS_DOI
    assert dossier["mechanical_state"] == "open"
    assert [e["title"] for e in dossier["evidence"]["citing_reviews"]] == ["A Review of Widget Studies"]
    assert fx.s2_paper_url(f"DOI:{fx.WIDGETS_DOI}") not in {c["url"] for c in transport.calls}


def test_pre_cutoff_seed_is_flagged_foundational(store, tmp_path):
    """``first_pub_year < foundational_before`` sets ``flags.foundational``
    and nothing else -- no verdict. The first publication year is a reprint
    pair's first year, else the year the citation gives."""
    launch = bootstrap_launch(store)
    transport = FakeTransport()
    fx.route_gadgets(transport)
    fx.route_widgets(transport)
    reprint = 'G. Early, "A Theory of Gadgets", 1969/2002, doi:10.9999/gadgets'
    seeds = [_seed(fx.GADGETS_SEED, line=1), _seed(reprint, line=2), _seed(fx.WIDGETS_SEED, line=3)]

    result = _run(store, transport, seeds, tmp_path, launch_id=launch, config=_CUTOFF_1980)

    gadgets = _dossier(result, fx.GADGETS_SEED)
    assert gadgets["flags"]["foundational"] is True
    assert (gadgets["flags"]["first_pub_year"], gadgets["flags"]["first_pub_year_from"]) == (1969, "cited_year")
    reprinted = _dossier(result, reprint)
    assert reprinted["flags"]["foundational"] is True
    assert (reprinted["flags"]["first_pub_year"], reprinted["flags"]["first_pub_year_from"]) == (1969, "reprint_pair")
    assert reprinted["resolution"]["status"] == "exact"  # the 1969 record matches the reprint pair's first year
    widgets = _dossier(result, fx.WIDGETS_SEED)
    assert widgets["flags"]["foundational"] is False  # 1986 is after this run's 1980 cut-off
    assert widgets["thresholds"]["foundational_before"] == 1980
    assert all(_row(store, s.seed_raw)["verdict"] is None for s in seeds)
    assert {d["mechanical_state"] for d in result["dossiers"]} == {"open"}


def test_dossier_carries_the_brief_schema_and_truncates_the_abstract(store, tmp_path):
    launch = bootstrap_launch(store)
    transport = FakeTransport()
    fx.route_widgets(transport)
    config = InvestigateConfig(abstract_max_words=5)

    result = _run(
        store, transport, [Seed("list-1", "row-1", fx.WIDGETS_SEED, "What are widgets for?", ["survey"], 1, 0)],
        tmp_path, launch_id=launch, config=config,
    )

    dossier = _dossier(result, fx.WIDGETS_SEED)
    for key in (
        "dossier_version", "list_id", "row_id", "seed_raw", "question", "literature", "parsed", "resolution",
        "held", "evidence", "flags", "mechanical_state", "thresholds", "calls_used", "stage_version",
        "created_by_launch", "ts",
    ):
        assert key in dossier, key
    assert dossier["dossier_version"] == 1
    assert (dossier["question"], dossier["literature"]) == ("What are widgets for?", ["survey"])
    assert set(dossier["resolution"]) >= {"status", "record", "match", "tried"}
    assert set(dossier["resolution"]["tried"][0]) >= {"call", "params", "provider_outcomes"}
    evidence = dossier["evidence"]
    assert set(evidence) >= {
        "abstract", "abstract_provider", "work_type", "cited_by_count", "counts_by_year", "citing",
        "citing_reviews", "references", "author_works", "oa_pdf_url", "arxiv_neighbours",
    }
    assert set(dossier["flags"]) >= {"foundational", "first_pub_year", "no_abstract", "wrong_identifier"}
    assert dossier["thresholds"] == {**config.to_dict(), "max_calls_per_seed": 8}

    # the longer abstract (Semantic Scholar's) won the merge, and is cut at five words
    assert evidence["abstract"] == "Widgets are studied here at"
    assert evidence["abstract_truncated"] is True
    assert evidence["abstract_provider"] == "semanticscholar"
    assert dossier["resolution"]["record"]["abstract"] == "Widgets are studied here at"
    assert "referenced_works" not in json.dumps(dossier["resolution"]["record"]["other"])
    assert evidence["work_type"] == "article"
    assert evidence["cited_by_count"] == 250
    assert evidence["counts_by_year"] == [{"year": 2025, "cited_by_count": 4}, {"year": 2024, "cited_by_count": 6}]
    assert [a["author_id"] for a in evidence["author_works"]] == list(fx.WIDGETS_AUTHORS)
    assert [w["title"] for w in evidence["author_works"][0]["works"]] == ["Widgets Revisited"]
    assert evidence["author_works"][1]["works"] == []
    assert [r["title"] for r in evidence["references"]] == ["Early Notes on Widgets"]
    assert evidence["arxiv_neighbours"] is None  # not asked for
    assert dossier["flags"]["no_abstract"] is False
    author_call = next(c for c in evidence["calls"] if c["call"] == "get_author_works")
    assert author_call["params"] == {"authors": 3, "since_year": fx.SINCE_YEAR, "limit": 8}


def test_seeds_file_rows_split_and_bad_lines_refused(tmp_path):
    good = fx.write_seeds(
        tmp_path / "seeds.jsonl",
        [
            {"list_id": "list-1", "row_id": "row-1", "question": "Q?",
             "seeds_raw": f"{fx.WIDGETS_SEED}; {fx.GADGETS_SEED}"},
            fx.seed_row(fx.WRONG_DOI_SEED, row_id="row-2"),
            fx.seed_row(fx.WRONG_DOI_SEED, row_id="row-2"),
        ],
    )
    seeds, warnings = load_seeds(good)
    assert [(s.row_id, s.seed_raw, s.line, s.position) for s in seeds] == [
        ("row-1", fx.WIDGETS_SEED, 1, 0), ("row-1", fx.GADGETS_SEED, 1, 1), ("row-2", fx.WRONG_DOI_SEED, 2, 0),
    ]
    assert seeds[0].question == "Q?" and len(warnings) == 1

    bad = tmp_path / "bad.jsonl"
    bad.write_text(
        "\n".join([
            "not json",
            json.dumps({"list_id": "../up", "row_id": "r", "seed_raw": "x"}),
            json.dumps({"list_id": "l", "row_id": "r", "seed_raw": "x", "seeds_raw": "y"}),
            json.dumps({"list_id": "l", "row_id": "r"}),
        ]),
        encoding="utf-8",
    )
    with pytest.raises(InvestigateError) as refused:
        load_seeds(bad)
    assert refused.value.code == "seeds_file_invalid"
    assert len(refused.value.details["problems"]) == 4


def test_investigate_refuses_an_unknown_launch_or_an_unsafe_id_before_asking_anything(store, tmp_path):
    launch = bootstrap_launch(store)
    transport = FakeTransport()
    with pytest.raises(XidTargetMissingError):
        _run(store, transport, [_seed(fx.WIDGETS_SEED)], tmp_path, launch_id="LNCH-nobody")
    with pytest.raises(InvestigateError) as refused:
        _run(store, transport, [_seed(fx.WIDGETS_SEED, row_id="../outside")], tmp_path, launch_id=launch)
    assert refused.value.code == "seeds_invalid"
    assert transport.calls == []
    assert not (tmp_path / "outside").exists()


def test_arxiv_neighbours_attached_per_question(store, tmp_path):
    launch = bootstrap_launch(store)
    transport = FakeTransport()
    fx.route_widgets(transport)
    fx.route_wrong_doi(transport)
    asked: list[list[str]] = []

    def neighbours(questions):
        asked.append(list(questions))
        return [[{"arxiv_id": f"2101.0000{i}", "title": f"Near {q}", "doi": None}] for i, q in enumerate(questions)]

    seeds = [
        Seed("list-1", "row-1", fx.WIDGETS_SEED, "What are widgets for?", None, 1, 0),
        Seed("list-1", "row-2", fx.WRONG_DOI_SEED, "What are widgets for?", None, 2, 0),
    ]
    result = _run(store, transport, seeds, tmp_path, launch_id=launch, neighbours=neighbours)

    assert asked == [["What are widgets for?"]]  # one pass over the distinct questions
    assert result["arxiv_neighbours"]["ran"] is True
    for raw in (fx.WIDGETS_SEED, fx.WRONG_DOI_SEED):
        dossier = _dossier(result, raw)
        assert dossier["evidence"]["arxiv_neighbours"] == [
            {"arxiv_id": "2101.00000", "title": "Near What are widgets for?", "doi": None}
        ]
        assert _row(store, raw)["dossier_sha256"] == _sha(_dossier_path(result, raw))


# ===========================================================================
# B3 -- the held check
# ===========================================================================


def _source(store, launch, *, title, year=None, doi=None, isbn=None, state="indexed"):
    return pipeline.register_source(
        store, kind="paper", title=title, license_tier="unknown", acquisition_route="user_delivered",
        registered_by_launch=launch, doi=doi, isbn=isbn, year=year, request_state=state,
    )


def test_held_by_doi_and_by_title_year(store):
    launch = bootstrap_launch(store)
    by_doi = _source(store, launch, title="A Study of Widgets", year=1986, doi=fx.WIDGETS_DOI)
    by_title = _source(store, launch, title="A Theory of Gadgets", year=1969, state="delivered")

    by_doi_match = held_match(store, parse_citation(fx.WIDGETS_SEED), None)
    assert (by_doi_match.match_on, by_doi_match.form, by_doi_match.source_id) == ("doi", "exact", by_doi["source_id"])
    assert by_doi_match.request_state == "indexed"

    within = held_match(store, parse_citation('G. Early, "A Theory of Gadgets", 1970'), None)
    assert (within.match_on, within.form, within.source_id) == ("title", "exact", by_title["source_id"])
    assert held_match(store, parse_citation('G. Early, "A Theory of Gadgets", 1975'), None) is None

    near = held_match(store, parse_citation('G. Early, "A Theory of the Gadgets", 1969'), None)
    assert (near.match_on, near.form) == ("title", "probable")
    assert near.title_similarity >= 0.9

    # the resolved record's own identifiers count too
    via_record = held_match(
        store, parse_citation("A. Nobody, 2001"), WorkRecord(title="Other", doi="10.9999/WIDGETS", year=1986),
    )
    assert via_record.source_id == by_doi["source_id"]


def test_open_request_is_reported_as_duplicate_ask(store):
    launch = bootstrap_launch(store)
    asked = _source(store, launch, title="A Review of Widget Studies", year=2015, doi="10.9999/widgets.review", state="wanted")
    _source(store, launch, title="A Theory of Gadgets", year=1969, doi=fx.GADGETS_DOI, state="rejected")

    match = held_match(store, parse_citation("A. Reviewer, 2015, doi:10.9999/widgets.review"), None)
    assert (match.match_on, match.matched_key, match.request_state) == ("open-request", "doi", "wanted")
    assert match.source_id == asked["source_id"]
    # a rejected ask is neither a held text nor an open one
    assert held_match(store, parse_citation(fx.GADGETS_SEED), None) is None

    # a held text of the same work outranks the open ask
    held = _source(store, launch, title="A Review of Widget Studies", year=2015, state="indexed")
    outranked = held_match(store, parse_citation('A. Reviewer, "A Review of Widget Studies", 2015, doi:10.9999/widgets.review'), None)
    assert (outranked.match_on, outranked.source_id) == ("title", held["source_id"])


def test_delivered_manifest_hit_reports_path(store, tmp_path):
    launch = bootstrap_launch(store)
    manifest_path = tmp_path / "delivered.tsv"
    manifest_path.write_text(
        "path\ttitle\tdoi\tisbn\n"
        "delivered/widgets.pdf\tA Study of Widgets\t10.9999/WIDGETS\t\n"
        "delivered/gadgets.djvu\tA Theory of Gadgets\t\t0-306-40615-2\n",
        encoding="utf-8",
    )
    manifest = load_delivered_manifest(manifest_path)

    hit = held_match(store, parse_citation(fx.WIDGETS_SEED), None, manifest=manifest)
    assert (hit.origin, hit.match_on, hit.path, hit.source_id) == ("manifest", "doi", "delivered/widgets.pdf", None)
    by_isbn = held_match(store, parse_citation("G. Early, Gadgets, 1969, ISBN 978-0-306-40615-7"), None, manifest=manifest)
    assert (by_isbn.match_on, by_isbn.path) == ("isbn", "delivered/gadgets.djvu")

    # through a run: held, the path in the dossier, no gather
    transport = FakeTransport()
    fx.route_widgets_lookup(transport)
    result = _run(store, transport, [_seed(fx.WIDGETS_SEED)], tmp_path, launch_id=launch, manifest=manifest)
    dossier = _dossier(result, fx.WIDGETS_SEED)
    assert dossier["mechanical_state"] == "held"
    assert dossier["held"]["path"] == "delivered/widgets.pdf"
    assert dossier["evidence"]["calls"] == []
    assert _row(store, fx.WIDGETS_SEED)["held_source_id"] is None

    headerless = tmp_path / "bad.tsv"
    headerless.write_text("delivered/x.pdf\tX\t\t\n", encoding="utf-8")
    with pytest.raises(InvestigateError) as refused:
        load_delivered_manifest(headerless)
    assert refused.value.code == "manifest_invalid"


def test_held_seed_is_not_gathered_and_names_its_source(store, tmp_path):
    launch = bootstrap_launch(store)
    source = _source(store, launch, title="A Study of Widgets", year=1986, doi=fx.WIDGETS_DOI)
    transport = FakeTransport()
    fx.route_widgets(transport)

    result = _run(store, transport, [_seed(fx.WIDGETS_SEED)], tmp_path, launch_id=launch)

    dossier = _dossier(result, fx.WIDGETS_SEED)
    assert dossier["mechanical_state"] == "held"
    assert dossier["resolution"]["status"] == "exact"
    assert dossier["held"]["source_id"] == source["source_id"]
    assert dossier["evidence"]["calls"] == [] and dossier["evidence"]["abstract"]  # resolved, just not gathered
    assert len(transport.calls) == 2
    assert _row(store, fx.WIDGETS_SEED)["held_source_id"] == source["source_id"]


def test_a_mistyped_doi_that_names_a_held_source_is_not_held(store, tmp_path):
    """The trap the held check must not fall into: the programme holds the
    unrelated work the mistyped DOI points at. Matching on that DOI would call
    the cited work held when it is not; the resolution says the DOI names
    another work, so the DOI is not trusted for the held check."""
    launch = bootstrap_launch(store)
    _source(store, launch, title="An Unrelated Evaluation of Boards", year=1985, doi=fx.BOARDS_DOI)
    transport = FakeTransport()
    fx.route_wrong_doi(transport)

    result = _run(store, transport, [_seed(fx.WRONG_DOI_SEED)], tmp_path, launch_id=launch)

    dossier = _dossier(result, fx.WRONG_DOI_SEED)
    assert dossier["mechanical_state"] == "wrong_identifier"
    assert dossier["held"] is None


# ===========================================================================
# B4 -- verdicts and the operator line
# ===========================================================================

_NEED_INFO_SEED = "Anonymous, Widget lore, n.d."
_REVIEW_DOI = "10.9999/widgets.review"
_REVIEW_SEED = "A. Reviewer, 2015, doi:10.9999/widgets.review"
_REPRINT_SEED = 'G. Early, "A Theory of Gadgets", 1969/2002, doi:10.9999/gadgets'


def _route_everything(transport):
    fx.route_widgets(transport)
    fx.route_widgets_search(transport)
    fx.route_gadgets(transport)
    fx.route_wrong_doi(transport)
    transport.add_json(fx.oa_search_url(_NEED_INFO_SEED), json_body=fx.EMPTY_PAGE)
    transport.add_json(fx.s2_search_url(_NEED_INFO_SEED), json_body={"total": 0, "offset": 0, "data": []})
    transport.add_json(fx.oa_doi_url(_REVIEW_DOI), json_body=load_fixture("investigate_openalex_citing.json")["results"][0])
    transport.add_json(fx.s2_paper_url(f"DOI:{_REVIEW_DOI}"), json_body={"error": "Paper not found"}, status_code=404)


@pytest.fixture()
def judged(store, tmp_path):
    """A run over one of every state the verdict rules care about."""
    launch = bootstrap_launch(store)
    _source(store, launch, title="A Review of Widget Studies", year=2015, doi=_REVIEW_DOI)
    transport = FakeTransport()
    _route_everything(transport)
    seeds = [
        Seed("list-1", "row-1", fx.WIDGETS_SEED, "What are widgets for?", None, 1, 0),
        Seed("list-1", "row-1", _NEED_INFO_SEED, "What are widgets for?", None, 2, 0),
        Seed("list-1", "row-2", fx.GADGETS_SEED, "Where did gadget theory come from?", None, 3, 0),
        Seed("list-1", "row-2", fx.TITLE_ONLY_SEED, "Where did gadget theory come from?", None, 4, 0),
        Seed("list-1", "row-3", _REVIEW_SEED, "What has already been read?", None, 5, 0),
        Seed("list-1", "row-3", _REPRINT_SEED, "What has already been read?", None, 6, 0),
        Seed("list-2", "row-1", fx.WIDGETS_SEED, "Which widget studies hold up?", None, 7, 0),
        Seed("list-2", "row-1", fx.WRONG_DOI_SEED, "Which widget studies hold up?", None, 8, 0),
    ]
    result = _run(store, transport, seeds, tmp_path, launch_id=launch, config=_CUTOFF_1980)
    paths = {(d["list_id"], d["seed_raw"]): d["dossier_path"] for d in result["dossiers"]}
    states = {(d["list_id"], d["seed_raw"]): d["mechanical_state"] for d in result["dossiers"]}
    assert states[("list-1", fx.WIDGETS_SEED)] == "open"
    assert states[("list-1", _NEED_INFO_SEED)] == "need_info"
    assert states[("list-1", _REVIEW_SEED)] == "held"
    assert states[("list-2", fx.WRONG_DOI_SEED)] == "wrong_identifier"
    return {"launch": launch, "paths": paths, "result": result}


def _verdict(store, judged, seed_raw, verdict, *, list_id="list-1", **kw):
    return record_verdict(store, judged["paths"][(list_id, seed_raw)], verdict=verdict, launch_id=judged["launch"], **kw)


def _refused(store, judged, seed_raw, verdict, code, *, list_id="list-1", **kw):
    path = Path(judged["paths"][(list_id, seed_raw)])
    before = path.read_bytes()
    row_before = _row(store, seed_raw, list_id)
    with pytest.raises(InvestigateError) as refused:
        _verdict(store, judged, seed_raw, verdict, list_id=list_id, **kw)
    assert refused.value.code == code, refused.value
    assert path.read_bytes() == before  # nothing written, to the file ...
    assert _row(store, seed_raw, list_id) == row_before  # ... or to the row
    return refused.value


def test_verdict_request_refused_for_unresolved_or_mismatch(store, judged):
    """FAILS BEFORE lane SI part B: no verdict verb existed."""
    for verdict in ("REQUEST", "REQUEST-AS-FOUNDATIONAL"):
        _refused(store, judged, fx.WRONG_DOI_SEED, verdict, "resolution_not_requestable", list_id="list-2",
                 detail={"founds": "x", "consolidator": "y", "foundational_reason": "own-text"})
        _refused(store, judged, _NEED_INFO_SEED, verdict, "resolution_not_requestable",
                 detail={"founds": "x", "consolidator": "y", "foundational_reason": "own-text"})
    assert _row(store, fx.WRONG_DOI_SEED, "list-2")["verdict"] is None

    # what a wrong identifier CAN be given
    dropped = _verdict(store, judged, fx.WRONG_DOI_SEED, "DROP", list_id="list-2", reason_code="wrong-identifier")
    assert dropped["verdict"] == "DROP"
    asked = _verdict(store, judged, _NEED_INFO_SEED, "NEED-INFO", detail={"question": "Which work is meant?"})
    assert asked["detail"]["question"] == "Which work is meant?"


def test_verdict_foundational_requires_consolidator_and_reason(store, judged):
    _refused(store, judged, fx.GADGETS_SEED, "REQUEST", "foundational_needs_request_as_foundational",
             detail={"why": "x"})
    refused = _refused(store, judged, fx.GADGETS_SEED, "REQUEST-AS-FOUNDATIONAL", "foundational_detail_incomplete")
    assert len(refused.details["missing"]) == 3
    _refused(store, judged, fx.GADGETS_SEED, "REQUEST-AS-FOUNDATIONAL", "foundational_detail_incomplete",
             detail={"founds": "gadget theory", "foundational_reason": "own-text"})
    _refused(store, judged, fx.GADGETS_SEED, "REQUEST-AS-FOUNDATIONAL", "foundational_detail_incomplete",
             detail={"founds": "gadget theory", "consolidator": "A Review", "foundational_reason": "because"})

    recorded = _verdict(
        store, judged, fx.GADGETS_SEED, "REQUEST-AS-FOUNDATIONAL",
        detail={"founds": "gadget theory", "consolidator": None, "foundational_reason": "no-consolidator"},
    )
    assert recorded["verdict"] == "REQUEST-AS-FOUNDATIONAL"
    # an ordinary, non-foundational, resolved work takes a plain REQUEST
    assert _verdict(store, judged, fx.WIDGETS_SEED, "REQUEST", detail={"why": "the primary study"})["verdict"] == "REQUEST"


def test_substitute_must_come_from_dossier_evidence(store, judged):
    _refused(store, judged, fx.WIDGETS_SEED, "SUBSTITUTE-WITH", "substitute_required")
    _refused(store, judged, fx.WIDGETS_SEED, "SUBSTITUTE-WITH", "substitute_not_in_evidence",
             substitute=("doi", "10.9999/not-in-the-evidence"))
    _refused(store, judged, fx.WIDGETS_SEED, "SUBSTITUTE-WITH", "substitute_not_in_evidence",
             substitute=("isbn", "0-306-40615-2"))
    _refused(store, judged, fx.WIDGETS_SEED, "DROP", "substitute_not_allowed", substitute=("doi", _REVIEW_DOI))

    recorded = _verdict(store, judged, fx.WIDGETS_SEED, "SUBSTITUTE-WITH", substitute=("doi", "10.9999/WIDGETS.REVIEW"),
                        detail={"why": "the review consolidates it"})
    assert recorded["detail"]["substitute"] == {"kind": "doi", "id": _REVIEW_DOI}
    # an author's later work and a reference are evidence too
    assert _verdict(store, judged, fx.TITLE_ONLY_SEED, "SUBSTITUTE-WITH", substitute=("doi", "10.9999/widgets.revisited"))


def test_second_verdict_needs_supersede_and_keeps_history(store, judged):
    first = _verdict(store, judged, fx.WIDGETS_SEED, "REQUEST", detail={"why": "first reading"})
    with pytest.raises(InvestigateError) as refused:
        _verdict(store, judged, fx.WIDGETS_SEED, "DROP")
    assert refused.value.code == "verdict_exists"

    second = _verdict(store, judged, fx.WIDGETS_SEED, "DROP", supersede=True, reason_code="duplicate")
    third = _verdict(store, judged, fx.WIDGETS_SEED, "NEED-INFO", supersede=True, detail={"question": "Which edition?"})

    assert second["superseded"] == "REQUEST" and third["superseded"] == "DROP"
    row = _row(store, fx.WIDGETS_SEED)
    assert row["verdict"] == "NEED-INFO"
    detail = json.loads(row["verdict_detail_json"])
    assert [h["verdict"] for h in detail["history"]] == ["REQUEST", "DROP"]
    assert detail["history"][0]["detail"] == {"why": "first reading"}
    assert detail["history"][0]["by_launch"] == judged["launch"] and detail["history"][0]["ts"] == first["ts"]
    assert detail["history"][1]["detail"] == {"reason_code": "duplicate"}
    path = Path(judged["paths"][("list-1", fx.WIDGETS_SEED)])
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["verdict"]["verdict"] == "NEED-INFO"
    assert on_disk["verdict"]["detail"] == detail
    assert row["dossier_sha256"] == _sha(path)


def test_verdict_refuses_a_changed_dossier_a_missing_held_block_and_an_unknown_launch(store, judged):
    _refused(store, judged, fx.WIDGETS_SEED, "HELD", "held_block_missing")
    assert _verdict(store, judged, _REVIEW_SEED, "HELD")["verdict"] == "HELD"

    path = Path(judged["paths"][("list-1", fx.GADGETS_SEED)])
    before = path.read_bytes()
    with pytest.raises(XidTargetMissingError):
        record_verdict(store, path, verdict="DROP", launch_id="LNCH-nobody")
    assert path.read_bytes() == before

    edited = json.loads(before)
    edited["evidence"]["citing"].append({"doi": "10.9999/planted"})
    path.write_text(json.dumps(edited), encoding="utf-8")
    _refused(store, judged, fx.GADGETS_SEED, "SUBSTITUTE-WITH", "dossier_changed", substitute=("doi", "10.9999/planted"))


def test_rerun_never_touches_a_judged_dossier(store, judged, tmp_path):
    _verdict(store, judged, fx.WIDGETS_SEED, "REQUEST", detail={"why": "x"})
    path = Path(judged["paths"][("list-1", fx.WIDGETS_SEED)])
    before = path.read_bytes()
    transport = FakeTransport()
    _route_everything(transport)

    again = _run(store, transport, [_seed(fx.WIDGETS_SEED)], tmp_path, launch_id=judged["launch"])

    assert again["skipped"]["verdict_recorded"] == 1 and again["investigated"] == 0
    assert path.read_bytes() == before
    assert transport.calls == []


def test_render_refuses_row_with_an_unruled_wrong_identifier_seed(store, judged):
    """A mismatch seed with no verdict yet blocks the whole render: nobody has
    decided what happens to the wrong identifier, so it could still be sent."""
    _verdict(store, judged, fx.WIDGETS_SEED, "REQUEST", list_id="list-2", detail={"why": "x"})

    with pytest.raises(InvestigateError) as refused:
        render_list(store, "list-2")

    assert refused.value.code == "render_refused"
    assert refused.value.details["rows"] == [
        {
            "row_id": "row-1",
            "seeds": [
                {
                    "seed_raw": fx.WRONG_DOI_SEED, "resolution": "mismatch",
                    "reason": "resolution mismatch: the cited identifier resolves to another work, "
                              "and no DROP or SUBSTITUTE-WITH verdict rules it out",
                    "dossier_path": _row(store, fx.WRONG_DOI_SEED, "list-2")["dossier_path"],
                }
            ],
        }
    ]
    assert fx.WRONG_DOI_SEED in str(refused.value)


def test_render_accepts_a_wrong_identifier_seed_once_it_is_dropped(store, judged):
    """DROP prints nothing, so the wrong identifier cannot reach the fetch
    list: the row renders, its dropped count holds the seed, and the cited
    identifier appears nowhere in the output."""
    _verdict(store, judged, fx.WIDGETS_SEED, "REQUEST", list_id="list-2", detail={"why": "x"})
    _verdict(store, judged, fx.WRONG_DOI_SEED, "DROP", list_id="list-2")

    out = render_list(store, "list-2")

    assert out["counts"]["dropped"] == 1
    rendered = "\n".join(out["lines"]) if "lines" in out else json.dumps(out)
    wrong = _row(store, fx.WRONG_DOI_SEED, "list-2")
    assert fx.WRONG_DOI_SEED not in rendered
    assert wrong["resolution"] == "mismatch"


def test_render_refuses_a_seed_nothing_was_tried_for(store, tmp_path):
    """A bare ISBN: no provider looks an ISBN up and there is no prose to
    search on, so nothing was tried -- not a seed to put in front of anyone."""
    launch = bootstrap_launch(store)
    transport = FakeTransport()
    result = _run(store, transport, [_seed("ISBN 0-306-40615-2")], tmp_path, launch_id=launch)
    dossier = _dossier(result, "ISBN 0-306-40615-2")
    assert dossier["resolution"]["tried"] == [] and dossier["mechanical_state"] == "need_info"
    assert transport.calls == []

    with pytest.raises(InvestigateError) as refused:
        render_list(store, "list-1")
    assert refused.value.details["rows"][0]["seeds"][0]["reason"] == "resolution none with nothing tried"


def _judge_the_golden_list(store, judged):
    _verdict(store, judged, fx.WIDGETS_SEED, "REQUEST", detail={"why": "the primary study"})
    _verdict(store, judged, _NEED_INFO_SEED, "NEED-INFO", detail={"question": "Which work on widget lore is meant, and by whom?"})
    _verdict(
        store, judged, fx.GADGETS_SEED, "REQUEST-AS-FOUNDATIONAL",
        detail={"founds": "gadget theory", "consolidator": "A Review of Widget Studies (does not cover gadgets)",
                "foundational_reason": "own-text", "why": "the origin of the theory"},
    )
    _verdict(store, judged, fx.TITLE_ONLY_SEED, "SUBSTITUTE-WITH", substitute=("doi", _REVIEW_DOI),
             detail={"why": "the review consolidates the study"})
    _verdict(store, judged, _REVIEW_SEED, "HELD")
    _verdict(store, judged, _REPRINT_SEED, "DROP", reason_code="duplicate-of-row-2")


def test_render_lines_golden(store, judged):
    _judge_the_golden_list(store, judged)

    rendered = render_list(store, "list-1")

    golden = (FIXTURES_DIR / "investigate_render_golden.txt").read_text(encoding="utf-8")
    assert rendered["text"] + "\n" == golden
    assert rendered["counts"] == {"held": 1, "dropped": 1, "substituted": 1, "unjudged": 0, "fetch": 3, "ask": 1}


def test_render_json_and_unjudged_count(store, judged):
    _verdict(store, judged, fx.WIDGETS_SEED, "REQUEST", detail={"why": "x"})

    rendered = render_list(store, "list-1", fmt="json")

    assert rendered["format"] == "json"
    assert [r["row_id"] for r in rendered["rows"]] == ["row-1", "row-2", "row-3"]
    assert rendered["counts"]["unjudged"] == 5
    assert rendered["lines"][-1] == "(held 0 · dropped 0 · substituted 0 · unjudged 5)"
    first = rendered["rows"][0]["seeds"][0]
    assert first["verdict"] == "REQUEST" and first["line"].startswith("FETCH Cat Writer & Dan Other, A Study of Widgets")
    with pytest.raises(InvestigateError) as refused:
        render_list(store, "no-such-list")
    assert refused.value.code == "list_not_found"
