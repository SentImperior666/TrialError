"""``trialerror.util.build.build_id`` -- design Section 3.2's resolution
order: env var, then git (short sha, ``-dirty`` if the tree is dirty), then
``"unknown"``. Cached per process; every test resets the cache first.
"""

from __future__ import annotations

import subprocess

import pytest

from trialerror.util import build


@pytest.fixture(autouse=True)
def _reset_cache():
    build._reset_cache_for_tests()
    yield
    build._reset_cache_for_tests()


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture()
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("1", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def test_the_env_var_wins(monkeypatch):
    monkeypatch.setenv("TRIALERROR_BUILD", "custom-build-42")
    assert build.build_id() == "custom-build-42"


def test_the_env_var_wins_even_inside_a_real_git_repo(monkeypatch, git_repo):
    monkeypatch.setenv("TRIALERROR_BUILD", "custom-build-42")
    monkeypatch.setattr(build, "_repo_root", lambda: git_repo)
    assert build.build_id() == "custom-build-42"


def test_a_clean_repo_reports_a_bare_short_sha(monkeypatch, git_repo):
    monkeypatch.delenv("TRIALERROR_BUILD", raising=False)
    monkeypatch.setattr(build, "_repo_root", lambda: git_repo)
    result = build.build_id()
    assert not result.endswith("-dirty")
    assert len(result) == 12


def test_a_dirty_tracked_file_appends_dirty(monkeypatch, git_repo):
    monkeypatch.delenv("TRIALERROR_BUILD", raising=False)
    monkeypatch.setattr(build, "_repo_root", lambda: git_repo)
    (git_repo / "a.txt").write_text("2", encoding="utf-8")
    result = build.build_id()
    assert result.endswith("-dirty")


def test_an_untracked_file_alone_does_not_count_as_dirty(monkeypatch, git_repo):
    """``--untracked-files=no`` (design Section 3.2 verbatim): a stray new
    file must not flip a build to -dirty."""
    monkeypatch.delenv("TRIALERROR_BUILD", raising=False)
    monkeypatch.setattr(build, "_repo_root", lambda: git_repo)
    (git_repo / "untracked.txt").write_text("x", encoding="utf-8")
    result = build.build_id()
    assert not result.endswith("-dirty")


def test_outside_git_is_unknown(monkeypatch):
    monkeypatch.delenv("TRIALERROR_BUILD", raising=False)
    monkeypatch.setattr(build, "_repo_root", lambda: None)
    assert build.build_id() == "unknown"


def test_the_result_is_cached_per_process(monkeypatch, git_repo):
    monkeypatch.delenv("TRIALERROR_BUILD", raising=False)
    monkeypatch.setattr(build, "_repo_root", lambda: git_repo)
    first = build.build_id()
    (git_repo / "a.txt").write_text("changed after first call", encoding="utf-8")
    assert build.build_id() == first


def test_every_git_call_passes_no_optional_locks(monkeypatch, git_repo):
    """N-5 fix round: `git status` may otherwise take index.lock to refresh
    the index, racing a real commit against the same live checkout in a
    long-lived process."""
    monkeypatch.delenv("TRIALERROR_BUILD", raising=False)
    monkeypatch.setattr(build, "_repo_root", lambda: git_repo)
    seen_argvs = []
    real_run = subprocess.run

    def _spy(argv, **kwargs):
        seen_argvs.append(list(argv))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", _spy)
    build.build_id()
    assert seen_argvs, "no git subprocess was invoked at all"
    for argv in seen_argvs:
        assert "--no-optional-locks" in argv, argv
