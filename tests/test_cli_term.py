"""``trialerror term`` CLI group -- argv parsing, envelope shaping, and the
lifecycle round trips, driven end to end through ``trialerror.cli.main``
(the ``tests/test_cli_extract.py`` convention -- ``--program-root`` placed
AFTER the action token; each CLI call opens its own ``Store``, so a test
that first writes fixture rows through ``store``/``lexicon.api`` directly
closes it before invoking the CLI).

Two relation-decision tests (``scoped``, ``not_conflict``) and the
duplicate-merge test open their own ``term_relation`` row directly through
``lexicon.api.open_relation`` rather than relying on a save-time scan.
That was originally because ``lexicon.candidates``/``lexicon.scan`` were not
in this tree (E3 branched from E1 alongside E2); they are here now, and the
direct seeding stays because it pins the exact member set each decision is
asserted against instead of depending on what a scan happened to open.
``test_lexicon_api.py`` seeds its ``decide_relation`` tests the identical
way for the identical reason.
"""

from __future__ import annotations

import json

import pytest

from trialerror.cli import main
from trialerror.lexicon import api as lexicon_api
from trialerror.lexicon import policy
from trialerror.stores.store import open_store
from trialerror.stores.writer import insert
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests._store_fixtures import populate_one_of_everything


def _call(argv, capsys):
    rc = main(argv)
    out = capsys.readouterr().out.strip()
    return rc, json.loads(out)


@pytest.fixture()
def ids(store):
    return populate_one_of_everything(store)


# ---------------------------------------------------------------------------
# registration + no-action
# ---------------------------------------------------------------------------


def test_group_name_and_help_registered():
    from trialerror.cli import term as cli_term

    assert cli_term.GROUP_NAME == "term"
    assert cli_term.HELP


def test_no_action_is_a_structured_error(program_root, platform_root, capsys):
    rc, env = _call(["term", "--program-root", str(program_root)], capsys)
    assert rc == 1
    assert env["error"]["code"] == "no_action"


# ---------------------------------------------------------------------------
# propose / accept / list / show / status / reindex
# ---------------------------------------------------------------------------


def test_propose_accept_show_list_status_reindex_round_trip(store, ids, program_root, platform_root, capsys):
    store.close()  # the CLI opens its own Store per invocation

    rc, env = _call(
        [
            "term", "propose", "--lemma", "quorum", "--gloss", "the minimum agreeing nodes",
            "--evidence", f"anchor:{ids['quote_anchor']}", "--by-launch", ids["launch"],
            "--program-root", str(program_root),
        ],
        capsys,
    )
    assert rc == 0, env
    assert env["result"]["created_term"] is True
    assert env["result"]["status"] == "proposed"
    term_id, sense_id = env["result"]["term_id"], env["result"]["sense_id"]

    rc, env = _call(["term", "accept", sense_id, "--by-launch", ids["launch"], "--program-root", str(program_root)], capsys)
    assert rc == 0, env
    assert env["result"]["status"] == "current"

    rc, env = _call(["term", "show", term_id, "--program-root", str(program_root)], capsys)
    assert rc == 0, env
    assert env["result"]["term"]["lemma"] == "quorum"
    assert env["result"]["term"]["status"] == "active"
    assert len(env["result"]["senses"]) == 1
    assert env["result"]["senses"][0]["status"] == "current"
    assert env["result"]["senses"][0]["evidence"][0]["source_key"] == ids["source"]

    rc, env = _call(["term", "list", "--program-root", str(program_root)], capsys)
    assert rc == 0, env
    assert "quorum" in {t["lemma"] for t in env["result"]["terms"]}

    rc, env = _call(["term", "status", "--program-root", str(program_root)], capsys)
    assert rc == 0, env
    assert env["result"]["senses_by_status"]["current"] >= 1

    rc, env = _call(["term", "reindex", "--program-root", str(program_root)], capsys)
    assert rc == 0, env
    assert env["result"]["terms"] >= 2  # this term + the fixture's own "Test Term"


def test_propose_without_evidence_is_a_structured_error(store, ids, program_root, platform_root, capsys):
    store.close()
    rc, env = _call(
        ["term", "propose", "--lemma", "x", "--gloss", "y", "--by-launch", ids["launch"], "--program-root", str(program_root)],
        capsys,
    )
    assert rc == 1
    assert env["error"]["code"] == "SenseWithoutEvidenceError"


