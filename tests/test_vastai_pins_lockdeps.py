"""The packaged pins file (marker 1.10.2) parses, and ``trialerror vastai
lock-deps`` over it writes a lock pip can install with ``--require-hashes``:
a sha256 on every requirement, the image's CUDA stack (torch, triton,
``nvidia-*``) left out. PyPI is a fake that answers for every pin; no
network call is made.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from pathlib import Path

import pytest

from tests._vastai_fakes import network_tripwire  # noqa: F401 - fixture
from trialerror.cli import build_parser
from trialerror.cli import vastai as cli_vastai
from trialerror.vastai import envlock

URL_RE = re.compile(r"^https://pypi\.org/pypi/([^/]+)/([^/]+)/json$")


@pytest.fixture(autouse=True)
def _no_network(network_tripwire, monkeypatch, tmp_path):  # noqa: F811
    def pypi_tripwire(url, **_kw):
        raise AssertionError(f"lock-deps reached the real network: {url}")

    monkeypatch.setattr(envlock, "urllib_get", pypi_tripwire)
    monkeypatch.setattr(cli_vastai, "_out", io.StringIO())
    # L8 part F (second fix step): no --program-root here falls back to
    # find_program_root(), which must never resolve to the harness checkout.
    monkeypatch.setenv("TRIALERROR_PROGRAM_ROOT", str(tmp_path / "no_program_root_given"))


def _fake_pypi(seen: list[str]):
    """Answers every release with two wheels, each with a distinct sha256."""

    def http(url: str) -> bytes:
        seen.append(url)
        match = URL_RE.match(url)
        assert match, url
        name, version = match.groups()
        files = [{"filename": f"{name}-{version}-{i}.whl",
                  "digests": {"sha256": hashlib.sha256(f"{name}=={version}#{i}".encode()).hexdigest()}}
                 for i in range(2)]
        return json.dumps({"info": {"name": name, "version": version}, "urls": files}).encode()

    return http


def _requirements(lock_text: str) -> list[str]:
    """The lock's requirements, one logical line each (continuations joined)."""
    joined = lock_text.replace(" \\\n", " ")
    return [line.strip() for line in joined.splitlines() if line.strip() and not line.startswith("#")]


def test_the_packaged_pins_file_parses():
    assert envlock.PACKAGED_PINS.name == "marker-1.10.2.pins.txt"
    assert envlock.PACKAGED_PINS.parent.name == "remote"
    pins = envlock.parse_pins(envlock.PACKAGED_PINS.read_text(encoding="utf-8"), origin=envlock.PACKAGED_PINS.name)
    names = [envlock.canonical_name(name) for name, _version in pins]
    assert ("marker-pdf", "1.10.2") in [(envlock.canonical_name(n), v) for n, v in pins]
    assert len(names) == len(set(names)) > 10, "one pin per package"


def test_lock_deps_over_the_packaged_pins_hashes_every_requirement_and_leaves_the_cuda_stack_out(
    tmp_path, monkeypatch, network_tripwire  # noqa: F811
):
    pins = envlock.parse_pins(envlock.PACKAGED_PINS.read_text(encoding="utf-8"), origin=envlock.PACKAGED_PINS.name)
    expected = [(n, v) for n, v in pins if not envlock.is_image_supplied(n)]
    seen: list[str] = []
    monkeypatch.setattr(cli_vastai, "_lock_http", _fake_pypi(seen))
    out = tmp_path / "marker-requirements.lock"

    args = build_parser().parse_args(["vastai", "lock-deps", "--out", str(out)])  # no --pins: the packaged file
    env = args.handler(args)

    assert env["ok"] is True, env
    assert Path(env["result"]["lock"]) == out and Path(env["result"]["pins"]) == envlock.PACKAGED_PINS
    requirements = _requirements(out.read_text(encoding="utf-8"))
    assert [r.split(" ", 1)[0] for r in requirements] == [f"{n}=={v}" for n, v in expected]
    for requirement in requirements:
        assert re.search(r"--hash=sha256:[0-9a-f]{64}", requirement), requirement
        name = envlock.canonical_name(requirement.split("==", 1)[0])
        assert name not in ("torch", "triton") and not name.startswith("nvidia-"), requirement
    assert env["result"]["locked"] == len(expected) and env["result"]["hashes"] == 2 * len(expected)
    assert len(seen) == len(expected), "one PyPI read per locked pin, none for the image's stack"
    assert network_tripwire == []
