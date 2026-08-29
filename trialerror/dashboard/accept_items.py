"""This lane's own ``GPU_LIVE_CC_ITEMS``-shaped enumeration
(``trialerror.accept.journeys.GPU_LIVE_CC_ITEMS`` -- read as this build's
reference pattern) for the one dashboard acceptance item that is genuinely
orchestrator/integration territory: a REAL browser exercising the served
page's DOM (tabs actually switch, SSE status chip actually updates on a
live store write, panel tables actually render real rows).

NOT added to ``trialerror.accept.journeys.GPU_LIVE_CC_ITEMS`` itself: this
agent's pathspec-limited write scope is ``trialerror/dashboard/``,
``trialerror/cli/dashboard.py``, static assets under ``trialerror/dashboard/
static/``, and its own tests -- ``trialerror/accept/journeys.py`` belongs to a
different lane. Per this build's own instructions ("add a dashboard entry
to that dict if it's importable, else your own module-level equivalent"),
this module is that "own module-level equivalent": same shape (a plain
``dict[str, str]`` of item-key -> the exact orchestrator step that
discharges it), same consumption pattern (a skip-marked pytest per item,
plus a structural test asserting the 1:1 correspondence) -- see
``tests/test_dashboard_accept_items.py``.

A future session that DOES own ``trialerror/accept/journeys.py`` may fold this
dict's one entry into ``GPU_LIVE_CC_ITEMS`` verbatim; until then this is
the single source of truth for the dashboard's own live-DOM gap.
"""

from __future__ import annotations

__all__ = ["DASHBOARD_LIVE_ITEMS"]

DASHBOARD_LIVE_ITEMS: dict[str, str] = {
    "live_dom_dashboard_serve_real_browser": (
        "LIVE real-browser DOM check of `trialerror dashboard serve` (design Section 11 v1 LIVE "
        "DASHBOARD): start `trialerror dashboard serve --program-root <a real or fixture-populated "
        "program>` and, in an actual browser (not a headless assertion against the JSON panel "
        "endpoints), confirm: (1) every tab (session/budget/jobs/gates/corpus/doctor) actually "
        "switches and renders its panel's data as a real table/list, not just that the "
        "underlying `/dashboard/api/<panel>` endpoint returns 200 with the right JSON shape; "
        "(2) the live-status chip shows 'live', then flips to 'reconnecting' if the server is "
        "killed, matching mechspace's own live/rebuilding/reconnecting/static-fallback chip "
        "states (research/tools/research_dashboard/mechspace/js/mechspace_live.js in the "
        "sibling origin-project repo, read as this build's reference); (3) writing "
        "to the watched program's stores (e.g. `trialerror jobs list` after `trialerror jobs "
        "start-worker`, or any CLI write) while the page is open causes the SSE `changed` event "
        "to arrive and the affected panel(s) to re-render with the new data, without a manual "
        "page reload; (4) `trialerror dashboard export` produces a snapshot .html that opens correctly "
        "over `file://` with the embedded static-data badge showing (no fetch/SSE attempted, no "
        "console errors from a doomed network call). Offline proxy already covered: "
        "tests/test_dashboard_serve.py (real subprocess: GET /, GET each panel JSON endpoint, "
        "SSE handshake receiving a `hello` event, clean shutdown) + "
        "tests/test_dashboard_export.py (snapshot file is well-formed HTML containing the "
        "embedded panel JSON) + tests/test_dashboard_data.py (every panel builder exercised "
        "against a fixture store with one of everything, per-field assertions). None of those "
        "can exercise an actual DOM/JS execution environment or a real SSE-driven re-render, "
        "which is exactly what this item names."
    ),
}
