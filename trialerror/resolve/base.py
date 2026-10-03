"""The resolver's shared shape: ``Description``, the prefix table every
per-store module registers into, and the ``describe()`` dispatcher.

Design Section 2: "the prefix table. Each kind's reader lives in one module
per store (``resolve/knowledge.py``, ``resolve/ops.py``,
``resolve/platform.py``, ``resolve/files.py`` for PKT), registered by
prefix." ``purpose`` is built from what the store knows, and says so when
it knows nothing rather than guessing (Section 6 trap 2).

Trap 1 (Section 6): this module is never given a slice-scoped or seat-
bound connection, and nothing here narrows "not found" into "exists but
withheld" -- a missing row and an out-of-scope row must read the same, so
the resolver is simply never handed a store that would tell them apart.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

__all__ = [
    "Description",
    "Reader",
    "register",
    "describe",
    "describe_line",
    "PREFIX_TABLE",
    "GenericSpec",
]


@dataclass
class Description:
    """What ``describe()`` answers for one id. ``related`` is
    ``[(id, kind_words, title)]`` -- the "which other things it is tied to"
    line, never more than a handful of entries."""

    id: str
    kind: str
    kind_words: str
    title: str | None = None
    state_words: str | None = None
    purpose: str | None = None
    related: list[tuple[str, str, str]] = field(default_factory=list)
    found: bool = True
    store: str | None = None


class Store(Protocol):  # pragma: no cover - structural typing only
    """What a reader needs: a duck-typed ``RoStore``/``Store`` (four
    connections by db_kind name) plus the program root PKT readers use."""

    platform: sqlite3.Connection | None
    ops: sqlite3.Connection | None
    knowledge: sqlite3.Connection | None
    jobs: sqlite3.Connection | None
    program_root: Any


Reader = Callable[[Store, str], Description]

#: prefix -> reader. Populated by each per-store module's ``register()``
#: call at import time (``trialerror/resolve/__init__.py`` imports every
#: module below through this one, at describe()'s first call).
_READERS: dict[str, Reader] = {}


def register(prefix: str, reader: Reader) -> None:
    """Register ``reader`` for ``prefix``. Called once per kind, at import
    time, by ``knowledge.py``/``ops.py``/``platform.py``/``files.py``."""
    _READERS[prefix] = reader


@dataclass(frozen=True)
class GenericSpec:
    """A kind this package recognises but has no dedicated reader for yet
    (Section 0 aims at "every id the stores mint"; the deep readers this
    lane wrote by hand cover the kinds the design worked through -- SRC,
    DOC, CHK, ROOM, CR, the C-#### rulings and LNCH -- everything else
    below still answers honestly from its own row, just without the
    cross-references those seven get). ``db_kind`` is one of
    ``platform``/``ops``/``knowledge``/``jobs``; ``title_cols``/
    ``state_cols`` are tried in order against the row's actual columns."""

    db_kind: str
    table: str
    pk_column: str
    kind_words: str
    title_cols: tuple[str, ...] = ("title", "topic", "summary", "name", "lemma", "key", "label")
    state_cols: tuple[str, ...] = ("state", "status")


