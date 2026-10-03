"""Readers for ops.db: CR (the gate the operator's complaint room-example
sits beside), EDIT (inside ``gate.edits``, never its own row), ROOM (the
operator's own worked example: "what is this room ROOM-01M2X...?"), and
C-#### rulings. Everything else ops.db mints (SESS, EVT, THR, POST, INBX,
PREG, ROST, ASGN, MEM, MREL) uses the generic reader registered in
``base.PREFIX_TABLE``.
"""

from __future__ import annotations

import json

from trialerror.resolve.base import Description, register


def _row(conn, table: str, pk_column: str, pk_value: str) -> dict | None:
    if conn is None or not pk_value:
        return None
    row = conn.execute(f"SELECT * FROM {table} WHERE {pk_column} = ?", (pk_value,)).fetchone()
    return dict(row) if row is not None else None


_REPRO_WORDS = {
    "match": "reproduced cleanly",
    "mismatch": "did not reproduce (mismatch)",
    "unrun": "not yet reproduced",
}


def describe_cr(stores, id_: str) -> Description:
    """Design note: "CR: state, verdict, reproduction status in words,
    disposition, artifact title." """
    row = _row(stores.ops, "gate", "gate_id", id_)
    if row is None:
        return Description(id=id_, kind="CR", kind_words="a gate", found=False, store="ops" if stores.ops is not None else None)
    artifact = _row(stores.ops, "artifact", "artifact_id", row.get("artifact_id") or "")
    parts = [row["state"]]
    if row.get("verdict"):
        parts.append(f"verdict {row['verdict']}")
    repro = _REPRO_WORDS.get(row.get("reproduction_status"))
    if repro:
        parts.append(repro)
    if row.get("disposition"):
        parts.append(f"disposition {row['disposition']}")
    related = []
    if artifact is not None:
        related.append((row["artifact_id"], "the artifact it gates", artifact.get("title") or row["artifact_id"]))
    purpose = (artifact or {}).get("purpose") or "no purpose is recorded for this gate's artifact"
    return Description(
        id=id_, kind="CR", kind_words="a gate", title=(artifact or {}).get("title"),
        state_words=", ".join(parts), purpose=purpose, related=related, found=True, store="ops",
    )


def describe_edit(stores, id_: str) -> Description:
    """A bare ``EDIT-...`` id: not its own row (design Section 1: "EDIT
    inside gate.edits"), so this scans every gate's ``edits`` JSON array for
    a matching ``edit_id``. Callers who already have the gate (the packet's
    own ``"<gate_id>::<edit_id>"`` refs) reach the richer ``::`` reader
    below instead, which skips the scan."""
    if stores.ops is None:
        return Description(id=id_, kind="EDIT", kind_words="a gate edit", found=False, store="ops")
    rows = stores.ops.execute("SELECT gate_id, edits FROM gate WHERE edits IS NOT NULL AND edits != ''").fetchall()
    for r in rows:
        try:
            edits = json.loads(r["edits"]) or []
        except (TypeError, ValueError):
            continue
        for e in edits:
            if e.get("edit_id") == id_:
                return _describe_edit_entry(stores, r["gate_id"], e)
    return Description(id=id_, kind="EDIT", kind_words="a gate edit", found=False, store="ops")


def _describe_edit_entry(stores, gate_id: str, entry: dict) -> Description:
    state = "verified" if entry.get("verified") else ("sent back" if entry.get("sent_back") else "unverified")
    words = "a blocking gate edit" if entry.get("blocking") else "a non-blocking gate edit"
    return Description(
        id=str(entry.get("edit_id")), kind="EDIT", kind_words=words, title=entry.get("text"), state_words=state,
        purpose=f"raised on the gate {gate_id}", related=[(gate_id, "its gate", gate_id)], found=True, store="ops",
    )


def _describe_edit_ref(stores, id_: str) -> Description:
    """The packet/dashboard's own ``"<gate_id>::<edit_id>"`` reference
    shape (``_gate_edit_items``' id)."""
    gate_id, _, edit_id = id_.partition("::")
    row = _row(stores.ops, "gate", "gate_id", gate_id)
    if row is None:
        return Description(id=id_, kind="EDIT", kind_words="a gate edit", found=False, store="ops")
    try:
        edits = json.loads(row.get("edits") or "[]") or []
    except (TypeError, ValueError):
        edits = []
    entry = next((e for e in edits if e.get("edit_id") == edit_id), None)
    if entry is None:
        return Description(id=id_, kind="EDIT", kind_words="a gate edit", found=False, store="ops")
    desc = _describe_edit_entry(stores, gate_id, entry)
    desc.id = id_
    return desc


