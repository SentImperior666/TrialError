"""Reader for PKT (design Section 1: "PKT in packet files") -- a decision
packet item, which lives in ``packet/pending.jsonl``/``answers.jsonl``, not
a store table (``trialerror/packet/store.py``'s own docstring)."""

from __future__ import annotations

from trialerror.resolve.base import Description, register


def describe_pkt(stores, id_: str) -> Description:
    root = getattr(stores, "program_root", None)
    if root is None:
        return Description(id=id_, kind="PKT", kind_words="a decision packet item", found=False, store="files")
    from trialerror.packet.store import packet_settings, read_jsonl

    try:
        settings = packet_settings(root)
    except Exception:  # noqa: BLE001 - an unreadable config must not raise out of describe()
        return Description(id=id_, kind="PKT", kind_words="a decision packet item", found=False, store="files")
    for row in read_jsonl(settings.pending):
        if row.get("id") == id_:
            return Description(
                id=id_, kind="PKT", kind_words="a decision packet item", title=row.get("what"),
                state_words=row.get("status"), purpose=row.get("why") or None, found=True, store="files",
            )
    return Description(id=id_, kind="PKT", kind_words="a decision packet item", found=False, store="files")


register("PKT", describe_pkt)
