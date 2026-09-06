"""Real-subprocess smoke test for ``trialerror dashboard serve`` -- same class
of test as ``tests/test_mcp_ops_protocol.py::
test_stdio_smoke_real_subprocess_initialize_and_tools_list`` (the "M14
stdio-smoke pattern" this build's brief names as the reference): launch the
actual CLI entry point (``python -m trialerror.cli dashboard serve
--foreground``) as a REAL child process, talk to it over real HTTP/TCP, and
shut it down cleanly -- proving the ``trialerror.cli`` wiring end to end, not
just ``trialerror.dashboard.serve``'s functions in-process.

A REAL browser exercising the served page's DOM/JS is out of this test's
reach (headless-DOM territory) -- see ``trialerror.dashboard.accept_items.
DASHBOARD_LIVE_ITEMS`` / ``tests/test_dashboard_accept_items.py`` for that
item, enumerated rather than silently skipped.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from queue import Empty

import pytest

from trialerror.artifacts.gates import open_gate, record_verdict, submit_gate
from trialerror.artifacts.registry import create_artifact
from trialerror.dashboard import serve as dashboard_serve
from trialerror.dashboard.doctor_run import doctor_state_path
from trialerror.dashboard.store_ro import open_store_ro
from trialerror.events.api import post_feed
from trialerror.stores.store import open_store
from tests._store_fixtures import populate_one_of_everything

REPO_ROOT = Path(__file__).resolve().parents[1]

_TOKEN_META_RE = re.compile(r'<meta name="dashboard-write-token" content="([0-9a-f]+)">')


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for_server(host: str, port: int, *, timeout_s: float = 15.0) -> None:
    deadline = time.time() + timeout_s
    last_exc: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://{host}:{port}/", timeout=1.0) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_exc = exc
            time.sleep(0.2)
    raise AssertionError(f"dashboard server never came up on {host}:{port}: {last_exc}")


def _get_write_token(host: str, port: int) -> str:
    """Fetch ``GET /`` and pull the per-serve-process write token out of the
    ``<meta name="dashboard-write-token">`` tag the page's own JS
    (``getWriteToken()``) reads the same way -- proves the token really is
    delivered to the served page, not just generated in-process."""
    with urllib.request.urlopen(f"http://{host}:{port}/", timeout=5) as resp:
        body = resp.read().decode("utf-8")
    m = _TOKEN_META_RE.search(body)
    assert m is not None, "served index page has no dashboard-write-token <meta> tag"
    return m.group(1)


def _post_json(host: str, port: int, path: str, body: dict, *, token: str | None) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-TrialError-Dashboard-Token"] = token
    req = urllib.request.Request(f"http://{host}:{port}{path}", data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(payload)
        except json.JSONDecodeError:
            return exc.code, {"raw": payload}


def _sse_handshake_bytes(host: str, port: int, *, timeout_s: float = 5.0) -> bytes:
    sock = socket.create_connection((host, port), timeout=timeout_s)
    try:
        sock.sendall(
            f"GET /dashboard/events HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode("ascii")
        )
        sock.settimeout(timeout_s)
        data = b""
        while b"\n\n" not in data and len(data) < 8192:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        return data
    finally:
        sock.close()


@pytest.fixture()
def seeded_program(tmp_path):
    program_root = tmp_path / "program"
    program_root.mkdir()
    (program_root / "trialerror.toml").write_text('[program]\nid = "PROG-dash-smoke"\n', encoding="utf-8")
    platform_root = tmp_path / "platform"

    store = open_store(program_root, platform_root=platform_root)
    ids = populate_one_of_everything(store)
    store.close()
    return program_root, platform_root, ids


def test_dashboard_serve_subprocess_smoke(seeded_program):
    program_root, platform_root, ids = seeded_program
    host = "127.0.0.1"
    port = _free_port()

    argv = [
        sys.executable, "-m", "trialerror.cli", "dashboard", "serve", "--foreground",
        "--host", host, "--port", str(port),
        "--program-root", str(program_root), "--platform-root", str(platform_root),
        "--poll-interval", "0.2", "--debounce", "0.3",
    ]
    # stdout/stderr -> a log file, not subprocess.PIPE: nothing in this test
    # drains the pipe while the server blocks in serve_forever(), and an
    # undrained PIPE can deadlock once the OS pipe buffer fills.
    log_path = program_root.parent / "dashboard_serve_stdout.log"
    log_fh = open(log_path, "wb")
    proc = subprocess.Popen(argv, cwd=str(REPO_ROOT), stdout=log_fh, stderr=subprocess.STDOUT)
    try:
        _wait_for_server(host, port)

        # GET / -- the dashboard page itself. HALIDE shell: a persistent
        # rail (data-role="rail") with one data-panel button per surface,
        # plus the DOM hooks every bespoke panel renderer targets. This is
        # a raw string/regex check on the SERVED (pre-JS-execution) HTML --
        # real per-panel content is JS-rendered client-side and out of this
        # test's reach (see trialerror.dashboard.accept_items.DASHBOARD_LIVE_ITEMS
        # for the real-browser DOM item), but every container hook a
        # renderer writes into must already exist in the markup, statically.
        with urllib.request.urlopen(f"http://{host}:{port}/", timeout=5) as resp:
            assert resp.status == 200
            body = resp.read().decode("utf-8")
        assert "TrialError dashboard" in body
        assert 'data-role="rail"' in body
        # Stage 3 (build-v2dash-writes): the live-served page carries a
        # per-serve-process write token -- a static export never does (see
        # tests/test_dashboard_export.py's own read-only-export assertion).
        m = _TOKEN_META_RE.search(body)
        assert m is not None, "served index page has no dashboard-write-token <meta> tag"
        write_token = m.group(1)
        assert len(write_token) >= 32  # secrets.token_hex(20) -> 40 hex chars
        for panel_name in (
            "home", "search", "evidence", "lexicon", "dossier", "course",
            "rooms", "feed", "determinations", "console",
        ):
            assert f'data-panel="{panel_name}"' in body, f"missing rail item / panel section for {panel_name!r}"
        # ext-panel injection points (KNOW + RUN), populated at runtime from
        # meta.ext_panels -- the containers must exist even with zero
        # extensions declared.
        assert 'data-role="rail-ext-KNOW"' in body
        assert 'data-role="rail-ext-RUN"' in body
        # the command line / search form hooks (Main's hero + the ASK tab)
        assert 'data-role="home-search-form"' in body
        assert 'data-role="home-search-input"' in body
        assert 'data-role="search-form"' in body
        assert 'data-role="search-input"' in body
        # per-surface content containers a bespoke renderer writes into
        for role in (
            "since-you-left-list", "needs-card", "ops-ribbon",           # home
            "feed-thread-list", "feed-post-list",                        # feed
            "rooms-list", "rooms-turns",                                 # rooms
            "determ-list", "determ-detail",                              # determinations
            "dossier-registry-list", "dossier-detail",                   # dossier
            "lexicon-index-list", "lexicon-detail",                      # lexicon
            "course-body",                                               # course
            # console: one container per TEConsole.CARD_ORDER entry, plus the
            # subbar's two readings. LEDGER and TIMELINE ship hidden until a
            # renderer for them exists -- the HOOK is here either way, which is
            # this page's rule: every container a renderer writes into is real
            # markup, findable without executing a line of JS.
            "console-body-session", "console-body-pools", "console-body-ledger",
            "console-body-timeline", "console-body-jobs", "console-body-gates",
            "console-body-corpus", "console-body-doctor",
            "console-asof", "console-health-tally",
        ):
            assert f'data-role="{role}"' in body, f"missing DOM hook data-role={role!r}"
        # HALIDE tokens: the page requests the two build-contract fonts and
        # links the one stylesheet a re-skin ever needs to touch
        assert "IBM+Plex+Mono" in body
        assert 'href="dashboard.css"' in body
        # the renderer split (spec section 0): the page LOADS console_render.js
        # instead of carrying its renderers inline.
        assert '<script src="console_render.js"></script>' in body

        # ...and the server serves that file, from the same static root as the
        # stylesheet. A 404 here is a page whose Console never renders.
        with urllib.request.urlopen(f"http://{host}:{port}/console_render.js", timeout=5) as resp:
            assert resp.status == 200
            js_body = resp.read().decode("utf-8")
        assert "TEConsole" in js_body

        # GET a static asset (the external stylesheet) -- proves the
        # document-root static serving works, not just the "/" rewrite --
        # and that it actually carries the HALIDE token variables, not the
        # V1 legibility-only stylesheet it replaced.
        with urllib.request.urlopen(f"http://{host}:{port}/dashboard.css", timeout=5) as resp:
            assert resp.status == 200
            css_body = resp.read().decode("utf-8")
        assert "--live: #3FE07A" in css_body
        assert "--crit-fill: #E13A47" in css_body

        # GET one panel JSON endpoint
        with urllib.request.urlopen(f"http://{host}:{port}/dashboard/api/session", timeout=5) as resp:
            assert resp.status == 200
            panel = json.loads(resp.read().decode("utf-8"))
        assert panel["status"] == "ok"
        assert panel["open_session"]["session_id"] == ids["session"]

        # GET the aggregate endpoint -- every panel present
        with urllib.request.urlopen(f"http://{host}:{port}/dashboard/api/all", timeout=5) as resp:
            all_payload = json.loads(resp.read().decode("utf-8"))
        assert set(all_payload["panels"]) == {
            "session", "budget", "jobs", "gates", "corpus", "doctor",
            "feed", "rooms", "determinations", "dossier", "lexicon", "course", "since_you_left",
        }
        assert all_payload["meta"]["program_root"] == str(program_root)

        # one new panel's own single-panel endpoint
        with urllib.request.urlopen(f"http://{host}:{port}/dashboard/api/feed", timeout=5) as resp:
            assert resp.status == 200
            feed_panel = json.loads(resp.read().decode("utf-8"))
        assert feed_panel["status"] == "ok"
        assert feed_panel["active_thread_id"] == ids["thread"]

        # query-param selector wiring: an explicit thread_id is honored.
        with urllib.request.urlopen(
            f"http://{host}:{port}/dashboard/api/feed?thread_id={ids['thread']}", timeout=5
        ) as resp:
            feed_scoped = json.loads(resp.read().decode("utf-8"))
        assert feed_scoped["active_thread_id"] == ids["thread"]

        # the search endpoint: empty query -> a well-formed, empty result,
        # never an error.
        with urllib.request.urlopen(f"http://{host}:{port}/dashboard/api/search?q=", timeout=5) as resp:
            assert resp.status == 200
            search_payload = json.loads(resp.read().decode("utf-8"))
        assert search_payload["status"] == "ok"
        assert search_payload["results"] == []

        # invalid search mode -> a clean "invalid_mode" status, not a 500.
        with urllib.request.urlopen(
            f"http://{host}:{port}/dashboard/api/search?q=hello&mode=not-a-real-mode", timeout=5
        ) as resp:
            assert resp.status == 200
            bad_mode_payload = json.loads(resp.read().decode("utf-8"))
        assert bad_mode_payload["status"] == "invalid_mode"

        # unknown panel name -> 404, not a crash
        try:
            urllib.request.urlopen(f"http://{host}:{port}/dashboard/api/nope", timeout=5)
            raise AssertionError("expected HTTPError for an unknown panel name")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404

        # doctor run-on-demand endpoint is now POST + token-guarded (Stage
        # 3, build-v2dash-writes) -- a bare GET is refused (405), the SAME
        # guard every other write action gets.
        try:
            urllib.request.urlopen(f"http://{host}:{port}/dashboard/api/doctor/run", timeout=5)
            raise AssertionError("expected HTTPError for a GET on a write-only route")
        except urllib.error.HTTPError as exc:
            assert exc.code == 405
            assert exc.headers.get("Allow") == "POST"
        status, doctor_panel = _post_json(host, port, "/dashboard/api/doctor/run", {}, token=write_token)
        assert status == 200
        assert doctor_panel["status"] == "ok"
        assert doctor_panel["last_run"]["summary"]["total"] > 0

        # ---- Stage 3 write-action guard proofs ----------------------------
        # missing token -> 403, never a silent write
        status, refused = _post_json(host, port, "/dashboard/api/write/feed-post", {"thread_id": ids["thread"], "body": "no token"}, token=None)
        assert status == 403
        assert refused["ok"] is False

        # wrong token -> 403
        status, refused = _post_json(host, port, "/dashboard/api/write/feed-post", {"thread_id": ids["thread"], "body": "wrong token"}, token="not-the-real-token")
        assert status == 403
        assert refused["ok"] is False

        # unknown write action -> 404, not a crash
        status, _body = _post_json(host, port, "/dashboard/api/write/not-a-real-action", {}, token=write_token)
        assert status == 404

        # a real write, correctly authorized: post into the fixture thread.
        status, posted = _post_json(
            host, port, "/dashboard/api/write/feed-post",
            {"thread_id": ids["thread"], "body": "operator directive over real HTTP"},
            token=write_token,
        )
        assert status == 200
        assert posted["ok"] is True
        assert posted["result"]["thread_id"] == ids["thread"]
        assert posted["result"]["author"].startswith("orchestrator:")

        # a clean business refusal surfaces the refusing module's own
        # message verbatim -- never a generic "failed" (design constraint).
        status, refused_missing = _post_json(
            host, port, "/dashboard/api/write/feed-post", {"thread_id": "THR-does-not-exist", "body": "x"}, token=write_token,
        )
        assert status == 200  # a business refusal is still a 200 with ok:false, not an HTTP error
        assert refused_missing["ok"] is False
        assert refused_missing["message"]  # the module's own text, not empty/generic

        # missing required field -> a clean refusal naming the field, never
        # a 500 or a store connection opened for nothing.
        status, refused_field = _post_json(host, port, "/dashboard/api/write/room-turn", {"room_id": ids["room"]}, token=write_token)
        assert status == 200
        assert refused_field["ok"] is False
        assert "launch_id" in refused_field["message"]

        # SSE handshake: a fresh connection gets a `hello` event immediately
        sse_bytes = _sse_handshake_bytes(host, port)
        assert b"event: hello" in sse_bytes
        assert b"data:" in sse_bytes
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=15)
        log_fh.close()
    # clean shutdown: the process actually ended (terminate delivered, not hung)
    assert proc.poll() is not None


@pytest.fixture()
def seeded_program_with_ext_panel(tmp_path):
    """Same fixture program as ``seeded_program``, plus one working and one
    deliberately-broken extension panel under ``trialerror_ext/panels/`` --
    exercises ``trialerror.dashboard.ext``'s serve-layer wiring (C-0070) end to
    end, over a REAL HTTP server, the same "extend the existing subprocess
    smoke pattern" this build's brief asks for."""
    program_root = tmp_path / "program"
    program_root.mkdir()
    (program_root / "trialerror.toml").write_text('[program]\nid = "PROG-dash-ext-smoke"\n', encoding="utf-8")
    platform_root = tmp_path / "platform"

    store = open_store(program_root, platform_root=platform_root)
    ids = populate_one_of_everything(store)
    store.close()

    panels_root = program_root / "trialerror_ext" / "panels"

    good_dir = panels_root / "job_count"
    good_dir.mkdir(parents=True)
    (good_dir / "panel.toml").write_text(
        '[panel]\ntitle = "Job Count"\nnav_group = "RUN"\norder = 1\ndescription = "fixture panel"\n',
        encoding="utf-8",
    )
    (good_dir / "builder.py").write_text(
        "def build_panel(rostore, program_root):\n"
        "    n = rostore.jobs.execute('SELECT COUNT(*) FROM job').fetchone()[0]\n"
        "    return {'status': 'ok', 'job_count': n}\n",
        encoding="utf-8",
    )

    broken_dir = panels_root / "broken"
    broken_dir.mkdir(parents=True)
    (broken_dir / "panel.toml").write_text(
        '[panel]\ntitle = "Broken"\nnav_group = "RUN"\norder = 2\n', encoding="utf-8"
    )
    (broken_dir / "builder.py").write_text(
        "def build_panel(rostore, program_root):\n    raise RuntimeError('deliberate fixture failure')\n",
        encoding="utf-8",
    )

    return program_root, platform_root, ids


