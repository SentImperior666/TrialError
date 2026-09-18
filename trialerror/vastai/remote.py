"""The channel to a rented instance, and the backend the offload worker drives.

The instance runs a byte copy of the SAME ``embed_backend.py`` the DEV
worker and the local CPU query path run (``[ingest.embed.query]
module_dir``), loaded once, behind a JSON-lines loop on stdin/stdout of one
SSH session. The SSH identity is a PATH the operator configures
(``[vastai] ssh_identity_path``); ``ssh`` opens it, TrialError never does.

UNTESTED LIVE (design section 8): the tests replace :class:`SshChannel`
with an in-memory fake.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Protocol, Sequence

__all__ = [
    "LeaseExpired",
    "REMOTE_DIR",
    "SERVE_SOURCE",
    "Channel",
    "SshChannel",
    "RemoteEmbedBackend",
]

REMOTE_DIR = "/root/te"

#: Runs ON the instance. Loads the model once; one request per line.
SERVE_SOURCE = "\n".join(
    [
        "import json, sys",
        f"sys.path.insert(0, {REMOTE_DIR!r})",
        "from embed_backend import load_backend",
        "b = load_backend(sys.argv[1])",
        "print(json.dumps({'ready': True, 'dims': int(b.dim)}), flush=True)",
        "for line in sys.stdin:",
        "    req = json.loads(line)",
        "    v = b.embed_batch(req['texts'], kind=req.get('kind', 'document'))",
        "    print(json.dumps({'vectors': v.tolist()}), flush=True)",
        "",
    ]
)


class LeaseExpired(KeyboardInterrupt):
    """The TTL watchdog destroyed the instance mid-job.

    A ``KeyboardInterrupt`` subclass ON PURPOSE: ``offload.worker._process_one``
    answers an interrupt by RETURNING the claim unrun (no offload attempt
    burned -- a sizing miss is not a GPU fault) and re-raising, which is
    exactly the right handling, and it keeps ``except Exception`` blocks from
    swallowing the expiry."""


class Channel(Protocol):
    def bootstrap(self, *, files: dict[str, bytes], pip_packages: Sequence[str]) -> None: ...
    def start(self, model_key: str) -> dict[str, Any]: ...
    def request(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def close(self) -> None: ...


class SshChannel:
    def __init__(self, *, host: str, port: int, identity_path: Path | None, known_hosts: Path, connect_timeout_s: int = 20):
        self.host = host
        self.port = int(port)
        self.identity_path = identity_path
        self.known_hosts = known_hosts
        self.connect_timeout_s = connect_timeout_s
        self._proc: subprocess.Popen | None = None

    def _base(self) -> list[str]:
        cmd = ["ssh", "-p", str(self.port), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
               "-o", f"UserKnownHostsFile={self.known_hosts}", "-o", f"ConnectTimeout={self.connect_timeout_s}",
               "-o", "ServerAliveInterval=30"]
        if self.identity_path:
            cmd += ["-i", str(self.identity_path), "-o", "IdentitiesOnly=yes"]
        return cmd + [f"root@{self.host}"]

    def _run(self, remote_cmd: str, *, data: bytes | None = None, timeout_s: float = 1800) -> None:
        res = subprocess.run(self._base() + [remote_cmd], input=data, capture_output=True, timeout=timeout_s)
        if res.returncode != 0:
            raise RuntimeError(f"remote command failed ({res.returncode}): {res.stderr[-800:].decode('utf-8', 'replace')}")

    def bootstrap(self, *, files: dict[str, bytes], pip_packages: Sequence[str]) -> None:
        self.known_hosts.parent.mkdir(parents=True, exist_ok=True)
        self._run(f"mkdir -p {REMOTE_DIR}", timeout_s=120)
        for name, data in files.items():
            self._run(f"cat > {REMOTE_DIR}/{name}", data=data, timeout_s=300)
        if pip_packages:
            quoted = " ".join("'" + p.replace("'", "") + "'" for p in pip_packages)
            self._run(f"python3 -m pip install -q {quoted}")

    def start(self, model_key: str) -> dict[str, Any]:
        self._proc = subprocess.Popen(
            self._base() + [f"cd {REMOTE_DIR} && python3 -u serve.py '{model_key}'"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        return self._read()

    def _read(self) -> dict[str, Any]:
        assert self._proc is not None and self._proc.stdout is not None
        line = self._proc.stdout.readline()
        if not line:
            raise EOFError("remote embed server closed the channel")
        return json.loads(line.decode("utf-8"))

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        self._proc.stdin.flush()
        return self._read()

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.kill()
            except OSError:  # pragma: no cover
                pass


class RemoteEmbedBackend:
    """``EmbedBackend``-shaped: what ``offload.worker._process_one`` calls
    ``embed_batch`` on. Checks every returned vector's dimensionality
    before the worker writes it; the sandbox re-verifies independently."""

    def __init__(self, channel: Channel, lease: Any, *, model_key: str, dims: int, module_sha256: str):
        self.channel = channel
        self.lease = lease
        self.model_key = model_key
        self.dims = int(dims)
        self.module_sha256 = module_sha256
        self.texts = 0
        self.bytes = 0
        self.seconds = 0.0

    def embed_batch(self, texts: Sequence[str], *, kind: str = "document") -> list[list[float]]:
        if self.lease.expired:
            raise LeaseExpired("vast.ai lease TTL reached before this batch")
        t0 = time.monotonic()
        try:
            out = self.channel.request({"texts": list(texts), "kind": kind})
        except (OSError, ValueError, EOFError, RuntimeError) as exc:
            if self.lease.expired:
                raise LeaseExpired("vast.ai lease TTL reached mid-batch; instance destroyed") from exc
            raise RuntimeError(f"remote embed failed: {exc}") from exc
        if self.lease.expired:
            raise LeaseExpired("vast.ai lease TTL reached mid-batch; instance destroyed")
        vectors = out.get("vectors")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise RuntimeError(f"remote returned {len(vectors or [])} vector(s) for {len(texts)} text(s)")
        bad = next((v for v in vectors if len(v) != self.dims), None)
        if bad is not None:
            raise RuntimeError(f"remote returned a {len(bad)}-dim vector, expected {self.dims}")
        self.seconds += time.monotonic() - t0
        self.texts += len(texts)
        self.bytes += sum(len(t.encode("utf-8")) for t in texts)
        return vectors


def module_bytes_and_sha(module_dir: Path | str) -> tuple[bytes, str]:
    data = (Path(module_dir) / "embed_backend.py").read_bytes()
    return data, hashlib.sha256(data).hexdigest()