def _freeze_event(conn, room_id: str) -> dict | None:
    row = conn.execute(
        "SELECT ts, payload FROM event WHERE type = 'room_frozen' "
        "AND json_extract(payload, '$.room_id') = ? ORDER BY ts DESC, rowid DESC LIMIT 1",
        (room_id,),
    ).fetchone()
    if row is None:
        return None
    payload = json.loads(row["payload"])
    return {"ts": row["ts"], "reason": payload.get("reason")}


def _close_record(conn, room_id: str) -> dict | None:
    row = conn.execute(
        "SELECT ts, payload FROM event WHERE type = 'room_closed' "
        "AND json_extract(payload, '$.room_id') = ? ORDER BY ts DESC, rowid DESC LIMIT 1",
        (room_id,),
    ).fetchone()
    if row is None:
        return None
    payload = json.loads(row["payload"])
    return {"ts": row["ts"], "reason": payload.get("reason"), "decided_by": payload.get("decided_by")}


def describe_room(stores, id_: str) -> Description:
    """The operator's own example (CHARTER.md, verbatim): "What is this
    room ROOM-01M2XAX6268A0YE6WMR2ABETFY? ... I have no idea what is it
    for". Design note: "ROOM: topic, state, freeze and close records,
    room_link -> idea -> round, deliverable artifact."."""
    row = _row(stores.ops, "room", "room_id", id_)
    if row is None:
        return Description(id=id_, kind="ROOM", kind_words="a discussion room", found=False, store="ops" if stores.ops is not None else None)
    state = row["state"]
    if state == "frozen":
        info = _freeze_event(stores.ops, id_)
        bits = ["frozen"]
        if info:
            bits.append(f"on {info['ts'][:10]}")
            if info.get("reason"):
                bits.append(f"because {info['reason']}")
        state_words = " ".join(bits)
    elif state == "closed":
        info = _close_record(stores.ops, id_)
        bits = ["closed"]
        if info:
            bits.append(f"on {info['ts'][:10]}")
            if info.get("decided_by"):
                bits.append(f"by the operator's decision {info['decided_by']}")
        state_words = " ".join(bits)
    else:
        state_words = state
    related: list[tuple[str, str, str]] = []
    idea_ids: list[str] = []
    round_id = None
    if stores.ops is not None:
        for link in stores.ops.execute("SELECT idea_id FROM room_link WHERE room_id = ?", (id_,)).fetchall():
            if link["idea_id"]:
                idea_ids.append(link["idea_id"])
    for idea_id in idea_ids:
        idea = _row(stores.knowledge, "idea", "idea_id", idea_id)
        if idea is not None:
            round_id = round_id or idea.get("round_id")
            snippet = (idea.get("body") or "")[:80].strip()
            related.append((idea_id, "an idea it vets", snippet or idea_id))
    deliverable_id = row.get("deliverable_artifact_id")
    if deliverable_id:
        art = _row(stores.ops, "artifact", "artifact_id", deliverable_id)
        related.append((deliverable_id, "its deliverable artifact", (art or {}).get("title") or deliverable_id))
    if idea_ids:
        purpose = f"vets {len(idea_ids)} idea(s)" + (f" from round {round_id}" if round_id else "")
    else:
        purpose = "no discussion point in this room is linked to an idea"
    return Description(
        id=id_, kind="ROOM", kind_words="a discussion room", title=row.get("topic"), state_words=state_words,
        purpose=purpose, related=related, found=True, store="ops",
    )


def describe_ruling(stores, id_: str) -> Description:
    row = _row(stores.ops, "ruling", "ruling_id", id_)
    if row is None:
        return Description(id=id_, kind="C", kind_words="an operator ruling", found=False, store="ops" if stores.ops is not None else None)
    related = []
    if row.get("supersedes"):
        prior = _row(stores.ops, "ruling", "ruling_id", row["supersedes"])
        related.append((row["supersedes"], "the ruling it supersedes", (prior or {}).get("summary") or row["supersedes"]))
    purpose = f"applies to: {row['domains']}" if row.get("domains") else None
    return Description(
        id=id_, kind="C", kind_words="an operator ruling", title=row.get("summary"), state_words=row.get("status"),
        purpose=purpose, related=related, found=True, store="ops",
    )


register("CR", describe_cr)
register("EDIT", describe_edit)
register("::", _describe_edit_ref)
register("ROOM", describe_room)
register("C", describe_ruling)
