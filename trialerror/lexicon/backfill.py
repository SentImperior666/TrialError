"""The bulk proposal routes: register records in, definition claims in, and
the source relink that follows once the sources behind them are ingested
(design ``docs/reviews/LANE_E_TERM_STORE_DESIGN.md`` §4 "register import /
backfill", §7's ``backfill-records`` / ``backfill-claims`` / ``relink``
verbs, rulings L-E2, L-E3 and L-E6).

Three functions, one shape. Each reads rows that already exist, proposes
one sense per row through :func:`trialerror.lexicon.api.propose`, and
reports counts; none of them decides anything the store had not already
decided.

**Why the register import lands ``current``** (ruling L-E2). The
denominator these rows come from was gated once already, by a human, at the
time the register was built. Re-queuing every one of them for a second
acceptance would be ceremony: the same person clicking the same yes on the
same rows. So each imported sense lands ``current`` with
``procedure_version='register-import-v1'`` and a ``review_after`` stamp -- the
decay flag (engram-F5) is what brings them back for review over time, one
window at a time, rather than all at once on day one. What the import does
NOT decide is the polysemy it uncovers: a lemma that reads two ways across
two registers opens a pending ``conflicts_with`` item and stays pending
until a launch resolves it.

**Idempotence is structural, not remembered.** ``term_sense`` carries
``UNIQUE(origin_kind, origin_ref) WHERE origin_ref IS NOT NULL``, and
``propose`` short-circuits on it. A second backfill run over the same rows
inserts nothing at all -- no sense, no evidence, no term, no event beyond
the run's own summary. That is the property the acceptance criterion
measures (design §9 B), and it holds without this module keeping a
watermark of its own.

**The import route's own gloss cap** (build step 1c, decision D5). Every
sense this module proposes carries ``origin_kind='record_import'``, so
:func:`trialerror.lexicon.api.propose` measures its gloss against
:data:`trialerror.lexicon.policy.GLOSS_MAX_WORDS_IMPORT` (160 words) and
not the hand-written 80. That is not the cap being relaxed. An imported
gloss is the source register's own reading, carried across whole so a
reviewer can see what the register said -- and ``review_after`` already
schedules that look. The first real import refused **271 of 7,475 rows** at
82-93 words apiece on the strict cap: rows dropped for being faithful.
Above 160 an import is still refused, by the same error and for the
original reason -- a payload that long is a section, not a gloss. The
family-map route below writes a hand-authored gloss and keeps the strict
cap, as does any later ``supersede`` of an imported reading.

**One bad row does not stop the run.** A record whose payload has no name,
or whose description is longer than the applicable gloss cap, is counted
under ``refused`` with the reason and the run continues. Aborting a 7,000-row
import on row 4,000 would leave the store half-imported and the operator
with a partial state to reason about; refusing loudly per row and
reporting the list at the end is the same information, without that state.
Nothing is silently dropped: every refusal is in the returned dict and in
the ``term_backfill_run`` event.

The per-row handler catches ``ValidationError`` as well as ``LexiconError``
(fix pass, finding F3). It used to catch only the latter, and a UNIQUE
violation does not arrive as one: ``trialerror.stores.writer.insert`` wraps
``sqlite3.IntegrityError`` in ``trialerror.stores.errors.ValidationError``,
a ``StoreError``. Two runs over one store therefore killed the loser at its
first collision -- no further rows imported, no ``term_backfill_run`` event
appended, nothing an operator could reconcile against -- which is the exact
outcome the paragraph above says this module exists to prevent, reached by
a route it had not considered. The DATA was never at risk (four concurrent
importers produced zero duplicate lemmas, zero duplicate origins and zero
orphan terms); the RUN was. A raced row is now counted under ``refused``
like any other, and the run finishes and reports.

**The relink never touches an anchor** (ruling L-E6). It rewrites
``term_sense_evidence.source_key`` and nothing else -- the anchor id, the
verbatim ``cite_raw`` string the register carried (D31), the excerpt, the
launch, the timestamps all stay byte-for-byte what they were. The D31
re-anchor pass on the sandbox is a separate step that runs *after* this
one; the verb and its guarantees are what this lane owes it.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping, Sequence

from trialerror.events.api import append_event
from trialerror.lexicon import api, policy
from trialerror.lexicon.errors import InvalidTermInputError, LexiconError
from trialerror.stores.errors import ValidationError
from trialerror.stores.writer import update
from trialerror.util.timeutil import now

__all__ = [
    "FAMILY_PROCEDURE_VERSION",
    "CLAIM_BACKFILL_PROCEDURE_VERSION",
    "MAX_FAMILY_EVIDENCE",
    "MAX_REPORTED_REFUSALS",
    "backfill_records",
    "backfill_claims",
    "relink_evidence_sources",
]

#: ``procedure_version`` for a level-1 family term minted from the
#: ``--family-map`` (ruling L-E3). Distinct from the instance rows'
#: :data:`trialerror.lexicon.policy.RECORD_IMPORT_PROCEDURE_VERSION` because
#: the two are not the same act: an instance sense is a register row read
#: as-is, a family sense is a hand-authored gloss the orchestrator extracted
#: from a document this repo never sees.
FAMILY_PROCEDURE_VERSION = "register-family-map-v1"

#: ``procedure_version`` for a definition claim projected into the store by
#: :func:`backfill_claims`. Not ``extract-term-v1``: that one means "the
#: extraction judgment named this lemma itself"; this one means "an
#: operator mapped this already-accepted claim onto a lemma afterwards",
#: and a later audit should be able to tell those apart.
CLAIM_BACKFILL_PROCEDURE_VERSION = "claim-backfill-v1"

#: How many register rows are attached as evidence to a family term. A
#: family stands on the instance rows filed under it, but attaching all
#: 300 of them would bury the sense's evidence list without adding a source
#: the disjointness rule had not already counted.
MAX_FAMILY_EVIDENCE = 5

#: How many refusals the result dict names individually. The count is
#: always exact; the list is capped so a pathological import does not
#: return a 7,000-entry payload through a CLI envelope.
MAX_REPORTED_REFUSALS = 20


def _extract_register_key() -> str:
    """The extraction queue's private register namespace, imported lazily.

    ``trialerror.ingest.extract`` imports this package's API for its accept
    hook, so importing it at module scope here would close the loop. The
    value is a constant either way -- this is about import order, not about
    the number changing."""
    from trialerror.ingest.extract import EXTRACT_REGISTER_KEY

    return EXTRACT_REGISTER_KEY


def _payload(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _tags_of(payload: Mapping[str, Any]) -> list[str]:
    tags = payload.get("f_tags")
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    if not isinstance(tags, (list, tuple)):
        return []
    return [str(t).strip() for t in tags if str(t).strip()]


def _record_rows(store, register_keys: Sequence[str] | None, limit: int | None) -> list[dict[str, Any]]:
    conn = store.conn_for_table("record")
    sql = "SELECT record_id, register_key, payload, seq FROM record WHERE register_key != ?"
    params: list[Any] = [_extract_register_key()]
    if register_keys:
        sql += f" AND register_key IN ({','.join('?' for _ in register_keys)})"
        params += list(register_keys)
    sql += " ORDER BY register_key, seq, record_id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _family_entry(entry: Any) -> tuple[str, str] | None:
    """``{tag: {"lemma","gloss"}}`` or ``{tag: "lemma"}`` -> ``(lemma, gloss)``."""
    if isinstance(entry, str):
        lemma = entry.strip()
        return (lemma, f"the {lemma} family of mechanisms") if lemma else None
    if isinstance(entry, Mapping):
        lemma = str(entry.get("lemma") or "").strip()
        gloss = str(entry.get("gloss") or "").strip() or f"the {lemma} family of mechanisms"
        return (lemma, gloss) if lemma else None
    return None


def backfill_records(
    store,
    *,
    by_launch: str,
    register_keys: Sequence[str] | None = None,
    family_map: Mapping[str, Any] | None = None,
    limit: int | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Import ``knowledge.record`` rows as ``current`` instance senses.

    Reads every record whose ``register_key`` is not the extraction queue's
    own (that namespace is the merge-review queue, not a register), maps
    the payload as design §4 specifies -- ``name`` -> lemma, ``description``
    -> gloss, ``f_tags`` -> tags, ``cites_raw`` -> the evidence row's
    verbatim citation string, ``register_key`` -> ``source_key`` -- and
    proposes one sense per row at ``status='current'``.

    ``family_map`` (``{tag: {"lemma", "gloss"}}``, ruling L-E3) additionally
    mints one level-1 ``family`` term per tag that actually appears in the
    imported rows, standing on those rows as evidence. Without it, family
    terms are simply absent and the instance terms still carry their tag
    codes -- which is why nothing downstream blocks on a map this repo is
    never allowed to hold.

    Returns exact counts plus the refusal list; appends one
    ``term_backfill_run`` event.
    """
    api._require_launch(store, by_launch, what="backfill_records")

    rows = _record_rows(store, register_keys, limit)
    terms_created = 0
    senses_created = 0
    evidence_rows = 0
    already_present = 0
    conflicts_opened = 0
    duplicates_opened = 0
    refused: list[dict[str, str]] = []
    refused_count = 0
    tag_examples: dict[str, list[str]] = {}
    keys_seen: set[str] = set()

    for row in rows:
        payload = _payload(row["payload"])
        if payload is None:
            refused_count += 1
            if len(refused) < MAX_REPORTED_REFUSALS:
                refused.append({"record_id": row["record_id"], "reason": "payload is not a JSON object"})
            continue
        lemma = str(payload.get("name") or "").strip()
        gloss = str(payload.get("description") or "").strip()
        if not lemma or not gloss:
            refused_count += 1
            if len(refused) < MAX_REPORTED_REFUSALS:
                missing = "name" if not lemma else "description"
                refused.append({"record_id": row["record_id"], "reason": f"payload has no {missing}"})
            continue

        tags = _tags_of(payload)
        for tag in tags:
            tag_examples.setdefault(tag, [])
            if len(tag_examples[tag]) < MAX_FAMILY_EVIDENCE:
                tag_examples[tag].append(row["record_id"])
        keys_seen.add(row["register_key"])

        try:
            result = api.propose(
                store,
                lemma=lemma,
                gloss=gloss,
                origin_kind="record_import",
                origin_ref=row["record_id"],
                evidence=[
                    {
                        "kind": "record",
                        "ref_id": row["record_id"],
                        "source_key": row["register_key"],
                        "cite_raw": payload.get("cites_raw"),
                        "excerpt": gloss,
                    }
                ],
                by_launch=by_launch,
                procedure_version=policy.RECORD_IMPORT_PROCEDURE_VERSION,
                granularity="instance",
                tags=tags or None,
                status="current",
                config=config,
            )
        except (LexiconError, ValidationError) as exc:
            # ValidationError is here because a UNIQUE violation -- the shape
            # a concurrent run produces -- is a StoreError, not a
            # LexiconError (finding F3). Counting it under `refused` is what
            # keeps a raced row from ending the run.
            refused_count += 1
            if len(refused) < MAX_REPORTED_REFUSALS:
                refused.append({"record_id": row["record_id"], "reason": str(exc)})
            continue

        if not result["created_sense"]:
            already_present += 1
            continue
        senses_created += 1
        terms_created += 1 if result["created_term"] else 0
        evidence_rows += len(result["evidence_ids"])
        conflicts = result.get("conflicts") or {}
        if conflicts.get("opened"):
            conflicts_opened += 1
        candidates = result.get("candidates") or {}
        duplicates_opened += len(candidates.get("opened") or [])

    if family_map:
        families = _mint_family_terms(
            store, family_map, tag_examples, by_launch=by_launch, config=config
        )
    else:
        # No map supplied is the NORMAL state (ruling L-E3: it arrives at E5,
        # from a document outside this repo). Every tag the import saw is
        # reported unmapped, which is the doctor check's WARN and not a
        # failure -- the instance terms carry their tag codes regardless.
        families = {"created": 0, "existing": 0, "unmapped_tags": sorted(tag_examples)}

    summary = {
        "status": "ok",
        "records_seen": len(rows),
        "register_keys": sorted(keys_seen),
        "terms_created": terms_created,
        "senses_created": senses_created,
        "evidence_rows": evidence_rows,
        "already_present": already_present,
        "conflicts_opened": conflicts_opened,
        "duplicates_opened": duplicates_opened,
        "refused_count": refused_count,
        "refused": refused,
        "families": families,
    }
    append_event(
        store,
        event_type="term_backfill_run",
        payload={k: v for k, v in summary.items() if k != "status"} | {"route": "records"},
        launch_id=by_launch,
    )
    return summary


