"""The idea content pipeline. Design Section 4.1 ``idea`` table (M1-landed
DDL, ``trialerror/stores/schema/ops.py``): "schema-now, writers-later landing
zone" — this module is that writer. Build brief: "the C-0047-style
full-text feed posting rides M5 (``post_feed`` binds authorship — your
tooling just supplies the content pipeline)": :func:`write_idea` creates the
``idea`` row; :func:`link_idea_to_feed_post` back-fills ``feed_post_ref``
once a caller (a skill, or the orchestrator) has posted the idea's full
text via ``trialerror.events.api.post_feed`` — this module never calls
``post_feed`` itself (that would put authorship-binding logic in two
places; M5's module docstring is explicit that it is the ONE place that
contract is enforced).

TRIALERROR-DEV-NOTE (idea-schema field gap — RESOLVED by schema-v2,
build-v1-schemav2): the build brief names ``home``/``assumed_circle``/
``provenance``/``tier``/``set_distance`` as fields the design's idea schema
carries. The M1-landed ``idea`` DDL had no such columns — only
``idea_id | round_id | author_launch | body | slice_ref | feed_post_ref |
status | created_ts`` — so this module used to pack them as a JSON object
into ``idea.slice_ref`` instead (docs/INTEGRATION_NOTES.md item 14;
docs/the migration-plan notes (internal, not in this export) Section 4 item 3). The
``knowledge_v2_idea_promoted_columns`` migration
(``trialerror/stores/schema/knowledge.py``) adds all five as real columns;
:func:`write_idea` below now writes them there directly. ``slice_ref`` is
kept populated for ONE version, via the exact same :func:`build_slice_ref`
JSON convention as before (now explicitly DEPRECATED — see its own
docstring) so any caller still reading ``idea.slice_ref`` does not break;
a future version may drop that write once nothing depends on it.
``assign_id``/``arm``/``distance_score``/``cluster_id`` were never named in
the promoted-columns list (INTEGRATION_NOTES item 14 names only the five
above), so they remain slice_ref-only.

Schema-v6 repeats that same promotion for the ten ideation-record fields
:data:`AIIF_FIELDS` names (``requirements`` … ``convergent_with``), which
rode inside the ``provenance`` JSON object under the interim convention,
and widens ``idea.status`` with ``eliminated`` and ``merged``.
:func:`read_idea` reads column-then-``provenance``-JSON, so a row written
before that migration answers identically to one written since — there was
no backfill pass and none is needed.
"""

from __future__ import annotations

import json
import re
from typing import Any

from trialerror.stores import get, insert, update
from trialerror.stores.store import Store
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

__all__ = [
    "IDEA_STATUSES",
    "ARCHIVED_STATUS",
    "PROVENANCE_DOCS_KEY",
    "PROVENANCE_DOCS_ALIAS",
    "normalize_requirements",
    "normalize_provenance",
    "normalize_operation_declared",
    "RECORD_FIELD_ALIASES",
    "REQUIRED_RECORD_FIELDS",
    "ARCHIVED_OPTIONAL_RECORD_FIELDS",
    "record_to_write_idea_kwargs",
    "intake_records",
    "IDEA_TIERS",
    "AIIF_FIELDS",
    "AIIF_JSON_FIELDS",
    "build_slice_ref",
    "write_idea",
    "read_idea",
    "link_idea_to_feed_post",
]

#: ``idea.status`` CHECK constraint (design Section 4.1, widened by
#: schema-v6 and schema-v9), transcribed for caller-side validation before
#: the DB round-trip. ``eliminated`` = killed at the convergence gate but
#: kept in the never-reset archive (revivable only by ruling); ``merged`` =
#: folded into another record as a near-duplicate. Neither is a deletion:
#: both statuses stay readable and stay in the reference set forever.
#:
#: ``archived`` (schema-v9) is what a PRIOR round's candidate or request row
#: takes when it is written in so the archive can be judged against as
#: reference set R2. It is in the reference set and is not a candidate,
#: which is the one thing every other status says falsely about such a row:
#: ``raw`` would put it in this round's judged scope and consolidate it at
#: the end of a round it did not take part in.
IDEA_STATUSES: tuple[str, ...] = ("raw", "consolidated", "promoted", "eliminated", "merged", "archived")

