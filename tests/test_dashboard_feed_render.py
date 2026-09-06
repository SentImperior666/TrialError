"""``static/feed_render.js`` -- the threaded post stream, tested as code.

Lane C item B (spec §2), ruling L-C4. The renderer lives in its own file and
builds every node through an injected element helper, so the SHIPPED file runs
under Node against ``tests/_dom_shim.js`` with no browser and no npm
dependency -- see ``tests/test_dashboard_console_render.py`` for the harness
this module reuses.

What is pinned here is the part of threading that is the client's alone: the
two reading orders, the indent, the collapse, the parent link, the reply
control's disabled state, and -- the one that matters most -- that lane b's
post body is passed through untouched at every depth. The SERVER's half
(``depth`` / ``root_post_id`` / ``order_threaded`` / ``reply_to_missing``) is
pinned in ``tests/test_dashboard_data_v2.py``.

Node is not a project dependency, so every test here skips with a stated
reason when it is absent.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "tests" / "_console_render_harness.js"
FEED_RENDER_JS = REPO_ROOT / "trialerror" / "dashboard" / "static" / "feed_render.js"

NODE = shutil.which("node")
requires_node = pytest.mark.skipif(
    NODE is None, reason="node not on PATH -- Feed DOM tests need Node >= 18"
)


def run_harness(fn: str, args: list | None = None, *, select: str | None = None, expect_ok: bool = True) -> dict:
    cmd = [NODE, str(HARNESS), "--module", "feed_render.js", "--fn", fn]
    with tempfile.TemporaryDirectory() as tmp:
        if args is not None:
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


def by_role(tree: dict, role: str) -> list[dict]:
    return find_all(tree, lambda n: n.get("attrs", {}).get("data-role") == role)


def cards(tree: dict) -> list[dict]:
    return by_role(tree, "feed-post-card")


# ---------------------------------------------------------------------------
# fixtures: one panel payload shaped exactly like build_feed_panel's
# ---------------------------------------------------------------------------

def _post(pid, *, author, ts, body, depth=0, root=None, parent=None, missing=False, replies=0):
    return {
        "post_id": pid,
        "thread_id": "THR-1",
        "author": author,
        "kind": author.split(":")[0],
        "ts": ts,
        "body": body,
        "in_reply_to": parent,
        "reply_to": parent,
        "reply_to_missing": missing,
        "depth": depth,
        "root_post_id": root or pid,
        "reply_count": replies,
        "translation": None,
        "translation_state": "absent",
    }


def tree_panel() -> dict:
    """The same shape ``_reply_tree`` seeds server-side:

        P1                arrival:  P1 P2 P3 P5 P4
          P2              threaded: P1 P2 P3 P4 P5
            P3
          P4
        P5
    """
    return {
        "status": "ok",
        "active_thread_id": "THR-1",
        "posts": [
            _post("P1", author="lens:L1", ts="2026-09-06T01:01:00.000Z", body="one", replies=2),
            _post("P2", author="critic:L2", ts="2026-09-06T01:02:00.000Z", body="two", depth=1, root="P1", parent="P1", replies=1),
            _post("P3", author="lens:L3", ts="2026-09-06T01:03:00.000Z", body="three", depth=2, root="P1", parent="P2"),
            _post("P5", author="orchestrator:S1", ts="2026-09-06T01:04:00.000Z", body="five"),
            _post("P4", author="lens:L4", ts="2026-09-06T01:05:00.000Z", body="four", depth=1, root="P1", parent="P1"),
        ],
        "order_threaded": ["P1", "P2", "P3", "P4", "P5"],
        "threads": [],
        "unread_directives": [],
        "translator_table_available": True,
        "translation_withheld_count": 0,
    }


def deep_panel() -> dict:
    """A chain six deep, for the indent cap and the stacking depth."""
    posts = [_post("D0", author="lens:L", ts="2026-09-06T01:00:00.000Z", body="d0", replies=1)]
    for i in range(1, 7):
        posts.append(_post(
            f"D{i}", author="lens:L", ts=f"2026-09-06T01:0{i}:00.000Z", body=f"d{i}",
            depth=i, root="D0", parent=f"D{i - 1}", replies=1 if i < 6 else 0,
        ))
    return {
        "status": "ok",
        "posts": posts,
        "order_threaded": [p["post_id"] for p in posts],
        "translator_table_available": True,
    }


#: The three things the PAGE owns and this file only calls: scroll-and-flash
#: the parent, flip a root's collapse, point the composer at a post. The
#: harness turns each marker into a recording stub, so "this row is clickable"
#: is assertable without firing anything.
CALLBACKS = {
    "onReply": {"__fn": "reply"},
    "onJumpToParent": {"__fn": "jump"},
    "onToggleCollapse": {"__fn": "collapse"},
}

LIVE_CTX = dict(CALLBACKS, order="threaded", writesEnabled=True)


# ---------------------------------------------------------------------------
# the file itself -- assertions that need no Node at all
# ---------------------------------------------------------------------------


def test_feed_render_js_never_touches_the_global_document():
    """The rule that makes the file testable at all: nodes come from the
    injected helper. One global element call here works in the browser and
    fails every test below for a reason that reads as a harness bug."""
    source = FEED_RENDER_JS.read_text(encoding="utf-8")
    body = "\n".join(
        line for line in source.splitlines()
        if not line.lstrip().startswith(("*", "/*", "//"))
    )
    assert "document." not in body, "feed_render.js must build nodes through the injected `h`"


def test_feed_render_js_is_inlinable():
    """A literal script end-tag would close the element early once export.py
    inlines this file into the portable snapshot."""
    assert "</script" not in FEED_RENDER_JS.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# reading order (L-C4: THREADED is the default)
# ---------------------------------------------------------------------------


@requires_node
def test_threaded_is_the_default_order_and_an_unknown_order_falls_back_to_it():
    """L-C4 chose THREADED because the operator's complaint was the missing
    structure. A page that forgets to pass an order, or a localStorage value
    written by an older build, must land on it rather than on nothing."""
    assert run_harness("normalizeOrder", [None])["value"] == "threaded"
    assert run_harness("normalizeOrder", ["grouped"])["value"] == "threaded"
    assert run_harness("normalizeOrder", ["arrived"])["value"] == "arrived"


@requires_node
def test_threaded_order_is_the_servers_dfs_pre_order():
    got = run_harness("orderedPosts", [tree_panel(), "threaded"])["value"]
    assert [p["post_id"] for p in got] == ["P1", "P2", "P3", "P4", "P5"]


@requires_node
def test_as_arrived_order_is_the_append_only_truth():
    """The other half of the toggle: `posts` verbatim, no reshuffle -- P5
    (posted before P4) comes back before it, which is exactly what the
    threaded view moves."""
    got = run_harness("orderedPosts", [tree_panel(), "arrived"])["value"]
    assert [p["post_id"] for p in got] == ["P1", "P2", "P3", "P5", "P4"]


@requires_node
def test_a_bundle_with_no_order_threaded_still_renders_every_post():
    """A static snapshot exported before this build has posts and no
    `order_threaded`. Threading degrades to arrival order; nothing is lost."""
    panel = tree_panel()
    del panel["order_threaded"]
    got = run_harness("orderedPosts", [panel, "threaded"])["value"]
    assert [p["post_id"] for p in got] == ["P1", "P2", "P3", "P5", "P4"]


@requires_node
def test_a_post_the_sequence_forgot_is_appended_rather_than_dropped():
    """Server/client skew is the realistic cause; dropping a post is the one
    outcome this surface may never have."""
    panel = tree_panel()
    panel["order_threaded"] = ["P1", "P2"]
    got = run_harness("orderedPosts", [panel, "threaded"])["value"]
    assert [p["post_id"] for p in got] == ["P1", "P2", "P3", "P5", "P4"]


# ---------------------------------------------------------------------------
# indent + collapse (spec §2.2)
# ---------------------------------------------------------------------------


@requires_node
def test_feed_threaded_indent_and_collapse():
    """Sweep-named test: replies are indented by depth, and the root's
    control collapses the whole subtree under it."""
    tree = run_harness("renderStream", [tree_panel(), LIVE_CTX])["tree"]
    assert tree["attrs"]["data-order"] == "threaded"
    rows = [(c["attrs"]["data-post-id"], c["classes"], c["attrs"]["data-depth"]) for c in cards(tree)]
    assert rows == [
        ("P1", ["post-card"], "0"),
        ("P2", ["post-card", "depth-1"], "1"),
        ("P3", ["post-card", "depth-2"], "2"),
        ("P4", ["post-card", "depth-1"], "1"),
        ("P5", ["post-card"], "0"),
    ]

    # one collapse control, on the only root that has replies, counting the
    # whole subtree it hides (three) -- not P1's two direct children.
    toggles = by_role(tree, "feed-collapse-btn")
    assert len(toggles) == 1
    assert toggles[0]["attrs"]["data-root-post-id"] == "P1"
    assert toggles[0]["text"] == "▾ 3 REPLIES"
    assert toggles[0]["attrs"]["aria-expanded"] == "true"
    assert toggles[0]["listeners"] == ["click"]

    collapsed = dict(LIVE_CTX, collapsed={"P1": True})
    tree2 = run_harness("renderStream", [tree_panel(), collapsed])["tree"]
    assert [c["attrs"]["data-post-id"] for c in cards(tree2)] == ["P1", "P5"]
    toggle2 = by_role(tree2, "feed-collapse-btn")[0]
    assert toggle2["text"] == "▸ 3 REPLIES"
    assert toggle2["attrs"]["aria-expanded"] == "false"
    # a collapsed subtree says so; an empty stream and a hidden one must not
    # look the same.
    assert by_role(tree2, "feed-collapsed-note")[0]["text"] == "3 REPLIES ARE COLLAPSED"


@requires_node
def test_the_indent_stops_at_four_levels_but_the_depth_does_not():
    """Past the cap the card stops moving right (a 20th-level reply would be
    one word wide); `data-depth` still reports the truth."""
    tree = run_harness("renderStream", [deep_panel(), LIVE_CTX])["tree"]
    rows = [(c["attrs"]["data-depth"], [x for x in c["classes"] if x.startswith("depth-")]) for c in cards(tree)]
    assert rows == [
        ("0", []), ("1", ["depth-1"]), ("2", ["depth-2"]), ("3", ["depth-3"]),
        ("4", ["depth-4"]), ("5", ["depth-4"]), ("6", ["depth-4"]),
    ]


@requires_node
def test_as_arrived_drops_the_indent_and_the_collapse_but_keeps_every_post():
    """The AS IT ARRIVED reading is deliberately flat -- indenting a list that
    is not in tree order would be a lie about what it shows."""
    tree = run_harness("renderStream", [tree_panel(), {"order": "arrived", "writesEnabled": True}])["tree"]
    assert tree["attrs"]["data-order"] == "arrived"
    assert [c["attrs"]["data-post-id"] for c in cards(tree)] == ["P1", "P2", "P3", "P5", "P4"]
    assert all(not [x for x in c["classes"] if x.startswith("depth-")] for c in cards(tree))
    assert by_role(tree, "feed-collapse-btn") == []
    # ...and a collapsed root does not silently hide anything in this order.
    tree2 = run_harness("renderStream", [tree_panel(), {"order": "arrived", "collapsed": {"P1": True}}])["tree"]
    assert len(cards(tree2)) == 5


# ---------------------------------------------------------------------------
# the parent link and the orphan flag
# ---------------------------------------------------------------------------


@requires_node
def test_feed_reply_head_links_parent():
    """Sweep-named test: every reply's head carries a control back to the post
    it answers, labelled with that post's own author kind and clock."""
    tree = run_harness("renderStream", [tree_panel(), LIVE_CTX])["tree"]
    links = {
        c["attrs"]["data-post-id"]: by_role(c, "feed-parent-link")
        for c in cards(tree)
    }
    assert links["P1"] == [] and links["P5"] == []      # roots answer nothing
    assert links["P2"][0]["attrs"]["data-parent-post-id"] == "P1"
    assert links["P2"][0]["text"] == "↳ lens · 01:01"   # P1's kind and P1's time
    assert links["P3"][0]["attrs"]["data-parent-post-id"] == "P2"
    assert links["P3"][0]["text"] == "↳ critic · 01:02"
    assert links["P2"][0]["listeners"] == ["click"]     # the page scrolls + flashes


