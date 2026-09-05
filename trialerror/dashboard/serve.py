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
- ``GET /dashboard.html`` / ``GET /dashboard.css`` -- plain static files
  from :data:`STATIC_DIR`.
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
  ``changed`` whenever the watcher detects a store file's mtime move,
  a heartbeat comment every 15s otherwise.

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
import http.server
import json
import secrets
import sys
import threading
import time
import traceback
import urllib.parse
from pathlib import Path
from queue import Empty, Queue
from typing import Any

from trialerror.dashboard.data import PANEL_BUILDERS, build_all_panels, build_doctor_panel, run_search
from trialerror.dashboard.doctor_run import read_doctor_state, run_doctor_and_persist
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
def _watched_paths(config: ServerConfig) -> list[Path]:
    """The DB file (+ its ``-wal``/``-shm`` WAL-mode sidecar files, which
    change on nearly every write, ahead of a checkpoint back to the main
    file) for every store this server can see. Re-derived on every poll
    (not cached once at startup) so a program that gets initialized AFTER
    the server starts (a fresh ``trialerror program init`` while the dashboard
    is already open) is picked up automatically."""
    bases: list[Path] = [store_paths.platform_db_path(root=config.platform_root)]
    if config.program_root is not None:
        cfg_path = config.program_root / "trialerror.toml"
        cfg = None
        if cfg_path.is_file():
            try:
                from trialerror.util.config import load_config

                cfg = load_config(cfg_path).raw
            except Exception:
                cfg = None
        bases.append(store_paths.ops_db_path(config.program_root, cfg))
        bases.append(store_paths.knowledge_db_path(config.program_root, cfg))
        bases.append(store_paths.jobs_db_path(config.program_root, cfg))
    out: list[Path] = []
    for base in bases:
        out.append(base)
        out.append(base.with_name(base.name + "-wal"))
        out.append(base.with_name(base.name + "-shm"))
    return out


def snapshot_mtimes(config: ServerConfig) -> dict[str, float]:
    result: dict[str, float] = {}
    for p in _watched_paths(config):
        try:
            if p.is_file():
                result[str(p)] = p.stat().st_mtime
        except OSError:
            pass
    return result


def diff_changed_paths(prev: dict[str, float], cur: dict[str, float]) -> set[str]:
    changed = set(cur) ^ set(prev)
    for k, v in cur.items():
        if k in prev and prev[k] != v:
            changed.add(k)
    return changed


def build_meta(config: ServerConfig) -> dict:
    return {
        "generated_ts": now(),
        "program_root": str(config.program_root) if config.program_root else None,
        "platform_root": str(config.platform_root),
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


#: panel name -> (query-string param name, the builder's own keyword it
#: feeds) for the handful of new panels that accept an optional selector
#: (``feed``'s ``thread_id``, ``rooms``'s ``room_id``, ``dossier``'s
#: ``artifact_id``, ``since_you_left``'s ``since``) -- every OTHER panel
#: (including every panel that existed before this build) ignores the
#: query string entirely, same as before. A request with no matching
#: param falls through to the builder's own default (e.g. "most recently
#: active thread"), never an error.
PANEL_QUERY_PARAMS: dict[str, tuple[str, str]] = {
    "feed": ("thread_id", "thread_id"),
    "rooms": ("room_id", "room_id"),
    "dossier": ("artifact_id", "artifact_id"),
    "since_you_left": ("since", "since"),
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
    spec = PANEL_QUERY_PARAMS.get(name)
    if spec is not None and query_params:
        qs_key, kwarg_name = spec
        values = query_params.get(qs_key)
        if values and values[0]:
            kwargs[kwarg_name] = values[0]
    rostore = open_store_ro(config.program_root, platform_root=config.platform_root)
    try:
        return builder(rostore, **kwargs)
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
    field names) are optional."""
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

    rostore = open_store_ro(config.program_root, platform_root=config.platform_root)
    try:
        return run_search(rostore, query=q, k=k, mode=mode, filters=filters or None)
    finally:
        rostore.close()


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
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path

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
                action = path[len("/dashboard/api/write/") :]
                if action not in WRITABLE_ACTIONS:
                    self.send_error(404, f"no such write action: {action!r}")
                    return
                if not self._require_token():
                    return
                body, err = self._read_json_body()
                if err is not None:
                    self._json_response({"ok": False, "status": "bad_request", "message": err})
                    return
                result = dispatch_write(
                    action,
                    program_root=config.program_root,
                    platform_root=config.platform_root,
                    body=body,
                )
                self._json_response(result)
                return

            self.send_error(404, f"no such POST route: {path!r}")

        def _require_token(self) -> bool:
            """The CSRF-class guard (module docstring, Stage 3 section):
            every write POST must echo the per-serve-process token back on
            :data:`WRITE_TOKEN_HEADER`. ``token is None`` (this handler
            class was built without one -- should never happen via ``main``,
            but a defensive default for any other caller of
            :func:`make_handler_class`) refuses every write outright rather
            than silently accepting an unguarded one."""
            supplied = self.headers.get(WRITE_TOKEN_HEADER)
            if token and supplied == token:
                return True
            body = json.dumps(
                {"ok": False, "status": "forbidden", "message": "missing or invalid " + WRITE_TOKEN_HEADER},
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return False

        def _read_json_body(self) -> tuple[dict, str | None]:
            """``(body, error_message)`` -- ``error_message`` is ``None`` on
            success. An empty body is treated as ``{}`` (some write actions,
            e.g. ``acquisition-delivered``, have zero REQUIRED fields)."""
            length_raw = self.headers.get("Content-Length")
            try:
                length = int(length_raw) if length_raw else 0
            except ValueError:
                return {}, f"invalid Content-Length: {length_raw!r}"
            raw = self.rfile.read(length) if length > 0 else b""
            if not raw.strip():
                return {}, None
            try:
                parsed_body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                return {}, f"request body is not valid JSON: {exc}"
            if not isinstance(parsed_body, dict):
                return {}, "request body must be a JSON object"
            return parsed_body, None

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

        def _json_response(self, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(200)
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
    allow_reuse_address = True


_shutdown = threading.Event()


# =============================================================================
# watcher thread
# =============================================================================
def watcher_loop(config: ServerConfig, broadcaster: Broadcaster, *, poll_interval: float, debounce: float) -> None:
    prev = snapshot_mtimes(config)
    changed_accum: set[str] = set()
    dirty_since: float | None = None
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
            dirty_since = time.time()
            prev = cur
        if dirty_since is not None and (time.time() - dirty_since) >= debounce:
            paths_list = sorted(changed_accum)
            changed_accum = set()
            dirty_since = None
            broadcaster.publish("changed", {"changed_paths": paths_list, "detected_ts": now()})


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
    server = DashboardServer((args.host, args.port), handler_cls)
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
