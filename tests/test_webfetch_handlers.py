"""The research side: enqueue, hand-off, settlement, extraction.

Driven through ``trialerror.jobs.worker.run_one`` — the same claim-run-settle
loop a real detached worker uses — so "parks without burning an attempt" and
"completes on a refusal" are properties of the ledger here, not of a mock.

The settlement table of design §5 is the spine of this file: every row of it
has a test, and each one checks the same three things the operator will ask
about later — what state the ``web_fetch`` row ended in, whether a ``wanted``
request row appeared, and whether the job was completed or parked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._ingest_fixtures import bootstrap_launch
from tests._webfetch_research import FakeSidecar, bootstrap_program, write_program_config
from trialerror.jobs import ledger
from trialerror.jobs.worker import run_one
from trialerror.webfetch import handlers
from trialerror.webfetch.config import WebFetchDisabledError, load_webfetch_config
from trialerror.webfetch.handlers import (
    EXTRACT_JOB_PREFIX,
    FETCH_JOB_PREFIX,
    WebFetchHandlerError,
    enqueue_batch,
    enqueue_fetch,
    fetch_report,
    live_row_for_url,
    recorded_links,
    refresh_fetches,
)
from trialerror.webfetch.links import ListEntry, parse_link_list

URL = "https://example.org/articles/one"
ARTICLE = (
    b"<html lang='en'><head><title>An article</title></head><body><main>"
    b"<h1>An article</h1><p>Body prose that is long enough to be a paragraph.</p>"
    b"</main></body></html>"
)


@pytest.fixture()
def program(store, program_root):
    launch_id = bootstrap_program(store, program_root)
    return launch_id


@pytest.fixture()
def queue_dir(program_root) -> Path:
    return program_root / "webfetch-queue"


@pytest.fixture()
def sidecar(queue_dir) -> FakeSidecar:
    return FakeSidecar(queue_dir)


def come_back_later(store) -> None:
    """Fast-forward past every pending job's backoff.

    A parked ``web_fetch`` job asks the ledger to look again in a minute
    (``PARK_RETRY_DELAY_S``), which is right for the jobs window and wrong for
    a test that would then sleep for a minute. Clearing ``next_attempt_ts`` is
    the smallest honest stand-in for "the worker loop came round again" — it
    moves the clock, not the logic: the job is still pending, still unclaimed,
    and still has to re-derive everything from the queue when it runs.
    """
    with store.jobs:
        store.jobs.execute("UPDATE job SET next_attempt_ts = NULL WHERE state = 'pending'")


def run_until_idle(store, limit: int = 12) -> list[dict]:
    out = []
    for i in range(limit):
        come_back_later(store)
        result = run_one(store, worker_id=f"w{i}")
        out.append(result)
        if result["status"] == "idle":
            break
    return out


def run_kind(store, kind: str, worker: str = "w") -> dict:
    come_back_later(store)
    return run_one(store, worker_id=worker, kinds=[kind])


def add(store, program_root, launch_id, url=URL, **kwargs):
    return enqueue_fetch(store, url=url, launch_id=launch_id, **kwargs)


def row_of(store, fetch_id):
    return handlers._row(store, fetch_id)


def events_of(store, event_type: str) -> list[dict]:
    rows = store.ops.execute("SELECT payload FROM event WHERE type = ? ORDER BY ts", (event_type,)).fetchall()
    return [json.loads(r["payload"]) for r in rows]


# ---------------------------------------------------------------------------
# enqueue — the one door
# ---------------------------------------------------------------------------


def test_add_writes_a_row_a_job_and_an_event(store, program_root, program):
    result = add(store, program_root, program)
    assert result.action == "enqueued"
    assert result.job_id == f"{FETCH_JOB_PREFIX}{result.fetch_id}"

    row = row_of(store, result.fetch_id)
    assert row["state"] == "queued"
    assert row["url_norm"] == URL
    # lane a fix pass (CONT-1): the unprivileged origin is the default.
    assert row["origin"] == "agent"
    assert row["kind"] == "page"
    assert ledger.get_job(store, result.job_id)["kind"] == "web_fetch"
    assert events_of(store, "web_fetch_enqueued")[0]["fetch_id"] == result.fetch_id


def test_the_privileged_origin_needs_a_list_ref_behind_it(store, program_root, program):
    """Design §4 T2's query-stripping control, against the modelled attacker.

    ``operator_list`` keeps the query string and skips the ``agent_daily``
    cap. It used to be a bare manifest field that anything calling this
    function could name — which meant neither control bound the prompt-
    injected agent they were written for. The label now means "off a list a
    human delivered", and a delivered list has a ``list_ref``.
    """
    bare = add(
        store,
        program_root,
        program,
        url="https://example.org/one?keep=me",
        origin="operator_list",
    )
    assert row_of(store, bare.fetch_id)["origin"] == "agent", "claimed, not backed"

    backed = add(
        store,
        program_root,
        program,
        url="https://example.org/two?keep=me",
        origin="operator_list",
        list_ref="sha256:" + "0" * 64 + "/deliveries/wave_0/links.md",
    )
    assert row_of(store, backed.fetch_id)["origin"] == "operator_list"


def test_add_refuses_a_bad_url_without_creating_anything(store, program_root, program):
    result = add(store, program_root, program, url="https://169.254.169.254/latest/meta-data/")
    assert result.action == "refused"
    assert result.reason == "ip_literal"
    assert result.fetch_id is None
    assert store.knowledge.execute("SELECT count(*) AS n FROM web_fetch").fetchone()["n"] == 0
    assert store.jobs.execute("SELECT count(*) AS n FROM job").fetchone()["n"] == 0


def test_add_refuses_an_unbooked_launch(store, program_root, program):
    """Design §4 T7: every fetch is attributable to a real launch."""
    with pytest.raises(WebFetchHandlerError, match="not booked"):
        add(store, program_root, "LNCH-never-booked")


def test_add_is_refused_while_webfetch_is_disabled(store, program_root):
    """C-0069's gate. Off is the default, and turning it on is a decision
    about egress rather than a formality."""
    launch_id = bootstrap_launch(store)
    write_program_config(program_root, enabled=False)
    with pytest.raises(WebFetchDisabledError):
        add(store, program_root, launch_id)


def test_a_second_add_of_the_same_url_dedups(store, program_root, program):
    first = add(store, program_root, program)
    second = add(store, program_root, program)
    assert second.action == "dedup"
    assert second.fetch_id == first.fetch_id
    assert store.knowledge.execute("SELECT count(*) AS n FROM web_fetch").fetchone()["n"] == 1


def test_dedup_is_on_the_canonical_url_not_the_typed_one(store, program_root, program):
    add(store, program_root, program, url="https://example.org/a?utm_source=x#section")
    second = add(store, program_root, program, url="https://example.org/a")
    assert second.action == "dedup"


def test_retry_refuses_to_supersede_a_row_still_in_flight(store, program_root, program):
    first = add(store, program_root, program)
    again = add(store, program_root, program, retry=True)
    assert again.action == "dedup"
    assert "in flight" in (again.detail or "")
    assert row_of(store, first.fetch_id)["superseded_by"] is None


def test_retry_supersedes_a_settled_row_and_keeps_it(store, program_root, program, sidecar):
    first = add(store, program_root, program)
    run_kind(store, "web_fetch")
    sidecar.refuse("paywalled")
    run_kind(store, "web_fetch", "w2")
    assert row_of(store, first.fetch_id)["state"] == "refused"

    again = add(store, program_root, program, retry=True)
    assert again.action == "superseded"
    assert row_of(store, first.fetch_id)["superseded_by"] == again.fetch_id
    assert row_of(store, first.fetch_id)["reason"] == "paywalled"  # history kept
    assert live_row_for_url(store, URL)["fetch_id"] == again.fetch_id


def test_http_is_left_for_the_sidecar_to_judge(store, program_root, program):
    """Whether plain HTTP is acceptable is a PER-HOST flag, and per-host
    policy lives on the host, read-only in the sidecar and absent from
    /workspace (ruling L-A2). This side cannot see it and must not pretend
    to: the manifest goes out, and the sidecar refuses ``scheme_not_allowed``
    if the host has no ``http`` flag."""
    result = add(store, program_root, program, url="http://intranet.example/doc")
    assert result.action == "enqueued"


def test_the_kind_is_inferred_from_the_url_shape(store, program_root, program):
    git = add(store, program_root, program, url="https://github.com/an-owner/a-repo")
    pdf = add(store, program_root, program, url="https://example.org/paper.pdf")
    page = add(store, program_root, program, url="https://example.org/page")
    assert row_of(store, git.fetch_id)["kind"] == "git"
    assert row_of(store, pdf.fetch_id)["kind"] == "pdf"
    assert row_of(store, page.fetch_id)["kind"] == "page"


# ---------------------------------------------------------------------------
# batch
# ---------------------------------------------------------------------------

LIST_MD = """# Requested links for research