@requires_node
def test_the_parent_link_is_drawn_in_the_as_arrived_order_too():
    """In the flat reading the link is the ONLY thing left saying what a post
    answers, which is the complaint this item exists to close."""
    tree = run_harness("renderStream", [tree_panel(), {"order": "arrived"}])["tree"]
    p2 = next(c for c in cards(tree) if c["attrs"]["data-post-id"] == "P2")
    assert by_role(p2, "feed-parent-link")[0]["attrs"]["data-parent-post-id"] == "P1"


@requires_node
def test_an_orphan_renders_at_root_level_with_the_flag_visible():
    """L-C4, verbatim: replies whose parent is missing "render at root level
    with the flag visible, never dropped"."""
    panel = tree_panel()
    panel["posts"].append(_post(
        "P9", author="lens:L9", ts="2026-09-06T01:09:00.000Z", body="nine",
        parent="POST-elsewhere", missing=True,
    ))
    panel["order_threaded"].append("P9")
    tree = run_harness("renderStream", [panel, LIVE_CTX])["tree"]

    p9 = next(c for c in cards(tree) if c["attrs"]["data-post-id"] == "P9")
    assert p9["attrs"]["data-depth"] == "0"
    assert [x for x in p9["classes"] if x.startswith("depth-")] == []
    flag = by_role(p9, "feed-orphan-flag")[0]
    assert flag["text"] == "↳ replying to a post outside this thread"
    assert "POST-elsewhere" in flag["attrs"]["title"]   # the flag explains, it does not erase
    assert by_role(p9, "feed-parent-link") == [], "an orphan has no parent here to jump to"
    assert p9["text"].strip() != ""


