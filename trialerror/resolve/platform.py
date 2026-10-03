"""Reader for platform.db's LNCH (design note: "LNCH: agent kind, purpose,
state, session, and spend when L7 has landed"). POOL, QSNAP, CALIB and ACC
use the generic reader registered in ``base.PREFIX_TABLE``.

Spend (L7) has not landed in this harness yet, so this reader reports only
what the launch row itself already carries: ``state``, and, when L6's
``spawn_ts`` is set, that the launch was spawned then -- never a
"stranded" verdict, which needs the filesystem search
``trialerror.budget.pools.find_stranded_launches`` does; that stays a
``budget status`` question, not a bare id lookup's.
"""

from __future__ import annotations

from trialerror.resolve.base import Description, register


def describe_lnch(stores, id_: str) -> Description:
    conn = stores.platform
    row = None
    if conn is not None:
        row = conn.execute("SELECT * FROM launch WHERE launch_id = ?", (id_,)).fetchone()
        row = dict(row) if row is not None else None
    if row is None:
        return Description(id=id_, kind="LNCH", kind_words="a launch", found=False, store="platform" if conn is not None else None)
    bits = [row["state"]]
    if row.get("spawn_ts"):
        bits.append(f"spawned at {row['spawn_ts']}")
    if row.get("actual_tokens") is not None:
        bits.append(f"{row['actual_tokens']} tokens (reconciled)")
    related = [(row["session_id"], "its session", row["session_id"])] if row.get("session_id") else []
    if row.get("parent_launch"):
        related.append((row["parent_launch"], "the launch that spawned it", row["parent_launch"]))
    return Description(
        id=id_, kind="LNCH", kind_words="a launch", title=f"{row['agent_kind']} ({row['model']})",
        state_words=", ".join(bits), purpose=row.get("purpose") or "no purpose is recorded for this launch",
        related=related, found=True, store="platform",
    )


register("LNCH", describe_lnch)
