"""``trialerror dashboard serve`` -- the live serve+watch+SSE layer.

Architecture modeled on the proven serve-and-watch design of an earlier
in-house research dashboard, read as a READ-ONLY reference -- this is
TrialError's own build: stdlib ``http.server.ThreadingHTTPServer``
serving the static page, a watcher thread polling store-file mtimes
(2-5s debounce, same window that earlier design cites), and a
Server-Sent-Events endpoint broadcasting a ``changed`` notification to
every open page when a watched file's mtime moves.

Deliberate departure from that earlier dashboard's shape: there is no
"rebuild" step here. That earlier dashboard watched markdown/jsonl SOURCE
files and had to invoke two expensive subprocess builds (embeddings-capable,
hence its own strict no-embed-on-rebuild contract) to turn them into a
servable data bundle. This dashboard watches the program's SQLite store
files directly and reads them fresh on every panel request -- the "rebuild"
IS the next cheap SQL query (:mod:`trialerror.dashboard.data`), so there is
nothing here that could ever reach an embeddings/LLM API, satisfying the
same lesson trivially rather than by a guard that has to be maintained.

Endpoints:

- ``GET /`` -- serves ``static/dashboard.html`` (SimpleHTTPRequestHandler
  has no built-in index redirect for a non-``index.html``-named file, so
  this is special-cased).
- ``GET /dashboard.html`` / ``GET /dashboard.css`` / ``GET
  /console_render.js`` -- plain static files from :data:`STATIC_DIR`,
  served byte-for-byte by ``SimpleHTTPRequestHandler``. The ``*_render.js``
  files are the per-surface renderers the dashboard keeps out of the inline
  script; the static export inlines them instead (``export.py``'s
  ``_INLINE_SCRIPTS``), which is why nothing here rewrites them.
- ``GET /dashboard/api/all`` -- ``{"meta": {...}, "panels": {<name>: ...}}``
  for every panel in one request (what a freshly-loaded page fetches).
- ``GET /dashboard/api/<panel>`` -- one panel's JSON, unwrapped (``session``
  / ``budget`` / ``jobs`` / ``gates`` / ``corpus`` / ``doctor``).
- ``GET /dashboard/api/ext`` -- the extension-panel listing (manifest info
  only, no panel data) -- see ``trialerror.dashboard.ext``.
- ``GET /dashboard/api/ext/<name>`` -- one extension panel's data, or 404
  for an unknown name. A broken extension panel is never a 500 here -- see
  ``trialerror.dashboard.ext.build_ext_panel``'s ``{"status": "ext_error", ...}``
  contract.
- ``GET /dashboard/events`` -- SSE stream: ``hello`` once on connect,
  ``changed`` whenever the watcher detects a WRITE to a watched file
  (``{changed_paths, changed_stores, detected_ts}``; ``changed_stores`` is
  the coarse ``platform``/``ops``/``knowledge``/``jobs``/``doctor``
  vocabulary a client can filter on), a heartbeat comment every 15s
  otherwise. Reads never produce a ``changed`` -- see
  :func:`_watch_targets` on why ``-shm`` is not watched.

**Stage 3 (build-v2dash-writes): operator write actions.** Every write goes
through ``POST``, guarded by a per-serve-process random token (see
``_WRITE_TOKEN`` below) that must be echoed back on the ``X-TrialError-Dashboard-
Token`` header -- a CSRF-class guard, since the server itself is
loopback-only but a malicious page open in the SAME browser could otherwise
blind-POST to it. The token is embedded into the served ``/``/
``/dashboard.html`` page (a ``<meta name="dashboard-write-token">`` tag,
injected at serve time -- see ``_serve_index``) and is NEVER present in a
``trialerror dashboard export`` snapshot (that code path never runs through this
module at all -- see ``trialerror.dashboard.export``), so every write button on a
static snapshot stays honestly disabled, by construction, not by a
convention that could drift. Full contract: ``docs/DASHBOARD_V2_API.md``
section 12.

- ``POST /dashboard/api/doctor/run`` -- runs ``trialerror doctor``'s full check
  suite on demand (see ``trialerror.dashboard.doctor_run`` for why this is a
  distinct action, never part of the watch loop) and returns the doctor
  panel's fresh JSON. Was a ``GET`` before this build; moved to ``POST``
  (it writes a sidecar state file) and is now token-guarded like every
  other write.
- ``POST /dashboard/api/write/<action>`` -- one operator write action (see
  ``trialerror.dashboard.writes.WRITABLE_ACTIONS`` for the full set: gate edit
  verification, KG merge accept/reject, the acquisition-delivered
  transition, a room turn/score/freeze, and a feed post). JSON request
  body, JSON response body: ``{"ok": true, "result": {...}}`` on success,
  ``{"ok": false, "message": "..."}`` on a clean business refusal (the
  refusing module's own error text, verbatim -- never a generic "failed").

Usage::

    python -m trialerror.cli dashboard serve --program-root <path> [--port 8850]
"""

from __future__ import annotations