def test_dashboard_serve_ext_panel_subprocess_smoke(seeded_program_with_ext_panel):
    program_root, platform_root, ids = seeded_program_with_ext_panel
    host = "127.0.0.1"
    port = _free_port()

    argv = [
        sys.executable, "-m", "trialerror.cli", "dashboard", "serve", "--foreground",
        "--host", host, "--port", str(port),
        "--program-root", str(program_root), "--platform-root", str(platform_root),
        "--poll-interval", "0.2", "--debounce", "0.3",
    ]
    log_path = program_root.parent / "dashboard_serve_ext_stdout.log"
    log_fh = open(log_path, "wb")
    proc = subprocess.Popen(argv, cwd=str(REPO_ROOT), stdout=log_fh, stderr=subprocess.STDOUT)
    try:
        _wait_for_server(host, port)

        # the listing: both fixture panels, sorted by order, error-free at
        # the manifest stage (the broken one only fails once build_panel
        # actually runs).
        with urllib.request.urlopen(f"http://{host}:{port}/dashboard/api/ext", timeout=5) as resp:
            assert resp.status == 200
            listing = json.loads(resp.read().decode("utf-8"))
        assert [row["name"] for row in listing] == ["job_count", "broken"]
        assert listing[0]["manifest_status"] == "ok"
        assert listing[0]["title"] == "Job Count"

        # the working panel's own data endpoint
        with urllib.request.urlopen(f"http://{host}:{port}/dashboard/api/ext/job_count", timeout=5) as resp:
            assert resp.status == 200
            panel = json.loads(resp.read().decode("utf-8"))
        assert panel == {"status": "ok", "job_count": 1}

        # the broken panel's own data endpoint: 200, never 500 -- an
        # extension crash must never look like a server failure.
        with urllib.request.urlopen(f"http://{host}:{port}/dashboard/api/ext/broken", timeout=5) as resp:
            assert resp.status == 200
            broken_panel = json.loads(resp.read().decode("utf-8"))
        assert broken_panel["status"] == "ext_error"
        assert "deliberate fixture failure" in broken_panel["message"]

        # unknown extension panel name -> 404, not a crash
        try:
            urllib.request.urlopen(f"http://{host}:{port}/dashboard/api/ext/nope", timeout=5)
            raise AssertionError("expected HTTPError for an unknown extension panel name")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404

        # /dashboard/api/all: core panel set unchanged, both ext panels
        # nested under panels["ext"], and the listing echoed in meta.
        with urllib.request.urlopen(f"http://{host}:{port}/dashboard/api/all", timeout=5) as resp:
            all_payload = json.loads(resp.read().decode("utf-8"))
        assert set(all_payload["panels"]) == {
            "session", "budget", "jobs", "gates", "corpus", "doctor",
            "feed", "rooms", "determinations", "dossier", "lexicon", "course", "since_you_left", "ext",
        }
        assert all_payload["panels"]["ext"]["job_count"] == {"status": "ok", "job_count": 1}
        assert all_payload["panels"]["ext"]["broken"]["status"] == "ext_error"
        assert len(all_payload["meta"]["ext_panels"]) == 2

        # doctor: the broken fixture panel surfaces as a warn, never a
        # dashboard-wide failure. POST + token-guarded (Stage 3).
        write_token = _get_write_token(host, port)
        status, doctor_panel = _post_json(host, port, "/dashboard/api/doctor/run", {}, token=write_token)
        assert status == 200
        checks = {c["name"]: c for c in doctor_panel["last_run"]["checks"]}
        assert checks["ext_panels_valid"]["status"] == "pass"  # manifest/import/signature only -- both fixtures are structurally sound
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=15)
        log_fh.close()
    assert proc.poll() is not None


