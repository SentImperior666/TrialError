"""``lens_citations_within_slice`` — the audit standing behind the
per-launch retrieval scope.

Exercised green AND red, and in both of its two distinct red shapes: a
citation that resolves to a document OUTSIDE the slice (a barrier crossed,
**fail**) and one that resolves to nothing at all (a citation of nothing,
**warn**). The two are different findings and the check must not fold them
into one headline.
"""

from __future__ import annotations

import json

import pytest

from trialerror.events.api import create_thread, post_feed
from trialerror.lens.checks import check_lens_citations_within_slice
from trialerror.stores import insert
from trialerror.util.doctor import DoctorContext
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests._inventory_fixtures import bootstrap_launch, build_corpus_with_inventory


@pytest.fixture()
def ctx(program_root, platform_root) -> DoctorContext:
    return DoctorContext(program_root=program_root, platform_root=platform_root)


def _post(store, *, launch_id: str, body: str) -> str:
    thread = create_thread(store, title="round thread", launch_id=launch_id)
    return post_feed(store, thread_id=thread["thread_id"], body=body, launch_id=launch_id)["post_id"]


def _lens_launch(store, *, slice_doc_ids) -> str:
    return bootstrap_launch(store, attrs={"slice_doc_ids": list(slice_doc_ids)}, purpose="ideation")


def _roster_backed_lens_launch(store, *, round_id: str, candidate_ids) -> str:
    """A lens launch that declares NO ``slice_doc_ids`` — the shape a
    booking made straight off ``lens export`` has — so the check has to walk
    ``roster_id -> lens_assignment.slice_spec`` to find the slice."""
    roster_id = new_id("ROST")
    insert(
        store, "lens_roster",
        {"roster_id": roster_id, "round_id": round_id, "lens_name": "lens-1", "vantage": "v",
         "seat": "standard", "model_class": "top", "created_ts": now()},
    )
    assign_ids = []
    for candidate_id in candidate_ids:
        assign_id = new_id("ASGN")
        insert(
            store, "lens_assignment",
            {"assign_id": assign_id, "roster_id": roster_id,
             "slice_spec": json.dumps({"candidate_id": candidate_id, "arm": "near"}),
             "arm": "near", "seed": "s", "created_ts": now()},
        )
        assign_ids.append(assign_id)
    return bootstrap_launch(
        store, attrs={"roster_id": roster_id, "assign_ids": assign_ids}, purpose="ideation"
    )


def _launch_linked_by_assign_ids(store, *, round_id: str, candidate_ids) -> str:
    """A lens launch carrying NO lens attrs at all, linked to its assignment
    rows only by ``lens_assignment.lens_launch_id`` -- the shape
    ``trialerror budget book --assign-id`` produces."""
    from trialerror.budget.pools import link_launch_to_assignments

    roster_id = new_id("ROST")
    insert(
        store, "lens_roster",
        {"roster_id": roster_id, "round_id": round_id, "lens_name": "lens-linked", "vantage": "v",
         "seat": "standard", "model_class": "top", "created_ts": now()},
    )
    assign_ids = []
    for candidate_id in candidate_ids:
        assign_id = new_id("ASGN")
        insert(
            store, "lens_assignment",
            {"assign_id": assign_id, "roster_id": roster_id,
             "slice_spec": json.dumps({"candidate_id": candidate_id, "arm": "near"}),
             "arm": "near", "seed": "s", "created_ts": now()},
        )
        assign_ids.append(assign_id)
    launch_id = bootstrap_launch(store, purpose="ideation")
    link_launch_to_assignments(store, launch_id=launch_id, assign_ids=assign_ids)
    return launch_id


# ---------------------------------------------------------------------------
# green
# ---------------------------------------------------------------------------


def test_a_post_citing_only_its_own_slice_passes(store, ctx):
    corpus = build_corpus_with_inventory(store)
    inside = corpus["corpus_doc_ids"][0]
    launch_id = _lens_launch(store, slice_doc_ids=[inside])
    _post(store, launch_id=launch_id, body=f"The mechanism appears in {inside}, which my slice holds.")
    store.close()

    r = check_lens_citations_within_slice(ctx)
    assert r.status == "pass"
    assert r.details["posts_checked"] == 1
    assert r.details["offenders"] == []


