"""The XID cross-store reference registry. Design Section 4's binding rule:
"any reference that crosses a ``.db`` file boundary is written ``XID``: a
typed id column whose target-existence is validated by the
``trialerror.stores`` write API at write time (refuse-on-missing), and
re-checked offline by ``trialerror doctor`` referential-integrity scans."

This module is the ONE place that enumeration lives in code (mirroring the
design's own prose enumeration, Section 4 l.172-176 as amended by the
delta-verify residuals applied at M1 kickoff — see
``docs/DESIGN_v0.md``): every ``(table, column) -> target`` pair below is
transcribed directly from that corrected list, not re-derived. Two
NON-members, stated explicitly so a future reader doesn't "fix" them back
in: ``launch.workpackage`` (free-form scoping string, no target table) and
same-file ``FK`` columns (SQLite enforces those itself via
``PRAGMA foreign_keys=ON`` in ``trialerror.stores.connection``).
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["DbKind", "XidTarget", "XID_REGISTRY", "xid_columns_for_table"]

#: The four store "kinds" a :class:`XidTarget` can point into — matches the
#: attribute names on ``trialerror.stores.store.Store``.
DbKind = str  # "platform" | "ops" | "knowledge" | "jobs"


@dataclass(frozen=True)
class XidTarget:
    db: DbKind
    table: str
    pk_column: str


#: (table, column) -> XidTarget. Grouped by design Section 4's own
#: enumeration order (the four bullets), then by which DB the referencING
#: table lives in.
XID_REGISTRY: dict[tuple[str, str], XidTarget] = {
    # ---- bullet 1: every *_launch / launch_id column outside platform.db,
    # -> platform.launch. -------------------------------------------------
    # ops.db referencers
    ("artifact", "registered_by_launch"): XidTarget("platform", "launch", "launch_id"),
    ("gate", "critic_launch"): XidTarget("platform", "launch", "launch_id"),
    ("gate_transition", "by_launch"): XidTarget("platform", "launch", "launch_id"),
    ("event", "launch_id"): XidTarget("platform", "launch", "launch_id"),
    ("thread", "created_by_launch"): XidTarget("platform", "launch", "launch_id"),
    ("feed_post", "launch_id"): XidTarget("platform", "launch", "launch_id"),
    ("lens_assignment", "launch_id"): XidTarget("platform", "launch", "launch_id"),
    ("room_turn", "author_launch"): XidTarget("platform", "launch", "launch_id"),
    # knowledge.db referencers
    ("source", "registered_by_launch"): XidTarget("platform", "launch", "launch_id"),
    ("quote_anchor", "created_by_launch"): XidTarget("platform", "launch", "launch_id"),
    ("claim", "created_by_launch"): XidTarget("platform", "launch", "launch_id"),
    ("entity", "created_by_launch"): XidTarget("platform", "launch", "launch_id"),
    ("merge_proposal", "proposed_by_launch"): XidTarget("platform", "launch", "launch_id"),
    ("hypothesis", "created_by_launch"): XidTarget("platform", "launch", "launch_id"),
    ("verdict", "issued_by_launch"): XidTarget("platform", "launch", "launch_id"),
    ("experiment", "created_by_launch"): XidTarget("platform", "launch", "launch_id"),
    ("idea", "author_launch"): XidTarget("platform", "launch", "launch_id"),
    ("prov_edge", "launch_id"): XidTarget("platform", "launch", "launch_id"),
    # build-v2-summary (knowledge_v3_summary_table): summary.created_by_launch.
    ("summary", "created_by_launch"): XidTarget("platform", "launch", "launch_id"),
    # ---- bullet 2: prereg_id referenced from knowledge.db (prereg lives in
    # ops.db). --------------------------------------------------------------
    ("hypothesis", "prereg_id"): XidTarget("ops", "prereg", "prereg_id"),
    ("verdict", "prereg_id"): XidTarget("ops", "prereg", "prereg_id"),
    ("experiment", "prereg_id"): XidTarget("ops", "prereg", "prereg_id"),
    # ---- bullet 3: launch.session_id (session rows live in ops.db). ------
    ("launch", "session_id"): XidTarget("ops", "session", "session_id"),
    # ---- bullet 4 (delta-verify residual, applied at M1 kickoff):
    # session.account_id and memory_item.account_id -> platform.account. ---
    ("session", "account_id"): XidTarget("platform", "account", "account_id"),
    ("memory_item", "account_id"): XidTarget("platform", "account", "account_id"),
    # ---- bullet 5: record.artifact_id / idea.feed_post_ref (ops.db
    # targets referenced from knowledge.db). --------------------------------
    ("record", "artifact_id"): XidTarget("ops", "artifact", "artifact_id"),
    ("idea", "feed_post_ref"): XidTarget("ops", "feed_post", "post_id"),
    # build-v2-polish (ops_v3_rooms_created_ts_scored_link_deliverable):
    # room_link.idea_id -- a per-discussion-point idea-vetting link
    # (trialerror/rooms/api.py module TRIALERROR-DEV-NOTE item 4), ops.db ->
    # knowledge.db, so this crosses the same file boundary bullet 5's own
    # two entries do (just knowledge -> ops instead of ops -> knowledge).
    ("room_link", "idea_id"): XidTarget("knowledge", "idea", "idea_id"),
    # build-v2dash-data (ops_v4_criterion_and_feed_post_translation): the
    # AISPEAK translator's sidecar table (docs/reviews/
    # AISPEAK_TRANSLATOR_DESIGN.md Section 4.2). ``post_id`` is NOT here --
    # it's a same-file FK (both tables live in ops.db). ``created_by_launch``
    # mirrors every other *_launch column's bullet-1 treatment;
    # ``faithfulness_verdict_id`` crosses to knowledge.verdict (the design
    # doc's own prose says "ops.verdict", but verdict actually lives in
    # knowledge.db per trialerror/stores/schema/knowledge.py -- registered against
    # the real table, not the doc's typo).
    ("feed_post_translation", "created_by_launch"): XidTarget("platform", "launch", "launch_id"),
    ("feed_post_translation", "faithfulness_verdict_id"): XidTarget("knowledge", "verdict", "verdict_id"),
}


def xid_columns_for_table(table: str) -> dict[str, XidTarget]:
    """All XID columns declared for ``table`` (empty dict if none)."""
    return {col: target for (tbl, col), target in XID_REGISTRY.items() if tbl == table}
