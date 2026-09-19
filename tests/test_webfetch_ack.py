"""``trialerror webfetch ack`` / ``acks`` and :mod:`trialerror.webfetch.acks`.

The property under test is not "a flag can be set". It is that the ONLY way
to retire a ``webfetch_unattributed`` finding leaves more evidence than it
found: the audit copy is untouched, an ``ops.meta`` row names the launch and
the note, an event row puts the act in the timeline, and ``acks`` lists every
one of them back. An acknowledgement that could be made anonymously, or made
without saying why, or made and then not listable, would be a way to make a
security finding disappear — so each of those three is its own test.
"""

from __future__ import annotations

import json

import pytest

from tests._ingest_fixtures import bootstrap_launch
from tests._webfetch_research import write_program_config
from trialerror.webfetch import acks as acks_mod
from trialerror.cli import webfetch as cli_webfetch


class Args:
    """The namespace argparse would have built."""

    def __init__(self, program_root, platform_root, **kw):
        self.program_root = str(program_root)
        self.platform_root = str(platform_root)
        for key, value in kw.items():
            setattr(self, key, value)


@pytest.fixture()
def cli(store, program_root, platform_root):
    """A configured program with its store closed — every verb opens its own."""
    write_program_config(program_root)
    launch_id = bootstrap_launch(store)
    store.close()

    def make(**kw) -> Args:
        return Args(program_root, platform_root, **kw)

    return launch_id, make


@pytest.fixture()
def reopened(program_root, platform_root):
    from trialerror.stores.store import open_store

    opened = open_store(program_root, platform_root=platform_root)
    yield opened
    opened.close()


def ack_args(make, launch_id, *, fetch_ids=(), job_ids=(), note="H-attrib runbook forgery"):
    return make(
        fetch_ids=list(fetch_ids),
        job_ids=list(job_ids),
        launch_id=launch_id,
        note=note,
    )


# ---------------------------------------------------------------------------
# what a good acknowledgement writes
# ---------------------------------------------------------------------------


def test_ack_writes_one_meta_row_per_id_and_names_the_launch(cli, reopened):
    launch_id, make = cli
    env = cli_webfetch._cmd_ack(
        ack_args(make, launch_id, fetch_ids=["WF-forged", "WF-forged2"])
    )
    assert env["ok"] is True
    assert env["result"]["new"] == 2
    assert env["result"]["alreadyAcknowledged"] == 0
    assert env["result"]["auditUnchanged"] is True

    rows = reopened.ops.execute(
        "SELECT key, value FROM meta WHERE key LIKE 'webfetch.ack.%' ORDER BY key"
    ).fetchall()
    assert [r["key"] for r in rows] == ["webfetch.ack.WF-forged", "webfetch.ack.WF-forged2"]
    stored = json.loads(rows[0]["value"])
    assert stored["launch_id"] == launch_id
    assert stored["note"] == "H-attrib runbook forgery"
    assert stored["kind"] == "fetch"
    assert stored["ts"]


def test_ack_records_the_kind_the_flag_named(cli, reopened):
    launch_id, make = cli
    cli_webfetch._cmd_ack(
        ack_args(make, launch_id, fetch_ids=["WF-a"], job_ids=["JOB-webfetch-WF-b"])
    )
    kinds = {
        r["key"]: json.loads(r["value"])["kind"]
        for r in reopened.ops.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'webfetch.ack.%'"
        ).fetchall()
    }
    assert kinds == {
        "webfetch.ack.WF-a": "fetch",
        "webfetch.ack.JOB-webfetch-WF-b": "job",
    }