# ---------------------------------------------------------------------------
# lane b's translation column, through threading
# ---------------------------------------------------------------------------


@requires_node
def test_feed_translation_column_survives_threading():
    """Sweep-named test. Lane b's ``buildPostBody`` is called unchanged for
    every card and its node is appended as-is; the only thing threading does
    to a translated post at depth >= 3 is put ``is-stacked`` on the CARD, so
    dashboard.css collapses the two columns into one column instead of two
    twenty-character ones. The body itself is never rebuilt here -- which is
    what this asserts: the injected renderer's own node, with its own class
    and its own text, is what ends up in the tree."""
    ctx = dict(LIVE_CTX, buildPostBody={
        "__fn": "buildPostBody",
        "__node": {"tag": "div", "class": "post-body-split", "text": "PLAIN ENGLISH BODY"},
    })
    tree = run_harness("renderStream", [deep_panel(), ctx])["tree"]

    for card in cards(tree):
        split = [n for n in find_all(card, lambda n: "post-body-split" in n.get("classes", []))]
        assert len(split) == 1, card["attrs"]["data-post-id"]
        assert split[0]["text"] == "PLAIN ENGLISH BODY"

    stacked = {c["attrs"]["data-post-id"]: "is-stacked" in c["classes"] for c in cards(tree)}
    assert stacked == {
        "D0": False, "D1": False, "D2": False,
        "D3": True, "D4": True, "D5": True, "D6": True,
    }