def test_reject_round_trip(store, ids, program_root, platform_root, capsys):
    store.close()
    rc, env = _call(
        [
            "term", "propose", "--lemma", "widget", "--gloss", "a thing", "--origin", "record_import",
            "--origin-ref", ids["record"], "--evidence", f"record:{ids['record']}",
            "--by-launch", ids["launch"], "--program-root", str(program_root),
        ],
        capsys,
    )
    assert rc == 0, env
    sense_id = env["result"]["sense_id"]

    rc, env = _call(
        ["term", "reject", sense_id, "--by-launch", ids["launch"], "--reason", "bad reading", "--program-root", str(program_root)],
        capsys,
    )
    assert rc == 0, env
    assert env["result"]["status"] == "rejected"
    assert env["result"]["reason"] == "bad reading"


def test_supersede_round_trip(store, ids, program_root, platform_root, capsys):
    store.close()
    rc, env = _call(
        [
            "term", "propose", "--lemma", "stress track", "--gloss", "original reading", "--status", "current",
            "--evidence", f"anchor:{ids['quote_anchor']}", "--by-launch", ids["launch"], "--program-root", str(program_root),
        ],
        capsys,
    )
    assert rc == 0, env
    sense_id = env["result"]["sense_id"]

    rc, env = _call(
        ["term", "supersede", sense_id, "--gloss", "corrected reading", "--by-launch", ids["launch"], "--program-root", str(program_root)],
        capsys,
    )
    assert rc == 0, env
    assert env["result"]["status"] == "current"
    assert env["result"]["superseded_sense_id"] == sense_id
    assert env["result"]["sense_id"] != sense_id


def test_retire_sense_round_trip_and_term_id_is_refused(store, ids, program_root, platform_root, capsys):
    store.close()
    rc, env = _call(
        [
            "term", "propose", "--lemma", "ephemeral", "--gloss", "a temp reading",
            "--evidence", f"anchor:{ids['quote_anchor']}", "--by-launch", ids["launch"], "--program-root", str(program_root),
        ],
        capsys,
    )
    assert rc == 0, env
    term_id, sense_id = env["result"]["term_id"], env["result"]["sense_id"]

    rc, env = _call(["term", "retire", sense_id, "--by-launch", ids["launch"], "--program-root", str(program_root)], capsys)
    assert rc == 0, env
    assert env["result"]["status"] == "retired"

    # design §7 names `retire <SENSE|TERM>`; a TERM id is refused with a
    # named error here rather than guessed at -- see cli/term.py's _run_retire.
    rc, env = _call(["term", "retire", term_id, "--by-launch", ids["launch"], "--program-root", str(program_root)], capsys)
    assert rc == 1
    assert env["error"]["code"] == "term_retire_not_implemented"


def test_retire_unknown_id_is_not_found(program_root, platform_root, capsys):
    rc, env = _call(["term", "retire", "SENSE-does-not-exist", "--by-launch", "LNCH-x", "--program-root", str(program_root)], capsys)
    assert rc == 1
    assert env["error"]["code"] == "not_found"


def test_mark_reviewed_round_trip(store, ids, program_root, platform_root, capsys):
    store.close()
    rc, env = _call(
        [
            "term", "propose", "--lemma", "cadence", "--gloss", "a reading", "--status", "current",
            "--evidence", f"anchor:{ids['quote_anchor']}", "--by-launch", ids["launch"], "--program-root", str(program_root),
        ],
        capsys,
    )
    assert rc == 0, env
    sense_id = env["result"]["sense_id"]

    rc, env = _call(
        ["term", "mark-reviewed", sense_id, "--by-launch", ids["launch"], "--program-root", str(program_root)], capsys,
    )
    assert rc == 0, env
    assert env["result"]["review_after"]


# ---------------------------------------------------------------------------
# decide / merge -- pending relations seeded via lexicon.api.open_relation
# directly (see module docstring: the E2 scan/candidates modules that would
# normally open these are not in this tree yet).
# ---------------------------------------------------------------------------


def test_decide_same_as_merges_via_cli(store, ids, program_root, platform_root, capsys):
    first = lexicon_api.propose(
        store, lemma="alpha", gloss="a reading", origin_kind="manual", origin_ref=None,
        evidence=[f"anchor:{ids['quote_anchor']}"], by_launch=ids["launch"], procedure_version="manual-v1",
    )
    second = lexicon_api.propose(
        store, lemma="alpha version two", gloss="another reading", origin_kind="manual", origin_ref=None,
        evidence=[f"anchor:{ids['quote_anchor']}"], by_launch=ids["launch"], procedure_version="manual-v1",
    )
    opened = lexicon_api.open_relation(
        store, src_kind="term", src_id=second["term_id"], dst_kind="term", dst_id=first["term_id"],
        verb="same_as", marked_by_kind="system",
    )
    store.close()

    rc, env = _call(
        ["term", "decide", opened["rel_id"], "--decision", "same_as", "--by-launch", ids["launch"], "--program-root", str(program_root)],
        capsys,
    )
    assert rc == 0, env
    assert env["result"]["merge"]["canonical_term_id"] == first["term_id"]
    assert env["result"]["merge"]["merged_term_id"] == second["term_id"]