import argparse
import hmac
import http.server
import json
import secrets
import socket
import sys
import threading
import time
import traceback
import urllib.parse
from pathlib import Path
from queue import Empty, Queue
from typing import Any

from trialerror.dashboard.data import (
    PANEL_BUILDERS,
    build_all_panels,
    build_doctor_panel,
    isolated_panel,
    run_search,
)
from trialerror.dashboard.doctor_run import doctor_state_path, read_doctor_state, run_doctor_and_persist
from trialerror.dashboard.ext import build_all_ext_panels, build_ext_panel, find_ext_panel_entry, list_ext_panels
from trialerror.dashboard.store_ro import open_store_ro
from trialerror.dashboard.writes import WRITABLE_ACTIONS, dispatch as dispatch_write
from trialerror.stores import paths as store_paths
from trialerror.util.timeutil import now

__all__ = ["ServerConfig", "make_handler_class", "DashboardServer", "main"]

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"

DEFAULT_POLL_INTERVAL_S = 2.0
DEFAULT_DEBOUNCE_S = 3.0
SSE_HEARTBEAT_S = 15.0

#: The CSRF-class guard header every write POST must echo back (module
#: docstring, Stage 3 section). Header names are case-insensitive per
#: ``http.client.HTTPMessage`` (``self.headers.get`` already handles that).
WRITE_TOKEN_HEADER = "X-TrialError-Dashboard-Token"

#: Hard ceiling on a write POST's ``Content-Length`` (M-WA-3). Every field
#: any write action reads is an id, a short note, or a feed post; 1 MiB is
#: orders of magnitude above the largest of those and still small enough
#: that refusing it costs nothing. Over the line the body is NEVER read --
#: the point of the cap is not to allocate it.
MAX_WRITE_BODY_BYTES = 1024 * 1024

#: How long the refusing side waits for the next chunk while draining a body
#: it has already rejected (see ``_discard_oversized_body``). Short: the
#: drain exists so the 413 can be delivered, not so the client can take its
#: time about a request that is already refused.
_DISCARD_TIMEOUT_S = 0.25

#: Marker byte string ``_serve_index`` injects the write-token ``<meta>``
#: tag right after -- present exactly once in ``static/dashboard.html``
#: (a structural test asserts this).
_INDEX_TOKEN_ANCHOR = '<meta charset="utf-8">'


class ServerConfig:
    def __init__(
        self,
        *,
        repo_root: Path,
        program_root: Path | None,
        platform_root: Path | None,
        poll_interval: float = DEFAULT_POLL_INTERVAL_S,
        debounce: float = DEFAULT_DEBOUNCE_S,
    ) -> None:
        self.repo_root = repo_root
        self.program_root = program_root
        self.platform_root = platform_root if platform_root is not None else store_paths.platform_root()
        self.poll_interval = poll_interval
        self.debounce = debounce
        #: Set by :func:`_watch_targets` on every re-derivation (LU-9):
        #: ``"ok"``, or ``"config_error: <type>: <message>"`` when the
        #: program's ``trialerror.toml`` exists but will not parse, in which
        #: case the watcher is polling DEFAULT store paths that a relocated
        #: ``[paths]`` program never writes to. Surfaced on ``build_meta``
        #: so the page can say so instead of looking eternally quiet.
        self.watch_status: str = "ok"


# =============================================================================
# SSE broadcaster (same fan-out shape as that earlier dashboard's Broadcaster)
# =============================================================================
class Broadcaster:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[Queue] = []

    def subscribe(self) -> Queue:
        q: Queue = Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def publish(self, event: str, data: dict) -> None:
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            q.put((event, data))

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


