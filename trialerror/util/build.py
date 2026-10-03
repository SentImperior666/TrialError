"""``build_id()`` -- a build identity for THIS install of ``trialerror``, so a
probe result, an answer stamp, or ``trialerror --version`` can tell one build
of the harness apart from another (design Section 3.2). The design's own
finding: ``trialerror.__version__`` is a static ``"0.1.0"`` -- "there is no
build id or git sha, so the version cannot tell builds apart."

Resolution order, design Section 3.2 verbatim:

1. ``TRIALERROR_BUILD``, if set -- an explicit override (a packaged/frozen
   install with no ``.git`` at all, or CI naming its own build).
2. Otherwise ``git -C <repo root of the trialerror package> rev-parse
   --short=12 HEAD``, plus ``-dirty`` when ``git status --porcelain
   --untracked-files=no`` is not empty.
3. Otherwise ``"unknown"``.

Cached per process (design: "cached per process") -- a build identity does
not change while this interpreter is alive, and shelling out to ``git`` on
every probe run / every search result's stamp would be wasteful.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

__all__ = ["build_id"]

_ENV_VAR = "TRIALERROR_BUILD"
_UNKNOWN = "unknown"
_GIT_TIMEOUT_S = 5.0

_cache: str | None = None


def _repo_root() -> Path | None:
    """Walk up from this package's own installed location looking for a
    ``.git`` entry (a directory for an ordinary checkout, a file for a
    worktree -- ``git -C`` accepts either)."""
    here = Path(__file__).resolve()
    for candidate in (here.parent, *here.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _git(repo_root: Path, *args: str) -> str | None:
    """N-5 fix round: ``--no-optional-locks`` -- ``git status`` may otherwise
    take ``index.lock`` to refresh the index, and this can run from a
    long-lived process (an MCP server, ``trialerror --version``) against the
    SAME live checkout a real commit is racing. Without this flag the two
    can collide ("index.lock exists"); with it, ``git status`` reports
    against a possibly-stale index rather than blocking or failing."""
    try:
        proc = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(repo_root), *args],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _compute() -> str:
    import os

    override = os.environ.get(_ENV_VAR)
    if override:
        return override

    repo_root = _repo_root()
    if repo_root is None:
        return _UNKNOWN

    sha = _git(repo_root, "rev-parse", "--short=12", "HEAD")
    if not sha:
        return _UNKNOWN

    porcelain = _git(repo_root, "status", "--porcelain", "--untracked-files=no")
    dirty = bool(porcelain)
    return f"{sha}-dirty" if dirty else sha


def build_id() -> str:
    """This process's build identity. Cached after the first call -- use
    :func:`_reset_cache_for_tests` to force a recompute (env-var override
    tests, git-repo fixture tests)."""
    global _cache
    if _cache is None:
        _cache = _compute()
    return _cache


def _reset_cache_for_tests() -> None:
    """Test-only: clear the per-process cache."""
    global _cache
    _cache = None
