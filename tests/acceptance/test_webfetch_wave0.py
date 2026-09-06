"""A-wave0 and the live half of B-hostile — the web-ingestion acceptance
journey, against the real internet.

**Skipped unless ``TRIALERROR_WEBFETCH_LIVE_TESTS=1``.** This is the only file
in the web-ingestion suite that opens a socket to a machine somebody else owns,
and it does so under the same discipline
``tests/test_litapi_live_smoke.py`` already sets: the default ``pytest`` run — a
fresh clone, an offline laptop, CI — makes no request at all.

    # PowerShell
    $env:TRIALERROR_WEBFETCH_LIVE_TESTS = "1"
    $env:TRIALERROR_WEBFETCH_LIST = "C:/path/to/a delivered list.md"
    $env:TRIALERROR_WEBFETCH_CONTACT = "you@example.org"
    pytest tests/acceptance/test_webfetch_wave0.py -v

    # bash
    TRIALERROR_WEBFETCH_LIVE_TESTS=1 \\
    TRIALERROR_WEBFETCH_LIST="/path/to/a delivered list.md" \\
    TRIALERROR_WEBFETCH_CONTACT="you@example.org" \\
      pytest tests/acceptance/test_webfetch_wave0.py -v

**Both halves run in one process, and that is honest rather than a shortcut.**
The two containers of the deployment share exactly one thing — a directory — so
running the fetch loop and the research handlers in one interpreter changes no
behaviour that matters here. What it does NOT test is the containment: that the
fetch process cannot see the corpus, that the research container's firewall is
untouched, that the allowlist is unreachable from inside. Those are container
facts, they are items G-fw / G-mounts / G-policy of
``docs/reviews/lane-a/ACCEPTANCE.md``, and no in-process test can stand in for
them. This file proves the PIPELINE; that runbook proves the BOX.

**What passing means** (design section 6, A-wave0, verbatim in spirit): every link
in the list ends in exactly one of

* a ``document`` with ``status='indexed'`` whose every ``quote_anchor`` still
  resolves, or
* a ``source`` row in ``request_state='wanted'`` carrying a reason from the
  closed vocabulary,

with zero links unaccounted for. It is emphatically NOT "N documents": a
paywalled newsletter and a JavaScript-only marketing page ending in ``wanted``
is the correct, lawful outcome, and a run that produced eight documents from a
list containing those would mean something had gone around a wall.

The allowlist this test writes is derived from the list file itself, which is the
same reasoning ``te-webfetch.sh import-list`` rests on: a list the operator
delivered IS the operator's statement of intent about those hosts. It is written
into a throwaway directory under ``tmp_path`` and never touches a deployment.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from tests._ingest_fixtures import bootstrap_launch
from tests._webfetch_research import write_program_config
from trialerror.ingest.anchors import spot_resolve
from trialerror.jobs.worker import run_one
from trialerror.webfetch import HUMAN_FIXABLE_REASONS, REASONS
from trialerror.webfetch.handlers import enqueue_batch, fetch_report, fetch_rows
from trialerror.webfetch.links import read_link_list
from trialerror.webfetch.policy import (
    ALLOWED_HOSTS_FILENAME,
    POLICY_FILENAME,
    ROBOTS_OVERRIDES_FILENAME,
)
from trialerror.webfetch.protocol import Queue
from trialerror.webfetch.sidecar import Sidecar, SidecarPaths

LIVE = os.environ.get("TRIALERROR_WEBFETCH_LIVE_TESTS") == "1"
LIST_PATH = os.environ.get("TRIALERROR_WEBFETCH_LIST", "")
CONTACT = os.environ.get("TRIALERROR_WEBFETCH_CONTACT", "")

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.skipif(
        not LIVE,
        reason=(
            "live network acceptance: set TRIALERROR_WEBFETCH_LIVE_TESTS=1 (and "
            "TRIALERROR_WEBFETCH_LIST to a delivered links file, "
            "TRIALERROR_WEBFETCH_CONTACT to your own address for the User-Agent). "
            "See the module docstring and docs/reviews/lane-a/ACCEPTANCE.md."
        ),
    ),
]

#: How many turns of the crank the journey gets before it gives up. Each turn is
#: one manifest out, one fetch, one settle; the per-host pacing is >= 3 s, so a
#: list of eight links is a couple of minutes at worst.
MAX_TURNS = 60


def _hosts_of(entries) -> list[str]:
    seen: list[str] = []
    for entry in entries:
        host = (urlsplit(entry.url).hostname or "").lower()
        if host and host not in seen:
            seen.append(host)
    return seen


def _write_policy(directory: Path, hosts: list[str], *, contact: str) -> Path:
    """The list's own hosts, `git` on any code-forge host, https only."""
    directory.mkdir(parents=True, exist_ok=True)
    lines = []
    for host in hosts:
        flags = " git" if host in ("github.com", "gitlab.com", "codeberg.org") else ""
        lines.append(f"{host}{flags}")
    (directory / ALLOWED_HOSTS_FILENAME).write_text("\n".join(lines) + "\n", encoding="utf-8")
    (directory / POLICY_FILENAME).write_text(
        "mode = \"allowlist\"\n"
        f"contact_mailto = \"{contact}\"\n"
        "honor_tdm_optout = false\n"
        # Deliberately the SHIPPED defaults for everything else: an acceptance run
        # that loosened a cap would be proving a configuration nobody deploys.
        "min_host_interval_s = 3.0\n",
        encoding="utf-8",
    )
    (directory / ROBOTS_OVERRIDES_FILENAME).write_text("", encoding="utf-8")
    return directory