@pytest.fixture()
def seeded_program_with_gate_edit(tmp_path):
    """The ``seeded_program`` fixture PLUS a real gate carrying one
    verified-pending blocking edit (``trialerror.artifacts.gates.record_verdict``,
    not a raw fixture row) -- ``populate_one_of_everything``'s own gate sits
    at ``state='draft'`` with no ``edits`` (a schema round-trip placeholder,
    not a real reviewed gate), which ``verify_edit`` refuses (it requires
    ``state='gated'``). This fixture builds one for real, end to end, so the
    write-action loop test below exercises the actual state machine."""
    program_root = tmp_path / "program"
    program_root.mkdir()
    (program_root / "trialerror.toml").write_text('[program]\nid = "PROG-dash-writes-e2e"\n', encoding="utf-8")
    platform_root = tmp_path / "platform"

    store = open_store(program_root, platform_root=platform_root)
    ids = populate_one_of_everything(store)
    artifact = create_artifact(
        store, type_key=ids["template"], title="e2e edit artifact", path="artifacts/e2e-edit.md",
        sha256="a" * 64, by_launch=ids["launch"], purpose="dashboard write e2e test",
    )
    gate = open_gate(store, artifact_id=artifact["artifact_id"])
    submit_gate(store, gate_id=gate["gate_id"], by_launch=ids["launch"])
    verdict = record_verdict(
        store, gate_id=gate["gate_id"], verdict="PASS_WITH_EDITS", critic_launch=ids["launch"],
        edits=[{"text": "fix the tally", "blocking": True}],
    )
    edit_id = json.loads(verdict["edits"])[0]["edit_id"]
    store.close()

    ids["edit_gate_id"] = gate["gate_id"]
    ids["edit_id"] = edit_id
    ids["edit_artifact_id"] = artifact["artifact_id"]
    return program_root, platform_root, ids


