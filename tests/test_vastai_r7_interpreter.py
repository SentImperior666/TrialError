"""Live finding 4, DEV's half: the instance's interpreter is whatever the
bootstrap FOUND, so every later remote command -- the canary page, the model
hash, ``marker_single --help`` and every range -- must go through the absolute
paths it reported, never through a bare ``python3``/``marker_single`` on a
non-interactive ssh command's PATH. And when the install fails, what reaches
the worker's log is the bootstrap's bounded digest, not pip's essay.

On fakes throughout (``tests/_vastai_shell_fakes.py``): no vast.ai call, no
ssh, no GPU, no key material. The fake instance shell now refuses any command
that is not addressed to the reported interpreter, so a missed call site fails
the test rather than the canary.
"""

from __future__ import annotations

import json

import pytest

from tests._vastai_fakes import isolated_state, network_tripwire  # noqa: F401 - fixtures
from tests._vastai_shell_fakes import (  # noqa: F401 - ssh_tripwire is a fixture
    JOB,
    ledger_rows,
    make_env,
    run_env,
    ssh_tripwire,
)
from trialerror.vastai.ocr import bootstrap_failure_text

CONDA = "/opt/conda/bin/python"
CONDA_MARKER = "/opt/conda/bin/marker_single"


@pytest.fixture(autouse=True)
def _no_network_no_ssh(network_tripwire, isolated_state, ssh_tripwire):  # noqa: F811
    yield


@pytest.fixture
def state(isolated_state):  # noqa: F811
    return isolated_state


def _commands(env) -> list[str]:
    return [cmd for _iid, cmd in env.world.commands]


def _refusal(summary) -> str:
    """A host failure destroys the lease and returns the claim unrun (the
    canary's ``max_leases_per_job = 1``), so what the operator reads is the
    worker's refusal message."""
    return " ".join(str(e.get("message", "")) for e in summary.get("refused", []))


#: A bootstrap failure as the instance prints it: the bounded digest around a
#: pip essay that stays on the host.
FAIL_BLOCK = "\n".join(
    ["TE-BOOTSTRAP-FAIL stage=pip install rc=1 python=/usr/bin/python3 python_version=3.12.3 pip=24.0 "
     "env=user-break-system-packages externally_managed=yes",
     "TE-BOOTSTRAP-LOG-HEAD",
     "candidate python: not on this PATH (/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin)",
     "candidate python3 -> /usr/bin/python3: imports torch",
     "TE-BOOTSTRAP-LOG-TAIL",
     "error: externally-managed-environment",
     "hint: See PEP 668 for the detailed specification.",
     "TE-BOOTSTRAP-END"]
)


# ---------------------------------------------------------------------------
# every later remote command goes through what the bootstrap reported
# ---------------------------------------------------------------------------
def test_a_whole_job_runs_through_the_reported_interpreter_and_marker(tmp_path, state):
    """The default world is the conda-style image: nothing is on the PATH."""
    env = make_env(tmp_path, state, pages=4)
    summary = run_env(env)

    assert summary["published"] == [JOB], summary
    commands = _commands(env)
    assert any(f"{CONDA} " in c and "te_canary.py" in c for c in commands)
    assert any(c.startswith(CONDA + " ") and "te_range.py" in c for c in commands)
    assert any(c.startswith(CONDA_MARKER + " --help") for c in commands)
    assert any(c.startswith("sh ") and " models " in c and c.endswith(CONDA) for c in commands)
    assert all(not c.startswith("python3 ") and not c.startswith("marker_single ") for c in commands)
    # marker itself is run BY PATH inside the wrapper's argv, not by bare name.
    assert env.world.marker_argv and all(argv[0] == CONDA_MARKER for argv in env.world.marker_argv)
    block = json.loads((tmp_path / "offload" / "done" / JOB / "result.json").read_text(encoding="utf-8"))["vastai"]
    assert block["remote_versions"]["python_exe"] == CONDA
    assert block["remote_versions"]["env_kind"] == "direct"


def test_an_externally_managed_image_runs_through_its_venv(tmp_path, state):
    env = make_env(tmp_path, state, pages=4)
    env.world.python_exe = "/var/tmp/te/VOCR/venv/bin/python"
    env.world.marker_exe = "/var/tmp/te/VOCR/venv/bin/marker_single"
    env.world.env_kind = "venv"
    summary = run_env(env)

    assert summary["published"] == [JOB], summary
    assert all(c.split()[0] in ("mkdir", "sh", "cat", "sha256sum", "rm", "true", env.world.python_exe,
                                env.world.marker_exe) for c in _commands(env))
    rows = [r for r in ledger_rows(env.state_dir) if r["kind"] == "outcome"]
    assert rows[-1]["remote_versions"]["env_kind"] == "venv"


