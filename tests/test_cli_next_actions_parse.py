"""Lane FB-1 item F10c: every emitted next action parses, and is emitted
only when the state calls for it.

A ``nextActions`` entry is a promise: an agent is expected to run that argv.
An entry naming a flag that does not exist, a verb that was renamed, or a
placeholder nobody can fill is worse than no entry at all -- it costs the
agent a failed command and teaches it to stop reading the field.

The guard is mechanical: drive the six sampled surfaces over a real fixture
program, collect EVERY emitted action, and parse each one under the shipped
top-level parser.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trialerror.cli import build_parser, budget as cli_budget, events as cli_events
from trialerror.cli import ingest as cli_ingest, inbox as cli_inbox, lit as cli_lit, query as cli_query
from trialerror.budget.pools import book_launch, create_pool
from trialerror.events.api import post_inbox
from trialerror.ingest import pipeline
from trialerror.litapi.client import LitApiClient
from trialerror.litapi.errors import ProviderTransportError
from trialerror.litapi.models import WorkRecord

from tests import _retrieve_fixtures as fx
from tests._budget_fixtures import open_account_session
from tests._ingest_fixtures import bootstrap_launch, write_scanned_pdf_fixture


def parse_action(argv) -> None:
    """Parse one emitted action's argv the way a shell would hand it to the
    console script: drop the program name, parse the rest."""
    argv = list(argv)
    assert argv and argv[0] == "trialerror", argv
    build_parser().parse_args(argv[1:])


def assert_every_action_parses(envelope: dict) -> list:
    actions = envelope.get("nextActions") or []
    for action in actions:
        assert action["kind"] == "shell", action
        parse_action(action["argv"])
        # A placeholder is not a runnable command, however well it parses.
        for token in action["argv"]:
            assert "<" not in str(token), action
    return actions


class _Args:
    def __init__(self, program_root, platform_root, **kw):
        self.program_root = str(program_root)
        self.platform_root = str(platform_root)
        for k, v in kw.items():
            setattr(self, k, v)


# ---------------------------------------------------------------------------
# query search / query stats
# ---------------------------------------------------------------------------


@pytest.fixture()
def corpus(store, program_root):
    fx.build_small_corpus(store)
    store.knowledge.commit()
    (program_root / "trialerror.toml").write_text(
        '[program]\nid = "PROG-na"\n\n[retrieve]\nfulltext_backend = "fts5"\n', encoding="utf-8"
    )
    return store


def _search_args(program_root, platform_root, query, **kw):
    fields = dict(
        query=query, k=5, mode="auto", source_ids=None, kinds=None, license_tiers=None,
        years=None, unfenced=False, launch_id=None,
    )
    fields.update(kw)  # an override replaces the default rather than colliding with it
    return _Args(program_root, platform_root, **fields)


def test_query_search_actions_parse(corpus, program_root, platform_root):
    corpus.close()
    zero = cli_query._run_search(_search_args(program_root, platform_root, "coordinator zzzqqxnothing"))
    assert assert_every_action_parses(zero), "a zero-result search should earn its re-run action"
    hit = cli_query._run_search(_search_args(program_root, platform_root, "coordinator"))
    assert assert_every_action_parses(hit) == [], "a search that returned rows earns no action"


def test_query_search_action_parses_with_a_leading_dash_term(corpus, program_root, platform_root):
    """fix-accept V-3. The guard passed before only because no fixture drove
    a term beginning with "-" -- argparse reads that as an option in the
    positional slot, and the one action this lane invented was unparseable
    for exactly the queries a typo produces. Driven here so the guarantee is
    mechanical rather than incidental."""
    corpus.close()
    env = cli_query._run_search(
        _search_args(program_root, platform_root, "-coordinator zzzqqxnothing", mode="fts")
    )
    actions = assert_every_action_parses(env)
    assert actions, "a zero-result search should earn its re-run action"
    parsed = build_parser().parse_args(actions[0]["argv"][1:])
    assert parsed.query == "-coordinator"


def test_query_search_action_carries_the_flags_that_scope_the_counts(corpus, program_root, platform_root):
    """fix-accept V-2. --launch-id turns a launch's declared slice into a
    forced doc_ids filter and --unfenced changes which chunks are served, so
    the per-term counts that produced this action were computed under both.
    The suggestion has to be asked under both too, or it answers a wider
    question than the one it explains."""
    corpus.close()
    env = cli_query._run_search(
        _search_args(
            program_root, platform_root, "coordinator zzzqqxnothing",
            unfenced=True, launch_id="LNCH-01M29RPX59BD7EK9SN66BZ9GYC",
        )
    )
    argv = assert_every_action_parses(env)[0]["argv"]
    parsed = build_parser().parse_args(argv[1:])
    assert parsed.unfenced is True
    assert parsed.launch_id == "LNCH-01M29RPX59BD7EK9SN66BZ9GYC"
    assert parsed.query == "coordinator"


def test_query_stats_actions_parse(corpus, program_root, platform_root):
    corpus.close()
    env = cli_query._run_stats(_Args(program_root, platform_root))
    assert_every_action_parses(env)


def test_query_stats_suggests_a_rebuild_only_for_the_backend_that_serves(corpus, program_root, platform_root):
    """The action has to be conditional on numbers in the same envelope AND
    on which backend is actually serving -- a program pinned to fts5 reports
    the tantivy index missing by construction, and telling its operator to
    build one would be advising against their own config."""
    from trialerror.retrieve import tantivysearch as tv

    corpus.close()
    pinned_fts5 = cli_query._run_stats(_Args(program_root, platform_root))
    argvs = [a["argv"] for a in assert_every_action_parses(pinned_fts5)]
    assert ["trialerror", "ingest", "reindex-fulltext"] not in argvs

    if tv.tantivy_available():
        (program_root / "trialerror.toml").write_text(
            '[program]\nid = "PROG-na"\n\n[retrieve]\nfulltext_backend = "tantivy"\n',
            encoding="utf-8",
        )
        on_tantivy = cli_query._run_stats(_Args(program_root, platform_root))
        if on_tantivy["result"]["fulltext_backend"] == "tantivy":
            argvs = [a["argv"] for a in assert_every_action_parses(on_tantivy)]
            assert ["trialerror", "ingest", "reindex-fulltext"] in argvs


def test_a_missing_chunk_fts_row_earns_the_checks_own_action(corpus, program_root, platform_root):
    corpus.knowledge.execute("DELETE FROM chunk_fts")
    corpus.knowledge.commit()
    corpus.close()
    env = cli_query._run_stats(_Args(program_root, platform_root))
    argvs = [a["argv"] for a in assert_every_action_parses(env)]
    assert ["trialerror", "doctor", "--only", "fulltext_index_stale"] in argvs


# ---------------------------------------------------------------------------
# budget status
# ---------------------------------------------------------------------------


def test_budget_status_actions_parse(store, program_root, platform_root):
    account_id, session_id = open_account_session(store)
    create_pool(
        store, account_id=account_id, model_class="mid", period="weekly", cap_tokens=100,
        billed_multiplier=2.0, soft_pct=10, hard_pct=100,
    )
    book_launch(
        store, session_id=session_id, program_id="PROG-test", agent_kind="lens", model_class="mid",
        model="sonnet", purpose="mechanical", est_tokens=50,
    )
    store.close()
    env = cli_budget._run_status(_Args(program_root, platform_root, account_id=None, model_class=None))
    argvs = [a["argv"] for a in assert_every_action_parses(env)]
    assert ["trialerror", "budget", "check", "--account-id", account_id] in argvs


def test_budget_status_with_no_pool_suggests_listing_pools(store, program_root, platform_root):
    account_id, session_id = open_account_session(store)
    store.close()
    env = cli_budget._run_status(_Args(program_root, platform_root, account_id=None, model_class=None))
    argvs = [a["argv"] for a in assert_every_action_parses(env)]
    assert ["trialerror", "budget", "pools", "--account-id", account_id] in argvs


def test_a_healthy_pool_earns_no_action(store, program_root, platform_root):
    account_id, session_id = open_account_session(store)
    create_pool(
        store, account_id=account_id, model_class="mid", period="weekly", cap_tokens=1_000_000,
        billed_multiplier=2.0, soft_pct=95, hard_pct=100,
    )
    store.close()
    env = cli_budget._run_status(_Args(program_root, platform_root, account_id=None, model_class=None))
    assert assert_every_action_parses(env) == []


def test_budget_refusal_actions_parse_too(store, program_root, platform_root):
    open_account_session(store, status="closed")
    store.close()
    env = cli_budget._run_status(_Args(program_root, platform_root, account_id=None, model_class=None))
    assert env["ok"] is False
    assert_every_action_parses(env)


# ---------------------------------------------------------------------------
# events tail
# ---------------------------------------------------------------------------


def _tail_args(program_root, **kw):
    args = _Args(program_root, program_root, workpackage=None, session_id=None, event_type=None, limit=20)
    for k, v in kw.items():
        setattr(args, k, v)
    return args


def test_events_tail_actions_parse_and_carry_the_filters(store, program_root, platform_root):
    from trialerror.events.api import append_event

    for i in range(3):
        append_event(store, event_type="fixture_event", payload={"i": i}, workpackage="WP-1")
    store.close()

    full = cli_events.run_tail(_tail_args(program_root, limit=2, workpackage="WP-1"))
    argvs = [a["argv"] for a in assert_every_action_parses(full)]
    assert argvs, "a full window should say there may be more"
    assert "--workpackage" in argvs[0] and "WP-1" in argvs[0]
    assert "--limit" in argvs[0] and "4" in argvs[0]

    partial = cli_events.run_tail(_tail_args(program_root, limit=50, workpackage="WP-1"))
    assert assert_every_action_parses(partial) == []


def test_an_unmatched_type_filter_suggests_the_same_tail_without_it(store, program_root, platform_root):
    store.close()
    env = cli_events.run_tail(_tail_args(program_root, event_type="no_such_type"))
    argvs = [a["argv"] for a in assert_every_action_parses(env)]
    assert argvs and "--type" not in argvs[0]


def test_an_empty_tail_with_no_filter_earns_no_action(store, program_root, platform_root):
    store.close()
    env = cli_events.run_tail(_tail_args(program_root))
    assert assert_every_action_parses(env) == []


# ---------------------------------------------------------------------------
# inbox read
# ---------------------------------------------------------------------------


def test_inbox_read_peek_actions_parse(store, program_root, platform_root):
    post_inbox(store, body="something the user wants read")
    store.close()

    peek = cli_inbox.run_read(_Args(program_root, program_root, session_id=None, no_mark_read=True))
    argvs = [a["argv"] for a in assert_every_action_parses(peek)]
    assert ["trialerror", "inbox", "read", "--program-root", str(program_root)] in argvs

    marked = cli_inbox.run_read(_Args(program_root, program_root, session_id=None, no_mark_read=False))
    assert assert_every_action_parses(marked) == [], "items just marked read need nothing"


def test_an_empty_inbox_peek_earns_no_action(store, program_root, platform_root):
    store.close()
    env = cli_inbox.run_read(_Args(program_root, program_root, session_id=None, no_mark_read=True))
    assert assert_every_action_parses(env) == []


# ---------------------------------------------------------------------------
# ingest status (and ingest add, which the same pass touched)
# ---------------------------------------------------------------------------


def test_ingest_status_and_add_actions_parse(store, program_root, platform_root):
    launch_id = bootstrap_launch(store)
    source = pipeline.register_source(
        store, kind="paper", title="T", license_tier="open", acquisition_route="web",
        registered_by_launch=launch_id,
    )
    raw = program_root / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    scan = write_scanned_pdf_fixture(raw / "scan.pdf")
    store.close()

    add_env = cli_ingest._cmd_add(
        _Args(
            program_root, platform_root, source_id=source["source_id"], path=str(scan),
            launch_id=launch_id, media_type="pdf-scan", yes=False,
        )
    )
    assert len(assert_every_action_parses(add_env)) == 2

    doc_id = add_env["result"]["document"]["doc_id"]
    status_env = cli_ingest._cmd_status(_Args(program_root, platform_root, doc_id=doc_id))
    assert_every_action_parses(status_env)


# ---------------------------------------------------------------------------
# lit search
# ---------------------------------------------------------------------------


class _Stub:
    search_scope = "test"

    def __init__(self, name, *, records=None, error=None):
        self.name = name
        self._records = records or []
        self._error = error

    def get_by_doi(self, doi):
        return None

    def get_by_arxiv(self, arxiv_id):
        return None

    def search(self, query, *, limit=10):
        if self._error:
            raise self._error
        return list(self._records)

    def get_citations(self, identifier, *, limit=100, offset=0):
        raise NotImplementedError


def test_lit_search_actions_parse(monkeypatch, tmp_path):
    class _Args2:
        program_root = None
        platform_root = None
        query = "retry budgets"
        limit = 10

    args = _Args2()
    args.program_root = str(tmp_path)

    failing = LitApiClient([_Stub("openalex", error=ProviderTransportError("HTTP 500", provider="openalex", status_code=500)),
                            _Stub("arxiv", records=[WorkRecord(title="T", arxiv_id="2401.1")])])
    monkeypatch.setattr(cli_lit, "_build_client", lambda a: failing)
    env = cli_lit._cmd_search(args)
    argvs = [a["argv"] for a in assert_every_action_parses(env)]
    assert ["trialerror", "doctor", "--only", "litapi_providers_ready"] in argvs

    empty = LitApiClient([_Stub("openalex"), _Stub("arxiv")])
    monkeypatch.setattr(cli_lit, "_build_client", lambda a: empty)
    env = cli_lit._cmd_search(args)
    argvs = [a["argv"] for a in assert_every_action_parses(env)]
    assert ["trialerror", "lit", "arxiv-semantic", "--q", "retry budgets"] in argvs

    found = LitApiClient([_Stub("openalex", records=[WorkRecord(title="T", doi="10.1/x")])])
    monkeypatch.setattr(cli_lit, "_build_client", lambda a: found)
    assert assert_every_action_parses(cli_lit._cmd_search(args)) == []


# ---------------------------------------------------------------------------
# the sampled surfaces, named -- so a future lane cannot quietly shrink the set
# ---------------------------------------------------------------------------


def test_every_sampled_surface_is_covered_here():
    covered = {
        "query search", "query stats", "budget status", "lit search", "ingest status",
        "events tail", "inbox read",
    }
    source = Path(__file__).read_text(encoding="utf-8")
    for surface in covered:
        group, verb = surface.split()
        assert f"cli_{group}" in source, surface
        assert verb in source, surface