Some prose the parser must ignore.

- [Deterministic lockstep](https://example.org/wiki/deterministic\\_lockstep)
- <https://docs.example.org/tutorials/> tier=open
- https://github.com/an-owner/a-repo kind=git
- [A paper](https://example.org/paper.pdf)
- [Again](https://example.org/wiki/deterministic\\_lockstep)
"""


def test_batch_enqueues_a_delivered_list_once(store, program_root, program, tmp_path):
    list_path = tmp_path / "links.md"
    list_path.write_text(LIST_MD, encoding="utf-8")
    entries = parse_link_list(LIST_MD)
    results = enqueue_batch(
        store, entries, launch_id=program, list_ref="sha256:abc/links.md"
    )
    actions = [r.action for r in results]
    assert actions.count("enqueued") == 4
    assert actions.count("dedup") == 1  # the repeated line
    rows = handlers.fetch_rows(store, list_ref="sha256:abc/links.md")
    assert len(rows) == 4
    assert {r["kind"] for r in rows} == {"page", "git", "pdf"}
    assert "deterministic_lockstep" in {r["url_norm"].rsplit("/", 1)[-1] for r in rows}


def test_re_running_the_same_batch_enqueues_nothing(store, program_root, program):
    entries = parse_link_list(LIST_MD)
    enqueue_batch(store, entries, launch_id=program, list_ref="L1")
    second = enqueue_batch(store, entries, launch_id=program, list_ref="L1")
    assert {r.action for r in second} == {"dedup"}
    assert store.knowledge.execute("SELECT count(*) AS n FROM web_fetch").fetchone()["n"] == 4


def test_a_list_line_tag_sets_the_license_tier(store, program_root, program):
    entries = [ListEntry(url="https://example.org/x", line_no=1, raw_line="", license_tier="open")]
    result = enqueue_batch(store, entries, launch_id=program, list_ref=None)[0]
    assert row_of(store, result.fetch_id)["license_detected"] == "operator:open"


# ---------------------------------------------------------------------------
# the fetch handler: hand-off and parking
# ---------------------------------------------------------------------------


def test_the_first_run_writes_a_manifest_and_parks_without_burning_an_attempt(
    store, program_root, program, queue_dir, sidecar
):
    result = add(store, program_root, program)
    outcome = run_kind(store, "web_fetch")

    assert outcome["status"] == "deferred"
    job = ledger.get_job(store, result.job_id)
    assert job["attempts"] == 0, "an absent sidecar is not the job's fault"
    assert job["failure_class"] == "environmental"
    assert sidecar.pending() == [result.job_id]
    assert row_of(store, result.fetch_id)["state"] == "pending"


def test_the_manifest_says_only_what_the_agent_side_may_say(
    store, program_root, program, sidecar
):
    """Design §2.3: the manifest is the one structure this side hands to the
    process with egress. No header, no method, no body — and the schema is
    closed, so a future field cannot be smuggled in either."""
    result = add(store, program_root, program)
    run_kind(store, "web_fetch")
    manifest = sidecar.manifest_for(result.job_id)
    assert set(manifest) == {
        "schema", "job_id", "fetch_id", "launch_id", "program_id", "url", "origin",
        "list_ref", "kind", "conditional", "robots_override_ruling", "created_ts",
    }
    assert manifest["url"] == URL
    assert manifest["robots_override_ruling"] is None


def test_a_second_run_writes_no_second_manifest(store, program_root, program, sidecar, queue_dir):
    result = add(store, program_root, program)
    run_kind(store, "web_fetch")
    before = (queue_dir / "pending" / f"{result.job_id}.json").read_bytes()
    run_kind(store, "web_fetch", "w2")
    assert (queue_dir / "pending" / f"{result.job_id}.json").read_bytes() == before


def test_the_handler_parks_while_webfetch_is_switched_off_mid_flight(
    store, program_root, program
):
    result = add(store, program_root, program)
    write_program_config(program_root, enabled=False)
    outcome = run_kind(store, "web_fetch")
    assert outcome["status"] == "deferred"
    assert ledger.get_job(store, result.job_id)["attempts"] == 0


# ---------------------------------------------------------------------------
# settlement table (design §5)
# ---------------------------------------------------------------------------


def _fetch_and_settle(store, program_root, launch_id, sidecar, **serve_kwargs):
    result = add(store, program_root, launch_id, **serve_kwargs.pop("add_kwargs", {}))
    run_kind(store, "web_fetch")
    sidecar.serve(**serve_kwargs)
    settle = run_kind(store, "web_fetch", "w2")
    return result, settle


def test_a_fetched_page_lands_on_disk_with_its_provenance(
    store, program_root, program, sidecar, queue_dir
):
    result, settle = _fetch_and_settle(store, program_root, program, sidecar, body=ARTICLE)
    assert settle["status"] == "complete"

    row = row_of(store, result.fetch_id)
    assert row["state"] == "fetched"
    assert row["outcome"] == "fetched"
    assert row["content_class"] == "html"
    assert row["bytes"] == len(ARTICLE)

    raw = program_root / row["raw_path"]
    assert raw.read_bytes() == ARTICLE
    provenance = json.loads((program_root / row["provenance_path"]).read_text(encoding="utf-8"))
    assert provenance["fetch_id"] == result.fetch_id
    assert provenance["sidecar_version"]
    # the done/ directory is swept once the bytes are ours
    assert not (queue_dir / "done" / result.job_id).exists()
    # and the next stage is queued
    assert ledger.get_job(store, f"{EXTRACT_JOB_PREFIX}{result.fetch_id}")["kind"] == "web_extract"


def test_a_result_whose_sha_does_not_match_its_bytes_is_never_ingested(
    store, program_root, program, sidecar
):
    """The queue is writable from both sides (design §8). A published result
    that does not describe the bytes beside it means the two sides disagree,
    and the conservative reading is that neither number can be trusted."""
    result = add(store, program_root, program)
    run_kind(store, "web_fetch")
    sidecar.serve(body=ARTICLE, corrupt_sha=True)
    settle = run_kind(store, "web_fetch", "w2")

    assert settle["status"] in ("failed", "deferred")
    assert ledger.get_job(store, result.job_id)["failure_class"] == "logic"
    assert row_of(store, result.fetch_id)["state"] == "pending"
    assert store.knowledge.execute("SELECT count(*) AS n FROM source").fetchone()["n"] == 0


def test_a_result_whose_size_does_not_match_is_refused_too(store, program_root, program, sidecar):
    result = add(store, program_root, program)
    run_kind(store, "web_fetch")
    sidecar.serve(body=ARTICLE, corrupt_size=True)
    run_kind(store, "web_fetch", "w2")
    assert ledger.get_job(store, result.job_id)["failure_class"] == "logic"


@pytest.mark.parametrize("reason", ["paywalled", "bot_challenge", "robots_disallow", "host_not_allowed"])
def test_a_human_fixable_refusal_completes_and_files_a_wanted_row(
    store, program_root, program, sidecar, reason
):
    result = add(store, program_root, program)
    run_kind(store, "web_fetch")
    sidecar.refuse(reason)
    settle = run_kind(store, "web_fetch", "w2")

    assert settle["status"] == "complete", "a refusal is a result, never a retry into the same wall"
    row = row_of(store, result.fetch_id)
    assert (row["state"], row["reason"]) == ("refused", reason)

    source = store.knowledge.execute("SELECT * FROM source WHERE request_state='wanted'").fetchone()
    assert source is not None
    assert source["kind"] == "web"
    assert source["acquisition_route"] == "user_delivered"
    assert source["url"] == URL
    assert "no bypass was attempted" in source["rights_notes"]
    assert reason in source["rights_notes"]
    assert (program_root / "requests" / "REQUESTS.md").is_file()
    assert row["source_id"] == source["source_id"]


@pytest.mark.parametrize("reason", ["ip_private", "redirect_off_allowlist", "url_too_long", "daily_cap"])
def test_a_policy_refusal_leaves_an_audit_trail_but_no_request_row(
    store, program_root, program, sidecar, reason
):
    """Nobody is going to hand-deliver the page at ``169.254.169.254``. A
    policy refusal is recorded and completed, and the request queue stays
    clean so the rows in it are ones an operator can actually act on."""
    result = add(store, program_root, program)
    run_kind(store, "web_fetch")
    sidecar.refuse(reason)
    settle = run_kind(store, "web_fetch", "w2")

    assert settle["status"] == "complete"
    assert row_of(store, result.fetch_id)["reason"] == reason
    assert store.knowledge.execute("SELECT count(*) AS n FROM source").fetchone()["n"] == 0
    assert events_of(store, "web_fetch_refused")[0]["reason"] == reason


def test_a_refusal_published_into_done_settles_the_same_way(store, program_root, program, sidecar):
    """Which directory the sidecar chose is not the interesting fact; the
    reason is."""
    result = add(store, program_root, program)
    run_kind(store, "web_fetch")
    sidecar.serve(outcome="refused", reason="timeout", body=b"", http_status=None, into_done=True)
    settle = run_kind(store, "web_fetch", "w2")
    assert settle["status"] == "complete"
    assert row_of(store, result.fetch_id)["reason"] == "timeout"


def test_a_304_records_the_check_and_produces_no_document(store, program_root, program, sidecar):
    result = add(store, program_root, program)
    run_kind(store, "web_fetch")
    sidecar.unchanged()
    settle = run_kind(store, "web_fetch", "w2")

    assert settle["status"] == "complete"
    row = row_of(store, result.fetch_id)
    assert (row["state"], row["http_status"]) == ("unchanged", 304)
    assert row["doc_id"] is None
    assert ledger.get_job(store, f"{EXTRACT_JOB_PREFIX}{result.fetch_id}") is None


def test_only_one_wanted_row_appears_however_many_times_a_url_is_refused(
    store, program_root, program, sidecar
):
    for _ in range(2):
        result = add(store, program_root, program, retry=True)
        run_kind(store, "web_fetch")
        sidecar.refuse("paywalled")
        run_kind(store, "web_fetch", "w2")
    assert store.knowledge.execute(
        "SELECT count(*) AS n FROM source WHERE request_state='wanted'"
    ).fetchone()["n"] == 1


# ---------------------------------------------------------------------------
# the extract handler
# ---------------------------------------------------------------------------


def _fetch_page(store, program_root, launch_id, sidecar, body=ARTICLE, url=URL, **serve):
    result = add(store, program_root, launch_id, url=url)
    run_kind(store, "web_fetch")
    sidecar.serve(body=body, **serve)
    run_kind(store, "web_fetch", "w2")
    return result


def test_extraction_registers_a_source_and_a_document(store, program_root, program, sidecar):
    result = _fetch_page(store, program_root, program, sidecar)
    settle = run_kind(store, "web_extract", "w3")
    assert settle["status"] == "complete"

    row = row_of(store, result.fetch_id)
    assert row["state"] == "extracted"
    assert row["title"] == "An article"
    assert row["extracted_words"] > 0
    assert row["extractor_version"]

    source = store.knowledge.execute(
        "SELECT * FROM source WHERE source_id = ?", (row["source_id"],)
    ).fetchone()
    assert source["kind"] == "web"
    assert source["acquisition_route"] == "web"
    assert source["request_state"] == "delivered"
    assert result.fetch_id in source["rights_notes"]

    document = store.knowledge.execute(
        "SELECT * FROM document WHERE doc_id = ?", (row["doc_id"],)
    ).fetchone()
    assert document["media_type"] == "html"
    assert (program_root / row["clean_path"]).is_file()
    assert (program_root / row["markdown_path"]).is_file()


def test_the_clean_html_is_what_gets_ingested_not_the_markdown(store, program_root, program, sidecar):
    """Design §2.2: an md round-trip would lose the ``Table`` elements the
    chunker's table isolation fires on, so the ingest input is the clean
    HTML and the markdown is an output beside it."""
    result = _fetch_page(store, program_root, program, sidecar)
    run_kind(store, "web_extract", "w3")
    row = row_of(store, result.fetch_id)
    document = store.knowledge.execute(
        "SELECT raw_path FROM document WHERE doc_id = ?", (row["doc_id"],)
    ).fetchone()
    assert document["raw_path"] == row["clean_path"]
    assert document["raw_path"].endswith(".clean.html")


def test_the_whole_pipeline_runs_to_an_indexed_document(store, program_root, program, sidecar):
    result = _fetch_page(store, program_root, program, sidecar)
    run_until_idle(store)
    row = row_of(store, result.fetch_id)
    document = store.knowledge.execute(
        "SELECT status, sha256 FROM document WHERE doc_id = ?", (row["doc_id"],)
    ).fetchone()
    assert document["status"] == "indexed"

    chunks = store.knowledge.execute(
        "SELECT count(*) AS n FROM chunk WHERE doc_id = ?", (row["doc_id"],)
    ).fetchone()["n"]
    assert chunks > 0
    anchors = store.knowledge.execute(
        "SELECT count(*) AS n FROM quote_anchor WHERE doc_id = ? AND doc_sha256 = ?",
        (row["doc_id"], document["sha256"]),
    ).fetchone()["n"]
    assert anchors == chunks, "every chunk's anchor must still resolve against the document"


def test_report_counts_chunks_and_anchors_without_quoting_the_page(
    store, program_root, program, sidecar
):
    result = _fetch_page(store, program_root, program, sidecar)
    run_until_idle(store)
    report = fetch_report(store)
    line = next(r for r in report if r["fetchId"] == result.fetch_id)
    assert line["documentStatus"] == "indexed"
    assert line["chunks"] > 0
    assert line["anchorsOk"] == line["chunks"]
    assert "Body prose" not in json.dumps(report)


THIN_JS = (
    b"<html><head><title>Loading</title></head><body><div id='root'></div>"
    b"<script id='__NEXT_DATA__'>{}</script></body></html>"
)


def test_a_javascript_shell_becomes_a_wanted_row_rather_than_a_document(
    store, program_root, program, sidecar
):
    result = _fetch_page(store, program_root, program, sidecar, body=THIN_JS)
    settle = run_kind(store, "web_extract", "w3")
    assert settle["status"] == "complete"

    row = row_of(store, result.fetch_id)
    assert (row["state"], row["reason"]) == ("refused", "needs_render")
    assert row["doc_id"] is None
    wanted = store.knowledge.execute("SELECT * FROM source WHERE request_state='wanted'").fetchone()
    assert wanted is not None and "needs_render" in wanted["rights_notes"]


SHORT_PAGE = b"<html><body><main><p>Three sentences is a page too.</p></main></body></html>"


def test_a_short_page_without_js_markers_is_ingested_anyway(store, program_root, program, sidecar):
    """Design §5: thin WITHOUT a JS marker is a short page, and a short page
    is still data."""
    result = _fetch_page(store, program_root, program, sidecar, body=SHORT_PAGE)
    run_kind(store, "web_extract", "w3")
    row = row_of(store, result.fetch_id)
    assert row["state"] == "extracted"
    assert row["thin_content"] == 1
    assert row["doc_id"] is not None


def test_the_same_content_under_two_urls_becomes_one_document(store, program_root, program, sidecar):
    """Design §5: content dedup on the EXTRACTED hash — raw HTML carries
    nonces, the cleaned article does not."""
    first = _fetch_page(store, program_root, program, sidecar)
    run_kind(store, "web_extract", "w3")
    second = _fetch_page(
        store, program_root, program, sidecar, url="https://example.org/mirror/one"
    )
    run_kind(store, "web_extract", "w4")

    first_row, second_row = row_of(store, first.fetch_id), row_of(store, second.fetch_id)
    assert second_row["state"] == "extracted"
    assert second_row["source_id"] == first_row["source_id"]
    assert store.knowledge.execute("SELECT count(*) AS n FROM document").fetchone()["n"] == 1
    assert events_of(store, "web_fetch_dedup")[0]["fetch_id"] == second.fetch_id


def test_recorded_links_are_printable_and_lead_to_no_job(store, program_root, program, sidecar):
    page = (
        b"<html><body><main><p>See <a href='https://other.example/next'>this</a>.</p>"
        b"<p>Also <a href='/local'>local</a>.</p></main></body></html>"
    )
    result = _fetch_page(store, program_root, program, sidecar, body=page)
    jobs_before = store.jobs.execute("SELECT count(*) AS n FROM job").fetchone()["n"]
    run_kind(store, "web_extract", "w3")

    links = recorded_links(store, result.fetch_id)
    assert "https://other.example/next" in links
    assert "https://example.org/local" in links
    # exactly one new job: this fetch's own normalize. No link became a fetch.
    after = store.jobs.execute("SELECT count(*) AS n FROM job").fetchone()["n"]
    assert after == jobs_before + 1
    assert store.knowledge.execute("SELECT count(*) AS n FROM web_fetch").fetchone()["n"] == 1


def test_an_operator_license_tag_outranks_the_page(store, program_root, program, sidecar):
    cc = (
        b"<html><head><title>t</title>"
        b"<link rel='license' href='https://creativecommons.org/licenses/by/4.0/'>"
        b"</head><body><main><p>text</p></main></body></html>"
    )
    tagged = add(store, program_root, program, license_tier="commercial_restricted")
    run_kind(store, "web_fetch")
    sidecar.serve(body=cc)
    run_kind(store, "web_fetch", "w2")
    run_kind(store, "web_extract", "w3")

    row = row_of(store, tagged.fetch_id)
    source = store.knowledge.execute(
        "SELECT license_tier FROM source WHERE source_id = ?", (row["source_id"],)
    ).fetchone()
    assert source["license_tier"] == "commercial_restricted"
    assert row["license_detected"] == "operator:commercial_restricted"


def test_a_declared_cc_license_reads_as_open_when_the_operator_said_nothing(
    store, program_root, program, sidecar
):
    cc = (
        b"<html><head><title>t</title>"
        b"<link rel='license' href='https://creativecommons.org/licenses/by/4.0/'>"
        b"</head><body><main><p>text</p></main></body></html>"
    )
    result = _fetch_page(store, program_root, program, sidecar, body=cc)
    run_kind(store, "web_extract", "w3")
    row = row_of(store, result.fetch_id)
    source = store.knowledge.execute(
        "SELECT license_tier FROM source WHERE source_id = ?", (row["source_id"],)
    ).fetchone()
    assert source["license_tier"] == "open"


TDM_PAGE = (
    b"<html><head><title>t</title><meta name='robots' content='noai'>"
    b"</head><body><main><p>text</p></main></body></html>"
)


def test_a_tdm_optout_is_recorded_and_by_default_not_enforced(store, program_root, program, sidecar):
    """Ruling L-A3: ``honor_tdm_optout`` is false under the internal-research
    posture. Recorded regardless, on every fetch."""
    result = _fetch_page(store, program_root, program, sidecar, body=TDM_PAGE)
    run_kind(store, "web_extract", "w3")
    row = row_of(store, result.fetch_id)
    assert json.loads(row["tdm_signals_json"])["optout"] is True
    assert row["state"] == "extracted"


def test_turning_the_tdm_knob_on_refuses_the_page(store, program_root, tmp_path, sidecar):
    launch_id = bootstrap_launch(store)
    write_program_config(program_root, extra="honor_tdm_optout = true\n")
    result = add(store, program_root, launch_id)
    run_kind(store, "web_fetch")
    sidecar.serve(body=TDM_PAGE)
    run_kind(store, "web_fetch", "w2")
    settle = run_kind(store, "web_extract", "w3")

    assert settle["status"] == "complete"
    row = row_of(store, result.fetch_id)
    assert (row["state"], row["reason"]) == ("refused", "tdm_optout")
    assert row["doc_id"] is None


def test_extraction_is_idempotent_on_a_resumed_job(store, program_root, program, sidecar):
    """A worker killed after ``add_document`` but before settling re-runs the
    whole handler on resume. It must not register the page twice."""
    result = _fetch_page(store, program_root, program, sidecar)
    run_kind(store, "web_extract", "w3")
    documents_before = store.knowledge.execute("SELECT count(*) AS n FROM document").fetchone()["n"]
    sources_before = store.knowledge.execute("SELECT count(*) AS n FROM source").fetchone()["n"]

    # Put the settled job back as a killed worker would have left it.
    with store.jobs:
        store.jobs.execute(
            "UPDATE job SET state='pending', claimed_by=NULL, settled_ts=NULL, "
            "next_attempt_ts=NULL, lease_expires_ts=NULL WHERE job_id = ?",
            (f"{EXTRACT_JOB_PREFIX}{result.fetch_id}",),
        )
    assert run_kind(store, "web_extract", "w9")["status"] == "complete"

    assert store.knowledge.execute("SELECT count(*) AS n FROM document").fetchone()["n"] == (
        documents_before
    )
    assert store.knowledge.execute("SELECT count(*) AS n FROM source").fetchone()["n"] == (
        sources_before
    )


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------


def test_refresh_supersedes_and_carries_the_conditional_headers(
    store, program_root, program, sidecar
):
    first = _fetch_page(
        store, program_root, program, sidecar, headers={"etag": '"v1"', "last-modified": "Mon"}
    )
    run_kind(store, "web_extract", "w3")

    [result] = refresh_fetches(store, launch_id=program, fetch_ids=[first.fetch_id])
    assert result.action == "superseded"
    run_kind(store, "web_fetch", "w5")
    manifest = sidecar.manifest_for(result.job_id)
    assert manifest["conditional"] == {"etag": '"v1"', "last_modified": "Mon"}


def test_refresh_leaves_a_row_that_is_still_working(store, program_root, program):
    first = add(store, program_root, program)
    [result] = refresh_fetches(store, launch_id=program, fetch_ids=[first.fetch_id])
    assert result.action == "dedup"
    assert "still queued" in (result.detail or "")


def test_refresh_older_than_skips_a_recent_row(store, program_root, program, sidecar):
    first = _fetch_page(store, program_root, program, sidecar)
    run_kind(store, "web_extract", "w3")
    assert refresh_fetches(store, launch_id=program, all_rows=True, older_than_s=86400) == []
    assert refresh_fetches(store, launch_id=program, all_rows=True, older_than_s=0)


def test_refresh_of_an_unknown_fetch_says_so(store, program_root, program):
    with pytest.raises(WebFetchHandlerError, match="no such fetch"):
        refresh_fetches(store, launch_id=program, fetch_ids=["WF-nope"])


# ---------------------------------------------------------------------------
# reading back
# ---------------------------------------------------------------------------


def test_fetch_rows_hides_superseded_rows_unless_asked(store, program_root, program, sidecar):
    first = add(store, program_root, program)
    run_kind(store, "web_fetch")
    sidecar.refuse("paywalled")
    run_kind(store, "web_fetch", "w2")
    add(store, program_root, program, retry=True)

    assert len(handlers.fetch_rows(store)) == 1
    assert len(handlers.fetch_rows(store, include_superseded=True)) == 2
    assert handlers.fetch_rows(store, include_superseded=True)[0]["fetch_id"] == first.fetch_id


def test_recorded_links_of_an_unknown_fetch_says_so(store, program_root, program):
    with pytest.raises(WebFetchHandlerError, match="no such fetch"):
        recorded_links(store, "WF-nope")


def test_config_defaults_are_off_and_sandbox_safe(store, program_root):
    cfg = load_webfetch_config({})
    assert cfg.enabled is False
    assert cfg.mode == "allowlist"
    assert cfg.require_sidecar is True