# =============================================================================
# panel/meta assembly (fresh RO connections per call -- see store_ro.py)
# =============================================================================
def _watch_targets(config: ServerConfig) -> list[tuple[Path, str]]:
    """``(path, store name)`` for every file this server watches, the store
    name being what a ``changed`` event reports in ``changed_stores``
    (``platform`` / ``ops`` / ``knowledge`` / ``jobs`` for the four store
    DBs, ``doctor`` for this layer's own sidecar). Re-derived on every poll
    (not cached once at startup) so a program that gets initialized AFTER
    the server starts (a fresh ``trialerror program init`` while the dashboard
    is already open) is picked up automatically.

    Each store contributes the DB file itself and its ``-wal`` sidecar, and
    **deliberately not** its ``-shm`` (LU-7, the self-sustaining refresh
    loop): in WAL mode a READER writes its read-mark into the shared-memory
    index, so every panel GET this server serves bumps ``-shm``'s mtime.
    With ``-shm`` in the watch set the watcher therefore re-fired on the
    dashboard's own reads, every poll+debounce, forever, with no writer
    anywhere -- and cross-triggered every other dashboard on the machine
    through the shared ``platform.db``. The main file and the WAL only carry
    CONTENT a writer put there, so watching those two makes a read-only page
    watch-silent by construction rather than by a filter the client would
    have to maintain. (A cold reader does have to create an EMPTY ``-wal``
    before it can read; :func:`snapshot_mtimes` declines to record that, for
    the reason given there.)

    An unparseable ``trialerror.toml`` is REPORTED here, not raised, and
    never silently swallowed (LU-9). Lane L0-C's design D13 states the same
    fact as "unparseable ``trialerror.toml`` always raises", and its
    complaint is this function's too: falling back to the DEFAULT ``stores/``
    location for a program whose ``[paths].stores_dir`` points elsewhere
    means watching files nobody writes and calling a live program frozen.
    The two differ only in the channel, and the channel matters at the merge
    of lane c into that lane: raising lands in :func:`watcher_loop`'s own
    ``except`` (``traceback.print_exc(); continue``), so the exception form
    is a traceback per poll into a log nobody has open and NOTHING on the
    page -- LU-9's failure exactly. The reported form prints the same
    traceback once per poll AND puts ``config_error: <Type>: <msg>`` on
    ``config.watch_status``, which :func:`build_meta` carries onto the SSE
    ``hello`` so the page can say it out loud. Strictly more visible, and
    the watcher keeps watching the paths it CAN resolve (``platform.db`` is
    not behind the program config).

    (``PRAGMA data_version`` on persistent read-only connections is a
    stronger signal still -- it distinguishes a real commit from a
    same-mtime rewrite -- but it holds four open file handles for the
    server's whole lifetime, which on Windows blocks the store-reset
    runbook. Recorded as the upgrade path, not chosen here.)"""
    bases: list[tuple[Path, str]] = [(store_paths.platform_db_path(root=config.platform_root), "platform")]
    extras: list[tuple[Path, str]] = []
    status = "ok"
    if config.program_root is not None:
        cfg_path = config.program_root / "trialerror.toml"
        cfg = None
        if cfg_path.is_file():
            try:
                from trialerror.util.config import load_config

                cfg = load_config(cfg_path).raw
            except Exception as exc:  # noqa: BLE001 - a bad config must not stop the watcher
                # LU-9 / L0-C D13: swallowing this silently left the watcher
                # polling DEFAULT store paths for a program whose [paths]
                # section relocates them -- the SSE connection stays open,
                # the page stays "live", and `changed` simply never fires
                # again. Log it like watcher_loop's own handler does, and
                # publish it on meta.watch_status so the page can say so.
                traceback.print_exc()
                cfg = None
                status = f"config_error: {type(exc).__name__}: {exc}"
        bases.append((store_paths.ops_db_path(config.program_root, cfg), "ops"))
        bases.append((store_paths.knowledge_db_path(config.program_root, cfg), "knowledge"))
        bases.append((store_paths.jobs_db_path(config.program_root, cfg), "jobs"))
        # M-LU-5: the doctor sidecar is written by this layer (an operator
        # doctor run, or the CLI's) and read by the doctor panel, but was
        # never watched -- a completed run reached other open pages only on
        # the next unrelated store write.
        extras.append((doctor_state_path(config.program_root), "doctor"))
    config.watch_status = status

    out: list[tuple[Path, str]] = []
    for base, kind in bases:
        out.append((base, kind))
        out.append((base.with_name(base.name + "-wal"), kind))
    out.extend(extras)
    return out


def _watched_paths(config: ServerConfig) -> list[Path]:
    """Just the paths of :func:`_watch_targets` (the watch set proper)."""
    return [p for p, _kind in _watch_targets(config)]


def snapshot_mtimes(config: ServerConfig) -> dict[str, tuple[int, int]]:
    """``{path: (size, mtime_ns)}`` for every watched file that exists.

    Keyed on the PAIR, not on ``st_mtime`` alone: a float mtime is
    second-or-worse granular on some filesystems, so two commits inside one
    tick used to be one event; and a same-length rewrite that lands in the
    same tick used to be none at all. Size catches the second case,
    ``st_mtime_ns`` the first.

    An EMPTY ``-wal`` is not recorded at all. Measured behaviour of a
    read-only connection against a WAL database whose ``-wal`` was
    checkpointed away by the last writer's close: SQLite has to CREATE the
    ``-wal`` (0 bytes) and ``-shm`` before it can read, because a WAL reader
    needs the shared-memory index even though it will never write a frame.
    ``-shm`` is out of the watch set already (see :func:`_watch_targets`);
    this is the other half of the same fact -- without it the first panel
    GET after a cold start still published a ``changed`` for all four
    stores, which is the very event this batch exists to stop. A zero-length
    WAL holds no committed frames by definition, so nothing is lost: a real
    commit appends frames (size > 0, detected), and a checkpoint that
    truncates the WAL back to zero has already moved the main DB file, which
    IS watched."""
    result: dict[str, tuple[int, int]] = {}
    for p in _watched_paths(config):
        try:
            if not p.is_file():
                continue
            st = p.stat()
            if st.st_size == 0 and p.name.endswith("-wal"):
                continue
            result[str(p)] = (st.st_size, st.st_mtime_ns)
        except OSError:
            pass
    return result


def diff_changed_paths(prev: dict[str, tuple[int, int]], cur: dict[str, tuple[int, int]]) -> set[str]:
    changed = set(cur) ^ set(prev)
    for k, v in cur.items():
        if k in prev and prev[k] != v:
            changed.add(k)
    return changed