def test_dashboard_write_actions_full_loop_subprocess(seeded_program_with_gate_edit):
    """The end-to-end subprocess loop the build brief asks for: post a
    directive -> it appears in the feed panel; verify a gate edit -> the
    gates panel (and the determinations queue) reflect it. Real subprocess,
    real HTTP, real token guard -- not ``writes.dispatch`` called
    in-process (see ``tests/test_dashboard_writes.py`` for that, faster,
    unit-level coverage of every action's success/refusal/missing-field
    path)."""
    program_root, platform_root, ids = seeded_program_with_gate_edit
    host = "127.0.0.1"
    port = _free_port()

    argv = [
        sys.executable, "-m", "trialerror.cli", "dashboard", "serve", "--foreground",
        "--host", host, "--port", str(port),
        "--program-root", str(program_root), "--platform-root", str(platform_root),
        "--poll-interval", "0.2", "--debounce", "0.3",
    ]
    log_path = program_root.parent / "dashboard_serve_writes_e2e.log"
    log_fh = open(log_path, "wb")
    proc = subprocess.Popen(argv, cwd=str(REPO_ROOT), stdout=log_fh, stderr=subprocess.STDOUT)
    try:
        _wait_for_server(host, port)
        write_token = _get_write_token(host, port)

        # ---- 1) post directive -> appears in the feed panel ---------------
        directive_body = "operator directive: check the tally on the e2e artifact"
        status, posted = _post_json(
            host, port, "/dashboard/api/write/feed-post",
            {"thread_id": ids["thread"], "body": directive_body}, token=write_token,
        )
        assert status == 200
        assert posted["ok"] is True

        with urllib.request.urlopen(f"http://{host}:{port}/dashboard/api/feed?thread_id={ids['thread']}", timeout=5) as resp:
            feed_panel = json.loads(resp.read().decode("utf-8"))
        bodies = [p["body"] for p in feed_panel["posts"]]
        assert directive_body in bodies
        posted_row = next(p for p in feed_panel["posts"] if p["body"] == directive_body)
        # authorship is server-derived -- posts as the orchestrator, never a
        # caller-supplied name (design brief: "operator directives;
        # authorship is server-derived").
        assert posted_row["author"].startswith("orchestrator:")
        assert posted_row["author"] == posted["result"]["author"]

        # sanity: BEFORE verifying, the edit is a live, blocking
        # determination item.
        with urllib.request.urlopen(f"http://{host}:{port}/dashboard/api/determinations", timeout=5) as resp:
            determ_before = json.loads(resp.read().decode("utf-8"))
        gate_edit_ids_before = {i["id"] for i in determ_before["items"] if i["kind"] == "gate_edit"}
        assert f"{ids['edit_gate_id']}::{ids['edit_id']}" in gate_edit_ids_before

        # ---- 2) verify edit -> the gates panel reflects it -----------------
        status, verified = _post_json(
            host, port, "/dashboard/api/write/verify-edit",
            {
                "gate_id": ids["edit_gate_id"], "edit_id": ids["edit_id"],
                "by_launch": ids["launch"], "verified_note": "confirmed by operator",
            },
            token=write_token,
        )
        assert status == 200
        assert verified["ok"] is True
        assert verified["result"]["state"] == "gated"  # verify_edit is NOT a state transition

        with urllib.request.urlopen(f"http://{host}:{port}/dashboard/api/gates", timeout=5) as resp:
            gates_panel = json.loads(resp.read().decode("utf-8"))
        pending_row = next(r for r in gates_panel["pending_edits"] if r["gate_id"] == ids["edit_gate_id"])
        # sweep §3.10 item 3 (spec §5.1): `edits` arrives DECODED -- no
        # client-side JSON.parse of a value the server just serialized.
        assert isinstance(pending_row["edits"], list)
        assert pending_row["unverified_count"] == 0
        edit_row = next(e for e in pending_row["edits"] if e["edit_id"] == ids["edit_id"])
        assert edit_row["verified"] is True
        assert edit_row["applied"] is True
        assert edit_row["verified_note"] == "confirmed by operator"
        assert edit_row["applied_by_launch"] == ids["launch"]

        # the determinations queue reflects it too: the now-verified edit no
        # longer appears as an open item (design constraint: "a successful
        # action ... shows the state change").
        with urllib.request.urlopen(f"http://{host}:{port}/dashboard/api/determinations", timeout=5) as resp:
            determ_after = json.loads(resp.read().decode("utf-8"))
        gate_edit_ids_after = {i["id"] for i in determ_after["items"] if i["kind"] == "gate_edit"}
        assert f"{ids['edit_gate_id']}::{ids['edit_id']}" not in gate_edit_ids_after
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=15)
        log_fh.close()
    assert proc.poll() is not None


# =============================================================================
# S1 -- server hardening (sweep batch S1: LU-7, LU-1, LU-9, M-LU-2, M-LU-4,
# M-LU-5). The watch set, the debounce, and the per-builder fence, tested at
# the level each actually lives at: the watch-set and flush rules in-process
# (fast, deterministic), and LU-7's own regression over a REAL SSE stream,
# because the bug WAS the interaction between serving a read and watching a
# file -- nothing smaller reproduces it.
# =============================================================================
def _config_for(program_root: Path, platform_root: Path) -> dashboard_serve.ServerConfig:
    return dashboard_serve.ServerConfig(
        repo_root=REPO_ROOT, program_root=program_root, platform_root=platform_root
    )


def _watched_db(config: dashboard_serve.ServerConfig, store_kind: str) -> Path:
    """The main DB file (not the ``-wal``) this server watches for one store."""
    return next(
        p for p, kind in dashboard_serve._watch_targets(config) if kind == store_kind and p.suffix == ".db"
    )