@requires_node
def test_a_card_whose_body_renderer_is_missing_still_shows_the_post():
    """Belt and braces: if the page's own body helper is not injected (an
    inline script that changed shape, or this file loaded on its own), the
    card falls back to the plain text rather than rendering an empty box."""
    tree = run_harness("renderStream", [tree_panel(), LIVE_CTX])["tree"]
    p1 = next(c for c in cards(tree) if c["attrs"]["data-post-id"] == "P1")
    assert "one" in p1["text"]


# ---------------------------------------------------------------------------
# the write control (spec §2.2 + DASHBOARD_V2_API §12.11)
# ---------------------------------------------------------------------------


@requires_node
def test_every_post_offers_reply_in_thread_when_writes_are_live():
    tree = run_harness("renderStream", [tree_panel(), LIVE_CTX])["tree"]
    buttons = by_role(tree, "feed-reply-btn")
    assert len(buttons) == 5
    assert {b["attrs"]["data-post-id"] for b in buttons} == {"P1", "P2", "P3", "P4", "P5"}
    for b in buttons:
        assert b["text"] == "REPLY IN THREAD ▸"
        assert "disabled" not in b["attrs"]
        assert b["listeners"] == ["click"]


@requires_node
def test_reply_in_thread_is_disabled_with_its_reason_in_a_read_only_page():
    """The static export never carries a write token, so `writesEnabled` is
    false there and every reply control is drawn disabled BY CONSTRUCTION --
    not by a convention the export step has to remember. A disabled control
    must say why (DASHBOARD_V2_API §12.11)."""
    ctx = dict(CALLBACKS, order="threaded", writesEnabled=False)   # a handler IS available
    tree = run_harness("renderStream", [tree_panel(), ctx])["tree"]
    buttons = by_role(tree, "feed-reply-btn")
    assert len(buttons) == 5
    for b in buttons:
        assert b["attrs"]["disabled"] == "disabled"
        assert b["attrs"]["aria-disabled"] == "true"
        assert "read-only" in b["attrs"]["title"]
        assert b["listeners"] == [], "a disabled control gets no handler, not even a no-op"