#: The status a row takes when it is in the reference set and is not a
#: candidate. Exported by name because three separate rules read it -- the
#: merge never folds INTO one, the judged screen never consolidates one, and
#: the R2 bundle is usually built FOR them.
ARCHIVED_STATUS = "archived"

#: ``idea.tier`` CHECK constraint (schema-v2), transcribed for caller-side
#: validation before the DB round-trip -- the same near/moderate/far
#: vocabulary ``trialerror.lens.stratify``/``trialerror.lens.assign`` already use.
IDEA_TIERS: tuple[str, ...] = ("near", "moderate", "far")

#: The schema-v6 promoted record fields, in the order the record schema
#: states them. Before v6 these rode inside the ``provenance`` JSON blob;
#: :func:`read_idea` still reads that blob as a fallback, so a row written
#: under the interim convention answers identically to one written since.
AIIF_FIELDS: tuple[str, ...] = (
    "requirements",
    "recipe_card",
    "operation_declared",
    "probe",
    "surprise",
    "author_rationale",
    "parent_ids",
    "statement_sha256",
    "corpus_snapshot_id",
    "convergent_with",
)

#: The subset of :data:`AIIF_FIELDS` stored as a JSON array (a list of ids)
#: rather than free text -- encoded on write, decoded on read.
AIIF_JSON_FIELDS: tuple[str, ...] = ("parent_ids", "convergent_with")


def build_slice_ref(
    *,
    assign_id: str | None = None,
    arm: str | None = None,
    distance_score: float | None = None,
    cluster_id: str | None = None,
    home: str | None = None,
    assumed_circle: str | None = None,
    tier: str | None = None,
    set_distance: float | None = None,
    provenance: Any = None,
) -> str:
    """Build the JSON convention this module packs into ``idea.slice_ref``.

    DEPRECATED as of schema-v2 for ``home``/``assumed_circle``/``tier``/
    ``set_distance``/``provenance`` — those five are now real ``idea``
    columns (see module docstring); :func:`write_idea` writes them there
    directly and calls this function only to keep ``slice_ref`` populated
    for backward compat during the deprecation window. ``assign_id``/
    ``arm``/``distance_score``/``cluster_id`` were never promoted and remain
    slice_ref-only — this function is still the right (only) way to carry
    those. Every field is optional and omitted from the JSON object when
    ``None`` (a lens output with no assignment behind it — a freeform idea —
    still gets a valid, if mostly-empty, slice_ref)."""
    obj: dict[str, Any] = {}
    for key, value in (
        ("assign_id", assign_id),
        ("arm", arm),
        ("distance_score", distance_score),
        ("cluster_id", cluster_id),
        ("home", home),
        ("assumed_circle", assumed_circle),
        ("tier", tier),
        ("set_distance", set_distance),
        ("provenance", provenance),
    ):
        if value is not None:
            obj[key] = value
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


#: Where a record names the documents it was written from. The novelty
#: screen's envelope builder reads exactly this key
#: (``trialerror.lens.novelty._provenance_docs``), and a record without it
#: reaches a judge declaring no provenance at all -- so it is validated at
#: intake rather than discovered at judging time.
PROVENANCE_DOCS_KEY = "docs"

#: The spelling an assignment-shaped caller reaches for first. Accepted and
#: COPIED into ``docs`` rather than rejected: the two mean the same thing to
#: everyone who writes one, and only one of them is read.
PROVENANCE_DOCS_ALIAS = "slice"

_AXIS_RE = re.compile(r"\b(opportunity|method)\s*[:=]\s*([^,;\n]+)", re.IGNORECASE)


