"""Path-flexibility knobs, end to end (the import-design notes (internal, not in this export) Sec 5,
C-0067(c)(i)): a single program whose ``trialerror.toml`` relocates ALL SEVEN
``[paths]`` knobs (the six this build wires -- ``stores_dir``,
``archive_dir``, ``law_digest_path``, ``handoffs_dir``, ``requests_path``,
``memory_dir`` -- plus ``ingest_roots``, the one that already worked)
round-trips through the real CLI: boot -> ingest -> law -> handoff ->
memory. Every location is deliberately nonstandard -- some absolute and
outside ``program_root`` entirely, some program-root-relative but under a
differently-named subtree -- mirroring the "bridge an existing non-TrialError
layout" use case ``plugin/skills/import-existing-project`` targets, not
just "moved the dir next door."

Every step also asserts the OLD default-named directory (``stores/``,
``archive/``, ``law/``, ``handoffs/``, ``requests/``, ``memory/``) was
NEVER created -- proof the knob actually redirected the write, not merely
that a copy also landed at the configured location.
"""

from __future__ import annotations

import json

from trialerror.cli import main
from trialerror.events.api import append_event
from trialerror.jobs.worker import run_one
from trialerror.stores.store import open_store

from tests.test_session_helpers import seed_launch


def test_doctor_resolves_relocated_stores_dir(tmp_path, platform_root, capsys):
    """fix-doctor-config-awareness (build-v2-polish): ``trialerror doctor``'s
    ``store_schema_version``/``xid_dangling`` checks used to resolve
    ops/knowledge/jobs DB paths via the hardcoded ``"stores"`` literal
    (``trialerror.stores.paths.*_db_path(ctx.program_root)`` called with no
    ``config``), ignoring a program's own ``[paths].stores_dir`` knob even
    though ``trialerror.stores.store.open_store`` already honored it -- so
    `trialerror doctor --program-root X` reported every program-scoped DB as
    "database file not found" on a knob-relocated program, even right after
    a real ``trialerror session boot`` had migrated all three DBs. Reuses this
    module's own relocation-fixture pattern (a nonstandard, absolute,
    outside-program_root ``stores_dir``), scoped to just that one knob --
    doctor doesn't touch the other six."""
    program_root = tmp_path / "program"
    program_root.mkdir()
    ext_stores = tmp_path / "external-stores"

    trialerror_toml = "\n".join(
        [
            "[program]",
            'id = "doctor-knob-check"',
            "",
            "[paths]",
            f"stores_dir = {ext_stores.as_posix()!r}",
            "",
        ]
    )
    (program_root / "trialerror.toml").write_text(trialerror_toml, encoding="utf-8")

    argv_root = ["--program-root", str(program_root), "--platform-root", str(platform_root)]
    rc, env = _call(argv_root + ["session", "boot", "--create-account", "tester"], capsys)
    assert rc == 0 and env["ok"] is True, env
    assert (ext_stores / "ops.db").is_file()
    assert (ext_stores / "knowledge.db").is_file()
    assert (ext_stores / "jobs.db").is_file()
    assert not (program_root / "stores").exists()

    rc, env = _call(
        [
            "doctor", "--only", "store_schema_version", "--only", "xid_dangling",
            "--vendored-root", str(tmp_path / "vendored"),
            "--program-root", str(program_root), "--platform-root", str(platform_root),
        ],
        capsys,
    )
    assert rc == 0, env
    checks_by_name = {c["name"]: c for c in env["result"]["checks"]}

    schema_check = checks_by_name["store_schema_version"]
    for db_kind in ("platform", "ops", "knowledge", "jobs"):
        assert schema_check["details"][db_kind].get("status") != "skip", schema_check
        assert schema_check["details"][db_kind]["match"] is True, schema_check

    xid_check = checks_by_name["xid_dangling"]
    assert xid_check["status"] == "pass", xid_check
    assert xid_check["details"]["offenders"] == {}


def _call(argv, capsys):
    rc = main(argv)
    out = capsys.readouterr().out.strip()
    return rc, json.loads(out)


def _drain(store, max_steps=10):
    for i in range(max_steps):
        result = run_one(store, worker_id=f"knob-drain-{i}")
        if result["status"] == "idle":
            break


