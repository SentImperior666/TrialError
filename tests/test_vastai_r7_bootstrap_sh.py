"""Live finding 4 (canary C4b, 2026-09-20): on the image
``pytorch/pytorch:2.13.0-cuda13.0-cudnn9-runtime``, reached through vast.ai's
ssh, the ``python3`` on a NON-INTERACTIVE ssh command's PATH is Debian's system
Python 3.12 -- which does not carry torch and refuses ``pip install`` under
PEP 668. The bootstrap must find the interpreter that DOES carry torch and
install DEV's hashed lock into an environment that sees it, on every image
shape; and when it cannot, it must say so in a BOUNDED way.

These run the REAL ``trialerror/vastai/remote/bootstrap.sh`` under ``sh``
against FAKE interpreters: no network, no pip, no torch, no GPU, no vast.ai,
nothing rented. Each fake records every call it was given, so the assertions
are about which interpreter ran what.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from trialerror.vastai.ocr import bootstrap_failure_text

BOOTSTRAP = Path(__file__).resolve().parents[1] / "trialerror" / "vastai" / "remote" / "bootstrap.sh"
_SH_CANDIDATES = ("sh", r"C:\Program Files\Git\usr\bin\sh.exe", "/bin/sh")


def _find_sh() -> str | None:
    for candidate in _SH_CANDIDATES:
        found = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
        if found:
            return found
    return None


SH = _find_sh()
pytestmark = pytest.mark.skipif(SH is None, reason="no POSIX sh on this machine to run the real bootstrap.sh")


def _posix(path: Path | str) -> str:
    """A path as the ``sh`` that runs the bootstrap sees it (git-bash mounts
    ``C:\\`` at ``/c``); identity on a POSIX machine."""
    text = str(path).replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", text):
        return "/" + text[0].lower() + text[2:]
    return text


#: One fake interpreter. It answers exactly the questions the bootstrap asks --
#: ``-c import torch``, ``-c <the PEP 668 marker probe>``, ``-c <version>``,
#: ``-m venv``, ``-m pip --version``, ``-m pip install``, and the report script
#: on stdin -- and records every call as ``<argv0>|<args>``.
FAKE = """#!/bin/sh
record='{record}'
echo "$0|$*" | head -n 1 >>"$record"   # one line per call: a -c program may be several
case "${{1:-}}" in
-c)
    case "$2" in
    *EXTERNALLY-MANAGED*) echo '{managed}'; exit 0 ;;
    *'import torch'*) exit {torch_rc} ;;
    *version_info*) echo '3.12.7'; exit 0 ;;
    esac
    echo "fake interpreter: an unexpected -c program" >&2
    exit 9
    ;;
-m)
    case "${{2:-}}" in
    venv)
        [ '{venv_ok}' = 'yes' ] || {{ echo "$0: No module named venv" >&2; exit 1; }}
        mkdir -p "$4/bin" || exit 1
        cp "$0" "$4/bin/python" || exit 1
        chmod +x "$4/bin/python" 2>/dev/null
        exit 0
        ;;
    pip)
        case "${{3:-}}" in
        --version) echo 'pip 24.0 from /usr/lib/python3/dist-packages/pip (python 3.12)'; exit 0 ;;
        install) {pip_body} ;;
        esac
        exit 9
        ;;
    esac
    exit 9
    ;;
-)
    cat >/dev/null
    printf 'TE-BOOTSTRAP {{"marker": "1.10.2", "cuda_available": true, "python_exe": "%s", "env_kind": "%s", "base_python": "%s", "pip_version": "%s", "venv_dir": "%s", "externally_managed": "%s", "model_cache_dir": "%s", "venv_error": "%s"}}\\n' \\
        "$0" "$3" "$4" "$5" "$6" "$7" "$2" "$8"
    exit 0
    ;;