def test_decide_scoped_round_trip(store, ids, program_root, platform_root, capsys):
    # Two current senses of the SAME lemma with disjoint source_keys (an
    # anchor's source vs. a record's own register_key) -- the disjointness
    # design §3's conflict rule cares about, without building a second
    # document/source from scratch.
    first = lexicon_api.propose(
        store, lemma="surprise", gloss="reading one", origin_kind="manual", origin_ref=None,
        evidence=[f"anchor:{ids['quote_anchor']}"], by_launch=ids["launch"], procedure_version="manual-v1",
        status="current",
    )
    second = lexicon_api.propose(
        store, lemma="surprise", gloss="reading two", origin_kind="record_import", origin_ref=ids["record"],
        evidence=[f"record:{ids['record']}"], by_launch=ids["launch"], procedure_version=policy.RECORD_IMPORT_PROCEDURE_VERSION,
        status="current",
    )
    assert second["term_id"] == first["term_id"], "same lemma must attach to the same term"
    opened = lexicon_api.open_relation(
        store, src_kind="term", src_id=first["term_id"], dst_kind="term", dst_id=first["term_id"],
        verb="conflicts_with", marked_by_kind="system",
        evidence={"sense_ids": [first["sense_id"], second["sense_id"]]},
    )
    store.close()

    rc, env = _call(
        [
            "term", "decide", opened["rel_id"], "--decision", "scoped",
            "--disambiguator", f"{first['sense_id']}=fate-family reading",
            "--disambiguator", f"{second['sense_id']}=census-import reading",
            "--by-launch", ids["launch"], "--program-root", str(program_root),
        ],
        capsys,
    )
    assert rc == 0, env
    assert set(env["result"]["member_sense_ids"]) == {first["sense_id"], second["sense_id"]}
    assert len(env["result"]["prov_edges"]) == 1


def test_decide_scoped_missing_disambiguator_is_a_structured_error(store, ids, program_root, platform_root, capsys):
    first = lexicon_api.propose(
        store, lemma="grappling", gloss="reading one", origin_kind="manual", origin_ref=None,
        evidence=[f"anchor:{ids['quote_anchor']}"], by_launch=ids["launch"], procedure_version="manual-v1",
        status="current",
    )
    second = lexicon_api.propose(
        store, lemma="grappling", gloss="reading two", origin_kind="record_import", origin_ref=ids["record"],
        evidence=[f"record:{ids['record']}"], by_launch=ids["launch"], procedure_version=policy.RECORD_IMPORT_PROCEDURE_VERSION,
        status="current",
    )
    opened = lexicon_api.open_relation(
        store, src_kind="term", src_id=first["term_id"], dst_kind="term", dst_id=first["term_id"],
        verb="conflicts_with", marked_by_kind="system",
        evidence={"sense_ids": [first["sense_id"], second["sense_id"]]},
    )
    store.close()

    rc, env = _call(
        [
            "term", "decide", opened["rel_id"], "--decision", "scoped",
            "--disambiguator", f"{first['sense_id']}=only one named",
            "--by-launch", ids["launch"], "--program-root", str(program_root),
        ],
        capsys,
    )
    assert rc == 1
    assert env["error"]["code"] == "MissingDisambiguatorError"


def test_decide_not_conflict_round_trip(store, ids, program_root, platform_root, capsys):
    first = lexicon_api.propose(
        store, lemma="cover system", gloss="reading one", origin_kind="manual", origin_ref=None,
        evidence=[f"anchor:{ids['quote_anchor']}"], by_launch=ids["launch"], procedure_version="manual-v1",
        status="current",
    )
    second = lexicon_api.propose(
        store, lemma="cover system", gloss="reading two", origin_kind="record_import", origin_ref=ids["record"],
        evidence=[f"record:{ids['record']}"], by_launch=ids["launch"], procedure_version=policy.RECORD_IMPORT_PROCEDURE_VERSION,
        status="current",
    )
    opened = lexicon_api.open_relation(
        store, src_kind="term", src_id=first["term_id"], dst_kind="term", dst_id=first["term_id"],
        verb="conflicts_with", marked_by_kind="system",
        evidence={"sense_ids": [first["sense_id"], second["sense_id"]]},
    )
    store.close()

    rc, env = _call(
        [
            "term", "decide", opened["rel_id"], "--decision", "not_conflict", "--into", first["sense_id"],
            "--by-launch", ids["launch"], "--program-root", str(program_root),
        ],
        capsys,
    )
    assert rc == 0, env
    assert env["result"]["kept_sense_id"] == first["sense_id"]
    assert env["result"]["superseded_sense_ids"] == [second["sense_id"]]