def normalize_requirements(value: Any) -> str | None:
    """``requirements`` as the TEXT column stores it.

    The record schema asks for "2 to 5 lines", so a caller hands a list --
    and a list reached sqlite as a bare ``type 'list' is not supported``,
    which names neither the field nor the fix. A list/tuple becomes newline
    bullets here, which is what the column held anyway when a caller
    formatted it by hand."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        lines = [str(item).strip() for item in value if str(item).strip()]
        return "\n".join(line if line.startswith(("-", "*", "\u2022")) else f"- {line}" for line in lines) or None
    raise ValueError(
        f"write_idea: requirements must be a string or a list of lines, got {type(value).__name__}"
    )


def normalize_provenance(value: Any) -> Any:
    """``provenance`` as an object that names its documents.

    An ideation record's provenance is not free text: the judge envelope is
    built from ``provenance.docs``, the slice audit resolves a post's
    citations against the same documents, and a record that declares none
    reaches a judge saying it came from nowhere. So a provenance that IS
    given must be an object carrying ``docs`` (or :data:`PROVENANCE_DOCS_ALIAS`,
    which is copied into ``docs``), and anything else is refused by name
    rather than stored and discovered later.

    ``None`` stays ``None``: a freeform idea with no slice behind it is a
    real shape this module has always written, and inventing an empty
    ``docs`` for it would be a claim nobody made. An EXPLICIT ``docs: []``
    is accepted for the same reason in reverse -- an author saying "from no
    document" has said something, and the defect this closes is the key
    being absent, which says nothing at all."""
    if value is None:
        return None
    obj: Any = value
    if isinstance(obj, str):
        try:
            decoded = json.loads(obj)
        except (TypeError, ValueError):
            decoded = None
        if not isinstance(decoded, dict):
            raise ValueError(
                "write_idea: provenance must be an object carrying "
                f"{PROVENANCE_DOCS_KEY!r} (a list of doc ids), got a plain string. The novelty screen's "
                f"judge envelope reads provenance.{PROVENANCE_DOCS_KEY}, so a record that declares no "
                "documents is judged as having come from nowhere"
            )
        obj = decoded
    if not isinstance(obj, dict):
        raise ValueError(
            f"write_idea: provenance must be an object carrying {PROVENANCE_DOCS_KEY!r} (a list of doc "
            f"ids), got {type(value).__name__}"
        )
    out = dict(obj)
    docs = out.get(PROVENANCE_DOCS_KEY)
    if docs is None and out.get(PROVENANCE_DOCS_ALIAS) is not None:
        docs = out[PROVENANCE_DOCS_ALIAS]
        out[PROVENANCE_DOCS_KEY] = docs
    if isinstance(docs, str):
        docs = [docs]
        out[PROVENANCE_DOCS_KEY] = docs
    if not isinstance(docs, (list, tuple)):
        raise ValueError(
            f"write_idea: provenance must carry {PROVENANCE_DOCS_KEY!r} -- the list of doc ids the record "
            f"was written from (or {PROVENANCE_DOCS_ALIAS!r}, which is copied into it). "
            f"Got keys {sorted(out)!r}"
        )
    out[PROVENANCE_DOCS_KEY] = [str(d) for d in docs]
    if PROVENANCE_DOCS_ALIAS in out:
        out[PROVENANCE_DOCS_ALIAS] = out[PROVENANCE_DOCS_KEY]
    return out


def normalize_operation_declared(value: Any) -> str | None:
    """``operation_declared`` in the canonical two-axis form the
    distribution card reads (``"<opportunity>/<method>"``).

    Three spellings arrive in practice and all three are accepted: the
    object ``{"opportunity": ..., "method": ...}``, the labelled string
    ``"opportunity:bridge, method:formalize"`` (or either half of it alone),
    and the canonical slash form itself. A bare token with neither label nor
    slash is read as an opportunity, which is how
    ``trialerror.lens.novelty._split_operation`` has always read it.

    Neither axis is checked against its vocabulary here: the distribution
    audit counts an unknown value into ``off_taxonomy``, and a refusal would
    make this module the place a taxonomy is revised."""
    if value is None:
        return None
    opportunity: Any = None
    method: Any = None
    if isinstance(value, dict):
        opportunity, method = value.get("opportunity"), value.get("method")
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.startswith("{"):
            try:
                decoded = json.loads(text)
            except (TypeError, ValueError):
                decoded = None
            if isinstance(decoded, dict):
                return normalize_operation_declared(decoded)
        pairs = {k.lower(): v.strip() for k, v in _AXIS_RE.findall(text)}
        if pairs:
            opportunity, method = pairs.get("opportunity"), pairs.get("method")
        elif "/" in text:
            left, right = text.split("/", 1)
            opportunity, method = left.strip(), right.strip()
        else:
            opportunity = text
    opportunity = str(opportunity).strip() if opportunity else ""
    method = str(method).strip() if method else ""
    if not opportunity and not method:
        return None
    if not method:
        return opportunity
    return f"{opportunity}/{method}"


def _text_or_json(value: Any) -> str | None:
    """``assumed_circle``/``provenance`` are documented ``TEXT/json``
    columns (docs/the migration-plan notes (internal, not in this export) Section 4 item 3): a caller-supplied
    plain string is stored verbatim (never re-quoted into a JSON string
    literal); anything else (dict/list/number/bool) is JSON-encoded. ``None``
    stays ``None`` (column left unset, not the literal string ``"null"``)."""
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _json_list_or_none(value: Any, *, field: str) -> str | None:
    """Encode a :data:`AIIF_JSON_FIELDS` value for storage. A list/tuple is
    JSON-encoded; an already-encoded JSON string is stored verbatim (so a
    caller round-tripping :func:`read_idea`'s raw row back through
    :func:`write_idea` does not double-encode); ``None`` stays ``None``."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value), ensure_ascii=False)
    raise ValueError(f"write_idea: {field} must be a list of ids (or a pre-encoded JSON string), got {type(value).__name__}")