@pytest.fixture()
def journey(store, program_root, tmp_path):
    """A program with fetching on, a real sidecar over a real socket, and the
    delivered list parsed."""
    if not LIST_PATH:
        pytest.skip("TRIALERROR_WEBFETCH_LIST is not set (see the module docstring)")
    if not CONTACT:
        pytest.skip(
            "TRIALERROR_WEBFETCH_CONTACT is not set — C-0069 says identify honestly, and "
            "this test will not put a placeholder address in a real User-Agent"
        )
    list_path = Path(LIST_PATH)
    if not list_path.is_file():
        pytest.skip(f"TRIALERROR_WEBFETCH_LIST points at nothing: {list_path}")

    entries, list_ref = read_link_list(list_path)
    assert entries, f"{list_path} names no links"

    write_program_config(program_root, queue_dir="webfetch-queue", wait_s=1.0, poll_interval_s=0.2)
    launch_id = bootstrap_launch(store)
    queue_dir = program_root / "webfetch-queue"
    Queue(queue_dir).ensure_layout()

    sidecar = Sidecar(
        SidecarPaths(
            queue=queue_dir,
            policy=_write_policy(tmp_path / "policy", _hosts_of(entries), contact=CONTACT),
            audit=tmp_path / "audit",
            state=tmp_path / "state",
            work=tmp_path / "work",
        ),
        worker_id="acceptance-sidecar",
        poll_interval_s=0.0,
    )
    return {
        "entries": entries,
        "list_ref": list_ref,
        "launch_id": launch_id,
        "queue_dir": queue_dir,
        "sidecar": sidecar,
        "audit_dir": tmp_path / "audit",
    }


def _crank(store, sidecar) -> None:
    """Run both halves until neither has anything left to do."""
    for _ in range(MAX_TURNS):
        with store.jobs:
            store.jobs.execute("UPDATE job SET next_attempt_ts = NULL WHERE state = 'pending'")
        outcome = run_one(store, worker_id="acceptance-worker")
        stats = sidecar.run(max_jobs=4, max_idle_polls=1)
        if outcome["status"] == "idle" and stats.claimed == 0:
            # One more pass: the last fetch may have landed after the worker
            # looked, and the settle for it is a job that does not exist yet.
            with store.jobs:
                store.jobs.execute("UPDATE job SET next_attempt_ts = NULL WHERE state = 'pending'")
            if run_one(store, worker_id="acceptance-worker")["status"] == "idle":
                return
    raise AssertionError(f"the journey did not settle within {MAX_TURNS} turns")