class _SseClient:
    """A raw-socket SSE reader. ``urllib`` cannot hold a stream open and let
    the test do other HTTP work meanwhile, which is exactly the shape LU-7's
    regression needs: an idle subscriber attached while read-only GETs are
    served."""

    def __init__(self, host: str, port: int, *, timeout_s: float = 5.0) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout_s)
        self.sock.sendall(
            f"GET /dashboard/events HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode("ascii")
        )
        self.buf = b""

    def read_for(self, seconds: float) -> None:
        deadline = time.time() + seconds
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(4096)
            except TimeoutError:
                return
            except OSError:
                return
            if not chunk:
                return
            self.buf += chunk

    def changed_frames(self) -> list[dict]:
        frames: list[dict] = []
        for block in self.buf.decode("utf-8", "replace").split("\n\n"):
            if "event: changed" not in block:
                continue
            for line in block.splitlines():
                if line.startswith("data: "):
                    frames.append(json.loads(line[len("data: ") :]))
        return frames

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def test_watch_set_drops_shm_and_covers_wal_and_the_doctor_sidecar(seeded_program):
    """LU-7's unit half (+ M-LU-5). ``-shm`` is where a WAL *reader* writes
    its read-mark, so watching it made every panel GET look like a write and
    the watcher re-fired on the dashboard's own reads forever. The watch set
    must be main-file + ``-wal`` for all four stores, plus the doctor
    sidecar, and must contain no ``-shm`` for ANY store."""
    program_root, platform_root, _ids = seeded_program
    config = _config_for(program_root, platform_root)

    targets = dashboard_serve._watch_targets(config)
    by_name = {p.name: kind for p, kind in targets}

    assert [n for n in by_name if n.endswith("-shm")] == []
    for db_name, kind in (
        ("platform.db", "platform"),
        ("ops.db", "ops"),
        ("knowledge.db", "knowledge"),
        ("jobs.db", "jobs"),
    ):
        assert by_name.get(db_name) == kind, f"{db_name} missing from the watch set"
        assert by_name.get(db_name + "-wal") == kind, f"{db_name}-wal missing from the watch set"
    assert by_name.get("doctor_state.json") == "doctor"
    assert doctor_state_path(program_root) in [p for p, _k in targets]

    # _watched_paths is just the paths of the same set (what snapshot_mtimes
    # walks), so the -shm rule holds there too.
    assert [p for p in dashboard_serve._watched_paths(config) if p.name.endswith("-shm")] == []

    # path -> store attribution, the changed_stores vocabulary
    ops_paths = [str(p) for p, kind in targets if kind == "ops"]
    assert dashboard_serve.changed_stores_for(config, ops_paths) == ["ops"]
    assert dashboard_serve.changed_stores_for(config, ["nothing/we/watch.db"]) == []


def test_snapshot_keys_on_size_and_mtime_ns(seeded_program):
    """A float ``st_mtime`` alone missed a same-tick rewrite; the pair
    ``(size, st_mtime_ns)`` catches both a length change at an unchanged
    timestamp and two commits inside one filesystem tick."""
    program_root, platform_root, _ids = seeded_program
    config = _config_for(program_root, platform_root)

    snap = dashboard_serve.snapshot_mtimes(config)
    ops_db = str(_watched_db(config, "ops"))
    assert ops_db in snap
    size, mtime_ns = snap[ops_db]
    assert isinstance(size, int) and isinstance(mtime_ns, int)

    same_time_different_length = dict(snap)
    same_time_different_length[ops_db] = (size + 4096, mtime_ns)
    assert dashboard_serve.diff_changed_paths(snap, same_time_different_length) == {ops_db}

    same_length_later = dict(snap)
    same_length_later[ops_db] = (size, mtime_ns + 1)
    assert dashboard_serve.diff_changed_paths(snap, same_length_later) == {ops_db}


def test_a_read_only_store_open_moves_nothing_in_the_watch_set(seeded_program):
    """LU-7 at unit scale, measured rather than assumed.

    Opening the four stores read-only and running a query is what EVERY
    panel GET does. Against a cold program (the last writer's close
    checkpointed the WAL away) SQLite must create ``-wal`` and ``-shm``
    before a WAL reader can proceed, and then re-stamps ``-shm`` on every
    subsequent read. Neither may register as a change, or the dashboard
    triggers its own refresh forever."""
    program_root, platform_root, _ids = seeded_program
    config = _config_for(program_root, platform_root)

    before = dashboard_serve.snapshot_mtimes(config)
    for _ in range(3):
        rostore = open_store_ro(program_root, platform_root=platform_root)
        try:
            rostore.ops.execute("SELECT COUNT(*) FROM event").fetchone()
            rostore.jobs.execute("SELECT COUNT(*) FROM job").fetchone()
            rostore.platform.execute("SELECT COUNT(*) FROM launch").fetchone()
        finally:
            rostore.close()
    after = dashboard_serve.snapshot_mtimes(config)

    assert dashboard_serve.diff_changed_paths(before, after) == set()

    # and the reader's own leftovers are genuinely there -- this test would
    # pass vacuously if nothing had been created at all.
    ops_db = _watched_db(config, "ops")
    assert ops_db.with_name(ops_db.name + "-shm").is_file()


def test_watch_status_reports_an_unparseable_program_config(tmp_path):
    """LU-9: a ``trialerror.toml`` that will not parse silently fell back to
    DEFAULT store paths -- for a program with a relocated ``[paths]`` the
    watcher then polled files nobody writes, and a permanently ``changed``-less
    stream is indistinguishable from a quiet program. It must be visible on
    meta."""
    program_root = tmp_path / "program"
    program_root.mkdir()
    (program_root / "trialerror.toml").write_text('[program]\nid = "PROG-ok"\n', encoding="utf-8")
    config = _config_for(program_root, tmp_path / "platform")

    dashboard_serve._watched_paths(config)
    assert dashboard_serve.build_meta(config)["watch_status"] == "ok"

    (program_root / "trialerror.toml").write_text("[program\nid = broken", encoding="utf-8")
    dashboard_serve._watched_paths(config)
    status = dashboard_serve.build_meta(config)["watch_status"]
    assert status.startswith("config_error: "), status
    assert "trialerror.toml" in status


def test_watcher_flushes_during_a_sustained_write_burst(seeded_program):
    """LU-1: the old rule published only after a quiet gap >= debounce, so a
    burst that never goes quiet (an ingest checkpointing every second -- the
    exact moment an operator is watching) produced NO event for its whole
    duration. The max-wait floor (2x debounce) must fire mid-burst."""
    program_root, platform_root, _ids = seeded_program
    config = _config_for(program_root, platform_root)
    ops_db = _watched_db(config, "ops")

    broadcaster = dashboard_serve.Broadcaster()
    q = broadcaster.subscribe()
    debounce = 0.4
    dashboard_serve._shutdown.clear()
    watcher = threading.Thread(
        target=dashboard_serve.watcher_loop,
        args=(config, broadcaster),
        kwargs={"poll_interval": 0.05, "debounce": debounce},
        daemon=True,
    )
    watcher.start()
    try:
        # writes land every 0.1s for 1.6s: never a 0.4s quiet gap anywhere
        # in the burst.
        burst_end = time.time() + 1.6
        while time.time() < burst_end:
            os.utime(ops_db, None)
            time.sleep(0.1)

        events = []
        while True:
            try:
                events.append(q.get_nowait())
            except Empty:
                break
    finally:
        dashboard_serve._shutdown.set()
        watcher.join(timeout=5)
        dashboard_serve._shutdown.clear()

    assert events, "no changed event published during a 1.6s burst with debounce=0.4"
    event_name, payload = events[0]
    assert event_name == "changed"
    assert "ops" in payload["changed_stores"]
    assert str(ops_db) in payload["changed_paths"]


