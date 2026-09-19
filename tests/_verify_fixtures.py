"""Not a test module (pytest only collects ``test_*.py``) — shared fixture
builders for the M9 (``trialerror.verify``) test suite.

Reuses M8's own corpus builder (``tests._retrieve_fixtures.build_small_corpus``)
rather than re-deriving a second copy of the same chunker/anchor-building
plumbing: M8 is fully landed by this build's order (M9 is order 6, "needs
M8"), so there is no concurrent-edit risk in importing its test helper
module (the self-containment precedent ``tests/_retrieve_fixtures.py``'s
own docstring states was about avoiding a CONCURRENT builder's private
files, not a rule against ever reusing a landed one).
"""

from __future__ import annotations

from typing import Any

from trialerror.stores.store import Store
from trialerror.stores.writer import insert
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

from tests._retrieve_fixtures import bootstrap_launch, build_small_corpus

__all__ = ["bootstrap_launch", "build_small_corpus", "anchor_for_chunk", "seed_hypothesis"]


def anchor_for_chunk(store: Store, chunk_id: str) -> dict[str, Any]:
    """The one ``quote_anchor`` row built for ``chunk_id`` by
    ``build_small_corpus`` (each fixture chunk gets exactly one)."""
    row = store.knowledge.execute("SELECT * FROM quote_anchor WHERE chunk_id = ?", (chunk_id,)).fetchone()
    assert row is not None, f"no quote_anchor for chunk_id={chunk_id!r}"
    return dict(row)


def seed_hypothesis(store: Store, *, launch_id: str, text: str, prereg_id: str | None = None) -> str:
    hyp_id = new_id("HYP")
    insert(
        store, "hypothesis",
        {"hyp_id": hyp_id, "text": text, "status": "open", "prereg_id": prereg_id, "created_ts": now(), "created_by_launch": launch_id},
    )
    return hyp_id