esac
echo "fake interpreter: an unexpected call ($*)" >&2
exit 9
"""

#: pip's PEP 668 refusal, as the canary met it: long, and not what DEV needs.
ESSAY = r"""i=1
        while [ $i -le 300 ]; do
            echo "error: externally-managed-environment line $i: sure you have python3-full installed" >&2
            i=$((i + 1))
        done
        echo "note: If you believe this is a mistake, please contact your Python installation or OS distribution provider. You can override this, at the risk of breaking your Python installation or OS, by passing --break-system-packages. See /usr/share/doc/python3.12/README.venv for more information, and PEP 668 for the detailed specification, which is very long indeed and keeps going well past two hundred characters." >&2
        exit 1"""


class Image:
    """One image shape: which interpreters exist, where, and what they carry."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir(parents=True, exist_ok=True)
        self.record = root / "calls.log"
        self.record.write_text("", encoding="utf-8")
        self.lease = root / "lease"
        self.lease.mkdir(exist_ok=True)
        (self.lease / "requirements.lock").write_text(
            "marker-pdf==1.10.2 --hash=sha256:aa\nsurya-ocr==0.17.1 --hash=sha256:bb\n", encoding="utf-8"
        )
        self.candidates = ["python", "python3"]

    def add(self, name: str, *, torch: bool, managed: str = "no", venv_ok: str = "yes",
            pip_body: str = "exit 0") -> str:
        """A fake interpreter, either on the PATH (a bare name) or at a path of
        its own (``opt/conda/bin/python``, added to the candidate list)."""
        path = self.bin / name if "/" not in name else self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            FAKE.format(record=_posix(self.record), managed=managed, torch_rc=0 if torch else 1,
                        venv_ok=venv_ok, pip_body=pip_body),
            encoding="utf-8", newline="\n",
        )
        os.chmod(path, 0o755)
        if "/" in name:
            self.candidates.append(_posix(path))
        return _posix(path)

    def run(self, *args: str) -> subprocess.CompletedProcess:
        assert SH is not None
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join([str(self.bin), str(Path(SH).parent), "/usr/bin", "/bin"])
        env["TE_BOOTSTRAP_CANDIDATES"] = " ".join(self.candidates)
        return subprocess.run([SH, _posix(BOOTSTRAP), *args], capture_output=True, text=True, timeout=300, env=env)

    def install(self) -> subprocess.CompletedProcess:
        return self.run("install", _posix(self.lease / "requirements.lock"),
                        _posix(self.root / "cache"), _posix(self.root / "disk" / "venv"))

    @property
    def calls(self) -> list[str]:
        return [line for line in self.record.read_text(encoding="utf-8").splitlines() if line]

    def pip_installs(self) -> list[str]:
        return [line for line in self.calls if "|-m pip install" in line]


def _report(stdout: str) -> dict[str, str]:
    line = next(one for one in stdout.splitlines() if one.startswith("TE-BOOTSTRAP "))
    return dict(re.findall(r'"(\w+)": "([^"]*)"', line))


def test_conda_style_image_installs_into_the_interpreter_that_has_torch(tmp_path):
    """The shape that stopped canary C4b, with the image's own Python under
    /opt/conda: the PATH's python/python3 do not import torch."""
    image = Image(tmp_path)
    image.add("python", torch=False)
    image.add("python3", torch=False, managed="yes")
    conda = image.add("opt/conda/bin/python", torch=True)

    done = image.install()

    assert done.returncode == 0, done.stderr
    assert _report(done.stdout)["python_exe"] == conda
    assert image.pip_installs() == [f"{conda}|-m pip install --no-deps --require-hashes --no-input "
                                    f"--disable-pip-version-check -q -r {_posix(image.lease / 'requirements.lock')}"]
    assert not (tmp_path / "disk" / "venv").exists(), "an interpreter that is not externally managed needs no venv"


def test_venv_style_image_is_found_at_its_own_path(tmp_path):
    image = Image(tmp_path)
    image.add("python3", torch=False, managed="yes")
    venv_python = image.add("opt/venv/bin/python", torch=True)

    done = image.install()

    assert done.returncode == 0, done.stderr
    report = _report(done.stdout)
    assert report["python_exe"] == venv_python and report["env_kind"] == "direct"
    assert image.pip_installs() and all(line.startswith(venv_python + "|") for line in image.pip_installs())
    others = {line.split("|", 1)[0] for line in image.calls} - {venv_python}
    assert all(line.split("|", 1)[1].startswith("-c ") for line in image.calls
               if line.split("|", 1)[0] in others), "no other interpreter is asked to do anything but answer"