# ---------------------------------------------------------------------------
# A-wave0
# ---------------------------------------------------------------------------


def test_every_link_in_the_delivered_list_is_accounted_for(store, program_root, journey):
    entries = journey["entries"]
    results = enqueue_batch(
        store,
        entries,
        launch_id=journey["launch_id"],
        list_ref=journey["list_ref"],
    )
    assert len(results) == len(entries)

    _crank(store, journey["sidecar"])

    rows = {row["url_norm"]: row for row in fetch_rows(store, list_ref=journey["list_ref"])}
    unaccounted: list[str] = []
    indexed: list[str] = []
    wanted: list[tuple[str, str]] = []

    for row in rows.values():
        if row["state"] == "extracted" and row["doc_id"]:
            document = store.knowledge.execute(
                "SELECT status FROM document WHERE doc_id = ?", (row["doc_id"],)
            ).fetchone()
            if document and document["status"] == "indexed":
                indexed.append(row["url_norm"])
                continue
            unaccounted.append(f"{row['url_norm']}: doc {row['doc_id']} is {document['status'] if document else 'missing'}")
            continue
        if row["state"] in ("refused", "unchanged", "failed"):
            reason = row["reason"]
            assert reason in REASONS or reason is None, f"{row['url_norm']}: reason {reason!r} is not in the closed vocabulary"
            if reason in HUMAN_FIXABLE_REASONS:
                source = store.knowledge.execute(
                    "SELECT source_id, request_state FROM source WHERE url = ?", (row["url"],)
                ).fetchone()
                assert source is not None, (
                    f"{row['url_norm']} was refused with {reason}, which a human can lawfully "
                    "fix, and no `wanted` source row was filed for them to fix it"
                )
                assert source["request_state"] == "wanted", (
                    f"{row['url_norm']}: source is {source['request_state']}, expected wanted"
                )
                wanted.append((row["url_norm"], reason))
                continue
            # A policy refusal (SSRF shape, a cap, a content type) is a complete
            # answer with no human action behind it — accounted for, no row.
            wanted.append((row["url_norm"], reason or row["state"]))
            continue
        unaccounted.append(f"{row['url_norm']}: state {row['state']}")

    # Every LINE of the list has to map to a row, not just every row to a line:
    # a link that was refused at enqueue never creates one, and silently losing
    # it is the failure mode this whole assertion exists to catch.
    for result in results:
        if result.action == "refused":
            assert result.reason in REASONS, f"{result.url_norm}: {result.reason!r}"
            wanted.append((result.url_norm or "(unparseable)", result.reason))

    report = fetch_report(store, list_ref=journey["list_ref"])
    print(json.dumps({"indexed": indexed, "accounted": wanted, "report": report}, indent=2))
    assert not unaccounted, "links with no outcome:\n  " + "\n  ".join(unaccounted)
    assert len(indexed) + len(wanted) >= len(entries)


def test_every_indexed_page_keeps_its_anchors_resolving(store, program_root, journey):
    enqueue_batch(
        store,
        journey["entries"],
        launch_id=journey["launch_id"],
        list_ref=journey["list_ref"],
    )
    _crank(store, journey["sidecar"])

    checked = 0
    for row in fetch_rows(store, list_ref=journey["list_ref"]):
        if not row["doc_id"]:
            continue
        # Same shape as trialerror.ingest.checks.check_anchor_spot_resolve:
        # recompute stream_v1 over the document's CURRENT elements and compare
        # against the hash the anchor stored. An anchor that stops resolving is
        # the failure the whole extract-then-chunk path exists to avoid.
        elements = [
            dict(r)
            for r in store.knowledge.execute(
                "SELECT * FROM element WHERE doc_id = ?", (row["doc_id"],)
            ).fetchall()
        ]
        anchors = store.knowledge.execute(
            "SELECT * FROM quote_anchor WHERE doc_id = ?", (row["doc_id"],)
        ).fetchall()
        for anchor in anchors:
            assert spot_resolve(elements, dict(anchor)), (
                f"{row['url_norm']}: anchor {anchor['anchor_id']} no longer resolves"
            )
            checked += 1
    print(f"resolved {checked} quote anchor(s)")