def _mint_family_terms(
    store,
    family_map: Mapping[str, Any],
    tag_examples: Mapping[str, Sequence[str]],
    *,
    by_launch: str,
    config: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """One ``family`` term per mapped tag that the imported rows actually
    used, standing on up to :data:`MAX_FAMILY_EVIDENCE` of those rows.

    Idempotent by an explicit check rather than by the origin index: a
    family sense has no origin row (its gloss came from a document outside
    this repo), so ``origin_ref`` is NULL and the partial unique index does
    not apply. Minting one against a fabricated ``origin_ref`` would buy
    idempotence at the price of a ``prov_edge`` pointing at a record that
    does not exist -- a lie in the provenance graph to save a SELECT.
    """
    created = 0
    existing = 0
    unmapped = [tag for tag in tag_examples if tag not in family_map]
    for tag in sorted(tag_examples):
        entry = _family_entry(family_map.get(tag))
        if entry is None:
            continue
        lemma, gloss = entry
        term = api.find_term(store, lemma)
        if term is not None and _has_family_sense(store, term["term_id"]):
            existing += 1
            continue
        try:
            api.propose(
                store,
                lemma=lemma,
                gloss=gloss,
                origin_kind="manual",
                origin_ref=None,
                evidence=[
                    {"kind": "record", "ref_id": record_id}
                    for record_id in list(tag_examples[tag])[:MAX_FAMILY_EVIDENCE]
                ],
                by_launch=by_launch,
                procedure_version=FAMILY_PROCEDURE_VERSION,
                granularity="family",
                tags=[tag],
                status="current",
                config=config,
            )
        except (LexiconError, ValidationError):
            continue
        created += 1
    return {"created": created, "existing": existing, "unmapped_tags": sorted(unmapped)}


def _has_family_sense(store, term_id: str) -> bool:
    row = store.conn_for_table("term_sense").execute(
        "SELECT 1 FROM term_sense WHERE term_id = ? AND procedure_version = ? "
        "AND status IN ('proposed','current') LIMIT 1",
        (term_id, FAMILY_PROCEDURE_VERSION),
    ).fetchone()
    return row is not None


def _unprojected_definition_claims(store) -> list[dict[str, Any]]:
    """Live definition claims with no ``term_sense_evidence(kind='claim')``
    behind them -- the same population the ``definition_claims_unprojected``
    doctor check counts (design §6), read once here so the check and the
    backfill cannot disagree about what is left to do."""
    rows = store.conn_for_table("claim").execute(
        "SELECT c.claim_id, c.text, c.anchor_id FROM claim c "
        "WHERE c.kind = 'definition' AND c.expired_at IS NULL AND NOT EXISTS ("
        "  SELECT 1 FROM term_sense_evidence e "
        "  WHERE e.evidence_kind = 'claim' AND e.ref_id = c.claim_id AND e.retracted_ts IS NULL"
        ") ORDER BY c.created_at, c.claim_id"
    ).fetchall()
    return [dict(r) for r in rows]


def backfill_claims(
    store,
    *,
    by_launch: str,
    lemma_map: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project already-accepted definition claims onto terms, using an
    operator-supplied ``{claim_id: lemma}`` (or ``{claim_id: {"lemma",
    "gloss"}}``) map.

    **Unmapped claims are counted, never guessed.** A claim's text is a
    sentence, not a lemma; inferring which words in it name the thing being
    defined is a judgment, and this module makes none. What is left over
    stays in the ``definition_claims_unprojected`` doctor count, which is
    the backlog made visible rather than a number quietly going to zero.

    Senses land ``current``: the claim itself came through the merge-review
    queue, and the map is a launch-attributed act of naming.
    """
    api._require_launch(store, by_launch, what="backfill_claims")
    mapping = dict(lemma_map or {})

    claims = _unprojected_definition_claims(store)
    projected = 0
    terms_created = 0
    refused: list[dict[str, str]] = []
    refused_count = 0
    unmapped: list[str] = []

    for claim in claims:
        entry = mapping.get(claim["claim_id"])
        if entry is None:
            unmapped.append(claim["claim_id"])
            continue
        if isinstance(entry, Mapping):
            lemma = str(entry.get("lemma") or "").strip()
            gloss = str(entry.get("gloss") or "").strip() or str(claim["text"] or "").strip()
        else:
            lemma = str(entry).strip()
            gloss = str(claim["text"] or "").strip()
        try:
            if not lemma:
                raise InvalidTermInputError(f"lemma_map entry for {claim['claim_id']!r} names no lemma")
            result = api.propose(
                store,
                lemma=lemma,
                gloss=gloss,
                origin_kind="extract",
                origin_ref=claim["claim_id"],
                evidence=[{"kind": "claim", "ref_id": claim["claim_id"]}],
                by_launch=by_launch,
                procedure_version=CLAIM_BACKFILL_PROCEDURE_VERSION,
                status="current",
                config=config,
            )
        except (LexiconError, ValidationError) as exc:
            refused_count += 1
            if len(refused) < MAX_REPORTED_REFUSALS:
                refused.append({"claim_id": claim["claim_id"], "reason": str(exc)})
            continue
        if result["created_sense"]:
            projected += 1
            terms_created += 1 if result["created_term"] else 0

    summary = {
        "status": "ok",
        "claims_seen": len(claims),
        "projected": projected,
        "terms_created": terms_created,
        "unmapped": len(unmapped),
        "unmapped_claim_ids": unmapped[:MAX_REPORTED_REFUSALS],
        "refused_count": refused_count,
        "refused": refused,
    }
    append_event(
        store,
        event_type="term_backfill_run",
        payload={k: v for k, v in summary.items() if k != "status"} | {"route": "claims"},
        launch_id=by_launch,
    )
    return summary


def _unlinked_source_keys(store) -> dict[str, int]:
    """``{source_key: row count}`` for evidence whose ``source_key`` matches
    no ``source.source_id`` -- the ``term_evidence_source_unlinked`` doctor
    check's population, and the number the relink is measured against."""
    rows = store.conn_for_table("term_sense_evidence").execute(
        "SELECT e.source_key AS key, count(*) AS n FROM term_sense_evidence e "
        "WHERE e.retracted_ts IS NULL AND NOT EXISTS ("
        "  SELECT 1 FROM source s WHERE s.source_id = e.source_key"
        ") GROUP BY e.source_key ORDER BY e.source_key"
    ).fetchall()
    return {r["key"]: int(r["n"]) for r in rows}


def relink_evidence_sources(
    store,
    source_map: Mapping[str, str],
    *,
    by_launch: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Rewrite ``term_sense_evidence.source_key`` from a
    ``{register_key: source_id}`` map -- the ``trialerror term relink`` verb
    (design §2; ruling L-E6).

    A register row's ``source_key`` is the register's own id until the
    document behind it has been ingested and has a real ``source`` row. This
    is the one-way step that closes that gap, and it is deliberately
    narrow:

    * every target must already name a ``source`` row -- a map entry
      pointing at nothing is refused *before* any row is rewritten, because
      relinking evidence to a source that does not exist would break the
      disjointness rule in the direction that hides conflicts;
    * only ``source_key`` changes. Anchors, ``cite_raw``, excerpts, launches
      and timestamps are untouched, byte for byte (the D31 re-anchor pass is
      a separate, later step);
    * it is idempotent -- a second run finds no row under the old key and
      rewrites nothing.

    ``dry_run=True`` reports exactly what would change and writes nothing,
    not even the event.
    """
    api._require_launch(store, by_launch, what="relink_evidence_sources")
    mapping = {str(k): str(v) for k, v in dict(source_map or {}).items() if str(k) and str(v)}
    if not mapping:
        raise InvalidTermInputError("relink needs a non-empty {register_key: source_id} map")

    conn = store.conn_for_table("term_sense_evidence")
    missing = [
        target
        for target in sorted(set(mapping.values()))
        if conn.execute("SELECT 1 FROM source WHERE source_id = ? LIMIT 1", (target,)).fetchone() is None
    ]
    if missing:
        raise InvalidTermInputError(
            f"relink map names {len(missing)} source_id(s) with no source row: {missing[:5]!r} -- "
            "evidence is not relinked to a source that does not exist"
        )

    keys = sorted(mapping)
    marks = ",".join("?" for _ in keys)
    rows = [
        dict(r)
        for r in conn.execute(
            f"SELECT evidence_id, source_key FROM term_sense_evidence WHERE source_key IN ({marks}) "
            "ORDER BY source_key, evidence_id",
            keys,
        ).fetchall()
    ]
    per_key: dict[str, int] = {}
    for row in rows:
        per_key[row["source_key"]] = per_key.get(row["source_key"], 0) + 1

    if not dry_run:
        ts = now()
        for row in rows:
            update(
                store,
                "term_sense_evidence",
                pk_column="evidence_id",
                pk_value=row["evidence_id"],
                changes={"source_key": mapping[row["source_key"]]},
            )
        append_event(
            store,
            event_type="term_relink_run",
            payload={
                "rows_relinked": len(rows),
                "per_key": per_key,
                "map": {k: mapping[k] for k in keys},
            },
            launch_id=by_launch,
            ts=ts,
        )

    return {
        "status": "ok",
        "dry_run": dry_run,
        "rows_relinked": 0 if dry_run else len(rows),
        "rows_matched": len(rows),
        "per_key": per_key,
        "unmatched_keys": [k for k in keys if k not in per_key],
        "unlinked_remaining": _unlinked_source_keys(store),
    }


def unlinked_source_keys(store) -> dict[str, int]:
    """Public read of the relink backlog, for the CLI's ``relink --status``
    rendering and for step E3's doctor check to share rather than restate."""
    return _unlinked_source_keys(store)


def iter_register_keys(store, *, exclude_extraction: bool = True) -> Iterable[str]:
    """Every register namespace the backfill can read from, in key order.

    Offered so the CLI can print the choices for ``--register-key`` instead
    of asking the operator to remember them."""
    sql = "SELECT DISTINCT register_key FROM record"
    params: list[Any] = []
    if exclude_extraction:
        sql += " WHERE register_key != ?"
        params.append(_extract_register_key())
    sql += " ORDER BY register_key"
    return [r[0] for r in store.conn_for_table("record").execute(sql, params).fetchall()]
