"""``static/evidence_render.js`` under the Node DOM shim -- lane C, C6.

The payloads driven through the renderer here are built by the REAL builder
(``build_evidence_panel`` over the same ``traced`` fixture
``tests/test_dashboard_evidence.py`` uses), not hand-written JSON. That is the
whole point of the split: a renderer test that invents its own input can go on
passing after the server's shape moves, which is exactly the drift the sweep's
harness exists to catch.

Node is not a project dependency, so every test that needs it skips with a
stated reason when it is absent (the repo's enumerated-skip discipline).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from trialerror.dashboard import data
from trialerror.dashboard.store_ro import open_store_ro
from trialerror.stores.store import open_store
from trialerror.stores.writer import insert as store_insert
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests.test_dashboard_evidence import traced  # noqa: F401 - the shared fixture

REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "tests" / "_console_render_harness.js"
EVIDENCE_RENDER_JS = REPO_ROOT / "trialerror" / "dashboard" / "static" / "evidence_render.js"

NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node not on PATH -- Evidence DOM tests need Node >= 18")


def run_harness(fn: str, args: list, *, select: str | None = None, expect_ok: bool = True) -> dict:
    cmd = [NODE, str(HARNESS), "--module", "evidence_render.js", "--fn", fn]
    with tempfile.TemporaryDirectory() as tmp:
        args_file = Path(tmp) / "args.json"
        args_file.write_text(json.dumps(args), encoding="utf-8")
        cmd += ["--args-file", str(args_file)]
        if select:
            cmd += ["--select", select]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", cwd=str(REPO_ROOT))
    assert proc.stdout, f"harness printed nothing; stderr={proc.stderr!r}"
    payload = json.loads(proc.stdout)
    if expect_ok:
        assert payload.get("kind") != "error", payload.get("error")
        assert proc.returncode == 0, proc.stderr
    return payload


def find_all(tree: dict, predicate) -> list[dict]:
    out = []
    if predicate(tree):
        out.append(tree)
    for child in tree.get("children", []):
        out.extend(find_all(child, predicate))
    return out


def by_class(tree: dict, cls: str) -> list[dict]:
    return find_all(tree, lambda n: cls in n.get("classes", []))


def by_tag(tree: dict, tag: str) -> list[dict]:
    return find_all(tree, lambda n: n.get("tag") == tag)


def texts(nodes: list[dict]) -> list[str]:
    return [n.get("text", "") for n in nodes]


@pytest.fixture()
def panel(traced):  # noqa: F811
    rostore, ids = traced
    return data.build_evidence_panel(rostore, claim_id=ids["claim"]), ids


# ---------------------------------------------------------------------------
# the file itself -- no Node needed
# ---------------------------------------------------------------------------


def test_evidence_render_js_never_touches_document():
    """The rule that makes this file testable. One ``document.createElement``
    would work in the browser and fail every test below for a reason that
    reads as a harness bug."""
    source = EVIDENCE_RENDER_JS.read_text(encoding="utf-8")
    body = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith(("*", "/*", "//"))
    )
    assert "document." not in body


def test_evidence_render_js_is_inlinable():
    assert "</script" not in EVIDENCE_RENDER_JS.read_text(encoding="utf-8")


def test_the_shared_primitives_are_not_copied_into_this_file():
    """C4's note: ``h2``/``rowButton`` take ``h`` first precisely so a second
    render file can call them unbound. Two definitions is how they drift, and
    a HALF copy (a fallback that draws almost the same node) is the worst of
    the three, because nothing fails when it diverges."""
    source = EVIDENCE_RENDER_JS.read_text(encoding="utf-8")
    assert "TEConsole" in source, "the shared primitives are reached through the global"
    assert "row-button__cell" not in source, "cell markup belongs to rowButton, not to a copy of it"
    assert 'h("button"' not in source, "no local button primitive -- rowButton is the one implementation"


def test_the_template_loads_console_render_before_evidence_render():
    """A hard dependency needs the tags in the right ORDER, and the browser
    gives no error for the wrong one -- TEEvidence.create would refuse and the
    tab would show a callout instead of a claim."""
    html = (REPO_ROOT / "trialerror" / "dashboard" / "static" / "dashboard.html").read_text(encoding="utf-8")
    console_at = html.index('<script src="console_render.js">')
    evidence_at = html.index('<script src="evidence_render.js">')
    assert console_at < evidence_at
    assert evidence_at < html.index("window.TEEvidence"), "both load before the inline script that reads them"


def test_export_inlines_the_evidence_renderer_in_load_order():
    from trialerror.dashboard import export as export_mod

    assert export_mod._INLINE_SCRIPTS.index("console_render.js") < \
        export_mod._INLINE_SCRIPTS.index("evidence_render.js")


# ---------------------------------------------------------------------------
# the rail
# ---------------------------------------------------------------------------


@requires_node
def test_index_rows_are_real_buttons_and_mark_the_active_claim(panel):
    p, ids = panel
    tree = run_harness("renderIndex", [p, {"onSelectClaim": {"__fn": "select"}}])["tree"]
    rows = by_class(tree, "ev-index-row")
    assert rows, "the rail drew no rows"
    assert all(r["tag"] == "button" for r in rows), "a clickable row must be a real button (A1)"
    assert all("click" in r["listeners"] for r in rows)
    selected = [r for r in rows if "is-selected" in r["classes"]]
    assert len(selected) == 1
    assert selected[0]["attrs"]["data-claim"] == ids["claim"]
    assert selected[0]["attrs"]["aria-current"] == "true"


@requires_node
def test_index_rows_are_disabled_with_a_reason_when_there_is_nothing_to_select_with(panel):
    """A static snapshot has no server to fetch a second claim from. 12.11: a
    control drawn disabled says why, and never gets a handler."""
    p, _ids = panel
    tree = run_harness("renderIndex", [p, {}])["tree"]
    rows = by_class(tree, "ev-index-row")
    assert all(r["attrs"].get("disabled") == "disabled" for r in rows)
    assert all("click" not in r["listeners"] for r in rows)
    assert all("snapshot" in r["attrs"].get("title", "") for r in rows)


@requires_node
def test_the_filter_is_client_side_over_the_page_the_rail_holds(panel):
    p, ids = panel
    tree = run_harness("renderIndex", [p, {"filter": "the traced claim"}])["tree"]
    rows = by_class(tree, "ev-index-row")
    assert [r["attrs"]["data-claim"] for r in rows] == [ids["claim"]]
    assert by_class(tree, "ev-index-count")[0]["text"].startswith("1 OF ")

    miss = run_harness("renderIndex", [p, {"filter": "zzz-no-such-thing"}])["tree"]
    assert not by_class(miss, "ev-index-row")
    assert "no claim in this page of the index matches" in by_class(miss, "empty")[0]["text"]


@requires_node
def test_a_truncated_rail_says_the_filter_only_searches_what_it_holds(panel):
    p, _ids = panel
    p = dict(p, index_truncated=True, index_total=4096)
    tree = run_harness("renderIndex", [p, {}])["tree"]
    line = by_class(tree, "ev-index-truncated")
    assert line and "FILTER SEARCHES ONLY THESE" in line[0]["text"]
    assert "4096" in line[0]["text"]


# ---------------------------------------------------------------------------
# WHAT IT STANDS ON -- the hash chips
# ---------------------------------------------------------------------------


@requires_node
def test_hash_chips_read_the_three_anchor_states(panel):
    p, ids = panel
    tree = run_harness("renderDetail", [p, {}])["tree"]
    chips = texts(by_class(tree, "ev-chip"))
    assert "✓ DOC SHA MATCHES" in chips
    assert "▲ STALE, DOCUMENT RE-INGESTED" in chips, "the re-ingested document's anchor"
    assert "○ QUOTE NOT RE-CHECKABLE" in chips, "the anchor that stored no quote_text"


@requires_node
def test_a_quote_that_no_longer_hashes_gets_its_own_crit_chip(panel):
    """Not one of the artboard's three chips, and it has to exist: silence
    would read as "checked, fine"."""
    p, _ids = panel
    p = json.loads(json.dumps(p))
    p["anchors"][0]["quote_sha_matches"] = False
    tree = run_harness("renderDetail", [p, {}])["tree"]
    crit = [c for c in by_class(tree, "ev-chip") if "ev-chip--crit" in c["classes"]]
    assert "▲ QUOTE HASH MISMATCH" in texts(crit)


@requires_node
def test_a_fenced_anchor_is_marked_and_its_quote_is_still_capped(traced):  # noqa: F811
    rostore, ids = traced
    p = data.build_evidence_panel(rostore, claim_id=ids["claim_fenced"])
    tree = run_harness("renderDetail", [p, {}])["tree"]
    assert "■ FENCED" in texts(by_class(tree, "ev-chip"))
    quote = by_class(tree, "ev-anchor-quote")[0]["text"]
    assert len(quote.split()) <= 20


@requires_node
def test_an_anchor_row_carries_its_full_coordinates(panel):
    p, ids = panel
    tree = run_harness("renderDetail", [p, {}])["tree"]
    coords = texts(by_class(tree, "ev-anchor-coords"))
    primary = [c for c in coords if ids["anchor_primary"] in c]
    assert primary, coords
    assert ids["source_open"] in primary[0]
    assert ids["doc_main"] in primary[0]
    assert "PAGE 1" in primary[0] and "CHARS 0–19" in primary[0]


# ---------------------------------------------------------------------------
# the untrusted wrapper never reaches the page, and never reaches it as HTML
# ---------------------------------------------------------------------------


@requires_node
def test_the_untrusted_wrapper_is_stripped_from_the_claim_body(panel):
    p, _ids = panel
    tree = run_harness("renderDetail", [p, {}])["tree"]
    body = by_class(tree, "ev-claim-text")[0]["text"]
    assert body == "the traced claim, which stands on three anchors"
    assert "untrusted-document-content" not in body
    # and it reached the page as a text node, never as markup -- the shim
    # throws on h(..., {html}), so a renderer that built a string would have
    # failed the harness rather than passed this assertion quietly.
    assert by_class(tree, "ev-claim-text")[0]["children"][0]["tag"] == "#text"


@requires_node
def test_fact_text_is_unwrapped_in_the_edge_table(panel):
    p, ids = panel
    tree = run_harness("renderDetail", [p, {}])["tree"]
    rows = by_class(tree, "ev-edge-row")
    assert len(rows) == 2
    assert all("untrusted-document-content" not in r["text"] for r in rows)
    assert any(ids["entity_a"] in r["text"] for r in rows)


# ---------------------------------------------------------------------------
# WHAT ARGUES WITH IT
# ---------------------------------------------------------------------------


@requires_node
def test_the_verdict_label_is_carried_verbatim(panel):
    p, _ids = panel
    tree = run_harness("renderDetail", [p, {}])["tree"]
    assert texts(by_class(tree, "ev-verdict-label")) == ["CONTRADICTED"]
    assert "contracrow v2" in texts(by_class(tree, "ev-chip"))


@requires_node
def test_an_empty_provenance_graph_reads_as_the_reading_not_a_blank(traced):  # noqa: F811
    rostore, ids = traced
    p = data.build_evidence_panel(rostore, claim_id=ids["claim_same_doc"])
    tree = run_harness("renderDetail", [p, {}])["tree"]
    assert "0 EDGES · THE GENERAL PROVENANCE GRAPH IS EMPTY" in texts(by_class(tree, "empty"))
    assert any("prov_edge has zero writers" in t for t in texts(by_class(tree, "note-strip")))


@requires_node
def test_the_two_unwired_verbs_are_drawn_disabled_with_their_reasons(panel):
    """12.11: no callable exists for either, so neither gets a handler."""
    p, _ids = panel
    tree = run_harness("renderDetail", [p, {}])["tree"]
    actions = {n["text"]: n for n in by_class(tree, "ev-action")}
    assert set(actions) == {"SEND TO DETERMINATIONS", "OPEN A ROOM ON IT"}
    for node in actions.values():
        assert node["attrs"].get("disabled") == "disabled"
        assert "click" not in node["listeners"]
        assert "no callable exists" in node["attrs"].get("title", "")


@requires_node
def test_the_term_conflict_region_is_a_stated_omission_not_an_empty_box(panel):
    """L-C5. Lane e adds the read; until then the page says which read is
    missing rather than drawing a box that reads "no term conflicts"."""
    p, _ids = panel
    tree = run_harness("renderDetail", [p, {}])["tree"]
    notes = texts(by_class(tree, "note-strip"))
    assert any(t.startswith("TERM-SENSE CONFLICTS:") and "conflicts_for_claim" in t for t in notes)
    assert not by_class(tree, "ev-term-conflict")


# ---------------------------------------------------------------------------
# NEIGHBOURHOOD -- the scale ladder
# ---------------------------------------------------------------------------


@requires_node
def test_the_graph_is_drawn_inline_under_the_node_ceiling(panel):
    p, ids = panel
    tree = run_harness("renderDetail", [p, {}])["tree"]
    svgs = by_tag(tree, "svg")
    assert len(svgs) == 1
    circles = by_tag(tree, "circle")
    assert len(circles) == len(p["neighbourhood"]["nodes"])
    assert any("ev-graph-node--claim" in c["classes"] for c in circles)
    assert len([c for c in circles if "is-seed" in c["classes"]]) == 3
    labels = texts(by_tag(tree, "text"))
    assert "Alpha" in labels and ids["claim"] in labels
    # the claim is joined to its seeds by DASHED spokes, because the claim is
    # not itself a node in the relation graph
    assert all(s["attrs"].get("stroke-dasharray") == "3 3" for s in by_class(tree, "ev-graph-spoke"))
    assert len(by_class(tree, "ev-graph-edge")) == 2


@requires_node
def test_past_the_ceiling_the_table_stands_alone(panel):
    p, _ids = panel
    p = json.loads(json.dumps(p))
    p["neighbourhood"]["node_count"] = 431
    tree = run_harness("renderDetail", [p, {}])["tree"]
    assert not by_tag(tree, "svg")
    assert any("431 NODES, DRAWN AS A TABLE ONLY" in t for t in texts(by_class(tree, "note-strip")))
    assert by_class(tree, "ev-edge-row"), "the table is always there"


@requires_node
def test_a_truncated_traversal_says_so_in_the_header(panel):
    p, _ids = panel
    p = json.loads(json.dumps(p))
    p["neighbourhood"]["truncated"] = True
    p["neighbourhood"]["hop_limit"] = 500
    p["neighbourhood"]["seeds_dropped"] = 4
    tree = run_harness("renderDetail", [p, {}])["tree"]
    chips = texts(by_class(tree, "ev-chip"))
    assert "▲ RESULT TRUNCATED AT 500 EDGES" in chips
    assert "▲ 4 SEED ENTITIES NOT EXPANDED" in chips
    assert any("HOPS " in t and "CEILING 500" in t for t in texts(find_all(tree, lambda n: "m" in n.get("classes", []))))


@requires_node
def test_an_evidence_free_claim_says_there_is_nothing_to_draw(traced):  # noqa: F811
    rostore, ids = traced
    p = data.build_evidence_panel(rostore, claim_id=ids["claim_fenced"])
    tree = run_harness("renderDetail", [p, {}])["tree"]
    assert not by_tag(tree, "svg")
    assert "NO LIVE RELATION IS ANCHORED ON THIS CLAIM'S EVIDENCE · NOTHING TO DRAW" in texts(by_class(tree, "empty"))


# ---------------------------------------------------------------------------
# the edges table
# ---------------------------------------------------------------------------


@requires_node
def test_the_edge_table_footer_counts_what_it_did_not_list(panel):
    p, _ids = panel
    p = json.loads(json.dumps(p))
    p["neighbourhood"]["edge_count"] = 250
    p["neighbourhood"]["edges_listed"] = 100
    tree = run_harness("renderDetail", [p, {}])["tree"]
    assert any("150 OF 250 EDGES NOT LISTED" in t for t in texts(by_class(tree, "card-footer")))


@requires_node
def test_the_edge_table_sorts_on_a_column_and_says_which_way(panel):
    p, _ids = panel
    asc = run_harness("renderDetail", [p, {"edgeSort": {"key": "target", "dir": "asc"}}])["tree"]
    desc = run_harness("renderDetail", [p, {"edgeSort": {"key": "target", "dir": "desc"}}])["tree"]
    asc_targets = [r["children"][1]["text"] for r in by_class(asc, "ev-edge-row")]
    desc_targets = [r["children"][1]["text"] for r in by_class(desc, "ev-edge-row")]
    assert asc_targets == sorted(asc_targets)
    assert desc_targets == list(reversed(asc_targets))
    heads = {n["text"].rstrip(" ▲▼"): n for n in by_class(asc, "ev-edge-headcell")}
    assert set(heads) == {"ROLE", "TARGET", "WHY", "SOURCE"}
    marked = [n["text"] for n in by_class(asc, "ev-edge-headcell") if "is-selected" in n["classes"]]
    assert marked == ["TARGET ▲"]


@requires_node
def test_header_cells_are_buttons_only_when_a_sort_handler_was_given(panel):
    p, _ids = panel
    wired = run_harness("renderDetail", [p, {"onSortEdges": {"__fn": "sort"}}])["tree"]
    assert all("click" in n["listeners"] for n in by_class(wired, "ev-edge-headcell"))
    bare = run_harness("renderDetail", [p, {}])["tree"]
    assert all(n["attrs"].get("disabled") == "disabled" for n in by_class(bare, "ev-edge-headcell"))


# ---------------------------------------------------------------------------
# co-anchored claims, and the not_found reading
# ---------------------------------------------------------------------------


@requires_node
def test_co_anchored_rows_select_their_claim_and_say_what_is_shared(panel):
    p, ids = panel
    tree = run_harness("renderDetail", [p, {"onSelectClaim": {"__fn": "select"}}])["tree"]
    rows = {r["attrs"]["data-claim"]: r for r in by_class(tree, "ev-coanchor-row")}
    assert ids["claim_same_anchor"] in rows
    assert rows[ids["claim_same_anchor"]]["attrs"]["data-shared"] == "anchor"
    assert "the same anchor" in rows[ids["claim_same_anchor"]]["text"]
    assert "click" in rows[ids["claim_same_anchor"]]["listeners"]
    assert rows[ids["claim_same_doc"]]["attrs"]["data-shared"] == "document"


@requires_node
def test_a_not_found_selector_renders_the_reading_and_no_detail(traced):  # noqa: F811
    rostore, _ids = traced
    p = data.build_evidence_panel(rostore, anchor_id="ANC-nope")
    tree = run_harness("renderDetail", [p, {}])["tree"]
    heads = texts(by_class(tree, "head"))
    assert "▲ NOTHING HERE YET" in heads
    assert any("anchor_id ANC-nope" in t for t in texts(by_class(tree, "body")))
    assert not by_class(tree, "ev-claim-text"), "there is no claim to draw"


@requires_node
def test_a_non_ok_panel_renders_its_status_and_message(traced):  # noqa: F811
    tree = run_harness("renderDetail", [{"status": "not_initialized", "message": "knowledge.db not found"}, {}])["tree"]
    assert "NOT_INITIALIZED" in texts(by_class(tree, "head"))
    assert "knowledge.db not found" in texts(by_class(tree, "body"))


# ---------------------------------------------------------------------------
# render() -- both regions at once
# ---------------------------------------------------------------------------


@requires_node
def test_render_paints_both_targets_and_reports_which(panel):
    p, _ids = panel
    payload = run_harness("render", [{"__targets": ["index", "detail"]}, p, {}])
    assert payload["value"] == ["index", "detail"]
    assert "ev-index" in payload["targets"]["index"]["children"][0]["classes"]
    assert "ev-detail" in payload["targets"]["detail"]["children"][0]["classes"]


@requires_node
def test_render_skips_a_target_that_is_not_there(panel):
    p, _ids = panel
    payload = run_harness("render", [{"__targets": ["detail"]}, p, {}])
    assert payload["value"] == ["detail"]


# ---------------------------------------------------------------------------
# stripUntrusted, as a unit
# ---------------------------------------------------------------------------


@requires_node
@pytest.mark.parametrize(
    "given,expected",
    [
        ("<untrusted-document-content>\nbody\n</untrusted-document-content>", "body"),
        ("plain text", "plain text"),
        ("", ""),
    ],
)
def test_strip_untrusted(given, expected):
    payload = run_harness("stripUntrusted", [given])
    assert payload["value"] == expected


@requires_node
def test_strip_untrusted_takes_the_last_close_so_a_forged_one_cannot_truncate():
    """``untrusted_wrap`` neutralises a delimiter that appears INSIDE the body,
    so a real payload cannot contain an unbroken one. This asserts the client
    half of that promise anyway: the outermost pair is the boundary."""
    forged = "<untrusted-document-content>\na</untrusted-document-content>b\n</untrusted-document-content>"
    payload = run_harness("stripUntrusted", [forged])
    assert payload["value"] == "a</untrusted-document-content>b"