def write_idea(
    store: Store,
    *,
    round_id: str | None,
    author_launch: str,
    body: str,
    home: str | None = None,
    assumed_circle: str | None = None,
    provenance: Any = None,
    tier: str | None = None,
    set_distance: float | None = None,
    slice_ref: str | None = None,
    status: str = "raw",
    requirements: str | None = None,
    recipe_card: str | None = None,
    operation_declared: str | None = None,
    probe: str | None = None,
    surprise: str | None = None,
    author_rationale: str | None = None,
    parent_ids: Any = None,
    statement_sha256: str | None = None,
    corpus_snapshot_id: str | None = None,
    convergent_with: Any = None,
    extra: Any = None,
    now_ts: str | None = None,
) -> dict[str, Any]:
    """Write one ``idea`` row. ``body`` is FULL TEXT (design: never a
    summary — the same C-0047 rule ``trialerror.events.api.post_feed`` enforces
    for feed posts, carried into this module's own docstring since the
    idea's body is exactly what a later ``post_feed`` call will post
    verbatim).

    ``home``/``assumed_circle``/``provenance``/``tier``/``set_distance`` are
    the schema-v2 promoted columns (docs/INTEGRATION_NOTES.md item 14) —
    written directly, not just packed into JSON. ``slice_ref`` remains for
    ``assign_id``/``arm``/``distance_score``/``cluster_id`` (never
    promoted — still JSON-only, via :func:`build_slice_ref`) and for
    backward compat: pass an already-built ``slice_ref`` (e.g. one carrying
    those four) and this function merges the five promoted fields' values
    into it too before writing, so a caller still reading ``idea.slice_ref``
    directly (the pre-schema-v2 convention) keeps seeing them. Omit
    ``slice_ref`` and this function builds one from just the promoted
    fields (``None`` if none were given — a plain freeform idea, unchanged
    from pre-schema-v2 behavior).

    :data:`AIIF_FIELDS` are the schema-v6 promoted columns, all optional and
    all defaulting to ``None`` so every pre-v6 caller keeps working
    unchanged. ``author_rationale`` and ``surprise`` are stored but are
    generator-facing only — they never belong in a judge envelope, and this
    module writes them exactly so a later envelope builder can prove it
    dropped them. ``parent_ids``/``convergent_with`` take a list of ids and
    are stored as a JSON array.

    Three fields are NORMALISED AND VALIDATED here rather than by each
    caller, because each was a defect a live round found and none of them
    surfaces until much later: ``requirements`` given as a list
    (:func:`normalize_requirements` -- a list used to reach sqlite as a bare
    "type 'list' is not supported"), ``provenance`` without its ``docs``
    (:func:`normalize_provenance` -- the judge envelope reads that key, so
    the record reached the judge declaring no source), and
    ``operation_declared`` in any of its three spellings
    (:func:`normalize_operation_declared` -- the distribution card reads one
    of them).

    ``extra`` (knowledge-v11, lane FB-7 item 8b) is the record's own free
    block -- any keys at all, stored as the JSON object the author wrote.
    It is rendered into the ONE text field a judge sees by exactly the
    function a plant's is (``trialerror.lens.novelty.render_extra_text``),
    which is the point: the envelope's shape is what keeps a plant
    indistinguishable from a record, so the two must be carried the same
    way or the difference becomes a tell."""
    if status not in IDEA_STATUSES:
        raise ValueError(f"write_idea: status must be one of {IDEA_STATUSES!r}, got {status!r}")
    if tier is not None and tier not in IDEA_TIERS:
        raise ValueError(f"write_idea: tier must be one of {IDEA_TIERS!r}, got {tier!r}")
    requirements = normalize_requirements(requirements)
    provenance = normalize_provenance(provenance)
    operation_declared = normalize_operation_declared(operation_declared)

    promoted = {"home": home, "assumed_circle": assumed_circle, "tier": tier, "set_distance": set_distance, "provenance": provenance}
    if slice_ref is not None:
        merged = json.loads(slice_ref)
        merged.update({k: v for k, v in promoted.items() if v is not None})
        effective_slice_ref: str | None = json.dumps(merged, sort_keys=True, ensure_ascii=False)
    else:
        built = build_slice_ref(**promoted)
        effective_slice_ref = built if built != "{}" else None

    row = {
        "idea_id": new_id("IDEA"),
        "round_id": round_id,
        "author_launch": author_launch,
        "body": body,
        "home": home,
        "assumed_circle": _text_or_json(assumed_circle),
        "provenance": _text_or_json(provenance),
        "tier": tier,
        "set_distance": set_distance,
        "slice_ref": effective_slice_ref,
        "feed_post_ref": None,
        "status": status,
        "created_ts": now_ts or now(),
        "requirements": requirements,
        "recipe_card": recipe_card,
        "operation_declared": operation_declared,
        "probe": probe,
        "surprise": surprise,
        "author_rationale": author_rationale,
        "parent_ids": _json_list_or_none(parent_ids, field="parent_ids"),
        "statement_sha256": statement_sha256,
        "corpus_snapshot_id": corpus_snapshot_id,
        "convergent_with": _json_list_or_none(convergent_with, field="convergent_with"),
        "extra": _text_or_json(extra),
    }
    return insert(store, "idea", row)


