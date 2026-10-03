"""Part A: the resolver. Design ``L10_plain-words-resolver-packet-outbox.md``
Section 2 and the operator's own worked example (CHARTER.md, verbatim):
"What is this room ROOM-01M2XAX6268A0YE6WMR2ABETFY? Without context I have
no idea what is it for"."""

from __future__ import annotations

from trialerror.dashboard.store_ro import open_store_ro
from trialerror.resolve import describe
from trialerror.resolve.base import describe_line
from trialerror.rooms.api import close_room, freeze_room
from trialerror.stores.store import open_store

from tests._store_fixtures import populate_one_of_everything


def _ro(program_root, platform_root):
    return open_store_ro(program_root, platform_root=platform_root)


def test_the_operators_room_example_now_reads_in_words(store, program_root, platform_root):
    ids = populate_one_of_everything(store)
    freeze_room(store, room_id=ids["room"], by_launch=ids["launch"], reason="the round ended with a failed result")
    store.close()
    with _ro(program_root, platform_root) as ro:
        desc = describe(ids["room"], ro)
    assert desc.found and desc.kind == "ROOM"
    assert desc.title == "test room"  # the topic -- never the bare id
    assert "frozen" in desc.state_words and "the round ended with a failed result" in desc.state_words
    assert "idea" in desc.purpose  # room_link -> idea -> round is not silent
    assert any(rid == ids["idea"] for rid, _, _ in desc.related)
    line = describe_line(desc)
    assert ids["room"] in line and "test room" in line and "frozen" in line
    # the operator's actual complaint: a bare id alone is never the whole answer
    assert line != ids["room"]


def test_a_closed_room_names_the_operators_decision(store, program_root, platform_root):
    ids = populate_one_of_everything(store)
    freeze_room(store, room_id=ids["room"], by_launch=ids["launch"], reason="stopped for review")
    close_room(store, room_id=ids["room"], by_launch=ids["launch"], reason="superseded", decided_by="C-0105")
    store.close()
    with _ro(program_root, platform_root) as ro:
        desc = describe(ids["room"], ro)
    assert "closed" in desc.state_words and "C-0105" in desc.state_words


def test_src_doc_and_chk_join_up_to_their_source(store, program_root, platform_root):
    ids = populate_one_of_everything(store)
    store.close()
    with _ro(program_root, platform_root) as ro:
        src = describe(ids["source"], ro)
        doc = describe(ids["document"], ro)
        chk = describe(ids["chunk"], ro)
    assert src.title == "test source" and src.purpose == "no purpose is recorded for this source"
    assert doc.title == "archive/test.md" and doc.related[0][2] == "test source"
    assert chk.related[0][1] == "a document" and chk.related[1][2] == "test source"
    assert "test.md" in chk.purpose or "test source" in chk.purpose


def test_cr_shows_state_verdict_reproduction_and_artifact_title(store, program_root, platform_root):
    from trialerror.stores import update as store_update

    ids = populate_one_of_everything(store)
    store_update(
        store, "gate", pk_column="gate_id", pk_value=ids["gate"],
        changes={"state": "gated", "verdict": "PASS_WITH_EDITS", "reproduction_status": "match"},
    )
    store.close()
    with _ro(program_root, platform_root) as ro:
        desc = describe(ids["gate"], ro)
    assert desc.title == "test artifact"
    assert "gated" in desc.state_words and "PASS_WITH_EDITS" in desc.state_words and "reproduced cleanly" in desc.state_words


def test_a_ruling_reads_by_its_summary(store, program_root, platform_root):
    ids = populate_one_of_everything(store)
    store.close()
    with _ro(program_root, platform_root) as ro:
        desc = describe(ids["ruling"], ro)
    assert desc.found and desc.title == "test ruling" and desc.state_words == "active"


def test_a_launch_shows_agent_kind_purpose_state_and_session(store, program_root, platform_root):
    ids = populate_one_of_everything(store)
    store.close()
    with _ro(program_root, platform_root) as ro:
        desc = describe(ids["launch"], ro)
    assert desc.found and "tester" in desc.title and desc.purpose == "fixture"
    assert desc.state_words.startswith("PROVISIONAL")
    assert (ids["session"], "its session", ids["session"]) in desc.related


def test_an_unknown_prefix_says_so_without_guessing(store, program_root, platform_root):
    store.close()
    with _ro(program_root, platform_root) as ro:
        desc = describe("ZZZZ-not-a-real-id", ro)
    assert not desc.found and desc.kind_words == "unknown kind of id"
    assert describe_line(desc) == "unknown kind of id [ZZZZ-not-a-real-id]"


def test_a_known_kind_missing_its_row_says_not_found(store, program_root, platform_root):
    ids = populate_one_of_everything(store)
    store.close()
    with _ro(program_root, platform_root) as ro:
        desc = describe("ROOM-00000000000000000000000000", ro)
    assert not desc.found and desc.store == "ops"
    assert "not found in ops" in describe_line(desc)


def test_never_guesses_a_purpose(store, program_root, platform_root):
    """Section 6 trap 2: a source carries no purpose column, and the
    resolver says so in words rather than inventing one."""
    ids = populate_one_of_everything(store)
    store.close()
    with _ro(program_root, platform_root) as ro:
        desc = describe(ids["source"], ro)
    assert desc.purpose == "no purpose is recorded for this source"


def test_n1_the_default_paragraph_carries_purpose_and_ties(store, program_root, platform_root):
    """Review N-1: the operator's own complaint was "no idea what it is
    for" -- the default (non-JSON) paragraph must say the purpose and the
    ties itself, not only carry them under --json."""
    ids = populate_one_of_everything(store)
    freeze_room(store, room_id=ids["room"], by_launch=ids["launch"], reason="the round ended")
    store.close()
    with _ro(program_root, platform_root) as ro:
        line = describe_line(describe(ids["room"], ro))
    assert "Vets" in line and "round-1" in line  # the purpose sentence (capitalized as its own sentence)
    assert "Tied to:" in line and ids["idea"] in line  # the ties line
    assert line.endswith(f"[{ids['room']}]")  # the id still comes last, in brackets


def test_n2_a_store_error_reading_a_known_kind_gives_found_false_not_a_crash(
    store, program_root, platform_root
):
    """Review N-2: a dedicated reader (ROOM's, here) queries its own tables
    unguarded; on an older schema missing one of them (room_link, in this
    case), describe() must not crash its caller -- the whole DECIDE panel
    or the packet's whole DECIDE section, in the real callers this
    protects."""
    ids = populate_one_of_everything(store)
    store.ops.execute("DROP TABLE room_link")
    store.ops.commit()
    store.close()
    with _ro(program_root, platform_root) as ro:
        desc = describe(ids["room"], ro)  # must not raise
    assert desc.found is False
    assert "store error" in desc.kind_words