#: Every other prefix the stores mint (ground truth: ``new_id(<prefix>)``
#: call sites, cross-checked against ``tests/_store_fixtures.py``'s
#: dependency-ordered insert of one row per table). SRC/DOC/CHK/ANC/ART/
#: ROOM/CR/LNCH/PKT are NOT here -- they have their own rich readers below
#: and in ``knowledge.py``/``ops.py``/``platform.py``/``files.py``.
PREFIX_TABLE: dict[str, GenericSpec] = {
    "ELM": GenericSpec("knowledge", "element", "element_id", "a document element"),
    "CLM": GenericSpec("knowledge", "claim", "claim_id", "a claim"),
    "ENT": GenericSpec("knowledge", "entity", "entity_id", "a knowledge-graph entity"),
    "REL": GenericSpec("knowledge", "relation", "rel_id", "a knowledge-graph relation"),
    "MRG": GenericSpec("knowledge", "merge_proposal", "prop_id", "a knowledge-graph merge proposal"),
    "HYP": GenericSpec("knowledge", "hypothesis", "hyp_id", "a hypothesis"),
    "VRD": GenericSpec("knowledge", "verdict", "verdict_id", "a verdict"),
    "RJDG": GenericSpec("knowledge", "verdict_rejudge", "rejudge_id", "a verdict re-judgment"),
    "EXP": GenericSpec("knowledge", "experiment", "exp_id", "an experiment"),
    "IDEA": GenericSpec("knowledge", "idea", "idea_id", "an idea"),
    "REC": GenericSpec("knowledge", "record", "record_id", "a registration record"),
    "EDGE": GenericSpec("knowledge", "prov_edge", "edge_id", "a provenance edge"),
    "SUM": GenericSpec("knowledge", "summary", "summary_id", "a summary"),
    "WF": GenericSpec("knowledge", "web_fetch", "fetch_id", "a web fetch"),
    "SEVD": GenericSpec("knowledge", "source_evidence", "evidence_id", "a source's evidence row"),
    "DOSS": GenericSpec("knowledge", "source_dossier", "dossier_id", "a source dossier"),
    "TERM": GenericSpec("knowledge", "term", "term_id", "a lexicon term"),
    "ALIAS": GenericSpec("knowledge", "term_alias", "alias_id", "a term alias"),
    "SENSE": GenericSpec("knowledge", "term_sense", "sense_id", "a term sense"),
    "TSE": GenericSpec("knowledge", "term_sense_evidence", "evidence_id", "a term sense's evidence row"),
    "TREL": GenericSpec("knowledge", "term_relation", "rel_id", "a term relation"),
    "SESS": GenericSpec("ops", "session", "session_id", "a session"),
    "EVT": GenericSpec("ops", "event", "event_id", "an event"),
    "THR": GenericSpec("ops", "thread", "thread_id", "a feed thread"),
    "POST": GenericSpec("ops", "feed_post", "post_id", "a feed post"),
    "INBX": GenericSpec("ops", "inbox_item", "item_id", "an inbox item"),
    "PREG": GenericSpec("ops", "prereg", "prereg_id", "a pre-registration"),
    "ROST": GenericSpec("ops", "lens_roster", "roster_id", "a lens roster row"),
    "ASGN": GenericSpec("ops", "lens_assignment", "assign_id", "a lens assignment"),
    "MEM": GenericSpec("ops", "memory_item", "memory_item_id", "a memory item"),
    "MREL": GenericSpec("ops", "memory_relation", "relation_id", "a memory relation"),
    "POOL": GenericSpec("platform", "budget_pool", "pool_id", "a budget pool"),
    "QSNAP": GenericSpec("platform", "quota_snapshot", "snap_id", "a quota snapshot"),
    "CALIB": GenericSpec("platform", "calibration", "calib_id", "a calibration row"),
    "ACC": GenericSpec("platform", "account", "account_id", "an account"),
    "JOB": GenericSpec("jobs", "job", "job_id", "a background job"),
}


def _generic_describe(spec: GenericSpec, stores: Store, id_: str) -> Description:
    conn = getattr(stores, spec.db_kind, None)
    if conn is None:
        return Description(
            id=id_, kind=id_.split("-", 1)[0], kind_words=spec.kind_words, found=False, store=spec.db_kind
        )
    try:
        row = conn.execute(f"SELECT * FROM {spec.table} WHERE {spec.pk_column} = ?", (id_,)).fetchone()
    except sqlite3.OperationalError:
        return Description(
            id=id_, kind=id_.split("-", 1)[0], kind_words=spec.kind_words, found=False, store=spec.db_kind
        )
    prefix = id_.split("-", 1)[0]
    if row is None:
        return Description(id=id_, kind=prefix, kind_words=spec.kind_words, found=False, store=spec.db_kind)
    d = dict(row)
    cols = set(d)
    title = next((str(d[c])[:120] for c in spec.title_cols if c in cols and d.get(c) is not None), None)
    state = next((str(d[c]) for c in spec.state_cols if c in cols and d.get(c) is not None), None)
    return Description(
        id=id_,
        kind=prefix,
        kind_words=spec.kind_words,
        title=title,
        state_words=state,
        purpose=f"no purpose is recorded for this {spec.kind_words}",
        found=True,
        store=spec.db_kind,
    )