def test_merge_cli(store, ids, program_root, platform_root, capsys):
    a = lexicon_api.propose(
        store, lemma="beta one", gloss="reading", origin_kind="manual", origin_ref=None,
        evidence=[f"anchor:{ids['quote_anchor']}"], by_launch=ids["launch"], procedure_version="manual-v1",
    )
    b = lexicon_api.propose(
        store, lemma="beta two", gloss="reading", origin_kind="manual", origin_ref=None,
        evidence=[f"anchor:{ids['quote_anchor']}"], by_launch=ids["launch"], procedure_version="manual-v1",
    )
    store.close()

    rc, env = _call(
        ["term", "merge", a["term_id"], "--into", b["term_id"], "--by-launch", ids["launch"], "--program-root", str(program_root)],
        capsys,
    )
    assert rc == 0, env
    assert env["result"]["canonical_term_id"] == b["term_id"]
    assert env["result"]["merged_term_id"] == a["term_id"]


# ---------------------------------------------------------------------------
# scan -- both halves, against the E2 modules the lane merge brought in.
# ---------------------------------------------------------------------------


def test_scan_one_term_runs_both_halves(store, ids, program_root, platform_root, capsys):
    """Before the lane merge these two fields read ``{"status":
    "unavailable"}``: E3 was built on a branch where ``lexicon.scan`` and
    ``lexicon.candidates`` did not exist. They exist now, so the verb is
    asserted on what it actually reports."""
    store.close()
    rc, env = _call(["term", "scan", "--term", ids["term"], "--program-root", str(program_root)], capsys)
    assert rc == 0, env
    assert env["result"]["scanned"] == 1
    scanned = env["result"]["terms"][0]
    assert scanned["term_id"] == ids["term"]
    # one current sense in the fixture -> nothing to be in conflict with,
    # and the scan says so rather than opening an item.
    assert scanned["conflicts"]["status"] == "ok"
    assert scanned["conflicts"]["opened"] is None
    assert "fewer than two" in scanned["conflicts"]["reason"]
    assert isinstance(scanned["duplicates"], list)


def test_scan_all_terms_when_no_term_given(store, ids, program_root, platform_root, capsys):
    store.close()
    rc, env = _call(["term", "scan", "--program-root", str(program_root)], capsys)
    assert rc == 0, env
    assert env["result"]["scanned"] >= 1