def test_a_chunk_or_anchor_id_is_resolved_back_to_its_document(store, ctx):
    """A lens cites what the retrieval tools hand it, and those hand back
    chunk and anchor ids. Checking only ``DOC-`` ids would audit the one
    citation shape a lens is least likely to write."""
    corpus = build_corpus_with_inventory(store)
    inside_chunk = corpus["corpus_chunk_ids"][0]
    inside_doc = store.knowledge.execute(
        "SELECT doc_id FROM chunk WHERE chunk_id = ?", (inside_chunk,)
    ).fetchone()["doc_id"]
    anchor = store.knowledge.execute(
        "SELECT anchor_id FROM quote_anchor WHERE doc_id = ?", (inside_doc,)
    ).fetchone()["anchor_id"]
    launch_id = _lens_launch(store, slice_doc_ids=[inside_doc])
    _post(store, launch_id=launch_id, body=f"Grounded at {inside_chunk} and {anchor}.")
    store.close()

    r = check_lens_citations_within_slice(ctx)
    assert r.status == "pass"


def test_a_slice_declared_only_through_assignment_rows_is_still_resolved(store, ctx):
    corpus = build_corpus_with_inventory(store)
    inside = corpus["corpus_doc_ids"][0]
    launch_id = _roster_backed_lens_launch(store, round_id="r-1", candidate_ids=[inside])
    _post(store, launch_id=launch_id, body=f"From my slice: {inside}.")
    store.close()

    r = check_lens_citations_within_slice(ctx)
    assert r.status == "pass"
    assert r.details["posts_checked"] == 1


# ---------------------------------------------------------------------------
# red
# ---------------------------------------------------------------------------


def test_a_post_citing_a_document_outside_the_slice_fails(store, ctx):
    corpus = build_corpus_with_inventory(store)
    inside, outside = corpus["corpus_doc_ids"][0], corpus["corpus_doc_ids"][1]
    launch_id = _lens_launch(store, slice_doc_ids=[inside])
    post_id = _post(store, launch_id=launch_id, body=f"Compare {inside} against {outside}.")
    store.close()

    r = check_lens_citations_within_slice(ctx)
    assert r.status == "fail"
    offender = r.details["offenders"][0]
    assert offender["post_id"] == post_id
    assert offender["cited_outside_slice"] == [outside]
    assert offender["resolved_docs"] == [outside]


def test_citing_an_inventory_row_is_caught_as_outside_the_slice(store, ctx):
    """The barrier this check exists beside: a lens naming a row of the
    reference set it is judged against. It is outside every lens slice by
    construction, and the audit says so in the same words as any other
    out-of-slice document."""
    corpus = build_corpus_with_inventory(store)
    inside = corpus["corpus_doc_ids"][0]
    inventory_doc = corpus["inventory_doc_ids"]["family-a"]
    launch_id = _lens_launch(store, slice_doc_ids=[inside])
    _post(store, launch_id=launch_id, body=f"This restates the entry in {inventory_doc}.")
    store.close()

    r = check_lens_citations_within_slice(ctx)
    assert r.status == "fail"
    assert r.details["offenders"][0]["cited_outside_slice"] == [inventory_doc]


def test_an_id_this_corpus_does_not_hold_is_warn_not_fail(store, ctx):
    """Different finding, reported differently: nothing crossed a slice
    boundary, so calling it a crossing would be reporting it wrongly."""
    corpus = build_corpus_with_inventory(store)
    inside = corpus["corpus_doc_ids"][0]
    launch_id = _lens_launch(store, slice_doc_ids=[inside])
    invented = "DOC-01J2KQ3P4R5S6T7V8W9X0Y1Z2A"
    _post(store, launch_id=launch_id, body=f"See {inside} and also {invented}.")
    store.close()

    r = check_lens_citations_within_slice(ctx)
    assert r.status == "warn"
    assert r.details["offenders"] == []
    assert r.details["unresolvable"][0]["cited_unresolvable"] == [invented]


