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

import json
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from trialerror.artifacts.gates import open_gate, record_verdict, submit_gate
from trialerror.artifacts.registry import create_artifact
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
            "console-body-session", "console-body-doctor",               # console
        ):
            assert f'data-role="{role}"' in body, f"missing DOM hook data-role={role!r}"
        # HALIDE tokens: the page requests the two build-contract fonts and
        # links the one stylesheet a re-skin ever needs to touch
        assert "IBM+Plex+Mono" in body
        assert 'href="dashboard.css"' in body

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
        edit_row = next(e for e in json.loads(pending_row["edits"]) if e["edit_id"] == ids["edit_id"])
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