def _provenance_obj(raw: Any) -> dict[str, Any]:
    """``idea.provenance`` as a dict, or ``{}`` when it is absent, a plain
    string, or not decodable — the interim convention packed a JSON OBJECT
    there, and anything else simply carries no fallback values."""
    if not raw or not isinstance(raw, str):
        return {}
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def read_idea(store: Store, *, idea_id: str) -> dict[str, Any] | None:
    """One ``idea`` row with :data:`AIIF_FIELDS` resolved, or ``None`` if
    ``idea_id`` names no row.

    Resolution order per field: the schema-v6 COLUMN first, then the same
    key inside the ``provenance`` JSON object (the interim convention rows
    written before that migration used), then ``None``. That fallback is
    the whole point of this function — an idea written under the interim
    convention reads back exactly like one written since, so no caller has
    to know which side of the migration a row landed on, and no backfill
    pass had to rewrite historical rows to make it so.

    :data:`AIIF_JSON_FIELDS` come back DECODED (a list), from either source;
    a stored value that will not decode as a JSON array is returned
    verbatim rather than being silently dropped."""
    row = get(store, "idea", pk_column="idea_id", pk_value=idea_id)
    if row is None:
        return None
    out = dict(row)
    fallback = _provenance_obj(out.get("provenance"))
    for field in AIIF_FIELDS:
        value = out.get(field)
        if value is None:
            value = fallback.get(field)
        if field in AIIF_JSON_FIELDS and isinstance(value, str):
            try:
                decoded = json.loads(value)
            except (TypeError, ValueError):
                decoded = value
            value = decoded if isinstance(decoded, list) else value
        out[field] = value
    return out


def link_idea_to_feed_post(store: Store, *, idea_id: str, feed_post_ref: str) -> None:
    """Back-fill ``idea.feed_post_ref`` after the caller has posted the
    idea's ``body`` via ``trialerror.events.api.post_feed`` (this module does
    not call ``post_feed`` itself — see module docstring). XID-validated
    against ``ops.feed_post`` by the same write API every other module
    uses (``trialerror.stores.xid``'s ``("idea", "feed_post_ref")`` entry)."""
    update(store, "idea", pk_column="idea_id", pk_value=idea_id, changes={"feed_post_ref": feed_post_ref})