def test_the_login_paths_python_is_tried_before_the_images_own(tmp_path):
    """The candidate order of the grant: `python`, `python3`, then the paths."""
    image = Image(tmp_path)
    on_path = image.add("python", torch=True)
    image.add("python3", torch=True)
    conda = image.add("opt/conda/bin/python", torch=True)

    done = image.install()

    assert _report(done.stdout)["python_exe"] == on_path
    assert not any(line.startswith(conda + "|") for line in image.calls)


def test_externally_managed_python_installs_into_a_system_site_packages_venv(tmp_path):
    """PEP 668 is answered with a venv that SEES the image's torch, never with
    a reinstall of torch and never by overriding the refusal."""
    image = Image(tmp_path)
    image.add("python", torch=False)
    system = image.add("python3", torch=True, managed="yes", venv_ok="yes")

    done = image.install()

    assert done.returncode == 0, done.stderr
    venv_python = _posix(tmp_path / "disk" / "venv") + "/bin/python"
    report = _report(done.stdout)
    assert report["python_exe"] == venv_python and report["env_kind"] == "venv"
    assert report["base_python"] == system and report["venv_dir"] == _posix(tmp_path / "disk" / "venv")
    venv_call = next(line for line in image.calls if "|-m venv" in line)
    assert venv_call == f"{system}|-m venv --system-site-packages {_posix(tmp_path / 'disk' / 'venv')}"
    assert image.pip_installs() == [f"{venv_python}|-m pip install --no-deps --require-hashes --no-input "
                                    f"--disable-pip-version-check -q -r {_posix(image.lease / 'requirements.lock')}"]


def test_no_venv_module_falls_back_to_break_system_packages_in_the_container(tmp_path):
    """The stated fallback, and only it: still ``--no-deps --require-hashes``."""
    image = Image(tmp_path)
    system = image.add("python3", torch=True, managed="yes", venv_ok="no")

    done = image.install()

    assert done.returncode == 0, done.stderr
    report = _report(done.stdout)
    assert report["python_exe"] == system and report["env_kind"] == "user-break-system-packages"
    install = image.pip_installs()
    assert len(install) == 1 and install[0].startswith(system + "|")
    assert "--break-system-packages --user" in install[0]
    assert "--no-deps" in install[0] and "--require-hashes" in install[0]


def test_the_fallback_says_why_the_venv_was_not_built(tmp_path):
    """Live records (canary attempt 3, 2026-09-20): ``user-break-system-packages`` was a verdict with no cause
    -- the canary's own run could only GUESS at ensurepip. The report carries
    the last line venv printed, cut, and a built venv carries none."""
    image = Image(tmp_path / "no-venv")
    system = image.add("python3", torch=True, managed="yes", venv_ok="no")

    report = _report(image.install().stdout)

    assert report["env_kind"] == "user-break-system-packages"
    assert report["venv_error"] == f"{system}: No module named venv"

    built = Image(tmp_path / "venv")
    built.add("python3", torch=True, managed="yes", venv_ok="yes")
    assert _report(built.install().stdout)["venv_error"] in (None, "")


def test_a_venv_failure_reaches_a_failed_bootstraps_digest_too(tmp_path):
    """The same one line on the path DEV sees when the install then fails:
    the fail line carries it, and :func:`bootstrap_failure_text` reads it."""
    image = Image(tmp_path)
    system = image.add("python3", torch=True, managed="yes", venv_ok="no", pip_body=ESSAY)

    done = image.install()

    assert done.returncode != 0
    assert f"venv_error={system}: No module named venv" in done.stderr
    text = bootstrap_failure_text(done.stderr)
    assert text is not None and f"after venv failed: {system}: No module named venv" in text
    assert "env user-break-system-packages" in text