@pytest.fixture()
def seeded_program_without_platform_db(tmp_path):
    """A real program (ops/knowledge/jobs all initialized and seeded) whose
    platform store is then removed -- the live misconfiguration M-LU-2 was
    reported from, where ``/dashboard/api/all`` 500'd while every single-panel
    route still answered 200, so the page rendered nothing at all and said
    "live" while doing it."""
    program_root = tmp_path / "program"
    program_root.mkdir()
    (program_root / "trialerror.toml").write_text('[program]\nid = "PROG-dash-no-platform"\n', encoding="utf-8")
    platform_root = tmp_path / "platform"

    store = open_store(program_root, platform_root=platform_root)
    ids = populate_one_of_everything(store)
    store.close()

    for leftover in platform_root.glob("platform.db*"):
        leftover.unlink()
    assert not (platform_root / "platform.db").exists()
    return program_root, platform_root, ids


def test_all_route_is_200_with_platform_db_absent(seeded_program_without_platform_db):
    """M-LU-2 / the C1 gate: one builder reaching into a store that is not
    there is that PANEL's error, never the route's. ``/all`` stays 200 and
    every other panel is intact."""
    program_root, platform_root, _ids = seeded_program_without_platform_db
    host = "127.0.0.1"
    port = _free_port()

    argv = [
        sys.executable, "-m", "trialerror.cli", "dashboard", "serve", "--foreground",
        "--host", host, "--port", str(port),
        "--program-root", str(program_root), "--platform-root", str(platform_root),
        "--poll-interval", "0.2", "--debounce", "0.3",
    ]
    log_path = program_root.parent / "dashboard_serve_no_platform.log"
    log_fh = open(log_path, "wb")
    proc = subprocess.Popen(argv, cwd=str(REPO_ROOT), stdout=log_fh, stderr=subprocess.STDOUT)
    try:
        _wait_for_server(host, port)

        with urllib.request.urlopen(f"http://{host}:{port}/dashboard/api/all", timeout=10) as resp:
            assert resp.status == 200
            all_payload = json.loads(resp.read().decode("utf-8"))

        panels = all_payload["panels"]
        # the guard, not a crash: ops.db exists, so the program is real --
        # what is missing is the platform store the session hangs off.
        assert panels["session"]["status"] == "error"
        assert "platform.db" in panels["session"]["message"]
        # platform-only panels report their own honest empty state ...
        assert panels["budget"]["status"] == "not_initialized"
        # ... and nothing else is collateral damage.
        for name in ("feed", "rooms", "gates", "corpus", "course", "lexicon", "determinations"):
            assert panels[name]["status"] == "ok", (name, panels[name])

        # the single-panel route agrees with the bundle (it used to 200 with
        # a real payload while /all 500'd on the same builder).
        with urllib.request.urlopen(f"http://{host}:{port}/dashboard/api/session", timeout=10) as resp:
            assert resp.status == 200
            session_panel = json.loads(resp.read().decode("utf-8"))
        assert session_panel["status"] == "error"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=15)
        log_fh.close()
    assert proc.poll() is not None


def test_read_only_gets_produce_zero_changed_events(seeded_program):
    """LU-7's regression, over a real server (sweep test 34).

    With an SSE subscriber attached, a burst of read-only panel GETs must
    produce ZERO ``changed`` frames -- before this fix each one bumped
    ``-shm``'s mtime, so the watcher re-fired every poll+debounce forever,
    the page refetched all twelve builders on that cadence, and every other
    dashboard on the machine was dragged along through the shared
    ``platform.db``. Then one REAL write must produce exactly one frame,
    attributed to ``ops`` -- proving the watcher is quiet, not deaf."""
    program_root, platform_root, ids = seeded_program
    host = "127.0.0.1"
    port = _free_port()
    poll, debounce = 0.2, 0.3

    argv = [
        sys.executable, "-m", "trialerror.cli", "dashboard", "serve", "--foreground",
        "--host", host, "--port", str(port),
        "--program-root", str(program_root), "--platform-root", str(platform_root),
        "--poll-interval", str(poll), "--debounce", str(debounce),
    ]
    log_path = program_root.parent / "dashboard_serve_lu7.log"
    log_fh = open(log_path, "wb")
    proc = subprocess.Popen(argv, cwd=str(REPO_ROOT), stdout=log_fh, stderr=subprocess.STDOUT)
    sse = None
    try:
        _wait_for_server(host, port)
        sse = _SseClient(host, port)
        sse.read_for(0.5)  # the hello frame
        assert b"event: hello" in sse.buf

        # ---- reads only ---------------------------------------------------
        read_start = time.time()
        for _ in range(10):
            with urllib.request.urlopen(f"http://{host}:{port}/dashboard/api/jobs", timeout=5) as resp:
                assert resp.status == 200
        for _ in range(2):
            with urllib.request.urlopen(f"http://{host}:{port}/dashboard/api/all", timeout=10) as resp:
                assert resp.status == 200
        assert time.time() - read_start < 5.0

        sse.read_for(3 * (poll + debounce))
        assert sse.changed_frames() == [], "a read-only GET produced a changed event"

        # ---- one real write -----------------------------------------------
        store = open_store(program_root, platform_root=platform_root)
        try:
            post_feed(store, thread_id=ids["thread"], body="LU-7 regression: one real write")
        finally:
            store.close()

        deadline = time.time() + 10.0
        while time.time() < deadline and not sse.changed_frames():
            sse.read_for(0.5)
        frames = sse.changed_frames()
        assert len(frames) == 1, f"expected exactly one changed event, got {len(frames)}: {frames}"
        assert "ops" in frames[0]["changed_stores"]
        assert frames[0]["changed_paths"]
        assert not [p for p in frames[0]["changed_paths"] if p.endswith("-shm")]
    finally:
        if sse is not None:
            sse.close()
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=15)
        log_fh.close()
    assert proc.poll() is not None


# =============================================================================
# W3 -- the write route's HTTP contract (sweep batch W3: WA-4, WA-5, WA-6,
# M-WA-1, M-WA-3, M-WA-4, and P-2's server half).
#
# The claim these tests make is narrow and total: NO request to a write route
# can produce anything but a JSON body with a named `status`. Before W3, four
# separate failures each produced something else -- an HTML error page, a
# misleading "no such panel", an unbounded read, and (worst) no response at
# all, because an unexpected exception escaped into http.server's own handler,
# which logs a traceback and closes the socket. Curl calls that last one
# HTTP 000; the dashboard's own `postWrite` calls it a network error and
# leaves the operator guessing.
#
# In-process (a real ThreadingHTTPServer on a real socket, but no subprocess):
# these are protocol assertions, and the subprocess loop test above already
# proves the CLI wiring. Keeping them in-process is also what makes it
# practical to monkeypatch `dispatch_write` for the one case that cannot be
# provoked honestly -- a genuine bug in the write layer.
# =============================================================================
_W3_TOKEN = "w3-test-token"