#: The record fields an intake caller writes, mapped to :func:`write_idea`'s
#: own keyword. Both spellings of the two fields the agent prompt and the
#: schema name differently (``statement``/``body``, ``home_mechanic``/
#: ``home``) are accepted, because both are what a lens actually returns.
RECORD_FIELD_ALIASES: dict[str, str] = {
    "statement": "body",
    "body": "body",
    "home_mechanic": "home",
    "home": "home",
    "requirements": "requirements",
    "assumed_circle": "assumed_circle",
    "provenance": "provenance",
    "probe": "probe",
    "surprise": "surprise",
    "author_rationale": "author_rationale",
    "recipe_card": "recipe_card",
    "operation_declared": "operation_declared",
    "operation": "operation_declared",
    "tier": "tier",
    "set_distance": "set_distance",
    "parent_ids": "parent_ids",
    "statement_sha256": "statement_sha256",
    "corpus_snapshot_id": "corpus_snapshot_id",
    "convergent_with": "convergent_with",
    "status": "status",
    # Lane FB-7 item 8b: the record's own free block, the same one a plant
    # declaration takes. Any keys; they reach the judge as one text field.
    "extra": "extra",
}

#: What a record may not leave out. ``statement`` is the record; ``probe``
#: is what makes it testable ("a record with no probe is not finished" --
#: plugin/agents/lens.md); ``provenance`` is what makes it traceable.
REQUIRED_RECORD_FIELDS: tuple[str, ...] = ("statement", "probe", "provenance")

#: What an ARCHIVED row may leave out (lane FB-6 item 6). An archive intake
#: writes rows that already exist -- a prior round's candidate, a request row
#: from a list somebody kept -- and most of them were never written under a
#: schema that had a probe. "A record with no probe is not finished" is a
#: rule about a record this round is asking a lens to produce; an archived
#: row is not a candidate and is never consolidated, so the rule has nothing
#: to bite on and demanding the field only produced invented probes.
ARCHIVED_OPTIONAL_RECORD_FIELDS: tuple[str, ...] = ("probe",)


def record_to_write_idea_kwargs(
    record: Any, *, index: int = 0, default_status: str | None = None
) -> dict[str, Any]:
    """One intake record as :func:`write_idea` keywords -- validated, with
    every refusal naming the record's position and the field.

    ``default_status`` is the status the CALL carries (``lens intake
    --status archived``), needed here because what a record must carry
    depends on what it IS: an archived row may omit its ``probe``
    (:data:`ARCHIVED_OPTIONAL_RECORD_FIELDS`), a candidate may not.

    Raises :class:`ValueError`. The caller is expected to run this over
    EVERY record before writing any of them: a half-intaken file leaves a
    round whose record count means nothing."""
    where = f"record {index}"
    if not isinstance(record, dict):
        raise ValueError(f"lens intake: {where} is a {type(record).__name__}, not an object")
    unknown = sorted(set(record) - set(RECORD_FIELD_ALIASES))
    if unknown:
        raise ValueError(
            f"lens intake: {where} carries unknown field(s) {unknown!r}. The record schema is "
            f"{sorted(set(RECORD_FIELD_ALIASES))!r} -- a field nothing reads is a field the author "
            "wrote for nobody"
        )
    effective_status = record.get("status") if isinstance(record, dict) else None
    effective_status = effective_status if effective_status is not None else default_status
    optional = (
        set(ARCHIVED_OPTIONAL_RECORD_FIELDS) if effective_status == ARCHIVED_STATUS else set()
    )
    for field in REQUIRED_RECORD_FIELDS:
        if field in optional and record.get(field) is None:
            continue
        value = record.get(field) if field != "statement" else (record.get("statement") or record.get("body"))
        if field == "provenance":
            if record.get("provenance") is None:
                raise ValueError(
                    f"lens intake: {where} declares no provenance. Every record names the documents it "
                    f"was written from, as provenance.{PROVENANCE_DOCS_KEY}"
                )
            continue
        if not str(value or "").strip():
            raise ValueError(
                f"lens intake: {where} has no {field!r}, which the record schema requires"
                + (
                    f" (only an {ARCHIVED_STATUS!r} row may omit "
                    f"{list(ARCHIVED_OPTIONAL_RECORD_FIELDS)!r})"
                    if field in ARCHIVED_OPTIONAL_RECORD_FIELDS
                    else ""
                )
            )

    kwargs: dict[str, Any] = {}
    for key, value in record.items():
        kwargs[RECORD_FIELD_ALIASES[key]] = value
    # Validated HERE, not at write time: the same normalisers write_idea
    # runs, so a file refuses before its first row lands rather than after
    # its second. The refusal is re-raised with the record's POSITION on it:
    # "provenance must carry docs" is unactionable over a file of fourteen.
    try:
        kwargs["requirements"] = normalize_requirements(kwargs.get("requirements"))
        kwargs["provenance"] = normalize_provenance(kwargs.get("provenance"))
        kwargs["operation_declared"] = normalize_operation_declared(kwargs.get("operation_declared"))
    except ValueError as exc:
        raise ValueError(f"lens intake: {where}: {exc}") from exc
    if kwargs.get("tier") is not None and kwargs["tier"] not in IDEA_TIERS:
        raise ValueError(f"lens intake: {where} tier {kwargs['tier']!r} is not one of {IDEA_TIERS!r}")
    if kwargs.get("status") is not None and kwargs["status"] not in IDEA_STATUSES:
        raise ValueError(f"lens intake: {where} status {kwargs['status']!r} is not one of {IDEA_STATUSES!r}")
    if kwargs.get("extra") is not None:
        # Rendered HERE, and the result thrown away, purely to refuse a
        # malformed block with the record's position on it (lane FB-7 item
        # 8b). The stored value is the object the author wrote; the render
        # happens again at envelope-build time, by the same function a
        # plant's block goes through. Imported locally: trialerror.lens.novelty
        # imports THIS module, so a module-level import would be a cycle.
        from trialerror.lens.novelty import NoveltyError, render_extra_text

        try:
            render_extra_text(kwargs["extra"])
        except NoveltyError as exc:
            raise ValueError(f"lens intake: {where}: {exc}") from exc
    return kwargs