def test_a_crossing_outranks_an_unresolvable_id_and_still_counts_it(store, ctx):
    corpus = build_corpus_with_inventory(store)
    inside, outside = corpus["corpus_doc_ids"][0], corpus["corpus_doc_ids"][1]
    launch_id = _lens_launch(store, slice_doc_ids=[inside])
    _post(store, launch_id=launch_id, body=f"{outside} and DOC-01J2KQ3P4R5S6T7V8W9X0Y1Z2A")
    store.close()

    r = check_lens_citations_within_slice(ctx)
    assert r.status == "fail"
    assert r.details["offenders"] and r.details["unresolvable"]
    assert "outside their own assigned slice" in r.message
    assert "does not hold" in r.message


# ---------------------------------------------------------------------------
# what it declines to judge
# ---------------------------------------------------------------------------


def test_a_post_by_a_launch_that_is_not_a_lens_is_not_judged(store, ctx):
    """The orchestrator's own posts cite the whole corpus by design."""
    corpus = build_corpus_with_inventory(store)
    launch_id = bootstrap_launch(store, purpose="orchestration")
    _post(store, launch_id=launch_id, body=f"Round summary covering {corpus['corpus_doc_ids'][1]}.")
    store.close()

    r = check_lens_citations_within_slice(ctx)
    assert r.status == "skip"
    assert "no feed posts by a launch carrying a lens slice" in r.message
    # and the skip names the two ways a lens launch gets linked to its
    # slice, so the reader is not left with a barrier that silently never
    # engaged
    assert "--assign-id" in r.message
    assert "lens export" in r.message


def test_a_launch_linked_only_by_book_assign_id_is_judged_against_its_slice(store, ctx):
    """The gap: a lens booked through `budget book` carries none of the
    exported attrs, resolved to "not a lens launch", and this whole audit
    SKIPped for exactly the launches it exists for."""
    corpus = build_corpus_with_inventory(store)
    inside, outside = corpus["corpus_doc_ids"][0], corpus["corpus_doc_ids"][1]
    launch_id = _launch_linked_by_assign_ids(store, round_id="r-link", candidate_ids=[inside])
    post_id = _post(store, launch_id=launch_id, body=f"Compare {inside} against {outside}.")
    store.close()

    r = check_lens_citations_within_slice(ctx)
    assert r.status == "fail"
    offender = r.details["offenders"][0]
    assert offender["post_id"] == post_id
    assert offender["cited_outside_slice"] == [outside]


def test_a_launch_linked_only_by_book_assign_id_passes_when_it_stays_inside(store, ctx):
    corpus = build_corpus_with_inventory(store)
    inside = corpus["corpus_doc_ids"][0]
    launch_id = _launch_linked_by_assign_ids(store, round_id="r-link", candidate_ids=[inside])
    _post(store, launch_id=launch_id, body=f"From my slice: {inside}.")
    store.close()

    r = check_lens_citations_within_slice(ctx)
    assert r.status == "pass"
    assert r.details["posts_checked"] == 1


def test_a_lens_post_citing_nothing_at_all_is_counted_but_not_flagged(store, ctx):
    corpus = build_corpus_with_inventory(store)
    launch_id = _lens_launch(store, slice_doc_ids=[corpus["corpus_doc_ids"][0]])
    _post(store, launch_id=launch_id, body="A record with a statement and a probe and no ids in it.")
    store.close()

    r = check_lens_citations_within_slice(ctx)
    assert r.status == "pass"
    assert r.details["posts_checked"] == 1


def test_no_program_and_no_platform_both_skip(tmp_path, program_root, platform_root, store):
    store.close()
    assert check_lens_citations_within_slice(DoctorContext(program_root=None)).status == "skip"
    absent = DoctorContext(program_root=tmp_path / "nope", platform_root=platform_root)
    assert check_lens_citations_within_slice(absent).status == "skip"


def test_the_check_is_registered_under_the_lens_category():
    from trialerror.util.doctor import discover_and_register_checks, registered_checks

    discover_and_register_checks()
    assert registered_checks()["lens_citations_within_slice"][0] == "lens"
