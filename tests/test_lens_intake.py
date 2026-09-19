"""The record schema, enforced where the records come in.

The reproduction: the judge-envelope builder reads ``provenance.docs`` and
the screen's distribution card reads a two-axis ``operation_declared``,
while nothing told the caller writing the records either -- and a
``requirements`` list reached sqlite as a bare "type 'list' is not
supported", which names neither the field nor the fix.
"""

from __future__ import annotations

import json

import pytest

from trialerror.cli import main
from trialerror.lens.ideas import (
    intake_records,
    normalize_operation_declared,
    normalize_provenance,
    normalize_requirements,
    read_idea,
    write_idea,
)
from trialerror.lens.novelty import _split_operation
from trialerror.stores.store import open_store

from tests._lens_fixtures import bootstrap_launch

ROUND_ID = "round-intake"


def _record(**overrides):
    record = {
        "requirements": ["any answer must be checkable by one seat", "no new bookkeeping"],
        "statement": "A spent resource advances a shared track by exactly one step.",
        "home_mechanic": "family-a/row-1",
        "assumed_circle": "a table of three to six seats",
        "provenance": {"docs": ["DOC-1", "DOC-2"], "card": "TRANSFER"},
        "operation_declared": "opportunity:bridge, method:formalize",
        "probe": "simulate twenty turns against row family-a/row-1 and compare track fill",
        "surprise": "the rate falls without anyone tracking the rate",
        "author_rationale": "the track already holds the state the rate needs",
    }
    record.update(overrides)
    return {k: v for k, v in record.items() if v is not _ABSENT}


class _Absent:
    pass


_ABSENT = _Absent()


# ---------------------------------------------------------------------------
# write_idea normalises and validates
# ---------------------------------------------------------------------------


def test_requirements_given_as_a_list_round_trip_as_bullets(store):
    launch_id = bootstrap_launch(store)
    row = write_idea(
        store, round_id=ROUND_ID, author_launch=launch_id, body="b",
        requirements=["first line", "second line"], provenance={"docs": ["DOC-1"]},
    )
    assert row["requirements"] == "- first line\n- second line"
    assert read_idea(store, idea_id=row["idea_id"])["requirements"] == "- first line\n- second line"


def test_requirements_already_bulleted_are_not_double_bulleted():
    assert normalize_requirements(["- already", "plain"]) == "- already\n- plain"


def test_a_requirements_value_that_is_neither_string_nor_list_is_refused_by_name():
    with pytest.raises(ValueError) as excinfo:
        normalize_requirements({"a": 1})
    assert "requirements" in str(excinfo.value)


def test_provenance_without_docs_is_refused(store):
    launch_id = bootstrap_launch(store)
    with pytest.raises(ValueError) as excinfo:
        write_idea(
            store, round_id=ROUND_ID, author_launch=launch_id, body="b",
            provenance={"card": "TRANSFER"},
        )
    assert "docs" in str(excinfo.value)
    assert store.knowledge.execute("SELECT COUNT(*) AS n FROM idea").fetchone()["n"] == 0


def test_the_slice_alias_is_copied_into_docs():
    out = normalize_provenance({"slice": ["DOC-7", "DOC-8"]})
    assert out["docs"] == ["DOC-7", "DOC-8"]
    assert out["slice"] == ["DOC-7", "DOC-8"]


def test_a_single_doc_id_is_read_as_a_one_element_list():
    assert normalize_provenance({"docs": "DOC-9"})["docs"] == ["DOC-9"]


def test_an_explicitly_empty_docs_list_is_a_declaration_and_is_kept():
    assert normalize_provenance({"docs": []})["docs"] == []


def test_no_provenance_at_all_stays_none():
    assert normalize_provenance(None) is None


@pytest.mark.parametrize(
    "declared,expected",
    [
        ("opportunity:bridge, method:formalize", ("bridge", "formalize")),
        ("method:formalize", (None, "formalize")),
        ("opportunity:bridge", ("bridge", None)),
        ({"opportunity": "bridge", "method": "formalize"}, ("bridge", "formalize")),
        ("bridge/formalize", ("bridge", "formalize")),
        ("decouple", ("decouple", None)),
        (None, (None, None)),
    ],
)
def test_every_operation_spelling_stores_the_form_the_distribution_card_reads(declared, expected):
    """The card splits ``operation_declared`` on its own axis rule, so the
    test asserts what the CARD will read back, not the storage bytes."""
    assert _split_operation(normalize_operation_declared(declared)) == expected