@contextlib.contextmanager
def _serving(config, *, token=_W3_TOKEN):
    handler = dashboard_serve.make_handler_class(config, dashboard_serve.Broadcaster(), token=token)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=10)


def _raw_request(host, port, method, path, *, headers=None, body=b""):
    """One request over a bare socket, returning ``(status, headers, body)``.

    ``urllib`` cannot express the two cases that matter here: a
    ``Content-Length`` that lies about the bytes actually sent (the 413 path
    must refuse BEFORE reading), and a response the server closes with no
    status line at all (the HTTP-000 regression). A socket can, and the
    assertion below turns that second shape into a test failure with a
    readable message rather than a urllib traceback."""
    sock = socket.create_connection((host, port), timeout=10)
    try:
        head = f"{method} {path} HTTP/1.1\r\nHost: {host}\r\n"
        for k, v in (headers or {}).items():
            head += f"{k}: {v}\r\n"
        head += "Connection: close\r\n\r\n"
        sock.sendall(head.encode("ascii"))
        if body:
            sock.sendall(body)
        sock.settimeout(10)
        raw = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            raw += chunk
    finally:
        sock.close()
    assert raw.startswith(b"HTTP/"), f"no status line at all (the HTTP 000 shape): {raw[:200]!r}"
    head_bytes, _, body_bytes = raw.partition(b"\r\n\r\n")
    lines = head_bytes.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    hdrs = {}
    for line in lines[1:]:
        if ":" in line:
            k, _, v = line.partition(":")
            hdrs[k.strip().lower()] = v.strip()
    return status, hdrs, body_bytes


def _json_body(body_bytes):
    return json.loads(body_bytes.decode("utf-8"))


def test_write_route_unknown_action_is_json_404_not_an_html_page(seeded_program):
    """WA-4. ``writes.dispatch`` has had an ``unknown_action`` branch since
    Stage 3; over HTTP it was unreachable, because the handler checked the
    action name itself and called ``send_error``, whose body is the
    stdlib's HTML template."""
    program_root, platform_root, _ = seeded_program
    with _serving(_config_for(program_root, platform_root)) as (host, port):
        status, hdrs, body = _raw_request(
            host, port, "POST", "/dashboard/api/write/not-a-real-action",
            headers={"Content-Type": "application/json", "Content-Length": "2",
                     dashboard_serve.WRITE_TOKEN_HEADER: _W3_TOKEN},
            body=b"{}",
        )
    assert status == 404
    assert hdrs["content-type"] == "application/json"
    payload = _json_body(body)
    assert payload["ok"] is False
    assert payload["status"] == "unknown_action"
    assert "not-a-real-action" in payload["message"]


def test_write_route_checks_the_token_before_the_action_name(seeded_program):
    """WA-5. An unauthenticated caller must not be able to tell a real
    action name from an invented one by the answer it gets."""
    program_root, platform_root, _ = seeded_program
    with _serving(_config_for(program_root, platform_root)) as (host, port):
        real, _, real_body = _raw_request(
            host, port, "POST", "/dashboard/api/write/feed-post",
            headers={"Content-Length": "2"}, body=b"{}",
        )
        invented, _, invented_body = _raw_request(
            host, port, "POST", "/dashboard/api/write/nope",
            headers={"Content-Length": "2"}, body=b"{}",
        )
    assert real == invented == 403
    assert _json_body(real_body) == _json_body(invented_body)
    assert _json_body(real_body)["status"] == "forbidden"


def test_write_route_rejects_a_near_miss_token(seeded_program):
    """M-WA-4's companion assertion: ``compare_digest`` still compares."""
    program_root, platform_root, _ = seeded_program
    with _serving(_config_for(program_root, platform_root)) as (host, port):
        status, _, body = _raw_request(
            host, port, "POST", "/dashboard/api/write/feed-post",
            headers={"Content-Length": "2", dashboard_serve.WRITE_TOKEN_HEADER: _W3_TOKEN + "x"},
            body=b"{}",
        )
    assert status == 403
    assert _json_body(body)["status"] == "forbidden"


def test_get_on_a_write_route_is_405_with_allow_post(seeded_program):
    """WA-6. It used to fall through to the generic panel branch and answer
    "no such panel: 'write/room-freeze'"."""
    program_root, platform_root, _ = seeded_program
    with _serving(_config_for(program_root, platform_root)) as (host, port):
        status, hdrs, body = _raw_request(host, port, "GET", "/dashboard/api/write/room-freeze")
    assert status == 405
    assert hdrs["allow"] == "POST"
    payload = _json_body(body)
    assert payload["status"] == "method_not_allowed"
    assert "room-freeze" in payload["message"]


def test_oversized_write_body_is_413_the_caller_can_actually_read(seeded_program):
    """M-WA-3, over a real socket. The ``Content-Length`` is a lie -- two
    bytes are sent, 1 MiB + 1 is declared -- which is the shape that made
    the first cut of this fix flaky: refusing WITHOUT draining closed a
    socket that still had inbound data pending, Windows answered with an
    RST, and the RST discarded the 413 already sitting in the client's
    receive buffer. The caller saw a dropped connection: the very shape
    this batch exists to eliminate. The drain is bounded by a short
    timeout, so the two-byte body ends this in well under a second."""
    program_root, platform_root, _ = seeded_program
    oversized = dashboard_serve.MAX_WRITE_BODY_BYTES + 1
    with _serving(_config_for(program_root, platform_root)) as (host, port):
        status, _, body = _raw_request(
            host, port, "POST", "/dashboard/api/write/feed-post",
            headers={"Content-Type": "application/json", "Content-Length": str(oversized),
                     dashboard_serve.WRITE_TOKEN_HEADER: _W3_TOKEN},
            body=b"{}",
        )
    assert status == 413
    payload = _json_body(body)
    assert payload["status"] == "bad_request"
    assert str(dashboard_serve.MAX_WRITE_BODY_BYTES) in payload["message"]


def test_an_oversized_declaration_never_sizes_a_buffer_from_it():
    """M-WA-3's actual invariant, asserted where it lives rather than
    through TCP: ``rfile.read(length)`` must never be called with a length
    the client chose. The stub's ``read`` refuses anything bigger than one
    chunk, so a single unbounded call fails the test; the bounded drain
    calls it repeatedly with 64 KiB and gets ``b""`` back."""

    class _Stub:
        close_connection = False

        def __init__(self):
            self.reads = []
            self.headers = {"Content-Length": str(dashboard_serve.MAX_WRITE_BODY_BYTES * 4096)}
            self.connection = self
            self.rfile = self

        # the socket half
        def settimeout(self, _t):
            pass

        # the file half
        def read(self, n):
            self.reads.append(n)
            assert n <= 65536, f"allocated a buffer of {n} bytes from a client-supplied length"
            return b""

    handler = dashboard_serve.make_handler_class(
        dashboard_serve.ServerConfig(repo_root=REPO_ROOT, program_root=None, platform_root=None),
        dashboard_serve.Broadcaster(), token="t",
    )
    stub = _Stub()
    # both methods run against the stub; nothing else on the handler is touched
    stub._discard_oversized_body = handler._discard_oversized_body.__get__(stub)
    body, err, status = handler._read_json_body(stub)
    assert (body, status) == ({}, 413)
    assert "accepts at most" in err
    assert stub.close_connection is True
    assert stub.reads and max(stub.reads) <= 65536