def test_the_install_is_given_a_venv_directory_on_the_container_disk(tmp_path, state):
    """RAM scratch is for the document (O8); a venv full of wheels is not."""
    env = make_env(tmp_path, state, pages=4)
    run_env(env)

    install = next(c for c in _commands(env) if " install " in c and "bootstrap.sh" in c)
    venv = install.split()[-1]
    assert venv.startswith("/var/tmp/te/") and venv.endswith("/venv"), install
    assert "/dev/shm" not in venv
    # and it is wiped on the way out, like the rest of the lease
    wipe = next(c for c in _commands(env) if c.startswith("rm -rf --"))
    assert venv.rsplit("/", 1)[0] in wipe


def test_the_worker_log_says_which_interpreter_the_instance_took(tmp_path, state):
    env = make_env(tmp_path, state, pages=4)
    run_env(env)

    line = next(one for one in env.world.log if "python 3.12.10" in one)
    assert CONDA in line and "direct" in line and "pip 25.2" in line and "torch 2.13.0+cu130" in line


# ---------------------------------------------------------------------------
# what the bootstrap did not report
# ---------------------------------------------------------------------------
def test_an_install_without_a_marker_script_refuses_and_ships_nothing(tmp_path, state):
    env = make_env(tmp_path, state, pages=4)
    env.world.marker_exe = None
    summary = run_env(env)

    assert [e["reason_code"] for e in summary["refused"]] == ["stack-mismatch"]
    message = summary["refused"][0]["message"]
    assert "no marker_single script came with it" in message and "The document was not uploaded." in message
    assert CONDA in message
    assert not [s for s in env.world.stdin_sent if s[2] == env.data], "the document stayed on DEV"


def test_an_install_that_reports_no_interpreter_is_a_host_failure(tmp_path, state):
    env = make_env(tmp_path, state, pages=4)
    env.world.python_exe = None
    summary = run_env(env)

    assert "did not report which interpreter" in _refusal(summary)
    assert not [s for s in env.world.stdin_sent if s[2] == env.data]


# ---------------------------------------------------------------------------
# the bounded failure text
# ---------------------------------------------------------------------------
def test_a_failed_install_reaches_the_worker_as_the_bounded_digest(tmp_path, state):
    env = make_env(tmp_path, state, pages=4)
    env.world.bootstrap_rc = 1
    env.world.bootstrap_stderr = FAIL_BLOCK + "\n"
    summary = run_env(env)

    error = _refusal(summary)
    assert "the bootstrap failed on the instance (1)" in error
    assert "stage 'pip install' with /usr/bin/python3" in error
    assert "Python 3.12.3, pip 24.0, env user-break-system-packages, externally managed: yes" in error
    assert "log head: candidate python: not on this PATH" in error
    assert "log tail: error: externally-managed-environment" in error
    assert "TE-BOOTSTRAP-LOG-HEAD" not in error, "the markers are read, not repeated"
    assert not [s for s in env.world.stdin_sent if s[2] == env.data]


def test_an_install_that_prints_no_digest_still_reaches_the_worker(tmp_path, state):
    env = make_env(tmp_path, state, pages=4)
    env.world.bootstrap_rc = 127
    env.world.bootstrap_stderr = "sh: 1: /dev/shm/te/VOCR/bootstrap.sh: not found\n"
    summary = run_env(env)

    assert "the bootstrap failed on the instance (127): sh: 1:" in _refusal(summary)


def test_bootstrap_failure_text_is_bounded_and_returns_none_without_a_block():
    assert bootstrap_failure_text("pip said something and nothing else\n") is None
    assert bootstrap_failure_text(b"") is None

    flood = "\n".join(
        ["TE-BOOTSTRAP-FAIL stage=pip install rc=1 python=/p python_version=3.12.3 pip=24.0 env=venv "
         "externally_managed=no", "TE-BOOTSTRAP-LOG-HEAD"]
        + [f"head {i} " + "x" * 500 for i in range(50)]
        + ["TE-BOOTSTRAP-LOG-TAIL"]
        + [f"tail {i} " + "y" * 500 for i in range(50)]
        + ["TE-BOOTSTRAP-END", "an unmarked line after the end that is not read"]
    )
    told = bootstrap_failure_text(flood, line_chars=200, max_lines=20)
    assert told is not None
    assert len(told) <= 2 * 20 * 200 + 400, len(told)
    assert "head 19" in told and "head 20" not in told, "at most 20 lines of each"
    assert "tail 19" in told and "tail 20" not in told
    assert "x" * 201 not in told and "y" * 201 not in told, "every line is cut"
    assert "an unmarked line after the end" not in told