# ---------------------------------------------------------------------------
# intake_records: all of them, or none of them
# ---------------------------------------------------------------------------


def test_two_records_write_two_rows_carrying_their_assignment(store):
    launch_id = bootstrap_launch(store)
    rows = intake_records(
        store, round_id=ROUND_ID, records=[_record(), _record(statement="A second mechanism.")],
        author_launch=launch_id, assign_ids=["ASGN-1", "ASGN-2"], arm="far",
    )
    assert len(rows) == 2
    first = read_idea(store, idea_id=rows[0]["idea_id"])
    assert first["body"].startswith("A spent resource")
    assert first["home"] == "family-a/row-1"
    assert first["probe"]
    assert json.loads(first["provenance"])["docs"] == ["DOC-1", "DOC-2"]
    assert first["operation_declared"] == "bridge/formalize"
    slice_ref = json.loads(first["slice_ref"])
    assert slice_ref["assign_id"] == "ASGN-1"
    assert slice_ref["assign_ids"] == ["ASGN-1", "ASGN-2"]
    assert slice_ref["arm"] == "far"


def test_a_bad_third_record_writes_nothing_at_all(store):
    launch_id = bootstrap_launch(store)
    records = [_record(), _record(statement="A second mechanism."), _record(provenance={"card": "X"})]
    with pytest.raises(ValueError) as excinfo:
        intake_records(store, round_id=ROUND_ID, records=records, author_launch=launch_id)
    assert "record 2" in str(excinfo.value)
    assert store.knowledge.execute("SELECT COUNT(*) AS n FROM idea").fetchone()["n"] == 0


@pytest.mark.parametrize("field", ["statement", "probe"])
def test_a_record_missing_a_required_field_names_it(store, field):
    launch_id = bootstrap_launch(store)
    with pytest.raises(ValueError) as excinfo:
        intake_records(
            store, round_id=ROUND_ID, records=[_record(**{field: _ABSENT})], author_launch=launch_id
        )
    assert field in str(excinfo.value)
    assert store.knowledge.execute("SELECT COUNT(*) AS n FROM idea").fetchone()["n"] == 0


def test_a_record_carrying_a_field_nothing_reads_is_refused(store):
    launch_id = bootstrap_launch(store)
    with pytest.raises(ValueError) as excinfo:
        intake_records(
            store, round_id=ROUND_ID, records=[_record(confidence="high")], author_launch=launch_id
        )
    assert "confidence" in str(excinfo.value)


def test_a_per_record_assign_id_is_refused_the_way_the_docstring_now_says(store):
    """Fix pass N-5. ``intake_records``' docstring promised that "a record
    naming its own assign_id keeps it"; the schema refuses it as an unknown
    field, and nothing in the verb reads a per-record assign id. The refusal
    is the right behaviour; the sentence was the defect."""
    launch_id = bootstrap_launch(store)
    with pytest.raises(ValueError) as excinfo:
        intake_records(
            store, round_id=ROUND_ID, records=[_record(assign_id="ASGN-1")], author_launch=launch_id,
        )
    assert "assign_id" in str(excinfo.value)
    assert "a record naming its own" not in (intake_records.__doc__ or "")
    assert store.knowledge.execute("SELECT COUNT(*) AS n FROM idea").fetchone()["n"] == 0


def test_an_empty_file_is_refused_rather_than_read_as_a_round_with_no_records(store):
    launch_id = bootstrap_launch(store)
    with pytest.raises(ValueError):
        intake_records(store, round_id=ROUND_ID, records=[], author_launch=launch_id)


def test_records_may_arrive_wrapped_in_an_object(store):
    launch_id = bootstrap_launch(store)
    rows = intake_records(
        store, round_id=ROUND_ID, records={"records": [_record()]}, author_launch=launch_id
    )
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# the CLI verb
# ---------------------------------------------------------------------------