def test_a_json_object_in_a_string_field_is_a_named_refusal_not_a_dead_socket(seeded_program):
    """M-WA-1, the sweep's own repro: ``{"body": {"x": 1}}``. It reached
    sqlite as a ``dict``, raised ``ProgrammingError``, and closed the socket
    with no response at all (HTTP 000). ``_raw_request`` asserts a status
    line exists, so on the pre-fix path this test fails at the parse, before
    reaching the assertions below."""
    program_root, platform_root, ids = seeded_program
    payload_bytes = json.dumps({"thread_id": ids["thread"], "body": {"x": 1}}).encode("utf-8")
    with _serving(_config_for(program_root, platform_root)) as (host, port):
        status, hdrs, body = _raw_request(
            host, port, "POST", "/dashboard/api/write/feed-post",
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload_bytes)),
                     dashboard_serve.WRITE_TOKEN_HEADER: _W3_TOKEN},
            body=payload_bytes,
        )
    assert hdrs["content-type"] == "application/json"
    payload = _json_body(body)
    assert payload["ok"] is False
    assert payload["status"] == "bad_request"
    assert "body must be a string, got dict" in payload["message"]
    # 200 + ok:false is the dispatch envelope's own refusal shape, the same
    # one `missing_fields` has always used -- a client bug the server
    # understood, not a server fault.
    assert status == 200


def test_an_unexpected_exception_in_the_write_layer_is_a_500_json_envelope(seeded_program, monkeypatch):
    """The other half of M-WA-1. ``writes.dispatch`` deliberately lets a
    genuine bug propagate rather than disguising it as a refusal, so
    something has to catch it at the edge. A real bug cannot be provoked
    honestly through the API any more (that is what the type table above
    bought), so it is injected here."""
    program_root, platform_root, ids = seeded_program

    def _boom(*_a, **_k):
        raise RuntimeError("deliberate fixture failure inside the write layer")

    monkeypatch.setattr(dashboard_serve, "dispatch_write", _boom)
    payload_bytes = json.dumps({"thread_id": ids["thread"], "body": "hi"}).encode("utf-8")
    with _serving(_config_for(program_root, platform_root)) as (host, port):
        status, hdrs, body = _raw_request(
            host, port, "POST", "/dashboard/api/write/feed-post",
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload_bytes)),
                     dashboard_serve.WRITE_TOKEN_HEADER: _W3_TOKEN},
            body=payload_bytes,
        )
    assert status == 500
    assert hdrs["content-type"] == "application/json"
    payload = _json_body(body)
    assert payload["ok"] is False
    assert payload["status"] == "internal_error"
    assert "RuntimeError" in payload["message"]
    assert "deliberate fixture failure" in payload["message"]


def test_unknown_post_route_is_json_too(seeded_program):
    program_root, platform_root, _ = seeded_program
    with _serving(_config_for(program_root, platform_root)) as (host, port):
        status, hdrs, body = _raw_request(
            host, port, "POST", "/dashboard/api/nope", headers={"Content-Length": "0"},
        )
    assert status == 404
    assert hdrs["content-type"] == "application/json"
    assert _json_body(body)["status"] == "unknown_route"


def test_a_search_the_engine_cannot_run_answers_search_error(seeded_program, monkeypatch):
    """P-2, server half. ``do_GET`` has no try/except of its own, so before
    ``build_search``'s fence an unanswerable query (the documented unbounded
    ``mode=vector`` case) closed the socket, and the page went on showing
    the previous question's results as though they answered the new one."""
    program_root, platform_root, _ = seeded_program

    def _boom(*_a, **_k):
        raise RuntimeError("32766 variables is the ceiling")

    monkeypatch.setattr(dashboard_serve, "run_search", _boom)
    with _serving(_config_for(program_root, platform_root)) as (host, port):
        status, hdrs, body = _raw_request(host, port, "GET", "/dashboard/api/search?q=anything&mode=vector")
    assert status == 200
    assert hdrs["content-type"] == "application/json"
    payload = _json_body(body)
    assert payload["status"] == "search_error"
    assert "RuntimeError" in payload["message"]
    assert payload["results"] == []
    assert payload["mode"] == "vector"


def test_panel_query_params_feeds_every_pair_a_panel_declares(seeded_program, monkeypatch):
    """``PANEL_QUERY_PARAMS`` maps a panel to a TUPLE of (query param, builder
    keyword) pairs, not one pair, because a panel can be selected by more than
    one thing -- Evidence resolves ``claim_id`` / ``anchor_id`` / ``chunk_id``
    and decides between them itself. The route's whole job is to hand over
    every param that is present, and to treat a blank one as absent."""
    program_root, platform_root, _ids = seeded_program
    config = _config_for(program_root, platform_root)
    seen: list[dict] = []

    def _builder(_rostore, **kwargs):
        seen.append(kwargs)
        return {"status": "ok", **kwargs}

    monkeypatch.setitem(dashboard_serve.PANEL_BUILDERS, "fixture", _builder)
    monkeypatch.setitem(
        dashboard_serve.PANEL_QUERY_PARAMS,
        "fixture",
        (("claim_id", "claim_id"), ("anchor_id", "anchor_id"), ("chunk", "chunk_id")),
    )

    panel = dashboard_serve.build_one_panel(
        config, "fixture", query_params={"claim_id": ["C-1"], "chunk": ["CH-9"], "anchor_id": [""]}
    )
    assert panel == {"status": "ok", "claim_id": "C-1", "chunk_id": "CH-9"}
    # the blank one is absent, not passed as "" -- a builder must never have to
    # tell "not asked" apart from "asked for nothing".
    assert "anchor_id" not in seen[0]

    # no query string at all: the builder's own default selection stands.
    assert dashboard_serve.build_one_panel(config, "fixture") == {"status": "ok"}


def test_panel_query_params_are_all_tuples_of_pairs():
    """Guards the shape itself: the old single-pair form (``("since",
    "since")``) still unpacks in a loop, silently feeding the builder a
    keyword named ``s`` from the string's characters."""
    for name, spec in dashboard_serve.PANEL_QUERY_PARAMS.items():
        assert isinstance(spec, tuple), name
        for pair in spec:
            assert isinstance(pair, tuple) and len(pair) == 2, (name, pair)
            assert all(isinstance(part, str) for part in pair), (name, pair)