def test_scan_unknown_term_is_not_found(store, ids, program_root, platform_root, capsys):
    store.close()
    rc, env = _call(["term", "scan", "--term", "TERM-does-not-exist", "--program-root", str(program_root)], capsys)
    assert rc == 1
    assert env["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# backfill-records / backfill-claims / relink -- the three lexicon.backfill
# verbs. E3 guessed the three function names before E2 had committed to
# them; the merge reconciled ``relink`` -> ``relink_evidence_sources``, so
# these assert the real routes rather than an absent module.
# ---------------------------------------------------------------------------


def test_backfill_records_imports_a_register_row(store, ids, program_root, platform_root, capsys):
    insert(
        store,
        "record",
        {
            "record_id": new_id("REC"),
            "register_key": "handbook-a",
            "artifact_id": ids["artifact"],
            "seq": 2,
            "payload": json.dumps({"name": "settling time", "description": "how long a reading takes to stop moving"}),
            "created_ts": now(),
        },
    )
    store.close()

    rc, env = _call(
        ["term", "backfill-records", "--by-launch", ids["launch"], "--program-root", str(program_root)], capsys,
    )
    assert rc == 0, env
    result = env["result"]
    assert result["status"] == "ok"
    assert result["senses_created"] == 1
    assert result["terms_created"] == 1
    assert "handbook-a" in result["register_keys"]
    # the fixture's own payload-less record is refused per row, not fatally
    assert result["refused_count"] == 1


def test_backfill_records_refuses_a_launch_that_names_no_row(program_root, platform_root, capsys):
    """Ruling L-E4's guard reaches the CLI unchanged: a fictional launch is
    the named XID refusal, never a fallback."""
    rc, env = _call(
        ["term", "backfill-records", "--by-launch", "LNCH-anything", "--program-root", str(program_root)], capsys,
    )
    assert rc == 1
    assert env["error"]["code"] == "record_refused"
    assert "platform.launch" in env["error"]["message"]


def test_backfill_claims_reports_what_is_still_unprojected(store, ids, program_root, platform_root, capsys):
    store.close()
    rc, env = _call(
        ["term", "backfill-claims", "--by-launch", ids["launch"], "--program-root", str(program_root)], capsys,
    )
    assert rc == 0, env
    assert env["result"]["status"] == "ok"
    assert env["result"]["projected"] == 0  # no lemma map supplied: counted, never guessed


def test_relink_rewrites_the_source_key_and_nothing_else(store, ids, program_root, platform_root, tmp_path, capsys):
    """Ruling L-E6's verb, end to end. Before the merge this test could only
    assert that the verb reported itself unavailable -- E3 imported a
    function name (``relink``) E2 had not shipped."""
    proposed = lexicon_api.propose(
        store,
        lemma="settling time",
        gloss="how long a reading takes to stop moving",
        origin_kind="record_import",
        origin_ref=ids["record"],
        evidence=[{"kind": "record", "ref_id": ids["record"]}],
        by_launch=ids["launch"],
        procedure_version=policy.RECORD_IMPORT_PROCEDURE_VERSION,
        status="current",
    )
    before = lexicon_api.evidence_for_sense(store, proposed["sense_id"])[0]
    assert before["source_key"] == "test-register"
    store.close()

    map_path = tmp_path / "map.json"
    map_path.write_text(json.dumps({"test-register": ids["source"]}), encoding="utf-8")
    rc, env = _call(
        [
            "term", "relink", "--map", str(map_path), "--by-launch", ids["launch"],
            "--program-root", str(program_root),
        ],
        capsys,
    )
    assert rc == 0, env
    assert env["result"]["status"] == "ok"
    assert env["result"]["rows_relinked"] == 1

    reopened = open_store(program_root, platform_root=platform_root)
    after = lexicon_api.evidence_for_sense(reopened, proposed["sense_id"])[0]
    assert after["source_key"] == ids["source"]
    for column in ("evidence_id", "anchor_id", "ref_id", "cite_raw", "excerpt", "created_by_launch", "created_ts"):
        assert after[column] == before[column], column
    reopened.close()


def test_relink_refuses_an_empty_map(program_root, platform_root, tmp_path, capsys, store, ids):
    store.close()
    map_path = tmp_path / "map.json"
    map_path.write_text("{}", encoding="utf-8")
    rc, env = _call(
        [
            "term", "relink", "--map", str(map_path), "--by-launch", ids["launch"],
            "--program-root", str(program_root),
        ],
        capsys,
    )
    assert rc == 1
    assert env["error"]["code"] == "InvalidTermInputError"


# ---------------------------------------------------------------------------
# list / show / status / review -- read-only browsing
# ---------------------------------------------------------------------------


def test_list_filters_by_state_and_q(store, ids, program_root, platform_root, capsys):
    store.close()
    rc, env = _call(["term", "list", "--state", "active", "--program-root", str(program_root)], capsys)
    assert rc == 0, env
    assert env["result"]["terms"]
    assert all(t["status"] == "active" for t in env["result"]["terms"])

    rc, env = _call(["term", "list", "--q", "test term", "--program-root", str(program_root)], capsys)
    assert rc == 0, env
    assert any(t["lemma"] == "Test Term" for t in env["result"]["terms"])


def test_show_by_sense_id(store, ids, program_root, platform_root, capsys):
    store.close()
    rc, env = _call(["term", "show", ids["term_sense"], "--program-root", str(program_root)], capsys)
    assert rc == 0, env
    assert env["result"]["sense"]["sense_id"] == ids["term_sense"]
    assert env["result"]["term"]["term_id"] == ids["term"]


def test_show_not_found(program_root, platform_root, capsys):
    rc, env = _call(["term", "show", "TERM-does-not-exist", "--program-root", str(program_root)], capsys)
    assert rc == 1
    assert env["error"]["code"] == "not_found"


def test_status_counts(store, ids, program_root, platform_root, capsys):
    store.close()
    rc, env = _call(["term", "status", "--program-root", str(program_root)], capsys)
    assert rc == 0, env
    result = env["result"]
    assert result["terms_by_status"]["active"] >= 1
    assert result["conflicts_pending"] >= 1  # the fixture's own pending conflicts_with row


def test_review_lists_the_fixtures_own_pending_conflict(store, ids, program_root, platform_root, capsys):
    store.close()
    rc, env = _call(["term", "review", "--program-root", str(program_root)], capsys)
    assert rc == 0, env
    assert "conflicts" in env["result"] and "duplicates" in env["result"] and "stale" in env["result"]
    assert any(c["rel_id"] == ids["term_relation"] for c in env["result"]["conflicts"])


def test_review_kind_filter_returns_only_that_section(store, ids, program_root, platform_root, capsys):
    store.close()
    rc, env = _call(["term", "review", "--kind", "conflict", "--program-root", str(program_root)], capsys)
    assert rc == 0, env
    assert list(env["result"].keys()) == ["conflicts"]


# ---------------------------------------------------------------------------
# scan --rescan -- the withdrawal pass (build step 1c, decision D4)
# ---------------------------------------------------------------------------


def _stale_candidate(store, ids) -> str:
    """One pending system ``same_as`` row the current duplicate gate would
    no longer open, produced the way the real ones were -- two names sharing
    a word that a third name later makes ordinary.

    The six unrelated names in front are what make the bm25 half mean
    anything: in an index of two rows every hit scores right at the floor
    (``tests/test_lexicon_candidates.py`` says why at length)."""
    from tests._lexicon_fixtures import specific_words

    words = specific_words(9)

    def _term(lemma: str) -> None:
        lexicon_api.propose(
            store, lemma=lemma, gloss=f"a reading of {lemma}", origin_kind="manual",
            origin_ref=None, evidence=[f"anchor:{ids['quote_anchor']}"], by_launch=ids["launch"],
            procedure_version=policy.MANUAL_PROCEDURE_VERSION,
        )

    for word in words[3:]:
        _term(word)
    for word in words[:3]:
        _term(f"{word} profile")
    rows = store.knowledge.execute(
        "SELECT rel_id FROM term_relation WHERE verb = 'same_as' AND status = 'pending'"
    ).fetchall()
    assert len(rows) == 1
    return rows[0]["rel_id"]


def test_rescan_withdraws_stale_candidates_through_the_cli(store, ids, program_root, platform_root, capsys):
    rel_id = _stale_candidate(store, ids)
    store.close()

    rc, env = _call(
        ["term", "scan", "--rescan", "--by-launch", ids["launch"], "--program-root", str(program_root)],
        capsys,
    )
    assert rc == 0, env
    assert env["result"]["withdrawn_count"] == 1
    assert env["result"]["examined"] == 1
    assert env["result"]["withdrawn"][0]["rel_id"] == rel_id

    reopened = open_store(program_root, platform_root=platform_root)
    try:
        row = lexicon_api.get_relation(reopened, rel_id)
    finally:
        reopened.close()
    assert row["status"] == "rejected"
    assert row["decided_by_launch"] == ids["launch"]


def test_rescan_dry_run_reports_and_writes_nothing(store, ids, program_root, platform_root, capsys):
    rel_id = _stale_candidate(store, ids)
    store.close()

    rc, env = _call(
        ["term", "scan", "--rescan", "--dry-run", "--by-launch", ids["launch"],
         "--program-root", str(program_root)],
        capsys,
    )
    assert rc == 0, env
    assert env["result"]["dry_run"] is True
    assert env["result"]["withdrawn_count"] == 1

    reopened = open_store(program_root, platform_root=platform_root)
    try:
        assert lexicon_api.get_relation(reopened, rel_id)["status"] == "pending"
    finally:
        reopened.close()


def test_rescan_without_a_launch_is_a_structured_refusal(store, ids, program_root, platform_root, capsys):
    """Withdrawing is an act somebody ran (ruling L-E4). The plain scan needs
    no launch because it only opens system rows; this mode takes them back,
    and the refusal says so and offers the dry run."""
    store.close()
    rc, env = _call(["term", "scan", "--rescan", "--program-root", str(program_root)], capsys)
    assert rc == 1
    assert env["error"]["code"] == "missing_launch"
    assert "--by-launch" in env["error"]["message"]
    assert env["nextActions"], "and it offers the dry run"


def test_rescan_with_an_unknown_launch_is_refused(store, ids, program_root, platform_root, capsys):
    store.close()
    rc, env = _call(
        ["term", "scan", "--rescan", "--by-launch", "LNCH-nope", "--program-root", str(program_root)],
        capsys,
    )
    assert rc == 1
    assert env["error"]["code"] == "record_refused"


def test_a_plain_scan_still_needs_no_launch(store, ids, program_root, platform_root, capsys):
    """The mode is opt-in: nothing about the ordinary scan changed."""
    store.close()
    rc, env = _call(["term", "scan", "--program-root", str(program_root)], capsys)
    assert rc == 0, env
    assert "scanned" in env["result"]


# ---------------------------------------------------------------------------
# the program's [lexicon] knobs actually reach the scan (build step 1d, D1)
# ---------------------------------------------------------------------------


def _write_lexicon_config(program_root, **lexicon) -> None:
    """A minimal ``trialerror.toml`` with a ``[lexicon]`` table -- the same
    two-line shape ``tests/test_cli_law.py`` writes for ``[paths]``."""
    body = '[program]\nid = "demo"\n\n[lexicon]\n'
    for key, value in lexicon.items():
        body += f"{key} = {value}\n"
    (program_root / "trialerror.toml").write_text(body, encoding="utf-8")


def test_rescan_reports_the_fraction_it_used(store, ids, program_root, platform_root, capsys):
    """The dry run's report has to name the knob, not only the threshold it
    produced (build step 1d, D1). An operator sweeping the fraction reads
    this line to find out whether the sweep did anything -- and on the live
    program it did not, because nothing was reading the config at all."""
    _stale_candidate(store, ids)
    store.close()

    rc, env = _call(
        ["term", "scan", "--rescan", "--dry-run", "--by-launch", ids["launch"],
         "--program-root", str(program_root)],
        capsys,
    )
    assert rc == 0, env
    result = env["result"]
    assert result["informative_token_fraction"] == policy.DUPLICATE_INFORMATIVE_TOKEN_FRACTION
    assert result["informative_df_threshold"] == pytest.approx(
        max(
            result["term_count"] * policy.DUPLICATE_INFORMATIVE_TOKEN_FRACTION,
            policy.DUPLICATE_INFORMATIVE_TOKEN_MIN_DF,
        )
    )


def test_rescan_reads_the_programs_own_fraction(store, ids, program_root, platform_root, capsys):
    """**The wiring bug build step 1d exists for.** ``[lexicon]`` is where a
    program overrides the duplicate gate's knobs, and until 1d this CLI
    never loaded ``trialerror.toml`` at all: the library took a ``config``
    argument and the verb passed none, so a sweep of this fraction across
    three values on a live program reported the same threshold every time.

    The fraction here is large enough that the threshold follows it rather
    than the small-store floor, which is what makes the assertion about the
    knob and not about ``DUPLICATE_INFORMATIVE_TOKEN_MIN_DF``."""
    _stale_candidate(store, ids)
    store.close()
    _write_lexicon_config(program_root, duplicate_informative_token_fraction=0.75)

    rc, env = _call(
        ["term", "scan", "--rescan", "--dry-run", "--by-launch", ids["launch"],
         "--program-root", str(program_root)],
        capsys,
    )
    assert rc == 0, env
    result = env["result"]
    assert result["informative_token_fraction"] == 0.75
    assert result["informative_df_threshold"] == pytest.approx(0.75 * result["term_count"])
    assert result["informative_df_threshold"] != pytest.approx(
        result["term_count"] * policy.DUPLICATE_INFORMATIVE_TOKEN_FRACTION
    ), "and it is not the default that was reported"


def test_a_configured_fraction_changes_what_the_rescan_withdraws(
    store, ids, program_root, platform_root, capsys
):
    """And the knob is not merely reported -- it decides. At a fraction that
    makes the shared word informative again the stale pair is KEPT; at the
    default it is withdrawn (the test above this section). Same store, same
    queue, one line of ``trialerror.toml``."""
    _stale_candidate(store, ids)
    store.close()
    _write_lexicon_config(program_root, duplicate_informative_token_fraction=0.75)

    rc, env = _call(
        ["term", "scan", "--rescan", "--dry-run", "--by-launch", ids["launch"],
         "--program-root", str(program_root)],
        capsys,
    )
    assert rc == 0, env
    assert env["result"]["examined"] == 1
    assert env["result"]["withdrawn_count"] == 0
    assert env["result"]["kept"] == 1


def test_a_configured_gloss_cap_reaches_the_propose_verb(
    store, ids, program_root, platform_root, capsys
):
    """The wiring gap was the whole GROUP's, not the scan's alone: every
    knob in ``trialerror.lexicon.policy`` is ``[lexicon]``-overridable and
    none of them could be reached through ``trialerror term``. The gloss cap
    is the one whose effect shows in a single verb's envelope -- a program
    that sets it to 3 refuses a four-word gloss the default 80 accepts."""
    store.close()
    _write_lexicon_config(program_root, gloss_max_words=3)

    rc, env = _call(
        ["term", "propose", "--lemma", "quorum", "--gloss", "the minimum agreeing nodes",
         "--evidence", f"anchor:{ids['quote_anchor']}", "--by-launch", ids["launch"],
         "--program-root", str(program_root)],
        capsys,
    )
    assert rc == 1, env
    assert env["error"]["code"] == "GlossTooLongError"


def _pair_the_plain_scan_must_judge(store, ids) -> tuple[str, str]:
    """Two names sharing one rare word, arranged so the CLI's ``term scan``
    is the thing that decides about them.

    The first goes in through ``propose`` (nothing to find yet, so its
    save-time scan opens nothing); the second goes in as rows, the way
    ``tests._lexicon_fixtures.install_name_corpus`` does and for the same
    reason -- a proposal would run the save-time scan and answer the
    question before the verb under test was ever invoked.

    Six unrelated names go in first, as bare terms: bm25 weighs a hit by how
    rare its tokens are, so in an index of three rows every hit scores right
    at ``DUPLICATE_BM25_FLOOR`` and the first stage never reaches the gate
    at all (``tests/test_lexicon_candidates.py`` says why at length).
    """
    from tests._lexicon_fixtures import specific_words

    stamp = now()
    for decoy in ("guard ring", "noise floor", "burst count", "ramp rate", "cold junction", "marker offset"):
        insert(store, "term", {
            "term_id": new_id("TERM"), "lemma": decoy, "lemma_norm": decoy,
            "granularity": "instance", "tags": None, "entity_id": None, "status": "active",
            "preferred_sense_id": None, "merged_into": None, "created_by_launch": ids["launch"],
            "created_at": stamp, "updated_ts": stamp,
        })

    word = specific_words(1)[0]
    first = lexicon_api.propose(
        store, lemma=f"{word} profile", gloss=f"a reading of {word} profile",
        origin_kind="manual", origin_ref=None, evidence=[f"anchor:{ids['quote_anchor']}"],
        by_launch=ids["launch"], procedure_version=policy.MANUAL_PROCEDURE_VERSION,
        status="current",
    )

    second_id = new_id("TERM")
    insert(store, "term", {
        "term_id": second_id, "lemma": f"{word} interval", "lemma_norm": f"{word} interval",
        "granularity": "instance", "tags": None, "entity_id": None, "status": "active",
        "preferred_sense_id": None, "merged_into": None, "created_by_launch": ids["launch"],
        "created_at": stamp, "updated_ts": stamp,
    })
    insert(store, "term_sense", {
        "sense_id": new_id("SENSE"), "term_id": second_id,
        "gloss": f"a reading of {word} interval", "origin_kind": "manual",
        "procedure_version": policy.MANUAL_PROCEDURE_VERSION, "status": "current",
        "created_at": stamp, "proposed_by_launch": ids["launch"],
    })
    lexicon_api.reindex_all(store)
    assert not store.knowledge.execute(
        "SELECT 1 FROM term_relation WHERE verb = 'same_as'"
    ).fetchall(), "nothing has been asked about this pair yet"
    return first["term_id"], second_id


def _same_as_between(program_root, platform_root, left: str, right: str) -> int:
    reopened = open_store(program_root, platform_root=platform_root)
    try:
        return reopened.knowledge.execute(
            "SELECT count(*) FROM term_relation WHERE verb = 'same_as' AND "
            "((src_id = ? AND dst_id = ?) OR (src_id = ? AND dst_id = ?))",
            (left, right, right, left),
        ).fetchone()[0]
    finally:
        reopened.close()


def test_the_plain_scan_opens_a_candidate_under_the_default_gate(
    store, ids, program_root, platform_root, capsys
):
    """The control for the test below: with no ``[lexicon]`` table the two
    names share a word two of the store's three terms carry, the token route
    fires, and the scan opens the pair."""
    left, right = _pair_the_plain_scan_must_judge(store, ids)
    store.close()

    rc, env = _call(["term", "scan", "--program-root", str(program_root)], capsys)
    assert rc == 0, env
    assert _same_as_between(program_root, platform_root, left, right) == 1


def test_the_plain_scan_opens_under_the_programs_own_gate(
    store, ids, program_root, platform_root, capsys
):
    """The same gap as the rescan's, on the verb that OPENS rows rather than
    taking them back: ``_run_scan`` passed no config either, so a program
    could not change the gate its own scan writes candidates under. With the
    fraction and its small-store floor both set to zero the program has
    declared every word generic, the token route cannot fire, and the pair
    the default gate opens is not opened at all."""
    left, right = _pair_the_plain_scan_must_judge(store, ids)
    store.close()
    _write_lexicon_config(
        program_root,
        duplicate_informative_token_fraction=0.0,
        duplicate_informative_token_min_df=0,
    )

    rc, env = _call(["term", "scan", "--program-root", str(program_root)], capsys)
    assert rc == 0, env
    assert _same_as_between(program_root, platform_root, left, right) == 0