def intake_records(
    store: Store,
    *,
    round_id: str,
    records: Any,
    author_launch: str,
    assign_ids: Any = None,
    arm: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """Write a lens's returned records as ``idea`` rows -- all of them, or
    none of them.

    The whole file is validated first (:func:`record_to_write_idea_kwargs`)
    and only then written, and a write that fails anyway takes back the rows
    already written. A partially intaken file is the worst of the three
    outcomes: the round's record count, its per-arm n and its distribution
    card all become numbers about an incomplete batch, and nothing in the
    store says so.

    ``status`` is the disposition the whole CALL writes -- the one an
    ARCHIVE intake needs (``archived``: prior rounds' candidates and request
    rows, written in so a round can be judged against them as reference set
    R2). It is a default, not an override: a record that names its own
    ``status`` field keeps it, because the file is the more specific
    statement. Omitted, every record lands ``raw``, exactly as before.

    ``assign_ids``/``arm`` are the round-level link the intake CALL carries,
    written onto every record's ``slice_ref``. A record carrying its own
    ``assign_id`` field is refused as an unknown field (fix pass N-5): the
    schema refuses what nothing reads, and per-record slice attribution is
    not something this verb resolves -- intake one call per assignment set
    if the records differ in theirs."""
    if isinstance(records, dict):
        records = records.get("records", records.get("ideas"))
    if not isinstance(records, (list, tuple)):
        raise ValueError(
            "lens intake: the records file must hold a JSON list of records (or an object with a "
            f"'records' list), got {type(records).__name__}"
        )
    if not records:
        raise ValueError("lens intake: the records file holds no records")

    if status is not None and status not in IDEA_STATUSES:
        raise ValueError(f"lens intake: status must be one of {IDEA_STATUSES!r}, got {status!r}")
    assign_ids = [str(a) for a in (assign_ids or [])]
    prepared = [
        record_to_write_idea_kwargs(record, index=i, default_status=status)
        for i, record in enumerate(records)
    ]
    if status is not None:
        for kwargs in prepared:
            if kwargs.get("status") is None:
                kwargs["status"] = status

    written: list[dict[str, Any]] = []
    try:
        for kwargs in prepared:
            slice_obj: dict[str, Any] = {}
            if assign_ids:
                slice_obj["assign_id"] = assign_ids[0]
                slice_obj["assign_ids"] = list(assign_ids)
            if arm:
                slice_obj["arm"] = arm
            row = write_idea(
                store, round_id=round_id, author_launch=author_launch,
                slice_ref=json.dumps(slice_obj, sort_keys=True, ensure_ascii=False) if slice_obj else None,
                **kwargs,
            )
            written.append(row)
    except Exception:
        for row in written:
            store.knowledge.execute("DELETE FROM idea WHERE idea_id = ?", (row["idea_id"],))
        store.knowledge.commit()
        raise
    return written
