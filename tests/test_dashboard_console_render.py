"""``static/console_render.js`` -- the Console's renderers, tested as code.

The dashboard's renderers used to be unreachable by any test: 2,500 lines of
inline script inside ``dashboard.html``, asserted on only as pre-JS HTML
strings. Sweep section 3.11's answer is a DOM-level harness with zero npm
dependencies -- ``tests/_dom_shim.js`` (a ~200-line tree that implements the
handful of methods the page's own element helper calls) and
``tests/_console_render_harness.js`` (loads the SHIPPED render file into a vm
context with the shim standing in for the browser, calls one function, prints
the resulting tree as JSON). This module drives that harness.

Node is not a project dependency, so every test here skips with a stated
reason when it is absent -- the repo's enumerated-skip discipline: a skip that
does not say why is indistinguishable from a test that never ran.

What is covered here is C4's half of the contract: the shim and harness
themselves, and the shell's two shared primitives (``h2``, ``rowButton``) plus
the card registry. The card renderers (sweep tests 1-23) arrive with the cards.
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
CONSOLE_RENDER_JS = REPO_ROOT / "trialerror" / "dashboard" / "static" / "console_render.js"

NODE = shutil.which("node")
requires_node = pytest.mark.skipif(
    NODE is None, reason="node not on PATH -- Console DOM tests need Node >= 18"
)


def run_harness(fn: str, args: list | None = None, *, select: str | None = None, expect_ok: bool = True) -> dict:
    """Call one renderer function under Node and return the harness's JSON.

    Arguments go through a file rather than the command line: fixtures are
    nested JSON, and Windows' command-line quoting mangles them.
    """
    cmd = [NODE, str(HARNESS), "--fn", fn]
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


def by_class(tree: dict, cls: str) -> list[dict]:
    return find_all(tree, lambda n: cls in n.get("classes", []))


# ---------------------------------------------------------------------------
# the file itself -- assertions that need no Node at all
# ---------------------------------------------------------------------------


def test_console_render_js_never_touches_document():
    """The one rule that makes this file testable: nodes come from the
    injected helper, never from a global ``document``. A single
    ``document.createElement`` here would work in the browser and fail every
    test in this module for a reason that reads as a harness bug."""
    source = CONSOLE_RENDER_JS.read_text(encoding="utf-8")
    body = "\n".join(
        line for line in source.splitlines()
        if not line.lstrip().startswith(("*", "/*", "//"))
    )
    assert "document." not in body, "console_render.js must build nodes through the injected `h`, not `document`"


def test_console_render_js_is_inlinable():
    """``</script`` anywhere in the file would end the element early when
    export.py inlines it into the static snapshot."""
    assert "</script" not in CONSOLE_RENDER_JS.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# the shim and harness themselves
# ---------------------------------------------------------------------------


@requires_node
def test_harness_loads_the_shipped_file_and_reports_unknown_functions():
    payload = run_harness("noSuchFunction", [], expect_ok=False)
    assert payload["kind"] == "error"
    assert "noSuchFunction" in payload["error"]


@requires_node
def test_harness_reports_a_renderer_that_reaches_for_innerhtml():
    """The shim refuses ``h(tag, {html})`` on purpose: a renderer that builds
    markup strings can inject a fenced quote into the page as HTML. Proving
    the refusal here means a later renderer cannot acquire the habit quietly."""
    payload = run_harness("h2", ["x", {"attrs": {"html": "<b>no</b>"}}], expect_ok=False)
    assert payload["kind"] == "error"
    assert "html" in payload["error"]


# ---------------------------------------------------------------------------
# shared primitives (spec section 3 item (i): introduced here, reused by
# Evidence, Feed and Determinations)
# ---------------------------------------------------------------------------


@requires_node
def test_h2_is_a_real_heading_carrying_the_title_class():
    tree = run_harness("h2", ["SESSION"])["tree"]
    assert tree["tag"] == "h2"
    assert tree["classes"] == ["title"]
    assert tree["text"] == "SESSION"


@requires_node
def test_h2_keeps_extra_classes_and_attributes():
    tree = run_harness("h2", ["POOLS", {"className": "card-title", "attrs": {"id": "x"}}])["tree"]
    assert tree["classes"] == ["title", "card-title"]
    assert tree["attrs"]["id"] == "x"


@requires_node
def test_row_button_is_a_button_with_one_cell_per_column():
    tree = run_harness("rowButton", [["contradicts", "C-0004", "same anchor"], {}])["tree"]
    assert tree["tag"] == "button"
    assert tree["attrs"]["type"] == "button"
    assert "row-button" in tree["classes"]
    cells = by_class(tree, "row-button__cell")
    assert [c["text"] for c in cells] == ["contradicts", "C-0004", "same anchor"]


@requires_node
def test_row_button_wires_a_click_handler_when_enabled():
    tree = run_harness("rowButton", ["OPEN", {"onClick": {"__fn": "open"}}])["tree"]
    assert tree["listeners"] == ["click"]
    assert "disabled" not in tree["attrs"]


@requires_node
def test_row_button_survives_a_missing_handler():
    """A row with nothing to do yet is still a row, not a crash."""
    tree = run_harness("rowButton", ["OPEN", {}])["tree"]
    assert tree["listeners"] == []
    assert "disabled" not in tree["attrs"]


@requires_node
def test_disabled_row_button_states_its_reason_and_takes_no_handler():
    """DASHBOARD_V2_API section 12.11: a control drawn disabled must say why.
    The reason IS the disabled flag, so it cannot be omitted by accident."""
    tree = run_harness("rowButton", ["SEND TO DETERMINATIONS", {"disabled": "no callable exists yet"}])["tree"]
    assert tree["attrs"]["disabled"] == "disabled"
    assert tree["attrs"]["aria-disabled"] == "true"
    assert tree["attrs"]["title"] == "no callable exists yet"
    assert tree["listeners"] == []


@requires_node
def test_selected_row_button_says_so_to_a_screen_reader_too():
    tree = run_harness("rowButton", ["EV-1", {"selected": True}])["tree"]
    assert "is-selected" in tree["classes"]
    assert tree["attrs"]["aria-current"] == "true"


# ---------------------------------------------------------------------------
# the card registry: the shell renders nothing until a card is registered
# ---------------------------------------------------------------------------


@requires_node
def test_render_into_paints_nothing_while_no_card_is_registered():
    """C4 ships the shell. Loading it must not change one pixel of the page:
    every container it is handed comes back untouched, and the painted list is
    empty, which is what tells dashboard.html to keep the generic renderer."""
    payload = run_harness("renderInto", [{"__targets": ["session", "jobs", "ledger"]}, {"panels": {}}])
    assert payload["value"] == []
    for name, tree in payload["targets"].items():
        assert tree["children"] == [], f"{name} was written to by the empty shell"


@requires_node
def test_card_order_is_the_order_the_canvas_lays_the_grid_out():
    """The registry refuses a name that is not in CARD_ORDER, so the list is
    load-bearing: a typo in a later build's ``registerCard`` fails loudly
    instead of registering a card nothing ever paints."""
    script = (
        "process.stdout.write(JSON.stringify(require(%s).CARD_ORDER))"
        % json.dumps(CONSOLE_RENDER_JS.as_posix())
    )
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [
        "session", "pools", "ledger", "timeline", "jobs", "diagnostics", "gates", "corpus"
    ]


@requires_node
def test_register_card_refuses_a_name_outside_card_order():
    payload = run_harness("registerCard", ["poolz", {"__fn": "render"}], expect_ok=False)
    assert payload["kind"] == "error"
    assert "poolz" in payload["error"]


# ---------------------------------------------------------------------------
# the seam between the two files
# ---------------------------------------------------------------------------

DASHBOARD_HTML = REPO_ROOT / "trialerror" / "dashboard" / "static" / "dashboard.html"


def test_the_render_file_loads_before_the_script_that_uses_it():
    """``window.TEConsole`` is read at start-up by the inline script. A tag
    placed after it (or given ``defer``) leaves TECONSOLE null on every load,
    which looks exactly like "the Console renderer is not finished yet"."""
    html = DASHBOARD_HTML.read_text(encoding="utf-8")
    tag = '<script src="console_render.js"></script>'
    assert tag in html
    assert html.index(tag) < html.index("<script>\n(function ()"), \
        "console_render.js must be parsed before the inline script, not after it"
    assert html.index(tag) < html.index("window.TEConsole.create")


def test_every_console_card_hook_is_real_markup_and_every_container_is_claimed():
    """Both directions of the card wiring, in one place: a hook the script
    reads must exist in the markup (or the card silently never paints), and a
    container in the markup must be claimed by the script (or it sits at
    "loading..." forever). Both failures are invisible in a browser."""
    html = DASHBOARD_HTML.read_text(encoding="utf-8")
    in_markup = set(re.findall(r'data-role="(console-body-[a-z]+)"', html))
    # the hooks consoleTargets() maps card names onto
    targets_block = html[html.index("function consoleTargets()"):]
    targets_block = targets_block[: targets_block.index("}\n")]
    in_script = set(re.findall(r'data-role="(console-body-[a-z]+)"', targets_block))
    assert in_script == in_markup, {"only in script": in_script - in_markup, "only in markup": in_markup - in_script}
    assert len(in_markup) == 8, sorted(in_markup)
