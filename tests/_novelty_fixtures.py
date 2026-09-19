"""Not a test module — the fixture ROUND the novelty screen is tested
against: three lenses, six ideas each, with the two shapes the screen exists
to catch planted deliberately.

Why the plants are exact-text duplicates rather than "similar" text: the
zero-setup embed backend is hash-derived, so two different strings have
essentially unrelated vectors and no amount of rewording produces a 0.92
cosine. Identical text produces exactly 1.0. That is the honest way to
exercise a threshold without a GPU — the fixture is not pretending two
paraphrases embed near each other, it is handing the screen the one case
where the distance is known.
"""

from __future__ import annotations

from typing import Any

from trialerror.lens.ideas import write_idea
from trialerror.stores.store import Store

from tests._inventory_fixtures import INVENTORY_ROWS, bootstrap_launch, build_corpus_with_inventory

ROUND_ID = "round-fixture"

#: (lens, home cell, declared operation, statement) for eighteen records.
#: Lens 3's first record is an exact restatement of lens 1's first record in
#: the same home cell — the near-duplicate the merge must fold. Lens 2's
#: fourth record is an inventory row verbatim — the KNOWN-MECHANIC flag.
#: "Verbatim" means the CHUNK's text, register table row and all, because
#: the zero-setup embed backend only reaches cosine 1.0 on an exact string;
#: that record therefore reads as a table row rather than as prose, which is
#: an artifact of the fixture's embedding and not a shape a lens produces.
_A_1 = "A tracked resource is converted into position, and the conversion rate falls as the track fills."
_KNOWN = INVENTORY_ROWS["family-a"][0]

IDEAS: list[tuple[str, str, str, str]] = [
    ("lens-1", "family-a/row-1", "bridge/synthesis-unify", _A_1),
    ("lens-1", "family-a/row-2", "scope-mismatch/formalize", "Two seats share one action budget and must declare their split before either acts."),
    ("lens-1", "family-b/row-1", "cost-bottleneck/robustify", "Bookkeeping is moved off the players by making the board itself carry the running total."),
    ("lens-1", "family-a/row-3", "unexplained-mechanic/empirical-map", "A hidden bid is replaced by a public commitment revealed one phase later."),
    ("lens-1", "family-b/row-2", "failure-risk/artifact", "A dispute is resolved by a written appeal that the table votes on once."),
    ("lens-1", "family-a/row-4", "contradiction-between-systems/extend-scope", "The refill schedule is coupled to the slowest participant rather than to the clock."),
    ("lens-2", "family-b/row-3", "bridge/synthesis-unify", "Turn order is derived from the same state that scoring reads, so a lead is self-correcting."),
    ("lens-2", "family-a/row-5", "missing-representation/formalize", "The state model gains an explicit representation of what each seat believes the others hold."),
    ("lens-2", "family-b/row-4", "scope-mismatch/search-optimize", "At forty participants the resolution step is replaced by a sampled subset of comparisons."),
    ("lens-2", "family-a/row-1", "cost-bottleneck/robustify", _KNOWN),
    ("lens-2", "family-b/row-5", "failure-risk/extend-scope", "A stalled phase is broken by a rule that spends the shared pool instead of waiting."),
    ("lens-2", "family-a/row-6", "unexplained-mechanic/empirical-map", "The value of a held card is made a function of how many phases it has been held."),
    ("lens-3", "family-a/row-1", "bridge/synthesis-unify", _A_1),
    ("lens-3", "family-b/row-6", "contradiction-between-systems/artifact", "Two subsystems that both consume time are made to consume the same token instead."),
    ("lens-3", "family-a/row-7", "missing-representation/robustify", "The model records why a state changed, not only that it changed."),
    ("lens-3", "family-b/row-7", "cost-bottleneck/formalize", "One shared display replaces every private tally, and the tally becomes a derived value."),
    ("lens-3", "family-a/row-8", "failure-risk/empirical-map", "A collapse of the bookkeeping is made recoverable by keeping one redundant summary row."),
    ("lens-3", "family-b/row-8", "scope-mismatch/extend-scope", "A rule written for three seats is restated so that it reads identically for one or for a thousand."),
]

#: Every idea in a degenerate batch declares the same move and says the same
#: thing — the distribution the collapse monitor exists to notice.
#:
#: Each sits in a DIFFERENT home cell, which is not a detail: the merge runs
#: first, and same-cell duplicates would be folded into one record before the
#: monitor ever saw a batch. Six records saying one thing about six different
#: cells is the shape that survives the merge and is still collapsed — and it
#: is the realistic one, since a collapsed round repeats a template across the
#: map rather than filing the same record six times in one cell.
DEGENERATE_ROUND_ID = "round-degenerate"
DEGENERATE_IDEAS: list[tuple[str, str, str, str]] = [
    (f"lens-{i}", f"family-a/row-{i}", "bridge/synthesis-unify",
     "The two subsystems are bridged and unified into one integrated mechanism.")
    for i in range(1, 7)
]


def build_round(
    store: Store, *, round_id: str = ROUND_ID, ideas: list[tuple[str, str, str, str]] | None = None
) -> dict[str, Any]:
    """A screened-shaped round: a corpus with an inventory, one launch per
    lens, and the records above written through ``write_idea`` — the same
    writer a real round's orchestrator uses, so what the screen reads is
    what a real round would hand it."""
    corpus = build_corpus_with_inventory(store)
    ideas = ideas if ideas is not None else IDEAS

    launches: dict[str, str] = {}
    written: list[dict[str, Any]] = []
    for lens, home, operation, statement in ideas:
        if lens not in launches:
            launches[lens] = bootstrap_launch(
                store,
                attrs={"lens_name": lens, "slice_doc_ids": corpus["corpus_doc_ids"][:2]},
                purpose="ideation",
            )
        row = write_idea(
            store,
            round_id=round_id,
            author_launch=launches[lens],
            body=statement,
            home=home,
            tier="near",
            provenance={"docs": corpus["corpus_doc_ids"][:2], "set_id": lens},
            operation_declared=operation,
            recipe_card="TRANSFER",
            requirements="The mechanism must be statable as one state transition.",
            probe="Check the transition against one inventory row.",
            author_rationale="I picked this because it felt underexplored.",
            surprise="I did not expect the coupling to matter.",
        )
        written.append(row)

    return {
        **corpus,
        "round_id": round_id,
        "launches": launches,
        "ideas": written,
        "idea_ids": [r["idea_id"] for r in written],
        "known_mechanic_text": _KNOWN,
    }