def test_ack_appends_a_type_keyed_event_attributed_to_the_launch(cli, reopened):
    launch_id, make = cli
    cli_webfetch._cmd_ack(ack_args(make, launch_id, fetch_ids=["WF-forged"]))
    rows = reopened.ops.execute(
        "SELECT launch_id, payload FROM event WHERE type = 'webfetch_ack'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["launch_id"] == launch_id
    payload = json.loads(rows[0]["payload"])
    assert payload["id"] == "WF-forged"
    assert payload["reacknowledged"] is False


# ---------------------------------------------------------------------------
# what it refuses
# ---------------------------------------------------------------------------


def test_ack_refuses_a_launch_nobody_booked(cli, reopened):
    _launch_id, make = cli
    env = cli_webfetch._cmd_ack(ack_args(make, "LNCH-never-booked", fetch_ids=["WF-forged"]))
    assert env["ok"] is False
    assert env["error"]["code"] == "ack_refused"
    assert "not booked" in env["error"]["message"]
    assert (
        reopened.ops.execute(
            "SELECT count(*) AS n FROM meta WHERE key LIKE 'webfetch.ack.%'"
        ).fetchone()["n"]
        == 0
    )


@pytest.mark.parametrize("note", ["", "   ", "\n\t "])
def test_ack_refuses_an_empty_note(cli, reopened, note):
    launch_id, make = cli
    env = cli_webfetch._cmd_ack(ack_args(make, launch_id, fetch_ids=["WF-forged"], note=note))
    assert env["ok"] is False
    assert env["error"]["code"] == "ack_refused"
    assert "--note is required" in env["error"]["message"]
    assert (
        reopened.ops.execute(
            "SELECT count(*) AS n FROM meta WHERE key LIKE 'webfetch.ack.%'"
        ).fetchone()["n"]
        == 0
    )


def test_ack_refuses_when_no_id_is_named(cli):
    launch_id, make = cli
    env = cli_webfetch._cmd_ack(ack_args(make, launch_id))
    assert env["ok"] is False
    assert env["error"]["code"] == "ack_refused"
    assert "--fetch-id" in env["error"]["message"]


@pytest.mark.parametrize(
    "bad", ["WF forged", "WF/../../etc", "WF#comment", "WF-" + "x" * 200, "%"]
)
def test_ack_refuses_an_id_that_is_not_an_id(cli, reopened, bad):
    launch_id, make = cli
    env = cli_webfetch._cmd_ack(ack_args(make, launch_id, fetch_ids=[bad]))
    assert env["ok"] is False
    assert env["error"]["code"] == "ack_refused"
    assert (
        reopened.ops.execute(
            "SELECT count(*) AS n FROM meta WHERE key LIKE 'webfetch.ack.%'"
        ).fetchone()["n"]
        == 0
    )


def test_one_bad_id_writes_nothing_at_all(cli, reopened):
    """Validation is complete before the first row: a two-id call with one
    typo must not leave the other id half-acknowledged."""
    launch_id, make = cli
    env = cli_webfetch._cmd_ack(ack_args(make, launch_id, fetch_ids=["WF-good", "WF bad"]))
    assert env["ok"] is False
    assert (
        reopened.ops.execute(
            "SELECT count(*) AS n FROM meta WHERE key LIKE 'webfetch.ack.%'"
        ).fetchone()["n"]
        == 0
    )


# ---------------------------------------------------------------------------
# idempotence
# ---------------------------------------------------------------------------


def test_re_acking_updates_the_note_and_reports_already_acknowledged(cli, reopened):
    launch_id, make = cli
    cli_webfetch._cmd_ack(ack_args(make, launch_id, fetch_ids=["WF-forged"], note="first pass"))
    env = cli_webfetch._cmd_ack(
        ack_args(make, launch_id, fetch_ids=["WF-forged"], note="second pass, same id")
    )
    assert env["ok"] is True
    assert env["result"]["new"] == 0
    assert env["result"]["alreadyAcknowledged"] == 1
    assert env["result"]["acknowledged"][0]["already"] is True

    rows = reopened.ops.execute(
        "SELECT value FROM meta WHERE key = 'webfetch.ack.WF-forged'"
    ).fetchall()
    assert len(rows) == 1, "a re-ack must update the row, never add a second one"
    assert json.loads(rows[0]["value"])["note"] == "second pass, same id"
    # the act itself is still logged twice: two operator acts, two events
    events = reopened.ops.execute(
        "SELECT payload FROM event WHERE type = 'webfetch_ack' ORDER BY event_id"
    ).fetchall()
    assert [json.loads(e["payload"])["reacknowledged"] for e in events] == [False, True]


def test_repeating_the_same_id_in_one_call_writes_it_once(cli, reopened):
    launch_id, make = cli
    env = cli_webfetch._cmd_ack(
        ack_args(make, launch_id, fetch_ids=["WF-forged", "WF-forged"])
    )
    assert env["result"]["new"] == 1
    assert (
        reopened.ops.execute(
            "SELECT count(*) AS n FROM meta WHERE key LIKE 'webfetch.ack.%'"
        ).fetchone()["n"]
        == 1
    )


# ---------------------------------------------------------------------------
# the list verb
# ---------------------------------------------------------------------------


def test_acks_lists_nothing_before_anything_is_acknowledged(cli):
    _launch_id, make = cli
    env = cli_webfetch._cmd_acks(make())
    assert env["ok"] is True
    assert env["result"]["acks"] == []
    assert env["result"]["total"] == 0
    assert env["result"]["supersededRecords"] == 0


def test_acks_lists_every_acknowledgement_with_its_launch_and_note(cli):
    launch_id, make = cli
    cli_webfetch._cmd_ack(
        ack_args(make, launch_id, fetch_ids=["WF-forged"], job_ids=["JOB-webfetch-WF-forged2"])
    )
    env = cli_webfetch._cmd_acks(make())
    assert env["result"]["total"] == 2
    by_id = {a["id"]: a for a in env["result"]["acks"]}
    assert set(by_id) == {"WF-forged", "JOB-webfetch-WF-forged2"}
    assert by_id["WF-forged"]["launchId"] == launch_id
    assert by_id["WF-forged"]["note"] == "H-attrib runbook forgery"
    assert by_id["JOB-webfetch-WF-forged2"]["kind"] == "job"
    assert by_id["WF-forged"]["revisions"] == 1
    assert by_id["WF-forged"]["superseded"] == 0


def test_acks_says_when_a_row_has_replaced_an_earlier_signature(cli):
    """`acks` reads the ops.meta row, and a re-ack UPDATEs it: the second
    signer's launch, note and timestamp replace the first's there. A surface
    whose whole justification is "an acknowledgement that could not be listed
    back would be a way to make a finding disappear" has to say when that has
    happened, or the first signature vanishes from it silently."""
    launch_id, make = cli
    cli_webfetch._cmd_ack(ack_args(make, launch_id, fetch_ids=["WF-forged"], note="signed by A"))
    cli_webfetch._cmd_ack(ack_args(make, launch_id, fetch_ids=["WF-forged"], note="signed by B"))

    env = cli_webfetch._cmd_acks(make())
    assert env["result"]["total"] == 1
    row = env["result"]["acks"][0]
    assert row["note"] == "signed by B"
    assert row["revisions"] == 2
    assert row["superseded"] == 1
    assert env["result"]["supersededRecords"] == 1
    assert "event log" in env["result"]["note"]


def test_the_superseded_signature_is_still_in_the_event_log(cli, reopened):
    launch_id, make = cli
    cli_webfetch._cmd_ack(ack_args(make, launch_id, fetch_ids=["WF-forged"], note="signed by A"))
    cli_webfetch._cmd_ack(ack_args(make, launch_id, fetch_ids=["WF-forged"], note="signed by B"))

    notes = [
        json.loads(r["payload"])["note"]
        for r in reopened.ops.execute(
            "SELECT payload FROM event WHERE type = 'webfetch_ack' ORDER BY rowid"
        ).fetchall()
    ]
    assert notes == ["signed by A", "signed by B"]
    assert acks_mod.ack_revision_counts(reopened) == {"WF-forged": 2}


def test_a_corrupt_ack_row_is_still_listed_rather_than_vanishing(cli, reopened):
    """An unreadable acknowledgement is still an acknowledgement someone
    made. Dropping it would let a mangled row silently un-acknowledge an id
    — the failure direction that hides a decision."""
    launch_id, make = cli
    cli_webfetch._cmd_ack(ack_args(make, launch_id, fetch_ids=["WF-forged"]))
    with reopened.ops:
        reopened.ops.execute(
            "UPDATE meta SET value = 'not json' WHERE key = 'webfetch.ack.WF-forged'"
        )
    records = acks_mod.load_acknowledged(reopened.ops)
    assert set(records) == {"WF-forged"}
    assert records["WF-forged"]["kind"] == "unknown"
    assert records["WF-forged"]["note"] == ""


# ---------------------------------------------------------------------------
# the module's own seams
# ---------------------------------------------------------------------------


def test_the_key_namespace_round_trips():
    assert acks_mod.ack_key("WF-1") == "webfetch.ack.WF-1"
    assert acks_mod.id_from_ack_key("webfetch.ack.WF-1") == "WF-1"
    assert acks_mod.id_from_ack_key("the (excluded) tenant-migration module.import_watermark") is None
    assert acks_mod.id_from_ack_key("webfetch.ack.") is None


def test_load_acknowledged_tolerates_a_store_with_no_meta_table(tmp_path):
    import sqlite3

    conn = sqlite3.connect(tmp_path / "bare.db")
    try:
        assert acks_mod.load_acknowledged(conn) == {}
    finally:
        conn.close()


def test_acknowledge_raises_rather_than_returning_a_verdict(store, program_root):
    """The module refuses; the CLI is what turns a refusal into an envelope.
    Keeping the boundary there is what lets the host script and any future
    caller reuse the rules without re-deriving them."""
    write_program_config(program_root)
    launch_id = bootstrap_launch(store)
    with pytest.raises(acks_mod.AckError):
        acks_mod.acknowledge(store, fetch_ids=["WF-1"], launch_id=launch_id, note="  ")
    with pytest.raises(acks_mod.AckError):
        acks_mod.acknowledge(store, fetch_ids=["WF-1"], launch_id="LNCH-nope", note="why")
    assert acks_mod.list_acks(store) == []