def changed_stores_for(config: ServerConfig, paths: list[str]) -> list[str]:
    """The store names behind a set of changed paths -- the ``changed_stores``
    field of a ``changed`` event. A path the current watch set no longer
    knows (a program re-pointed mid-flight) is simply dropped: the raw
    ``changed_paths`` list still carries it."""
    kinds = {str(p): kind for p, kind in _watch_targets(config)}
    return sorted({kinds[p] for p in paths if p in kinds})


def build_meta(config: ServerConfig) -> dict:
    return {
        "generated_ts": now(),
        "program_root": str(config.program_root) if config.program_root else None,
        "platform_root": str(config.platform_root),
        # LU-9: "ok" or "config_error: ...". A watcher polling the wrong
        # paths is otherwise indistinguishable from a quiet program.
        "watch_status": config.watch_status,
        # the extension-panel "listing" (trialerror.dashboard.ext, C-0070):
        # manifest info for every panel this program declares under
        # trialerror_ext/panels/, present on every meta payload (including the
        # SSE "hello" event) -- always [] when the program has none, never
        # a missing key, so a client can rely on the field existing.
        "ext_panels": list_ext_panels(config.program_root),
    }


def build_all(config: ServerConfig) -> dict:
    rostore = open_store_ro(config.program_root, platform_root=config.platform_root)
    try:
        doctor_state = read_doctor_state(config.program_root)
        panels = build_all_panels(rostore, doctor_state=doctor_state)
        # extension panels (trialerror.dashboard.ext): only add the "ext" key
        # when this program actually declares at least one -- keeps the
        # core panel set's shape byte-for-byte unchanged for every program
        # that doesn't use the extension protocol at all (see
        # tests/test_dashboard_serve.py's exact-set assertion).
        ext_panels = build_all_ext_panels(config.program_root, rostore)
        if ext_panels:
            panels["ext"] = ext_panels
    finally:
        rostore.close()
    return {"meta": build_meta(config), "panels": panels}


#: panel name -> a TUPLE of ``(query-string param, the builder keyword it
#: feeds)`` pairs, for the handful of panels that accept an optional
#: selector (``feed``'s ``thread_id``, ``rooms``'s ``room_id``,
#: ``dossier``'s ``artifact_id``, ``since_you_left``'s ``since``) -- every
#: OTHER panel (including every panel that existed before this build)
#: ignores the query string entirely, same as before. A request with no
#: matching param falls through to the builder's own default (e.g. "most
#: recently active thread"), never an error.
#:
#: A tuple, not a single pair, because a panel can be selected by more than
#: one thing: the Evidence builder takes ``claim_id`` / ``anchor_id`` /
#: ``chunk_id`` and resolves whichever it is given (spec section 1.2). Every
#: pair whose param is present is passed through, so the builder -- not the
#: route -- owns the precedence between them; the route's only rule is that
#: a blank value is the same as an absent one.
PANEL_QUERY_PARAMS: dict[str, tuple[tuple[str, str], ...]] = {
    "feed": (("thread_id", "thread_id"),),
    "rooms": (("room_id", "room_id"),),
    "dossier": (("artifact_id", "artifact_id"),),
    "since_you_left": (("since", "since"),),
}


def build_one_panel(
    config: ServerConfig, name: str, *, query_params: dict[str, list[str]] | None = None
) -> dict | None:
    if name == "doctor":
        return build_doctor_panel(read_doctor_state(config.program_root))
    builder = PANEL_BUILDERS.get(name)
    if builder is None:
        return None
    kwargs: dict[str, Any] = {}
    if query_params:
        for qs_key, kwarg_name in PANEL_QUERY_PARAMS.get(name, ()):
            values = query_params.get(qs_key)
            if values and values[0]:
                kwargs[kwarg_name] = values[0]
    rostore = open_store_ro(config.program_root, platform_root=config.platform_root)
    try:
        # M-LU-2: one raising builder is that PANEL's error, never the
        # route's -- same isolation build_all_panels applies to the bundle,
        # so a single-panel refresh and the /all bundle agree on what a
        # broken builder looks like ({"status": "error", ...}).
        return isolated_panel(name, builder, rostore, **kwargs)
    finally:
        rostore.close()


