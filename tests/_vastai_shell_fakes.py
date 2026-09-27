"""Fakes for the vast.ai OCR backend's remote side. No network, no ssh, no GPU.

* :class:`HostFakeVast` -- L1's ``FakeVast`` (subclassed, never edited) whose
  instances each get their own ssh port, so a shell can tell them apart, and
  which can make an instance die (``actual_status = "exited"``).
* :class:`FakeInstanceShell` -- stands in for ``trialerror.vastai.shell.SshShell``.
  An in-memory remote filesystem that interprets EXACTLY the commands the
  backend sends (``true``, ``mkdir -p --``, ``cat > P && sha256sum -- P``,
  ``cat --``, ``sh bootstrap.sh install|models``, ``python3 te_canary.py``,
  ``python3 te_range.py ... -- marker_single ...``, ``marker_single --help``,
  ``rm -rf --``) and refuses anything else, so a command the design does not
  name fails the test. Inside it a fake ``marker_single`` writes ``{N}``-
  paginated markdown per range.
* :class:`World` -- one test's fakes and knobs (the behaviours of design
  section 14), and everything that was sent anywhere, for the no-identifier
  assertion.
* ``ssh_tripwire`` -- fails a test that starts a REAL ``ssh``/``scp`` process.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Callable

import pytest

from tests._vastai_fakes import FakeClock, FakeVast
from trialerror.vastai import shell as sh

PACKAGED_MODELS = Path(__file__).resolve().parents[1] / "trialerror" / "vastai" / "remote" / "marker-models-1.10.2.sha256"
HELP_WITH_FLAG = "Usage: marker_single [OPTIONS] FPATH\n  --page_range TEXT  Page range to convert\n  --output_dir PATH\n"
HELP_WITHOUT_FLAG = "Usage: marker_single [OPTIONS] FPATH\n  --page_ranges TEXT  (renamed)\n  --output_dir PATH\n"


class HostFakeVast(FakeVast):
    """``FakeVast`` with a distinct ssh port per instance."""

    PORT_BASE = 20000

    def _new_instance(self, offer_id: int, req: dict[str, Any]) -> int:
        iid = super()._new_instance(offer_id, req)
        self.instances[iid]["ssh_port"] = self.PORT_BASE + iid
        return iid

    def kill(self, iid: int) -> None:
        if iid in self.instances:
            self.instances[iid]["actual_status"] = "exited"


class World:
    """One test's fakes, knobs and records. Knobs (attributes):

    ``total_pages``; ``numbering`` (``absolute``/``relative``/``one_based``) and
    ``numbering_by_range`` (``{"3-5": "relative"}``); ``garbage`` (no markers);
    ``overlap_on`` (a range flag that also emits its predecessor's first page);
    ``marker_rc`` (``{flag: (rc, stderr)}``); ``hang_on`` (flags the wrapper
    reports as timed out); ``die_on`` (flags, consumed per occurrence: the
    instance dies mid-range); ``expire_on`` (a flag: the clock jumps past every
    deadline mid-range); ``upload_flips`` / ``download_flips`` (how many
    document uploads / markdown downloads arrive corrupted); ``marker_version``;
    ``model_overrides`` (``{path: sha}``) and ``models_missing``; ``canary_ok``;
    ``canary_renders``; ``shm_avail`` (bytes, or a list consumed per lease);
    ``help_has_flag``; ``cuda_available``; ``reach_failures`` (``[(rc, stderr)]``
    for the first reachability probes); ``on_range`` (``callback(first, last)``);
    the image's shape as the bootstrap reports it (live finding 4) --
    ``python_exe``, ``marker_exe`` (``None``: the console script was not found),
    ``env_kind``, ``venv_error``, ``pip_version`` -- and ``bootstrap_stderr`` (what a failed
    install prints, with ``bootstrap_rc``).
    """

    def __init__(self, *, total_pages: int, clock: FakeClock | None = None, offers: list | None = None):
        self.vast = HostFakeVast(offers)
        self.clock = clock or FakeClock()
        self.total_pages = total_pages
        self.numbering = "absolute"
        self.numbering_by_range: dict[str, str] = {}
        self.garbage = False
        self.overlap_on: str | None = None
        self.marker_rc: dict[str, tuple[int, str]] = {}
        self.hang_on: set[str] = set()
        self.die_on: list[str] = []
        self.expire_on: str | None = None
        self.upload_flips = 0
        self.download_flips = 0
        self.marker_version = "1.10.2"
        self.model_overrides: dict[str, str] = {}
        self.models_missing: set[str] = set()
        self.canary_ok = True
        self.canary_renders = True
        self.shm_avail: int | list[int] | None = 8_000_000_000
        self.help_has_flag = True
        self.cuda_available = True
        self.reach_failures: list[tuple[int, str]] = []
        self.on_range: Callable[[int, int], Any] | None = None
        # The image's shape, as the bootstrap reports it (live finding 4). The
        # default is the conda-style image: nothing runs by bare name.
        self.python_exe: str | None = "/opt/conda/bin/python"
        self.marker_exe: str | None = "/opt/conda/bin/marker_single"
        self.env_kind = "direct"
        #: Why the venv was not built, when one was tried and failed (live
        #: records, canary attempt 3): the bootstrap's own last line of it,
        #: ``None`` otherwise.
        self.venv_error: str | None = None
        self.pip_version = "25.2"
        self.bootstrap_rc = 0
        self.bootstrap_stderr = ""
        # records
        self.log: list[str] = []
        self.commands: list[tuple[int, str]] = []
        self.stdin_sent: list[tuple[int, str, bytes]] = []
        self.ranges_run: list[tuple[int, str]] = []
        self.marker_argv: list[list[str]] = []
        self.shells: dict[int, "FakeInstanceShell"] = {}
        self.shell_args: list[dict[str, Any]] = []
        self._shm_by_iid: dict[int, int | None] = {}

    # -- the backend's shell_factory -----------------------------------------
    def shell_factory(self, *, host: str, port: int, identity_path: Path, known_hosts: Path) -> "FakeInstanceShell":
        iid = int(port) - HostFakeVast.PORT_BASE
        assert iid in self.vast.instances, f"a shell for an instance that does not exist ({host}:{port})"
        self.shell_args.append({"host": host, "port": port, "identity_path": identity_path, "known_hosts": known_hosts})
        shell = FakeInstanceShell(self, iid)
        self.shells[iid] = shell
        return shell

    def shm_for(self, iid: int) -> int | None:
        """``None`` is a host whose ``os.statvfs("/dev/shm")`` raised: the real
        bootstrap reports ``null`` for it, and DEV must not invent a number."""
        if iid not in self._shm_by_iid:
            value = self.shm_avail
            if isinstance(value, list):
                value = value.pop(0) if len(value) > 1 else value[0]
            self._shm_by_iid[iid] = None if value is None else int(value)
        return self._shm_by_iid[iid]

    # -- what left DEV, for the no-identifier assertion ------------------------
    def everything_sent(self, *, document: bytes | None = None) -> str:
        """Every API body, label, onstart, remote command and remote path, and
        every stdin EXCEPT the document's own bytes, as one string."""
        parts: list[str] = [json.dumps(b, sort_keys=True, default=str) for b in self.vast.bodies]
        parts += [cmd for _iid, cmd in self.commands]
        for _iid, path, data in self.stdin_sent:
            parts.append(path)
            if document is None or data != document:
                parts.append(data.decode("utf-8", "replace"))
        return "\n".join(parts)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _flip(data: bytes) -> bytes:
    if not data:
        return b"\x00"
    return bytes([data[0] ^ 0x01]) + data[1:]


class FakeInstanceShell:
    """One instance's shell. See the module docstring."""

    def __init__(self, world: World, iid: int):
        self.world = world
        self.iid = iid
        self.fs: dict[str, bytes] = {}
        self.dirs: set[str] = set()
        self.closed = False

    # -- the RemoteShell protocol ---------------------------------------------
    def wait_reachable(self, *, check, sleep, timeout_s, interval_s=10.0, clock=None) -> None:
        sh.wait_reachable(self, check=check, sleep=sleep, timeout_s=timeout_s, interval_s=interval_s,
                          clock=clock or self.world.clock)

    def close(self) -> None:
        self.closed = True

    def run(self, cmd: str, *, stdin_bytes: bytes | None = None, timeout_s: float) -> tuple[int, bytes, bytes]:
        self.world.commands.append((self.iid, cmd))
        inst = self.world.vast.instances.get(self.iid)
        if inst is None or inst.get("actual_status") != "running":
            return 255, b"", b"ssh: connect to host 10.0.0.1: Connection refused"
        if cmd == "true" and self.world.reach_failures:
            rc, err = self.world.reach_failures.pop(0)
            return rc, b"", err.encode()
        tokens = shlex.split(cmd)
        subcommands: list[list[str]] = [[]]
        for token in tokens:
            if token == "&&":
                subcommands.append([])
            else:
                subcommands[-1].append(token)
        out = b""
        for index, sub in enumerate(subcommands):
            rc, sub_out, err = self._one(sub, stdin_bytes if index == 0 else None)
            out += sub_out
            if rc != 0:
                return rc, out, err
        return 0, out, b""

    # -- the interpreter ------------------------------------------------------
    def _one(self, t: list[str], stdin: bytes | None) -> tuple[int, bytes, bytes]:
        w = self.world
        if t == ["true"]:
            return 0, b"", b""
        if t[:3] == ["mkdir", "-p", "--"] and len(t) >= 4:
            self.dirs.update(t[3:])
            return 0, b"", b""
        if len(t) == 3 and t[:2] == ["cat", ">"]:
            data = stdin or b""
            w.stdin_sent.append((self.iid, t[2], data))
            if t[2].rsplit("/", 1)[-1].startswith("input.") and w.upload_flips > 0:
                w.upload_flips -= 1
                data = _flip(data)
            self.fs[t[2]] = data
            return 0, b"", b""
        if len(t) == 3 and t[:2] == ["sha256sum", "--"]:
            if t[2] not in self.fs:
                return 1, b"", b"sha256sum: no such file"
            return 0, f"{_sha(self.fs[t[2]])}  {t[2]}\n".encode(), b""
        if len(t) == 3 and t[:2] == ["cat", "--"]:
            if t[2] not in self.fs:
                return 1, b"", b"cat: no such file"
            data = self.fs[t[2]]
            if t[2].endswith(".md") and "/canary/" not in t[2] and w.download_flips > 0:
                w.download_flips -= 1
                data = _flip(data)
            return 0, data, b""
        if t[:3] == ["rm", "-rf", "--"]:
            for prefix in t[3:]:
                for path in [p for p in self.fs if p == prefix or p.startswith(prefix + "/")]:
                    del self.fs[path]
            return 0, b"", b""
        if len(t) >= 3 and t[0] == "sh" and t[1].endswith("/bootstrap.sh"):
            return self._bootstrap(t)
        if len(t) == 4 and t[0] == w.python_exe and t[1].endswith("/te_canary.py"):
            if t[1] not in self.fs or t[2] not in self.fs:
                return 1, b"", b"python: can't open file"
            if not w.canary_renders:
                return 1, b"", b"ModuleNotFoundError: No module named 'PIL'"
            self.fs[t[3]] = b"%PDF-1.4 canary image page"
            return 0, b"", b""
        if len(t) >= 2 and t[0] == w.python_exe and t[1].endswith("/te_range.py"):
            return self._wrapper(t)
        if t == [w.marker_exe, "--help"]:
            return 0, (HELP_WITH_FLAG if w.help_has_flag else HELP_WITHOUT_FLAG).encode(), b""
        raise AssertionError(f"FakeInstanceShell: a command the design does not name: {t!r}")

    def _bootstrap(self, t: list[str]) -> tuple[int, bytes, bytes]:
        w = self.world
        if t[1] not in self.fs:
            return 2, b"", b"sh: bootstrap.sh: not found"
        if t[2] == "install" and len(t) == 6:
            if t[3] not in self.fs:
                return 1, b"", b"ERROR: could not open requirements file"
            if w.bootstrap_rc:
                return w.bootstrap_rc, b"", w.bootstrap_stderr.encode()
            self.dirs.add(t[5])  # the venv the install may have built, on the container disk
            report = {
                "marker": w.marker_version, "surya": "0.17.1", "torch": "2.13.0+cu130", "torch_cuda": "13.0",
                "cuda_available": w.cuda_available, "gpu": "NVIDIA GeForce RTX 3090", "driver": "580.00",
                "python": "3.12.10", "model_cache_dir": t[4], "shm_size_bytes": w.shm_for(self.iid),
                "shm_avail_bytes": w.shm_for(self.iid), "disk_avail_bytes": 20_000_000_000,
                "python_exe": w.python_exe, "marker_exe": w.marker_exe, "env_kind": w.env_kind,
                "venv_error": w.venv_error,
                "base_python": "/usr/bin/python3", "pip_version": w.pip_version,
                "externally_managed": "yes" if w.env_kind != "direct" else "no",
                "venv_dir": t[5] if w.env_kind == "venv" else None,
            }
            return 0, ("pip: ok\nTE-BOOTSTRAP " + json.dumps(report) + "\n").encode(), b""
        if t[2] == "models" and len(t) == 5:
            assert t[4] == w.python_exe, f"the model hash runs through the chosen interpreter, not {t[4]!r}"
            lines = []
            for line in PACKAGED_MODELS.read_text(encoding="utf-8").splitlines():
                digest, rel = line.split("  ", 1)
                if rel in w.models_missing:
                    continue
                lines.append(f"{w.model_overrides.get(rel, digest)}  {rel}")
            return 0, ("\n".join(lines) + "\n").encode(), b""
        return 2, b"", b"usage"

    def _wrapper(self, t: list[str]) -> tuple[int, bytes, bytes]:
        w = self.world
        if t[1] not in self.fs:
            return 2, b"", b"python: can't open file te_range.py"
        sep = t.index("--")
        opts = dict(zip(t[2:sep:2], t[3:sep:2]))
        argv = t[sep + 1:]
        assert set(opts) == {"--timeout", "--out", "--stem", "--model-cache"}, opts
        assert argv[0] == self.world.marker_exe and argv[2:7] == [
            "--paginate_output", "--output_dir", opts["--out"], "--disable_tqdm", "--disable_multiprocessing"
        ], argv
        w.marker_argv.append(list(argv))
        input_path, out, stem = argv[1], opts["--out"], opts["--stem"]
        if input_path not in self.fs:
            return 0, self._status(1, "FileNotFoundError: " + input_path, None), b""
        canary = "/canary/" in input_path
        if "--page_range" in argv:
            flag = argv[argv.index("--page_range") + 1]
            first, last = (int(x) for x in flag.split("-"))
        else:
            flag, first, last = "all", 0, (0 if canary else w.total_pages - 1)
        if not canary:
            w.ranges_run.append((self.iid, flag))
            if w.on_range is not None:
                w.on_range(first, last)
            if flag in w.die_on:
                w.die_on.remove(flag)
                w.vast.kill(self.iid)
                return 255, b"", b"Connection to 10.0.0.1 closed by remote host."
            if w.expire_on == flag:
                w.expire_on = None
                w.clock.advance(10_000_000)
                return 255, b"", b"Connection to 10.0.0.1 closed by remote host."
            w.clock.advance(60 + 8 * (last - first + 1))
            if flag in w.hang_on:
                return 0, self._status(None, "killed after timeout", None, timed_out=True), b""
            if flag in w.marker_rc:
                rc, err = w.marker_rc[flag]
                return 0, self._status(rc, err, None), b""
        text = self._canary_markdown() if canary else self._markdown(flag, first, last)
        md = f"{out}/{stem}/{stem}.md"
        self.fs[md] = text.encode("utf-8")
        return 0, self._status(0, "", md), b""

    def _status(self, rc: int | None, stderr: str, md: str | None, *, timed_out: bool = False) -> bytes:
        status = {
            "rc": rc, "timed_out": timed_out, "stderr_tail": stderr, "ru_maxrss_bytes": 21_000_000_000,
            "wall_s": 42.0, "md_path": md,
            "md_sha256": _sha(self.fs[md]) if md else None, "md_bytes": len(self.fs[md]) if md else None,
        }
        return ("TE-RANGE " + json.dumps(status) + "\n").encode()

    def _canary_markdown(self) -> str:
        from trialerror.vastai.ocr import canary_text

        if not self.world.canary_ok:
            return "{0}------------------------------------------------\n\nlorem ipsum dolor sit amet\n"
        lines = canary_text().splitlines()
        return "{0}------------------------------------------------\n\n# " + lines[0] + "\n\n" + "\n\n".join(lines[1:]) + "\n"

    def _markdown(self, flag: str, first: int, last: int) -> str:
        w = self.world
        if w.garbage:
            return "@@@@ unreadable scan noise without any page marker @@@@\n"
        numbering = w.numbering_by_range.get(flag, w.numbering)
        pages = list(range(first, last + 1))
        if w.overlap_on == flag and first > 0:
            pages = [first - 1] + pages
        blocks = []
        for page in pages:
            n = {"absolute": page, "relative": page - first, "one_based": page - first + 1}[numbering]
            blocks.append(f"{{{n}}}------------------------------------------------\n\nbody of page {page}\n\n")
        return "".join(blocks)


class _TripwirePopen(subprocess.Popen):
    """``subprocess.Popen`` that refuses to start ssh/scp/sftp."""

    started: list[list[str]] = []

    def __init__(self, args, *a, **kw):  # noqa: D401
        argv = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
        exe = os.path.basename(str(argv[0])).lower() if argv else ""
        if exe in ("ssh", "ssh.exe", "scp", "scp.exe", "sftp", "sftp.exe"):
            _TripwirePopen.started.append([str(a) for a in argv])
            raise sh.ShellError(f"ssh tripwire: a test started a real {exe} process: {argv[:3]}")
        super().__init__(args, *a, **kw)


@pytest.fixture
def ssh_tripwire(monkeypatch):
    """Fail the test if anything starts a real ssh/scp/sftp process."""
    _TripwirePopen.started = []
    monkeypatch.setattr(subprocess, "Popen", _TripwirePopen)
    yield _TripwirePopen.started
    if _TripwirePopen.started:
        pytest.fail(f"ssh tripwire: {len(_TripwirePopen.started)} real ssh process(es): {_TripwirePopen.started}")


# ---------------------------------------------------------------------------
# one test's DEV root, queue and backend
# ---------------------------------------------------------------------------
JOB = "JOB-vocr-1"
DOC = "DOC-vocr-1"
#: What the queue names the input in these tests: a title that must never
#: reach the instance (it is uploaded as ``input.pdf``).
TITLE_NAME = "Confidential_Book_Title.pdf"


class RecordingTransport:
    """``LocalTransport`` that records every verb (to prove "refused before
    the pull")."""

    def __init__(self, root: Any, *, worker_id: str = "dev"):
        from trialerror.offload.transport import LocalTransport

        self.inner = LocalTransport(root, worker_id=worker_id)
        self.verbs: list[tuple[str, str]] = []

    def list_jobs(self) -> list[str]:
        return self.inner.list_jobs()

    def claim(self, job_id: str) -> dict:
        self.verbs.append(("claim", job_id))
        return self.inner.claim(job_id)

    def pull(self, job_id: str) -> bytes:
        self.verbs.append(("pull", job_id))
        return self.inner.pull(job_id)

    def push(self, job_id: str, data: bytes) -> None:
        self.verbs.append(("push", job_id))
        self.inner.push(job_id, data)

    def publish(self, job_id: str) -> None:
        self.verbs.append(("publish", job_id))
        self.inner.publish(job_id)

    def return_job(self, job_id: str) -> None:
        self.verbs.append(("return", job_id))
        self.inner.return_job(job_id)

    def heartbeat(self, job_id: str, *, progress: bytes | None = None) -> str:
        return self.inner.heartbeat(job_id, progress=progress)


def make_env(
    tmp_path: Path,
    state_dir: Path,
    *,
    pages: int = 10,
    egress: dict[str, Any] | None = None,
    vastai: dict[str, Any] | None = None,
    ocr: dict[str, Any] | None = None,
    ingest_ocr: dict[str, Any] | None = None,
    approve: bool = True,
    tier: str | None = "unknown",
    page_count: int | None = None,
    input_name: str = TITLE_NAME,
    jobs: tuple[str, ...] = (JOB,),
    offers: list | None = None,
) -> Any:
    """A DEV backend-config-root (fake key, identity PATH, lock), an approved
    egress policy naming the document's sha256 (unless ``egress`` says
    otherwise), a queue holding ``jobs`` for one ``pages``-page A4 PDF, and a
    ``VastaiMarkerOcrBackend`` wired to a :class:`World`."""
    from types import SimpleNamespace

    from tests._ocr_range_fixtures import A4_PT, make_pdf
    from tests._vastai_fakes import FAKE_KEY, dev_toml, write_key
    from trialerror.offload import protocol
    from trialerror.vastai.api import VastClient
    from trialerror.vastai.config import load_vast_config
    from trialerror.vastai.egress import sign_egress_approval, write_egress_approval
    from trialerror.vastai.ocr import VastaiMarkerOcrBackend

    root = tmp_path / "devroot"
    write_key(root / "keys")
    (root / "keys" / "vastai_ed25519").write_text("placeholder: never opened by TrialError\n", encoding="utf-8")
    (root / "marker-requirements.lock").write_text("marker-pdf==1.10.2 --hash=sha256:" + "0" * 64 + "\n", encoding="utf-8")
    pdf = make_pdf(tmp_path / "doc.pdf", [A4_PT] * pages)
    data = pdf.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    toml = dev_toml(
        egress={"allow_documents": [sha]} if egress is None else egress,
        ocr={"max_range_pages": 3, **(ocr or {})},
        vastai=vastai,
        ingest_ocr=ingest_ocr,
    )
    world = World(total_pages=pages, offers=offers)
    cfg = load_vast_config(toml, config_root=root)
    if approve:
        write_egress_approval(cfg, sign_egress_approval(cfg, now=world.clock.now(), key_reader=lambda _p: FAKE_KEY))
    queue = protocol.ensure_layout(tmp_path / "offload")
    for job_id in jobs:
        expect = {"stage": "ocr", "backend": "marker", "outputs": ["pages.json"], "input_name": input_name,
                  "page_count": pages if page_count is None else page_count}
        if tier is not None:
            expect["license_tier"] = tier
        protocol.queue_marker(queue, job_id=job_id, stage="ocr", doc_id=DOC, expect=expect, config_hash="cfg",
                              inputs=[(input_name, data)])
    overrides = dict(
        client=VastClient(cfg.api_key_path, http=world.vast.http), shell_factory=world.shell_factory,
        state_dir=state_dir, clock=world.clock, sleep=world.clock.sleep, watchdog=False, log=world.log.append,
    )
    backend = VastaiMarkerOcrBackend.from_toml(toml, config_root=root, **overrides)
    return SimpleNamespace(root=root, toml=toml, cfg=cfg, world=world, queue=queue, backend=backend, pdf=pdf,
                           data=data, sha=sha, state_dir=state_dir, tmp=tmp_path, overrides=overrides)


def ledger_rows(state_dir: Path) -> list[dict[str, Any]]:
    from trialerror.vastai.ledger import Ledger

    return Ledger(state_dir).read().rows


def run_env(env: Any, *, transport: Any = None, backends: Any = None, **kwargs: Any) -> dict[str, Any]:
    from tests._offload_fixtures import StubDevBackends
    from trialerror.offload.worker import run_worker

    return run_worker(
        transport=transport or RecordingTransport(env.queue),
        backends=backends or StubDevBackends(ocr=env.backend),
        work_root=env.tmp / "work",
        heartbeat_interval_s=0.02,
        **{"log": env.world.log.append, **kwargs},
    )
