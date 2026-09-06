"""Both halves, over the real protocol, in one process.

Everywhere else in this suite one side is stood in for: the sidecar's tests
hand it manifests nobody enqueued, and the handlers' tests read results
nobody fetched. Here the two halves meet. A URL goes in through
``webfetch add``; the research handler writes a manifest; the **real**
:class:`~trialerror.webfetch.sidecar.Sidecar` loop claims it and fetches from
a **real** ``http.server`` on loopback; the research handler reads the result
back and the existing ingest pipeline carries it to an indexed document with
resolving anchors.

Nothing here reaches the internet: the netguard's resolver answers
``127.0.0.1`` and its socket factory dials the fixture's port, both as
constructor arguments — the design's rule is that no mounted *file* can
weaken the address policy, and a Python object passed in a test is not one.

The two containers are simulated by nothing at all, and that is the point:
the only thing they share is a directory, so running both halves in one
process changes no behaviour that matters. If this file passes, the transport
ruled in L-A1 works end to end.
"""

from __future__ import annotations

import json
import shutil
import tarfile
from pathlib import Path

import pytest

from tests._ingest_fixtures import bootstrap_launch
from tests._webfetch_fixtures import LocalSite, Route, loopback_netguard, write_policy
from tests._webfetch_research import write_program_config
from tests.test_webfetch_gitfetch import build_repo
from tests.test_webfetch_handlers import run_kind, run_until_idle
from trialerror.jobs import ledger
from trialerror.jobs.worker import run_one
from trialerror.stores.writer import insert
from trialerror.util.timeutil import now
from trialerror.webfetch import handlers
from trialerror.webfetch.gitfetch import GitFetcher
from trialerror.webfetch.handlers import enqueue_fetch, fetch_report
from trialerror.webfetch.protocol import Queue
from trialerror.webfetch.sidecar import Sidecar, SidecarPaths

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")

ALLOW_ROBOTS = Route(body=b"User-agent: *\nDisallow:\n", content_type="text/plain")
DENY_ROBOTS = Route(body=b"User-agent: *\nDisallow: /\n", content_type="text/plain")
ARTICLE_URL = "http://test.example/article"

