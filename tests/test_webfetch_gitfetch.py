"""Repositories are cloned, never scraped.

The clone half of this file runs against a **real local repository** built by
the fixture below and reached over ``file://``: a real ``git clone --depth 1
--no-checkout`` and a real ``git archive``, so the flags, the environment and
the streaming size cap are exercised rather than described. The only seam is
``_clone_url_fn``, which points the clone at that repository instead of at
``github.com``; everything else is the production path.

The parsing half needs no git at all — it is the rule that only four URL
shapes are clonable, and that everything else under a repository (an issue,
a pull request, a release page) is refused rather than guessed at, because
guessing is how a fetcher ends up scraping the web UI it exists to avoid.
"""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from trialerror.webfetch import WebFetchRefused
from trialerror.webfetch.gitfetch import (
    LICENSE_FILENAMES,
    GitFetcher,
    detect_license_id,
    parse_git_url,
)
from trialerror.webfetch.policy import Caps

MIT_TEXT = (
    "MIT License\n\nCopyright (c) 2026 Someone\n\n"
    "Permission is hereby granted, free of charge, to any person obtaining a copy...\n"
)

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")


# --------------------------------------------------------------------------
# URL shapes
# --------------------------------------------------------------------------


def test_a_bare_repo_url_parses() -> None:
    spec = parse_git_url("https://github.com/stanfordnlp/dspy")
    assert (spec.host, spec.owner, spec.repo) == ("github.com", "stanfordnlp", "dspy")
    assert spec.ref is None and spec.path is None
    assert spec.clone_url == "https://github.com/stanfordnlp/dspy.git"
    assert spec.slug == "stanfordnlp__dspy"


def test_a_dot_git_suffix_is_not_part_of_the_name() -> None:
    assert parse_git_url("https://github.com/o/r.git").repo == "r"


def test_a_tree_url_narrows_to_a_subtree() -> None:
    spec = parse_git_url("https://github.com/o/r/tree/main/docs/guide")
    assert (spec.ref, spec.path) == ("main", "docs/guide")


def test_a_tree_url_without_a_path_is_just_a_branch() -> None:
    spec = parse_git_url("https://github.com/o/r/tree/release-2")
    assert (spec.ref, spec.path) == ("release-2", None)


def test_a_blob_url_narrows_to_one_file() -> None:
    spec = parse_git_url("https://github.com/o/r/blob/main/README.md")
    assert (spec.ref, spec.path) == ("main", "README.md")


@pytest.mark.parametrize(
    "url,why",
    [
        ("https://github.com/o/r/issues/12", "an issue is not a tree"),
        ("https://github.com/o/r/pull/3", "a pull request is not a tree"),
        ("https://github.com/o/r/releases/latest", "a release page is not a tree"),
        ("https://github.com/o/r/actions", "a UI page is not a tree"),
        ("https://github.com/o", "no repository named"),
        ("https://github.com/", "nothing named at all"),
        ("https://github.com/o/r/tree", "a /tree/ with no ref"),
        ("https://github.com/o/r/blob/main", "a /blob/ with no file path"),
        ("https://github.com/-bad/r", "an owner starting with a hyphen"),
        ("https://github.com/o/r/tree/main/../../etc", "a path that climbs"),
        ("https://github.com/o/r/tree/a..b", "a ref with a double dot"),
        ("https://github.com/o/r/tree/main/a%2Fb", "an encoded separator is not a path segment"),
        ("https://github.com/o/r/tree/main/do%00cs", "a NUL byte in a path segment"),
    ],
)
def test_unclonable_shapes_are_refused(url: str, why: str) -> None:
    with pytest.raises(WebFetchRefused) as caught:
        parse_git_url(url)
    assert caught.value.reason == "git_url_shape", why


def test_a_bare_commit_sha_is_refused() -> None:
    """``--depth 1 --branch <sha>`` is not something git can do, and falling
    back to a full clone to reach it would turn a one-file request into a
    gigabyte."""
    with pytest.raises(WebFetchRefused) as caught:
        parse_git_url("https://github.com/o/r/tree/" + "a1b2c3d4" * 5)
    assert caught.value.reason == "git_url_shape"
    assert "commit sha" in caught.value.detail


def test_a_short_hexish_branch_name_is_left_alone() -> None:
    """Deliberately: an abbreviated sha is indistinguishable from a branch
    whose name happens to be hex, and refusing real branches to catch a rare
    mistake is the worse trade."""
    assert parse_git_url("https://github.com/o/r/tree/deadbee").ref == "deadbee"


