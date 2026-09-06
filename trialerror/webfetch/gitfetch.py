"""Repositories are cloned, never scraped.

Design §2.2 ("git") and the operator's rule behind it: for a GitHub URL the
lawful, faithful and cheap route is the git protocol, not the web UI. A
rendered file page is chrome around a fragment; a clone is the thing itself,
with a commit sha that makes the provenance record reproducible.

What this module does, and deliberately does not do:

* ``git clone --depth 1 --single-branch --no-checkout --no-tags`` — one
  commit, one branch, **no working tree**. Nothing from the remote is ever
  written out as a file the sidecar might then look at; the objects stay in
  ``.git`` and the only thing that leaves is a tar stream.
* ``git archive --format=tar HEAD [-- <path>]`` — streamed to disk through a
  byte counter, refused at ``max_git_archive_bytes``. ``git archive`` output
  is deterministic for a commit, which is what lets the research side use
  its sha256 as the source's content hash.
* Hooks are pointed at an empty directory and the system/global git config
  is switched off, so nothing in the operator's environment or in a remote's
  suggestion changes how this runs. ``GIT_TERMINAL_PROMPT=0`` means a
  private or deleted repository fails in one second instead of blocking on a
  credential prompt forever.
* **No tar is opened here.** The sidecar produces ``repo.tar`` and stops; the
  research container extracts it with ``tarfile.extractall(filter='data')``
  (design §1 P1 — the process that parses hostile containers has no egress).

The URL shapes accepted are exactly the four the design names. Anything else
under a repository — an issue, a pull request, a release page, a raw blob
host — is ``git_url_shape``, because guessing what a human meant by an
unfamiliar GitHub URL is how a fetcher ends up scraping the web UI it was
built to avoid. A bare commit sha as the ref is refused for a plainer
reason: ``--depth 1 --branch <sha>`` is not a thing git can do, and silently
falling back to a full clone would turn a one-file request into a gigabyte.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import urlsplit

from trialerror.webfetch import WebFetchRefused
from trialerror.webfetch.policy import Caps
from trialerror.webfetch.urlcheck import normalize, peek_host

__all__ = [
    "LICENSE_FILENAMES",
    "GitSpec",
    "GitArchive",
    "GitFetcher",
    "parse_git_url",
]

#: Files checked, in order, for a declared licence. The first that exists
#: wins; the text is capped and handed to the research side, which decides
#: the tier — the sidecar never assigns one.
LICENSE_FILENAMES: tuple[str, ...] = (
    "LICENSE",
    "LICENSE.md",
    "LICENSE.txt",
    "LICENCE",
    "LICENSE.rst",
    "COPYING",
)

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,255}$")
_SPDX_HINTS: tuple[tuple[str, str], ...] = (
    ("apache license", "Apache-2.0"),
    ("mit license", "MIT"),
    ("permission is hereby granted, free of charge", "MIT"),
    ("bsd 3-clause", "BSD-3-Clause"),
    ("bsd 2-clause", "BSD-2-Clause"),
    ("redistribution and use in source and binary forms", "BSD"),
    ("creative commons", "CC"),
    ("mozilla public license", "MPL-2.0"),
    ("gnu general public license", "GPL"),
)
_MAX_LICENSE_BYTES = 64 * 1024
_ARCHIVE_BLOCK = 1024 * 1024


def _refuse_shape(detail: str, **context: object) -> WebFetchRefused:
    return WebFetchRefused("git_url_shape", detail, **context)


@dataclass(frozen=True)
class GitSpec:
    """A parsed repository reference."""

    host: str
    owner: str
    repo: str
    ref: str | None
    path: str | None
    url_norm: str

    @property
    def clone_url(self) -> str:
        return f"https://{self.host}/{self.owner}/{self.repo}.git"

    @property
    def slug(self) -> str:
        """``owner__repo`` — the directory-safe name the research side uses
        under ``raw/web/<host>/``."""
        return f"{self.owner}__{self.repo}"


@dataclass(frozen=True)
class GitArchive:
    """What one clone produced."""

    tar_path: Path
    size_bytes: int
    sha256: str
    head: str
    ref: str
    path: str | None
    license_name: str | None
    license_text: str | None
    license_id: str | None

    def as_result_git_block(self) -> dict:
        """The ``git`` object of ``result.json`` (design §2.3)."""
        return {"head": self.head, "ref": self.ref, "path": self.path}


def parse_git_url(url: str) -> GitSpec:
    """Parse one of the four accepted GitHub URL shapes, or refuse.

    Runs the ordinary URL shape check first, so an IP literal, a userinfo
    field or a reserved suffix is refused with its own reason before this
    module's narrower rules apply.

    **https only**, whatever flags the host carries. The ``http`` flag exists
    so a page on an internal host can be read over plain HTTP; a clone is a
    different thing, the constructed clone URL is always ``https://``, and
    fetching source over a channel anyone on the path can rewrite has no
    upside worth the sentence it would take to justify.
    """
    host = peek_host(url)
    nurl = normalize(url, keep_query=False)
    parts = [segment for segment in urlsplit(nurl.url).path.split("/") if segment]
    if len(parts) < 2:
        raise _refuse_shape(f"{url} does not name <owner>/<repo>", host=host)

    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    if not _NAME_RE.match(owner) or not _NAME_RE.match(repo):
        raise _refuse_shape(f"{owner}/{repo} is not a usable owner/repo pair", host=host)

    ref: str | None = None
    path: str | None = None
    rest = parts[2:]
    if rest:
        verb = rest[0]
        if verb not in ("tree", "blob"):
            raise _refuse_shape(
                f"only /tree/ and /blob/ URLs are clonable; {url} points at '{verb}'",
                host=host,
            )
        if len(rest) < 2:
            raise _refuse_shape(f"{url} has a /{verb}/ with no ref", host=host)
        ref = rest[1]
        if _FULL_SHA_RE.match(ref):
            # Only the unambiguous case is refused. A 40-hex ref is a commit
            # sha, and `--depth 1 --branch <sha>` is not something git can
            # do; falling back to a full clone to reach it would turn a
            # one-file request into a gigabyte. An abbreviated sha is left
            # alone deliberately — it is indistinguishable from a branch
            # whose name happens to be hex, and refusing real branches to
            # catch a rare mistake is the worse trade.
            raise _refuse_shape(
                f"{ref} is a commit sha; a shallow single-branch clone needs a branch or "
                "tag name — name the branch instead",
                host=host,
            )
        if not _REF_RE.match(ref) or ".." in ref:
            raise _refuse_shape(f"{ref!r} is not a usable branch or tag name", host=host)
        segments = rest[2:]
        if segments:
            for segment in segments:
                if segment in (".", "..") or not _PATH_SEGMENT_RE.match(segment):
                    raise _refuse_shape(f"{'/'.join(segments)!r} is not a usable path", host=host)
            path = "/".join(segments)
        elif verb == "blob":
            raise _refuse_shape(f"{url} is a /blob/ URL with no file path", host=host)

    return GitSpec(
        host=host, owner=owner, repo=repo, ref=ref, path=path, url_norm=nurl.url_norm
    )


#: The only transports ``protocol.<scheme>.allow=always`` may name. In
#: production the clone URL is always ``https`` (:func:`parse_git_url` builds
#: it); ``file`` is here only because ``_clone_url_fn`` — the test seam — hands
#: the same code path a local bare repository, and a closed set that a
#: reviewer can read is worth more than a branch that skips the whole rule
#: whenever the seam is in use.
_CLONE_SCHEMES: frozenset[str] = frozenset({"https", "file"})


def _clone_scheme(clone_url: str) -> str:
    scheme = urlsplit(clone_url).scheme.lower()
    if scheme not in _CLONE_SCHEMES:
        raise _refuse_shape(f"{scheme or '(none)'!r} is not a clonable transport")
    return scheme


class GitFetcher:
    """Clone shallowly and produce one tar, under caps.

    ``_clone_url_fn`` is the codebase's usual underscore-prefixed test seam:
    it lets a test point a clone at a local bare repository while every other
    line — the arguments, the environment, the caps, the streaming — stays
    the production path. It is not reachable from any configuration file.
    """

    def __init__(
        self,
        caps: Caps,
        *,
        work_dir: str | Path,
        git_exe: str = "git",
        _clone_url_fn: Callable[[GitSpec], str] | None = None,
    ) -> None:
        self.caps = caps
        self.work_dir = Path(work_dir)
        self.git_exe = git_exe
        self._clone_url_fn = _clone_url_fn or (lambda spec: spec.clone_url)

    # -- public ----------------------------------------------------------
    def fetch(self, spec: GitSpec, *, job_id: str) -> GitArchive:
        """Clone ``spec`` and return the tar plus its provenance."""
        workspace = self.work_dir / job_id
        self.cleanup(job_id)
        workspace.mkdir(parents=True, exist_ok=True)
        hooks = workspace / "nohooks"
        hooks.mkdir(exist_ok=True)
        repo_dir = workspace / "repo"
        tar_path = workspace / "repo.tar"

        clone_url = self._clone_url_fn(spec)
        clone = [
            self.git_exe,
            "-c",
            f"core.hooksPath={hooks}",
            "-c",
            "advice.detachedHead=false",
            # git does its own redirect following (default
            # `http.followRedirects=initial`), and nothing in this package
            # sees those hops: the sidecar's `redirect_off_allowlist` rule
            # cannot run inside a child process. So git does not follow them
            # at all — an allowlisted `git` host that answers 301 to
            # somewhere else fails the clone loudly instead of quietly
            # cloning from a host the operator never approved.
            "-c",
            "http.followRedirects=false",
            # Whatever transports this git build supports, exactly one is
            # permitted: the scheme of the URL this fetcher constructed.
            # Closes `ext::`, `file://` via a redirect, and the rest of the
            # transport surface in one rule rather than by enumeration.
            "-c",
            "protocol.allow=never",
            "-c",
            f"protocol.{_clone_scheme(clone_url)}.allow=always",
            "clone",
            "--depth",
            "1",
            "--single-branch",
            "--no-checkout",
            "--no-tags",
            "--recurse-submodules=no",
        ]
        if spec.ref:
            clone += ["--branch", spec.ref]
        clone += ["--", clone_url, str(repo_dir)]
        self._run(clone, cwd=workspace, what=f"clone {spec.owner}/{spec.repo}")

        head = self._run(
            [self.git_exe, "-C", str(repo_dir), "rev-parse", "HEAD"],
            cwd=workspace,
            what="rev-parse HEAD",
        ).strip()
        ref = spec.ref or self._current_branch(repo_dir) or "HEAD"

        size, digest = self._archive(repo_dir, tar_path, path=spec.path, host=spec.host)
        license_name, license_text = self._license(repo_dir)
        return GitArchive(
            tar_path=tar_path,
            size_bytes=size,
            sha256=digest,
            head=head,
            ref=ref,
            path=spec.path,
            license_name=license_name,
            license_text=license_text,
            license_id=detect_license_id(license_text),
        )

    def cleanup(self, job_id: str) -> None:
        """Remove a job's scratch tree. The ``/work`` volume is per-job
        scratch, never an archive (design §8: cleaned per job)."""
        _rmtree(self.work_dir / job_id)

    # -- internals -------------------------------------------------------
    def _archive(
        self, repo_dir: Path, tar_path: Path, *, path: str | None, host: str
    ) -> tuple[int, str]:
        argv = [self.git_exe, "-C", str(repo_dir), "archive", "--format=tar", "HEAD"]
        if path:
            argv += ["--", path]
        cap = self.caps.max_git_archive_bytes
        digest = hashlib.sha256()
        total = 0
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._env(),
            cwd=str(repo_dir),
        )
        assert process.stdout is not None
        try:
            with tar_path.open("wb") as out:
                while True:
                    block = process.stdout.read(_ARCHIVE_BLOCK)
                    if not block:
                        break
                    total += len(block)
                    if total > cap:
                        process.kill()
                        raise WebFetchRefused(
                            "git_too_large",
                            f"archive of {repo_dir.name} passed the {cap}-byte cap",
                            host=host,
                        )
                    digest.update(block)
                    out.write(block)
        finally:
            stderr = b""
            try:
                process.stdout.close()
            except OSError:  # pragma: no cover - defensive
                pass
            if process.stderr is not None:
                try:
                    stderr = process.stderr.read()
                    process.stderr.close()
                except OSError:  # pragma: no cover - defensive
                    pass
            try:
                process.wait(timeout=self.caps.git_timeout_s)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                process.kill()
        if process.returncode not in (0, None):
            raise WebFetchRefused(
                "git_url_shape",
                f"git archive failed ({process.returncode}): "
                f"{stderr.decode('utf-8', errors='replace')[:400]}",
                host=host,
            )
        return total, digest.hexdigest()

    def _current_branch(self, repo_dir: Path) -> str | None:
        try:
            return self._run(
                [self.git_exe, "-C", str(repo_dir), "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=repo_dir,
                what="current branch",
            ).strip()
        except WebFetchRefused:  # pragma: no cover - detached HEAD in a fixture
            return None

    def _license(self, repo_dir: Path) -> tuple[str | None, str | None]:
        for name in LICENSE_FILENAMES:
            try:
                text = self._run(
                    [self.git_exe, "-C", str(repo_dir), "show", f"HEAD:{name}"],
                    cwd=repo_dir,
                    what=f"read {name}",
                )
            except WebFetchRefused:
                continue
            return name, text[:_MAX_LICENSE_BYTES]
        return None, None

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                # No prompt can ever appear: a private or missing repository
                # must fail, not block a single-threaded sidecar forever.
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": "",
                "SSH_ASKPASS": "",
                # Nothing from the machine's own git configuration takes part.
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_ATTR_NOSYSTEM": "1",
                "GIT_LFS_SKIP_SMUDGE": "1",
            }
        )
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
            env.pop(key, None)
        return env

    def _run(self, argv: Sequence[str], *, cwd: str | Path, what: str) -> str:
        try:
            completed = subprocess.run(
                list(argv),
                cwd=str(cwd),
                env=self._env(),
                capture_output=True,
                timeout=self.caps.git_timeout_s,
                check=False,
            )
        except FileNotFoundError as exc:
            raise WebFetchRefused(
                "git_url_shape", f"git executable not found: {exc}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise WebFetchRefused(
                "timeout", f"git timed out after {self.caps.git_timeout_s}s during {what}"
            ) from exc
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()[:400]
            raise WebFetchRefused(
                "git_url_shape", f"git {what} failed ({completed.returncode}): {detail}"
            )
        return completed.stdout.decode("utf-8", errors="replace")


def detect_license_id(text: str | None) -> str | None:
    """A best-effort SPDX guess from licence text.

    A *guess*, and named as one: the research side records it as a signal
    beside the full text, and the operator's tag always wins. Nothing here
    ever grants a tier by itself.
    """
    if not text:
        return None
    lowered = text.lower()
    for needle, spdx in _SPDX_HINTS:
        if needle in lowered:
            return spdx
    return None


def _rmtree(target: Path) -> None:
    """Remove a scratch tree, tolerating Windows' read-only ``.git`` objects.

    Git marks pack and object files read-only; on Windows that makes
    ``shutil.rmtree`` fail outright, which would leave every cloned
    repository behind on the ``/work`` volume. One chmod pass fixes it
    without needing the version-dependent ``onerror``/``onexc`` callback.
    """
    if not target.exists():
        return
    shutil.rmtree(target, ignore_errors=True)
    if not target.exists():
        return
    for root, dirs, files in os.walk(target):  # pragma: no cover - Windows path
        for name in dirs + files:
            try:
                os.chmod(os.path.join(root, name), 0o700)
            except OSError:
                pass
    shutil.rmtree(target, ignore_errors=True)