def _prefix_of(id_: str) -> str:
    return id_.split("-", 1)[0] if "-" in id_ else id_


def _safe_dispatch(reader: Reader, stores: Store, id_: str) -> Description:
    """Review finding N-2: a dedicated reader queries its tables unguarded
    (only the generic reader catches ``OperationalError``) -- on an older
    schema (a store predating a migration this build assumes, e.g. no
    ``room_link`` or ``ruling`` table yet) a raised ``sqlite3.Error`` used
    to take down the whole caller: the DECIDE panel, or a packet's whole
    DECIDE section, rather than just this one id's line."""
    try:
        return reader(stores, id_)
    except sqlite3.Error:
        return Description(
            id=id_, kind=_prefix_of(id_),
            kind_words="an id whose details could not be read (a store error)",
            found=False, store=None,
        )


def describe(id_: str, stores: Store) -> Description:
    """What ``id_`` is, in one place. Never raises: an unrecognised prefix,
    a missing row, or a store error reading a known kind's own tables all
    come back as a :class:`Description` with ``found=False`` -- the CLI and
    the packet renderer decide what to say."""
    id_ = str(id_).strip()
    # importing the per-store modules here (not at package import time)
    # avoids a circular import: they import ``register``/``Description``
    # from this module.
    from trialerror.resolve import files as _files  # noqa: F401
    from trialerror.resolve import knowledge as _knowledge  # noqa: F401
    from trialerror.resolve import ops as _ops  # noqa: F401
    from trialerror.resolve import platform as _platform  # noqa: F401

    if "::" in id_:
        # the packet/dashboard's own "<gate_id>::<edit_id>" ref shape
        # (design Section 1: "EDIT inside gate.edits") -- not a prefix at
        # all, so it is checked before the ordinary prefix split below.
        composite_reader = _READERS.get("::")
        if composite_reader is not None:
            return _safe_dispatch(composite_reader, stores, id_)
    prefix = _prefix_of(id_)
    reader = _READERS.get(prefix)
    if reader is not None:
        return _safe_dispatch(reader, stores, id_)
    spec = PREFIX_TABLE.get(prefix)
    if spec is not None:
        return _generic_describe(spec, stores, id_)
    return Description(id=id_, kind=prefix, kind_words="unknown kind of id", found=False, store=None)


def describe_line(desc: Description, *, label: str | None = None) -> str:
    """``"<label>: <kind_words> '<title>' (<state_words>). <Purpose>. Tied
    to: ...  [<id>]"``, the one rendering both the CLI and the packet's ref
    renderer use (design Section 4 item 2: "each ref is rendered through
    ``describe()`` ... with the id last, in brackets"). Review finding N-1:
    the purpose and the ties are no longer left to ``--json`` alone -- the
    operator's own complaint (CHARTER.md) was "no idea what it is for", and
    a bare kind/title/state answers what the id names without answering
    that."""
    if not desc.found:
        if desc.kind_words == "unknown kind of id":
            text = "unknown kind of id"
        else:
            text = f"{desc.kind_words}, not found in {desc.store or 'its store'}"
    else:
        bits = [desc.kind_words]
        if desc.title:
            bits.append(f"'{desc.title}'")
        text = " ".join(bits)
        if desc.state_words:
            text += f" ({desc.state_words})"
        sentences = [text]
        if desc.purpose:
            sentences.append(desc.purpose[:1].upper() + desc.purpose[1:])
        if desc.related:
            tied = "; ".join(f"{title} [{rid}]" for rid, _kind_words, title in desc.related)
            sentences.append(f"Tied to: {tied}")
        text = ". ".join(s.rstrip(".") for s in sentences)
    prefixed = f"{label}: {text}" if label else text
    return f"{prefixed} [{desc.id}]"