ARTICLE = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Deterministic lockstep</title>
<meta name="author" content="A. Researcher">
<link rel="license" href="https://creativecommons.org/licenses/by/4.0/"></head>
<body>
<nav>Home | About</nav>
<main>
<h1>Deterministic lockstep</h1>
<p>Every peer simulates the same world from the same inputs, so only the
inputs travel. The network carries intent, never state.</p>
<h2>Why it holds</h2>
<p>Determinism of the simulation step, and an agreed ordering of the inputs
that feed it.</p>
<ul><li>Determinism</li><li>Ordering</li></ul>
<table><tr><th>Term</th><th>Meaning</th></tr><tr><td>Tick</td><td>One step</td></tr></table>
<div style="display:none">HIDDEN-INSTRUCTION</div>
<!-- HIDDEN-COMMENT -->
<script>var x = "HIDDEN-SCRIPT";</script>
<p>More at <a href="https://other.example/next">this link</a>.</p>
</main>
<footer>All rights reserved</footer>
</body></html>
""".encode(
    "utf-8"
)


def make_sidecar(
    tmp_path, site, queue_dir, *, hosts="test.example http\n", netguard_hosts=(), **kwargs
) -> Sidecar:
    # lane a fix pass (CONT-5): the git branch now resolves the clone host
    # through NetGuard before it invokes git, so a test whose policy allows a
    # host the fixture resolver has never heard of has to say so here too.
    return Sidecar(
        SidecarPaths(
            queue=queue_dir,
            policy=write_policy(tmp_path, hosts=hosts),
            audit=tmp_path / "audit",
            state=tmp_path / "state",
            work=tmp_path / "work",
        ),
        worker_id="sidecar-1",
        netguard=loopback_netguard(site.port, hosts=netguard_hosts),
        poll_interval_s=0,
        **kwargs,
    )


@pytest.fixture()
def site():
    with LocalSite(
        {
            "/robots.txt": ALLOW_ROBOTS,
            "/article": Route(body=ARTICLE, content_type="text/html; charset=utf-8"),
        }
    ) as running:
        yield running


@pytest.fixture()
def wired(store, program_root, site, tmp_path):
    """A program with web fetching on, and a sidecar pointed at its queue."""
    write_program_config(program_root, queue_dir="webfetch-queue")
    launch_id = bootstrap_launch(store)
    queue_dir = program_root / "webfetch-queue"
    return launch_id, make_sidecar(tmp_path, site, queue_dir), queue_dir


def carry(store, sidecar, *, jobs: int = 1) -> None:
    """One full turn of the crank: park, fetch, settle, ingest.

    The outbound passes deliberately do NOT fast-forward the backoff. A
    parked job asks the ledger to come back in a minute, and that is what
    makes the next pass claim the *next* job rather than the same one again —
    the real worker loop gets the same round-robin for free. The inbound
    passes do fast-forward, because by then the answer is already in the
    queue and there is nothing to wait for.
    """
    for index in range(jobs):
        run_one(store, worker_id=f"out{index}", kinds=["web_fetch"])
    sidecar.run(max_jobs=jobs, max_idle_polls=1)
    for index in range(jobs):
        run_kind(store, "web_fetch", f"back{index}")
    run_until_idle(store)


# ---------------------------------------------------------------------------
# the whole path
# ---------------------------------------------------------------------------


def test_a_url_becomes_an_indexed_document_with_resolving_anchors(store, program_root, wired):
    launch_id, sidecar, queue_dir = wired
    added = enqueue_fetch(store, url=ARTICLE_URL, launch_id=launch_id)
    assert added.action == "enqueued"

    # 1. the research side writes a manifest and parks — no attempt burned
    assert run_kind(store, "web_fetch")["status"] == "deferred"
    assert Queue(queue_dir).pending_job_ids() == [added.job_id]
    assert ledger.get_job(store, added.job_id)["attempts"] == 0

    # 2. the real sidecar loop fetches it over a real socket
    stats = sidecar.run(max_jobs=1, max_idle_polls=1)
    assert stats.fetched == 1

    # 3. the research side settles, extracts, and the existing pipeline runs
    assert run_kind(store, "web_fetch", "w2")["status"] == "complete"
    run_until_idle(store)

    row = handlers._row(store, added.fetch_id)
    assert row["state"] == "extracted"
    assert row["outcome"] == "fetched"
    assert row["http_status"] == 200
    assert row["title"] == "Deterministic lockstep"
    assert row["author"] == "A. Researcher"
    assert row["robots_verdict"] == "allow"
    assert row["sidecar_version"]

    document = store.knowledge.execute(
        "SELECT * FROM document WHERE doc_id = ?", (row["doc_id"],)
    ).fetchone()
    assert document["status"] == "indexed"
    assert document["media_type"] == "html"

    chunks = store.knowledge.execute(
        "SELECT count(*) AS n FROM chunk WHERE doc_id = ?", (row["doc_id"],)
    ).fetchone()["n"]
    anchors = store.knowledge.execute(
        "SELECT count(*) AS n FROM quote_anchor WHERE doc_id = ? AND doc_sha256 = ?",
        (row["doc_id"], document["sha256"]),
    ).fetchone()["n"]
    assert chunks > 0
    assert anchors == chunks, "every chunk's anchor must resolve against the document"


def test_the_page_arrives_as_elements_the_chunker_understands(store, program_root, wired):
    launch_id, sidecar, _queue_dir = wired
    added = enqueue_fetch(store, url=ARTICLE_URL, launch_id=launch_id)
    carry(store, sidecar)

    row = handlers._row(store, added.fetch_id)
    types = [
        r["type"]
        for r in store.knowledge.execute(
            "SELECT type FROM element WHERE doc_id = ? ORDER BY seq", (row["doc_id"],)
        )
    ]
    assert types[0] == "Title"
    assert "ListItem" in types
    assert "Table" in types, "the md round-trip §2.2 rejected would have lost this"


def test_nothing_hidden_and_no_chrome_reaches_the_corpus(store, program_root, wired):
    launch_id, sidecar, _queue_dir = wired
    added = enqueue_fetch(store, url=ARTICLE_URL, launch_id=launch_id)
    carry(store, sidecar)

    row = handlers._row(store, added.fetch_id)
    corpus = " ".join(
        (r["text"] or "")
        for r in store.knowledge.execute(
            "SELECT text FROM element WHERE doc_id = ?", (row["doc_id"],)
        )
    )
    for hidden in ("HIDDEN-INSTRUCTION", "HIDDEN-COMMENT", "HIDDEN-SCRIPT"):
        assert hidden not in corpus
    assert "All rights reserved" not in corpus
    assert "The network carries intent" in corpus


def test_the_provenance_record_survives_beside_the_bytes(store, program_root, wired):
    launch_id, sidecar, _queue_dir = wired
    added = enqueue_fetch(store, url=ARTICLE_URL, launch_id=launch_id)
    carry(store, sidecar)

    row = handlers._row(store, added.fetch_id)
    provenance = json.loads((program_root / row["provenance_path"]).read_text(encoding="utf-8"))
    assert provenance["url"] == ARTICLE_URL
    assert provenance["final_url"].endswith("/article")
    assert provenance["resolved_ips"] == ["127.0.0.1"]
    assert provenance["robots"]["verdict"] == "allow"
    assert provenance["content_sha256"] == row["content_sha256"]
    assert (program_root / row["raw_path"]).read_bytes() == ARTICLE


def test_a_declared_cc_license_carries_through_to_the_source_row(store, program_root, wired):
    launch_id, sidecar, _queue_dir = wired
    added = enqueue_fetch(store, url=ARTICLE_URL, launch_id=launch_id)
    carry(store, sidecar)

    row = handlers._row(store, added.fetch_id)
    source = store.knowledge.execute(
        "SELECT * FROM source WHERE source_id = ?", (row["source_id"],)
    ).fetchone()
    assert source["license_tier"] == "open"
    assert source["acquisition_route"] == "web"
    assert "robots=allow" in source["rights_notes"]


def test_the_queue_is_left_clean(store, program_root, wired):
    """``done/<job>/`` is swept once the bytes are the corpus's. The queue
    directory sits inside the hourly snapshots (design §8), so leaving copies
    of every page in it would be a slow leak with a backup schedule."""
    launch_id, sidecar, queue_dir = wired
    enqueue_fetch(store, url=ARTICLE_URL, launch_id=launch_id)
    carry(store, sidecar)

    queue = Queue(queue_dir)
    assert queue.pending_job_ids() == []
    assert list(queue.iter_results()) == []
    assert not list(queue.claimed_dir.glob("*/*.json"))


# ---------------------------------------------------------------------------
# the lawful refusal, over the whole path
# ---------------------------------------------------------------------------


def test_a_robots_disallowed_page_ends_as_a_wanted_row(store, program_root, tmp_path):
    """Design §1 P7 end to end: refused by the sidecar's robots check,
    settled by the research side as a *result*, and handed to the operator
    with no bypass attempted anywhere along the way."""
    with LocalSite(
        {"/robots.txt": DENY_ROBOTS, "/article": Route(body=ARTICLE, content_type="text/html")}
    ) as site:
        write_program_config(program_root, queue_dir="webfetch-queue")
        launch_id = bootstrap_launch(store)
        sidecar = make_sidecar(tmp_path, site, program_root / "webfetch-queue")

        added = enqueue_fetch(store, url=ARTICLE_URL, launch_id=launch_id)
        run_kind(store, "web_fetch")
        assert sidecar.run(max_jobs=1, max_idle_polls=1).refused == 1
        settle = run_kind(store, "web_fetch", "w2")

    assert settle["status"] == "complete", "a refusal is a result, never a retry into the wall"
    row = handlers._row(store, added.fetch_id)
    assert (row["state"], row["reason"]) == ("refused", "robots_disallow")
    assert row["doc_id"] is None

    wanted = store.knowledge.execute("SELECT * FROM source WHERE request_state='wanted'").fetchone()
    assert wanted is not None
    assert wanted["acquisition_route"] == "user_delivered"
    assert "no bypass was attempted" in wanted["rights_notes"]
    assert (program_root / "requests" / "REQUESTS.md").is_file()
    assert "wanted" in (program_root / "requests" / "REQUESTS.md").read_text(encoding="utf-8")


def test_the_report_accounts_for_every_link_not_just_the_ones_that_worked(
    store, program_root, wired
):
    """Design §6's A-wave0 criterion in miniature: the pass condition is
    "every link accounted for with a reason", not "N documents"."""
    launch_id, sidecar, _queue_dir = wired
    enqueue_fetch(store, url=ARTICLE_URL, launch_id=launch_id)
    enqueue_fetch(store, url="http://test.example/missing", launch_id=launch_id)
    carry(store, sidecar, jobs=2)

    report = fetch_report(store)
    assert len(report) == 2
    by_url = {line["url"]: line for line in report}
    good = by_url[ARTICLE_URL]
    assert good["documentStatus"] == "indexed"
    assert good["chunks"] > 0
    assert good["anchorsOk"] == good["chunks"]
    missing = by_url["http://test.example/missing"]
    assert missing["verdict"] == "http_error"
    assert missing["docId"] is None
    # ids and counts only — no page text anywhere in what a CLI would print
    assert "network carries intent" not in json.dumps(report)


# ---------------------------------------------------------------------------
# repositories
# ---------------------------------------------------------------------------


@requires_git
def test_a_repo_becomes_documents_under_one_source(store, program_root, tmp_path, site):
    """Design §2.2's git branch over the real clone path: one source, the
    README and the docs as documents, the commit recorded, and no code."""
    write_program_config(program_root, queue_dir="webfetch-queue")
    launch_id = bootstrap_launch(store)
    repo = build_repo(tmp_path / "repo")
    sidecar = make_sidecar(
        tmp_path,
        site,
        program_root / "webfetch-queue",
        hosts="github.com http git\n",
        netguard_hosts=("github.com",),
        gitfetcher_factory=lambda policy, work: GitFetcher(
            policy.caps, work_dir=work, _clone_url_fn=lambda _spec: repo.as_uri()
        ),
    )

    added = enqueue_fetch(
        store, url="https://github.com/an-owner/a-repo", launch_id=launch_id
    )
    assert handlers._row(store, added.fetch_id)["kind"] == "git"
    carry(store, sidecar)

    row = handlers._row(store, added.fetch_id)
    assert row["state"] == "extracted"
    assert row["content_class"] == "git"
    assert row["git_head"]

    documents = [
        dict(r)
        for r in store.knowledge.execute(
            "SELECT raw_path, media_type FROM document WHERE source_id = ?", (row["source_id"],)
        )
    ]
    names = {Path(d["raw_path"]).name for d in documents}
    assert "README.md" in names
    assert "guide.md" in names
    assert "app.py" not in names, "code files are deferred, and named as deferred"
    assert all(d["media_type"] == "md" for d in documents)

    source = store.knowledge.execute(
        "SELECT license_tier, rights_notes FROM source WHERE source_id = ?", (row["source_id"],)
    ).fetchone()
    assert source["license_tier"] == "open"  # the fixture ships MIT
    assert row["git_head"] in source["rights_notes"]


def test_a_hostile_tar_member_cannot_escape_and_the_rest_still_lands(store, program_root, tmp_path):
    """``filter="data"`` is Python's, not ours, and it handles the two
    classic traversals differently — which is worth pinning down rather than
    assuming, because the difference decides what this test may assert.

    A ``..`` escape RAISES (``OutsideDestinationError``): the member is
    refused, counted, and the rest of the archive still lands, because one
    hostile entry in a thousand-file repository is not a reason to lose the
    repository. An ABSOLUTE path is instead *neutralised* — the filter strips
    the leading separator, so ``/tmp/absolute.md`` becomes
    ``tmp/absolute.md`` inside the destination. It is extracted, and that is
    fine: the property that matters is that nothing lands outside the
    extraction directory, and both members satisfy it.
    """
    write_program_config(program_root)
    launch_id = bootstrap_launch(store)

    raw_dir = program_root / "raw" / "web" / "github.com"
    raw_dir.mkdir(parents=True)
    tar_path = raw_dir / "WF-repo.tar"
    payload = tmp_path / "README.md"
    payload.write_text("# Fine\n", encoding="utf-8")
    with tarfile.open(tar_path, "w") as archive:
        archive.add(payload, arcname="README.md")
        for name in ("../../escaped.md", "/tmp/absolute.md"):
            member = tarfile.TarInfo(name)
            member.size = 0
            archive.addfile(member)

    insert(
        store,
        "web_fetch",
        {
            "fetch_id": "WF-repo",
            "job_id": "JOB-webfetch-WF-repo",
            "launch_id": launch_id,
            "url": "https://github.com/an-owner/a-repo",
            "url_norm": "https://github.com/an-owner/a-repo",
            "kind": "git",
            "origin": "operator_list",
            "state": "fetched",
            "content_class": "git",
            "content_sha256": "a" * 64,
            "git_head": "abcdef1234567890",
            "raw_path": "raw/web/github.com/WF-repo.tar",
            "created_ts": now(),
        },
    )
    ledger.enqueue(
        store,
        kind="web_extract",
        payload={"fetch_id": "WF-repo", "created_by_launch": launch_id},
        job_id="JOB-webextract-WF-repo",
    )
    assert run_kind(store, "web_extract", "w1")["status"] == "complete"

    row = handlers._row(store, "WF-repo")
    assert row["state"] == "extracted"
    documents = [
        Path(r["raw_path"])
        for r in store.knowledge.execute(
            "SELECT raw_path FROM document WHERE source_id = ?", (row["source_id"],)
        )
    ]
    # The good file landed, and so did the neutralised absolute one — both
    # under the extraction directory and nowhere else.
    assert {p.name for p in documents} == {"README.md", "absolute.md"}
    extraction_root = raw_dir / "WF-repo@abcdef123456"
    for document in documents:
        assert (program_root / document).resolve().is_relative_to(extraction_root.resolve())
    assert not (program_root / "escaped.md").exists()
    assert not (program_root.parent / "escaped.md").exists()
    assert not (tmp_path / "absolute.md").exists()

    event = store.ops.execute(
        "SELECT payload FROM event WHERE type = 'web_fetch_extracted'"
    ).fetchone()
    payload = json.loads(event["payload"])
    assert payload["refused_member_count"] == 1
    assert any("escaped.md" in entry for entry in payload["refused_members"])