def test_the_ordinary_url_rules_still_apply_first() -> None:
    with pytest.raises(WebFetchRefused) as caught:
        parse_git_url("https://127.0.0.1/o/r")
    assert caught.value.reason == "ip_literal"


def test_the_query_string_is_dropped_from_a_clone_target() -> None:
    spec = parse_git_url("https://github.com/o/r?utm_source=news")
    assert spec.url_norm == "https://github.com/o/r"


# --------------------------------------------------------------------------
# licence detection
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        (MIT_TEXT, "MIT"),
        ("Apache License\nVersion 2.0", "Apache-2.0"),
        ("BSD 3-Clause License", "BSD-3-Clause"),
        ("Creative Commons Attribution 4.0", "CC"),
        ("Mozilla Public License Version 2.0", "MPL-2.0"),
        ("GNU General Public License v3", "GPL"),
        ("All rights reserved.", None),
        (None, None),
        ("", None),
    ],
)
def test_licence_detection_is_a_labelled_guess(text: str | None, expected: str | None) -> None:
    assert detect_license_id(text) == expected


def test_the_licence_filenames_cover_the_usual_spellings() -> None:
    assert "LICENSE" in LICENSE_FILENAMES and "COPYING" in LICENSE_FILENAMES


# --------------------------------------------------------------------------
# cloning a real local repository
# --------------------------------------------------------------------------


