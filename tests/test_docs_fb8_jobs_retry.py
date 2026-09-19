"""Lane FB-8a item 6: the command catalog and the behaviour in lockstep.

Two kinds of assertion, and the split matters:

- the CATALOG check reads the ``jobs`` row of ``OPERATOR_GUIDE.md``'s
  command table against the verbs the CLI parser actually registers, so a
  verb added without a row (or a row for a verb that was removed) fails
  here rather than being discovered by an operator running ``--help`` and
  finding something the guide never mentions. It found one pre-existing
  gap the day it was written: ``abandon`` had shipped in lane FB-3 and was
  never added to the table.
- the CLAIM checks pin, narrowly, that each behaviour this lane shipped is
  findable where the brief says it should be -- the same shape
  ``tests/test_docs_small_items.py`` uses. A behaviour nobody can find is
  one that surprises somebody later, and this lane is made of exactly the
  distinctions an operator gets wrong when nothing written down says
  otherwise: which states retry accepts, what it keeps, and what happens to
  the failed attempt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_DOCS = Path(__file__).resolve().parents[1] / "docs"


def _read(name: str) -> str:
    path = _DOCS / name
    if not path.is_file():
        pytest.skip(f"{name} not present in this tree")
    return path.read_text(encoding="utf-8")


def _group_row(group: str) -> str:
    text = _read("OPERATOR_GUIDE.md")
    rows = [line for line in text.splitlines() if line.startswith(f"| `{group}` |")]
    assert rows, f"OPERATOR_GUIDE.md's command table has no `{group}` row"
    return rows[0]


def _registered_verbs(group: str) -> set[str]:
    import argparse

    from trialerror.cli import build_parser

    parser = build_parser()
    groups = [
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    ][0]
    sub = groups.choices[group]
    verbs = [a for a in sub._actions if isinstance(a, argparse._SubParsersAction)][0]
    return set(verbs.choices)


def _catalogued_verbs(group: str) -> set[str]:
    import re

    cells = _group_row(group).split("|")
    return set(re.findall(r"`([a-z][a-z0-9-]*)`", cells[2]))


# ---------------------------------------------------------------------------
# the catalog, counted
# ---------------------------------------------------------------------------
#: Scoped to ``jobs`` deliberately. When this was written, running the same
#: check against ``offload`` failed for a reason outside this lane: the
#: command table had no ``offload`` row at all -- the only ``| `offload` |``
#: line in the guide was the DOCTOR catalog's. Writing a whole group's row
#: from inside a lane about one verb is how a catalog gets a row nobody
#: reviewed, so it was named in this lane's report instead. Lane FB-8b item 3
#: closed it, and ``tests/test_docs_fb8b_small.py`` holds the ``offload``
#: row's own derived checks (its verbs AND its flag spellings); this tuple
#: stays scoped to ``jobs`` rather than asserting the same thing twice.
_CATALOGUED_GROUPS = ("jobs",)


@pytest.mark.parametrize("group", _CATALOGUED_GROUPS)
def test_the_command_catalog_names_every_registered_verb(group):
    registered = _registered_verbs(group)
    catalogued = _catalogued_verbs(group)
    assert not (registered - catalogued), (
        f"OPERATOR_GUIDE.md's `{group}` row does not name: {sorted(registered - catalogued)}"
    )
    assert not (catalogued - registered), (
        f"OPERATOR_GUIDE.md's `{group}` row names verbs that do not exist: "
        f"{sorted(catalogued - registered)}"
    )


def test_the_jobs_row_counts_what_the_parser_registers():
    assert len(_catalogued_verbs("jobs")) == len(_registered_verbs("jobs"))


def test_retry_is_in_the_catalog_with_its_flags():
    row = _group_row("jobs")
    assert "`retry`" in row
    assert "--reason" in row
    assert "--max-attempts" in row
    assert "--clear-checkpoint" in row


# ---------------------------------------------------------------------------
# the claims
# ---------------------------------------------------------------------------
def test_the_guide_says_which_states_retry_accepts_and_which_it_refuses():
    guide = _read("OPERATOR_GUIDE.md")
    assert "Retry — the way back from `failed`/`abandoned`" in guide
    block = guide.split("Retry — the way back from", 1)[1].split("\n- **", 1)[0]
    for state in ("complete", "pending", "claimed", "running", "paused"):
        assert f"`{state}`" in block, f"the refusal for {state!r} is not documented"
    assert "jobs resume" in block, "the paused refusal must point at the verb that does apply"


def test_the_guide_says_what_retry_keeps():
    guide = _read("OPERATOR_GUIDE.md")
    block = guide.split("Retry — the way back from", 1)[1].split("\n- **", 1)[0]
    assert "`last_error` is kept" in block
    assert "retried <ts>: " in block
    assert "--clear-checkpoint" in block
    assert "1–10" in block


def test_the_guide_carries_the_two_machine_recipe():
    guide = _read("OPERATOR_GUIDE.md")
    heading = "After a fix on master: update the worker, then `jobs retry` the jobs that met the defect."
    assert heading in guide
    block = guide.split(heading, 1)[1].split("\n- **", 1)[0]
    assert "terminal after N DEV attempt(s)" in block
    assert "`failed/_retried/<job_id>.<stamp>/`" in block
    assert "kept, never deleted" in block
    assert "parked_largeformat" in block
    assert "refuses by name" in block


def test_the_doctor_catalog_says_the_check_does_not_fire_on_retried():
    row = [
        line
        for line in _read("OPERATOR_GUIDE.md").splitlines()
        if line.startswith("| `offload` | `offload_backlog`")
    ]
    assert row, "the doctor catalog has no `offload` row"
    assert "failed/_retried/" in row[0]
    assert "details.retried" in row[0]


def test_user_setup_says_terminal_is_not_permanent():
    setup = _read("USER_SETUP.md")
    assert "jobs retry" in setup
    assert "offload/failed/_retried/" in setup