def build_search(config: ServerConfig, query_params: dict[str, list[str]]) -> dict:
    """``GET /dashboard/api/search`` -- wires ``trialerror.dashboard.data.
    run_search`` (in turn ``trialerror.retrieve.engine.search``) over a fresh
    read-only store. ``q`` may be omitted/blank (degrades to an empty,
    well-formed result set, matching the engine's own contract for a blank
    query); ``k``/``mode`` fall back to ``run_search``'s own defaults on a
    missing or unparseable value rather than erroring. Facet filters
    (``source_ids``/``kind``/``license_tier``/``year``, each a single
    comma-separated query param, matching ``SearchRequest.filters``'
    field names) are optional.

    P-2: the engine call is fenced. ``mode=vector`` with no filters is the
    documented unbounded case (``trialerror.retrieve.engine``'s own B.4b
    note: the ``IN (...)`` candidate list hits SQLite's 32,766-variable
    ceiling on a real corpus), and ``do_GET`` has no try/except of its own
    -- so before this, such a query closed the socket with NO response and
    the page silently kept showing the previous question's results as
    though they answered the new one. A failed search is now a
    well-formed ``{"status": "search_error"}`` reading the client renders
    as a callout, with the traceback on the server's log. This is the same
    per-surface isolation ``isolated_panel`` gives every panel builder
    (M-LU-2); search only ever lacked it because it is not in
    ``PANEL_BUILDERS``."""
    q = (query_params.get("q") or [""])[0]
    k_raw = (query_params.get("k") or [None])[0]
    try:
        k = int(k_raw) if k_raw is not None else None
    except ValueError:
        k = None
    mode = (query_params.get("mode") or ["auto"])[0]

    filters: dict[str, Any] = {}
    if query_params.get("source_ids"):
        filters["source_ids"] = [v for v in query_params["source_ids"][0].split(",") if v]
    if query_params.get("kind"):
        filters["kind"] = [v for v in query_params["kind"][0].split(",") if v]
    if query_params.get("license_tier"):
        filters["license_tier"] = [v for v in query_params["license_tier"][0].split(",") if v]
    if query_params.get("year"):
        filters["year"] = [int(y) for y in query_params["year"][0].split(",") if y.strip().lstrip("-").isdigit()]

    try:
        rostore = open_store_ro(config.program_root, platform_root=config.platform_root)
        try:
            return run_search(rostore, query=q, k=k, mode=mode, filters=filters or None)
        finally:
            rostore.close()
    except Exception as exc:  # noqa: BLE001 - one bad query must not take the route down
        traceback.print_exc()
        return {
            "status": "search_error",
            "message": f"{type(exc).__name__}: {exc}",
            "mode": mode,
            "results": [], "tiers_used": [], "stats": {},
        }


def build_one_ext_panel(config: ServerConfig, name: str) -> dict | None:
    """One extension panel's data by name, or ``None`` (no such extension
    panel -- the HTTP handler turns that into a 404, same as an unknown
    core panel name)."""
    entry = find_ext_panel_entry(config.program_root, name)
    if entry is None:
        return None
    rostore = open_store_ro(config.program_root, platform_root=config.platform_root)
    try:
        return build_ext_panel(entry, rostore, config.program_root)
    finally:
        rostore.close()