@requires_node
def test_the_post_being_answered_is_marked_on_the_card():
    tree = run_harness("renderStream", [tree_panel(), dict(LIVE_CTX, replyToId="P3")])["tree"]
    marked = [c["attrs"]["data-post-id"] for c in cards(tree) if "is-reply-target" in c["classes"]]
    assert marked == ["P3"]


# ---------------------------------------------------------------------------
# empty and degenerate panels
# ---------------------------------------------------------------------------


@requires_node
def test_an_empty_thread_says_so_rather_than_rendering_nothing():
    tree = run_harness("renderStream", [{"status": "ok", "posts": [], "order_threaded": []}, LIVE_CTX])["tree"]
    assert cards(tree) == []
    assert tree["text"] == "no posts in this thread yet"


@requires_node
def test_the_stream_survives_a_panel_with_no_threading_fields_at_all():
    """A pre-C8 snapshot: no depth, no root_post_id, no order_threaded. Every
    post still renders, flat, with nothing claiming a structure it lacks."""
    panel = {
        "status": "ok",
        "posts": [
            {"post_id": "X1", "author": "lens:L", "ts": "2026-09-06T02:00:00.000Z", "body": "x1", "in_reply_to": None},
            {"post_id": "X2", "author": "lens:L", "ts": "2026-09-06T02:01:00.000Z", "body": "x2", "in_reply_to": "X1"},
        ],
    }
    tree = run_harness("renderStream", [panel, LIVE_CTX])["tree"]
    assert [c["attrs"]["data-post-id"] for c in cards(tree)] == ["X1", "X2"]
    # `in_reply_to` alone is enough for the head link -- the raw column is read
    # when the derived `reply_to` is absent.
    x2 = next(c for c in cards(tree) if c["attrs"]["data-post-id"] == "X2")
    assert by_role(x2, "feed-parent-link")[0]["attrs"]["data-parent-post-id"] == "X1"
    assert by_role(tree, "feed-collapse-btn") == []


@requires_node
def test_an_unknown_author_kind_is_badged_neutrally_not_dropped():
    """DASHBOARD_V2_API §2: `kind` is real data, not a closed enum."""
    panel = {"status": "ok", "posts": [_post("Z1", author="weathervane:L", ts="2026-09-06T02:00:00.000Z", body="z")]}
    tree = run_harness("renderStream", [panel, LIVE_CTX])["tree"]
    assert "WEATHERVANE" in cards(tree)[0]["text"]