def test_a_repo_link_is_cloned_rather_than_scraped(store, program_root, journey):
    """The operator's own rule: a code forge is cloned, never fetched through
    its web UI. If the list names no forge, there is nothing to prove."""
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH")
    forge_entries = [e for e in journey["entries"] if e.resolved_kind == "git"]
    if not forge_entries:
        pytest.skip("the delivered list names no repository")

    enqueue_batch(
        store,
        journey["entries"],
        launch_id=journey["launch_id"],
        list_ref=journey["list_ref"],
    )
    _crank(store, journey["sidecar"])

    git_rows = [r for r in fetch_rows(store, list_ref=journey["list_ref"]) if r["kind"] == "git"]
    assert len(git_rows) == len(forge_entries)
    for row in git_rows:
        if row["state"] != "extracted":
            continue
        assert row["git_head"], f"{row['url_norm']}: no HEAD sha recorded"
        assert row["content_class"] == "git"

    # ...and nothing went at the web UI instead.
    audit_text = "".join(
        path.read_text(encoding="utf-8")
        for path in sorted(journey["audit_dir"].glob("webfetch-*.jsonl"))
    )
    for line in audit_text.splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("host") in ("github.com", "gitlab.com", "codeberg.org"):
            assert record.get("outcome") != "fetched" or record.get("content_class") in (None, "git"), (
                f"a code forge was fetched rather than cloned: {record}"
            )


def test_every_attempt_left_one_attributable_audit_line(store, program_root, journey):
    enqueue_batch(
        store,
        journey["entries"],
        launch_id=journey["launch_id"],
        list_ref=journey["list_ref"],
    )
    _crank(store, journey["sidecar"])

    lines = []
    for path in sorted(journey["audit_dir"].glob("webfetch-*.jsonl")):
        lines.extend(
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
        )
    assert lines, "no audit lines were written for a run that fetched something"
    for record in lines:
        assert record["launch_id"] == journey["launch_id"], record
        assert record["job_id"], record
        assert record["fetch_id"], record


# ---------------------------------------------------------------------------
# B-hostile, the two rows that need a real resolver
# ---------------------------------------------------------------------------

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "webfetch" / "hostile_urls.jsonl"


def _live_fixture_rows() -> list[dict]:
    rows = [
        json.loads(line)
        for line in _FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [r for r in rows if r.get("live") and r["layer"] != "_meta"]


def test_a_public_name_that_resolves_to_loopback_is_refused(store, program_root, tmp_path):
    """``127.0.0.1.nip.io`` is a real, public, resolvable name whose A record is
    loopback. It is the one case the syntactic layer structurally cannot catch —
    the URL is perfectly well formed — so it is the case that proves there is a
    second layer at all.

    Which row of the fixture this is, and why, lives in
    ``tests/fixtures/webfetch/hostile_urls.jsonl`` under id ``nip-io-private``.
    """
    row = next(r for r in _live_fixture_rows() if r["id"] == "nip-io-private")
    if not CONTACT:
        pytest.skip("TRIALERROR_WEBFETCH_CONTACT is not set")

    from trialerror.webfetch import WebFetchRefused
    from trialerror.webfetch.fetcher import Fetcher
    from trialerror.webfetch.netguard import NetGuard
    from trialerror.webfetch.policy import HostPacer, Policy

    host = urlsplit(row["input"]).hostname or ""
    policy = Policy.load(_write_policy(tmp_path / "policy", [host], contact=CONTACT))
    fetcher = Fetcher(policy, NetGuard(), pacer=HostPacer(0.0))
    with pytest.raises(WebFetchRefused) as caught:
        fetcher.fetch(row["input"])
    assert caught.value.reason == row["expected_reason"], row["why"]


def test_the_live_fixture_rows_are_the_two_the_runbook_names():
    """A guard on the fixture rather than on the code: if a third live row
    appears, somebody has to decide whether the runbook covers it."""
    assert sorted(r["id"] for r in _live_fixture_rows()) == [
        "nip-io-private",
        "redirect-to-metadata",
    ]