@pytest.fixture()
def cli_program_root(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIALERROR_PLATFORM_ROOT", str(tmp_path / "platform_root"))
    root = tmp_path / "program"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _run(capsys, argv):
    exit_code = main(argv)
    envelope = json.loads(capsys.readouterr().out.strip())
    envelope["_exit_code"] = exit_code
    return envelope


def test_the_intake_verb_writes_one_idea_per_record(cli_program_root, tmp_path, capsys):
    store = open_store(cli_program_root, platform_root=tmp_path / "platform_root")
    launch_id = bootstrap_launch(store)
    store.close()
    records_file = tmp_path / "records.json"
    records_file.write_text(
        json.dumps([_record(), _record(statement="A second mechanism.")]), encoding="utf-8"
    )

    env = _run(capsys, [
        "lens", "--program-root", str(cli_program_root), "intake", "--round-id", ROUND_ID,
        "--records", str(records_file), "--author-launch", launch_id,
        "--assign-id", "ASGN-1", "--arm", "near",
    ])
    assert env["ok"] is True
    assert env["result"]["count"] == 2
    assert len(env["result"]["idea_ids"]) == 2


def test_the_intake_verb_refuses_a_bad_file_as_one_envelope_and_writes_nothing(
    cli_program_root, tmp_path, capsys
):
    store = open_store(cli_program_root, platform_root=tmp_path / "platform_root")
    launch_id = bootstrap_launch(store)
    store.close()
    records_file = tmp_path / "records.json"
    records_file.write_text(json.dumps([_record(), _record(probe=_ABSENT)]), encoding="utf-8")

    env = _run(capsys, [
        "lens", "--program-root", str(cli_program_root), "intake", "--round-id", ROUND_ID,
        "--records", str(records_file), "--author-launch", launch_id,
    ])
    assert env["ok"] is False
    assert env["error"]["code"] == "record_refused"
    assert "probe" in env["error"]["message"]

    store = open_store(cli_program_root, platform_root=tmp_path / "platform_root")
    try:
        assert store.knowledge.execute("SELECT COUNT(*) AS n FROM idea").fetchone()["n"] == 0
    finally:
        store.close()


# ---------------------------------------------------------------------------
# lane FB-5 item 4: the archive intake
# ---------------------------------------------------------------------------


def test_the_status_flag_writes_an_archive_round(cli_program_root, tmp_path, capsys):
    store = open_store(cli_program_root, platform_root=tmp_path / "platform_root")
    booked_launch = bootstrap_launch(store)
    store.close()
    records = [
        {"statement": "A prior round's candidate, kept as reference.", "probe": "check it",
         "provenance": {"docs": []}},
        {"statement": "A prior round's request row.", "probe": "check it", "provenance": {"docs": []}},
    ]
    path = tmp_path / "archive.json"
    path.write_text(json.dumps(records), encoding="utf-8")
    env = _run(
        capsys,
        ["lens", "--program-root", str(cli_program_root), "intake", "--round-id", "round-archive",
         "--records", str(path), "--author-launch", booked_launch, "--status", "archived"],
    )
    assert env["ok"] is True, env
    assert env["result"]["status"] == "archived"
    store = open_store(cli_program_root, platform_root=tmp_path / "platform_root")
    statuses = [read_idea(store, idea_id=i)["status"] for i in env["result"]["idea_ids"]]
    store.close()
    assert statuses == ["archived", "archived"]


def test_intake_with_no_status_flag_still_writes_raw_records(cli_program_root, tmp_path, capsys):
    store = open_store(cli_program_root, platform_root=tmp_path / "platform_root")
    booked_launch = bootstrap_launch(store)
    store.close()
    path = tmp_path / "records.json"
    path.write_text(
        json.dumps([{"statement": "A live record.", "probe": "check it", "provenance": {"docs": []}}]),
        encoding="utf-8",
    )
    env = _run(
        capsys,
        ["lens", "--program-root", str(cli_program_root), "intake", "--round-id", "round-1",
         "--records", str(path), "--author-launch", booked_launch],
    )
    assert env["ok"] is True, env
    assert env["result"]["status"] == "raw"
    store = open_store(cli_program_root, platform_root=tmp_path / "platform_root")
    assert read_idea(store, idea_id=env["result"]["idea_ids"][0])["status"] == "raw"
    store.close()
