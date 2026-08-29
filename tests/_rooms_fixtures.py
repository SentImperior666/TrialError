"""Not a test module (pytest only collects ``test_*.py``) — shared fixture
builders for the ``trialerror.rooms`` test suite.

Self-contained per the established precedent (``tests/_verify_fixtures.py``
module docstring: importing a CONCURRENT lane's private test helpers risks
colliding with a file that lane is still editing; this build's own
``bootstrap_launch`` is a small, deliberate duplicate of the same shape
every other fixture module carries, not a shortcut around that rule).
"""

from __future__ import annotations

from typing import Any

from trialerror.stores.store import Store
from trialerror.stores.writer import insert
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

__all__ = ["bootstrap_launch", "seed_idea", "seed_template"]


def bootstrap_launch(store: Store, *, agent_kind: str = "tester") -> str:
    """Insert a fresh account/session/launch chain and return the
    ``launch_id`` — every room write this module's callers exercise is
    XID-validated against ``platform.launch``. Each call makes a NEW
    account+session+launch (never reuses one), which is exactly what a
    test needing two visibly-distinct identities (e.g. an idea's author vs.
    a different participant) wants."""
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "test account", "created_ts": now()})
    session_id = new_id("SESS")
    insert(store, "session", {"session_id": session_id, "account_id": account_id, "opened_ts": now(), "status": "open"})
    launch_id = new_id("LNCH")
    insert(
        store,
        "launch",
        {
            "launch_id": launch_id,
            "account_id": account_id,
            "program_id": "PROG-test",
            "session_id": session_id,
            "agent_kind": agent_kind,
            "model_class": "top",
            "model": "sonnet",
            "purpose": "fixture",
            "est_tokens": 100,
            "booked_ts": now(),
            "state": "PROVISIONAL",
        },
    )
    return launch_id


def seed_idea(store: Store, *, author_launch: str, body: str = "test idea") -> str:
    """A minimal ``knowledge.idea`` row, for exercising the NEITHER-
    ownership invariant (``trialerror.rooms.api.post_message`` refuses a turn
    from the SAME launch that authored the idea a discussion point names)."""
    idea_id = new_id("IDEA")
    insert(
        store,
        "idea",
        {"idea_id": idea_id, "round_id": None, "author_launch": author_launch, "body": body, "status": "raw", "created_ts": now()},
    )
    return idea_id


def seed_template(store: Store, *, type_key: str = "room_theory_doc", gated: bool = False) -> dict[str, Any]:
    """A minimal ``template`` row — ``trialerror.artifacts.registry.
    create_artifact`` (which ``trialerror.rooms.api.register_room_deliverable``
    wires to) requires an existing ``template.type_key`` (same-file FK).
    This module does not seed templates itself (out of lane — see
    ``trialerror/rooms/api.py``'s own module TRIALERROR-DEV-NOTE item 5)."""
    row = {"type_key": type_key, "title": type_key, "version": "1", "path": f"templates/{type_key}.md", "gated": int(gated)}
    return insert(store, "template", row)