# ---------------------------------------------------------------------------
# the seam between the two files -- no Node needed, and no browser could
# catch either of these without someone happening to open the Feed tab
# ---------------------------------------------------------------------------

DASHBOARD_HTML = REPO_ROOT / "trialerror" / "dashboard" / "static" / "dashboard.html"

#: `qs('[data-role="feed-x"]')` in the inline script, exactly -- the compound
#: selectors that address nodes feed_render.js CREATES (`feed-post-card` plus a
#: post id) do not match this shape and are not markup to begin with.
_QS_FEED_ROLE = re.compile(r"""qs\('\[data-role="(feed-[a-z0-9-]+)"\]'\)""")


def _markup_and_script() -> tuple[str, str]:
    html = DASHBOARD_HTML.read_text(encoding="utf-8")
    cut = html.index("<script>\n(function ()")
    return html[:cut], html[cut:]


def test_the_feed_render_file_loads_before_the_script_that_uses_it():
    """``window.TEFeed`` is read at start-up. A tag placed after the inline
    script (or given ``defer``) leaves TEFEED null on every load, which looks
    exactly like "threading was never built" -- the page falls back to a flat
    list and says nothing."""
    html = DASHBOARD_HTML.read_text(encoding="utf-8")
    tag = '<script src="feed_render.js"></script>'
    assert tag in html
    assert html.index(tag) < html.index("<script>\n(function ()")
    assert html.index(tag) < html.index("window.TEFeed.create")


def test_every_feed_hook_the_script_reads_is_real_markup():
    """The failure this catches is total: the order chips are wired at module
    scope, so one missing ``data-role`` throws before the page finishes
    booting and NOTHING renders -- not just the Feed. A browser only shows it
    to whoever opens the dashboard next."""
    markup, script = _markup_and_script()
    wanted = set(_QS_FEED_ROLE.findall(script))
    assert wanted, "the regex stopped matching the script's own idiom"
    missing = sorted(r for r in wanted if f'data-role="{r}"' not in markup)
    assert missing == [], f"the inline script reads hooks that no markup declares: {missing}"


def test_the_reading_order_chips_exist_and_threaded_is_the_one_lit_by_default():
    """L-C4's default, in the shipped markup rather than only in the script:
    a viewer with no stored preference (and a page whose script has not run
    yet) sees THREADED as the live chip."""
    markup, _script = _markup_and_script()
    threaded = re.search(r'<span[^>]*data-role="feed-order-threaded"[^>]*>', markup)
    arrived = re.search(r'<span[^>]*data-role="feed-order-arrived"[^>]*>', markup)
    assert threaded is not None and arrived is not None
    assert "chip--live" in threaded.group(0)
    assert "chip--neutral" in arrived.group(0)
    # GROUPED BY GOAL is still the disabled third option, with its reason.
    grouped = re.search(r'<span[^>]*aria-disabled="true"[^>]*>GROUPED BY GOAL</span>', markup)
    assert grouped is not None and "title=" in grouped.group(0)


def test_the_composer_sends_the_reply_target_on_the_wire():
    """REPLY IN THREAD's entire effect on the server is this one field. Wiring
    the button and forgetting the body would put every reply at root level
    with no error anywhere."""
    _markup, script = _markup_and_script()
    body_fn = script[script.index('"feed-post",'):]
    body_fn = body_fn[: body_fn.index("[\"thread_id\", \"body\"]")]
    assert "in_reply_to" in body_fn
    assert "state.feedReplyTo" in body_fn


def test_the_reading_order_is_persisted_per_viewer_not_on_the_server():
    """L-C4: localStorage, per viewer. A guarded read/write both ways --
    localStorage throws outright under some privacy settings, and a reading
    preference must never be able to take the page down."""
    _markup, script = _markup_and_script()
    assert "LS_FEED_ORDER" in script
    assert "lsGet(LS_FEED_ORDER)" in script
    assert "lsSet(LS_FEED_ORDER" in script
    for helper in ("function lsGet(", "function lsSet("):
        block = script[script.index(helper):]
        block = block[: block.index("\n  }")]
        assert "try {" in block and "catch" in block, helper
