"""The vast.ai OCR backend's parts, one at a time, on fakes: the hardened ssh
argv, the ported reachability wait, the routing through ``ConfigDevBackends``
(construction, ``describe()``, ``validate()`` with its start-up reap), the
package data shipped to the instance, and the small pure helpers.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._vastai_fakes import isolated_state, network_tripwire  # noqa: F401 - fixtures
from tests._vastai_shell_fakes import ssh_tripwire  # noqa: F401 - fixture
from tests._vastai_shell_fakes import JOB, make_env, run_env
from trialerror.offload.worker import ConfigDevBackends
from trialerror.vastai import ocr as ocr_mod
from trialerror.vastai import shell as sh
from trialerror.vastai.errors import VastConfigError
from trialerror.vastai.ocr import VastaiMarkerOcrBackend, canary_similarity, canary_text, parse_models_manifest

REMOTE = Path(ocr_mod.__file__).resolve().parent / "remote"


@pytest.fixture(autouse=True)
def _no_network_no_ssh(network_tripwire, isolated_state, ssh_tripwire):  # noqa: F811
    yield


# ---------------------------------------------------------------------------
# ssh: the hardened argv and the ported reachability wait
# ---------------------------------------------------------------------------
def _shell(tmp_path) -> sh.SshShell:
    return sh.SshShell(host="ssh4.example", port=41234, identity_path=tmp_path / "keys" / "vastai_ed25519",
                       known_hosts=sh.known_hosts_path(tmp_path / "state", "VOCR-0123456789abcdef"))


def test_the_ssh_argv_carries_every_hardening_option_and_only_the_vastai_identity(tmp_path):
    shell = _shell(tmp_path)
    argv = shell.argv("true")
    assert argv[:3] == ["ssh", "-F", "none", ][:3] and argv[1:3] == ["-F", "none"]
    options = [argv[i + 1] for i, a in enumerate(argv) if a == "-o"]
    for option in ("IdentitiesOnly=yes", "IdentityAgent=none", "ForwardAgent=no", "ClearAllForwardings=yes",
                   "ForwardX11=no", "PermitLocalCommand=no", "BatchMode=yes", "StrictHostKeyChecking=accept-new"):
        assert option in options, option
    known = [o for o in options if o.startswith("UserKnownHostsFile=")]
    assert known == [f"UserKnownHostsFile={tmp_path / 'state' / 'known_hosts' / 'VOCR-0123456789abcdef'}"]
    # Exactly one identity, and it is the vast.ai key pair's private half: with
    # -F none, IdentitiesOnly and no agent, nothing else can be offered.
    identities = [argv[i + 1] for i, a in enumerate(argv) if a == "-i"]
    assert identities == [str(tmp_path / "keys" / "vastai_ed25519")]
    assert not any("id_rsa" in a or "id_ed25519" in a or "te-offload" in a or "queue" in a.lower() for a in argv)
    assert "-A" not in argv and "-R" not in argv and "-L" not in argv and "-D" not in argv
    assert argv[-2:] == ["root@ssh4.example", "true"]


def test_a_host_that_is_not_a_plain_name_is_refused(tmp_path):
    with pytest.raises(ValueError, match="plain host name"):
        sh.SshShell(host="-oProxyCommand=evil", port=22, identity_path=tmp_path / "k", known_hosts=tmp_path / "kh")


def test_the_ssh_shell_runs_ssh_through_subprocess_with_the_command_last(tmp_path, monkeypatch):
    seen = []

    def fake_run(argv, **kwargs):
        seen.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=b"ok", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    rc, out, _err = _shell(tmp_path).run("cat -- /dev/shm/te/VOCR-0123456789abcdef/input.pdf", timeout_s=5)
    assert (rc, out) == (0, b"ok")
    argv, kwargs = seen[0]
    assert argv[-1] == "cat -- /dev/shm/te/VOCR-0123456789abcdef/input.pdf" and kwargs["timeout"] == 5


class _ScriptedShell:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def run(self, cmd, *, stdin_bytes=None, timeout_s):
        self.calls += 1
        rc, err = self.results.pop(0) if self.results else (0, "")
        return rc, b"", err.encode()


def test_ssh_refusals_are_retried_until_the_port_accepts():
    shell, checks, sleeps = _ScriptedShell([(255, "Connection refused")] * 2), [], []
    sh.wait_reachable(shell, check=lambda: checks.append(1), sleep=sleeps.append, timeout_s=600, clock=lambda: 0.0)
    assert shell.calls == 3 and len(sleeps) == 2 and len(checks) == 3


def test_a_rejected_identity_fails_at_once():
    shell = _ScriptedShell([(255, "root@host: Permission denied (publickey).")])
    with pytest.raises(sh.IdentityRejected, match="registered with the vast.ai account"):
        sh.wait_reachable(shell, check=lambda: None, sleep=lambda s: None, timeout_s=600, clock=lambda: 0.0)
    assert shell.calls == 1


def test_ssh_readiness_gives_up_at_its_budget():
    t = [0.0]
    shell = _ScriptedShell([(255, "Connection refused")] * 100)
    with pytest.raises(sh.ConnectionLost, match="not reachable after 60 s"):
        sh.wait_reachable(shell, check=lambda: None, sleep=lambda s: t.__setitem__(0, t[0] + s), timeout_s=60,
                          clock=lambda: t[0])


def test_the_real_ssh_tripwire_catches_a_real_ssh(tmp_path, ssh_tripwire):  # noqa: F811
    with pytest.raises(sh.ShellError, match="ssh tripwire"):
        subprocess.Popen(["ssh", "-V"])
    ssh_tripwire.clear()  # recorded, and cleared so this test itself passes


def test_a_rejected_identity_at_lease_time_is_key_missing_and_stops_vastai(tmp_path, isolated_state):  # noqa: F811
    env = make_env(tmp_path, isolated_state, jobs=(JOB, "JOB-vocr-2"))
    env.world.reach_failures = [(255, "root@10.0.0.1: Permission denied (publickey).")]
    summary = run_env(env)
    assert [e["reason_code"] for e in summary["refused"]] == ["key-missing", "vastai-disabled-for-run"]
    assert env.world.vast.instances == {}


# ---------------------------------------------------------------------------
# routing through the worker's own config reader
# ---------------------------------------------------------------------------
def _wired(env, monkeypatch):
    for key, value in env.overrides.items():
        monkeypatch.setitem(ocr_mod.FACTORY_OVERRIDES, key, value)
    return ConfigDevBackends({**env.toml, "ingest": {**env.toml["ingest"], "embed": {
        "backend": "stub-real-embed", "python_exe": sys.executable, "module_dir": str(env.tmp), "model_key": "stub", "dims": 8,
    }}}, root=env.root)


def test_executor_vastai_builds_the_vastai_backend_and_never_exposes_a_remote_marker_path(tmp_path, isolated_state, monkeypatch):  # noqa: F811
    env = make_env(tmp_path, isolated_state)
    backends = _wired(env, monkeypatch)
    backend = backends.ocr()
    assert type(backend) is VastaiMarkerOcrBackend and not backend.marker_single_exe
    described = backends.describe()["stages"]["ocr"]
    assert described["executor"] == "vastai" and described["constructed"] is True
    assert "marker_single_exe" not in described["paths"]
    files = described["vastai"]["files"]
    assert files["[vastai] api_key_path"]["exists"] is True and files["[vastai] ssh_identity_path"]["exists"] is True
    assert files["[vastai] approval_path"]["exists"] is True
    assert "test-key-not-a-real-secret" not in repr(described), "existence only, never contents"
    assert env.world.vast.calls == [], "describe() makes no vast.ai call"


def test_validate_checks_the_runtime_files_then_reaps_once(tmp_path, isolated_state, monkeypatch):  # noqa: F811
    env = make_env(tmp_path, isolated_state)
    backends = _wired(env, monkeypatch)
    backends.validate()
    assert env.world.vast.verbs("GET"), "the start-up reap listed the account"
    (env.root / "keys" / "vastai_ed25519").unlink()
    with pytest.raises(VastConfigError, match="ssh_identity_path"):
        backends.validate()
    described = backends.describe()["stages"]["ocr"]
    assert described["constructed"] is False and "ssh_identity_path" in described["error"]


def test_a_start_up_reap_that_cannot_list_the_account_is_loud_and_the_worker_goes_on(tmp_path, isolated_state, monkeypatch):  # noqa: F811
    env = make_env(tmp_path, isolated_state)
    env.world.vast.list_failures = 100
    backends = _wired(env, monkeypatch)
    backends.validate()
    assert any(line.startswith("!!! vast.ai reap at worker start could not list the account") for line in env.world.log)


def test_the_executor_needs_the_marker_backend_and_refuses_path_arguments(tmp_path, isolated_state):  # noqa: F811
    env = make_env(tmp_path, isolated_state)
    toml = {**env.toml, "ingest": {"ocr": {**env.toml["ingest"]["ocr"], "backend": "fake"}}}
    with pytest.raises(VastConfigError, match="backend must be \"marker\""):
        VastaiMarkerOcrBackend.from_toml(toml, config_root=env.root, **env.overrides)
    toml = {**env.toml, "ingest": {"ocr": {**env.toml["ingest"]["ocr"], "marker_extra_args": ["--config_json", "C:/x.json"]}}}
    with pytest.raises(VastConfigError, match="looks like a path"):
        VastaiMarkerOcrBackend.from_toml(toml, config_root=env.root, **env.overrides)


def test_run_without_admission_sends_nothing(tmp_path, isolated_state):  # noqa: F811
    env = make_env(tmp_path, isolated_state)
    with pytest.raises(Exception, match="without an admitted job"):
        env.backend.run(input_path=env.pdf, work_dir=tmp_path / "w")
    assert env.world.vast.calls == []


# ---------------------------------------------------------------------------
# package data and helpers
# ---------------------------------------------------------------------------
def test_the_packaged_model_manifest_is_c1s_cache_sorted_by_path():
    text = (REMOTE / "marker-models-1.10.2.sha256").read_text(encoding="utf-8")
    entries = parse_models_manifest(text)
    assert len(entries) == 48 and list(entries) == sorted(entries)
    assert all(len(d) == 64 for d in entries.values())
    assert {p.split("/")[0] for p in entries} == {
        "layout", "ocr_error_detection", "table_recognition", "text_detection", "text_recognition"
    }


def test_the_pins_file_is_the_freeze_without_torch_and_says_the_image_supplies_it():
    lines = (REMOTE / "marker-1.10.2.pins.txt").read_text(encoding="utf-8").splitlines()
    pins = [l for l in lines if l and not l.startswith("#")]
    header = " ".join(l for l in lines if l.startswith("#"))
    assert "marker-pdf==1.10.2" in pins and "surya-ocr==0.17.1" in pins and len(pins) == 78
    assert not any(p.lower().startswith("torch==") for p in pins)
    assert "torch 2.13.0+cu130" in header and "CUDA 13.0" in header


def test_the_remote_scripts_compile_and_carry_no_identifying_or_local_text():
    for name in ("te_range.py", "te_canary.py"):
        compile((REMOTE / name).read_text(encoding="utf-8"), name, "exec")
    for name in ocr_mod.TOOL_FILES:
        data = (REMOTE / name).read_bytes()
        assert b"\r\n" not in data
        for needle in (b"C:\\", b"LOCALAPPDATA", b"job_id", b"doc_id"):
            assert needle not in data, (name, needle)
    assert "--require-hashes" in (REMOTE / "bootstrap.sh").read_text(encoding="utf-8")
    assert "MODEL_CACHE_DIR" in (REMOTE / "bootstrap.sh").read_text(encoding="utf-8")


def test_canary_similarity_reads_through_markdown_and_rejects_other_text():
    text = canary_text()
    lines = text.splitlines()
    good = "{0}------------------------------------------------\n\n# " + lines[0] + "\n\n" + "\n\n".join(lines[1:])
    assert canary_similarity(text, good) == 1.0
    assert canary_similarity(text, "lorem ipsum dolor sit amet") < 0.5
    assert canary_similarity(text, "") == 0.0


def test_a_remote_path_uses_only_the_lease_id():
    assert sh.lease_dir("VOCR-0123456789abcdef") == "/dev/shm/te/VOCR-0123456789abcdef"
    with pytest.raises(ValueError):
        sh.lease_dir("JOB-secret")


def test_a_vastai_table_with_the_local_executor_makes_no_vastai_call(tmp_path, isolated_state, monkeypatch):  # noqa: F811
    env = make_env(tmp_path, isolated_state)
    for key, value in env.overrides.items():
        monkeypatch.setitem(ocr_mod.FACTORY_OVERRIDES, key, value)
    toml = {**env.toml, "ingest": {
        "ocr": {**env.toml["ingest"]["ocr"], "executor": "local", "marker_single_exe": "dev-marker"},
        "embed": {"backend": "stub-real-embed", "python_exe": sys.executable, "module_dir": str(env.tmp),
                  "model_key": "stub", "dims": 8},
    }}
    backends = ConfigDevBackends(toml, root=env.root)
    backends.validate()
    described = backends.describe()["stages"]["ocr"]
    assert type(backends.ocr()).__name__ == "RealMarkerOcrBackend" and described["executor"] == "local"
    assert env.world.vast.calls == [] and env.world.commands == []


def test_the_remote_wrapper_runs_its_child_reports_one_status_line_and_keeps_only_the_markdown(tmp_path, monkeypatch, capsys):
    """te_range.py for real, with a small Python child standing in for
    marker_single (``resource`` is POSIX-only, so it is stubbed here)."""
    import importlib.util
    import json as _json
    import types

    fake_resource = types.SimpleNamespace(RUSAGE_CHILDREN=-1, getrusage=lambda _who: types.SimpleNamespace(ru_maxrss=2048))
    monkeypatch.setitem(sys.modules, "resource", fake_resource)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)  # no __pycache__ inside the package data
    spec = importlib.util.spec_from_file_location("te_range_under_test", REMOTE / "te_range.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out = tmp_path / "out" / "r000000-000002"
    child = (
        "import os, sys; d = os.path.join(sys.argv[1], 'input'); os.makedirs(d, exist_ok=True); "
        "open(os.path.join(d, 'input.md'), 'w').write('{0}---' + chr(10) + 'body' + chr(10)); "
        "open(os.path.join(d, 'picture.jpeg'), 'wb').write(b'x' * 10); sys.stderr.write('done')"
    )
    rc = module.main(["--timeout", "60", "--out", str(out), "--stem", "input", "--model-cache", str(tmp_path / "m"),
                      "--", sys.executable, "-c", child, str(out)])
    assert rc == 0
    line = [l for l in capsys.readouterr().out.splitlines() if l.startswith("TE-RANGE ")][-1]
    status = _json.loads(line[len("TE-RANGE "):])
    md = out / "input" / "input.md"
    assert status["rc"] == 0, status["stderr_tail"]
    assert status["timed_out"] is False and status["stderr_tail"] == "done"
    assert status["ru_maxrss_bytes"] == 2048 * 1024
    assert status["md_path"] == str(md) and status["md_sha256"] == __import__("hashlib").sha256(md.read_bytes()).hexdigest()
    assert not (out / "input" / "picture.jpeg").exists(), "only the markdown is kept"