def test_all_seven_knobs_relocated_round_trips_boot_ingest_law_handoff_memory(
    tmp_path, platform_root, capsys
):
    program_root = tmp_path / "program"
    program_root.mkdir()
    ext_raw = tmp_path / "external-raw"  # ingest_roots: outside program_root entirely
    ext_raw.mkdir()
    ext_stores = tmp_path / "external-stores"
    ext_archive = tmp_path / "external-archive"
    ext_handoffs = tmp_path / "external-handoffs"
    ext_memory = tmp_path / "external-memory"
    # program-root-relative, but under differently-named subtrees -- proof
    # the knobs aren't secretly requiring an absolute path.
    rel_law_digest = "governance/DIGEST.md"
    rel_requests = "acquire/WANTED.md"

    trialerror_toml = "\n".join(
        [
            '[program]',
            'id = "nonstandard"',
            "",
            "[paths]",
            f"stores_dir = {ext_stores.as_posix()!r}",
            f"archive_dir = {ext_archive.as_posix()!r}",
            f'law_digest_path = "{rel_law_digest}"',
            f"handoffs_dir = {ext_handoffs.as_posix()!r}",
            f'requests_path = "{rel_requests}"',
            f"memory_dir = {ext_memory.as_posix()!r}",
            f"ingest_roots = [{ext_raw.as_posix()!r}]",
            "",
        ]
    )
    (program_root / "trialerror.toml").write_text(trialerror_toml, encoding="utf-8")

    argv_root = ["--program-root", str(program_root), "--platform-root", str(platform_root)]

    # -- 1. boot: [paths].stores_dir -----------------------------------
    rc, env = _call(argv_root + ["session", "boot", "--create-account", "tester"], capsys)
    assert rc == 0 and env["ok"] is True, env
    session_id = env["result"]["session_id"]
    account_id = env["result"]["account_id"]
    assert (ext_stores / "ops.db").is_file()
    assert (ext_stores / "knowledge.db").is_file()
    assert (ext_stores / "jobs.db").is_file()
    assert not (program_root / "stores").exists()

    # -- 2. law append: [paths].law_digest_path -------------------------
    rc, env = _call(argv_root + ["law", "append", "--summary", "nonstandard paths round trip"], capsys)
    assert rc == 0 and env["ok"] is True, env
    assert env["result"]["rendered_path"] == rel_law_digest
    assert (program_root / "governance" / "DIGEST.md").is_file()
    assert not (program_root / "law").exists()

    # -- 3. ingest a document: [paths].ingest_roots + [paths].archive_dir
    raw_file = ext_raw / "note.md"
    raw_file.write_text("# hello\n\nsome content to drive through the pipeline.\n", encoding="utf-8")

    # source.registered_by_launch is XID-validated against platform.launch
    # -- seed a real one directly, bound to the SAME account/session the
    # boot step above already opened (unlike tests/_ingest_fixtures.py's
    # own bootstrap_launch, which inserts a SECOND 'open' session -- fatal
    # here, since trialerror.sessions.lifecycle.resolve_open_session refuses
    # when more than one session is open at once). state='RECONCILED' so
    # it's terminal, not a dangling launch close would refuse on.
    store = open_store(program_root, platform_root=platform_root)
    try:
        launch_id = seed_launch(store, account_id=account_id, session_id=session_id, state="RECONCILED")
    finally:
        store.close()

    rc, env = _call(
        argv_root
        + [
            "ingest", "add-source", "--kind", "other", "--title", "Knob doc",
            "--license-tier", "open", "--acquisition-route", "web", "--launch-id", launch_id,
        ],
        capsys,
    )
    assert rc == 0 and env["ok"] is True, env
    source_id = env["result"]["source"]["source_id"]

    rc, env = _call(
        argv_root + ["ingest", "add", "--source-id", source_id, "--path", str(raw_file), "--launch-id", launch_id],
        capsys,
    )
    assert rc == 0 and env["ok"] is True, env
    doc_id = env["result"]["document"]["doc_id"]
    rel_path = env["result"]["document"]["rel_path"]
    assert rel_path == (ext_archive / f"{doc_id}.txt").as_posix()
    assert not (program_root / "archive").exists()

    # drive normalize -> chunk -> embed -> index to completion with the
    # deterministic fake backends (no [ingest.ocr]/[ingest.embed] table in
    # this trialerror.toml, no GPU needed) -- this is what actually WRITES the
    # archived text at the configured archive_dir (trialerror.ingest.handlers.
    # _finish_normalize_stage: ``store.program_root / doc["rel_path"]``,
    # which pathlib resolves straight to the absolute override).
    store = open_store(program_root, platform_root=platform_root)
    try:
        _drain(store)
        doc = store.knowledge.execute("SELECT * FROM document WHERE doc_id = ?", (doc_id,)).fetchone()
        assert dict(doc)["status"] == "indexed", dict(doc)
    finally:
        store.close()
    archived_files = list(ext_archive.glob("*.txt"))
    assert archived_files, f"expected an archived .txt under {ext_archive}"
    assert not (program_root / "archive").exists()

    # -- 4. requests-md: [paths].requests_path ---------------------------
    rc, env = _call(argv_root + ["ingest", "requests-md"], capsys)
    assert rc == 0 and env["ok"] is True, env
    assert env["result"]["path"] == str(program_root / "acquire" / "WANTED.md")
    assert (program_root / "acquire" / "WANTED.md").is_file()
    assert not (program_root / "requests").exists()

    # -- 5. session close: [paths].handoffs_dir --------------------------
    # hooks aren't running under test (test_session_cli.py's own
    # convention) -- seed the hook_alive marker directly.
    store = open_store(program_root, platform_root=platform_root)
    try:
        append_event(store, event_type="hook_alive", session_id=session_id, payload={"hook": "spawn_gate"})
    finally:
        store.close()

    course_check = {"rungs": "1", "build_vs_theory": "build", "drift_flag": False}
    rc, env = _call(argv_root + ["session", "close", "--course-check", json.dumps(course_check)], capsys)
    assert rc == 0 and env["ok"] is True, env
    handoff_filename = env["result"]["close_report"]["handoff_filename"]
    assert (ext_handoffs / handoff_filename).is_file()
    assert not (program_root / "handoffs").exists()

    # render-handoff also finds it at the configured location.
    rc, env = _call(argv_root + ["session", "render-handoff", "--session-id", session_id], capsys)
    assert rc == 0 and env["ok"] is True, env
    assert env["result"]["path"] == str(ext_handoffs / handoff_filename)

    # -- 6. memory sync-export: [paths].memory_dir -----------------------
    rc, env = _call(argv_root + ["memory", "sync-export"], capsys)
    assert rc == 0 and env["ok"] is True, env
    assert env["result"]["out_dir"] == str(ext_memory)
    assert ext_memory.is_dir()
    assert not (program_root / "memory").exists()

    # -- final: nothing landed at ANY of the seven old default locations
    for default_name in ("stores", "archive", "law", "handoffs", "requests", "memory"):
        assert not (program_root / default_name).exists(), f"unexpected default dir created: {default_name}"
