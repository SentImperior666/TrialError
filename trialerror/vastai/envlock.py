"""The hashed requirements lock for the rented OCR host (``trialerror vastai
lock-deps``).

The instance installs marker's Python dependencies with pip's
``--require-hashes``, so every pin needs the sha256 of the file pip will
download. ``pip freeze`` carries none. This module turns a pins file (one
``name==version`` per line, the output of ``pip freeze`` in DEV's marker
environment) into a lock that pip accepts on the instance whatever its Python
and platform: for each pin it reads PyPI's JSON for that exact release and
writes a ``--hash=sha256:`` line for EVERY file of the release (each wheel and
the sdist), so the file pip picks there is always one of them.

* torch, triton and ``nvidia-*`` are left out: the base image supplies them
  (``[vastai.ocr] image``), and pinning them here would make pip download a second
  multi-gigabyte CUDA stack.
* A line that is not ``name==version`` is refused by line number: an editable
  install, a URL, a range or an environment marker cannot be hash-locked from
  a release listing.
* The HTTP function is injectable; :func:`urllib_get` is the only function
  here that touches the network, and only the operator's command calls it.

Nothing here reads a key or a credential.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from trialerror.util.atomic import atomic_write_text
from trialerror.vastai.errors import VastError

__all__ = [
    "PACKAGED_PINS",
    "PYPI_RELEASE_URL",
    "EnvLockError",
    "LockResult",
    "canonical_name",
    "is_image_supplied",
    "parse_pins",
    "release_hashes",
    "build_lock",
    "write_lock",
    "urllib_get",
]

#: The pins file shipped with the package: DEV's marker environment (C1),
#: ``pip freeze`` for marker-pdf 1.10.2.
PACKAGED_PINS = Path(__file__).resolve().parent / "remote" / "marker-1.10.2.pins.txt"
PYPI_RELEASE_URL = "https://pypi.org/pypi/{name}/{version}/json"

#: Supplied by the image, never pinned in the lock.
IMAGE_SUPPLIED = ("torch", "triton")
IMAGE_SUPPLIED_PREFIXES = ("nvidia-",)

_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+!_-]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class EnvLockError(VastError):
    """A pins line, a PyPI answer or the output path was refused by name."""


def canonical_name(name: str) -> str:
    """PEP 503: lower case, every run of ``-``, ``_`` and ``.`` as one ``-``."""
    return re.sub(r"[-_.]+", "-", name).lower()


def is_image_supplied(name: str) -> bool:
    canon = canonical_name(name)
    return canon in IMAGE_SUPPLIED or canon.startswith(IMAGE_SUPPLIED_PREFIXES)


def parse_pins(text: str, *, origin: str = "pins") -> list[tuple[str, str]]:
    """``[(name, version), ...]`` in file order. Blank lines and ``#``
    comments are skipped; anything else that is not exactly
    ``name==version`` raises :class:`EnvLockError` naming the line."""
    pins: list[tuple[str, str]] = []
    seen: dict[str, int] = {}
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, version = line.partition("==")
        name, version = name.strip(), version.strip()
        if sep != "==" or not _NAME_RE.match(name) or not _VERSION_RE.match(version) or "==" in version:
            raise EnvLockError(
                f"{origin}:{line_no}: {line[:120]!r} is not `name==version`; an editable install, a URL, a "
                "version range or an environment marker cannot be hash-locked from a PyPI release",
                next_actions=[f"replace line {line_no} of {origin} with an exact `name==version` pin, or remove it"],
            )
        canon = canonical_name(name)
        if canon in seen:
            raise EnvLockError(
                f"{origin}:{line_no}: {name} is pinned twice (first on line {seen[canon]})",
                next_actions=[f"keep one pin for {name} in {origin}"],
            )
        seen[canon] = line_no
        pins.append((name, version))
    if not pins:
        raise EnvLockError(f"{origin} has no `name==version` pins", next_actions=["pass --pins <pip freeze output>"])
    return pins


def urllib_get(url: str, *, timeout_s: float = 30.0) -> bytes:
    """GET ``url`` (the one network call of this module)."""
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "trialerror-vastai-lock-deps"}
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310 - https PyPI only
        return response.read()


def release_hashes(name: str, version: str, *, http: Callable[[str], bytes]) -> list[str]:
    """The sha256 of every file of PyPI's release ``name==version``, sorted.
    Raises :class:`EnvLockError` when PyPI cannot be read, answers for
    another release, or lists no file with a sha256."""
    url = PYPI_RELEASE_URL.format(name=canonical_name(name), version=version)
    try:
        raw = http(url)
        data = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
    except EnvLockError:
        raise
    except Exception as exc:  # noqa: BLE001 - named in the refusal
        raise EnvLockError(
            f"PyPI has no readable release {name}=={version} ({type(exc).__name__}: {exc})",
            next_actions=["check the pin against PyPI", "or retry when PyPI is reachable"],
        ) from None
    info = data.get("info") if isinstance(data, dict) else None
    files = data.get("urls") if isinstance(data, dict) else None
    if not isinstance(info, dict) or not isinstance(files, list):
        raise EnvLockError(f"PyPI's answer for {name}=={version} is not a release listing",
                           next_actions=["retry later"])
    if canonical_name(str(info.get("name") or "")) != canonical_name(name) or str(info.get("version")) != version:
        raise EnvLockError(
            f"PyPI answered for {info.get('name')}=={info.get('version')} when {name}=={version} was asked",
            next_actions=["check the pin's spelling"],
        )
    hashes = sorted(
        {
            str((f.get("digests") or {}).get("sha256") or "").lower()
            for f in files
            if isinstance(f, dict) and _SHA256_RE.match(str((f.get("digests") or {}).get("sha256") or "").lower())
        }
    )
    if not hashes:
        raise EnvLockError(
            f"PyPI lists no file with a sha256 for {name}=={version}; pip's --require-hashes cannot install it",
            next_actions=[f"remove {name} from the pins if the image supplies it, or pin a release PyPI hosts"],
        )
    return hashes


@dataclass(frozen=True)
class LockResult:
    text: str
    locked: tuple[tuple[str, str], ...]
    dropped: tuple[str, ...]
    hashes: int
    origin: str
    generated: str = field(default="")


def build_lock(
    pins: Iterable[tuple[str, str]],
    *,
    http: Callable[[str], bytes],
    origin: str = "pins",
    now: datetime | None = None,
) -> LockResult:
    """The lock text for ``pins``, image-supplied packages dropped."""
    generated = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0).isoformat()
    locked: list[tuple[str, str]] = []
    dropped: list[str] = []
    blocks: list[str] = []
    total = 0
    for name, version in pins:
        if is_image_supplied(name):
            dropped.append(f"{name}=={version}")
            continue
        hashes = release_hashes(name, version, http=http)
        total += len(hashes)
        locked.append((name, version))
        blocks.append(" \\\n".join([f"{name}=={version}"] + [f"    --hash=sha256:{h}" for h in hashes]))
    if not locked:
        raise EnvLockError(f"{origin}: every pin is supplied by the image; there is nothing to lock",
                           next_actions=["pass the pip freeze of DEV's marker environment as --pins"])
    header = [
        f"# Generated by `trialerror vastai lock-deps` from {origin} at {generated}.",
        "# Install with: pip install --require-hashes -r <this file>",
        "# torch, triton and nvidia-* are supplied by the image and are not pinned here: "
        + (", ".join(dropped) if dropped else "none were in the pins"),
        "",
    ]
    return LockResult(
        text="\n".join(header + blocks) + "\n",
        locked=tuple(locked),
        dropped=tuple(dropped),
        hashes=total,
        origin=origin,
        generated=generated,
    )


def write_lock(path: Path | str, result: LockResult) -> Path:
    target = Path(path)
    if target.exists() and target.is_dir():
        raise EnvLockError(f"the lock's path {target} is a directory", next_actions=["pass --out <file>"])
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, result.text)
    return target