# =============================================================================
# HTTP handler
# =============================================================================
def make_handler_class(
    config: ServerConfig, broadcaster: Broadcaster, token: str | None = None
) -> type[http.server.SimpleHTTPRequestHandler]:
    class DashboardHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

        def log_message(self, fmt: str, *args) -> None:
            sys.stderr.write(f"[trialerror.dashboard] {self.address_string()} - {fmt % args}\n")

        def do_GET(self) -> None:  # noqa: N802 - stdlib method name
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path

            if path == "/":
                path = "/dashboard.html"

            if path == "/dashboard.html":
                self._serve_index()
                return
            if path == "/dashboard/events":
                self._handle_sse()
                return
            if path == "/dashboard/api/all":
                self._json_response(build_all(config))
                return
            if path == "/dashboard/api/doctor/run":
                # Stage 3 (build-v2dash-writes): this now WRITES a sidecar
                # state file, so it moved to POST + the token guard, same as
                # every other write action -- see module docstring.
                self.send_response(405)
                self.send_header("Allow", "POST")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            # extension-panel routes (trialerror.dashboard.ext, C-0070) -- must
            # be checked BEFORE the generic "/dashboard/api/" branch below,
            # since "/dashboard/api/ext/<name>" also starts with that
            # prefix and would otherwise be misread as a core panel named
            # "ext/<name>" (always a 404 there).
            if path == "/dashboard/api/ext":
                self._json_response(list_ext_panels(config.program_root))
                return
            if path.startswith("/dashboard/api/ext/"):
                name = path[len("/dashboard/api/ext/") :]
                panel = build_one_ext_panel(config, name)
                if panel is None:
                    self.send_error(404, f"no such extension panel: {name!r}")
                    return
                self._json_response(panel)
                return
            # search: checked BEFORE the generic "/dashboard/api/" branch
            # below for the same reason the ext routes are (path.startswith
            # would otherwise misread "/dashboard/api/search" as a core
            # panel literally named "search", which doesn't exist in
            # PANEL_BUILDERS -- see run_search's own docstring for why
            # search is a dedicated route rather than a PANEL_BUILDERS
            # entry).
            if path == "/dashboard/api/search":
                query_params = urllib.parse.parse_qs(parsed.query)
                self._json_response(build_search(config, query_params))
                return
            # WA-6: a GET on a write route used to fall through to the
            # generic panel branch below and answer "no such panel:
            # 'write/room-freeze'", which is both wrong and misleading.
            # 405 + Allow, the same shape /dashboard/api/doctor/run already
            # uses -- and as JSON, because the caller asked an API route.
            if path.startswith("/dashboard/api/write/"):
                action = path[len("/dashboard/api/write/") :]
                self.send_response(405)
                self.send_header("Allow", "POST")
                body = json.dumps(
                    {"ok": False, "status": "method_not_allowed",
                     "message": f"write action {action!r} is POST-only"},
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if path.startswith("/dashboard/api/"):
                name = path[len("/dashboard/api/") :]
                query_params = urllib.parse.parse_qs(parsed.query)
                panel = build_one_panel(config, name, query_params=query_params)
                if panel is None:
                    self.send_error(404, f"no such panel: {name!r}")
                    return
                self._json_response(panel)
                return

            self.path = path
            super().do_GET()

        def do_POST(self) -> None:  # noqa: N802 - stdlib method name
            """Every branch below answers with JSON, whatever happens.

            W3 (sweep batch): before this, four different failures on a
            write route each answered with something the client's own
            ``postWrite`` could not read as the documented
            ``{"ok", "status", "message"}`` envelope -- an unknown action
            got the stdlib's HTML 404 page (WA-4), a GET fell through to
            "no such panel" (WA-6), an oversized body was read in full
            (M-WA-3), and ANY unexpected exception escaped
            ``dispatch_write`` into ``http.server``'s own handler, which
            closes the socket after logging a traceback: no status line at
            all, which curl reports as HTTP 000 (M-WA-1). All four are now
            JSON with a named ``status``."""
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path

            try:
                if path == "/dashboard/api/doctor/run":
                    if not self._require_token():
                        return
                    state = run_doctor_and_persist(
                        repo_root=config.repo_root,
                        program_root=config.program_root,
                        platform_root=config.platform_root,
                    )
                    self._json_response(build_doctor_panel(state))
                    return
                if path.startswith("/dashboard/api/write/"):
                    # WA-5: the token guard runs FIRST, so an unauthenticated
                    # caller learns nothing about which action names exist.
                    if not self._require_token():
                        return
                    action = path[len("/dashboard/api/write/") :]
                    if action not in WRITABLE_ACTIONS:
                        # WA-4: JSON, not the stdlib's HTML error page --
                        # `writes.dispatch` has had this exact branch since
                        # Stage 3 and it was unreachable through HTTP.
                        self._json_response(
                            {"ok": False, "status": "unknown_action",
                             "message": f"no such write action: {action!r}"},
                            status=404,
                        )
                        return
                    body, err, err_status = self._read_json_body()
                    if err is not None:
                        self._json_response(
                            {"ok": False, "status": "bad_request", "message": err}, status=err_status
                        )
                        return
                    result = dispatch_write(
                        action,
                        program_root=config.program_root,
                        platform_root=config.platform_root,
                        body=body,
                    )
                    self._json_response(result)
                    return

                self._json_response(
                    {"ok": False, "status": "unknown_route", "message": f"no such POST route: {path!r}"},
                    status=404,
                )
            except Exception as exc:  # noqa: BLE001 - the last line before the socket
                # `writes.dispatch` deliberately lets a genuine bug escape
                # (its own docstring: "a genuine bug must look like one,
                # never a disguised refusal"). This is where that promise
                # is kept: the traceback goes to stderr where the operator
                # running `dashboard serve` can see it, and the browser
                # gets a 500 with a NAMED status it can render, instead of
                # a closed socket.
                traceback.print_exc()
                self._json_response(
                    {"ok": False, "status": "internal_error",
                     "message": f"{type(exc).__name__}: {exc} (the server's log has the traceback)"},
                    status=500,
                )

        def _require_token(self) -> bool:
            """The CSRF-class guard (module docstring, Stage 3 section):
            every write POST must echo the per-serve-process token back on
            :data:`WRITE_TOKEN_HEADER`. ``token is None`` (this handler
            class was built without one -- should never happen via ``main``,
            but a defensive default for any other caller of
            :func:`make_handler_class`) refuses every write outright rather
            than silently accepting an unguarded one.

            M-WA-4: the comparison is :func:`hmac.compare_digest`, not
            ``==``. Nothing here is high-value (the token is embedded in
            the page this same server hands out), but a secret compared
            with a short-circuiting operator is the kind of detail that
            gets copied into a place where it does matter."""
            supplied = self.headers.get(WRITE_TOKEN_HEADER)
            if token and supplied and hmac.compare_digest(supplied, token):
                return True
            self._json_response(
                {"ok": False, "status": "forbidden", "message": "missing or invalid " + WRITE_TOKEN_HEADER},
                status=403,
            )
            return False

        def _read_json_body(self) -> tuple[dict, str | None, int]:
            """``(body, error_message, http_status)`` -- ``error_message``
            is ``None`` on success. An empty body is treated as ``{}``
            (some write actions, e.g. ``acquisition-delivered``, have zero
            REQUIRED fields).

            M-WA-3: a ``Content-Length`` above :data:`MAX_WRITE_BODY_BYTES`
            is refused without ever being allocated --
            ``self.rfile.read(length)`` on an attacker- (or bug-) supplied
            length is an unbounded allocation in a thread of a server the
            operator runs on their own machine. See
            :meth:`_discard_oversized_body` for why the bytes are then
            drained in fixed chunks rather than simply ignored."""
            length_raw = self.headers.get("Content-Length")
            try:
                length = int(length_raw) if length_raw else 0
            except ValueError:
                return {}, f"invalid Content-Length: {length_raw!r}", 400
            if length > MAX_WRITE_BODY_BYTES:
                self._discard_oversized_body(length)
                return (
                    {},
                    f"request body is {length} bytes; the write API accepts at most "
                    f"{MAX_WRITE_BODY_BYTES} ({MAX_WRITE_BODY_BYTES // (1024 * 1024)} MiB)",
                    413,
                )
            raw = self.rfile.read(length) if length > 0 else b""
            if not raw.strip():
                return {}, None, 200
            try:
                parsed_body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                return {}, f"request body is not valid JSON: {exc}", 400
            if not isinstance(parsed_body, dict):
                return {}, "request body must be a JSON object", 400
            return parsed_body, None, 200

        def _discard_oversized_body(self, length: int) -> None:
            """Throw away up to :data:`MAX_WRITE_BODY_BYTES` of a body this
            handler has already decided to refuse, in fixed-size chunks,
            then mark the connection for closing.

            The memory guard M-WA-3 asks for is about ALLOCATION, not about
            touching the bytes: ``rfile.read(length)`` would size one buffer
            from a number the client chose, while reading 64 KiB at a time
            and dropping it costs the same whether the client declared 2 MiB
            or 2 TB. Draining matters because of what happens otherwise --
            closing a socket that still has unread inbound data sends a TCP
            RST, and an RST discards the response that was already sitting
            in the peer's receive buffer. The caller would see a dropped
            connection instead of the 413 explaining what it did wrong,
            which is exactly the HTTP-000 shape the rest of this batch
            exists to eliminate.

            The drain is bounded twice over: by the byte budget, and by a
            short socket timeout, so a client that declares a huge body and
            then sends nothing cannot hold this thread open. A body that
            outruns the budget still ends in an RST -- at that point the
            caller is not one this server can hold a conversation with."""
            self.close_connection = True
            remaining = min(length, MAX_WRITE_BODY_BYTES)
            try:
                self.connection.settimeout(_DISCARD_TIMEOUT_S)
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except OSError:
                # A timeout or a peer that hung up mid-drain: nothing to do
                # but stop -- the response still has to go out.
                pass
            finally:
                try:
                    self.connection.settimeout(None)
                except OSError:
                    pass

        def _serve_index(self) -> None:
            """``GET /`` / ``GET /dashboard.html`` -- the ONE static asset
            this handler ever rewrites in flight: the per-serve-process
            write token is injected as a ``<meta>`` tag (module docstring,
            Stage 3 section) so the page's own JS can read it without a
            round-trip. Every other static asset (``dashboard.css``, ...)
            is untouched, served byte-for-byte by
            ``SimpleHTTPRequestHandler`` as before."""
            html = (STATIC_DIR / "dashboard.html").read_text(encoding="utf-8")
            if token and _INDEX_TOKEN_ANCHOR in html:
                injected = f'\n<meta name="dashboard-write-token" content="{token}">'
                html = html.replace(_INDEX_TOKEN_ANCHOR, _INDEX_TOKEN_ANCHOR + injected, 1)
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json_response(self, payload: dict, *, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _handle_sse(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            q = broadcaster.subscribe()
            try:
                self._sse_send("hello", build_meta(config))
                while not _shutdown.is_set():
                    try:
                        event, data = q.get(timeout=SSE_HEARTBEAT_S)
                        self._sse_send(event, data)
                    except Empty:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            finally:
                broadcaster.unsubscribe(q)

        def _sse_send(self, event: str, data: dict) -> None:
            msg = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
            self.wfile.write(msg.encode("utf-8"))
            self.wfile.flush()

    return DashboardHandler


class DashboardServer(http.server.ThreadingHTTPServer):
    daemon_threads = True  # SSE's long-lived request threads must not block shutdown

    # M-LU-4: SO_REUSEADDR does NOT mean the same thing on Windows that it
    # means on POSIX. There it lets a new listener take a port still in
    # TIME_WAIT; on Windows it lets a SECOND live socket bind a port another
    # process is already listening on, and the two then split incoming
    # connections nondeterministically. Two dashboards on one port means two
    # write tokens, two watchers, and an SSE stream that answers from
    # whichever server won the accept -- a state no operator can diagnose
    # from the page. So: keep the POSIX behaviour, and on Windows ask for
    # the opposite guarantee explicitly (SO_EXCLUSIVEADDRUSE) so a second
    # bind fails loudly instead.
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self) -> None:
        if sys.platform == "win32":
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if exclusive is not None:
                try:
                    self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
                except OSError:
                    # An older/oddly-configured stack that refuses the option
                    # is not a reason to refuse to serve; allow_reuse_address
                    # is already False, which is most of the protection.
                    traceback.print_exc()
        super().server_bind()


_shutdown = threading.Event()


# =============================================================================
# watcher thread
# =============================================================================
def watcher_loop(config: ServerConfig, broadcaster: Broadcaster, *, poll_interval: float, debounce: float) -> None:
    """Poll the watch set, coalesce, broadcast ``changed``.

    Two publish conditions, not one (LU-1). The original rule -- "publish
    once nothing has moved for ``debounce`` seconds" -- is a wait-for-quiet
    debounce, and a write burst that keeps landing inside the window (a
    long ingest checkpointing every second, exactly when the operator is
    watching) never has a quiet gap, so the page froze for the whole run
    and then updated once at the end. ``max_wait`` (2x debounce) is the
    starvation floor: however busy the program is, a ``changed`` goes out
    at least that often while it stays dirty."""
    prev = snapshot_mtimes(config)
    changed_accum: set[str] = set()
    last_change_ts: float | None = None
    first_dirty_ts: float | None = None
    max_wait = 2 * debounce
    while not _shutdown.is_set():
        _shutdown.wait(poll_interval)
        if _shutdown.is_set():
            break
        try:
            cur = snapshot_mtimes(config)
        except Exception:  # noqa: BLE001 - watcher must never crash the server
            traceback.print_exc()
            continue
        changed = diff_changed_paths(prev, cur)
        if changed:
            changed_accum |= changed
            last_change_ts = time.time()
            if first_dirty_ts is None:
                first_dirty_ts = last_change_ts
            prev = cur
        if last_change_ts is None or first_dirty_ts is None:
            continue
        checked_at = time.time()
        quiet_enough = (checked_at - last_change_ts) >= debounce
        starved = (checked_at - first_dirty_ts) >= max_wait
        if quiet_enough or starved:
            paths_list = sorted(changed_accum)
            changed_accum = set()
            last_change_ts = None
            first_dirty_ts = None
            broadcaster.publish(
                "changed",
                {
                    "changed_paths": paths_list,
                    # which STORES moved, so a client can decide whether the
                    # panel it is showing is even affected -- e.g. a
                    # platform-only change while the operator sits on Feed.
                    # The raw paths stay for a human reading the stream.
                    "changed_stores": changed_stores_for(config, paths_list),
                    "detected_ts": now(),
                },
            )


# =============================================================================
# main
# =============================================================================
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8850)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--program-root", default=None)
    ap.add_argument("--platform-root", default=None)
    ap.add_argument("--repo-root", default=None)
    ap.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_S)
    ap.add_argument("--debounce", type=float, default=DEFAULT_DEBOUNCE_S)
    ap.add_argument("--no-watch", action="store_true", help="serve only, disable the watcher/SSE-change thread")
    args = ap.parse_args(argv)

    _shutdown.clear()  # in case main() runs more than once in the same process (tests)

    config = ServerConfig(
        repo_root=Path(args.repo_root) if args.repo_root else Path.cwd(),
        program_root=Path(args.program_root) if args.program_root else None,
        platform_root=Path(args.platform_root) if args.platform_root else None,
        poll_interval=args.poll_interval,
        debounce=args.debounce,
    )
    broadcaster = Broadcaster()

    # Prime config.watch_status once up front (LU-9) so meta reports a
    # broken trialerror.toml even under --no-watch, where nothing else ever
    # re-derives the watch set.
    _watch_targets(config)

    watcher_thread = None
    if not args.no_watch:
        watcher_thread = threading.Thread(
            target=watcher_loop,
            args=(config, broadcaster),
            kwargs={"poll_interval": args.poll_interval, "debounce": args.debounce},
            name="trialerror-dashboard-watcher",
            daemon=True,
        )
        watcher_thread.start()

    # Stage 3 (build-v2dash-writes): one random token per serve-process
    # lifetime -- see WRITE_TOKEN_HEADER / _serve_index's own docstrings.
    # secrets.token_hex is CSPRNG-backed (unlike random/uuid4), appropriate
    # for a value that gates real writes even though this server only ever
    # binds loopback.
    write_token = secrets.token_hex(20)
    handler_cls = make_handler_class(config, broadcaster, write_token)
    try:
        server = DashboardServer((args.host, args.port), handler_cls)
    except OSError as exc:
        # M-LU-4: with SO_EXCLUSIVEADDRUSE the second server on a port now
        # FAILS to bind instead of silently sharing it. Say why, in one
        # line, rather than leaving a raw traceback in the detached log.
        _shutdown.set()
        print(
            f"[trialerror.dashboard] cannot bind {args.host}:{args.port} -- "
            f"another server is already listening there ({exc}). "
            f"Open http://{args.host}:{args.port}/ , or start this one on a different --port.",
            file=sys.stderr,
        )
        return 1
    print(f"[trialerror.dashboard] serving at http://{args.host}:{args.port}/  (Ctrl+C to stop)", file=sys.stderr)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n[trialerror.dashboard] shutting down...", file=sys.stderr)
    finally:
        _shutdown.set()
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via `trialerror dashboard serve`
    raise SystemExit(main())