def build_repo(root: Path, *, extra_branch: str | None = None) -> Path:
    """A small real repository, committed on ``main``."""
    repo = root / "origin"
    repo.mkdir(parents=True)
    (repo / "README.md").write_text("# The project\n", encoding="utf-8")
    (repo / "LICENSE").write_text(MIT_TEXT, encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")

    def run(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=str(repo), check=True, capture_output=True, timeout=60
        )

    run("init", "-b", "main")
    run("config", "user.email", "fixture@example.invalid")
    run("config", "user.name", "Fixture")
    run("config", "commit.gpgsign", "false")
    run("add", "-A")
    run("commit", "-m", "initial")
    if extra_branch:
        run("checkout", "-b", extra_branch)
        (repo / "docs" / "extra.md").write_text("# Extra\n", encoding="utf-8")
        run("add", "-A")
        run("commit", "-m", "extra")
        run("checkout", "main")
    return repo


def fetcher_for(repo: Path, work: Path, caps: Caps | None = None) -> GitFetcher:
    return GitFetcher(
        caps or Caps(),
        work_dir=work,
        _clone_url_fn=lambda _spec: repo.as_uri(),
    )


def tar_names(path: Path) -> set[str]:
    with tarfile.open(path) as archive:
        return {member.name for member in archive.getmembers() if member.isfile()}


@requires_git
def test_a_clone_produces_a_tar_with_its_commit(tmp_path: Path) -> None:
    repo = build_repo(tmp_path)
    spec = parse_git_url("https://github.com/o/r")
    archive = fetcher_for(repo, tmp_path / "work").fetch(spec, job_id="JOB-1")

    assert archive.tar_path.is_file()
    assert tar_names(archive.tar_path) == {
        "README.md",
        "LICENSE",
        "docs/guide.md",
        "src/app.py",
    }
    assert len(archive.head) == 40
    assert archive.ref == "main"
    assert archive.size_bytes == archive.tar_path.stat().st_size
    assert len(archive.sha256) == 64


@requires_git
def test_no_working_tree_is_ever_written(tmp_path: Path) -> None:
    """``--no-checkout``: nothing from the remote lands as a file the sidecar
    might then look at. The objects stay in ``.git``; only the tar leaves."""
    repo = build_repo(tmp_path)
    work = tmp_path / "work"
    fetcher_for(repo, work).fetch(parse_git_url("https://github.com/o/r"), job_id="JOB-1")
    checkout = work / "JOB-1" / "repo"
    assert sorted(p.name for p in checkout.iterdir()) == [".git"]


@requires_git
def test_the_archive_hash_is_stable_for_one_commit(tmp_path: Path) -> None:
    """``git archive`` output is deterministic per commit, which is what lets
    the research side use its sha256 as the source's content hash."""
    repo = build_repo(tmp_path)
    spec = parse_git_url("https://github.com/o/r")
    fetcher = fetcher_for(repo, tmp_path / "work")
    first = fetcher.fetch(spec, job_id="JOB-1")
    first_sha, first_head = first.sha256, first.head
    second = fetcher.fetch(spec, job_id="JOB-2")
    assert (second.sha256, second.head) == (first_sha, first_head)


@requires_git
def test_the_licence_text_comes_back_with_a_guess(tmp_path: Path) -> None:
    repo = build_repo(tmp_path)
    archive = fetcher_for(repo, tmp_path / "work").fetch(
        parse_git_url("https://github.com/o/r"), job_id="JOB-1"
    )
    assert archive.license_name == "LICENSE"
    assert archive.license_text is not None and "MIT License" in archive.license_text
    assert archive.license_id == "MIT"


@requires_git
def test_a_repository_with_no_licence_says_so(tmp_path: Path) -> None:
    repo = build_repo(tmp_path)
    subprocess.run(
        ["git", "rm", "-q", "LICENSE"], cwd=str(repo), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "drop licence"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    archive = fetcher_for(repo, tmp_path / "work").fetch(
        parse_git_url("https://github.com/o/r"), job_id="JOB-1"
    )
    assert (archive.license_name, archive.license_text, archive.license_id) == (None, None, None)


@requires_git
def test_a_tree_url_archives_only_that_subtree(tmp_path: Path) -> None:
    repo = build_repo(tmp_path)
    spec = parse_git_url("https://github.com/o/r/tree/main/docs")
    archive = fetcher_for(repo, tmp_path / "work").fetch(spec, job_id="JOB-1")
    assert tar_names(archive.tar_path) == {"docs/guide.md"}
    assert archive.path == "docs"


@requires_git
def test_a_blob_url_archives_only_that_file(tmp_path: Path) -> None:
    repo = build_repo(tmp_path)
    spec = parse_git_url("https://github.com/o/r/blob/main/README.md")
    archive = fetcher_for(repo, tmp_path / "work").fetch(spec, job_id="JOB-1")
    assert tar_names(archive.tar_path) == {"README.md"}


@requires_git
def test_a_named_branch_is_the_one_cloned(tmp_path: Path) -> None:
    repo = build_repo(tmp_path, extra_branch="release-2")
    spec = parse_git_url("https://github.com/o/r/tree/release-2")
    archive = fetcher_for(repo, tmp_path / "work").fetch(spec, job_id="JOB-1")
    assert archive.ref == "release-2"
    assert "docs/extra.md" in tar_names(archive.tar_path)


@requires_git
def test_a_branch_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    repo = build_repo(tmp_path)
    spec = parse_git_url("https://github.com/o/r/tree/no-such-branch")
    with pytest.raises(WebFetchRefused) as caught:
        fetcher_for(repo, tmp_path / "work").fetch(spec, job_id="JOB-1")
    assert caught.value.reason == "git_url_shape"


@requires_git
def test_an_archive_over_the_cap_is_refused_while_streaming(tmp_path: Path) -> None:
    repo = build_repo(tmp_path)
    caps = dataclasses.replace(Caps(), max_git_archive_bytes=512)
    spec = parse_git_url("https://github.com/o/r")
    with pytest.raises(WebFetchRefused) as caught:
        fetcher_for(repo, tmp_path / "work", caps).fetch(spec, job_id="JOB-1")
    assert caught.value.reason == "git_too_large"


@requires_git
def test_the_scratch_tree_is_removable_after_a_job(tmp_path: Path) -> None:
    """``/work`` is scratch, never an archive — and on Windows that means
    coping with git's read-only object files."""
    repo = build_repo(tmp_path)
    work = tmp_path / "work"
    fetcher = fetcher_for(repo, work)
    fetcher.fetch(parse_git_url("https://github.com/o/r"), job_id="JOB-1")
    assert (work / "JOB-1").exists()
    fetcher.cleanup("JOB-1")
    assert not (work / "JOB-1").exists()


@requires_git
def test_a_second_job_reusing_an_id_starts_clean(tmp_path: Path) -> None:
    repo = build_repo(tmp_path)
    work = tmp_path / "work"
    fetcher = fetcher_for(repo, work)
    fetcher.fetch(parse_git_url("https://github.com/o/r"), job_id="JOB-1")
    (work / "JOB-1" / "leftover.txt").write_text("stale", encoding="utf-8")
    fetcher.fetch(parse_git_url("https://github.com/o/r"), job_id="JOB-1")
    assert not (work / "JOB-1" / "leftover.txt").exists()


@requires_git
def test_the_tar_extracts_under_the_data_filter(tmp_path: Path) -> None:
    """The research side extracts with ``filter='data'`` (design §2.2); a tar
    this side produces must survive that, or the git route ends at the
    boundary."""
    repo = build_repo(tmp_path)
    archive = fetcher_for(repo, tmp_path / "work").fetch(
        parse_git_url("https://github.com/o/r"), job_id="JOB-1"
    )
    destination = tmp_path / "extracted"
    with tarfile.open(archive.tar_path) as tar:
        tar.extractall(destination, filter="data")
    assert (destination / "README.md").read_text(encoding="utf-8") == "# The project\n"


@requires_git
def test_the_result_block_matches_the_schema_field_names(tmp_path: Path) -> None:
    repo = build_repo(tmp_path)
    archive = fetcher_for(repo, tmp_path / "work").fetch(
        parse_git_url("https://github.com/o/r/tree/main/docs"), job_id="JOB-1"
    )
    block = archive.as_result_git_block()
    assert set(block) == {"head", "ref", "path"}
    assert block["path"] == "docs"


@requires_git
def test_a_missing_remote_fails_without_prompting(tmp_path: Path) -> None:
    """``GIT_TERMINAL_PROMPT=0``: a private or deleted repository fails in a
    second instead of blocking a single-threaded sidecar on a credential
    prompt forever."""
    fetcher = GitFetcher(
        Caps(),
        work_dir=tmp_path / "work",
        _clone_url_fn=lambda _spec: (tmp_path / "nothing-here").as_uri(),
    )
    with pytest.raises(WebFetchRefused) as caught:
        fetcher.fetch(parse_git_url("https://github.com/o/r"), job_id="JOB-1")
    assert caught.value.reason == "git_url_shape"


def test_a_missing_git_executable_is_a_refusal_not_a_crash(tmp_path: Path) -> None:
    fetcher = GitFetcher(Caps(), work_dir=tmp_path / "work", git_exe="git-that-is-not-installed")
    with pytest.raises(WebFetchRefused) as caught:
        fetcher.fetch(parse_git_url("https://github.com/o/r"), job_id="JOB-1")
    assert caught.value.reason == "git_url_shape"


@requires_git
def test_the_clone_follows_no_redirects_and_speaks_one_transport(tmp_path: Path) -> None:
    """lane a fix pass (CONT-5): git's own redirect following is off.

    Git defaults to ``http.followRedirects=initial``, and a hop taken inside
    a child process is a hop this package's ``redirect_off_allowlist`` rule
    never sees — so a ``git``-flagged allowlisted host could bounce a clone
    onto any other host, with nothing in the audit to show it. Belt: exactly
    one transport is permitted, so ``ext::``, ``ssh://`` and the rest of the
    surface are closed by rule rather than by enumeration.
    """
    captured: list[list[str]] = []
    fetcher = fetcher_for(build_repo(tmp_path), tmp_path / "work")
    original = fetcher._run

    def record(argv, **kwargs):
        captured.append(list(argv))
        return original(argv, **kwargs)

    fetcher._run = record
    fetcher.fetch(parse_git_url("https://github.com/o/r"), job_id="JOB-1")

    clone = next(argv for argv in captured if "clone" in argv)
    assert "-c" in clone
    settings = [clone[i + 1] for i, token in enumerate(clone) if token == "-c"]
    assert "http.followRedirects=false" in settings
    assert "protocol.allow=never" in settings
    # The fixture clones a local bare repository through the `_clone_url_fn`
    # seam, so the one permitted transport here is `file`; in production
    # `parse_git_url` builds an https URL and it is `protocol.https.allow`.
    assert "protocol.file.allow=always" in settings
    assert not any(setting.startswith("protocol.ssh") for setting in settings)


def test_a_clone_url_on_an_unclonable_transport_is_refused(tmp_path: Path) -> None:
    """The transport allowance is chosen from a closed set, never from
    whatever scheme the URL happened to carry."""
    fetcher = GitFetcher(
        Caps(),
        work_dir=tmp_path / "work",
        _clone_url_fn=lambda _spec: "ext::sh -c whoami",
    )
    with pytest.raises(WebFetchRefused) as caught:
        fetcher.fetch(parse_git_url("https://github.com/o/r"), job_id="JOB-1")
    assert caught.value.reason == "git_url_shape"
