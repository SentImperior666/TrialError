"""The three records the first live run argued for (canary attempt 3, 2026-09-20).

Canary attempt 3 (2026-09-20) ran end to end and still left three questions
that only another rental could answer:

* how much room ``/dev/shm`` had on the host -- ``_choose_scratch`` read it,
  decided, and dropped it, so "will a rented host hold 1.2 GB of a large scan in
  RAM?" had no recorded answer;
* WHY the venv was not built -- ``env_kind: user-break-system-packages`` was a
  verdict with no cause, and ensurepip was a guess;
* what marker actually wrote -- the range cache is removed once the document
  is published, so the raw text had to be reconstructed by arithmetic.

Driven with L2's fakes (``tests/_vastai_shell_fakes.py``): no network, no ssh,
no GPU, no real ``marker_single``, nothing rented. The bootstrap's own half of
the venv record is in ``test_vastai_r7_bootstrap_sh.py`` (the REAL shell).
"""

from __future__ import annotations

import re

import pytest

from tests._vastai_fakes import FAKE_KEY, default_offers, isolated_state, network_tripwire  # noqa: F401 - fixtures
from tests._vastai_shell_fakes import JOB, make_env, run_env, ssh_tripwire  # noqa: F401 - fixtures
from tests.test_vastai_worker import _cache, _published, _rows

#: Docker's default ``/dev/shm``, on every host: too small for any document here.
SMALL_SHM = 64_000_000
VENV_ERROR = "/usr/bin/python3: No module named venv"


@pytest.fixture(autouse=True)
def _guarded(network_tripwire, isolated_state, ssh_tripwire):  # noqa: F811
    yield


@pytest.fixture
def state(isolated_state):  # noqa: F811
    return isolated_state


# ---------------------------------------------------------------------------
# 1. the free /dev/shm, recorded beside the kind it decided
# ---------------------------------------------------------------------------


def test_the_free_shm_is_recorded_beside_the_scratch_kind(tmp_path, state):
    env = make_env(tmp_path, state)
    env.world.shm_avail = 7_000_000_000
    summary = run_env(env)

    assert summary["published"] == [JOB]
    shipped, outcome = _rows(env, "shipped")[0], _rows(env, "outcome")[0]
    assert shipped["scratch_kind"] == outcome["scratch_kind"] == "shm"
    assert shipped["shm_avail_bytes"] == outcome["shm_avail_bytes"] == 7_000_000_000
    assert any("/dev/shm 7000000000 byte(s) free" in line for line in env.world.log), env.world.log


def test_a_host_refused_for_too_little_shm_records_what_it_had(tmp_path, state):
    """The host whose number matters most is the one that was REFUSED: without
    this row, "how much would a host have to have?" costs a rental to ask."""
    env = make_env(tmp_path, state)
    env.world.shm_avail = SMALL_SHM
    summary = run_env(env)

    assert [e["reason_code"] for e in summary["refused"]] == ["shm-too-small"]
    outcomes = _rows(env, "outcome")
    assert outcomes and all(row["shm_avail_bytes"] == SMALL_SHM for row in outcomes)
    assert all(row["scratch_kind"] is None for row in outcomes), "nothing was shipped anywhere"
    assert _rows(env, "shipped") == []


def test_a_host_that_reported_no_shm_at_all_records_none_not_a_number(tmp_path, state):
    """``os.statvfs`` can fail on the instance: the report then carries no
    number, and an absent number must not become a made-up one (the host is
    refused, as it was before, because no positive signal means no RAM scratch)."""
    env = make_env(tmp_path, state)
    env.world.shm_avail = None
    summary = run_env(env)

    assert [e["reason_code"] for e in summary["refused"]] == ["shm-too-small"]
    assert all(row["shm_avail_bytes"] is None for row in _rows(env, "outcome"))
    assert any("/dev/shm unknown byte(s) free" in line for line in env.world.log), env.world.log


# ---------------------------------------------------------------------------
# 2. why the venv was not built
# ---------------------------------------------------------------------------


def test_the_venv_failure_is_reported_beside_the_env_kind(tmp_path, state):
    env = make_env(tmp_path, state)
    env.world.env_kind = "user-break-system-packages"
    env.world.venv_error = VENV_ERROR
    summary = run_env(env)

    assert summary["published"] == [JOB]
    versions = _rows(env, "outcome")[0]["remote_versions"]
    assert versions["env_kind"] == "user-break-system-packages" and versions["venv_error"] == VENV_ERROR
    assert any(f"venv: {VENV_ERROR}" in line for line in env.world.log), env.world.log
    result, _pages = _published(env)
    assert result["vastai"]["remote_versions"]["venv_error"] == VENV_ERROR


def test_a_built_venv_reports_no_cause_because_there_is_none(tmp_path, state):
    env = make_env(tmp_path, state)
    env.world.env_kind = "venv"
    assert run_env(env)["published"] == [JOB]

    assert _rows(env, "outcome")[0]["remote_versions"]["venv_error"] is None
    assert not any("venv: " in line for line in env.world.log), env.world.log


# ---------------------------------------------------------------------------
# 3. --keep-range-cache: marker's own text, kept
# ---------------------------------------------------------------------------


def test_keep_range_cache_keeps_markers_own_text_after_the_document_is_published(tmp_path, state):
    env = make_env(tmp_path, state)
    summary = run_env(env, keep_range_cache=True)

    assert summary["published"] == [JOB]
    kept = sorted(p.name for p in _cache(env).glob("range-*.md"))
    assert kept == [
        "range-000000-000002.md",
        "range-000003-000005.md",
        "range-000006-000008.md",
        "range-000009-000009.md",
    ]
    text = (_cache(env) / kept[0]).read_text(encoding="utf-8")
    assert re.search(r"\{\d+\}-{4,}", text), "the kept text is marker's own, page separators and all"
    assert any("--keep-range-cache" in line for line in env.world.log), env.world.log


def test_without_the_flag_a_published_document_still_leaves_no_cache(tmp_path, state):
    """The default is unchanged: a finished document's ranges are nobody's
    resume, and nothing about the flag may alter that."""
    env = make_env(tmp_path, state)
    assert run_env(env)["published"] == [JOB]

    assert not _cache(env).exists()
    assert not any("--keep-range-cache" in line for line in env.world.log)