def test_a_venv_message_that_looks_like_fields_cannot_rewrite_the_digest():
    """V9-F04: `venv_error` is the one field on the fail line carrying the
    IMAGE's own text. A message holding a ` word=` run must not split into
    fields -- ``dict`` keeps the last pair, so a bogus ``stage=`` would rewrite
    the real one in the digest DEV prints. No shell: the line is the input."""
    line = (
        "TE-BOOTSTRAP-FAIL stage=pip install rc=1 python=/usr/bin/python3 python_version=3.12.3 "
        "pip=26.1.2 env=user-break-system-packages externally_managed=yes "
        "venv_error=Failing command: ['/v/bin/python3', '-m', 'ensurepip'] stage=nonsense pip=0\n"
        "TE-BOOTSTRAP-END\n"
    )

    text = bootstrap_failure_text(line)

    assert text is not None
    assert "stage 'pip install'" in text and "pip 26.1.2" in text
    assert "stage 'nonsense'" not in text and "pip 0" not in text
    assert ("after venv failed: Failing command: ['/v/bin/python3', '-m', 'ensurepip'] "
            "stage=nonsense pip=0") in text


def test_the_lock_is_the_only_thing_installed_and_torch_is_never_touched(tmp_path):
    for venv_ok in ("yes", "no"):
        image = Image(tmp_path / venv_ok)
        image.add("python3", torch=True, managed="yes", venv_ok=venv_ok)
        image.install()
        for line in image.pip_installs():
            args = line.split("|", 1)[1]
            assert "--no-deps" in args, args
            assert "torch" not in args, args
            assert args.count("-r ") == 1, args


def test_no_interpreter_with_torch_refuses_by_name_and_says_what_it_tried(tmp_path):
    image = Image(tmp_path)
    image.add("python", torch=False)
    image.add("python3", torch=False)
    image.add("opt/conda/bin/python", torch=False)

    done = image.install()

    assert done.returncode == 3
    assert "TE-BOOTSTRAP-FAIL stage=interpreter" in done.stderr
    assert not image.pip_installs(), "nothing may be installed when no interpreter carries torch"
    told = bootstrap_failure_text(done.stderr)
    assert told is not None and "'interpreter'" in told
    assert "does not import torch" in told


def test_a_failed_install_reports_a_bounded_digest_not_pips_essay(tmp_path):
    image = Image(tmp_path)
    image.add("python3", torch=True, managed="yes", venv_ok="no", pip_body=ESSAY)

    done = image.install()

    assert done.returncode == 1
    assert "TE-BOOTSTRAP-FAIL stage=pip install" in done.stderr
    assert "TE-BOOTSTRAP-LOG-HEAD" in done.stderr and "TE-BOOTSTRAP-END" in done.stderr
    body = done.stderr.splitlines()
    assert len(body) <= 45, f"the digest is bounded, not pip's essay: {len(body)} lines"
    digest = [line for line in body if not line.startswith("TE-BOOTSTRAP")]
    assert max(len(line) for line in digest) <= 200, "every log line is cut"
    assert len(done.stderr) < 8000, "what DEV reads back stays small whatever pip printed"
    assert "line 300" in done.stderr and "line 1:" in done.stderr, "head AND tail of the log"
    assert "line 150" not in done.stderr, "the middle of the essay is dropped"
    assert len((image.lease / "bootstrap.log").read_text(encoding="utf-8")) > 20000, "the whole essay IS on the host"

    told = bootstrap_failure_text(done.stderr)
    assert told is not None
    assert "'pip install'" in told and "pip 24.0" in told and "Python 3.12.7" in told
    assert "env user-break-system-packages" in told and "externally managed: yes" in told
    # At most the head and the tail the bootstrap is allowed: 2 x 20 lines x 200 characters.
    assert len(told) <= 2 * 20 * 200 + 400 and "log head:" in told and "log tail:" in told


def test_the_model_hash_runs_through_the_interpreter_it_is_given(tmp_path):
    """``models`` takes the interpreter the install chose: an image need not
    have ``python3`` on a non-interactive PATH at all."""
    image = Image(tmp_path)
    chosen = image.add("opt/conda/bin/python", torch=True)

    done = image.run("models", _posix(image.root / "cache"), chosen)

    assert done.returncode == 0, done.stderr
    assert [line.split("|", 1)[0] for line in image.calls] == [chosen]


def test_usage_still_refuses_an_unknown_mode(tmp_path):
    image = Image(tmp_path)
    done = image.run("rent-me-a-gpu")
    assert done.returncode == 2 and "usage: sh bootstrap.sh" in done.stderr
