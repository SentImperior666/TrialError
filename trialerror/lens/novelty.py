"""The novelty screen: the step between a round's generation phase and its
convergence phase (framework design Sections 3 and 4; ``docs/AIIF_DESIGN.md``
Sections 3 and 7).

**What this module will not do.** It never turns a distance into the word
"novel". Embedding distance measures topical proximity, not mechanism
identity: a rephrased known mechanic can sit far and a genuinely new one
near. So the mechanical half records distances, merges exact-shaped
near-duplicates, flags what sits on top of a known inventory row, and routes
ideas to a judge -- and the judged half returns discrete labels a person or
a top-model launch assigned by comparing the record against retrieved rows.
An idea nothing retrieves is labelled ``no-close-neighbour``, which is not
"new-mechanism" and is never printed as one.

**Two halves, deliberately unequal.**

*3a, mechanical, incremental, no LLM* (:func:`run_mechanical_screen`). Runs
after each batch of records posts, because nothing it produces is ever shown
to a generator -- that is what makes running it early safe. It writes one
dossier per idea and updates the round's adjudication draft.

*3b, judged, at round end* (:func:`build_judged_batch`,
:func:`record_novelty_verdicts`). Selects the judged scope, builds envelopes
carrying raw record fields and retrieved text ONLY, injects plants, and
records the returned labels as ``verdict`` rows. This module never calls a
model: like :mod:`trialerror.verify.hypothesis`, it builds the envelope and
takes the answer back (see ``trialerror/verify/__init__.py``'s
LLM-judgment-boundary note).

**Five reference sets, snapshot-hashed on the round**
(:func:`reference_snapshot`):

===  =====================================================================
R1   the round's own other ideas
R2   the whole idea archive, including ``eliminated`` and ``merged`` rows
R3   the structured inventory (source kind ``inventory``, one chunk per row)
R4   the curriculum corpus, retrieved stratified 40/40/20 with a far floor
     of 2 -- :func:`trialerror.verify.hypothesis.stratified_retrieve`
     verbatim, not a second implementation of it
R5   external indexes, behind :class:`ExternalProvider`, under an
     ``external_query_mode`` that decides what text may leave the machine
===  =====================================================================

**Two conventions this module reads, both stated rather than assumed.**

1. *An inventory row's family.* R3 is ingested as one document per register
   and one chunk per row, so a row's family is its document, and the family
   LABEL is that document's ``rel_path`` stem. That is what lets ``d_home``
   mean "distance to the rows of the cell the author declared" and ``leap``
   mean "the nearest row somewhere else". A program whose inventory is laid
   out differently passes its own ``family_of``.
2. *An idea's home family.* ``idea.home`` names a cell; everything before
   the first ``/`` is its family (the whole string when there is no ``/``).

Neither convention is load-bearing for the merge, the flag or the judged
labels -- only for the two positioning statistics that need a notion of
"same family", which report ``None`` rather than guessing when they cannot
resolve one.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from trialerror.events.api import append_event
from trialerror.ingest.pipeline import INVENTORY_SOURCE_KIND
from trialerror.lens.errors import LensError
from trialerror.lens.ideas import ARCHIVED_STATUS, _provenance_obj, read_idea
from trialerror.lens.quota import derive_rng
from trialerror.lens.stratify import stratify
from trialerror.lens.vectors import fetch_doc_vectors, program_config
from trialerror.retrieve import engine as retrieve_engine
from trialerror.retrieve.fence import excerpt_words, is_fenced_license
from trialerror.retrieve.vecsearch import (
    cosine_similarity,
    fetch_vector_matrix,
    fetch_vectors,
    rank_by_query_vector,
)
from trialerror.retrieve.wrap import untrusted_wrap
from trialerror.util import vecmath
from trialerror.util.vecmath import cosine_many
from trialerror.stores import update as store_update
from trialerror.stores.store import Store
from trialerror.util.timeutil import now
from trialerror.verify.hypothesis import DEFAULT_FAR_FLOOR, DEFAULT_WEIGHTS, stratified_retrieve
from trialerror.verify.independence import DEFAULT_PROXIMITY_THRESHOLD, UnionFind
from trialerror.verify.errors import PreregVoidedError
from trialerror.verify.prereg import prereg_compliance_detail
from trialerror.verify.verdicts import record_verdict

__all__ = [
    "SCREEN_VERSION",
    "PROCEDURE",
    "PROCEDURE_VERSION",
    "NEAR_DUPLICATE_THRESHOLD",
    "KNOWN_MECHANIC_THRESHOLD",
    "INVENTORY_LABELS",
    "LITERATURE_LABELS",
    "UNJUDGED_LABEL",
    "UNJUDGED_QUALIFIER",
    "UNLABELLED_SCOPE_CAVEAT_FRACTION",
    "REFERENCE_SETS",
    "REFERENCE_SET_ROWS",
    "REFERENCE_SET_LABEL_BLOCK",
    "REFERENCE_SET_VOCABULARIES",
    "DEFAULT_JUDGED_SETS",
    "DEFAULT_ARCHIVE_ROWS_K",
    "IDEA_VECTOR_TABLE",
    "statement_digest",
    "fetch_idea_vectors",
    "store_idea_vectors",
    "normalize_judged_sets",
    "LABEL_FILE_EXTRA_KEYS",
    "DEFAULT_SEED_LABELS",
    "UNSCREENABLE_LABEL",
    "load_label_vocabularies",
    "label_block_for_judge",
    "canonical_label",
    "unscreenable_word",
    "accepted_labels_for_set",
    "round_labels_for_canonical",
    "MIN_ON_TOPIC_SEEDS",
    "SEED_FIELDS",
    "on_topic_seed_label",
    "normalize_seeds",
    "OPPORTUNITY_AXIS",
    "METHOD_AXIS",
    "TEMPLATE_OPPORTUNITY",
    "TEMPLATE_METHOD",
    "EXTERNAL_QUERY_MODES",
    "PLANTS_PER_BATCH",
    "JUDGED_SAMPLE_FRACTION",
    "SECOND_JUDGE_FRACTION",
    "BATCH_STATUSES",
    "NoveltyError",
    "QueryEmbedBackendUnrunnableError",
    "ExternalQuery",
    "ExternalProvider",
    "NullExternalProvider",
    "StaticExternalProvider",
    "ArxivIndexProvider",
    "LitApiSearchProvider",
    "neutral_abstract",
    "home_family",
    "reference_snapshot",
    "round_dir",
    "DEFAULT_CANDIDATE_HIT_SIMILARITY",
    "DEFAULT_CORPUS_K",
    "DEFAULT_CORPUS_MODE",
    "DEFAULT_EXTERNAL_K",
    "DEFAULT_INVENTORY_ROWS_K",
    "JUDGE_VISIBLE_FIELDS",
    "WITHHELD_FROM_JUDGE",
    "run_mechanical_screen",
    "strip_self_assessment",
    "build_verifier_envelope",
    "select_judged_scope",
    "MECHANIC_ROW_ID_PATTERN",
    "parse_mechanic_rows",
    "mechanic_row_candidates",
    "PARAPHRASE_BACKENDS",
    "DEFAULT_PARAPHRASE_BACKEND",
    "PLANT_KINDS",
    "KIND_REFUSAL_CAP",
    "PLANT_CLASS_MAX_CHARS",
    "DEFAULT_BATCH_FAIL_ON",
    "EXTERNAL_PLANT_FIELDS",
    "render_extra_text",
    "normalize_fail_kinds",
    "load_external_plants",
    "build_external_plants",
    "plants_for_batch",
    "expected_labels_for_set",
    "plant_reference_bundle",
    "PLANT_EXPECTED_CANONICAL",
    "build_plants",
    "JUDGE_MASK_PREFIX",
    "build_judge_views",
    "unmask_labels",
    "build_judged_batch",
    "score_plants",
    "cohens_kappa",
    "confusion_table",
    "catch_by_expected",
    "pearson_r",
    "baseline_cosine_distribution",
    "baseline_distribution",
    "CorpusVectorTable",
    "PERCENTILE_METHOD",
    "CALIBRATION_PROCEDURE_VERSION",
    "CALIBRATION_JUDGES",
    "build_calibration_batch",
    "fill_idea_vector_cache",
    "record_calibration",
    "record_novelty_verdicts",
    "CONVERGENT_RECHECK_VERSION",
    "CONVERGENT_LINK_FIELDS",
    "known_neighbour_keys",
    "read_dossier",
    "record_convergent_links",
    "recheck_idea_convergence",
]


class NoveltyError(LensError):
    """A structural refusal from the screen: an unknown round, an
    unsupported query mode, a label outside its vocabulary, a judged batch
    whose returned labels do not line up with the batch that was built.

    ``code`` is the envelope error code a surface should report this refusal
    under, for the cases where a generic ``screen_refused`` would hide a
    condition an agent can act on. ``None`` is the default and keeps every
    existing refusal reporting exactly what it reported before (the same
    shape :class:`trialerror.retrieve.errors.RetrievalError` uses)."""

    code: str | None = None


class QueryEmbedBackendUnrunnableError(NoveltyError):
    """The screen refused because this process cannot embed the statements it
    is about to embed (lane F-1 item D).

    Its own class, and its own ``code``, because the CLI has to be able to
    tell this refusal from every other structural one -- and telling them
    apart by looking for the doctor check's NAME inside the message (which is
    what this class replaced) made the envelope's code depend on the prose of
    :func:`trialerror.retrieve.engine.query_embed_refusal_message`: any reword,
    or any future refusal that happens to quote the check name for another
    reason, silently changed what an agent parsing the envelope saw. Same
    shape as :class:`trialerror.verify.errors.QueryEmbedBackendUnrunnableError`
    on the verify side, for the same reason."""

    code = retrieve_engine.QUERY_EMBED_UNRUNNABLE_CODE


#: Bumped when the mechanical half's OUTPUT changes shape or meaning. It is
#: stamped on every dossier so a dossier read months later says which
#: screen wrote it.
SCREEN_VERSION = "novelty-v2"

#: ``verdict.procedure`` / ``procedure_version`` for every row this module
#: writes. ``custom`` is already in ``verdict.procedure``'s CHECK domain
#: (:data:`trialerror.verify.verdicts.PROCEDURES`) -- the screen is a
#: procedure the verdict table was built to accept, not a new kind of row.
PROCEDURE = "custom"
PROCEDURE_VERSION = "novelty-v2"

#: Cosine similarity at or above which two ideas with the SAME home cell are
#: one idea. Deliberately the same constant the independence clustering uses
#: (:data:`trialerror.verify.independence.DEFAULT_PROXIMITY_THRESHOLD`):
#: "near-duplicate, not merely topically similar" is the same judgement in
#: both places, and two numbers that mean the same thing drift apart.
NEAR_DUPLICATE_THRESHOLD = DEFAULT_PROXIMITY_THRESHOLD

#: Cosine similarity at or above which an idea is FLAGGED as sitting on an
#: existing inventory row. A flag is a routing decision -- it makes the
#: judged label mandatory -- and never a verdict on its own.
KNOWN_MECHANIC_THRESHOLD = DEFAULT_PROXIMITY_THRESHOLD

#: The judged label set against R3 (the inventory), strongest relation
#: first. ``unscreenable`` is the honest answer for a record with no
#: concrete mechanic or transition sketch: vague is not new.
INVENTORY_LABELS: tuple[str, ...] = ("same", "variant", "recombination", "new-mechanism", "unscreenable")

#: The judged label set against R4/R5 (corpus and external prior art).
LITERATURE_LABELS: tuple[str, ...] = ("stated", "implied", "adjacent", "absent")

#: The reference sets a judged batch may DECLARE, each paired with the key a
#: judge returns its label under. One pair, one verdict row, one ``label``
#: prefixed with the reference set it is a claim about.
#:
#: Declared per round rather than hardwired (lane FB-5 item 1). The screen
#: began as one instrument -- the judge saw the nearest R3 inventory rows and
#: the R4/R5 corpus passages, and nothing else was expressible without
#: editing this module. A round that judges a LITERATURE rather than a
#: mechanic needs the nearest R2 archive rows (prior rounds' candidates and
#: request rows) in their place, and a round that judges both wants both. So
#: the sets are a parameter, and the DEFAULT is exactly what was hardwired
#: before: :data:`DEFAULT_JUDGED_SETS`.
#:
#: R5 is absent on purpose and always has been: it is EVIDENCE for the
#: literature label, not a labelled set of its own (see the dossier's own
#: note), and its hits ride inside the R4 bundle.
REFERENCE_SETS: dict[str, str] = {"R2": "label_archive", "R3": "label_inventory", "R4": "label_corpus"}

#: Which envelope key each declared set's rows ride in. An envelope carries
#: exactly the DECLARED sets' keys and no other -- an undeclared set is
#: ABSENT, not empty, because an empty list reads to a judge (and to every
#: later reader of the batch file) as "this set was consulted and held
#: nothing", which is a different sentence from "this set was not consulted".
REFERENCE_SET_ROWS: dict[str, str] = {"R2": "archive_rows", "R3": "inventory_rows", "R4": "retrieved"}

#: What the envelope's ``labels`` block calls each set's vocabulary. Named
#: for the RELATION the judge is being asked about rather than for the set
#: id, which is the spelling the block has always used ("inventory",
#: "literature") and the spelling a judge prompt reads.
REFERENCE_SET_LABEL_BLOCK: dict[str, str] = {"R2": "archive", "R3": "inventory", "R4": "literature"}

#: The design's own fixed vocabulary per set -- what a round's own
#: vocabulary maps ONTO (lane FB-5 item 2) so every downstream count keeps
#: working whatever the round calls its labels. R2 shares R3's: "does a row
#: already state this" is the same question about a different reference set.
REFERENCE_SET_VOCABULARIES: dict[str, tuple[str, ...]] = {
    "R2": INVENTORY_LABELS,
    "R3": INVENTORY_LABELS,
    "R4": LITERATURE_LABELS,
}

#: What a round declares when it declares nothing: the instrument as it was
#: before it was one -- the inventory and the corpus.
DEFAULT_JUDGED_SETS: tuple[str, ...] = ("R3", "R4")

#: How many R2 rows ride in a judged envelope, nearest first. The same order
#: as :data:`DEFAULT_INVENTORY_ROWS_K` and for the same reason: more than a
#: handful stops being a pairwise comparison and starts being a reading task.
DEFAULT_ARCHIVE_ROWS_K = 5

#: What an idea outside the judged scope carries. It is a statement about
#: retrieval, not about novelty, and every report that prints it must print
#: ``unjudged`` beside it.
UNJUDGED_LABEL = "no-close-neighbour"

#: The distribution audit's two taxonomy axes. Provisional and single-source
#: by the design's own grading -- they exist here as a data table precisely
#: so re-deriving them is an edit to one tuple rather than a rewrite.
OPPORTUNITY_AXIS: tuple[str, ...] = (
    "contradiction-between-systems",
    "unexplained-mechanic",
    "scope-mismatch",
    "missing-representation",
    "bridge",
    "failure-risk",
    "cost-bottleneck",
)
METHOD_AXIS: tuple[str, ...] = (
    "synthesis-unify",
    "extend-scope",
    "robustify",
    "formalize",
    "empirical-map",
    "artifact",
    "search-optimize",
)

#: The pair whose over-representation is the collapse pattern the single
#: source reports. "Template mass" is the share of a batch that is both.
TEMPLATE_OPPORTUNITY = "bridge"
TEMPLATE_METHOD = "synthesis-unify"

#: What may be sent to an external index, per the round's pre-registration.
#: ``none`` issues no query at all; ``neutral_abstract`` sends a template
#: built mechanically from structured fields (:func:`neutral_abstract`);
#: ``statement`` sends the record's own statement. The mode is recorded on
#: every logged query, so what left the machine is answerable after the
#: fact rather than a matter of recollection.
EXTERNAL_QUERY_MODES: tuple[str, ...] = ("none", "neutral_abstract", "statement")

#: Plants per judged batch, per kind (:func:`build_plants`).
PLANTS_PER_BATCH = 5

#: The seeded random share of otherwise-unjudged ideas that is judged
#: anyway, so the label distribution is not conditioned entirely on what
#: retrieval happened to surface.
JUDGED_SAMPLE_FRACTION = 0.20

#: The share of judged ideas re-judged by a second launch, for kappa.
SECOND_JUDGE_FRACTION = 0.10

#: A judged batch's outcome. ``reopened_with_caveat`` is what a missed
#: inventory plant produces: the batch's labels are recorded and marked, the
#: ideas are NOT consolidated, and nothing about it is silent.
BATCH_STATUSES: tuple[str, ...] = ("screened", "reopened_with_caveat")

#: Similarity at or above which a retrieved neighbour counts as a candidate
#: HIT -- which is what routes an idea into the judged scope. A threshold,
#: not a verdict: it decides who gets looked at, never what the answer is.
DEFAULT_CANDIDATE_HIT_SIMILARITY = 0.5

#: Neighbours fetched per idea from the corpus (R4).
DEFAULT_CORPUS_K = 6

#: Retrieval mode for R4. ``stratified_retrieve``'s own default is
#: ``hybrid``, which is right for a hypothesis: a short claim in the
#: corpus's own vocabulary, where an exact-term prefilter earns its place.
#: An idea statement is neither. The lexical tier ANDs every quoted token
#: (``trialerror.retrieve.ftssearch.fts_query_string``), so a 30-word
#: statement retrieves nothing at all, and in hybrid mode the vector tier
#: only reranks what the lexical tier found -- meaning the whole prior-art
#: bundle would come back empty for exactly the records it matters most for.
#: ``vector`` ranks the corpus by embedding, which is what "how close is
#: this idea to prior work" actually asks. Named as a parameter, not
#: hardcoded, so a program with a keyword-shaped corpus can say otherwise.
DEFAULT_CORPUS_MODE = "vector"

#: Results requested per idea from an external provider (R5).
DEFAULT_EXTERNAL_K = 20

#: How many R3 rows ride in a judged envelope, nearest first.
#:
#: The pairwise comparison against retrieved rows is the load-bearing
#: defence (design 5.2(1)), so an envelope that carried no inventory rows
#: would ask the judge to answer "does a register row already state this"
#: from memory. More than a handful stops being a comparison and starts
#: being a reading task; five is the same order as the corpus bundle
#: (:data:`DEFAULT_CORPUS_K`) the same envelope carries.
DEFAULT_INVENTORY_ROWS_K = 5


# ---------------------------------------------------------------------------
# R5: the external-query seam
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExternalQuery:
    """One external lookup, as issued. Carries the MODE alongside the text
    so a logged query answers "what left the machine, and under which
    pre-registered setting" without anyone having to reconstruct it."""

    text: str
    mode: str
    k: int
    idea_id: str | None = None
    round_id: str | None = None


class ExternalProvider(Protocol):
    """R5 behind one interface, for the same reason
    :class:`trialerror.arxiv_index.encoder.QueryEncoder` exists: the real
    adapters reach the network (or a several-million-row local index that
    still embeds its query off-machine), and a test must be able to exercise
    every path around them without either.

    ``snapshot_id`` is what gets hashed into the round's reference snapshot:
    an external index's answer depends on which build of it answered."""

    name: str

    def snapshot_id(self) -> str: ...

    def search(self, query: ExternalQuery) -> list[dict[str, Any]]: ...


class NullExternalProvider:
    """``external_query_mode="none"``: issues nothing, returns nothing, and
    says so in the snapshot. The round-0 default when the operator has not
    ruled on egress."""

    name = "none"

    def snapshot_id(self) -> str:
        return "none"

    def search(self, query: ExternalQuery) -> list[dict[str, Any]]:
        return []


@dataclass
class StaticExternalProvider:
    """A provider whose answers are supplied up front, keyed by query text
    (with ``default`` for anything else). This is what the tests use, and it
    is a real class rather than a mock so the calling code under test is the
    same code that runs against a real adapter."""

    results: Mapping[str, Sequence[Mapping[str, Any]]] = field(default_factory=dict)
    default: Sequence[Mapping[str, Any]] = ()
    name: str = "static"
    snapshot: str = "static-1"
    calls: list[ExternalQuery] = field(default_factory=list)

    def snapshot_id(self) -> str:
        return self.snapshot

    def search(self, query: ExternalQuery) -> list[dict[str, Any]]:
        self.calls.append(query)
        return [dict(r) for r in self.results.get(query.text, self.default)]


@dataclass
class ArxivIndexProvider:
    """The real local-index adapter: encode the query, then
    :func:`trialerror.arxiv_index.query.semantic_search` over an already-open
    connection. It calls the landed modules rather than reimplementing
    either half, and it takes both as constructor arguments so this module
    never opens a connection or holds a key.

    Note what "local" does and does not mean here: the VECTORS are on this
    machine, but the encoder embeds the query text wherever the encoder
    lives. That is exactly why the mode travels with the query and every
    call is logged."""

    conn: Any
    encoder: Any
    name: str = "arxiv-index"

    def snapshot_id(self) -> str:
        from trialerror.arxiv_index.store import get_build_state

        state = get_build_state(self.conn)
        return str(state.get("built_ts") or state.get("backend") or "unknown")

    def search(self, query: ExternalQuery) -> list[dict[str, Any]]:
        from trialerror.arxiv_index.query import semantic_search

        vector = self.encoder.encode_query(query.text)
        return [r.to_dict() for r in semantic_search(self.conn, vector, k=query.k)]


@dataclass
class LitApiSearchProvider:
    """The real title-search adapter, wrapping
    :meth:`trialerror.litapi.client.LitApiClient.search`. Same egress class
    as the index adapter above and the same logging obligation; which
    providers sit behind the client is the client's own composition."""

    client: Any
    name: str = "litapi"

    def snapshot_id(self) -> str:
        return "+".join(sorted(p.name for p in getattr(self.client, "providers", [])))

    def search(self, query: ExternalQuery) -> list[dict[str, Any]]:
        result = self.client.search(query.text, limit=query.k)
        return [r.to_dict() if hasattr(r, "to_dict") else dict(r) for r in result.records]


def neutral_abstract(idea: Mapping[str, Any]) -> str:
    """The ``neutral_abstract`` query text: a template over the record's
    STRUCTURED fields -- home cell, declared operation, home family -- built
    with no model in the loop.

    The point is not brevity, it is that nothing an author wrote steers the
    query. A statement can be tuned until it retrieves nothing and then be
    called new; a template over declared fields cannot be, because the
    fields are a closed vocabulary the distribution audit already counts.
    The statement and the probe do not leave the machine under this mode."""
    home = str(idea.get("home") or "").strip()
    operation = str(idea.get("operation_declared") or "").strip()
    parts = [p for p in (home_family(home), home, operation) if p]
    # dedupe while keeping order -- a home with no "/" makes family == home
    seen: list[str] = []
    for part in parts:
        if part not in seen:
            seen.append(part)
    return " ".join(seen) if seen else "unspecified mechanism"


# ---------------------------------------------------------------------------
# conventions: families and statements
# ---------------------------------------------------------------------------


def home_family(home: str | None) -> str:
    """The family half of a home-cell label: everything before the first
    ``/``, or the whole string when there is no ``/``. ``""`` for an absent
    home -- an idea that declares no cell has no family, and the two
    statistics that need one report ``None`` rather than inventing it."""
    if not home:
        return ""
    return str(home).split("/", 1)[0].strip()


def _statement(idea: Mapping[str, Any]) -> str:
    """The text the screen embeds and judges: the record body, which is the
    full text the author wrote and the feed posted verbatim."""
    return str(idea.get("body") or "")


def _provenance_obj(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if not raw or not isinstance(raw, str):
        return {}
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return dict(obj) if isinstance(obj, dict) else {}


def _provenance_docs(idea: Mapping[str, Any]) -> list[str]:
    docs = _provenance_obj(idea.get("provenance")).get("docs")
    if isinstance(docs, (list, tuple)):
        return [str(d) for d in docs]
    return []


# ---------------------------------------------------------------------------
# reference sets
# ---------------------------------------------------------------------------


def _digest(pairs: Iterable[tuple[str, str]]) -> str:
    h = hashlib.sha256()
    for left, right in sorted(pairs):
        h.update(left.encode("utf-8"))
        h.update(b"\x00")
        h.update(right.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def _load_ideas(store: Store, *, round_id: str | None = None) -> list[dict[str, Any]]:
    """Every ``idea`` row (optionally one round's), each resolved through
    :func:`trialerror.lens.ideas.read_idea` so a record written under the
    interim ``provenance``-JSON convention reads identically to one written
    since."""
    if round_id is None:
        rows = store.knowledge.execute("SELECT idea_id FROM idea ORDER BY created_ts, idea_id").fetchall()
    else:
        rows = store.knowledge.execute(
            "SELECT idea_id FROM idea WHERE round_id = ? ORDER BY created_ts, idea_id", (round_id,)
        ).fetchall()
    out = []
    for row in rows:
        idea = read_idea(store, idea_id=row["idea_id"])
        if idea is not None:
            out.append(idea)
    return out


def _decode_extra(raw: Any) -> Any:
    """``idea.extra`` as the object the author wrote (knowledge-v11).

    Stored as JSON text by :func:`trialerror.lens.ideas.write_idea`; a value
    that will not decode is returned verbatim so
    :func:`render_extra_text` can refuse it by name rather than this
    function swallowing it into ``None``. An absent column (a row written
    before the migration) is ``None``, which renders to nothing."""
    if raw is None or isinstance(raw, Mapping):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw
    return raw


def _fenced_text(text: str | None, license_tier: str | None) -> str:
    """A reference row's text as a judge may see it: the whole row when its
    source is open, a <=20-word excerpt when the licence fences it, wrapped
    as untrusted either way -- the same treatment
    :mod:`trialerror.retrieve.engine` gives every chunk it serves. The
    screen reads R3 straight out of SQL rather than through ``search``, so
    the fence has to be applied here or this one path would serve fenced
    text in full."""
    body = str(text or "")
    if is_fenced_license(license_tier):
        body = excerpt_words(body)
    return untrusted_wrap(body)


def _inventory_rows(store: Store, *, model_key: str, family_of: Callable[[Mapping[str, Any]], str] | None = None) -> list[dict[str, Any]]:
    """R3: every chunk of every :data:`INVENTORY_SOURCE_KIND` source, with
    its family label, its licence tier and its vector.

    The kind is read from the ingest pipeline's own constant rather than
    from ``DEFAULT_EXCLUDED_KINDS[0]``. They hold the same string today, but
    they answer different questions -- "what IS the inventory" and "what is
    kept off the lens surfaces" -- and a second excluded kind would silently
    repoint R3 at it.

    Rows with no vector under ``model_key`` are returned without one and are
    simply not compared against -- the same "missing is absent" contract
    ``fetch_vectors`` states. That is a narrowing the CALLER must notice,
    which is why every caller counts what it got back
    (:func:`_inventory_coverage`): an inventory that is present but
    unembedded silently disables the KNOWN-MECHANIC flag and the plant
    battery at once."""
    rows = [
        dict(r)
        for r in store.knowledge.execute(
            """
            SELECT c.chunk_id AS row_id, c.doc_id AS doc_id, c.seq AS seq, c.text AS text,
                   d.rel_path AS rel_path, s.license_tier AS license_tier
            FROM chunk c
            JOIN document d ON d.doc_id = c.doc_id
            JOIN source s ON s.source_id = d.source_id
            WHERE s.kind = ?
            ORDER BY d.rel_path, c.seq, c.chunk_id
            """,
            (INVENTORY_SOURCE_KIND,),
        ).fetchall()
    ]
    vectors = fetch_vectors(store, model_key, [r["row_id"] for r in rows]) if rows else {}
    for row in rows:
        row["family"] = family_of(row) if family_of else Path(str(row["rel_path"] or "")).stem
        row["vector"] = vectors.get(row["row_id"])
    return rows


def normalize_judged_sets(judged_sets: Any) -> tuple[str, ...]:
    """The declared reference sets, validated and put in
    :data:`REFERENCE_SETS` order.

    Canonical order rather than declaration order, so two rounds that
    declared ``R4,R2`` and ``R2,R4`` write envelopes whose keys land in the
    same places and whose verdict rows come back in the same sequence -- the
    order a round happened to type its flag in is not a property of the
    instrument.

    A set nobody has ever heard of is a refusal by name: it would otherwise
    produce an envelope silently missing the rows the round thought it had
    declared, which is exactly the failure the whole declaration exists to
    prevent. ``None`` means the default."""
    if judged_sets is None:
        return DEFAULT_JUDGED_SETS
    if isinstance(judged_sets, str):
        judged_sets = [part.strip() for part in judged_sets.split(",") if part.strip()]
    names = [str(name).strip().upper() for name in judged_sets]
    unknown = [name for name in names if name not in REFERENCE_SETS]
    if unknown:
        raise NoveltyError(
            f"judged_sets names reference set(s) {unknown!r} this screen does not know. The declarable "
            f"sets are {sorted(REFERENCE_SETS)!r}: R2 the archive of idea rows, R3 the inventory, R4 the "
            "corpus (R5 is evidence for the R4 label, not a labelled set of its own, and rides inside "
            "that bundle)"
        )
    if not names:
        raise NoveltyError(
            "judged_sets is empty: a judged batch with no declared reference set asks a judge to compare "
            "a record against nothing. Declare at least one of " + repr(sorted(REFERENCE_SETS))
        )
    return tuple(name for name in REFERENCE_SETS if name in set(names))


#: The keys a round's label file may carry beside one block per reference
#: set: ``extra`` (vocabularies that are not a reference set's -- today the
#: seed-work labels of item 5) and ``unscreenable`` (the round's own spelling
#: of the label a record with no statable mechanism gets).
LABEL_FILE_EXTRA_KEYS: tuple[str, ...] = ("extra", "unscreenable")

#: The seed-work vocabulary, when a round re-spells nothing. Seed labels are
#: NOT a reference set: they record whether the work a judge resolved on a
#: subject was on topic at all, and they ride in the verdict row's evidence
#: (lane FB-5 item 5).
DEFAULT_SEED_LABELS: tuple[str, ...] = ("on-topic", "off-topic")

#: The design's own word for a record with no statable mechanism, which is
#: what a round's ``unscreenable`` key re-spells.
UNSCREENABLE_LABEL = "unscreenable"

#: The number of ON-TOPIC resolved seeds below which a round may justify an
#: ``unscreenable``. The RULE is the round's own text -- this module records
#: the seeds and the count and reports which subjects sit below the bar; it
#: never refuses a label on it, because "how much work was resolved before a
#: record was called unscreenable" is a judgement the round pre-registers
#: and a reader checks, not an arithmetic the screen enforces.
MIN_ON_TOPIC_SEEDS = 2

#: What a seed declaration may carry.
SEED_FIELDS: tuple[str, ...] = ("ref", "label")


def on_topic_seed_label(vocabularies: Mapping[str, Any] | None) -> str:
    """Which seed label means "this work was on topic".

    The FIRST label in the seed vocabulary, by the same convention
    :data:`INVENTORY_LABELS` states for itself ("strongest relation first"):
    a re-spelled vocabulary has to say which end is which somehow, and an
    ordered list is the one way that needs no second key and cannot
    disagree with itself."""
    seeds = list((vocabularies or {}).get("seeds") or DEFAULT_SEED_LABELS)
    return seeds[0]


def normalize_seeds(
    seeds: Any, *, subject_id: str, vocabularies: Mapping[str, Any] | None = None
) -> list[dict[str, str]]:
    """One subject's seed work, validated.

    Seed labels are NOT a reference set: they say whether the work a judge
    resolved on this subject was on topic at all, which is a fact about the
    judging rather than a claim about a reference set. So they ride in the
    verdict row's EVIDENCE and never produce a row of their own -- there is
    no ``label_seed`` and no R-number for them."""
    if seeds is None:
        return []
    vocabulary = list((vocabularies or {}).get("seeds") or DEFAULT_SEED_LABELS)
    if not isinstance(seeds, (list, tuple)):
        raise NoveltyError(
            f"seeds for {subject_id!r} must be a list of {{ref, label}} objects, got {type(seeds).__name__}"
        )
    out: list[dict[str, str]] = []
    for index, seed in enumerate(seeds):
        where = f"seeds[{index}] for {subject_id!r}"
        if not isinstance(seed, Mapping):
            raise NoveltyError(f"{where} is a {type(seed).__name__}, not an object")
        unknown = sorted(set(seed) - set(SEED_FIELDS))
        if unknown:
            raise NoveltyError(f"{where} carries unknown field(s) {unknown!r}; a seed declares {list(SEED_FIELDS)!r}")
        ref = str(seed.get("ref") or "").strip()
        if not ref:
            raise NoveltyError(f"{where} has no ref -- a seed nobody can resolve is not resolved work")
        label = str(seed.get("label") or "").strip()
        if label not in vocabulary:
            raise NoveltyError(
                f"{where} is labelled {label!r}, which is not in this round's seed vocabulary "
                f"({vocabulary!r})"
            )
        out.append({"ref": ref, "label": label})
    return out


def load_label_vocabularies(
    declared: Any, *, judged_sets: Sequence[str] = DEFAULT_JUDGED_SETS
) -> dict[str, Any]:
    """A round's own label vocabularies, validated, with the CANONICAL
    mapping onto the design's fixed vocabulary and a hash over the whole
    thing.

    The shape a round declares (every key optional; a declared set the file
    says nothing about keeps the design's vocabulary under an identity
    mapping)::

        {"R2": {"labels": ["requested", "variant", "new"],
                "canonical": {"requested": "same", "variant": "variant",
                              "new": "new-mechanism"}},
         "R4": {"labels": [...], "canonical": {...}},
         "extra": {"seed": ["on-topic", "off-topic"]},
         "unscreenable": "unscreenable"}

    **The mapping must be TOTAL over the round's labels.** A partial one is
    refused by name rather than filled in, and the reason is the only thing
    the mapping is for: every count downstream of the screen -- the
    adjudication draft, the gate suite's bundle check, a report that says
    "three records were labelled ``same``" -- reads the design's vocabulary.
    A label with no canonical value would vanish from all of them while
    still sitting in a verdict row, which is worse than either recording it
    or refusing it.

    A canonical value outside the design's own vocabulary for that set is
    refused for the same reason in reverse: it would be a fourth literature
    label that no reader has a column for.

    Returns ``{"sets": {...}, "seeds": [...], "unscreenable": str,
    "sha256": str, "declared_sets": [...]}``. ``sha256`` is over the
    resolved structure and is stamped on the batch, so a round cannot judge
    under one vocabulary and record under another without it being visible.
    """
    wanted = normalize_judged_sets(judged_sets)
    if declared is None:
        declared = {}
    if not isinstance(declared, Mapping):
        raise NoveltyError(
            "label vocabularies: the labels file must hold a JSON OBJECT keyed by reference set "
            f"({sorted(REFERENCE_SETS)!r}), got {type(declared).__name__}"
        )
    allowed = set(REFERENCE_SETS) | set(LABEL_FILE_EXTRA_KEYS)
    unknown = sorted(set(declared) - allowed)
    if unknown:
        raise NoveltyError(
            f"label vocabularies: unknown key(s) {unknown!r}. A labels file carries one block per "
            f"reference set ({sorted(REFERENCE_SETS)!r}) plus {list(LABEL_FILE_EXTRA_KEYS)!r} -- a key "
            "nothing reads is a vocabulary the round wrote for nobody"
        )
    # A block for a set the round did NOT declare is refused BY NAME rather
    # than dropped, the way a plants file refuses an expectation for one. A
    # dropped block is the worst of the three outcomes: the set-name typo
    # that writes R3 for R2 would leave the judge on the DESIGN's words for
    # the set it is actually shown, and the round would only discover it when
    # --record-verdicts refused the answers it got back.
    undeclared = sorted(set(declared) & set(REFERENCE_SETS) - set(wanted))
    if undeclared:
        raise NoveltyError(
            f"label vocabularies: carries a block for reference set(s) {undeclared!r}, which this round "
            f"did not declare (it declared {list(wanted)!r}). The judge is never shown those sets, so the "
            "vocabulary would be one the round wrote for nobody -- while the sets it IS shown would "
            "silently keep the design's own words. Declare the set with --judged-sets, or drop the block"
        )

    sets: dict[str, dict[str, Any]] = {}
    for name in wanted:
        block = declared.get(name)
        if block is None:
            # Declared as a judged set, not re-spelled: the design's own
            # vocabulary under the identity mapping. Recorded explicitly
            # rather than left absent, so the batch says which vocabulary the
            # judge was shown for every set it was shown.
            vocabulary = list(REFERENCE_SET_VOCABULARIES[name])
            sets[name] = {"labels": vocabulary, "canonical": {label: label for label in vocabulary}}
            continue
        if not isinstance(block, Mapping):
            raise NoveltyError(
                f"label vocabularies: {name} must be an object carrying 'labels' and 'canonical', got "
                f"{type(block).__name__}"
            )
        labels = block.get("labels")
        if not isinstance(labels, (list, tuple)) or not labels:
            raise NoveltyError(
                f"label vocabularies: {name}.labels must be a non-empty list of the round's own label "
                "names -- a judge shown no vocabulary invents one"
            )
        names = [str(label).strip() for label in labels]
        if any(not label for label in names):
            raise NoveltyError(f"label vocabularies: {name}.labels carries an empty label name")
        if len(set(names)) != len(names):
            raise NoveltyError(
                f"label vocabularies: {name}.labels repeats a label name ({names!r}); two labels with one "
                "spelling cannot be told apart in a count"
            )
        canonical = block.get("canonical")
        if not isinstance(canonical, Mapping):
            raise NoveltyError(
                f"label vocabularies: {name}.canonical must be an object mapping every one of this round's "
                f"labels onto the design's own vocabulary {list(REFERENCE_SET_VOCABULARIES[name])!r}"
            )
        mapping = {str(k).strip(): str(v).strip() for k, v in canonical.items()}
        missing = [label for label in names if label not in mapping]
        if missing:
            raise NoveltyError(
                f"label vocabularies: {name}.canonical does not map {missing!r}. The mapping must be TOTAL "
                "over the round's labels: every count downstream of the screen reads the design's "
                f"vocabulary {list(REFERENCE_SET_VOCABULARIES[name])!r}, so an unmapped label would sit in "
                "a verdict row and be absent from every report of it"
            )
        stray = sorted(set(mapping) - set(names))
        if stray:
            raise NoveltyError(
                f"label vocabularies: {name}.canonical maps {stray!r}, which {name}.labels does not offer "
                "the judge. A mapping for a label nobody can return is a rule about nothing"
            )
        design = set(REFERENCE_SET_VOCABULARIES[name])
        outside = sorted({value for value in mapping.values() if value not in design})
        if outside:
            raise NoveltyError(
                f"label vocabularies: {name}.canonical maps onto {outside!r}, which is outside the design's "
                f"own vocabulary for {name} ({list(REFERENCE_SET_VOCABULARIES[name])!r}). The canonical "
                "half exists so every downstream count keeps working; a value no reader has a column for "
                "would defeat exactly that"
            )
        sets[name] = {"labels": names, "canonical": mapping}

    extra = declared.get("extra") or {}
    if not isinstance(extra, Mapping):
        raise NoveltyError(
            f"label vocabularies: 'extra' must be an object, got {type(extra).__name__}"
        )
    extra_unknown = sorted(set(extra) - {"seed"})
    if extra_unknown:
        raise NoveltyError(
            f"label vocabularies: extra carries unknown vocabulary/ies {extra_unknown!r}; the only one "
            "this screen reads is 'seed'"
        )
    seeds = extra.get("seed")
    if seeds is None:
        seed_labels = list(DEFAULT_SEED_LABELS)
    elif isinstance(seeds, (list, tuple)) and seeds and all(str(s).strip() for s in seeds):
        seed_labels = [str(s).strip() for s in seeds]
    else:
        raise NoveltyError(
            "label vocabularies: extra.seed must be a non-empty list of the round's own seed labels "
            f"(default {list(DEFAULT_SEED_LABELS)!r})"
        )

    unscreenable = declared.get("unscreenable")
    if unscreenable is None:
        unscreenable = UNSCREENABLE_LABEL
    elif not str(unscreenable).strip():
        raise NoveltyError("label vocabularies: 'unscreenable' must name the round's own spelling of it")
    elif str(unscreenable).strip() != UNSCREENABLE_LABEL and not any(
        str(unscreenable).strip() in block["labels"] for block in sets.values()
    ):
        # A round that gives 'unscreenable' its OWN spelling has to offer the
        # judge that spelling: the word is only ever read back off an answer.
        # Unoffered, the judge's answer is refused at --record-verdicts by
        # name and `unscreenable_below_bar` -- the whole point of the key --
        # can never fire for that round. Refused here, where the file is, and
        # only for a re-spelling: the design's own word stays acceptable for a
        # round whose sets simply never offer it (R4's vocabulary never has).
        raise NoveltyError(
            f"label vocabularies: 'unscreenable' is re-spelled {str(unscreenable).strip()!r}, which none "
            "of this round's declared sets offers the judge ("
            + ", ".join(f"{name}: {block['labels']!r}" for name, block in sets.items())
            + "). A judge answering it would be refused at --record-verdicts, and the seed-count report "
            "would never fire. List it in the labels of the set it belongs to (mapped onto "
            f"{UNSCREENABLE_LABEL!r}), or drop the re-spelling"
        )
    resolved: dict[str, Any] = {
        "declared_sets": list(wanted),
        "sets": sets,
        "seeds": seed_labels,
        "unscreenable": str(unscreenable).strip(),
    }
    resolved["sha256"] = hashlib.sha256(
        json.dumps(resolved, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return resolved


def label_block_for_judge(vocabularies: Mapping[str, Any] | None) -> dict[str, list[str]] | None:
    """``{reference_set: [label, ...]}`` for :func:`_envelope`'s ``labels``
    block, or ``None`` when the round declared no vocabulary of its own (in
    which case the envelope shows the design's, exactly as it always did).

    The judge is shown the ROUND's spellings and no others: a judge handed
    both would be free to answer in either, and a verdict row is not the
    place to discover which."""
    if not vocabularies:
        return None
    return {name: list(block["labels"]) for name, block in (vocabularies.get("sets") or {}).items()}


def canonical_label(vocabularies: Mapping[str, Any] | None, *, reference_set: str, label: str) -> str:
    """The design's own label for one the round returned. Identity when the
    round declared no vocabulary, or when its own spelling IS the design's.

    The round's ``unscreenable`` word maps onto :data:`UNSCREENABLE_LABEL`
    for EVERY set, including a set whose own ``canonical`` block never
    mentions it (lane FB-6 item 4). The word is not a claim about a
    reference set -- it says the record states no mechanism to compare with
    anything -- so it is one answer with one canonical spelling wherever it
    is given, and a count of it must not depend on which set it was given
    against."""
    if not vocabularies:
        return label
    block = (vocabularies.get("sets") or {}).get(reference_set) or {}
    mapping = block.get("canonical") or {}
    if label in mapping:
        return str(mapping[label])
    if label == unscreenable_word(vocabularies):
        return UNSCREENABLE_LABEL
    return label


def unscreenable_word(vocabularies: Mapping[str, Any] | None) -> str:
    """The spelling this round gives :data:`UNSCREENABLE_LABEL`."""
    return str((vocabularies or {}).get("unscreenable") or UNSCREENABLE_LABEL)


def accepted_labels_for_set(
    vocabularies: Mapping[str, Any] | None, *, reference_set: str
) -> list[str]:
    """Every word a judge may return for one declared set: that set's
    vocabulary, plus the round's ``unscreenable`` word when the set's own
    list does not already carry it (lane FB-6 item 4).

    Observed on a live calibration: two judges answered ``unscreenable``
    against R2 -- the design's own word, for a record that states no
    mechanism at all -- and the recorder refused it, because the round had
    re-spelled R2's labels and its list did not repeat it. The answers had
    to be passed as UNLABELLED, which says something different and false: a
    judge that says "there is nothing here to compare" has judged, and a
    judge that says nothing has not.

    The word is a statement about the RECORD, not about the reference set,
    so it is an answer for every set a round declares. It is scored as a
    non-catch for a plant (a plant is a row that exists, so ``unscreenable``
    is wrong about it), it is its own category in the kappa, and it is
    written to the verdict row with ``label_canonical = unscreenable``
    whatever the round spells it.

    This is NOT the same list as :func:`label_block_for_judge`, which is
    what the judge is SHOWN: adding a word to every set's displayed
    vocabulary would change the envelope every round has been judging
    against. What changes here is only what is accepted back."""
    vocabulary = list(
        ((vocabularies or {}).get("sets") or {}).get(reference_set, {}).get("labels")
        or REFERENCE_SET_VOCABULARIES[reference_set]
    )
    word = unscreenable_word(vocabularies)
    return vocabulary if word in vocabulary else [*vocabulary, word]


def round_labels_for_canonical(
    vocabularies: Mapping[str, Any] | None, *, reference_set: str, canonical: Sequence[str]
) -> list[str]:
    """The ROUND's own spellings of a set of design labels, in the round's
    label order -- the inverse of :func:`canonical_label`.

    The harness plant battery states its expectation in the DESIGN's words
    (``["same", "variant"]``: a planted register row is a row, so those are
    the only honest answers). A round that re-spells the set the battery is
    scored against hands its judge other words, and a judge answering
    perfectly in them would miss every harness plant -- the batch would fail
    its own audit on a judge that did the task. So the expectation is
    translated into the round's words where the battery is built
    (:func:`build_plants`), and this is that translation.

    Non-injective on purpose: if two of a round's labels both map onto
    ``same``, both catch the plant, because both say what the plant is. The
    list is EMPTY when the round's vocabulary for that set offers no word
    for any of the design labels asked for -- a battery that no answer can
    catch, which :func:`build_plants` refuses to build rather than ship."""
    wanted = {str(name) for name in canonical}
    if not vocabularies:
        return [label for label in canonical if label in wanted]
    block = (vocabularies.get("sets") or {}).get(reference_set) or {}
    labels = list(block.get("labels") or [])
    if not labels:
        return [label for label in canonical if label in wanted]
    mapping = block.get("canonical") or {}
    return [label for label in labels if str(mapping.get(label, label)) in wanted]


#: The per-model idea-vector cache (knowledge schema v10). Keyed by
#: ``(idea_id, model_key)`` and carrying the SHA-256 of the exact text that
#: was embedded -- see :func:`fetch_idea_vectors`.
IDEA_VECTOR_TABLE = "vec_ideas"

#: How many ids :func:`fetch_idea_vectors` binds into one ``IN (...)``.
#: SQLite's compiled-in parameter ceiling is 32766 on a current build and
#: 999 on older ones; 500 clears both with room for the ``model_key`` bind,
#: and the archive read is one cheap indexed lookup per chunk either way.
_ID_BIND_CHUNK = 500


def _pack_vector(values: Sequence[float]) -> bytes:
    """Little-endian float64, NOT the float32 packing
    :mod:`trialerror.stores.vecindex` uses for ``vec_chunks``.

    The difference is deliberate and it is the whole reason this is written
    out here. ``vec_chunks`` mirrors sqlite-vec's own fixed-width column, so
    float32 is the shape it has to have; this table is read by nothing but
    :func:`_archive_rows`, and a cached vector that differs from a freshly
    embedded one in the seventh decimal would mean a batch built from the
    cache and a batch built under ``--reembed-archive`` ranked the archive
    minutely differently -- a difference nobody could explain from the
    artifacts, for eight bytes a dimension."""
    return struct.pack(f"<{len(values)}d", *[float(v) for v in values])


def _unpack_vector(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 8}d", blob))


def statement_digest(text: str) -> str:
    """The hash an idea vector is cached under: SHA-256 over the exact text
    the backend was handed.

    Deliberately NOT ``idea.statement_sha256``: that column is written by
    whoever created the row and may be absent or stale, while this one is
    computed from the very string that was embedded. A cache whose key can
    disagree with what it caches is worse than no cache."""
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def fetch_idea_vectors(
    store: Store, *, model_key: str, idea_ids: Sequence[str] = ()
) -> dict[str, dict[str, Any]]:
    """``{idea_id: {"statement_sha256", "vector"}}`` from :data:`IDEA_VECTOR_TABLE`.

    ``idea_ids`` empty means every cached row for this model key. The caller
    compares the stored hash against :func:`statement_digest` of the text it
    is about to embed -- a stale row is a miss, never a silently wrong
    vector.

    The ``IN`` list is issued in chunks of :data:`_ID_BIND_CHUNK`, because
    :func:`_archive_rows` names EVERY archived idea and SQLite refuses a
    statement with more than 32766 bound variables. That ceiling is the one
    failure mode in this cache that grows with the archive -- which is the
    growth the cache exists to bound -- and it would have surfaced as an
    uncaught ``sqlite3.OperationalError`` out of ``--judged-prep`` rather
    than as a refusal or a cache miss. Chunking rather than dropping the
    clause: ``model_key`` alone does not narrow a store holding several
    rounds' archives under one model."""
    sql = f"SELECT idea_id, statement_sha256, vector FROM {IDEA_VECTOR_TABLE} WHERE model_key = ?"
    ids = list(idea_ids)
    chunks: list[list[Any]] = (
        [[]] if not ids
        else [ids[i : i + _ID_BIND_CHUNK] for i in range(0, len(ids), _ID_BIND_CHUNK)]
    )
    out: dict[str, dict[str, Any]] = {}
    for chunk in chunks:
        statement = sql
        params: list[Any] = [model_key]
        if chunk:
            statement += f" AND idea_id IN ({','.join('?' for _ in chunk)})"
            params.extend(chunk)
        for row in store.knowledge.execute(statement, params).fetchall():
            out[str(row["idea_id"])] = {
                "statement_sha256": str(row["statement_sha256"]),
                "vector": _unpack_vector(row["vector"]),
            }
    return out


def store_idea_vectors(
    store: Store, *, model_key: str, vectors: Mapping[str, tuple[str, Sequence[float]]]
) -> int:
    """Write ``{idea_id: (statement_sha256, vector)}`` into the cache,
    replacing whatever that ``(idea_id, model_key)`` held.

    Replace rather than append: the cache answers ONE question ("what is
    this statement's vector under this model"), and a second row for a
    superseded statement is a row nothing would ever read."""
    if not vectors:
        return 0
    stamp = now()
    rows = [
        (idea_id, model_key, sha, len(list(vector)), _pack_vector(vector), stamp)
        for idea_id, (sha, vector) in sorted(vectors.items())
    ]
    with store.knowledge as conn:
        conn.executemany(
            f"INSERT OR REPLACE INTO {IDEA_VECTOR_TABLE} "
            "(idea_id, model_key, statement_sha256, dim, vector, created_ts) VALUES (?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def fill_idea_vector_cache(
    store: Store, *, idea_ids: Sequence[str], reembed: bool = False
) -> dict[str, Any]:
    """Embed ``idea_ids``' statements into :data:`IDEA_VECTOR_TABLE` now,
    rather than at the next screen. Lane FB-7 item 8a.

    FB-6 item 1 made the cache fill at SCREEN time, which is the first
    moment a vector was needed. Intake is the first moment one can EXIST,
    and it is also the moment the operator is sitting there: a round that
    intakes ninety records and then runs its first screen pays for ninety
    embeddings inside the screen, where the wait looks like the screen
    being slow.

    Returns ``{"model_key", "n_requested", "n_embedded", "n_cached",
    "warnings"}`` and NEVER raises for a backend problem. An absent or
    parked embedding backend is a warning here, not a refusal -- the
    screen-time fill is still there and still correct, so failing an intake
    over an optimisation would lose the records for nothing."""
    ids = [str(i) for i in dict.fromkeys(idea_ids) if i]
    out: dict[str, Any] = {
        "model_key": None, "n_requested": len(ids), "n_embedded": 0, "n_cached": 0, "warnings": [],
    }
    if not ids:
        return out
    try:
        model_key, backend = retrieve_engine._resolve_embed_backend(store, side="query")
    except Exception as exc:  # noqa: BLE001 - any backend resolution problem is a warning here
        out["warnings"].append(
            f"idea-vector cache not filled at intake: no usable embedding backend ({exc}). The screen "
            "fills it on its first read, so nothing is lost but the wait moves there"
        )
        return out
    out["model_key"] = model_key
    rows = [read_idea(store, idea_id=i) for i in ids]
    present = [r for r in rows if r is not None]
    digests = {str(r["idea_id"]): statement_digest(_statement(r)) for r in present}
    cached = (
        {} if reembed
        else fetch_idea_vectors(store, model_key=model_key, idea_ids=sorted(digests))
    )
    misses = [
        r for r in present
        if (cached.get(str(r["idea_id"])) or {}).get("statement_sha256") != digests[str(r["idea_id"])]
    ]
    out["n_cached"] = len(present) - len(misses)
    if not misses:
        return out
    try:
        fresh = backend.embed_batch([_statement(r) for r in misses], kind="document")
    except Exception as exc:  # noqa: BLE001 - a parked backend is a warning, not a lost intake
        out["warnings"].append(
            f"idea-vector cache not filled at intake: the embedding backend refused ({exc}). The screen "
            "fills it on its first read"
        )
        return out
    written: dict[str, tuple[str, Sequence[float]]] = {}
    for row, vector in zip(misses, fresh):
        written[str(row["idea_id"])] = (digests[str(row["idea_id"])], list(vector))
    store_idea_vectors(store, model_key=model_key, vectors=written)
    out["n_embedded"] = len(written)
    return out


def _archive_rows(
    store: Store, *, backend: Any, model_key: str, reembed: bool = False
) -> list[dict[str, Any]]:
    """R2: every ``idea`` row with its statement's vector under this model.

    The same vector path R3 takes (:func:`build_plants`'s own bundle builder
    embeds the plant statement and cosines it against the register rows), for
    the same reason: the archive's rows are records, not chunks, so no
    ``vec_chunks`` table holds them and the only honest way to rank them
    against a statement is to compare them under the batch's own model.

    **Embedded ONCE per statement per model** (lane FB-6 item 1). The archive
    is never reset, so re-embedding it on every ``--judged-prep`` and every
    ``--calibration`` was the one cost here that grows for the rest of a
    programme's life -- a live round measured 76 rows at minutes per build.
    The vectors are cached in :data:`IDEA_VECTOR_TABLE` keyed by ``idea_id``
    and ``model_key``, with the hash of the exact text embedded: a row whose
    statement changed hashes differently and is re-embedded, a row written
    since the last build is embedded for the first time, and a run under a
    different embedding model misses every row rather than mixing two vector
    spaces in one ranking. ``reembed=True`` (the CLI's
    ``--reembed-archive``) rebuilds the lot, which is the answer when a
    backend's own weights changed under an unchanged model key.

    Read WHOLE, every status included. A merged or eliminated record is
    still in the reference set forever (that is what "never reset" means),
    and an archived row -- a prior round's candidate or request row written
    in through ``lens intake --status archived`` -- is the content a round
    that declares R2 is usually declaring it FOR."""
    rows = [
        dict(r)
        for r in store.knowledge.execute(
            "SELECT idea_id, round_id, status, body, created_ts FROM idea ORDER BY created_ts, idea_id"
        ).fetchall()
    ]
    if not rows:
        return []
    digests = {str(r["idea_id"]): statement_digest(str(r["body"] or "")) for r in rows}
    cached = (
        {} if reembed
        else fetch_idea_vectors(store, model_key=model_key, idea_ids=sorted(digests))
    )
    misses = [
        r for r in rows
        if (cached.get(str(r["idea_id"])) or {}).get("statement_sha256") != digests[str(r["idea_id"])]
    ]
    if misses:
        fresh = backend.embed_batch([str(r["body"] or "") for r in misses], kind="document")
        written: dict[str, tuple[str, Sequence[float]]] = {}
        for row, vector in zip(misses, fresh):
            idea_id = str(row["idea_id"])
            cached[idea_id] = {"statement_sha256": digests[idea_id], "vector": list(vector)}
            written[idea_id] = (digests[idea_id], list(vector))
        store_idea_vectors(store, model_key=model_key, vectors=written)
    for row in rows:
        row["vector"] = (cached.get(str(row["idea_id"])) or {}).get("vector")
    return rows


def _nearest_archive_rows(
    vector: Sequence[float] | None,
    rows: Sequence[Mapping[str, Any]],
    *,
    k: int = DEFAULT_ARCHIVE_ROWS_K,
    exclude: Sequence[str] = (),
    must_include: str | None = None,
) -> list[dict[str, Any]]:
    """The nearest R2 rows to one statement, nearest first, with their text.

    ``exclude`` drops the subject's own row: handing a judge the record it
    is judging, as the nearest archive row, guarantees the answer ``same``
    for every record in the round. ``must_include`` forces a row into the
    bundle whatever the ranking says -- passed for the donor of a plant cut
    from an archive row, on exactly the argument
    :func:`build_plants`'s ``must_include`` states: if the row the plant was
    cut from is absent, a judge doing the task honestly answers
    "new-mechanism" and the battery fails the batch for the embedding's
    shortcomings rather than the judge's."""
    if vector is None:
        return []
    dropped = set(exclude)
    scored = sorted(
        (
            (cosine_similarity(list(vector), list(row["vector"])), row)
            for row in rows
            if row.get("vector") and row["idea_id"] not in dropped
        ),
        key=lambda pair: (-pair[0], str(pair[1]["idea_id"])),
    )
    top = scored[: max(k, 0)]
    if must_include and must_include not in {row["idea_id"] for _sim, row in top}:
        forced = next((pair for pair in scored if pair[1]["idea_id"] == must_include), None)
        if forced is not None:
            top = sorted([*top[: max(k - 1, 0)], forced], key=lambda pair: (-pair[0], str(pair[1]["idea_id"])))
    return [
        {
            "idea_id": row["idea_id"],
            "round_id": row["round_id"],
            "status": row["status"],
            # Same hygiene the record's own statement gets, and the same
            # untrusted wrapper every other reference row a judge is handed
            # carries: an archive row is another author's text.
            "statement": untrusted_wrap(strip_self_assessment(str(row["body"] or ""))["text"]),
            "similarity": round(sim, 6),
        }
        for sim, row in top
    ]


def _inventory_coverage(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """``{"n_rows", "n_vectorized"}`` for one R3 read.

    Recorded on the snapshot, on the batch and on every dossier. An R3 with
    rows but no vectors is not a quiet degradation: the flag never fires, no
    idea is routed to a mandatory judgment, and the plants that would have
    caught it cannot be built either -- so the number that says whether R3
    was usable travels with every artifact that depends on it."""
    return {"n_rows": len(rows), "n_vectorized": sum(1 for r in rows if r.get("vector"))}


def reference_snapshot(
    store: Store, *, round_id: str, external: ExternalProvider | None = None, model_key: str | None = None
) -> dict[str, Any]:
    """Content hashes and counts for R1-R5, as of now.

    Novelty is relative to named reference sets AT A TIMESTAMP -- that is
    the whole claim the screen is allowed to make -- so every dossier and
    every verdict carries this snapshot. Without it, "nothing retrieved
    this" is a sentence about an unknown corpus."""
    round_ideas = store.knowledge.execute(
        "SELECT idea_id, COALESCE(statement_sha256, '') AS sha FROM idea WHERE round_id = ?", (round_id,)
    ).fetchall()
    all_ideas = store.knowledge.execute("SELECT idea_id, status FROM idea").fetchall()
    inventory = store.knowledge.execute(
        """
        SELECT c.chunk_id AS id, c.sha256 AS sha FROM chunk c
        JOIN document d ON d.doc_id = c.doc_id
        JOIN source s ON s.source_id = d.source_id
        WHERE s.kind = ?
        """,
        (INVENTORY_SOURCE_KIND,),
    ).fetchall()
    # R4 is "the corpus a lens can retrieve", so it is the complement of the
    # WHOLE exclusion tuple, not of its first element: a second excluded kind
    # would otherwise be counted here as ordinary prior art at the same
    # moment it stopped being reachable.
    excluded = tuple(dict.fromkeys((*retrieve_engine.DEFAULT_EXCLUDED_KINDS, INVENTORY_SOURCE_KIND)))
    corpus = store.knowledge.execute(
        f"""
        SELECT c.chunk_id AS id, c.sha256 AS sha FROM chunk c
        JOIN document d ON d.doc_id = c.doc_id
        JOIN source s ON s.source_id = d.source_id
        WHERE s.kind NOT IN ({','.join('?' for _ in excluded)})
        """,
        excluded,
    ).fetchall()

    # How much of R3 the screen could actually compare against. Counted here
    # rather than inferred, because "R3 n=8" beside a flag that never fired
    # reads as "nothing was close" when it may mean "nothing was measured".
    coverage = {"n_rows": len(inventory), "n_vectorized": 0}
    if inventory and model_key:
        coverage["n_vectorized"] = len(fetch_vectors(store, model_key, [r["id"] for r in inventory]))

    return {
        "ts": now(),
        "model_key": model_key,
        "R1": {"n": len(round_ideas), "sha256": _digest((r["idea_id"], r["sha"]) for r in round_ideas)},
        "R2": {"n": len(all_ideas), "sha256": _digest((r["idea_id"], r["status"]) for r in all_ideas)},
        "R3": {
            "n": len(inventory),
            "sha256": _digest((r["id"], r["sha"]) for r in inventory),
            "n_vectorized": coverage["n_vectorized"],
            "kind": INVENTORY_SOURCE_KIND,
        },
        "R4": {"n": len(corpus), "sha256": _digest((r["id"], r["sha"]) for r in corpus), "excluded_kinds": list(excluded)},
        "R5": {"provider": getattr(external, "name", "none"), "snapshot_id": external.snapshot_id() if external else "none"},
    }


# ---------------------------------------------------------------------------
# where a round's artifacts live
# ---------------------------------------------------------------------------


def round_dir(program_root: Path | str, round_id: str) -> Path:
    """``<program>/artifacts/rounds/<round_id>`` -- the round's own
    directory under the scaffold's existing ``artifacts/``. The screen
    writes files here and registers nothing: artifact registration is the
    orchestrator's, and a screen that registered its own output would be
    grading its own homework."""
    return Path(program_root) / "artifacts" / "rounds" / str(round_id)


def _dossier_path(base: Path, idea_id: str) -> Path:
    return base / "novelty" / f"{idea_id}.json"


def _next_batch_id(base: Path) -> str:
    """``batch-N``, N counting the batch records already on file. Named off
    the BATCH directory rather than the dossier count so the ids read
    ``batch-0, batch-1, ...`` in the order the batches ran, instead of
    jumping by however many records each one happened to carry."""
    existing = list((base / "batches").glob("*.json")) if (base / "batches").is_dir() else []
    return f"batch-{len(existing)}"


# ---------------------------------------------------------------------------
# 3a: the mechanical screen
# ---------------------------------------------------------------------------


def _distance(a: Sequence[float] | None, b: Sequence[float] | None) -> float | None:
    if a is None or b is None:
        return None
    return 1.0 - cosine_similarity(list(a), list(b))


def _entropy(counts: Mapping[str, int], *, vocabulary_size: int) -> float | None:
    """Shannon entropy over ``counts``, normalized by ``log(vocabulary_size)``
    so it reads 0..1 regardless of how many labels the axis has. ``None``
    for an empty count set or a single-label vocabulary -- an entropy over
    nothing is not zero, it is undefined, and reporting 0.0 there would read
    as maximal collapse."""
    total = sum(counts.values())
    if total <= 0 or vocabulary_size < 2:
        return None
    h = 0.0
    for n in counts.values():
        if n <= 0:
            continue
        p = n / total
        h -= p * math.log(p)
    return round(h / math.log(vocabulary_size), 6)


def _split_operation(declared: Any) -> tuple[str | None, str | None]:
    """``operation_declared`` as ``(opportunity, method)``.

    Two spellings are accepted because both occur in practice: a JSON object
    with the two keys, and the flat ``"opportunity/method"`` string. Anything
    else is read as an opportunity alone -- half a declaration is still a
    declaration, and dropping it would understate the count it belongs in."""
    if isinstance(declared, Mapping):
        opportunity = declared.get("opportunity")
        method = declared.get("method")
        return (str(opportunity) if opportunity else None, str(method) if method else None)
    text = str(declared or "").strip()
    if not text:
        return (None, None)
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except (TypeError, ValueError):
            obj = None
        if isinstance(obj, dict):
            return _split_operation(obj)
    if "/" in text:
        opportunity, method = text.split("/", 1)
        return (opportunity.strip() or None, method.strip() or None)
    return (text, None)


def _axis_counts(values: Sequence[str | None], vocabulary: Sequence[str]) -> dict[str, Any]:
    """Counts per axis value, plus the two buckets that must never be
    silently folded away: ``undeclared`` (the author named nothing) and
    ``off_taxonomy`` (the author named something outside the vocabulary).
    Entropy is computed over the declared values only, and the two buckets
    are reported beside it so a high entropy built out of off-taxonomy
    labels cannot pass for a diverse batch."""
    counts = {v: 0 for v in vocabulary}
    undeclared = 0
    off_taxonomy: dict[str, int] = {}
    for value in values:
        if not value:
            undeclared += 1
        elif value in counts:
            counts[value] += 1
        else:
            off_taxonomy[value] = off_taxonomy.get(value, 0) + 1
    return {
        "counts": counts,
        "undeclared": undeclared,
        "off_taxonomy": off_taxonomy,
        "entropy": _entropy(counts, vocabulary_size=len(vocabulary)),
    }


def _pairwise_stats(vectors: Sequence[Sequence[float]]) -> dict[str, Any]:
    """Median / 90th percentile / max pairwise cosine similarity within a
    batch -- the "are these all the same idea" signal, reported as a
    distribution rather than reduced to one number."""
    sims: list[float] = []
    for i in range(len(vectors)):
        for j in range(i + 1, len(vectors)):
            sims.append(cosine_similarity(list(vectors[i]), list(vectors[j])))
    if not sims:
        return {"n_pairs": 0, "median": None, "p90": None, "max": None}
    sims.sort()
    def _q(p: float) -> float:
        if len(sims) == 1:
            return sims[0]
        idx = min(len(sims) - 1, int(round(p * (len(sims) - 1))))
        return sims[idx]
    return {
        "n_pairs": len(sims),
        "median": round(_q(0.5), 6),
        "p90": round(_q(0.9), 6),
        "max": round(sims[-1], 6),
    }


def _collapse(
    distribution: Mapping[str, Any], alarms: Mapping[str, Any] | None
) -> dict[str, Any]:
    """The collapse flag, against PRE-REGISTERED alarm values only.

    With no alarms the result is ``descriptive``: the numbers are reported
    and nothing is flagged. That is not a placeholder -- the design grades
    the evidence behind these alarms as single-source and says the values
    must come from a round's own first batch before they can act. A flag
    raised against a threshold invented at screen time would be a decision
    rule written after seeing the data."""
    if not alarms:
        return {"mode": "descriptive", "flag": False, "reasons": []}
    reasons: list[dict[str, Any]] = []
    median = distribution["pairwise_similarity"]["median"]
    max_median = alarms.get("median_pairwise_cosine_max")
    if median is not None and max_median is not None and median > max_median:
        reasons.append({"alarm": "median_pairwise_cosine_max", "value": median, "bound": max_median})
    entropy_floors = alarms.get("operation_entropy_min") or {}
    for axis in ("opportunity", "method"):
        floor = entropy_floors.get(axis) if isinstance(entropy_floors, Mapping) else None
        entropy = distribution["declared_operations"][axis]["entropy"]
        if entropy is not None and floor is not None and entropy < floor:
            reasons.append({"alarm": f"operation_entropy_min.{axis}", "value": entropy, "bound": floor})
    template_max = alarms.get("template_mass_max")
    template = distribution["template_mass"]["template_share"]
    if template is not None and template_max is not None and template > template_max:
        reasons.append({"alarm": "template_mass_max", "value": template, "bound": template_max})
    return {"mode": "armed", "flag": bool(reasons), "reasons": reasons}


def _merge_near_duplicates(
    store: Store,
    *,
    batch: Sequence[Mapping[str, Any]],
    archive: Sequence[Mapping[str, Any]],
    vectors: Mapping[str, Sequence[float]],
    threshold: float,
) -> dict[str, Any]:
    """Fold each batch idea that is a near-duplicate of another idea, in
    this round or anywhere in the archive, into one surviving record.

    Two conditions, both required: cosine at or above ``threshold`` AND the
    same home cell. Similarity alone would merge two genuinely different
    mechanics that happen to be described in the same vocabulary; the home
    cell is the author's own claim about what the idea is FOR, and two
    records that make the same claim in near-identical words are one record.

    The survivor is the oldest member of each cluster, so a later
    re-proposal folds into the original rather than the other way round --
    which is what keeps the archive's own precedence intact. The folded
    record is NOT deleted: it takes status ``merged``, points at the
    survivor through ``parent_ids``, and stays in the reference set forever.
    The survivor's provenance gains the union of the cluster's documents and
    a ``merged_from`` list; every other key it carried is left untouched,
    because that blob is also where interim-convention record fields live.

    **An ARCHIVED row is never merged, in either direction** (lane FB-5 item
    4). An archived row is a prior round's candidate or request row, written
    in so this round can be judged against it as reference set R2; it is in
    the reference set and is not a candidate. Folding a live record INTO one
    would silently retire this round's record into an archive entry nobody
    reviewed, and folding an archived row into a live record would rewrite
    the archive from the round it is meant to be the fixed background for.
    So the pair is reported instead: the live record carries an
    ``archive_hit`` -- the archived row's id and the cosine -- into its
    dossier, and a judge decides. A flag, not a fold, for exactly the reason
    the KNOWN-MECHANIC flag is a flag."""
    by_id = {i["idea_id"]: i for i in archive}
    for idea in batch:
        by_id.setdefault(idea["idea_id"], idea)
    scoreable = [i for i in by_id.values() if vectors.get(i["idea_id"]) is not None]
    uf = UnionFind([i["idea_id"] for i in scoreable])
    archived_ids = {i["idea_id"] for i in scoreable if i.get("status") == ARCHIVED_STATUS}

    # Bucket by home cell before comparing. Two records in different cells
    # can never merge, so a full pairwise sweep of the archive would spend
    # O(archive^2) cosines to prove what the bucket already knows -- and the
    # archive is never reset, so that cost only grows.
    batch_ids = {i["idea_id"] for i in batch}
    by_home: dict[Any, list[Mapping[str, Any]]] = {}
    for idea in scoreable:
        by_home.setdefault(idea.get("home") or None, []).append(idea)

    pairs: list[dict[str, Any]] = []
    archive_hits: dict[str, list[dict[str, Any]]] = {}
    for bucket in by_home.values():
        for i, left in enumerate(bucket):
            for right in bucket[i + 1 :]:
                if left["idea_id"] not in batch_ids and right["idea_id"] not in batch_ids:
                    continue  # two already-screened records are not this batch's business
                sim = cosine_similarity(list(vectors[left["idea_id"]]), list(vectors[right["idea_id"]]))
                if sim < threshold:
                    continue
                left_archived = left["idea_id"] in archived_ids
                right_archived = right["idea_id"] in archived_ids
                if left_archived or right_archived:
                    # A flag on the LIVE record, never a fold. Two archived
                    # rows near each other are the archive's own business and
                    # not this batch's, so nothing is recorded for that pair.
                    if left_archived and right_archived:
                        continue
                    live, archived_row = (
                        (right["idea_id"], left["idea_id"]) if left_archived
                        else (left["idea_id"], right["idea_id"])
                    )
                    archive_hits.setdefault(live, []).append(
                        {"idea_id": archived_row, "round_id": by_id[archived_row].get("round_id"),
                         "similarity": round(sim, 6)}
                    )
                    continue
                uf.union(left["idea_id"], right["idea_id"])
                pairs.append({"a": left["idea_id"], "b": right["idea_id"], "similarity": round(sim, 6)})

    merged: list[dict[str, Any]] = []
    for members in uf.groups().values():
        if len(members) < 2:
            continue
        ordered = sorted(members, key=lambda mid: (str(by_id[mid].get("created_ts") or ""), mid))
        survivor, folded = ordered[0], ordered[1:]
        docs: list[str] = []
        for member in ordered:
            for doc in _provenance_docs(by_id[member]):
                if doc not in docs:
                    docs.append(doc)
        provenance = _provenance_obj(by_id[survivor].get("provenance"))
        if docs:
            provenance["docs"] = docs
        provenance["merged_from"] = sorted(set(provenance.get("merged_from") or []) | set(folded))
        store_update(
            store, "idea", pk_column="idea_id", pk_value=survivor,
            changes={"provenance": json.dumps(provenance, ensure_ascii=False, sort_keys=True)},
        )
        for member in folded:
            if by_id[member].get("status") == "merged":
                continue
            parent_ids = sorted(set(by_id[member].get("parent_ids") or []) | {survivor})
            store_update(
                store, "idea", pk_column="idea_id", pk_value=member,
                changes={"status": "merged", "parent_ids": json.dumps(parent_ids, ensure_ascii=False)},
            )
            merged.append({"idea_id": member, "merged_into": survivor})
    return {
        "merged": merged,
        "pairs": pairs,
        "archive_hits": {
            live: sorted(hits, key=lambda h: (-h["similarity"], h["idea_id"]))
            for live, hits in sorted(archive_hits.items())
        },
    }


def _require_query_embed_backend(store: Store, *, corpus_mode: str, action: str) -> None:
    """Refuse, up front, when this process cannot embed the text a screen is
    about to embed (lane F-1 item D, C-0096).

    A screen is a status-changing read: it writes dossiers, merges
    near-duplicates, and its labels become verdict rows. Every one of those
    rests on the statement's own vector, so there is no version of this that
    degrades quietly -- and the failure it replaces was
    :class:`~trialerror.offload.marker.OffloadNotRunnable` surfacing as a
    traceback out of the middle of a batch, after some dossiers had been
    written.

    ``corpus_mode="fts"`` is named in the message rather than honoured here,
    and that distinction is worth being precise about: ``--corpus-mode fts``
    removes the need for a query vector from the R4 RETRIEVAL tier (see
    :func:`trialerror.verify.hypothesis.stratified_retrieve`, which is where
    that carve-out lives and where it is recorded as
    ``stratify_method="rank_fallback"``), but it cannot remove the screen's
    own need to embed the record's statement for R1/R2/R3. So a screen on a
    program with no runnable query-side backend refuses in either mode, and
    says which of the two needs are unmet."""
    runnable, reason = retrieve_engine.query_embed_runnable(store)
    if runnable:
        return
    raise QueryEmbedBackendUnrunnableError(
        retrieve_engine.query_embed_refusal_message(reason, action=action)
        + " The screen embeds each record's own statement for the R1/R2/R3 reference sets, so"
        f" --corpus-mode {corpus_mode!r} does not lift this: it only restricts the R4 retrieval tier."
    )


def run_mechanical_screen(
    store: Store,
    *,
    round_id: str,
    launch_id: str | None = None,
    idea_ids: Sequence[str] | None = None,
    batch_id: str | None = None,
    external: ExternalProvider | None = None,
    external_query_mode: str = "none",
    alarms: Mapping[str, Any] | None = None,
    family_of: Callable[[Mapping[str, Any]], str] | None = None,
    corpus_k: int = DEFAULT_CORPUS_K,
    corpus_mode: str = DEFAULT_CORPUS_MODE,
    external_k: int = DEFAULT_EXTERNAL_K,
    candidate_hit_similarity: float = DEFAULT_CANDIDATE_HIT_SIMILARITY,
    inventory_rows_k: int = DEFAULT_INVENTORY_ROWS_K,
    out_dir: Path | str | None = None,
    rescreen: bool = False,
) -> dict[str, Any]:
    """Phase 3a. No LLM anywhere in this function.

    Screens one BATCH -- by default every idea in the round that is still
    ``raw`` and has no dossier on file yet, which is what makes repeated
    calls incremental rather than a re-run. ``idea_ids`` names a batch
    explicitly; ``rescreen=True`` re-screens ideas that already have one.

    Returns the batch record: per-idea dossiers, the merge outcome, the
    round-level distribution audit, the collapse flag and the reference
    snapshot. The same content is written under the round's directory, one
    JSON file per idea plus an adjudication draft, because a dossier that
    exists only in a return value cannot be read by the person the round is
    reported to.
    """
    if external_query_mode not in EXTERNAL_QUERY_MODES:
        raise NoveltyError(
            f"external_query_mode must be one of {list(EXTERNAL_QUERY_MODES)!r}, got {external_query_mode!r}"
        )
    if external_query_mode != "none" and external is None:
        raise NoveltyError(
            f"external_query_mode={external_query_mode!r} names a query this call has no provider to issue; "
            "pass external=<provider>, or run with external_query_mode='none'"
        )

    base = Path(out_dir) if out_dir is not None else round_dir(store.program_root, round_id)
    # Lane F-1 item D: a screen embeds FRESH TEXT (every idea statement in the
    # batch) at read time, so it resolves the QUERY side -- on a program whose
    # document embeddings are produced on another machine, the document
    # backend cannot compute here at all. The key is the same either way
    # (resolution refuses a query side that disagrees), so every R3/R4 vector
    # lookup below is unchanged.
    _require_query_embed_backend(store, corpus_mode=corpus_mode, action="lens screen (--mechanical)")
    model_key, backend = retrieve_engine._resolve_embed_backend(store, side="query")

    archive = _load_ideas(store)
    round_ideas = [i for i in archive if i.get("round_id") == round_id]
    if idea_ids is not None:
        wanted = set(idea_ids)
        batch = [i for i in round_ideas if i["idea_id"] in wanted]
        missing = wanted - {i["idea_id"] for i in batch}
        if missing:
            raise NoveltyError(f"round {round_id!r} has no idea(s) {sorted(missing)!r} to screen")
    else:
        batch = [
            i for i in round_ideas
            if i.get("status") == "raw" and (rescreen or not _dossier_path(base, i["idea_id"]).exists())
        ]

    batch_id = batch_id or _next_batch_id(base)
    snapshot = reference_snapshot(store, round_id=round_id, external=external, model_key=model_key)

    # --- embeddings ------------------------------------------------------
    # Embed the batch, and from the archive only what the batch could
    # possibly merge with: a record in another home cell cannot merge with
    # anything here, so embedding it would be paying for a comparison the
    # bucketing below will not make. The archive is never reset, so "embed
    # everything on every batch" is the one line here that would get slower
    # every round for the rest of the program's life.
    batch_homes = {i.get("home") or None for i in batch}
    needed = {i["idea_id"]: i for i in batch}
    for idea in archive:
        if (idea.get("home") or None) in batch_homes:
            needed.setdefault(idea["idea_id"], idea)
    ordered_ids = sorted(needed)
    embedded = backend.embed_batch([_statement(needed[i]) for i in ordered_ids], kind="document") if ordered_ids else []
    vectors: dict[str, Sequence[float]] = {iid: vec for iid, vec in zip(ordered_ids, embedded)}
    # Lane FB-6 item 1: these are the same statements, under the same model
    # key, that the R2 archive is later ranked by -- so the screen fills the
    # cache it will read at --judged-prep / --calibration rather than paying
    # for the identical embedding a second time. Written here and not only in
    # :func:`_archive_rows` because this is the earliest moment a vector
    # exists, which is what "filled at screen time" means.
    store_idea_vectors(
        store,
        model_key=model_key,
        vectors={iid: (statement_digest(_statement(needed[iid])), vec) for iid, vec in vectors.items()},
    )

    # --- near-duplicate merge (R1 + R2) ----------------------------------
    merge = _merge_near_duplicates(
        store, batch=batch, archive=archive, vectors=vectors, threshold=NEAR_DUPLICATE_THRESHOLD
    )
    merged_ids = {m["idea_id"] for m in merge["merged"]}
    survivors = [i for i in batch if i["idea_id"] not in merged_ids]

    # --- R3: the inventory ------------------------------------------------
    all_inventory = _inventory_rows(store, model_key=model_key, family_of=family_of)
    coverage = _inventory_coverage(all_inventory)
    if coverage["n_rows"] and not coverage["n_vectorized"]:
        raise NoveltyError(
            f"reference set R3 holds {coverage['n_rows']} inventory row(s) but none is embedded under "
            f"model_key={model_key!r}: the KNOWN-MECHANIC flag cannot fire, no idea can be routed to a "
            "mandatory judgment, and the inventory plants that would catch that cannot be built either. "
            "Embed the inventory (trialerror ingest embed) before screening, or screen a round whose "
            "program has no inventory at all"
        )
    inventory = [r for r in all_inventory if r["vector"]]

    dossiers: dict[str, dict[str, Any]] = {}
    for idea in survivors:
        dossiers[idea["idea_id"]] = _build_dossier(
            store,
            idea=idea,
            vector=vectors.get(idea["idea_id"]),
            inventory=inventory,
            inventory_coverage=coverage,
            model_key=model_key,
            snapshot=snapshot,
            round_id=round_id,
            batch_id=batch_id,
            launch_id=launch_id,
            external=external,
            external_query_mode=external_query_mode,
            corpus_k=corpus_k,
            corpus_mode=corpus_mode,
            external_k=external_k,
            candidate_hit_similarity=candidate_hit_similarity,
            inventory_rows_k=inventory_rows_k,
            archive_hits=merge["archive_hits"].get(idea["idea_id"]) or [],
        )

    # --- within-round terciles (the slice metric, applied to outputs) ----
    _assign_terciles(dossiers, vectors)

    # --- round-level distribution audit ----------------------------------
    distribution = _distribution(survivors, [vectors[i["idea_id"]] for i in survivors if vectors.get(i["idea_id"])])
    collapse = _collapse(distribution, alarms)

    record = {
        "round_id": round_id,
        "batch_id": batch_id,
        "screen_version": SCREEN_VERSION,
        "ts": now(),
        "launch_id": launch_id,
        "external_query_mode": external_query_mode,
        "reference_snapshot": snapshot,
        "n_screened": len(batch),
        "n_merged": len(merge["merged"]),
        "inventory": coverage,
        "merged": merge["merged"],
        "near_duplicate_pairs": merge["pairs"],
        # Live records that landed on an ARCHIVED row: flagged, never folded
        # (see `_merge_near_duplicates`). Reported at batch level as well as
        # on each dossier, so "nothing merged" and "three records sit on the
        # archive" are two readable facts rather than one silence.
        "archive_hits": merge["archive_hits"],
        "dossiers": dossiers,
        "distribution": distribution,
        "collapse": collapse,
        "params": {
            "near_duplicate_threshold": NEAR_DUPLICATE_THRESHOLD,
            "known_mechanic_threshold": KNOWN_MECHANIC_THRESHOLD,
            "candidate_hit_similarity": candidate_hit_similarity,
            "corpus_k": corpus_k,
            "corpus_mode": corpus_mode,
            "external_k": external_k,
            "inventory_rows_k": inventory_rows_k,
            "weights": list(DEFAULT_WEIGHTS),
            "far_floor": DEFAULT_FAR_FLOOR,
        },
    }
    _write_batch(base, record)
    return record


def _build_dossier(
    store: Store,
    *,
    idea: Mapping[str, Any],
    vector: Sequence[float] | None,
    inventory: Sequence[Mapping[str, Any]],
    inventory_coverage: Mapping[str, int],
    model_key: str,
    snapshot: Mapping[str, Any],
    round_id: str,
    batch_id: str,
    launch_id: str | None,
    external: ExternalProvider | None,
    external_query_mode: str,
    corpus_k: int,
    corpus_mode: str,
    external_k: int,
    candidate_hit_similarity: float,
    inventory_rows_k: int = DEFAULT_INVENTORY_ROWS_K,
    archive_hits: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """One idea's dossier: what is near it, how near, and in which
    direction. Every number here is a distance or a count. None of them is
    a verdict, and the dossier carries no field a room admission order or a
    selection rule may read.

    It also carries the reference ROWS themselves -- the nearest R3 rows and
    the R4/R5 neighbours, each with its text -- because the judged envelope
    is built out of this dossier, and a judge asked to compare a record
    pairwise against retrieved rows has to be handed the rows."""
    family = home_family(idea.get("home"))

    # --- KNOWN-MECHANIC + the two family statistics ---------------------
    scored: list[tuple[float, Mapping[str, Any]]] = []
    best_same_family: tuple[float, Mapping[str, Any]] | None = None
    best_other_family: tuple[float, Mapping[str, Any]] | None = None
    best_overall: tuple[float, Mapping[str, Any]] | None = None
    if vector is not None:
        for row in inventory:
            sim = cosine_similarity(list(vector), list(row["vector"]))
            scored.append((sim, row))
            if best_overall is None or sim > best_overall[0]:
                best_overall = (sim, row)
            if family and row["family"] == family:
                if best_same_family is None or sim > best_same_family[0]:
                    best_same_family = (sim, row)
            elif family:
                if best_other_family is None or sim > best_other_family[0]:
                    best_other_family = (sim, row)

    known_mechanic = None
    if best_overall is not None and best_overall[0] >= KNOWN_MECHANIC_THRESHOLD:
        known_mechanic = {
            "row_id": best_overall[1]["row_id"],
            "family": best_overall[1]["family"],
            "similarity": round(best_overall[0], 6),
        }

    # The nearest R3 rows WITH their text, nearest first. Whether the flag
    # fired is deliberately NOT carried alongside them into the envelope: a
    # judge told "this one was flagged" has been given the answer.
    scored.sort(key=lambda pair: (-pair[0], str(pair[1]["row_id"])))
    inventory_rows = [
        {
            "row_id": row["row_id"],
            "doc_id": row["doc_id"],
            "family": row["family"],
            "similarity": round(sim, 6),
            "text": _fenced_text(row["text"], row.get("license_tier")),
        }
        for sim, row in scored[: max(inventory_rows_k, 0)]
    ]

    # --- d_prov ----------------------------------------------------------
    provenance_docs = _provenance_docs(idea)
    d_prov = None
    if vector is not None and provenance_docs:
        doc_vectors = fetch_doc_vectors(store, model_key=model_key, doc_ids=provenance_docs)
        distances = [d for d in (_distance(vector, v) for v in doc_vectors.values()) if d is not None]
        if distances:
            d_prov = round(sum(distances) / len(distances), 6)

    # --- R4 + R5 candidate hits -----------------------------------------
    statement = _statement(idea)
    corpus = _corpus_neighbours(
        store, statement=statement, vector=vector, model_key=model_key, k_total=corpus_k,
        mode=corpus_mode, hit_similarity=candidate_hit_similarity,
    )
    external_hits = _external_neighbours(
        store, idea=idea, external=external, mode=external_query_mode, k=external_k,
        launch_id=launch_id, round_id=round_id,
    )

    return {
        "idea_id": idea["idea_id"],
        "round_id": round_id,
        "batch_id": batch_id,
        "screen_version": SCREEN_VERSION,
        "ts": now(),
        # Which retrieval tier built R4 for THIS record (lane F-1 item D).
        # The batch record's `params` already carried it for the batch; a
        # dossier is what a judge and a reviewer read, and "these neighbours
        # came from the full-text tier only" is part of what the bundle IS.
        "corpus_mode": corpus_mode,
        "stratify_method": corpus.get("stratify_method"),
        "lens_launch": idea.get("author_launch"),
        "home": idea.get("home"),
        "home_family": family or None,
        "tier": idea.get("tier"),
        "recipe_card": idea.get("recipe_card"),
        "operation_declared": idea.get("operation_declared"),
        "reference_snapshot": snapshot,
        "known_mechanic": known_mechanic,
        # Lane FB-5 item 4: the archived rows this record sits on top of.
        # A near-duplicate of an ARCHIVED row is not merged into it -- the
        # archive is the fixed background a round is judged against, not a
        # place to retire this round's records into -- so the pair is
        # recorded here and a judge decides. Empty is the ordinary case and
        # means "compared, nothing close", the same way `inventory_compared`
        # says whether the R3 comparison happened at all.
        "archive_hit": [dict(hit) for hit in archive_hits],
        # What R3 actually offered this comparison. `n_compared` is the rows
        # this dossier was scored against; `n_rows`/`n_vectorized` say
        # whether that was all of them. A flag that never fired means
        # something different under 8/8 than under 8/0.
        "inventory_compared": {
            "n_compared": len(scored),
            "n_rows": inventory_coverage.get("n_rows", 0),
            "n_vectorized": inventory_coverage.get("n_vectorized", 0),
        },
        "inventory_rows": inventory_rows,
        "distances": {
            "d_prov": d_prov,
            "d_home": round(1.0 - best_same_family[0], 6) if best_same_family else None,
            "leap": (
                {
                    "row_id": best_other_family[1]["row_id"],
                    "family": best_other_family[1]["family"],
                    "distance": round(1.0 - best_other_family[0], 6),
                }
                if best_other_family
                else None
            ),
            "H_prior": corpus["H_prior"],
            "tercile": None,
            "tercile_metric": None,
        },
        "candidate_hits": {"R4": corpus["hits"], "R5": external_hits},
        "judged": False,
        # Two labelled reference sets, not three. R5 is EVIDENCE for the
        # literature label, not a labelled set of its own: the design's
        # literature vocabulary (stated/implied/adjacent/absent) is one
        # judgment "against R4/R5", and a `label_external` nothing ever read
        # or wrote was a field asserting a third verdict row that does not
        # exist. R5's hits ride in the same envelope and are cited in the
        # same verdict's evidence.
        "label_inventory": UNJUDGED_LABEL,
        "label_corpus": None,
    }


def _corpus_neighbours(
    store: Store,
    *,
    statement: str,
    vector: Sequence[float] | None,
    model_key: str,
    k_total: int,
    mode: str,
    hit_similarity: float,
    config: Any = None,
) -> dict[str, Any]:
    """R4, through :func:`trialerror.verify.hypothesis.stratified_retrieve`
    verbatim -- the 40/40/20 stratified retrieval with a far floor of 2 that
    the design names, reused rather than re-derived so the screen's prior-art
    bundle is stratified by exactly the machinery the hypothesis pipeline
    is. The inventory is absent from these results by the engine's own
    default kind exclusion, which is what keeps R3 and R4 separate sets.

    ``H_prior`` is the normalized entropy of the idea's similarity
    distribution over the neighbours retrieved -- a positioning statistic
    (is this idea near one prior thing or spread evenly over many?), never a
    quality score. Negative cosines are clipped to zero before normalizing,
    because a distribution needs non-negative mass and "points away from
    this neighbour" is not evidence of proximity to it; the consequence,
    stated rather than hidden, is that a bundle where one neighbour is the
    only positive match reads 0.0 -- concentrated, which is what it is."""
    if not statement.strip():
        return {"hits": [], "H_prior": None, "stratify_method": None}
    arms = stratified_retrieve(
        store, query=statement, k_total=k_total, weights=DEFAULT_WEIGHTS, far_floor=DEFAULT_FAR_FLOOR, mode=mode
    )
    rows = arms["all"]
    if not rows:
        return {"hits": [], "H_prior": None, "stratify_method": arms.get("stratify_method")}
    chunk_vectors = fetch_vectors(store, model_key, [r["chunk_id"] for r in rows])

    # R4's cosines in ONE scan (lane FB-7 item 1). The pass is
    # :func:`trialerror.util.vecmath.cosine_many`, which stays on the plain
    # Python cosine for the few dozen rows a normal ``k_total`` retrieves and
    # switches to a blocked numpy matmul only when a screen asks for a
    # universe big enough to be worth it -- so the numbers on a small screen
    # are bit-for-bit the ones this function always produced, and a large one
    # stops being a ten-minute wait. The keys are the arm rows in arm order,
    # so ``scored`` is indexed by exactly the loop below.
    scan_rows = [row for arm in ("near", "moderate", "far") for row in arms[arm]]
    scored: dict[int, float] = {}
    if vector is not None:
        present = [
            (index, chunk_vectors[row["chunk_id"]])
            for index, row in enumerate(scan_rows)
            if chunk_vectors.get(row["chunk_id"]) is not None
        ]
        if present:
            values = cosine_many(
                list(vector),
                [vec for _index, vec in present],
                # The PROGRAM's config, resolved off the store when the
                # caller handed none (lane FB-7 fix pass, V-1): passing
                # ``None`` here read the process environment and never the
                # program's own ``[retrieve] numpy_fastpath``, so ``off``
                # turned nothing off for the screen it was added for.
                config=program_config(store, config),
            )
            scored = {index: value for (index, _vec), value in zip(present, values)}

    hits: list[dict[str, Any]] = []
    similarities: list[float] = []
    position = 0
    for arm in ("near", "moderate", "far"):
        for row in arms[arm]:
            sim = scored.get(position)
            position += 1
            if sim is not None:
                similarities.append(max(0.0, sim))
            if sim is not None and sim >= hit_similarity:
                hits.append(
                    {
                        "reference_set": "R4",
                        "chunk_id": row["chunk_id"],
                        "source_id": row["citation"]["source_id"],
                        "arm": arm,
                        "similarity": round(sim, 6),
                        # The engine has already fenced and untrusted-wrapped
                        # this text. Carrying it is the point: a hit recorded
                        # as an id and a number tells a judge that something
                        # is near without telling it what, which is not a
                        # pairwise comparison.
                        "text": row.get("text"),
                    }
                )

    h_prior = None
    total = sum(similarities)
    if len(similarities) > 1 and total > 0:
        h = 0.0
        for sim in similarities:
            p = sim / total
            if p > 0:
                h -= p * math.log(p)
        h_prior = round(h / math.log(len(similarities)), 6)
    return {"hits": hits, "H_prior": h_prior, "stratify_method": arms.get("stratify_method")}


def _external_neighbours(
    store: Store,
    *,
    idea: Mapping[str, Any],
    external: ExternalProvider | None,
    mode: str,
    k: int,
    launch_id: str | None,
    round_id: str,
) -> list[dict[str, Any]]:
    """R5. Every issued query is logged as an event carrying the launch id
    and the mode -- what left the machine, on whose booking, under which
    pre-registered setting. ``mode="none"`` issues nothing and logs
    nothing, because a query that was not made is not an egress event.

    The event is written in a ``finally``, so a provider that RAISES still
    leaves the audit line. The text had already left the machine by then;
    logging only on the happy path would mean the one record of what was
    sent is missing exactly when something went wrong with sending it."""
    if external is None or mode == "none":
        return []
    text = _statement(idea) if mode == "statement" else neutral_abstract(idea)
    query = ExternalQuery(text=text, mode=mode, k=k, idea_id=idea["idea_id"], round_id=round_id)
    results: list[dict[str, Any]] = []
    error: str | None = None
    try:
        results = [dict(r) for r in external.search(query)]
    except Exception as exc:  # noqa: BLE001 -- recorded, then re-raised unchanged
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        append_event(
            store,
            event_type="novelty_external_query",
            payload={
                "round_id": round_id,
                "idea_id": idea["idea_id"],
                "provider": getattr(external, "name", "unknown"),
                "external_query_mode": mode,
                "query_text": text,
                "k": k,
                "n_results": len(results),
                "error": error,
            },
            launch_id=launch_id,
        )
    for row in results:
        row.setdefault("reference_set", "R5")
    return results


def _assign_terciles(dossiers: Mapping[str, dict[str, Any]], vectors: Mapping[str, Sequence[float]]) -> None:
    """Cut this batch's ideas into within-round terciles with
    :func:`trialerror.lens.stratify.stratify` -- the same tercile machinery
    the slice assignment uses on INPUTS, applied here to outputs.

    The metric is ``d_prov`` when every scoreable idea has one, and distance
    to the batch centroid otherwise; whichever ran is recorded on each
    dossier, because "far third" means something different under the two and
    a reader must not have to guess which. An idea with neither is left
    ``None`` rather than dropped into a third it was not measured for."""
    d_prov = {iid: d["distances"]["d_prov"] for iid, d in dossiers.items() if d["distances"]["d_prov"] is not None}
    if d_prov and len(d_prov) == len(dossiers):
        scores, metric = d_prov, "d_prov"
    else:
        centroid_ids = [iid for iid in dossiers if vectors.get(iid) is not None]
        if len(centroid_ids) < 2:
            return
        dims = len(vectors[centroid_ids[0]])
        centroid = [sum(vectors[iid][d] for iid in centroid_ids) / len(centroid_ids) for d in range(dims)]
        scores = {iid: 1.0 - cosine_similarity(list(vectors[iid]), centroid) for iid in centroid_ids}
        metric = "centroid"
    for candidate in stratify(scores):
        dossiers[candidate.candidate_id]["distances"]["tercile"] = candidate.arm
        dossiers[candidate.candidate_id]["distances"]["tercile_metric"] = metric


def _distribution(ideas: Sequence[Mapping[str, Any]], vectors: Sequence[Sequence[float]]) -> dict[str, Any]:
    """The round-level audit: declared-operation counts and entropy per
    axis, the pairwise-similarity distribution, and template mass.

    Monitoring signals and pre-registered outcomes -- never a per-idea
    score, and never an admission criterion. Nothing downstream reads them
    per idea, because there is nothing per idea here to read."""
    split = [_split_operation(i.get("operation_declared")) for i in ideas]
    opportunities = [o for o, _m in split]
    methods = [m for _o, m in split]
    n = len(ideas)
    bridge = sum(1 for o in opportunities if o == TEMPLATE_OPPORTUNITY)
    synthesis = sum(1 for m in methods if m == TEMPLATE_METHOD)
    template = sum(1 for o, m in split if o == TEMPLATE_OPPORTUNITY and m == TEMPLATE_METHOD)
    return {
        "n": n,
        "declared_operations": {
            "opportunity": _axis_counts(opportunities, OPPORTUNITY_AXIS),
            "method": _axis_counts(methods, METHOD_AXIS),
        },
        "pairwise_similarity": _pairwise_stats(vectors),
        "template_mass": {
            "bridge_share": round(bridge / n, 6) if n else None,
            "synthesis_share": round(synthesis / n, 6) if n else None,
            "template_share": round(template / n, 6) if n else None,
        },
    }


def _write_batch(base: Path, record: Mapping[str, Any]) -> None:
    """One JSON file per dossier, one per batch, and a regenerated
    adjudication draft. Written with ``newline="\\n"`` so the same round
    produces the same bytes on either platform."""
    (base / "novelty").mkdir(parents=True, exist_ok=True)
    (base / "batches").mkdir(parents=True, exist_ok=True)
    for idea_id, dossier in record["dossiers"].items():
        _dossier_path(base, idea_id).write_text(
            json.dumps(dossier, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8", newline="\n",
        )
    (base / "batches" / f"{record['batch_id']}.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n",
    )
    _write_adjudication_draft(base, record)


def _all_dossiers_on_file(base: Path) -> list[dict[str, Any]]:
    """Every dossier the round has, read back off disk. A dossier whose JSON
    will not parse is skipped rather than taking the draft down with it."""
    novelty_dir = base / "novelty"
    if not novelty_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(novelty_dir.glob("*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def _write_adjudication_draft(base: Path, record: Mapping[str, Any]) -> None:
    """The adjudication draft: a DRAFT, in the round's directory, never a
    registered artifact. Registration is the orchestrator's under the
    registry rule, and a screen that registered its own adjudication would
    be registering a document about its own output.

    It is regenerated from every dossier ON FILE, not from the batch just
    screened. The mechanical half runs once per lens batch, so a draft built
    from one batch would silently drop the round's earlier records every
    time a new batch landed -- and the round's adjudication is supposed to
    be the round's."""
    dossiers = sorted(_all_dossiers_on_file(base), key=lambda d: d["idea_id"])
    lines = [
        f"# Adjudication draft - round {record['round_id']}, batch {record['batch_id']}",
        "",
        f"- screen version: `{record['screen_version']}`",
        f"- written: {record['ts']}",
        f"- external query mode: `{record['external_query_mode']}`",
        f"- reference snapshot: R1 `{record['reference_snapshot']['R1']['sha256'][:12]}` "
        f"R3 `{record['reference_snapshot']['R3']['sha256'][:12]}` "
        f"R4 `{record['reference_snapshot']['R4']['sha256'][:12]}`",
        f"- last batch: screened {record['n_screened']}, merged {record['n_merged']}",
        f"- dossiers on file for this round: {len(dossiers)}",
        "",
        "Distances are recorded, never thresholded into \"novel\". An idea with no close",
        "neighbour is labelled `no-close-neighbour, unjudged`, which is not `new-mechanism`.",
        "",
        "| idea | home | card | known mechanic | d_prov | d_home | tercile | R4 hits | R5 hits | judged |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for d in dossiers:
        known = d["known_mechanic"]["row_id"] if d["known_mechanic"] else "-"
        lines.append(
            "| {idea} | {home} | {card} | {known} | {d_prov} | {d_home} | {tercile} | {r4} | {r5} | {judged} |".format(
                idea=d["idea_id"], home=d.get("home") or "-", card=d.get("recipe_card") or "-",
                known=known, d_prov=d["distances"]["d_prov"], d_home=d["distances"]["d_home"],
                tercile=d["distances"]["tercile"] or "-", r4=len(d["candidate_hits"]["R4"]),
                r5=len(d["candidate_hits"]["R5"]), judged="yes" if d["judged"] else "no",
            )
        )
    lines += [
        "",
        "## Distribution audit",
        "",
        "```json",
        json.dumps(record["distribution"], indent=2, ensure_ascii=False, sort_keys=True),
        "```",
        "",
        f"Collapse: `{record['collapse']['mode']}`, flag `{record['collapse']['flag']}`.",
        "",
    ]
    (base / "adjudication.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# 3b: judged preparation
# ---------------------------------------------------------------------------

#: Sentences a record may make about its own novelty. Stripped from JUDGE
#: envelopes only -- never from the record, never from the feed post, which
#: stays full text.
_SELF_ASSESSMENT_RE = re.compile(
    r"[^.!?\n]*\b("
    r"novel|novelty|unlike any|unlike anything|first of its kind|unprecedented|"
    r"no (?:existing|prior|other) (?:system|work|approach|mechanic)|"
    r"never been (?:done|tried|proposed)|state of the art|breakthrough"
    r")\b[^.!?\n]*[.!?]?",
    re.IGNORECASE,
)


def strip_self_assessment(text: str) -> dict[str, Any]:
    """Remove sentences in which a record grades its own novelty, returning
    ``{"text", "removed", "mostly_self_assessment"}``.

    Stated plainly, because it matters: this is HYGIENE, not the defence.
    It is trivially paraphrased -- "no register row states this procedure"
    says the same thing and matches nothing here. The load-bearing defence
    is that the judge compares the record pairwise against retrieved rows
    and that a record which is mostly self-assessment is labelled
    ``unscreenable``. ``mostly_self_assessment`` is the flag that supports
    that label; it is reported, never applied automatically."""
    removed = [m.group(0).strip() for m in _SELF_ASSESSMENT_RE.finditer(text)]
    stripped = _SELF_ASSESSMENT_RE.sub(" ", text)
    stripped = re.sub(r"[ \t]+", " ", stripped)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped).strip()
    original_len = len(text.strip())
    return {
        "text": stripped,
        "removed": removed,
        "mostly_self_assessment": bool(original_len) and len(stripped) < original_len * 0.5,
    }


#: Record fields a novelty judge may see. Everything not on this list is
#: withheld, and the three that matter most are named in
#: :data:`WITHHELD_FROM_JUDGE` so the omission is asserted rather than
#: assumed.
JUDGE_VISIBLE_FIELDS: tuple[str, ...] = (
    "requirements", "statement", "home_mechanic", "probe", "extra_text",
)

#: What never enters a judge envelope. ``author_rationale`` and ``surprise``
#: are the author grading its own work; ``assumed_circle``, the seat, the
#: card and the lens name identify the author or its condition, and a judge
#: that can see the arm can judge the arm.
WITHHELD_FROM_JUDGE: tuple[str, ...] = (
    "author_rationale", "surprise", "assumed_circle", "seat", "recipe_card", "lens", "author_launch", "tier",
    # A plant's free class tag (lane FB-7 item 4). It is the ANSWER: a round
    # that classes its battery present/adjacent/absent and then shows the
    # class to the judge has stopped running a battery and started running a
    # quiz with the answers printed on it. No real record carries the key
    # either, so it would be a tell twice over.
    "class",
    # Which batch a plant was seeded into (lane FB-7 item 5) -- bookkeeping
    # for the round, and a field no real record has.
    "batch",
)


def _envelope(
    *,
    subject_id: str,
    record: Mapping[str, Any],
    statement: str,
    inventory_rows: Sequence[Mapping[str, Any]] = (),
    retrieved: Sequence[Mapping[str, Any]] = (),
    archive_rows: Sequence[Mapping[str, Any]] = (),
    judged_sets: Sequence[str] = DEFAULT_JUDGED_SETS,
    label_block: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The one envelope shape, built for real records and for plants by the
    same function.

    That is not tidiness. A plant a judge can pick out tests nothing, and
    every field a plant fills differently from a real record is a tell --
    so there is exactly one place where an envelope's keys are decided, and
    all three callers (records, harness plants, external plants) go through
    it with the same kinds of value.

    ``archive_rows`` (R2), ``inventory_rows`` (R3) and ``retrieved``
    (R4/R5) are separate keys because they are separate reference sets with
    separate label vocabularies, and the verdict rows written from the
    answer cite them separately.

    **The envelope carries exactly the DECLARED sets' keys and no other.**
    An undeclared set is absent, not empty: ``inventory_rows: []`` reads to
    a judge, and to every later reader of the batch file, as "the register
    was consulted and held nothing near this", which is a different
    sentence from "this round did not judge against the register". The
    ``labels`` block is filtered the same way, so a judge is never shown a
    vocabulary for a set it is not being asked about."""
    stripped = strip_self_assessment(statement)
    declared = normalize_judged_sets(judged_sets)
    rows_by_set: dict[str, Sequence[Mapping[str, Any]]] = {
        "R2": archive_rows, "R3": inventory_rows, "R4": retrieved,
    }
    envelope: dict[str, Any] = {
        "subject_id": subject_id,
        "kind": "novelty-pairwise",
        "procedure_version": PROCEDURE_VERSION,
        "record": {
            "requirements": record.get("requirements"),
            "statement": stripped["text"],
            "home_mechanic": record.get("home_mechanic"),
            "probe": record.get("probe"),
            "provenance_docs": list(record.get("provenance_docs") or []),
            # Lane FB-6 item 6. Always present, None when the subject has
            # none: the KEY is part of the envelope shape, so a plant that
            # carries text here is not pickable on its key set. A round that
            # seeds extra text on its plants and none on its records is
            # making the values a tell, which is the round's decision to
            # make and not something a shape can prevent.
            "extra_text": record.get("extra_text"),
        },
    }
    for name in declared:
        envelope[REFERENCE_SET_ROWS[name]] = [dict(r) for r in rows_by_set[name]]
    envelope["labels"] = (
        {REFERENCE_SET_LABEL_BLOCK[name]: list(label_block[name]) for name in declared}
        if label_block is not None
        else {REFERENCE_SET_LABEL_BLOCK[name]: list(REFERENCE_SET_VOCABULARIES[name]) for name in declared}
    )
    envelope["self_assessment_removed"] = stripped["removed"]
    envelope["mostly_self_assessment"] = stripped["mostly_self_assessment"]
    return envelope


def build_verifier_envelope(
    idea: Mapping[str, Any],
    *,
    retrieved: Sequence[Mapping[str, Any]] = (),
    inventory_rows: Sequence[Mapping[str, Any]] = (),
    archive_rows: Sequence[Mapping[str, Any]] = (),
    judged_sets: Sequence[str] = DEFAULT_JUDGED_SETS,
    label_block: Mapping[str, Any] | None = None,
    subject_id: str | None = None,
) -> dict[str, Any]:
    """One judged envelope: raw record fields plus the retrieved rows
    themselves, and nothing else.

    ``home_mechanic`` is the record's ``home``; ``statement`` is its body,
    run through :func:`strip_self_assessment` first. The envelope carries
    the two label vocabularies verbatim so a judge cannot invent a third,
    and it carries no field from :data:`WITHHELD_FROM_JUDGE`.

    **Both reference sets travel with the record.** ``inventory_rows`` is
    R3 -- the nearest register rows, with their text -- and ``retrieved`` is
    the R4/R5 bundle. Design 5.2(1) names the pairwise comparison against
    retrieved rows as the load-bearing defence (the self-assessment stripper
    is hygiene and is trivially paraphrased), so an envelope carrying ids
    and similarities but no rows would ask a judge to answer "does a
    register row already state this" out of its own memory -- which is the
    one thing this whole procedure exists not to rely on.

    ``extra_text`` (lane FB-7 item 8b) is the record's own free block,
    rendered by :func:`render_extra_text` -- the SAME function a plant's is
    rendered by, and into the same one envelope field. That is not tidiness
    either: a plant carrying a key no record has is pickable on its shape
    alone, and the converse is just as true."""
    return _envelope(
        subject_id=subject_id or str(idea.get("idea_id")),
        record={
            "requirements": idea.get("requirements"),
            "home_mechanic": idea.get("home"),
            "probe": idea.get("probe"),
            "provenance_docs": _provenance_docs(idea),
            "extra_text": render_extra_text(_decode_extra(idea.get("extra"))),
        },
        statement=_statement(idea),
        inventory_rows=inventory_rows,
        retrieved=retrieved,
        archive_rows=archive_rows,
        judged_sets=judged_sets,
        label_block=label_block,
    )


def select_judged_scope(
    dossiers: Mapping[str, Mapping[str, Any]], *, seed: str, sample_fraction: float = JUDGED_SAMPLE_FRACTION
) -> dict[str, Any]:
    """Which ideas a judge sees: every flagged idea, every idea with a
    retrieval hit, and a seeded random share of the remainder.

    The first two are mandatory because they are exactly the ideas whose
    dossier says "something close to this already exists" -- an unexamined
    flag would be the screen deciding a question it is not allowed to
    decide. The third exists so the label distribution is not conditioned
    entirely on what retrieval happened to surface: without it, every
    reported rate would be a rate among ideas retrieval already suspected.
    The remainder carries ``no-close-neighbour, unjudged``."""
    flagged, hits, rest = [], [], []
    for idea_id in sorted(dossiers):
        dossier = dossiers[idea_id]
        if dossier.get("known_mechanic"):
            flagged.append(idea_id)
        elif dossier["candidate_hits"]["R4"] or dossier["candidate_hits"]["R5"]:
            hits.append(idea_id)
        else:
            rest.append(idea_id)
    rng = derive_rng(seed, salt="judged-scope")
    n_sample = int(math.ceil(len(rest) * sample_fraction)) if rest else 0
    sampled = sorted(rng.sample(rest, n_sample)) if n_sample else []
    return {
        "flagged": flagged,
        "retrieval_hits": hits,
        "sampled": sampled,
        "scope": sorted(set(flagged) | set(hits) | set(sampled)),
        "unjudged": sorted(set(rest) - set(sampled)),
        "sample_fraction": sample_fraction,
        "seed": seed,
    }


#: The register's own row-id pattern: ``<source>-M<nnn>`` -- a source token,
#: a literal ``M`` and a row number. A register table's first cell carries
#: one of these on every MECHANIC row and on nothing else, which is what
#: makes the pattern usable as a filter rather than as a convention.
MECHANIC_ROW_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.]*-M\d{2,}"
_MECHANIC_ROW_ID_RE = re.compile(rf"^{MECHANIC_ROW_ID_PATTERN}$")

#: Header cells (lowercased) that name the column a mechanic row's STATEMENT
#: lives in. A register whose table says so in its header is read from the
#: header; one that does not falls back to the positional rule
#: :func:`parse_mechanic_rows` documents.
_DESCRIPTION_HEADERS: frozenset[str] = frozenset(
    {"description", "what it does", "statement", "mechanic", "rule", "effect"}
)


def _table_cells(line: str) -> list[str] | None:
    """The cells of one pipe-delimited table row, or ``None`` when the line
    is not one (or is a separator rule like ``|---|---|``)."""
    stripped = line.strip()
    if stripped.count("|") < 2:
        return None
    if not stripped.startswith("|"):
        return None
    cells = [c.strip() for c in stripped.strip("|").split("|")]
    if len(cells) < 2:
        return None
    if all(set(c) <= set("-: ") for c in cells):
        return None
    return cells


def _table_blocks(lines: Sequence[str]) -> list[list[list[str]]]:
    """The chunk's pipe tables, as blocks of parsed rows.

    A block is a maximal run of table lines (separator rules included, and
    dropped): any prose line, heading or blank line ends it. Two tables
    printed one after the other with no blank line between them are one
    block by this rule, which is the conservative reading -- the column
    positions of a run of adjacent rows are the only thing a header in that
    run can be speaking about."""
    blocks: list[list[list[str]]] = []
    current: list[list[str]] = []
    for line in lines:
        stripped = line.strip()
        is_table_line = stripped.startswith("|") and stripped.count("|") >= 2
        if not is_table_line:
            if current:
                blocks.append(current)
                current = []
            continue
        cells = _table_cells(line)
        if cells is not None:
            current.append(cells)
    if current:
        blocks.append(current)
    return blocks


def parse_mechanic_rows(text: str | None) -> list[dict[str, Any]]:
    """The MECHANIC ROWS a chunk of register text states, as
    ``[{"row_id", "name", "statement"}, ...]``.

    A plant candidate is a mechanic row, never an arbitrary chunk of the
    reference set. The distinction is the whole value of an inventory plant:
    the plant asks a judge "does a register row already state this", the
    only correct answers are ``same``/``variant``, and a miss fails the
    batch. A chunk that states no mechanism -- a citation-convention note,
    a coverage-gap list, a heading -- has no such answer, so a judge doing
    the task honestly returns ``unscreenable``, the scorer counts a missed
    inventory plant, and the batch fails for the battery's own sampling
    rather than for the judge's labels. That is a false alarm the audit
    cannot tell apart from a real one, which is why the filter is here and
    not in the prompt that describes it.

    A row qualifies when its FIRST cell matches :data:`MECHANIC_ROW_ID_PATTERN`
    and its description cell is non-empty. Which cell that is: the column a
    header row names (:data:`_DESCRIPTION_HEADERS`) **inside the M-row's own
    contiguous table block**, else the third cell (``id | name | description
    | ...``), else the second when the row has exactly two (``id |
    description``). ``name`` is optional and is reported separately -- the
    plant statement is the description, so a register that numbers and names
    its rows does not feed the judge a bare label to compare against.

    The header search is scoped to the block (fix pass N-1). A chunk that
    carries a coverage or summary table AHEAD of the register table -- the
    mixed chunk this filter exists for -- has a ``description`` column of its
    own at another position, and reading it as the register's turned the
    plant statement into the register row's two-word NAME: a bare label, the
    very shape the filter exists to prevent.
    """
    lines = str(text or "").splitlines()
    out: list[dict[str, Any]] = []
    for block in _table_blocks(lines):
        description_col: int | None = None
        name_col: int | None = None
        for cells in block:
            if _MECHANIC_ROW_ID_RE.match(cells[0]):
                continue
            lowered = [c.strip().lower() for c in cells]
            for i, cell in enumerate(lowered):
                if i and cell in _DESCRIPTION_HEADERS and description_col is None:
                    description_col = i
                    name_col = 1 if i > 1 else None
            if description_col is not None:
                break

        for cells in block:
            if not _MECHANIC_ROW_ID_RE.match(cells[0]):
                continue
            if description_col is not None and description_col < len(cells):
                col = description_col
                name_at = name_col
            elif len(cells) >= 3:
                col, name_at = 2, 1
            else:
                col, name_at = 1, None
            statement = cells[col].strip()
            if not statement:
                continue
            out.append(
                {
                    "row_id": cells[0],
                    "name": (cells[name_at].strip() or None) if name_at is not None and name_at < len(cells) else None,
                    "statement": statement,
                }
            )
    return out


def mechanic_row_candidates(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every mechanic row of every R3 chunk in ``rows``, flattened, each
    carrying the chunk it came from.

    ``row_id`` is the CHUNK (what the bundle ranks and what ``must_include``
    forces in); ``source_row_id`` is the M-row id inside it. Both travel on
    the plant, because "which row was planted" and "which chunk the judge
    was handed" are different questions and the audit asks both."""
    out: list[dict[str, Any]] = []
    for row in rows:
        for mech in parse_mechanic_rows(row.get("text")):
            out.append(
                {
                    "source_row_id": mech["row_id"],
                    "name": mech["name"],
                    "statement": mech["statement"],
                    "row_id": row["row_id"],
                    "doc_id": row.get("doc_id"),
                    "family": row.get("family"),
                    "license_tier": row.get("license_tier"),
                }
            )
    return out


#: How a paraphrase plant is written. ``deterministic`` is the only
#: implemented backend and the default; ``llm`` is DECLARED and refused, so
#: an operator reaching for it is told it does not exist rather than
#: discovering a silent fallback. A battery whose plants an LLM rewrote
#: would make the audit depend on the class of system it audits.
PARAPHRASE_BACKENDS: tuple[str, ...] = ("deterministic", "llm")
DEFAULT_PARAPHRASE_BACKEND = "deterministic"


#: Frames a planted statement may be written in, drawn seeded and shared by
#: BOTH plant kinds.
#:
#: The battery's whole value is that a judge cannot tell a plant from a
#: record, and the previous version gave itself away twice over: inventory
#: plants always opened "Proposal: " and paraphrase plants always
#: "Restated: ", so a two-prefix rule recovered every plant from a shuffled
#: batch. A judge that learns the tell passes the audit without doing the
#: task, and the audit is what makes a batch fail closed -- so the tell is
#: not a cosmetic defect.
#:
#: The bare ``"{text}"`` frame is in the pool deliberately: a plant with no
#: frame at all is the shape a real record has, and any rule keyed on the
#: presence of a frame is wrong about those. Frames stay shallow on purpose
#: -- a plant rewritten until it no longer resembles its row would test
#: nothing, and one an LLM rewrote would make the battery depend on the
#: class of system it audits.
_PLANT_FRAMES: tuple[str, ...] = (
    "{text}",
    "{text}",
    "{text} The change is stated as one state transition.",
    "{text} Bookkeeping stays where it already is.",
    "In the shared state: {text}",
    "{text} The rule reads the same at every table size.",
)


def _plant_statement(rng: Any, text: str) -> str:
    frame = rng.choice(_PLANT_FRAMES)
    body = str(text or "").strip()
    if frame.startswith("In the shared state:") and body:
        body = body[0].lower() + body[1:]
    return frame.format(text=body).strip()


#: The frames a PARAPHRASE plant may be written in: :data:`_PLANT_FRAMES`
#: minus the identity frame. A paraphrase plant that carried its donor's
#: statement byte-for-byte tested nothing at all -- it asked the judge
#: whether a record is the same as itself, and a judge that answered
#: `new-mechanism` was being asked a question about an identity, not about a
#: paraphrase. The frame is only half of the answer; see
#: :func:`_paraphrase_statement` for the other half.
_PARAPHRASE_FRAMES: tuple[str, ...] = tuple(f for f in _PLANT_FRAMES if f.strip() != "{text}")

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

#: Connectives a clause may be fronted around, most meaning-preserving
#: first. Every entry either introduces a subordinate clause (English fronts
#: these with a comma without changing what the sentence says: "X unless Y"
#: -> "Unless Y, X") or heads a prepositional phrase that fronts the same
#: way. Coordinating "and"/"but" are deliberately absent: swapping their
#: sides reorders two coordinate facts, which is a different statement, not
#: a paraphrase of one.
_FRONTABLE_CONNECTIVES: tuple[str, ...] = (
    " unless ", " until ", " whenever ", " when ", " because ", " while ",
    " after ", " before ", " if ", " so that ", " rather than ",
    " at the ", " on the ", " in the ", " for each ", " for every ",
    " by ", " to ", " from ", " into ", " with ", " for ", " at ", " on ",
    # A complement PP headed by a wh-clause fronts cleanly ("Of what each
    # seat believes, the model gains a representation"); a bare " of " does
    # not, and is deliberately absent.
    " of what ", " of how ", " of whether ", " of which ", " of when ",
)

#: Coordinating connectives a sentence may be SPLIT at, and what the second
#: sentence opens with once it is. Coordination is the one place two
#: sentences say exactly what one said, so this is a reshaping rather than a
#: reordering -- ``", and "`` drops the conjunction entirely (two coordinate
#: facts, still both stated, still in order), the others keep theirs as a
#: sentence-initial conjunction because dropping "so"/"but" would drop the
#: relation between the halves.
_SPLITTABLE_CONNECTIVES: tuple[tuple[str, str], ...] = (
    (", and then ", "Then "),
    (", and ", ""),
    (", but ", "But "),
    (", so ", "So "),
    (", then ", "Then "),
)

#: Letters a renamed symbol may be renamed to. Drawn seeded, minus whatever
#: the statement already uses, so a rename never collides with a symbol the
#: statement gave a different meaning.
_SYMBOL_POOL: tuple[str, ...] = tuple("GHJKLMNPQRSTUVWXYZ")

#: Single capitals that are words, not symbols.
_NOT_SYMBOLS: frozenset[str] = frozenset({"A", "I"})

_SYMBOL_RE = re.compile(r"\b([A-Z])\b")

#: First words that may be lowercased when a clause is moved out of sentence
#: position. Anything else keeps its capital: this module cannot tell a
#: proper noun from a sentence-initial capital, and guessing wrong would
#: rewrite a name rather than a case.
_LOWERCASEABLE_HEADS: frozenset[str] = frozenset(
    {
        # determiners and quantifiers
        "a", "an", "the", "this", "that", "these", "those", "each", "every", "one", "two", "three",
        "four", "five", "no", "any", "all", "both", "either", "neither", "some", "most", "few",
        # pronouns and possessives
        "it", "its", "they", "their", "his", "her", "he", "she", "we", "our", "you", "your",
        # prepositions and conjunctions a fronted head can begin with
        "at", "on", "in", "by", "for", "from", "to", "with", "within", "without", "after",
        "before", "during", "under", "over", "across", "per", "when", "where", "while", "if",
        "unless", "until", "whenever", "because", "once", "so", "rather", "instead", "nothing",
        "nobody", "whoever", "whatever", "what", "how", "not", "only",
        # common nouns of the register itself, which are never proper nouns
        # here. Anything NOT listed keeps its capital: a wrongly lowercased
        # name would be a content change, a capitalised common noun is a
        # blemish, and the two are not equally bad.
        "bookkeeping", "initiative", "information", "territory", "turn", "control",
        "players", "seats", "scoring", "state", "order", "play", "score",
    }
)


def _sentences(text: str) -> list[str]:
    return [part for part in _SENTENCE_SPLIT_RE.split(str(text or "").strip()) if part]


def _permute_sentences(rng: Any, sentences: Sequence[str]) -> str | None:
    """A seeded NON-identity rotation of a multi-sentence statement.

    A rotation rather than a shuffle because a shuffle can draw the identity
    permutation, and "the paraphrase is sometimes the donor" is the defect
    this whole function exists to close."""
    if len(sentences) < 2:
        return None
    shift = rng.randrange(1, len(sentences))
    return " ".join([*sentences[shift:], *sentences[:shift]])


def _lower_head(text: str) -> str:
    """``text`` with its first word lowercased when that word is one this
    module can be sure was capitalised only by sentence position."""
    first = text.split(" ", 1)[0].strip(",.;:")
    if first.lower() in _LOWERCASEABLE_HEADS and first[:1].isupper():
        return text[0].lower() + text[1:]
    return text


def _front_clause(text: str) -> str | None:
    """One sentence with its subordinate clause (or leading prepositional
    phrase) moved to the front, or ``None`` when it has none to move."""
    body = str(text or "").strip()
    if not body:
        return None
    end = ""
    if body[-1] in ".!?":
        body, end = body[:-1], body[-1]
    lowered = body.lower()
    for connective in _FRONTABLE_CONNECTIVES:
        idx = lowered.find(connective)
        if idx <= 0:
            continue
        head = body[:idx].strip().rstrip(",")
        tail = body[idx + 1:].strip()
        if len(head.split()) < 2 or len(tail.split()) < 3:
            continue
        fronted = tail[0].upper() + tail[1:]
        return f"{fronted}, {_lower_head(head)}{end}"
    return None


def _split_clause(text: str) -> tuple[str, list[str]] | None:
    """One coordinated sentence restated as two, and the tokens the split
    dropped -- or ``None`` when the sentence coordinates nothing.

    The dropped tokens are REPORTED rather than assumed harmless: dropping
    ``and`` between two coordinate facts states the same two facts, but the
    only way a reader can check that is if the method says what went."""
    body = str(text or "").strip()
    if not body:
        return None
    end = ""
    if body[-1] in ".!?":
        body, end = body[:-1], body[-1]
    lowered = body.lower()
    for connective, opener in _SPLITTABLE_CONNECTIVES:
        idx = lowered.find(connective)
        if idx <= 0:
            continue
        head = body[:idx].strip()
        tail = body[idx + len(connective):].strip()
        if len(head.split()) < 3 or len(tail.split()) < 3:
            continue
        second = f"{opener}{tail}" if opener else tail[0].upper() + tail[1:]
        dropped = [w for w in connective.strip(" ,").split() if w not in opener.lower()]
        return f"{head}. {second}{end or '.'}", dropped
    return None


def _front_tail_after_last_comma(text: str) -> str | None:
    """The material after a sentence's LAST comma moved to the front.

    The lowest-priority reordering, and the one that catches the trailing
    adjunct no connective in the two tables above names ("X, not only Y." ->
    "Not only Y, X."). Both sides must be substantial, so a parenthetical
    two-word tail is left where it is."""
    body = str(text or "").strip()
    if not body:
        return None
    end = ""
    if body[-1] in ".!?":
        body, end = body[:-1], body[-1]
    idx = body.rfind(", ")
    if idx <= 0:
        return None
    head, tail = body[:idx].strip(), body[idx + 2:].strip()
    if len(head.split()) < 3 or len(tail.split()) < 3:
        return None
    return f"{tail[0].upper() + tail[1:]}, {_lower_head(head)}{end}"


def _rename_symbols(rng: Any, text: str) -> tuple[str, dict[str, str]] | None:
    """Every single-letter symbol renamed consistently, or ``None`` when the
    statement uses none. ``A`` and ``I`` are words, not symbols."""
    used = {m for m in _SYMBOL_RE.findall(text or "") if m not in _NOT_SYMBOLS}
    if not used:
        return None
    pool = [c for c in _SYMBOL_POOL if c not in used and c not in str(text or "").upper()]
    if len(pool) < len(used):
        pool = [c for c in _SYMBOL_POOL if c not in used]
    if len(pool) < len(used):
        return None
    chosen = rng.sample(pool, len(used))
    mapping = {old: new for old, new in zip(sorted(used), chosen)}
    renamed = _SYMBOL_RE.sub(lambda m: mapping.get(m.group(1), m.group(1)), text)
    return renamed, mapping


def _paraphrase_statement(rng: Any, text: str) -> tuple[str, dict[str, Any]]:
    """A paraphrase plant's statement: a NON-identity frame plus at least
    one structural transformation, and never the donor's own bytes.

    Observed on a live batch: two paraphrase plants carried the donor
    statement verbatim, because the shared frame pool contains the identity
    frame and nothing else touched the text. Such a plant asks a judge
    whether a statement is the same as itself; it cannot catch a judge that
    has drifted, and a batch whose paraphrase half is identities is
    reporting a catch rate about a question nobody asked.

    The transformations are deterministic under the batch seed and lossless:
    sentence order is rotated (never shuffled -- a shuffle can draw the
    identity), a single sentence has its subordinate clause or leading
    prepositional phrase fronted, and single-letter symbols are renamed
    consistently. Every number and every word the donor used survives, with
    the renamed symbols named in ``symbol_map`` and the coordinating
    conjunction a split consumed named in ``dropped_tokens`` -- both
    auditable rather than silent losses. Nothing here rewrites the mechanism: a plant
    rewritten until it no longer resembles its donor would test nothing, and
    one an LLM rewrote would make the battery depend on the class of system
    it audits."""
    donor = str(text or "").strip()
    transformations: list[str] = []
    body = donor
    symbol_map: dict[str, str] = {}
    dropped: list[str] = []

    sentences = _sentences(body)
    permuted = _permute_sentences(rng, sentences)
    if permuted is not None:
        body = permuted
        transformations.append("sentence_order")
    else:
        split = _split_clause(body)
        if split is not None:
            body, dropped = split
            transformations.append("clause_split")
        else:
            fronted = _front_clause(body)
            if fronted is not None:
                body = fronted
                transformations.append("clause_fronting")
            else:
                tail_fronted = _front_tail_after_last_comma(body)
                if tail_fronted is not None:
                    body = tail_fronted
                    transformations.append("tail_fronting")

    renamed = _rename_symbols(rng, body)
    if renamed is not None:
        body, symbol_map = renamed
        transformations.append("symbol_rename")

    frame = rng.choice(_PARAPHRASE_FRAMES)
    if frame.startswith("In the shared state:") and body:
        body = body[0].lower() + body[1:]
    # A frame that appends a sentence to a body with no terminal punctuation
    # runs the two together, which is a surface difference between a plant
    # and a record -- and the one thing a plant may not have.
    if not frame.endswith("{text}") and body and body[-1] not in ".!?":
        body = f"{body}."
    statement = frame.format(text=body).strip()
    if statement == donor:
        raise NoveltyError(
            "plant battery: a paraphrase plant came out byte-identical to its donor, which asks a judge "
            "whether a record is the same as itself and catches nothing. This is a defect in the "
            "paraphrase transformations, not a property of the donor"
        )
    return statement, {
        "frame": frame,
        "transformations": transformations,
        "symbol_map": symbol_map,
        "dropped_tokens": dropped,
    }


def _donor_fields(
    store: Store, rng: Any, dossiers: Mapping[str, Mapping[str, Any]], *, family: str | None
) -> dict[str, Any]:
    """The non-statement fields a plant wears, taken from a real record in
    this same batch -- its home cell, ``requirements``, ``probe`` and
    provenance documents.

    A plant is a synthetic record; the honest question is not "what did it
    really come from" (the batch's ``source_ref`` answers that, and the
    judge is not entitled to it) but "does it look like the others". The
    previous version answered no on three counts a judge could read
    directly: a plant's ``home_mechanic`` was a bare family where a real
    record's is family/cell, its provenance pointed at an inventory
    document no lens ever cites, and its ``requirements``/``probe`` were two
    fixed strings. Borrowing a donor's fields removes all three at once.

    ``family`` prefers a donor in the plant's own family so the home cell
    still means something; any donor otherwise."""
    candidates = list(dossiers.values())
    if family:
        same = [d for d in candidates if d.get("home_family") == family]
        if same:
            candidates = same
    if not candidates:
        return {"home_mechanic": family or None, "requirements": None, "probe": None, "provenance_docs": []}
    donor = rng.choice(sorted(candidates, key=lambda d: str(d["idea_id"])))
    record = read_idea(store, idea_id=donor["idea_id"]) or {}
    return {
        "idea_id": donor["idea_id"],
        "home_mechanic": record.get("home") or donor.get("home"),
        "requirements": record.get("requirements"),
        "probe": record.get("probe"),
        "provenance_docs": _provenance_docs(record),
    }


#: The kinds a plant may declare itself. ``inventory`` and ``paraphrase``
#: are what the harness battery builds; ``area`` (a rewrite of a reference
#: row that is not a register mechanic -- a prior round's request row, a
#: coverage note) and ``custom`` exist for the plants a ROUND defines in a
#: ``--plants-file``, and are named rather than free text so a per-kind
#: catch rate is a table a reader can compare across rounds.
PLANT_KINDS: tuple[str, ...] = ("area", "paraphrase", "inventory", "custom")

#: Whose misses FAIL a batch, when a round says nothing: exactly today's
#: rule. An inventory plant IS an existing row wearing an idea's clothes, so
#: a judge that calls one new cannot be trusted on the rows it was not
#: handed. Paraphrase detection is the harder task and the design does not
#: stake the batch on it.
DEFAULT_BATCH_FAIL_ON: tuple[str, ...] = ("inventory",)

#: What an external plant declaration may carry.
EXTERNAL_PLANT_FIELDS: tuple[str, ...] = (
    "plant_id", "kind", "statement", "expected_labels", "donor_ref", "source_ref",
    "requirements", "home_mechanic", "probe", "extra", "class", "batch",
)

#: How many offending plant ids a kind refusal prints before it says "and N
#: more". A file with forty plants and one wrong word in every one of them
#: should not print forty lines, and a refusal that prints ONE of them (what
#: this used to do) makes a reader fix one line and run again, forty times.
KIND_REFUSAL_CAP = 10

#: Longest a plant's free ``class`` tag may be. Forty characters is a label
#: ("present", "adjacent-mechanism", "absent-from-corpus"), not a sentence;
#: it groups a table on the calibration card, and a group key that is a
#: paragraph makes that table unreadable.
PLANT_CLASS_MAX_CHARS = 40


def render_extra_text(extra: Any) -> str | None:
    """A plant declaration's ``extra`` block as the ONE text field a judge
    sees (lane FB-6 item 6).

    A round writes its plants by hand, and a live one wanted ``literature``,
    ``unlock`` and ``seeds`` on each of them -- the same three keys its own
    records carry. The plants file refused every one, so the text had to be
    folded into the statement by hand, in a different place for each plant.

    ``extra`` takes any keys at all, and they are rendered here into a
    single ``key: value`` block in sorted key order. One text field rather
    than one envelope field per key, because the envelope's shape is what
    keeps a plant indistinguishable from a record: a plant carrying three
    keys no record has would be pickable on its shape alone.

    Empty values are dropped rather than rendered as ``key:`` -- a key the
    author left blank says nothing, and a judge reading ``unlock:`` with
    nothing after it is being told something false about the record."""
    if extra is None:
        return None
    if isinstance(extra, str):
        return extra.strip() or None
    if not isinstance(extra, Mapping):
        raise NoveltyError(
            f"extra must be an object of the round's own keys (or a plain string), got "
            f"{type(extra).__name__}"
        )
    parts: list[str] = []
    for key in sorted(extra):
        value = extra[key]
        if value is None:
            continue
        if isinstance(value, str):
            rendered = value.strip()
        elif isinstance(value, (list, tuple)):
            rendered = "; ".join(str(v).strip() for v in value if str(v).strip())
        elif isinstance(value, Mapping):
            rendered = json.dumps(value, sort_keys=True, ensure_ascii=False)
        else:
            rendered = str(value).strip()
        if rendered:
            parts.append(f"{key}: {rendered}")
    return "\n".join(parts) or None


def normalize_fail_kinds(kinds: Any) -> tuple[str, ...]:
    """The plant kinds whose misses fail a batch, validated.

    ``None`` is :data:`DEFAULT_BATCH_FAIL_ON`. An empty declaration is a
    refusal, not "nothing fails it": a battery that cannot fail the batch it
    polices reports a catch rate about a question nobody acts on, which is
    the one shape :func:`score_plants` exists to call ``unauditable``."""
    if kinds is None:
        return DEFAULT_BATCH_FAIL_ON
    if isinstance(kinds, str):
        kinds = [part.strip() for part in kinds.split(",") if part.strip()]
    names = [str(kind).strip() for kind in kinds]
    unknown = [kind for kind in names if kind not in PLANT_KINDS]
    if unknown:
        raise NoveltyError(
            f"batch_fail_on names plant kind(s) {unknown!r} this screen does not know; the kinds are "
            f"{list(PLANT_KINDS)!r}"
        )
    if not names:
        raise NoveltyError(
            "batch_fail_on is empty: a battery whose misses can never fail the batch reports a catch rate "
            "about a question nobody acts on. Name at least one kind, or lower --plants to 0 and say "
            "outright that this batch is unaudited"
        )
    return tuple(kind for kind in PLANT_KINDS if kind in set(names))


def load_external_plants(
    declared: Any,
    *,
    judged_sets: Sequence[str] = DEFAULT_JUDGED_SETS,
    label_vocabularies: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The plants a ROUND declares, validated -- statements and expected
    labels only; the donor fields and the reference bundle are filled in
    later by :func:`build_external_plants`.

    The harness battery can only build what it can derive: register mechanic
    rows and paraphrases of the batch's own records. A round that judges
    against its archive wants plants it defines -- a rewritten request row,
    a paraphrase of a prior round's candidate -- and there was no way to say
    so. Each declaration::

        {"plant_id": "P-1", "kind": "area", "statement": "...",
         "expected_labels": {"R2": ["requested"], "R4": ["present"]},
         "donor_ref": "IDEA-...", "source_ref": "IDEA-...",
         "requirements": "...", "home_mechanic": "...", "probe": "..."}

    ``expected_labels`` is a mapping per reference set, and every set it
    names must be one the round DECLARED and every label one the round's
    judge is actually offered -- both refusals by name. A plant with no
    expectation for any declared set is refused too: it could never be
    scored, so it would ride in the batch as a record the judge's answers
    about are thrown away."""
    wanted = normalize_judged_sets(judged_sets)
    if isinstance(declared, Mapping):
        declared = declared.get("plants", declared.get("records"))
    if not isinstance(declared, (list, tuple)):
        raise NoveltyError(
            "plants file: must hold a JSON list of plant declarations (or an object with a 'plants' "
            f"list), got {type(declared).__name__}"
        )
    if not declared:
        raise NoveltyError("plants file: holds no plants")

    # --- kind, over the WHOLE file first (lane FB-7 item 4) --------------
    # A live plants file spelled `kind` as the plant's CLASS -- present,
    # adjacent, absent -- and was refused one plant at a time, so the author
    # fixed one line and ran again, twelve times, before the twelfth refusal
    # finally said something about the thirteenth. The whole file is checked
    # here, before anything else is validated, and the refusal names every
    # offender at once and where the class actually goes.
    offenders = [
        (str(raw.get("plant_id") or "").strip() or f"(plant {index})", str(raw.get("kind") or "").strip())
        for index, raw in enumerate(declared)
        if isinstance(raw, Mapping) and str(raw.get("kind") or "").strip() not in PLANT_KINDS
    ]
    if offenders:
        shown = ", ".join(f"{pid} (kind={kind!r})" for pid, kind in offenders[:KIND_REFUSAL_CAP])
        more = (
            f" ... and {len(offenders) - KIND_REFUSAL_CAP} more"
            if len(offenders) > KIND_REFUSAL_CAP else ""
        )
        raise NoveltyError(
            f"plants file: {len(offenders)} plant(s) declare a kind that is not one of "
            f"{list(PLANT_KINDS)!r}: {shown}{more}. 'kind' says how the plant was BUILT -- those four "
            "words are the harness's construction vocabulary and nothing else may be spelled in them. A "
            "round's OWN class for a plant (a present/adjacent/absent battery, say) goes in the optional "
            f"'class' key: free text up to {PLANT_CLASS_MAX_CHARS} characters, which groups the "
            "calibration card's by_class table and is never shown to a judge"
        )

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(declared):
        where = f"plants file: plant {index}"
        if not isinstance(raw, Mapping):
            raise NoveltyError(f"{where} is a {type(raw).__name__}, not an object")
        unknown = sorted(set(raw) - set(EXTERNAL_PLANT_FIELDS))
        if unknown:
            raise NoveltyError(
                f"{where} carries unknown field(s) {unknown!r}; a plant declares "
                f"{list(EXTERNAL_PLANT_FIELDS)!r}. A round's OWN fields go under 'extra', which takes any "
                "keys and reaches the judge as one text block -- a top-level key stays a refusal so a "
                "misspelt 'statement' is not silently accepted as a note"
            )
        plant_id = str(raw.get("plant_id") or "").strip()
        if not plant_id:
            raise NoveltyError(f"{where} has no plant_id, which is how its labels are scored back to it")
        if plant_id in seen:
            raise NoveltyError(f"{where} repeats plant_id {plant_id!r}; two plants with one id cannot be scored apart")
        seen.add(plant_id)
        kind = str(raw.get("kind") or "").strip()  # already checked over the whole file, above
        plant_class = raw.get("class")
        if plant_class is not None:
            plant_class = str(plant_class).strip()
            if not plant_class:
                plant_class = None
            elif len(plant_class) > PLANT_CLASS_MAX_CHARS:
                raise NoveltyError(
                    f"{where} ({plant_id}) declares a class of {len(plant_class)} characters; a class is "
                    f"a label of at most {PLANT_CLASS_MAX_CHARS} (it becomes a group key on the "
                    "calibration card). Put the reasoning in 'extra' instead, which the judge does see"
                )
        statement = str(raw.get("statement") or "").strip()
        if not statement:
            raise NoveltyError(f"{where} ({plant_id}) has no statement -- a plant with no text tests nothing")
        expected = raw.get("expected_labels")
        if not isinstance(expected, Mapping) or not expected:
            raise NoveltyError(
                f"{where} ({plant_id}) must declare expected_labels per reference set, e.g. "
                '{"R2": ["requested", "variant"]}'
            )
        normalized: dict[str, list[str]] = {}
        for name, values in expected.items():
            reference_set = str(name).strip().upper()
            if reference_set not in wanted:
                raise NoveltyError(
                    f"{where} ({plant_id}) expects labels for reference set {reference_set!r}, which this "
                    f"round did not declare (it declared {list(wanted)!r}). The judge is never shown that "
                    "set, so the expectation could not be met or missed"
                )
            if isinstance(values, str):
                values = [values]
            if not isinstance(values, (list, tuple)) or not values:
                raise NoveltyError(
                    f"{where} ({plant_id}) expected_labels[{reference_set}] must be a non-empty list of labels"
                )
            vocabulary = (
                ((label_vocabularies.get("sets") or {}).get(reference_set) or {}).get("labels")
                if label_vocabularies
                else None
            ) or list(REFERENCE_SET_VOCABULARIES[reference_set])
            labels = [str(value).strip() for value in values]
            outside = [label for label in labels if label not in vocabulary]
            if outside:
                raise NoveltyError(
                    f"{where} ({plant_id}) expects {outside!r} for {reference_set}, which is not in the "
                    f"vocabulary this round's judge is offered ({list(vocabulary)!r}). An expectation a "
                    "judge cannot return is a plant that always misses"
                )
            normalized[reference_set] = labels
        if not normalized:
            raise NoveltyError(
                f"{where} ({plant_id}) expects nothing for any declared set, so no answer about it could "
                "ever be scored"
            )
        try:
            extra_text = render_extra_text(raw.get("extra"))
        except NoveltyError as exc:
            raise NoveltyError(f"{where} ({plant_id}): {exc}") from exc
        out.append(
            {
                "plant_id": plant_id,
                "kind": kind,
                "statement": statement,
                "expected_labels": {name: normalized[name] for name in wanted if name in normalized},
                "donor_ref": raw.get("donor_ref"),
                "source_ref": raw.get("source_ref"),
                "requirements": raw.get("requirements"),
                "home_mechanic": raw.get("home_mechanic"),
                "probe": raw.get("probe"),
                "extra": dict(raw["extra"]) if isinstance(raw.get("extra"), Mapping) else raw.get("extra"),
                "extra_text": extra_text,
                "class": plant_class,
                "batch": raw.get("batch"),
            }
        )
    return out


def plants_for_batch(
    declarations: Sequence[Mapping[str, Any]], *, batch_id: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """``(declarations for THIS batch, warnings)``. Lane FB-7 item 5.

    ``--plants-file`` takes one file, so a round that seeds different plants
    into each judged batch had to split the file by hand and keep the
    splits in step with the batch ids -- which is exactly the bookkeeping a
    batch id exists to do.

    A plant may now declare ``batch``. One whose ``batch`` equals this
    batch's id is injected here and nowhere else; one that declares NO
    batch is injected into every batch, which is what every plants file did
    before this key existed and therefore what an unchanged file keeps
    doing. Compared as STRIPPED strings, so ``2``, ``"2"`` and ``" 2 "`` are
    one batch -- a JSON file written by hand should not turn on that, and a
    value that is only whitespace reads as no batch at all.

    The warning is the load-bearing half. A ``--batch-id`` that matches no
    plant's ``batch`` injects none of the batched ones, and silently
    building a battery out of only the unbatched plants is how a round
    judges a batch it thinks it seeded and did not. It says which batch was
    asked for and which ones the file actually declares."""
    # Stripped ONCE, and the stripped value is what is compared (lane FB-7
    # fix pass, V-11). The first build stripped to decide "unbatched" and
    # then matched unstripped, so a plant declaring `"batch": " judged-1 "`
    # counted as batched -- excluded from every other batch -- and matched
    # no --batch-id at all, landing nowhere. A hand-written JSON file should
    # not turn on trailing whitespace in either direction.
    wanted = str(batch_id).strip()
    keep: list[dict[str, Any]] = []
    batched = 0
    declared_batches: set[str] = set()
    for declaration in declarations:
        value = declaration.get("batch")
        name = "" if value is None else str(value).strip()
        if not name:
            keep.append(dict(declaration))
            continue
        batched += 1
        declared_batches.add(name)
        if name == wanted:
            keep.append(dict(declaration))
    warnings: list[str] = []
    if batched and wanted not in declared_batches:
        warnings.append(
            f"plants file: no plant declares batch {wanted!r}; {batched} plant(s) are batched and "
            f"declare {sorted(declared_batches)!r}, so none of them was injected into this batch. "
            f"{len(keep)} unbatched plant(s) rode in as they always do"
        )
    return keep, warnings


def build_external_plants(
    store: Store,
    *,
    declarations: Sequence[Mapping[str, Any]],
    dossiers: Mapping[str, Mapping[str, Any]],
    seed: str,
    inventory: Sequence[Mapping[str, Any]],
    archive: Sequence[Mapping[str, Any]],
    backend: Any,
    model_key: str,
    judged_sets: Sequence[str] = DEFAULT_JUDGED_SETS,
    inventory_rows_k: int = DEFAULT_INVENTORY_ROWS_K,
    archive_rows_k: int = DEFAULT_ARCHIVE_ROWS_K,
    corpus_k: int = DEFAULT_CORPUS_K,
    corpus_mode: str = DEFAULT_CORPUS_MODE,
    candidate_hit_similarity: float = DEFAULT_CANDIDATE_HIT_SIMILARITY,
) -> list[dict[str, Any]]:
    """A round's declared plants, dressed as records and given their own
    reference bundles -- the same shape :func:`build_plants` produces, so
    nothing downstream has to know which battery a plant came from.

    **Missing donor fields are filled exactly as a harness plant's are**
    (:func:`_donor_fields`): a plant whose ``requirements`` and ``probe``
    were two fixed strings, or whose home was a bare family where a real
    record's is family/cell, is a plant a judge picks out on the shape of
    its fields rather than on its content. A declaration that NAMES a
    ``donor_ref`` borrows that record's fields; one that does not borrows a
    seeded donor from this batch.

    The row a plant was cut from is forced into its own bundle when
    ``source_ref`` names one, on :func:`plant_reference_bundle`'s own
    argument."""
    declared = normalize_judged_sets(judged_sets)
    rng = derive_rng(seed, salt="external-plants")
    inventory_ids = {str(row["row_id"]) for row in inventory}
    archive_ids = {str(row["idea_id"]) for row in archive}
    plants: list[dict[str, Any]] = []
    for declaration in declarations:
        donor_ref = declaration.get("donor_ref")
        if donor_ref:
            idea = read_idea(store, idea_id=str(donor_ref)) or {}
            donor = {
                "idea_id": str(donor_ref),
                "home_mechanic": idea.get("home"),
                "requirements": idea.get("requirements"),
                "probe": idea.get("probe"),
                "provenance_docs": _provenance_docs(idea),
            }
        else:
            donor = _donor_fields(
                store, rng, dossiers, family=home_family(declaration.get("home_mechanic")) or None
            )
        statement = declaration["statement"]
        source_ref = declaration.get("source_ref")
        record = {
            "requirements": declaration.get("requirements")
            if declaration.get("requirements") is not None
            else donor.get("requirements"),
            "statement": statement,
            "home_mechanic": declaration.get("home_mechanic") or donor.get("home_mechanic"),
            "probe": declaration.get("probe") if declaration.get("probe") is not None else donor.get("probe"),
            "provenance_docs": list(donor.get("provenance_docs") or []),
            "extra_text": declaration.get("extra_text"),
        }
        bundle = plant_reference_bundle(
            store, statement=statement, inventory=inventory, archive=archive, backend=backend,
            model_key=model_key, judged_sets=declared, inventory_rows_k=inventory_rows_k,
            archive_rows_k=archive_rows_k, corpus_k=corpus_k, corpus_mode=corpus_mode,
            candidate_hit_similarity=candidate_hit_similarity,
            must_include_row=str(source_ref) if source_ref and str(source_ref) in inventory_ids else None,
            must_include_idea=next(
                (str(ref) for ref in (source_ref, donor_ref) if ref and str(ref) in archive_ids), None
            ),
        )
        plants.append(
            {
                "plant_id": declaration["plant_id"],
                "kind": declaration["kind"],
                "source_ref": source_ref,
                "source_row_id": str(source_ref) if source_ref and str(source_ref) in inventory_ids else None,
                "paraphrase_method": None,
                "donor_ref": donor_ref or donor.get("idea_id"),
                "expected_labels": dict(declaration["expected_labels"]),
                # The orchestrator's copy says where this plant came from.
                # Nothing here reaches a judge: an envelope is built from
                # `record` and the bundle, and this key is neither.
                "external": True,
                # The round's own class for this plant (lane FB-7 item 4) and
                # the batch it was seeded into (item 5). Both are carried on
                # the ORCHESTRATOR's copy, for the same reason `external` is,
                # and both are named in WITHHELD_FROM_JUDGE so their absence
                # from every envelope is asserted rather than assumed.
                "class": declaration.get("class"),
                "batch": declaration.get("batch"),
                "record": record,
                **bundle,
            }
        )
    return plants


def plant_reference_bundle(
    store: Store,
    *,
    statement: str,
    inventory: Sequence[Mapping[str, Any]],
    archive: Sequence[Mapping[str, Any]],
    backend: Any,
    model_key: str,
    judged_sets: Sequence[str] = DEFAULT_JUDGED_SETS,
    inventory_rows_k: int = DEFAULT_INVENTORY_ROWS_K,
    archive_rows_k: int = DEFAULT_ARCHIVE_ROWS_K,
    corpus_k: int = DEFAULT_CORPUS_K,
    corpus_mode: str = DEFAULT_CORPUS_MODE,
    candidate_hit_similarity: float = DEFAULT_CANDIDATE_HIT_SIMILARITY,
    must_include_row: str | None = None,
    must_include_idea: str | None = None,
    exclude_ideas: Sequence[str] = (),
) -> dict[str, Any]:
    """One plant's own reference bundle, computed the way a record's is, for
    exactly the DECLARED sets.

    A module-level function rather than a closure inside
    :func:`build_plants` because three callers need the identical
    computation: the harness battery, the external plants a round declares
    in a ``--plants-file`` (lane FB-5 item 3), and calibration mode (item
    6). A plant whose bundle was retrieved a different way from the records
    it rides among is a plant a judge can pick out on the shape of its
    evidence rather than on its content, and the whole battery rests on that
    not being possible.

    ``must_include_row``/``must_include_idea`` name a reference row the
    bundle must carry whatever the ranking says. They are passed for the row
    a plant was CUT from, and the reason is the audit's own logic: the plant
    asks "does a reference row already state this", the only correct answers
    are ``same`` and ``variant``, and a miss fails the batch. If the row
    were absent from the bundle, a judge doing the task honestly would
    answer ``new-mechanism`` -- and the battery would be failing batches for
    the embedding's shortcomings rather than the judge's. The row enters at
    its own similarity, in rank order, indistinguishable from the rest."""
    declared = normalize_judged_sets(judged_sets)
    vector = backend.embed_batch([statement], kind="document")[0]
    bundle: dict[str, Any] = {}
    if "R2" in declared:
        bundle["archive_rows"] = _nearest_archive_rows(
            vector, archive, k=archive_rows_k, exclude=exclude_ideas, must_include=must_include_idea,
        )
    if "R3" in declared:
        scored = sorted(
            ((cosine_similarity(list(vector), list(r["vector"])), r) for r in inventory),
            key=lambda pair: (-pair[0], str(pair[1]["row_id"])),
        )
        top = scored[: max(inventory_rows_k, 0)]
        if must_include_row and must_include_row not in {r["row_id"] for _sim, r in top}:
            forced = next((pair for pair in scored if pair[1]["row_id"] == must_include_row), None)
            if forced is not None:
                top = sorted([*top[: max(inventory_rows_k - 1, 0)], forced],
                             key=lambda pair: (-pair[0], str(pair[1]["row_id"])))
        bundle["inventory_rows"] = [
            {
                "row_id": row["row_id"],
                "doc_id": row["doc_id"],
                "family": row["family"],
                "similarity": round(sim, 6),
                "text": _fenced_text(row["text"], row.get("license_tier")),
            }
            for sim, row in top
        ]
    if "R4" in declared:
        corpus = _corpus_neighbours(
            store, statement=statement, vector=vector, model_key=model_key, k_total=corpus_k,
            mode=corpus_mode, hit_similarity=candidate_hit_similarity,
        )
        bundle["retrieved"] = corpus["hits"]
    return bundle


#: What the harness battery expects a judge to answer about one of its
#: plants, in the DESIGN's words. A planted register row and a paraphrase of
#: a record in the batch are both rows that already exist, so ``same`` and
#: ``variant`` are the only honest answers. A round that re-spells the set
#: the battery is scored against gets these translated into its own words
#: (:func:`round_labels_for_canonical`) where the plants are built, never
#: shown the design's spelling it was not offered.
PLANT_EXPECTED_CANONICAL: tuple[str, ...] = ("same", "variant")


def build_plants(
    store: Store,
    *,
    dossiers: Mapping[str, Mapping[str, Any]],
    seed: str,
    k: int = PLANTS_PER_BATCH,
    family_of: Callable[[Mapping[str, Any]], str] | None = None,
    corpus_k: int = DEFAULT_CORPUS_K,
    corpus_mode: str = DEFAULT_CORPUS_MODE,
    candidate_hit_similarity: float = DEFAULT_CANDIDATE_HIT_SIMILARITY,
    inventory_rows_k: int = DEFAULT_INVENTORY_ROWS_K,
    archive_rows_k: int = DEFAULT_ARCHIVE_ROWS_K,
    judged_sets: Sequence[str] = DEFAULT_JUDGED_SETS,
    label_vocabularies: Mapping[str, Any] | None = None,
    reembed_archive: bool = False,
) -> list[dict[str, Any]]:
    """The seeded-plant battery: ``k`` MECHANIC ROWS rewritten as idea
    statements, and ``k`` paraphrases of ideas already in this batch.

    **Only a mechanic row may be planted** (:func:`parse_mechanic_rows`).
    The reference set holds more than mechanics -- citation-convention
    notes, coverage-gap lists, headings -- and a plant cut from one of those
    states no mechanism, so the only honest label for it is ``unscreenable``
    and the batch fails on the battery's own sampling rather than on the
    judge. The rows are filtered first and sampled second, and a reference
    set that cannot supply ``k`` of them is a refusal
    (``code="no_mechanic_rows"``), never a short list.

    An inventory plant IS an existing row, so its only correct labels are
    ``same`` or ``variant``. A judge that calls one ``new-mechanism`` is
    either being gamed or drifting, and either way its labels for that batch
    cannot be trusted -- which is why a miss fails the batch outright
    (:func:`score_plants`) rather than being noted.

    Each plant is built to be indistinguishable from a real envelope: a
    statement in a seeded frame (:data:`_PLANT_FRAMES`), the home cell,
    requirements, probe and provenance of a real donor record
    (:func:`_donor_fields`), and its OWN retrieved bundle -- R3 rows scored
    against the plant statement, R4 hits retrieved for it. An inventory
    plant's bundle therefore contains the row it was cut from, which is the
    point: the judge is handed the row and asked whether the record states
    it, exactly as it is for a flagged record.

    Returns ``[]`` for the inventory half when R3 has no usable vectors --
    :func:`build_judged_batch` refuses such a batch rather than shipping an
    unauditable one. That case is kept apart from the refusal above: a
    register nobody embedded and a register with no mechanic rows need
    different fixes.

    **The expectation is stamped in the ROUND's words.** Every plant here is
    a row that already exists, so the design's answer is ``same`` or
    ``variant`` (:data:`PLANT_EXPECTED_CANONICAL`); a round that re-spells
    the set the battery is scored against is shown -- and answers in -- its
    own vocabulary, so the expectation is translated through the round's
    canonical mapping (:func:`round_labels_for_canonical`) before it is
    stamped. Without that, a PERFECT judge answering in the words it was
    offered misses every harness plant, the batch fails its own audit, and
    the verdict rows record ``plants_failed`` about a judge that did the
    task. A round whose vocabulary offers no word for any of them is a
    refusal (``code="plant_labels_unexpressible"``), because that battery
    cannot be caught by any answer the judge can give."""
    rng = derive_rng(seed, salt="plants")
    _require_query_embed_backend(store, corpus_mode=corpus_mode, action="lens screen (--judged-prep, plant battery)")
    model_key, backend = retrieve_engine._resolve_embed_backend(store, side="query")
    declared = normalize_judged_sets(judged_sets)
    # The set a harness plant's LIST expectation is scored against, chosen by
    # the same rule `score_plants` uses, so the words stamped here and the
    # words compared there are words for the same question.
    primary = "R3" if "R3" in declared else declared[0]
    expected_labels = round_labels_for_canonical(
        label_vocabularies, reference_set=primary, canonical=PLANT_EXPECTED_CANONICAL
    )
    if k > 0 and not expected_labels:
        refusal = NoveltyError(
            f"plant battery: this round's vocabulary for {primary} offers the judge no word that means "
            f"{list(PLANT_EXPECTED_CANONICAL)!r} (it offers "
            f"{list(((label_vocabularies or {}).get('sets') or {}).get(primary, {}).get('labels') or [])!r}), "
            "so a harness plant -- a row the judge is handed verbatim -- could not be caught by any answer "
            "the judge can return, and the batch would fail its own audit on a judge that did the task. "
            "Map one of this round's labels onto 'same' or 'variant', or seed the round's own plants with "
            "--plants-file and --plants 0"
        )
        refusal.code = "plant_labels_unexpressible"
        raise refusal
    rows = [r for r in _inventory_rows(store, model_key=model_key, family_of=family_of) if r["vector"]]
    archive = (
        _archive_rows(store, backend=backend, model_key=model_key, reembed=reembed_archive)
        if "R2" in declared else []
    )
    plants: list[dict[str, Any]] = []

    def _bundle(statement: str, *, must_include: str | None = None, must_include_idea: str | None = None) -> dict[str, Any]:
        return plant_reference_bundle(
            store, statement=statement, inventory=rows, archive=archive, backend=backend,
            model_key=model_key, judged_sets=declared, inventory_rows_k=inventory_rows_k,
            archive_rows_k=archive_rows_k, corpus_k=corpus_k, corpus_mode=corpus_mode,
            candidate_hit_similarity=candidate_hit_similarity, must_include_row=must_include,
            must_include_idea=must_include_idea,
        )

    candidates = mechanic_row_candidates(rows)
    # ``rows`` empty is the OTHER refusal (no R3 at all, or none embedded):
    # :func:`build_judged_batch` names that one, and naming it here too would
    # report a missing register as a register with no mechanic rows.
    if rows and k > 0 and len(candidates) < k:
        refusal = NoveltyError(
            f"plant battery: the reference set states {len(candidates)} mechanic row(s) "
            f"({MECHANIC_ROW_ID_PATTERN}) and {k} inventory plant(s) were asked for. A plant cut from a "
            "chunk that states no mechanism has no same/variant answer, so a judge doing the task returns "
            "'unscreenable', the scorer counts a missed inventory plant and the batch fails for the "
            "battery's own sampling. Ingest the register's mechanic rows (or lower --plants) rather than "
            "planting prose"
        )
        refusal.code = "no_mechanic_rows"
        raise refusal
    chosen_rows = rng.sample(candidates, min(k, len(candidates))) if candidates else []
    for i, mech in enumerate(chosen_rows):
        statement = _plant_statement(rng, mech["statement"])
        donor = _donor_fields(store, rng, dossiers, family=mech["family"])
        plants.append(
            {
                "plant_id": f"PLANT-inventory-{i}",
                "kind": "inventory",
                "source_ref": mech["row_id"],
                "source_row_id": mech["source_row_id"],
                "paraphrase_method": None,
                "donor_ref": donor.get("idea_id"),
                "expected_labels": list(expected_labels),
                "record": {
                    "requirements": donor["requirements"],
                    "statement": statement,
                    "home_mechanic": donor["home_mechanic"],
                    "probe": donor["probe"],
                    "provenance_docs": donor["provenance_docs"],
                },
                **_bundle(statement, must_include=mech["row_id"]),
            }
        )

    idea_ids = sorted(dossiers)
    chosen_ideas = rng.sample(idea_ids, min(k, len(idea_ids))) if idea_ids else []
    for i, idea_id in enumerate(chosen_ideas):
        idea = read_idea(store, idea_id=idea_id) or {}
        donor_statement = strip_self_assessment(_statement(idea))["text"]
        statement, method = _paraphrase_statement(rng, donor_statement)
        plants.append(
            {
                "plant_id": f"PLANT-paraphrase-{i}",
                "kind": "paraphrase",
                "source_ref": idea_id,
                "source_row_id": None,
                "paraphrase_method": method,
                "donor_ref": idea_id,
                "expected_labels": list(expected_labels),
                "record": {
                    "requirements": idea.get("requirements"),
                    "statement": statement,
                    "home_mechanic": idea.get("home"),
                    "probe": idea.get("probe"),
                    "provenance_docs": _provenance_docs(idea),
                },
                **_bundle(statement, must_include_idea=idea_id),
            }
        )
    return plants


#: The prefix a masked subject id carries into a judge's prompt.
JUDGE_MASK_PREFIX = "J-"


def build_judge_views(envelopes: Sequence[Mapping[str, Any]], *, seed: str) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """``(views, mask)`` -- the envelopes as a JUDGE may see them, and the
    map back, which stays with the batch.

    A plant a judge can pick out tests nothing, and an envelope's
    ``subject_id`` is ``PLANT-inventory-0`` -- so any prompt builder that
    copies the envelope hands the judge the answer key. Every view carries a
    ``J-<n>`` id instead, numbered under the batch seed rather than by
    position, and the mask lives only in the batch file the orchestrator
    holds.

    Nothing else about a view differs from its envelope: the two are built
    from one object, so a field that would identify a plant cannot appear in
    one and not the other."""
    order = list(range(len(envelopes)))
    derive_rng(seed, salt="judge-mask").shuffle(order)
    views: list[dict[str, Any]] = []
    mask: dict[str, str] = {}
    for position, envelope in enumerate(envelopes):
        masked_id = f"{JUDGE_MASK_PREFIX}{order[position]}"
        mask[masked_id] = str(envelope["subject_id"])
        views.append({**{k: v for k, v in envelope.items() if k != "subject_id"}, "subject_id": masked_id})
    return views, mask


def unmask_labels(labels: Mapping[str, Any], mask: Mapping[str, str] | None) -> dict[str, Any]:
    """Labels keyed by masked id, real id, or a mix, keyed by real id.

    A judge answers under the ids it was shown; an orchestrator relaying a
    second judge's sheet may not have translated them. Both are accepted
    because refusing one of them would make the mask a trap rather than a
    barrier."""
    if not mask:
        return dict(labels)
    out: dict[str, Any] = {}
    for key, value in labels.items():
        out[mask.get(str(key), str(key))] = value
    return out


def build_judged_batch(
    store: Store,
    *,
    round_id: str,
    dossiers: Mapping[str, Mapping[str, Any]],
    seed: str,
    sample_fraction: float = JUDGED_SAMPLE_FRACTION,
    second_judge_fraction: float = SECOND_JUDGE_FRACTION,
    plants_per_kind: int = PLANTS_PER_BATCH,
    family_of: Callable[[Mapping[str, Any]], str] | None = None,
    batch_id: str | None = None,
    out_dir: Path | str | None = None,
    corpus_mode: str = DEFAULT_CORPUS_MODE,
    judged_sets: Sequence[str] | None = None,
    archive_rows_k: int = DEFAULT_ARCHIVE_ROWS_K,
    label_vocabularies: Mapping[str, Any] | None = None,
    external_plants: Any = None,
    batch_fail_on: Sequence[str] | None = None,
    reembed_archive: bool = False,
) -> dict[str, Any]:
    """Phase 3b preparation: scope, plants, envelopes, second-judge sample.

    The envelopes are returned SHUFFLED under the seed, plants mixed in
    among the real records, because plants a judge can pick out of a
    trailing block test nothing. Each envelope's ``subject_id`` is the only
    thing that says which is which, and the mapping stays here.

    Every envelope -- real record or plant -- is built by the same
    :func:`_envelope` and carries exactly the reference sets the round
    DECLARED (``judged_sets``, default :data:`DEFAULT_JUDGED_SETS`): the
    nearest R3 rows with their text, the R4/R5 bundle with theirs, and --
    when the round declares R2 -- the nearest archive idea rows with theirs.
    The declaration is recorded on the batch, so
    :func:`record_novelty_verdicts` writes rows for the sets the judge was
    actually shown rather than for the sets this module used to hardwire.

    **Refuses a batch with no inventory plants.** Design 4.4 stakes the
    batch on them: any inventory-plant miss fails it. A batch that carries
    none cannot fail that way, so its plant score is vacuous -- ``catch_rate
    0.0``, ``inventory_failures []``, ``batch_failed False`` -- and the
    ideas would consolidate on the word of a judge nothing checked. That is
    a refusal rather than a caveat because the condition is structural (no
    inventory, or none embedded), not a judgement about this batch's
    labels.
    """
    declared = normalize_judged_sets(judged_sets)
    # The round's own label vocabulary, resolved once. `None` means the round
    # declared none, and the batch then records none: a default vocabulary
    # written onto the batch as though it were a declaration would be a claim
    # the round never made, and `record_novelty_verdicts` reads the absence
    # correctly either way.
    vocabularies = (
        load_label_vocabularies(label_vocabularies, judged_sets=declared)
        if label_vocabularies is not None and not label_vocabularies.get("sha256")
        else label_vocabularies
    )
    judge_labels = label_block_for_judge(vocabularies)
    scope = select_judged_scope(dossiers, seed=seed, sample_fraction=sample_fraction)
    # ``corpus_mode`` reaches the plant battery because a plant's bundle is
    # built exactly the way a record's was (lane F-1: and a battery retrieved
    # through a different tier than the batch it polices would be testing the
    # judge against a different instrument). ``judged_sets`` reaches it for
    # the same reason: a plant carrying a reference set the records do not is
    # a plant a judge can pick out without reading it.
    fail_on = normalize_fail_kinds(batch_fail_on)
    # This batch's id is resolved BEFORE the plants are filtered, because it
    # is what they are filtered by (lane FB-7 item 5).
    this_batch_id = batch_id or "judged-0"
    batch_warnings: list[str] = []
    declarations = (
        load_external_plants(external_plants, judged_sets=declared, label_vocabularies=vocabularies)
        if external_plants is not None
        else []
    )
    if declarations:
        declarations, batch_warnings = plants_for_batch(declarations, batch_id=this_batch_id)
    plants = build_plants(
        store, dossiers=dossiers, seed=seed, k=plants_per_kind, family_of=family_of,
        reembed_archive=reembed_archive,
        corpus_mode=corpus_mode, judged_sets=declared, archive_rows_k=archive_rows_k,
        # The round's vocabulary reaches the battery so its plants expect the
        # words the judge is actually offered (lane FB-5 stage 3, B1): a
        # battery stamped in the design's words scores a perfect judge at
        # zero the moment a round re-spells the set it is scored against.
        label_vocabularies=vocabularies,
    )

    # R2 is computed HERE rather than in the mechanical half, because the
    # declaration is the judged batch's: a round that screens mechanically
    # and only later decides it judges against the archive would otherwise
    # have to re-screen to get the rows. Embedded once for the whole batch
    # and shared with the external plants' own bundles.
    archive: list[dict[str, Any]] = []
    if "R2" in declared or declarations:
        _require_query_embed_backend(
            store, corpus_mode=corpus_mode, action="lens screen (--judged-prep, R2 archive rows)"
        )
        batch_model_key, batch_backend = retrieve_engine._resolve_embed_backend(store, side="query")
        if "R2" in declared:
            archive = _archive_rows(
                store, backend=batch_backend, model_key=batch_model_key, reembed=reembed_archive
            )

    if declarations:
        clashes = sorted({d["plant_id"] for d in declarations} & {p["plant_id"] for p in plants})
        if clashes:
            raise NoveltyError(
                f"plants file: plant_id(s) {clashes!r} collide with the harness battery's own ids. A "
                "collision makes two plants one row in every score; rename them (the harness uses "
                "PLANT-<kind>-<n>)"
            )
        plants = [
            *plants,
            *build_external_plants(
                store, declarations=declarations, dossiers=dossiers, seed=seed,
                inventory=[
                    r for r in _inventory_rows(store, model_key=batch_model_key, family_of=family_of)
                    if r["vector"]
                ],
                archive=archive, backend=batch_backend, model_key=batch_model_key,
                judged_sets=declared, archive_rows_k=archive_rows_k, corpus_mode=corpus_mode,
            ),
        ]

    n_inventory_plants = sum(1 for p in plants if p["kind"] == "inventory")
    # Auditability is about the kinds this batch FAILS on, not about the
    # inventory by name: a round that seeds its own area plants and fails on
    # them is audited, and a round whose only failing kind is missing is not,
    # however many plants of other kinds it carries. `plants_per_kind == 0`
    # with no plants file is a batch that asked for no battery at all, which
    # is `unauditable` at scoring time and says so there.
    if (plants_per_kind > 0 or declarations) and not any(p["kind"] in fail_on for p in plants):
        raise NoveltyError(
            f"judged batch: no plant of a failing kind ({list(fail_on)!r}) could be built, so this batch "
            "cannot fail the audit that makes its labels trustworthy (design 4.4: any inventory-plant miss "
            "fails the batch) -- no inventory plants, and its ideas would consolidate on the word of a "
            "judge nothing checked. Either the program has no inventory source, or none of its rows is "
            "embedded under this program's model_key, or the plants file seeds no failing kind. Ingest and "
            "embed the inventory, or declare --batch-fail-on for the kinds this round does seed"
        )

    envelopes: list[dict[str, Any]] = []
    for idea_id in scope["scope"]:
        idea = read_idea(store, idea_id=idea_id)
        if idea is None:
            raise NoveltyError(f"judged batch: idea {idea_id!r} named by the scope no longer exists")
        dossier = dossiers[idea_id]
        retrieved = list(dossier["candidate_hits"]["R4"]) + list(dossier["candidate_hits"]["R5"])
        archive_rows: list[dict[str, Any]] = []
        if "R2" in declared:
            subject = next((row for row in archive if row["idea_id"] == idea_id), None)
            archive_rows = _nearest_archive_rows(
                subject.get("vector") if subject else None, archive, k=archive_rows_k, exclude=(idea_id,),
            )
        envelopes.append(
            build_verifier_envelope(
                idea, retrieved=retrieved, inventory_rows=dossier.get("inventory_rows") or (),
                archive_rows=archive_rows, judged_sets=declared, label_block=judge_labels,
            )
        )
    for plant in plants:
        envelopes.append(
            _envelope(
                subject_id=plant["plant_id"],
                record=plant["record"],
                statement=plant["record"]["statement"],
                inventory_rows=plant.get("inventory_rows") or (),
                retrieved=plant.get("retrieved") or (),
                archive_rows=plant.get("archive_rows") or (),
                judged_sets=declared,
                label_block=judge_labels,
            )
        )
    derive_rng(seed, salt="envelope-order").shuffle(envelopes)
    judge_views, mask = build_judge_views(envelopes, seed=seed)

    rng = derive_rng(seed, salt="second-judge")
    n_second = int(math.ceil(len(scope["scope"]) * second_judge_fraction)) if scope["scope"] else 0
    second_judge = sorted(rng.sample(scope["scope"], n_second)) if n_second else []

    # The snapshot the dossiers were built against, carried onto the batch
    # so every verdict written from it can cite the reference sets AS OF the
    # moment they were measured -- which is the only timestamp under which
    # "nothing retrieved this" means anything.
    snapshot = next(iter(dossiers.values()))["reference_snapshot"] if dossiers else None
    batch = {
        "round_id": round_id,
        "batch_id": this_batch_id,
        "procedure": PROCEDURE,
        "procedure_version": PROCEDURE_VERSION,
        "seed": seed,
        # Which of the round's own plants this batch actually got (lane FB-7
        # item 5), and anything the filter declined to do quietly. Recorded
        # on the batch rather than left to be re-derived from the plants
        # file, because the file can be edited between two batches and the
        # question "what did THIS batch contain" has to survive that.
        "plants_injected": sorted(str(d["plant_id"]) for d in declarations),
        "warnings": list(batch_warnings),
        # The instrument this batch WAS: which reference sets the judge was
        # shown and will return a label for. Recorded rather than assumed,
        # because `record_novelty_verdicts` runs in a separate invocation and
        # a round that re-declared its sets between the two would otherwise
        # write verdict rows for sets nobody was shown.
        "judged_sets": list(declared),
        "reference_snapshot": snapshot,
        "scope": scope,
        "plants": plants,
        "n_inventory_plants": n_inventory_plants,
        "n_paraphrase_plants": sum(1 for p in plants if p["kind"] == "paraphrase"),
        "batch_fail_on": list(fail_on),
        "envelopes": envelopes,
        # What a prompt builder copies. The envelopes keep their real ids
        # because the orchestrator scores against them; the views carry the
        # masked ones and nothing else differs.
        "judge_views": judge_views,
        "mask": mask,
        "second_judge": second_judge,
        "ts": now(),
    }
    if vocabularies is not None:
        # Hashed onto the batch so a round cannot judge under one vocabulary
        # and record under another without it being visible. Absent when the
        # round declared none -- see the note where `vocabularies` is
        # resolved.
        batch["label_vocabularies"] = vocabularies
        batch["labels_sha256"] = vocabularies["sha256"]
    base = Path(out_dir) if out_dir is not None else round_dir(store.program_root, round_id)
    (base / "judged").mkdir(parents=True, exist_ok=True)
    (base / "judged" / f"{batch['batch_id']}.json").write_text(
        json.dumps(batch, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n",
    )
    return batch


# ---------------------------------------------------------------------------
# 3b: recording what the judge returned
# ---------------------------------------------------------------------------


def expected_labels_for_set(
    plant: Mapping[str, Any], reference_set: str, *, primary: str
) -> list[str] | None:
    """What a plant expects a judge to answer FOR ONE reference set, or
    ``None`` when it expects nothing there.

    Two shapes, both honoured, and the reason is that they say different
    things. A harness plant carries a LIST (``["same", "variant"]``): it is
    an expectation about the one set the battery is built out of, which is
    the round's primary set. A round's own plant carries a MAPPING keyed by
    reference set, because a plant cut from an archive row can legitimately
    be ``requested`` against R2 and ``absent`` against R4 at once."""
    expected = plant.get("expected_labels")
    if isinstance(expected, Mapping):
        values = expected.get(reference_set)
        return [str(v) for v in values] if values else None
    if reference_set != primary or not expected:
        return None
    return [str(v) for v in expected]


def score_plants(
    batch: Mapping[str, Any],
    labels: Mapping[str, Mapping[str, Any]],
    *,
    judged_sets: Sequence[str] | None = None,
    batch_fail_on: Sequence[str] | None = None,
    label_vocabularies: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Did the judge catch the plants?

    An INVENTORY plant is an existing row wearing an idea's clothes. Missing
    one -- or not labelling it at all -- fails the batch, because a judge
    that cannot recognise a row it was handed verbatim cannot be trusted to
    have recognised the ones it was not. A paraphrase miss is reported and
    counted but does not fail: paraphrase detection is the harder task and
    the design does not stake the batch on it.

    A batch carrying no plant of a FAILING kind is ``unauditable`` and fails
    on that ground alone. :func:`build_judged_batch` refuses to build one, so
    this arm only fires for a batch assembled elsewhere -- but without it,
    the one shape that cannot fail the audit would be the one that passes it
    silently (``catch_rate 0.0``, no failures, ``batch_failed False``).

    **Every declared set, not only the inventory** (lane FB-5 item 3). A
    plant is scored against ``expected_labels[set]`` for every set the round
    declared that it states an expectation for; it is ``caught`` only when
    it was caught on ALL of them, ``missed`` the moment it misses one, and
    ``unlabelled`` when a set it expects an answer for got none. A harness
    plant carries a list rather than a mapping and is therefore scored
    against the primary set alone -- which is exactly what this function did
    before there was anything else to score.

    ``batch_fail_on`` names which KINDS' misses fail the batch (default
    :data:`DEFAULT_BATCH_FAIL_ON`, i.e. today's rule). ``inventory_failures``
    keeps meaning what its name says -- inventory misses -- and ``failures``
    is the set the batch actually turns on, so a reader is never handed a
    list of paraphrase ids under a key that says inventory.

    **The comparison is canonical as well as literal** (lane FB-5 stage 3,
    B1). :func:`build_plants` stamps the harness battery in the round's own
    words, so the literal comparison is the one that fires; the canonical
    arm catches the two cases it cannot -- a batch built before that (its
    plants expect the design's words while its judge answers the round's)
    and a judge that answers in the design's word for a label the round
    re-spelled. Two spellings of one label are one answer, and a round's
    audit must not turn on which of them a batch file happens to hold."""
    plants_by_id = {p["plant_id"]: p for p in batch["plants"]}
    vocabularies = (
        label_vocabularies if label_vocabularies is not None else batch.get("label_vocabularies")
    )
    declared = normalize_judged_sets(
        judged_sets if judged_sets is not None else batch.get("judged_sets")
    )
    fail_on = normalize_fail_kinds(
        batch_fail_on if batch_fail_on is not None else batch.get("batch_fail_on")
    )
    primary = "R3" if "R3" in declared else declared[0]

    caught, missed, unlabelled = [], [], []
    per_set: dict[str, dict[str, list]] = {
        name: {"caught": [], "missed": [], "unlabelled": [], "n_expecting": 0} for name in declared
    }
    for plant in batch["plants"]:
        given = labels.get(plant["plant_id"]) or {}
        plant_missed: list[dict[str, Any]] = []
        plant_unlabelled = False
        scored_any = False
        for reference_set in declared:
            expected = expected_labels_for_set(plant, reference_set, primary=primary)
            if expected is None:
                continue
            scored_any = True
            per_set[reference_set]["n_expecting"] += 1
            label = given.get(REFERENCE_SETS[reference_set])
            if label is None:
                plant_unlabelled = True
                per_set[reference_set]["unlabelled"].append(plant["plant_id"])
            elif label in expected or canonical_label(
                vocabularies, reference_set=reference_set, label=label
            ) in {
                canonical_label(vocabularies, reference_set=reference_set, label=want)
                for want in expected
            }:
                per_set[reference_set]["caught"].append(plant["plant_id"])
            else:
                plant_missed.append(
                    {"plant_id": plant["plant_id"], "reference_set": reference_set,
                     "label": label, "expected": expected}
                )
                per_set[reference_set]["missed"].append(plant["plant_id"])
        if plant_missed:
            # ONE entry per plant, keeping the pre-FB-5 keys (`plant_id`,
            # `label`, `expected`) so a reader that counted misses before
            # counts the same number, with the set it missed on named beside
            # them. A plant that misses on two sets is one miss, not two:
            # `missed` has always been a list of plants.
            first = plant_missed[0]
            missed.append(
                {"plant_id": first["plant_id"], "label": first["label"],
                 "expected": first["expected"], "reference_set": first["reference_set"]}
            )
        elif plant_unlabelled or not scored_any:
            unlabelled.append(plant["plant_id"])
        else:
            caught.append(plant["plant_id"])

    def _kind(pid: str) -> str:
        return plants_by_id[pid]["kind"]

    def _failures(kinds: Sequence[str]) -> list[str]:
        ids = [m["plant_id"] for m in missed if _kind(m["plant_id"]) in kinds]
        ids += [pid for pid in unlabelled if _kind(pid) in kinds]
        return sorted(set(ids))

    inventory_failures = _failures(("inventory",))
    failures = _failures(fail_on)
    n_plants = len(batch["plants"])
    n_inventory_plants = sum(1 for p in batch["plants"] if p["kind"] == "inventory")
    # Counted by KIND, not as "everything that is not inventory": with a
    # round's own area/custom plants in the batch, the subtraction would
    # report them as paraphrases.
    n_paraphrase_plants = sum(1 for p in batch["plants"] if p["kind"] == "paraphrase")
    n_failable = sum(1 for p in batch["plants"] if p["kind"] in fail_on)
    unauditable = not n_failable

    by_kind: dict[str, dict[str, Any]] = {}
    for kind in PLANT_KINDS:
        ids = [p["plant_id"] for p in batch["plants"] if p["kind"] == kind]
        if not ids:
            continue
        kind_caught = [pid for pid in caught if pid in set(ids)]
        by_kind[kind] = {
            "n": len(ids),
            "caught": sorted(kind_caught),
            "missed": sorted(m["plant_id"] for m in missed if m["plant_id"] in set(ids)),
            "unlabelled": sorted(pid for pid in unlabelled if pid in set(ids)),
            "catch_rate": round(len(kind_caught) / len(ids), 6),
            "fails_batch": kind in fail_on,
        }
    by_set = {
        name: {
            "n_expecting": block["n_expecting"],
            "caught": sorted(block["caught"]),
            "missed": sorted(block["missed"]),
            "unlabelled": sorted(block["unlabelled"]),
            "catch_rate": (
                round(len(block["caught"]) / block["n_expecting"], 6) if block["n_expecting"] else None
            ),
        }
        for name, block in per_set.items()
    }
    return {
        "n_plants": n_plants,
        "n_inventory_plants": n_inventory_plants,
        "n_paraphrase_plants": n_paraphrase_plants,
        "caught": caught,
        "missed": missed,
        "unlabelled": unlabelled,
        "catch_rate": round(len(caught) / n_plants, 6) if n_plants else None,
        "inventory_failures": inventory_failures,
        "unauditable": unauditable,
        "batch_failed": bool(failures) or unauditable,
        "batch_fail_on": list(fail_on),
        "failures": failures,
        "by_kind": by_kind,
        "by_set": by_set,
    }


def cohens_kappa(
    first: Mapping[str, str], second: Mapping[str, str], *, categories: Sequence[str]
) -> dict[str, Any]:
    """Cohen's kappa over the subjects both judges labelled.

    Reported with ``n`` and ``observed_agreement`` beside it, never alone: a
    kappa over four subjects is a number, not a finding. ``None`` when
    fewer than two subjects overlap, and ``1.0`` by convention when the two
    judges agree on everything and the expected agreement is also 1 (one
    category used throughout) -- the degenerate case where the formula is
    0/0 and "they agreed" is the honest reading."""
    shared = sorted(set(first) & set(second))
    n = len(shared)
    if n < 2:
        return {"kappa": None, "n": n, "observed_agreement": None, "note": "fewer than two co-labelled subjects"}
    agree = sum(1 for s in shared if first[s] == second[s])
    po = agree / n
    pe = 0.0
    for category in categories:
        p1 = sum(1 for s in shared if first[s] == category) / n
        p2 = sum(1 for s in shared if second[s] == category) / n
        pe += p1 * p2
    if pe >= 1.0:
        kappa = 1.0 if po >= 1.0 else 0.0
    else:
        kappa = (po - pe) / (1.0 - pe)
    return {"kappa": round(kappa, 6), "n": n, "observed_agreement": round(po, 6), "expected_agreement": round(pe, 6)}


def confusion_table(
    first: Mapping[str, str],
    second: Mapping[str, str],
    *,
    categories: Sequence[str],
    masked_as: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """``(confusion, disagreements)`` for one reference set. Lane FB-7 item 3.

    :func:`cohens_kappa` reduces two judges to one number, ``n`` and an
    observed agreement. That is the right headline and it is not enough to
    read a balanced battery: a kappa of 0.4 can be two judges disagreeing
    everywhere or two judges agreeing on everything except one label pair
    they read differently, and only the matrix says which. Assembling it by
    hand from two sheets is exactly the work this card exists to have
    already done.

    ``confusion`` is ``{"labels", "matrix", "n_paired", "n_unpaired",
    "unpaired"}``. ``matrix`` is nested ``{judge_a_label: {judge_b_label:
    n}}``, dense over ``labels`` (every cell present, zeros included --
    a matrix with holes in it cannot be read as a matrix) and in the labels
    file's own order, with the round's ``unscreenable`` word among them:
    two judges that both answer it AGREE, and a table that omitted it would
    be about a different vocabulary from the one they answered in.

    ``labels`` is ``categories`` plus, sorted after them, any label a judge
    actually returned that ``categories`` does not list. That cannot happen
    through :func:`record_calibration`, which refuses an out-of-vocabulary
    label before it reaches here; a direct caller can, and when it does the
    stray label is on the AXES as well as in the matrix, so a reader
    iterating ``labels x labels`` sees every count. Silently holding rows
    the axes do not list is the same failure as a matrix with holes, one
    level up.

    A subject exactly ONE judge labelled is counted in ``n_unpaired``,
    listed in ``unpaired``, and excluded from the matrix -- a cell cannot
    hold half a pair. A subject NEITHER judge labelled is not unpaired
    either; it is absent, and it is already reported as ``unlabelled`` by
    the catch tables.

    ``disagreements`` lists every paired subject the two answered
    differently, under the id the JUDGES saw (``masked_as`` maps real id to
    masked id) -- because that is the id on the envelope whoever re-reads
    the item will go looking for."""
    labels = list(dict.fromkeys(str(c) for c in categories))
    paired = sorted(set(first) & set(second))
    unpaired = sorted(set(first) ^ set(second))
    # A label outside the declared vocabulary cannot happen through
    # ``record_calibration``, which refuses one before it gets here, but a
    # direct caller can produce one -- and when it does the label goes into
    # ``labels`` as well as into the matrix (lane FB-7 fix pass, V-12). The
    # first build added a ROW for it and left ``labels`` at the declared
    # vocabulary, so a reader iterating ``labels x labels`` -- which this
    # docstring's own "dense over categories" invites -- silently dropped
    # those counts. A matrix whose axes do not list its own rows is exactly
    # the thing the density rule exists to prevent, one level up.
    stray = [
        label
        for label in dict.fromkeys(
            [str(first[s]) for s in paired] + [str(second[s]) for s in paired]
        )
        if label not in labels
    ]
    labels = labels + sorted(stray)
    matrix = {a: {b: 0 for b in labels} for a in labels}
    disagreements: list[dict[str, Any]] = []
    for subject in paired:
        a, b = str(first[subject]), str(second[subject])
        matrix[a][b] = matrix[a].get(b, 0) + 1
        if a != b:
            disagreements.append(
                {
                    "subject_id": (masked_as or {}).get(subject, subject),
                    "idea_id": subject,
                    "a": a,
                    "b": b,
                }
            )
    confusion = {
        "labels": labels,
        "matrix": matrix,
        "n_paired": len(paired),
        "n_unpaired": len(unpaired),
        "unpaired": [(masked_as or {}).get(s, s) for s in unpaired],
    }
    return confusion, disagreements


def _expected_key(plant: Mapping[str, Any], declared: Sequence[str], *, primary: str) -> str:
    """The canonical spelling of one plant's expectation, used as the
    grouping key of :func:`catch_by_expected`.

    ``R3=same,variant|R4=absent`` -- sets in the round's declared order,
    labels sorted inside each so two plants that expect the same thing in a
    different order group together, and sets that expect nothing omitted.
    A string rather than a tuple because this is a JSON key on an artifact
    somebody reads."""
    parts = []
    for reference_set in declared:
        expected = expected_labels_for_set(plant, reference_set, primary=primary)
        if expected:
            parts.append(f"{reference_set}=" + ",".join(sorted(str(e) for e in expected)))
    return "|".join(parts) if parts else "(no expectation)"


def catch_by_expected(
    batch: Mapping[str, Any],
    sheet: Mapping[str, Mapping[str, Any]],
    scored: Mapping[str, Any],
    *,
    declared: Sequence[str],
    group_by: str = "expected",
) -> dict[str, Any]:
    """One judge's performance grouped by what each plant EXPECTED, rather
    than by the plant's kind. Lane FB-7 item 3.

    ``catch_by_kind`` answers "did this judge recognise a paraphrase", which
    is a question about the battery's construction. A balanced battery asks
    a different one -- "when the right answer was ``absent``, what did this
    judge actually say" -- and nothing on the card answered it, so a round
    reading its own calibration had to join two sheets against the plants
    file by hand.

    Per group: ``n`` plants in it, ``caught`` (the judge got every set this
    plant expects right, the same verdict :func:`score_plants` reaches), and
    ``given`` -- the full histogram of what was actually answered, per
    declared set, ``null`` counted under the key ``"(unlabelled)"``. The
    histogram is the point: a catch rate says how often the judge was right
    and the histogram says what it said when it was wrong, which is the
    thing a round changes its instructions over.

    ``group_by="class"`` groups by the plant's free ``class`` tag instead
    (lane FB-7 item 4); plants that declare none are absent from it, so an
    empty result means the plants file carried no classes rather than that
    nothing was scored."""
    primary = "R3" if "R3" in declared else declared[0]
    caught_ids = set(scored.get("caught") or ())
    groups: dict[str, dict[str, Any]] = {}
    for plant in batch.get("plants") or ():
        if group_by == "class":
            key = plant.get("class")
            if key is None or str(key) == "":
                continue
            key = str(key)
        else:
            key = _expected_key(plant, declared, primary=primary)
        bucket = groups.setdefault(
            key, {"n": 0, "caught": 0, "given": {name: {} for name in declared}}
        )
        bucket["n"] += 1
        if plant["plant_id"] in caught_ids:
            bucket["caught"] += 1
        given = sheet.get(plant["plant_id"]) or {}
        for reference_set in declared:
            label = given.get(REFERENCE_SETS[reference_set])
            word = "(unlabelled)" if label is None else str(label)
            histogram = bucket["given"][reference_set]
            histogram[word] = histogram.get(word, 0) + 1
    return {key: groups[key] for key in sorted(groups)}


def pearson_r(pairs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Pearson *r* between two paired series, reported with ``n`` beside it
    and ``None`` rather than a number when it would not mean one.

    ``None`` in two cases, each named in ``note``: fewer than three pairs
    (an *r* over two points is 1.0 or -1.0 by construction), and zero
    variance in either series (the formula divides by 0, and "every human
    rating was 0.5" is a fact about the ratings, not a correlation).

    Written here rather than pulled from a stats package for the same reason
    :func:`cohens_kappa` is: it is eight lines, and this module already
    refuses to add a dependency for eight lines."""
    xs = [float(p["x"]) for p in pairs]
    ys = [float(p["y"]) for p in pairs]
    n = len(xs)
    if n < 3:
        return {"r": None, "n": n, "note": "fewer than three rated pairs -- an r over two points is +-1 by construction"}
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    dx = [x - mean_x for x in xs]
    dy = [y - mean_y for y in ys]
    var_x = sum(d * d for d in dx)
    var_y = sum(d * d for d in dy)
    if var_x <= 0 or var_y <= 0:
        return {
            "r": None, "n": n,
            "note": "no variance in " + ("the cosines" if var_x <= 0 else "the human ratings"),
        }
    covariance = sum(a * b for a, b in zip(dx, dy))
    return {"r": round(covariance / math.sqrt(var_x * var_y), 6), "n": n}


def _percentile(values: Sequence[float], p: float) -> float | None:
    """The percentile of a SORTED series at index ``round(p * (n - 1))`` --
    :data:`PERCENTILE_METHOD`, and the same convention
    :func:`_pairwise_stats` uses, reused rather than re-derived so two
    distributions reported side by side in one card were cut the same way.

    The value AT a rank, never an interpolation between two. NOT the
    textbook nearest-rank percentile, which is the value at rank
    ``ceil(p * n)`` and differs for many small ``n`` -- see
    :data:`PERCENTILE_METHOD` for why the reported name spells the formula
    out instead of borrowing that one."""
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 6)
    index = min(len(values) - 1, int(round(p * (len(values) - 1))))
    return round(values[index], 6)


def baseline_cosine_distribution(
    dossiers: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    plants: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Each subject's NEAREST corpus neighbour cosine, as a distribution with
    its 50th/90th/95th percentiles -- the number a round needs before it can
    read any single record's distance as far or near.

    **Over the round's RECORDS, or over a calibration's PLANTS** (lane FB-6
    item 5). ``over`` says which, and it is on the card because the two are
    different populations and nothing else in the artifact would tell them
    apart. A calibration runs before a round has a single record, so the
    record distribution it used to report read ``n 0`` -- a baseline over
    nothing, on the one card whose whole job is to say what a cosine means
    here. The plants have exactly the number that is wanted: each carries its
    own retrieved bundle, and ``retrieved[].similarity`` is the same
    statement-to-corpus cosine a record's ``candidate_hits`` carries.

    **Two counts travel with it and are not decoration.** Both bundles are
    thresholded (:data:`DEFAULT_CANDIDATE_HIT_SIMILARITY`): a subject nothing
    retrieved above the bar contributes no cosine at all, so a distribution
    over the rest would be a distribution over the subjects retrieval already
    liked. ``n`` and ``n_with_neighbour`` say exactly how much of the
    population the percentiles are about, and a reader that ignores them is
    reading a biased sample as a baseline. ``n_records`` is the same number
    under the name it carried before there was a second population."""
    if plants is not None:
        over, subjects = "plants", list(plants)
        bundles = [list(p.get("retrieved") or ()) for p in subjects]
    else:
        over, subjects = "records", list((dossiers or {}).values())
        bundles = [list((d.get("candidate_hits", {}) or {}).get("R4") or ()) for d in subjects]
    cosines: list[float] = []
    for hits in bundles:
        similarities = [float(h["similarity"]) for h in hits if h.get("similarity") is not None]
        if similarities:
            cosines.append(max(similarities))
    cosines.sort()
    return {
        "over": over,
        "n": len(subjects),
        "n_records": len(subjects),
        "n_with_neighbour": len(cosines),
        "hit_similarity_floor": DEFAULT_CANDIDATE_HIT_SIMILARITY,
        "min": round(cosines[0], 6) if cosines else None,
        "p50": _percentile(cosines, 0.50),
        "p90": _percentile(cosines, 0.90),
        "p95": _percentile(cosines, 0.95),
        "max": round(cosines[-1], 6) if cosines else None,
        "percentile_method": PERCENTILE_METHOD,
    }


#: How every percentile this module reports is cut. :func:`_percentile`
#: indexes a sorted series at ``round(p * (n - 1))`` -- the value AT a rank,
#: never an interpolation between two of them -- so a reported p90 is always
#: a cosine some subject actually scored. Named in the output (lane FB-7
#: item 2) because a threshold defined as "the 90th percentile of the
#: earlier round's distribution" is a different number under the linear
#: convention, and a reader cannot tell which one they have by looking.
#:
#: **The name is the formula, and that is the point** (lane FB-7 fix pass,
#: V-6). This shipped as ``"nearest-rank"``, which is the name of a
#: DIFFERENT cut: the textbook nearest-rank percentile is the value at rank
#: ``ceil(p * n)``, and the two disagree for many small ``n`` -- at n=6,
#: p=0.9 this returns the 5th value and nearest-rank the 6th; at n=20,
#: p=0.5 this returns the 11th and nearest-rank the 10th. Since ``round``
#: is banker's rounding the disagreement is irregular rather than a
#: constant offset, so a reader who took the label at its word and
#: recomputed the threshold got a number that was wrong in a way no rule
#: would recover. A field added so that "the 90th percentile" means ONE
#: thing has to name the thing it means; the cut itself is unchanged,
#: because every number this module has ever reported was cut this way and
#: renaming a convention must not silently restate old cards.
PERCENTILE_METHOD = "index-round(p*(n-1))"


class CorpusVectorTable:
    """The whole corpus vector table, fetched and decoded ONCE for a pass
    that asks it a question per record. Lane FB-7 fix pass, V-2.

    **What this replaces.** ``--baseline --corpus-mode vector`` asks for the
    true nearest corpus neighbour of every record in a round, and the first
    build asked for it the obvious way: per record, fetch the whole table
    and rank it. That is ``n_records`` whole-table reads, each one decoding
    every BLOB into a list of Python floats through
    :func:`~trialerror.retrieve.vecsearch.fetch_vectors` -- the exact cost
    lane FB-7 item 1 exists to remove. Measured on this lane's own machine
    at the width and scale the brief names (2048 dimensions, a store grown
    to 108k chunks): 107.6 s and 6.8 GB of transient Python objects PER
    RECORD, so a 90-record round asking for its own baseline is hours. The
    number item 2 exists to make readable could still not be read.

    So the table is read once per PASS and held: as a numpy float32 matrix
    when :func:`~trialerror.retrieve.vecsearch.fetch_vector_matrix` can
    build one (``numpy.frombuffer`` over the BLOBs, no per-float Python
    object at all -- the 17x path), and otherwise as the
    ``dict[str, list[float]]`` the plain path has always used. Either way
    the decode happens once and every record after the first is arithmetic.

    **The answer does not change.** On the matrix route numpy is used only
    to NARROW -- it scores every row, keeps everything within
    :data:`trialerror.util.vecmath._SELECTION_MARGIN` of the best, and then
    hands that superset to
    :func:`~trialerror.retrieve.vecsearch.rank_by_query_vector` as Python
    floats, so the id and the score returned are produced by the plain path
    on both routes. This is :mod:`trialerror.retrieve.vecmatrix`'s own
    argument, reused rather than re-derived.

    **Why not** :func:`trialerror.retrieve.vecmatrix.top_ranked`, which
    already answers "rank the whole table" for the retrieval engine: it
    builds and PERSISTS a resident-matrix cache under the program root, and
    ``--baseline`` is documented as writing nothing but the idea-vector
    cache. A read-only verb must not decide to write a file on its way past.
    """

    def __init__(self, store: Store, *, model_key: str, config: Any = None) -> None:
        self._store = store
        self._model_key = model_key
        self._config = config
        self._loaded = False
        self._ids: list[str] = []
        self._matrix: Any = None
        self._by_id: dict[str, list[float]] | None = None

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        universe = retrieve_engine._all_chunk_ids(self._store)
        if not universe:
            return
        fetched = fetch_vector_matrix(self._store, self._model_key, universe, config=self._config)
        if fetched is not None:
            self._ids, self._matrix = fetched
            return
        # numpy absent, the knob off, or a table whose rows disagree about
        # width: the plain path, still fetched once rather than per record.
        self._by_id = fetch_vectors(self._store, self._model_key, universe) or None

    @property
    def n_rows(self) -> int:
        self._load()
        return len(self._ids) if self._matrix is not None else len(self._by_id or {})

    def nearest(self, vector: Sequence[float]) -> tuple[str, float] | None:
        """``(chunk_id, cosine)`` for the single nearest row, or ``None``
        when there is nothing to compare against."""
        self._load()
        query = list(vector)
        if self._matrix is None:
            if not self._by_id:
                return None
            ranked = rank_by_query_vector(query, self._by_id, k=1, config=self._config)
            return ranked[0] if ranked else None
        if not self._ids:
            return None
        scores = cosine_many(query, self._matrix, config=self._config)
        finite = [s for s in scores if s == s and s not in (float("inf"), float("-inf"))]
        if len(finite) == len(scores) and finite:
            threshold = max(finite) - vecmath._SELECTION_MARGIN
            keep = [i for i, s in enumerate(scores) if s >= threshold]
        else:
            # A non-finite score makes the threshold meaningless (NaN
            # compares false against every bound), so the whole set goes to
            # the exact pass -- :func:`trialerror.util.vecmath.top_k`'s own
            # posture for the same case. A table of NaNs is a broken table,
            # not a fast path.
            keep = list(range(len(self._ids)))
        superset = {self._ids[i]: [float(v) for v in self._matrix[i]] for i in keep}
        ranked = rank_by_query_vector(query, superset, k=1, config=self._config)
        return ranked[0] if ranked else None


def _nearest_corpus_neighbour(
    store: Store,
    *,
    vector: Sequence[float] | None,
    statement: str,
    model_key: str,
    mode: str,
    k: int,
    config: Any = None,
    corpus: "CorpusVectorTable | None" = None,
) -> dict[str, Any] | None:
    """The single nearest corpus chunk to one statement's vector, with the
    document it belongs to -- or ``None`` when nothing could be compared.

    ``mode="vector"`` is the honest instrument here and the one
    ``--baseline`` is meant to be run with: the universe is the WHOLE vector
    table, so the answer is the true nearest neighbour rather than the
    nearest of whatever a prefilter proposed. The scan goes through
    :func:`trialerror.retrieve.vecsearch.rank_by_query_vector` with ``k=1``,
    which on a program with numpy narrows in one blocked matmul
    (:mod:`trialerror.util.vecmath`) and returns the same row either way.

    Any other mode retrieves ``k`` candidates through the ordinary search
    path and takes the nearest of those. That is a different question --
    "the nearest thing this tier proposed" -- and the caller records which
    one it asked, because a baseline read under a prefilter is not
    comparable with one read over the corpus.

    ``config`` is the program config the scan reads ``[retrieve]
    numpy_fastpath`` from; omitted, it is read off the store, so ``off``
    reaches this pass too (lane FB-7 fix pass, V-1).

    ``corpus`` is a :class:`CorpusVectorTable` a caller asking this question
    of MANY records built once and is reusing (lane FB-7 fix pass, V-2).
    Omitted, one is built for this single call, so a lone caller sees
    exactly what it always did."""
    if vector is None:
        return None
    config = program_config(store, config)
    if mode == "vector":
        table = corpus if corpus is not None else CorpusVectorTable(
            store, model_key=model_key, config=config
        )
        best = table.nearest(vector)
        ranked = [best] if best is not None else []
    else:
        if not statement.strip():
            return None
        found = retrieve_engine.search(store, query=statement, k=max(k, 1), mode=mode)
        rows = found.get("results") or []
        if not rows:
            return None
        vectors = fetch_vectors(store, model_key, [r["chunk_id"] for r in rows])
        if not vectors:
            return None
        ranked = rank_by_query_vector(list(vector), vectors, k=1, config=config)
    if not ranked:
        return None
    chunk_id, cosine = ranked[0]
    row = store.knowledge.execute("SELECT doc_id FROM chunk WHERE chunk_id = ?", (chunk_id,)).fetchone()
    return {
        "nearest_chunk_id": chunk_id,
        "nearest_doc_id": row["doc_id"] if row is not None else None,
        "cosine": round(float(cosine), 6),
    }


def _matches_where(idea: Mapping[str, Any], where: Mapping[str, str]) -> bool:
    """Whether one record satisfies every ``provenance.<key>=<value>`` term.

    Each key is resolved the way :func:`trialerror.lens.ideas.read_idea`
    resolves a record's own fields -- the schema COLUMN first, then the same
    key inside the ``provenance`` JSON object -- and compared as a string.
    That is the whole reason the filter is spelled ``provenance.<key>``
    rather than naming a column: a round's own bookkeeping (which lens, which
    slice, which arm) lives in that object and never became a column, while
    the AIIF fields did, and a reader picking a population should not have to
    know which side of a migration each field landed on.

    A list-valued field matches when the wanted value is one of its items. A
    key a record does not carry at all does not match -- selecting nothing is
    the honest answer to a filter over a field nobody wrote, and selecting
    everything would report a number for the wrong population."""
    fallback = _provenance_obj(idea.get("provenance"))
    for key, wanted in where.items():
        value = idea.get(key)
        if value is None:
            value = fallback.get(key)
        if value is None:
            return False
        if isinstance(value, (list, tuple)):
            if str(wanted) not in [str(v) for v in value]:
                return False
        elif str(value) != str(wanted):
            return False
    return True


def baseline_distribution(
    store: Store,
    *,
    round_id: str,
    status: str | None = None,
    where: Mapping[str, str] | None = None,
    corpus_mode: str = "vector",
    corpus_k: int = DEFAULT_CORPUS_K,
    reembed: bool = False,
) -> dict[str, Any]:
    """Every record of one round against the corpus, as a distribution of
    record→corpus NEAREST-NEIGHBOUR cosines. Lane FB-7 item 2.

    **Why this exists as its own read.** A round that wants to define a
    threshold as "the 90th percentile of the earlier round's record→corpus
    nearest-neighbour cosine" could not get that number out of the harness.
    :func:`baseline_cosine_distribution` reads it off the bundles a screen
    already wrote, which means two things it cannot help: on a calibration
    it is over PLANTS, and the bundles are thresholded
    (:data:`DEFAULT_CANDIDATE_HIT_SIMILARITY`), so a record nothing
    retrieved above the bar contributes no cosine at all and the
    percentiles are over the subjects retrieval already liked. This function
    asks the corpus directly, per record, with no floor -- ``min`` here is a
    real nearest neighbour, not the smallest one that cleared a bar -- and
    it works on an ARCHIVED round, which is the round a later one wants the
    number from.

    **It writes nothing.** No verdict row, no dossier, no idea row, no
    batch file, no event. The one exception, stated because it is a write:
    a record whose statement is not in the per-model idea-vector cache is
    embedded and CACHED, exactly as a screen would have cached it -- the
    cache answers "what is this statement's vector under this model", which
    is true independently of who asked, and paying for the same embedding
    twice to keep a read technically read-only would be a worse trade.
    ``reembed=True`` rebuilds those rows.

    ``status`` restricts to records in one state (``archived`` for a closed
    round); ``where`` is a mapping of resolved record fields to values, ANDed
    (see :func:`_matches_where`). ``corpus_mode`` picks the instrument and is
    reported back: only ``"vector"`` asks the whole corpus.

    Returns ``{round_id, corpus_mode, status, where, n_records,
    n_with_neighbour, percentile_method, min, p50, p90, p95, max,
    per_record, reference_snapshot}``, ``per_record`` sorted by idea id."""
    _require_query_embed_backend(store, corpus_mode=corpus_mode, action="lens screen (--baseline)")
    model_key, backend = retrieve_engine._resolve_embed_backend(store, side="query")
    # Once for the whole pass, not once per record: the knob is a property
    # of the program (lane FB-7 fix pass, V-1).
    config = program_config(store)

    records = [i for i in _load_ideas(store, round_id=round_id)]
    if status:
        records = [i for i in records if str(i.get("status") or "") == str(status)]
    if where:
        records = [i for i in records if _matches_where(i, where)]
    records.sort(key=lambda i: str(i["idea_id"]))

    # The cache first, the backend only for what it does not hold -- the
    # same read/miss/fill shape :func:`_archive_rows` uses, for the same
    # reason: an archived round's statements never change, so a second
    # baseline read over the same round embeds nothing at all.
    digests = {str(i["idea_id"]): statement_digest(_statement(i)) for i in records}
    cached = (
        {} if reembed
        else fetch_idea_vectors(store, model_key=model_key, idea_ids=sorted(digests))
    )
    misses = [i for i in records if (cached.get(str(i["idea_id"])) or {}).get("statement_sha256") != digests[str(i["idea_id"])]]
    if misses:
        fresh = backend.embed_batch([_statement(i) for i in misses], kind="document")
        written: dict[str, tuple[str, Sequence[float]]] = {}
        for idea, vector in zip(misses, fresh):
            idea_id = str(idea["idea_id"])
            cached[idea_id] = {"statement_sha256": digests[idea_id], "vector": list(vector)}
            written[idea_id] = (digests[idea_id], list(vector))
        store_idea_vectors(store, model_key=model_key, vectors=written)

    # ONE read of the vector table for the whole pass, not one per record
    # (lane FB-7 fix pass, V-2). Lazy, so a mode that never asks the corpus
    # -- and a round with no records -- reads nothing at all.
    corpus = CorpusVectorTable(store, model_key=model_key, config=config)

    per_record: list[dict[str, Any]] = []
    cosines: list[float] = []
    for idea in records:
        idea_id = str(idea["idea_id"])
        vector = (cached.get(idea_id) or {}).get("vector")
        nearest = _nearest_corpus_neighbour(
            store, vector=vector, statement=_statement(idea), model_key=model_key,
            mode=corpus_mode, k=corpus_k, config=config, corpus=corpus,
        )
        row = {
            "idea_id": idea_id,
            "nearest_chunk_id": None,
            "nearest_doc_id": None,
            "cosine": None,
        }
        if nearest is not None:
            row.update(nearest)
            cosines.append(float(nearest["cosine"]))
        per_record.append(row)
    cosines.sort()

    return {
        "round_id": round_id,
        "corpus_mode": corpus_mode,
        "status": status,
        "where": dict(where or {}),
        "model_key": model_key,
        "n_records": len(records),
        "n_with_neighbour": len(cosines),
        "percentile_method": PERCENTILE_METHOD,
        "min": round(cosines[0], 6) if cosines else None,
        "p50": _percentile(cosines, 0.50),
        "p90": _percentile(cosines, 0.90),
        "p95": _percentile(cosines, 0.95),
        "max": round(cosines[-1], 6) if cosines else None,
        "per_record": per_record,
        "reference_snapshot": reference_snapshot(store, round_id=round_id, model_key=model_key),
    }


#: What a calibration's verdict rows are stamped with. A separate
#: ``procedure_version`` from :data:`PROCEDURE_VERSION`, not a flag on the
#: same one: a calibration's labels are about PLANTS, no record is
#: consolidated on them, and a later count of "how did this round's judge
#: label its records" must not sweep them in. It also keeps the
#: one-submission rule (:func:`_existing_novelty_verdicts`, which filters on
#: the version) from reading a calibration as a round's first submission.
CALIBRATION_PROCEDURE_VERSION = "novelty-v2-calibration"

#: The two judges a calibration compares, in the order the card reports them.
CALIBRATION_JUDGES: tuple[str, ...] = ("a", "b")


def _next_calibration_id(base: Path) -> str:
    existing = list((base / "judged").glob("calibration-*.json")) if (base / "judged").is_dir() else []
    return f"calibration-{len([p for p in existing if not p.stem.endswith('-card')])}"


def build_calibration_batch(
    store: Store,
    *,
    round_id: str,
    external_plants: Any,
    seed: str,
    dossiers: Mapping[str, Mapping[str, Any]] | None = None,
    judged_sets: Sequence[str] | None = None,
    label_vocabularies: Mapping[str, Any] | None = None,
    batch_fail_on: Sequence[str] | None = None,
    family_of: Callable[[Mapping[str, Any]], str] | None = None,
    batch_id: str | None = None,
    out_dir: Path | str | None = None,
    corpus_mode: str = DEFAULT_CORPUS_MODE,
    archive_rows_k: int = DEFAULT_ARCHIVE_ROWS_K,
    inventory_rows_k: int = DEFAULT_INVENTORY_ROWS_K,
    reembed_archive: bool = False,
) -> dict[str, Any]:
    """A judged batch of PLANTS ONLY, for calibrating the instrument before
    a single real record exists.

    The chicken-and-egg this closes: the plant battery is what makes a
    round's labels trustworthy, and until now the only way to run it was to
    run a round -- so the first batch a programme ever judged was also the
    first test of whether its judges could judge. A calibration is the same
    envelope, the same masking, the same per-kind scoring, over plants the
    round declares and nothing else. Two judges label it, and the card
    (:func:`record_calibration`) says whether they agree and whether they
    catch what they were handed.

    No records, therefore no scope and nothing to consolidate: the batch
    carries the empty scope shape so every reader that walks a batch keeps
    working, and :func:`record_calibration` never touches an ``idea`` row.
    ``dossiers`` is optional and is used for two things only -- donor fields
    for a plant that declares none, and the baseline cosine distribution the
    card reports.

    The fail-kind refusal :func:`build_judged_batch` makes does NOT apply
    here, and the difference is the point: a judged batch that cannot fail
    its audit consolidates records on an unchecked judge, while a calibration
    that catches nothing has simply measured a judge that catches nothing --
    which is the finding."""
    declared = normalize_judged_sets(judged_sets)
    vocabularies = (
        load_label_vocabularies(label_vocabularies, judged_sets=declared)
        if label_vocabularies is not None and not label_vocabularies.get("sha256")
        else label_vocabularies
    )
    judge_labels = label_block_for_judge(vocabularies)
    fail_on = normalize_fail_kinds(batch_fail_on)
    declarations = load_external_plants(
        external_plants, judged_sets=declared, label_vocabularies=vocabularies
    )
    dossiers = dossiers or {}
    # The batch id has to be resolved before the plants are filtered by it
    # (lane FB-7 item 5), which means reading the round's directory here
    # rather than where the batch dict is built.
    base = Path(out_dir) if out_dir is not None else round_dir(store.program_root, round_id)
    this_batch_id = batch_id or _next_calibration_id(base)
    # A calibration is filtered by `batch` on exactly the same rule as a
    # judged batch. The alternative -- one file per battery -- is the manual
    # splitting item 5 exists to remove, and a plants file that is handed to
    # --calibration and then to --judged-prep must not mean two different
    # things depending on which verb read it.
    declarations, batch_warnings = plants_for_batch(declarations, batch_id=this_batch_id)

    _require_query_embed_backend(store, corpus_mode=corpus_mode, action="lens screen (--calibration)")
    model_key, backend = retrieve_engine._resolve_embed_backend(store, side="query")
    inventory = [r for r in _inventory_rows(store, model_key=model_key, family_of=family_of) if r["vector"]]
    archive = (
        _archive_rows(store, backend=backend, model_key=model_key, reembed=reembed_archive)
        if "R2" in declared else []
    )

    plants = build_external_plants(
        store, declarations=declarations, dossiers=dossiers, seed=seed, inventory=inventory,
        archive=archive, backend=backend, model_key=model_key, judged_sets=declared,
        inventory_rows_k=inventory_rows_k, archive_rows_k=archive_rows_k, corpus_mode=corpus_mode,
    )
    envelopes = [
        _envelope(
            subject_id=plant["plant_id"],
            record=plant["record"],
            statement=plant["record"]["statement"],
            inventory_rows=plant.get("inventory_rows") or (),
            retrieved=plant.get("retrieved") or (),
            archive_rows=plant.get("archive_rows") or (),
            judged_sets=declared,
            label_block=judge_labels,
        )
        for plant in plants
    ]
    derive_rng(seed, salt="envelope-order").shuffle(envelopes)
    judge_views, mask = build_judge_views(envelopes, seed=seed)

    batch = {
        "round_id": round_id,
        "batch_id": this_batch_id,
        "plants_injected": sorted(str(d["plant_id"]) for d in declarations),
        "warnings": list(batch_warnings),
        "calibration": True,
        "procedure": PROCEDURE,
        "procedure_version": CALIBRATION_PROCEDURE_VERSION,
        "seed": seed,
        "judged_sets": list(declared),
        "batch_fail_on": list(fail_on),
        "reference_snapshot": reference_snapshot(store, round_id=round_id, model_key=model_key),
        # The empty scope, in the shape every batch reader already walks. A
        # calibration judges no record, and saying so with the real keys is
        # what keeps `score_plants` and `record_calibration` from needing a
        # second code path.
        "scope": {
            "flagged": [], "retrieval_hits": [], "sampled": [], "scope": [], "unjudged": [],
            "sample_fraction": 0.0, "seed": seed,
        },
        "plants": plants,
        "n_inventory_plants": sum(1 for p in plants if p["kind"] == "inventory"),
        "n_paraphrase_plants": sum(1 for p in plants if p["kind"] == "paraphrase"),
        "envelopes": envelopes,
        "judge_views": judge_views,
        "mask": mask,
        "second_judge": [],
        # Over the PLANTS, not the dossiers: a calibration exists for the
        # moment before a round has records, and "over: records, n: 0" is a
        # baseline over nothing on the card whose job is to say what a cosine
        # means here (lane FB-6 item 5).
        "baseline_cosine_distribution": baseline_cosine_distribution(plants=plants),
        "ts": now(),
    }
    if vocabularies is not None:
        batch["label_vocabularies"] = vocabularies
        batch["labels_sha256"] = vocabularies["sha256"]
    (base / "judged").mkdir(parents=True, exist_ok=True)
    (base / "judged" / f"{batch['batch_id']}.json").write_text(
        json.dumps(batch, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n",
    )
    return batch


def _rated_pair_cosines(
    store: Store,
    *,
    batch: Mapping[str, Any],
    pair_ratings: Sequence[Mapping[str, Any]],
    backend: Any,
) -> tuple[list[dict[str, Any]], list[str]]:
    """``([{a, b, human, cosine}], warnings)`` for each rated pair, embedding
    whatever each id names -- a plant in this batch or an ``idea`` row.

    A pair naming something neither is a refusal, not a dropped row: a
    correlation computed over the pairs that happened to resolve is a
    different statistic from the one the round asked for, and nothing on the
    card would say which it got.

    An EXTRA key is the opposite case and is treated as one: this file is
    written by a person, so a ``pair_id`` or a ``why`` beside the three
    fields read here is a note, not an error, and refusing the file over it
    cost a live round a round-trip. It is ignored and named in
    ``warnings`` -- named because a misspelt ``human`` is also an extra key,
    and silence would drop a rating without saying so."""
    statements: dict[str, str] = {
        str(p["plant_id"]): str(p["record"]["statement"]) for p in batch.get("plants") or []
    }
    warnings: list[str] = []
    resolved: list[dict[str, Any]] = []
    for index, rating in enumerate(pair_ratings):
        where = f"pair ratings[{index}]"
        if not isinstance(rating, Mapping):
            raise NoveltyError(f"{where} is a {type(rating).__name__}, not an object")
        # Lane FB-6 item 6: a person writes this file by hand, and a
        # `pair_id` or a `why` beside the three fields this reads is a note
        # to a later reader, not a mistake. Ignored and NAMED -- silence
        # would let a misspelt `human` ("humna") pass as an extra key and
        # take the rating with it.
        ignored = sorted(set(rating) - {"a", "b", "human"})
        if ignored:
            warnings.append(
                f"{where}: ignored extra field(s) {ignored!r}; a pair is read for ['a', 'b', 'human']"
            )
        texts = []
        for side in ("a", "b"):
            ref = str(rating.get(side) or "").strip()
            if not ref:
                raise NoveltyError(f"{where} has no {side!r}")
            if ref in statements:
                texts.append(statements[ref])
                continue
            idea = read_idea(store, idea_id=ref)
            if idea is None:
                raise NoveltyError(
                    f"{where} names {ref!r} on side {side!r}, which is neither a plant in this calibration "
                    "batch nor an idea row. A correlation over the pairs that happened to resolve is a "
                    "different statistic from the one asked for"
                )
            texts.append(_statement(idea))
        try:
            human = float(rating.get("human"))
        except (TypeError, ValueError) as exc:
            raise NoveltyError(f"{where} has no numeric 'human' rating") from exc
        if not 0.0 <= human <= 1.0:
            raise NoveltyError(f"{where} human rating {human} is outside 0..1")
        left, right = backend.embed_batch(texts, kind="document")
        resolved.append(
            {
                "a": str(rating["a"]), "b": str(rating["b"]), "human": human,
                "cosine": round(cosine_similarity(list(left), list(right)), 6),
            }
        )
    return resolved, warnings


def prereg_stamp(
    store: Store,
    *,
    prereg_id: str | None,
    executed_procedure: str | None = None,
    executed_params: Mapping[str, Any] | None = None,
) -> tuple[bool | None, str, dict[str, Any] | None]:
    """``(prereg_compliant, prose, detail)`` for a set of rows about to be
    written under a pre-registration.

    One helper for both recording paths (lane FB-8b item 5). The round path
    computed this inline and the calibration path did not compute it at all:
    ``lens screen --record-calibration --prereg-id … --executed-procedure-file
    …`` linked ``prereg_id`` onto every verdict row and left
    ``prereg_compliant`` NULL, so a calibration recorded under a procedure
    that had drifted was indistinguishable from one recorded under the
    procedure as committed. A copy of the round's four lines would have drifted
    from it the first time either was touched, so this is the shared one.

    **It never refuses.** A recording is evidence of what happened, and a
    non-compliant recording is the case the field exists to record --
    refusing it would delete the only trace that the procedure moved.
    ``prereg_compliant=False`` rows are written, exactly like compliant ones,
    and the prose says **which half** disagreed: a bare ``false`` once sent a
    round hunting for a procedure change when what had happened was a
    trailing newline stripped off a ``$(cat file)`` argument.

    The three states, and they are three rather than two:

    * no ``prereg_id`` -- ``None``, as it always was. Nothing was promised.
    * a ``prereg_id`` and no executed procedure -- ``None``, with the prose
      naming the argument that would stamp it. Not "compliant by default":
      nothing was checked.
    * both -- ``True``/``False`` from a recomputed hash, with the full
      :func:`~trialerror.verify.prereg.prereg_compliance_detail` beside it.

    **A VOIDED pre-registration is the fourth answer and is not a refusal**
    (this lane's probe (c)). Compliance with a voided commitment is undefined,
    and :func:`prereg_compliance_detail` says so by raising -- which, on the
    calibration path, would have turned a recording that WAS accepted before
    this item (``prereg_id`` linked, column NULL) into a refusal, throwing
    away two judges' labels over a column. So it is caught and reported as
    "not stamped", with the reason: the rows land exactly as they used to, and
    now say why nothing was claimed. A prereg that does not exist at all is
    NOT caught here -- it is a bad argument rather than an undefined question,
    and the row write refuses it anyway (the ``verdict.prereg_id`` XID), so
    letting it out lets the caller refuse it before anything is written.
    """
    if prereg_id is None:
        return None, "no prereg_id given", None
    if executed_procedure is None:
        return (
            None,
            "not stamped: pass executed_procedure (and executed_params) -- the round's own "
            "pre-registered procedure, which the screen does not hold",
            None,
        )
    try:
        detail = prereg_compliance_detail(
            store,
            prereg_id=prereg_id,
            executed_procedure=executed_procedure,
            executed_params=executed_params,
        )
    except PreregVoidedError as exc:
        return None, f"not stamped: {exc}", None
    compliant = bool(detail["compliant"])
    if compliant:
        return compliant, "recomputed against the executed procedure: both hashes match", detail
    # Which half moved, by name.
    prose = (
        "NOT compliant: "
        + " and ".join(
            f"the {axis} hash disagrees "
            f"(committed {detail['committed_' + axis + '_sha256'][:12]}, "
            f"executed {detail['executed_' + axis + '_sha256'][:12]})"
            for axis in detail["mismatched"]
        )
        + ". Pass the procedure BYTE-EXACT (--executed-procedure-file hashes the file as it is on "
        "disk; a shell $(cat file) strips its trailing newline)"
    )
    return compliant, prose, detail


def record_calibration(
    store: Store,
    *,
    round_id: str,
    batch: Mapping[str, Any],
    labels_a: Mapping[str, Mapping[str, Any]],
    labels_b: Mapping[str, Mapping[str, Any]],
    issued_by_launch: str,
    launch_a: str | None = None,
    launch_b: str | None = None,
    pair_ratings: Sequence[Mapping[str, Any]] | None = None,
    label_vocabularies: Mapping[str, Any] | None = None,
    batch_fail_on: Sequence[str] | None = None,
    dossiers: Mapping[str, Mapping[str, Any]] | None = None,
    prereg_id: str | None = None,
    executed_procedure: str | None = None,
    executed_params: Mapping[str, Any] | None = None,
    supersede: bool = False,
    out_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Score two judges against one calibration batch and write the card.

    The card answers three questions a round has to answer before its labels
    mean anything, and keeps them apart:

    * **Do the judges catch what they were handed?** ``catch_by_kind``, per
      judge, from the same :func:`score_plants` the real batches use.
    * **Do they agree with each other?** ``kappa_by_set``, Cohen's kappa per
      declared set over the plants BOTH labelled, with ``n`` and the observed
      agreement beside it -- never the number alone, because a kappa over
      four subjects is a number and not a finding.
    * **Does the embedding agree with a person?** ``r_embedding_human``,
      Pearson *r* between each rated pair's embedding cosine and its human
      rating, when ``pair_ratings`` is given. This is the one number that
      says whether the mechanical half's distances track the judgement the
      round actually cares about, and it is absent rather than assumed when
      nobody rated anything.

    ``baseline_cosine_distribution`` rides on the card beside them, because
    every one of those three is read against "what does a cosine of 0.61
    mean in this corpus".

    **Nothing is consolidated and no ``idea`` row is touched.** A calibration
    judges plants; there is no record whose status it could advance. The
    verdict rows it writes are stamped
    :data:`CALIBRATION_PROCEDURE_VERSION` so a later count of the round's own
    labels cannot sweep them in.

    **One submission, like the round's own** (lane FB-5 stage 3, N2). The
    card file is rewritten in place on a re-run, but the verdict rows are
    not: a second call used to append a second full set of them, leaving two
    rows per plant per judge with nothing joining them -- precisely the shape
    :func:`record_novelty_verdicts` refuses under design 5.2(3), and a shape
    only a later count of the calibration rows would notice. A re-run is
    refused unless ``supersede=True``, and the rows it then writes name the
    rows they supersede.

    **``prereg_compliant`` is stamped here the way the round path stamps it**
    (lane FB-8b item 5, :func:`prereg_stamp`). Passing ``prereg_id`` alone
    used to link the pre-registration onto every row and leave the compliance
    column NULL, which reads as "not under a prereg" rather than "under one,
    unchecked" -- so a calibration run against a procedure that had drifted
    looked exactly like one run against the procedure as committed. Give
    ``executed_procedure`` (and ``executed_params``) and the hashes are
    recomputed; a mismatch is RECORDED, never refused, and the card's
    ``prereg_compliance`` says which of the two halves disagreed."""
    declared = normalize_judged_sets(batch.get("judged_sets"))
    vocabularies = label_vocabularies if label_vocabularies is not None else batch.get("label_vocabularies")
    if vocabularies is not None and not vocabularies.get("sha256"):
        vocabularies = load_label_vocabularies(vocabularies, judged_sets=declared)
    batch_hash = batch.get("labels_sha256")
    if vocabularies is not None and batch_hash and vocabularies["sha256"] != batch_hash:
        raise NoveltyError(
            "record_calibration: the labels file given here hashes "
            f"{vocabularies['sha256'][:12]} but batch {batch['batch_id']!r} was judged under "
            f"{str(batch_hash)[:12]}. Both judges answered in the vocabulary they were shown"
        )
    plant_ids = {p["plant_id"] for p in batch.get("plants") or []}
    if not plant_ids:
        raise NoveltyError(
            f"record_calibration: batch {batch['batch_id']!r} carries no plants, so there is nothing to "
            "calibrate against. Build it with --calibration --plants-file"
        )

    # Scoped to THIS round and THIS batch (lane FB-7 item 9): a plant id is
    # whatever the round's plants file called it, so an earlier battery that
    # happened to reuse a name is not this battery's second submission.
    prior = _existing_novelty_verdicts(
        store, sorted(plant_ids), procedure_version=CALIBRATION_PROCEDURE_VERSION,
        round_id=round_id, batch_id=str(batch["batch_id"]),
    )
    if prior and not supersede:
        raise NoveltyError(
            f"record_calibration: plant(s) "
            f"{sorted({subject for subject, _ in prior})!r} already carry a "
            f"{CALIBRATION_PROCEDURE_VERSION} verdict in round(s) {_rounds_named(prior)!r}, batch "
            f"{batch['batch_id']!r}. A second call would write a full second set of rows beside the "
            "first with nothing joining them -- two rows per plant per judge, and only a later count of "
            "them would notice. Pass supersede=True if this re-scoring is deliberate; the new rows will "
            "name the rows they replace, and only rows of THIS round and batch are replaced"
        )
    superseded: list[str] = sorted(
        r["verdict_id"] for rows in prior.values() for r in rows
    ) if supersede else []

    sheets: dict[str, dict[str, Any]] = {}
    for judge, raw in zip(CALIBRATION_JUDGES, (labels_a, labels_b)):
        given = unmask_labels(raw, batch.get("mask"))
        unknown = sorted(set(given) - plant_ids)
        if unknown:
            raise NoveltyError(
                f"record_calibration: judge {judge.upper()}'s sheet names subject(s) {unknown!r} that are "
                "not plants in this calibration batch"
            )
        for subject, entry in given.items():
            for reference_set in declared:
                label = entry.get(REFERENCE_SETS[reference_set])
                if label is None:
                    continue
                vocabulary = accepted_labels_for_set(vocabularies, reference_set=reference_set)
                if label not in vocabulary:
                    raise NoveltyError(
                        f"record_calibration: judge {judge.upper()} labelled {subject!r} "
                        f"{label!r} for {reference_set}, which is not one of {list(vocabulary)!r}"
                    )
        sheets[judge] = given

    scored = {
        judge: score_plants(
            batch, sheet, judged_sets=declared, batch_fail_on=batch_fail_on,
            label_vocabularies=vocabularies,
        )
        for judge, sheet in sheets.items()
    }
    kappa_by_set: dict[str, Any] = {}
    confusion_by_set: dict[str, Any] = {}
    disagreements_by_set: dict[str, list[dict[str, Any]]] = {}
    # Real id -> the id the JUDGES saw. The mask is stored the other way
    # round (masked -> real) because that is the direction a returned sheet
    # is translated in; a disagreement is reported in the judge's direction
    # because that is the envelope somebody will open.
    masked_as = {real: masked for masked, real in (batch.get("mask") or {}).items()}
    for reference_set in declared:
        key = REFERENCE_SETS[reference_set]
        # The unscreenable word is a category of its own, not a gap: two
        # judges that both answer it AGREE, and an expected-agreement
        # computed over a category list that omits it is a kappa about a
        # different vocabulary from the one they answered in.
        categories = accepted_labels_for_set(vocabularies, reference_set=reference_set)
        first = {s: v[key] for s, v in sheets["a"].items() if v.get(key) is not None}
        second = {s: v[key] for s, v in sheets["b"].items() if v.get(key) is not None}
        kappa_by_set[reference_set] = cohens_kappa(first, second, categories=categories)
        # What a kappa reader needs BESIDE the kappa (lane FB-7 item 3): the
        # matrix that says which label pair the two judges read differently,
        # and the item list to go and look at.
        confusion_by_set[reference_set], disagreements_by_set[reference_set] = confusion_table(
            first, second, categories=categories, masked_as=masked_as
        )

    rated: list[dict[str, Any]] = []
    warnings: list[str] = []
    r_embedding_human: dict[str, Any] | None = None
    if pair_ratings:
        _require_query_embed_backend(
            store, corpus_mode=DEFAULT_CORPUS_MODE, action="lens screen (--record-calibration, pair ratings)"
        )
        _model_key, backend = retrieve_engine._resolve_embed_backend(store, side="query")
        rated, pair_warnings = _rated_pair_cosines(
            store, batch=batch, pair_ratings=pair_ratings, backend=backend
        )
        warnings.extend(pair_warnings)
        r_embedding_human = pearson_r([{"x": p["cosine"], "y": p["human"]} for p in rated])

    misses_by_id: dict[str, dict[str, Any]] = {}
    for plant_id in sorted(plant_ids):
        entry = {}
        for judge in CALIBRATION_JUDGES:
            miss = next((m for m in scored[judge]["missed"] if m["plant_id"] == plant_id), None)
            if miss is not None:
                entry[judge] = {"label": miss["label"], "expected": miss["expected"],
                                "reference_set": miss.get("reference_set")}
            elif plant_id in scored[judge]["unlabelled"]:
                entry[judge] = {"label": None, "expected": None, "reference_set": None}
        if entry:
            misses_by_id[plant_id] = entry

    card = {
        "round_id": round_id,
        "batch_id": batch["batch_id"],
        "procedure": PROCEDURE,
        "procedure_version": CALIBRATION_PROCEDURE_VERSION,
        "judged_sets": list(declared),
        "batch_fail_on": scored["a"]["batch_fail_on"],
        "catch_by_kind": {judge: scored[judge]["by_kind"] for judge in CALIBRATION_JUDGES},
        "catch_by_set": {judge: scored[judge]["by_set"] for judge in CALIBRATION_JUDGES},
        "catch_rate": {judge: scored[judge]["catch_rate"] for judge in CALIBRATION_JUDGES},
        "kappa_by_set": kappa_by_set,
        # Lane FB-7 item 3. Pure additions -- nothing above them moved, and
        # no schema changed: a card is a file, and a reader that only knew
        # the old keys still finds every one of them where it was.
        "confusion": confusion_by_set,
        "disagreements": disagreements_by_set,
        "by_expected": {
            judge: catch_by_expected(batch, sheets[judge], scored[judge], declared=declared)
            for judge in CALIBRATION_JUDGES
        },
        # The same table grouped by the plants' own class tag (lane FB-7
        # item 4), which is the grouping a round that built a balanced
        # present/adjacent/absent battery actually wants to read. Empty
        # when the plants file declared no classes -- an absent grouping
        # rather than a table of one group called "everything".
        "by_class": {
            judge: catch_by_expected(
                batch, sheets[judge], scored[judge], declared=declared, group_by="class"
            )
            for judge in CALIBRATION_JUDGES
        },
        "r_embedding_human": r_embedding_human,
        "rated_pairs": rated,
        # Everything this call accepted but did not read. Empty on every card
        # where nothing was ignored, so a non-empty list is always something
        # the round's author should look at once.
        "warnings": warnings,
        "misses_by_id": misses_by_id,
        # The plants this batch was built from, recomputed here rather than
        # copied off the batch so a card scored against an edited batch file
        # reports that file's plants. ``dossiers`` still overrides, for a
        # caller that wants the ROUND's records as the baseline instead.
        "baseline_cosine_distribution": (
            baseline_cosine_distribution(dossiers) if dossiers is not None
            else baseline_cosine_distribution(plants=batch.get("plants") or ())
            if batch.get("plants")
            else batch.get("baseline_cosine_distribution")
        ),
        "labels_sha256": (vocabularies or {}).get("sha256"),
        "ts": now(),
    }

    # Recomputed BEFORE the rows are written, because every row this call
    # writes carries the answer. Never a refusal: see `prereg_stamp`.
    prereg_compliant, prereg_compliance, prereg_detail = prereg_stamp(
        store,
        prereg_id=prereg_id,
        executed_procedure=executed_procedure,
        executed_params=executed_params,
    )
    card["prereg_id"] = prereg_id
    card["prereg_compliant"] = prereg_compliant
    card["prereg_compliance"] = prereg_compliance
    card["prereg_compliance_detail"] = prereg_detail

    snapshot = batch.get("reference_snapshot") or {}
    snapshot_note = " ".join(
        f"{name}={snapshot[name]['sha256'][:12]}" for name in ("R1", "R2", "R3", "R4") if name in snapshot
    ) or "reference snapshot not carried on this batch"
    written: list[dict[str, Any]] = []
    for judge, launch in zip(CALIBRATION_JUDGES, (launch_a or issued_by_launch, launch_b or issued_by_launch)):
        for plant_id in sorted(sheets[judge]):
            for reference_set in declared:
                label = sheets[judge][plant_id].get(REFERENCE_SETS[reference_set])
                if label is None:
                    continue
                expected = expected_labels_for_set(
                    next(p for p in batch["plants"] if p["plant_id"] == plant_id),
                    reference_set,
                    primary="R3" if "R3" in declared else declared[0],
                )
                written.append(
                    record_verdict(
                        store,
                        subject_kind="claim",
                        subject_id=plant_id,
                        procedure=PROCEDURE,
                        procedure_version=CALIBRATION_PROCEDURE_VERSION,
                        label=f"{reference_set}:{label}",
                        label_canonical=(
                            f"{reference_set}:"
                            + canonical_label(vocabularies, reference_set=reference_set, label=label)
                        ),
                        evidence=[
                            {"note": f"calibration judge {judge.upper()} (launch {launch})"},
                            {"note": f"expected {expected!r}" if expected else "no expectation for this set"},
                            {"note": f"reference_snapshot {snapshot_note}"},
                            *(
                                [{"note": "supersedes " + ", ".join(
                                    r["verdict_id"] for r in prior.get((plant_id, reference_set), [])
                                )}]
                                if supersede and prior.get((plant_id, reference_set))
                                else []
                            ),
                        ],
                        prereg_id=prereg_id,
                        # Lane FB-8b item 5: stamped, not left NULL. A row
                        # that names a pre-registration and says nothing
                        # about whether it was followed is the shape a reader
                        # mistakes for "no prereg at all".
                        prereg_compliant=prereg_compliant,
                        # Lane FB-7 item 9: the row says which round and
                        # batch produced it, so the next battery's guard can
                        # tell "this plant id already answered HERE" from
                        # "another round once used this name".
                        round_id=round_id,
                        batch_id=str(batch["batch_id"]),
                        issued_by_launch=launch,
                    )
                )
    card["n_verdicts"] = len(written)
    card["superseded"] = superseded
    card["verdicts"] = written

    base = Path(out_dir) if out_dir is not None else round_dir(store.program_root, round_id)
    (base / "judged").mkdir(parents=True, exist_ok=True)
    (base / "judged" / f"{batch['batch_id']}-card.json").write_text(
        json.dumps(
            {k: v for k, v in card.items() if k != "verdicts"}, indent=2, ensure_ascii=False, sort_keys=True
        ) + "\n",
        encoding="utf-8", newline="\n",
    )
    return card


#: The qualifier an unjudged record carries beside :data:`UNJUDGED_LABEL`.
#: Printed together, always: "no-close-neighbour" is a statement about
#: retrieval, and without "unjudged" beside it a reader supplies the wrong
#: second half.
UNJUDGED_QUALIFIER = "unjudged"

#: Share of a judged scope that may come back unlabelled before the batch is
#: caveated. A judge that simply omits a subject has answered nothing about
#: it, and above this share the batch's label distribution is not the
#: distribution it was scoped to be.
UNLABELLED_SCOPE_CAVEAT_FRACTION = 0.10

#: The reference sets this module writes a ``verdict`` row for are the ones
#: the BATCH declared -- see :data:`REFERENCE_SETS` and
#: :func:`normalize_judged_sets`, which is where that vocabulary now lives.


def _existing_novelty_verdicts(
    store: Store,
    subject_ids: Sequence[str],
    *,
    procedure_version: str = PROCEDURE_VERSION,
    round_id: str | None = None,
    batch_id: str | None = None,
) -> dict[tuple[str, str], list[dict[str, str]]]:
    """``{(subject_id, reference_set): [{verdict_id, label, round_id,
    batch_id}, ...]}`` for the rows of ``procedure_version`` this store
    already holds for those subjects, IN THIS ROUND AND BATCH.

    Keyed by reference set because that is the grain the one-submission rule
    is about: an idea may legitimately carry an R3 row and an R4 row, and
    must not carry two R3 rows nobody ordered.

    ``procedure_version`` is a parameter because a calibration's rows are
    stamped :data:`CALIBRATION_PROCEDURE_VERSION` and are invisible to the
    round's own rule by design -- and were invisible to every rule, which is
    how a calibration could be recorded twice (lane FB-5 stage 3, N2).

    **Scoped to the round, and optionally to the batch** (lane FB-7 item 9,
    knowledge-v12). Keying on the subject alone is right for a RECORD, whose
    ``idea_id`` is unique across the programme, and wrong for a PLANT, whose
    ``plant_id`` is whatever the round's plants file called it. A live
    supplement battery was refused because twelve of its plants re-used an
    earlier battery's ids, and the only offered way through --
    ``--supersede`` -- would have marked the EARLIER round's rows as
    replaced. A verdict of another round now neither blocks nor is
    superseded.

    **``batch_id`` is a calibration's grain, not a record's** (lane FB-7 fix
    pass, V-3). :func:`record_calibration` passes it, because its subjects
    are plant ids and two batteries of one round legitimately re-use them.
    :func:`record_novelty_verdicts` does NOT, because its subjects are
    records: a round that preps two judged batches would otherwise be able
    to record two contradicting labels for one idea, silently, which is
    design 5.2(3) undone by a key.

    **A NULL round is UNKNOWN, not "no round".** A row written before the
    migration cannot say which round it belongs to, so it may be this one's
    and it still blocks -- which is precisely the behaviour it had before.
    Only rows that positively name a DIFFERENT round are excluded. The same
    rule applies to ``batch_id``, one level down.

    ``round_id`` omitted means "every round", which is the whole-store
    lookup this function used to be and is what a caller with no round in
    hand still gets.
    """
    ids = [s for s in dict.fromkeys(subject_ids) if s]
    if not ids:
        return {}
    ph = ",".join("?" for _ in ids)
    where = [
        "procedure = ?", "procedure_version = ?", f"subject_id IN ({ph})",
    ]
    params: list[Any] = [PROCEDURE, procedure_version, *ids]
    if round_id is not None:
        where.append("(round_id IS NULL OR round_id = ?)")
        params.append(str(round_id))
    if batch_id is not None:
        where.append("(batch_id IS NULL OR batch_id = ?)")
        params.append(str(batch_id))
    rows = store.knowledge.execute(
        "SELECT verdict_id, subject_id, label, round_id, batch_id FROM verdict "
        f"WHERE {' AND '.join(where)} ORDER BY ts, verdict_id",
        params,
    ).fetchall()
    out: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in rows:
        reference_set = str(row["label"]).split(":", 1)[0]
        out.setdefault((row["subject_id"], reference_set), []).append(
            {
                "verdict_id": row["verdict_id"],
                "label": row["label"],
                "round_id": row["round_id"],
                "batch_id": row["batch_id"],
            }
        )
    return out


def _rounds_named(prior: Mapping[tuple[str, str], Sequence[Mapping[str, Any]]]) -> list[str]:
    """Which rounds the blocking rows belong to, for the refusal message.
    A row whose round is NULL is named ``"(unrecorded)"`` rather than
    dropped: "these rows are from a round nobody can name" is the thing the
    operator has to know before deciding whether to supersede them."""
    seen = {
        (r.get("round_id") or "(unrecorded)")
        for rows in prior.values() for r in rows
    }
    return sorted(seen)


def record_novelty_verdicts(
    store: Store,
    *,
    round_id: str,
    batch: Mapping[str, Any],
    labels: Mapping[str, Mapping[str, Any]],
    issued_by_launch: str,
    prereg_id: str | None = None,
    executed_procedure: str | None = None,
    executed_params: Mapping[str, Any] | None = None,
    second_judge_labels: Mapping[str, Mapping[str, Any]] | None = None,
    consolidate: bool = True,
    supersede: bool = False,
    out_dir: Path | str | None = None,
    label_vocabularies: Mapping[str, Any] | None = None,
    batch_fail_on: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Take the judge's discrete labels back and write them down.

    Order matters here and is not an implementation detail. Plants are
    scored FIRST. If an inventory plant was missed, the batch is
    ``reopened_with_caveat``: the verdict rows are still written (the labels
    happened and hiding them would be the silence the design forbids) and
    they carry the caveat, but nothing is consolidated -- an idea whose
    judge just failed an audit does not advance on that judge's word.

    Every row is ``procedure=custom``, ``procedure_version=novelty-v2``,
    with the round's ``prereg_id`` attached, and one row per idea per
    reference set: the inventory label and the literature label are
    different claims about different reference sets, so they are recorded as
    different verdicts and cite different evidence -- the R3 rows for the R3
    verdict, the R4 chunks for the R4 one.

    **Every survivor is consolidated, not only the judged ones.** The
    round's Phase 3 output is ``status='consolidated'`` for the batch, and
    Phase 5 rooms every consolidated idea; the design's own stepping-stone
    rule is that nothing is pruned on a proxy. So an idea outside the judged
    scope is consolidated too, carrying the mechanical
    ``no-close-neighbour . unjudged`` pair as its own verdict row -- which
    is what it means for that label to be recorded rather than assumed.
    Leaving those ideas ``raw`` silently pruned two thirds of a round behind
    a retrieval threshold and a 20% sample.

    An idea INSIDE the scope that the judge did not label is a different
    case and is not consolidated: it was routed to a judge and the judge
    said nothing, so there is no answer to record. Those ids are reported in
    ``unlabelled_scope``, and a share above
    :data:`UNLABELLED_SCOPE_CAVEAT_FRACTION` caveats the batch.

    **One submission per idea per judge** (design 5.2(3)). A subject that
    already carries a novelty-v2 verdict for a reference set IN THIS ROUND
    is refused unless ``supersede=True``, and only a record whose current
    status is ``raw`` is consolidated -- so a re-screen cannot write a
    contradicting row beside the first, and cannot revive a ``merged``
    record to ``consolidated`` without the ruling Phase 7 requires.

    **The round, not the round and the batch.** A second JUDGED BATCH of the
    same round is still a second submission about the same idea, so it is
    refused by the same rule and the refusal names the batches the prior
    rows came from. Only a verdict of ANOTHER round is invisible here -- that
    is what lane FB-7 item 9 scoped, and it is a claim about plant ids, which
    are a calibration's subjects and never a record's.

    ``prereg_compliant`` is stamped by recomputing the procedure hash when
    the caller passes the ``executed_procedure``/``executed_params`` the
    round actually ran (the charter's, which this module does not hold: its
    own parameters are not what the round pre-registered, and comparing them
    would produce a confident False). Without them the column stays NULL and
    ``prereg_compliance`` says why, rather than the omission being invisible.
    """
    scope = list(batch["scope"]["scope"])
    unjudged_scope = list(batch["scope"].get("unjudged") or [])
    plant_ids = {p["plant_id"] for p in batch["plants"]}
    # The instrument the judge was shown, off the batch itself. A batch
    # written before the sets were declarable carries no key, and the
    # default IS what those batches were built with -- so an old batch file
    # records exactly the rows it always did.
    declared = normalize_judged_sets(batch.get("judged_sets"))
    # The round's own label vocabulary, off the batch unless the caller
    # hands one in -- and when both are present they must be the SAME one:
    # validating labels against a vocabulary other than the one the judge
    # was shown is how a round discovers, months later, that its counts were
    # about a different question.
    vocabularies = label_vocabularies if label_vocabularies is not None else batch.get("label_vocabularies")
    if vocabularies is not None and not vocabularies.get("sha256"):
        vocabularies = load_label_vocabularies(vocabularies, judged_sets=declared)
    batch_hash = batch.get("labels_sha256")
    if vocabularies is not None and batch_hash and vocabularies["sha256"] != batch_hash:
        raise NoveltyError(
            "record_novelty_verdicts: the labels file given here hashes "
            f"{vocabularies['sha256'][:12]} but batch {batch['batch_id']!r} was judged under "
            f"{str(batch_hash)[:12]}. The judge answered in the vocabulary it was shown; validating those "
            "answers against a different one would record labels nobody returned"
        )
    if vocabularies is None and batch_hash:
        raise NoveltyError(
            f"record_novelty_verdicts: batch {batch['batch_id']!r} was judged under a round vocabulary "
            f"({str(batch_hash)[:12]}) that this batch file no longer carries. Re-prep the batch, or pass "
            "the labels file it was built from"
        )
    # The judge answered under masked ids if the prompt was built from this
    # batch's judge_views; translate before anything reads a key.
    labels = unmask_labels(labels, batch.get("mask"))
    if second_judge_labels is not None:
        second_judge_labels = unmask_labels(second_judge_labels, batch.get("mask"))
    unknown = sorted(set(labels) - set(scope) - plant_ids)
    if unknown:
        raise NoveltyError(
            f"record_novelty_verdicts: labels name subject(s) {unknown!r} that are neither in this "
            "batch's judged scope nor among its plants"
        )
    declared_keys = {REFERENCE_SETS[name]: name for name in declared}
    undeclared_keys = {key: name for name, key in REFERENCE_SETS.items() if key not in declared_keys}
    # Lane FB-5 item 5: the seed work a judge resolved per subject, validated
    # here with everything else so a malformed sheet refuses before a row is
    # written rather than after half of them are.
    seeds_by_subject = {
        subject: normalize_seeds(given.get("seeds"), subject_id=subject, vocabularies=vocabularies)
        for subject, given in labels.items()
    }
    for subject, given in labels.items():
        for key, reference_set in undeclared_keys.items():
            if given.get(key) is not None:
                raise NoveltyError(
                    f"{key} for {subject!r} is a label for reference set {reference_set}, which this batch "
                    f"did not declare (it declared {list(declared)!r}). The judge was never shown that "
                    "set's rows, so a label about it is a claim nothing backs"
                )
        for key, reference_set in declared_keys.items():
            label = given.get(key)
            if label is None:
                continue
            # The ROUND's vocabulary when it declared one, the design's
            # otherwise, plus the round's `unscreenable` word for every set
            # (lane FB-6 item 4). A judge answering outside that is refused
            # by name before anything is written.
            vocabulary = accepted_labels_for_set(vocabularies, reference_set=reference_set)
            if label not in vocabulary:
                raise NoveltyError(
                    f"{key} for {subject!r} must be one of {list(vocabulary)!r}, got {label!r}"
                )

    # --- one submission per idea per judge -------------------------------
    survivors = [*scope, *unjudged_scope]
    # Scoped to the ROUND, and deliberately NOT to the batch (lane FB-7 fix
    # pass, V-3). The plant-id collision item 9 exists for is across ROUNDS
    # -- two batteries in two rounds that both called a plant ``C-1`` -- and
    # a record's subject is an ``idea_id``, unique across the programme. Had
    # the batch stayed in this key, a round that preps two judged batches --
    # which is the shape item 5 of this same lane exists to support -- could
    # record the same idea twice, with no refusal, no supersede and nothing
    # joining the two rows. That is verbatim design 5.2(3)'s "one submission
    # per idea per judge", lost. ``record_calibration`` keeps the batch in
    # its key, because ITS subjects are plant ids.
    already = _existing_novelty_verdicts(store, survivors, round_id=round_id)
    if already and not supersede:
        clashes = sorted(
            f"{sid}/{rset}={rows[-1]['label']}" for (sid, rset), rows in already.items()
        )
        prior_batches = sorted({str(r.get("batch_id") or "(unrecorded)") for rows in already.values() for r in rows})
        raise NoveltyError(
            f"record_novelty_verdicts: subject(s) {clashes!r} already carry a {PROCEDURE_VERSION} verdict "
            f"for that reference set in round(s) {_rounds_named(already)!r}, batch(es) {prior_batches!r}. "
            "Design 5.2(3) is one submission per idea per judge: a second call would write a "
            "contradicting row beside the first rather than replacing it, and a second BATCH of the same "
            "round is still a second submission about the same idea. Pass supersede=True if this "
            "re-scoring is deliberate and the round records why; only rows of THIS round are replaced"
        )
    superseded: list[str] = sorted(
        r["verdict_id"] for rows in already.values() for r in rows
    ) if supersede else []

    # The scoring rule may be overridden here, unlike the declared sets and
    # the label vocabulary: which KINDS' misses fail a batch is a decision
    # about the battery, not something the judge was shown, so re-scoring an
    # already-judged batch under a different rule changes no answer. The
    # rule that was applied is reported.
    plants = score_plants(
        batch, labels, judged_sets=declared, batch_fail_on=batch_fail_on,
        label_vocabularies=vocabularies,
    )

    def _answered(subject_id: str) -> bool:
        """Did the judge return a LABEL for this subject, for any declared
        set?

        Asked about the labels rather than about the dict, because a sheet
        entry can now carry seed work and no label at all (item 5), and a
        non-empty dict with nothing but seeds in it is a subject the judge
        said nothing about -- which must not consolidate on the strength of
        having a key."""
        given = labels.get(subject_id) or {}
        return any(given.get(REFERENCE_SETS[name]) is not None for name in declared)

    unlabelled_scope = sorted(i for i in scope if not _answered(i))
    unlabelled_share = round(len(unlabelled_scope) / len(scope), 6) if scope else 0.0
    caveats: list[str] = []
    if plants["batch_failed"]:
        caveats.append("plants_failed")
    if scope and unlabelled_share > UNLABELLED_SCOPE_CAVEAT_FRACTION:
        caveats.append("unlabelled_scope")
    status = "reopened_with_caveat" if caveats else "screened"

    # What each subject's judge actually saw, taken off the envelopes this
    # very batch was built from -- not re-derived, so the evidence recorded
    # is the evidence shown.
    envelopes = {e["subject_id"]: e for e in (batch.get("envelopes") or [])}

    snapshot = batch.get("reference_snapshot") or {}
    snapshot_note = " ".join(
        f"{name}={snapshot[name]['sha256'][:12]}" for name in ("R1", "R2", "R3", "R4") if name in snapshot
    ) or "reference snapshot not carried on this batch"

    # The same check and the same words the calibration path stamps with
    # (lane FB-8b item 5) -- one helper, so the two cannot drift.
    prereg_compliant, prereg_compliance, prereg_detail = prereg_stamp(
        store,
        prereg_id=prereg_id,
        executed_procedure=executed_procedure,
        executed_params=executed_params,
    )

    def _evidence(reference_set: str, subject_id: str, label: str) -> list[dict[str, Any]]:
        """The rows the judge was shown FOR THIS REFERENCE SET. An R3
        verdict citing R4 chunks would be a claim about the inventory
        anchored in the corpus."""
        envelope = envelopes.get(subject_id) or {}
        evidence: list[dict[str, Any]] = []
        if reference_set == "R2":
            # An archive row is an idea ROW, not a chunk, so it cannot be
            # cited by `chunk_id` -- a citation resolver handed one would
            # look for it in the corpus and report a dangling anchor. It is
            # named in a note carrying its id and status instead, with the
            # same `stance` the chunk-shaped rows carry.
            evidence += [
                {"note": f"R2 archive row {row['idea_id']} (status {row.get('status')})", "stance": label}
                for row in (envelope.get("archive_rows") or []) if row.get("idea_id")
            ]
        elif reference_set == "R3":
            evidence += [
                {"chunk_id": row["row_id"], "stance": label}
                for row in (envelope.get("inventory_rows") or []) if row.get("row_id")
            ]
        else:
            external = 0
            for hit in envelope.get("retrieved") or []:
                if hit.get("chunk_id"):
                    evidence.append({"chunk_id": hit["chunk_id"], "stance": label})
                else:
                    external += 1
            if external:
                # R5 results are not chunks in this corpus, so they cannot be
                # cited by chunk_id. They are evidence for this same
                # literature label, and the count says so rather than
                # vanishing.
                provider = (snapshot.get("R5") or {}).get("provider", "none")
                evidence.append({"note": f"R5 {provider}: {external} external result(s) in this envelope"})
        # The seed work, on EVERY row for this subject rather than on one of
        # them. A verdict row is read on its own -- in a dossier, in a gate
        # review, in a report -- and "the judge resolved four seeds, three of
        # them on topic, before answering this" is part of what the answer
        # IS. Seeds are evidence, never a reference set: there is no
        # `label_seed` and no row of their own.
        seeds = seeds_by_subject.get(subject_id) or []
        if seeds:
            on_topic = on_topic_seed_label(vocabularies)
            evidence += [{"note": f"seed {seed['ref']}: {seed['label']}"} for seed in seeds]
            evidence.append(
                {"note": f"seed work: {sum(1 for s in seeds if s['label'] == on_topic)} {on_topic} "
                         f"of {len(seeds)} resolved"}
            )
        evidence.append({"note": f"reference_snapshot {snapshot_note}"})
        return evidence

    def _write(subject_id: str, reference_set: str, label: str, evidence: list[dict[str, Any]]) -> dict[str, Any]:
        prior = already.get((subject_id, reference_set)) or []
        if supersede and prior:
            # The verdict table is append-only, so the new row has to say
            # which rows it replaces; two rows for one subject and reference
            # set with nothing joining them is the contradiction the
            # one-submission rule exists to prevent.
            evidence = [
                *evidence,
                {"note": "supersedes " + ", ".join(r["verdict_id"] for r in prior)},
            ]
        suffix = f":{'+'.join(caveats)}" if caveats else ""
        # `label` is what the ROUND's judge returned, in the round's own
        # words. `label_canonical` is the same row in the design's fixed
        # vocabulary and in the SAME composite shape, so a reader that
        # parses `label` today can be pointed at `label_canonical` and keep
        # working unchanged -- which is the whole reason the canonical half
        # exists. A round that declared no vocabulary writes the two equal.
        canonical = canonical_label(vocabularies, reference_set=reference_set, label=label)
        return record_verdict(
            store,
            subject_kind="claim",
            subject_id=subject_id,
            procedure=PROCEDURE,
            procedure_version=PROCEDURE_VERSION,
            label=f"{reference_set}:{label}" + suffix,
            label_canonical=f"{reference_set}:{canonical}" + suffix,
            evidence=evidence,
            prereg_id=prereg_id,
            prereg_compliant=prereg_compliant,
            # Lane FB-7 item 9: the row says which round and batch produced
            # it, so a later guard can scope itself to them.
            round_id=round_id,
            batch_id=str(batch["batch_id"]),
            issued_by_launch=issued_by_launch,
        )

    statuses = {
        row["idea_id"]: row["status"]
        for row in store.knowledge.execute(
            "SELECT idea_id, status FROM idea WHERE idea_id IN ({})".format(",".join("?" for _ in survivors)),
            survivors,
        )
    } if survivors else {}

    written: list[dict[str, Any]] = []
    consolidated: list[str] = []
    consolidated_unjudged: list[str] = []
    consolidation_skipped: list[dict[str, str]] = []

    def _consolidate(idea_id: str, *, unjudged: bool) -> None:
        current = statuses.get(idea_id)
        if current != "raw":
            # A merged record revived to 'consolidated' by a re-screen is a
            # folded record back in the round without the Phase 7 ruling
            # that alone reopens one. An ARCHIVED row is refused here for a
            # different reason and by the same rule: it is a prior round's
            # entry, in the reference set and not a candidate, so there is
            # nothing about this round for it to be consolidated INTO. (It
            # never reaches a judged scope either -- the mechanical screen
            # batches `raw` rows only -- so this is the second of two locks,
            # which is what a rule worth stating twice looks like.)
            consolidation_skipped.append({"idea_id": idea_id, "status": str(current)})
            return
        store_update(store, "idea", pk_column="idea_id", pk_value=idea_id, changes={"status": "consolidated"})
        consolidated.append(idea_id)
        if unjudged:
            consolidated_unjudged.append(idea_id)

    for idea_id in scope:
        given = labels.get(idea_id)
        if not _answered(idea_id):
            continue
        for reference_set in declared:
            label = given.get(REFERENCE_SETS[reference_set])
            if label is None:
                continue
            written.append(_write(idea_id, reference_set, label, _evidence(reference_set, idea_id, label)))
        if status == "screened" and consolidate:
            _consolidate(idea_id, unjudged=False)

    # ONE row for an unjudged record, not one per declared set: the pair it
    # carries is a statement about RETRIEVAL (nothing came back above the
    # candidate threshold), which is one fact however many sets the round
    # declared. It is filed under R3 when the round declares it -- the set
    # the flag that would have routed this record belongs to -- and under
    # the round's first declared set otherwise, so no row ever names a set
    # the batch did not declare.
    unjudged_set = "R3" if "R3" in declared else declared[0]
    for idea_id in unjudged_scope:
        # The mechanical pair, written down rather than left implicit. No
        # judge saw this record; the row says exactly that, and the label is
        # not `new-mechanism` and never reads as one.
        written.append(
            _write(
                idea_id, unjudged_set, f"{UNJUDGED_LABEL}:{UNJUDGED_QUALIFIER}",
                [
                    {"note": "outside the judged scope: no inventory flag and no retrieval hit above the "
                             "candidate threshold, and not drawn in the seeded sample"},
                    {"note": f"reference_snapshot {snapshot_note}"},
                ],
            )
        )
        if status == "screened" and consolidate:
            _consolidate(idea_id, unjudged=True)

    kappa: dict[str, Any] | None = None
    if second_judge_labels:
        kappa = {}
        for reference_set in declared:
            key = REFERENCE_SETS[reference_set]
            # Kappa's expected agreement is computed over the categories a
            # judge could have used, which are the ROUND's when it declared
            # a vocabulary -- a chance term over five design labels where
            # the judge was offered three is a kappa about a different
            # instrument. The unscreenable word is one of them for every
            # declared set (lane FB-6 item 4).
            categories = accepted_labels_for_set(vocabularies, reference_set=reference_set)
            first = {s: v[key] for s, v in labels.items() if v.get(key) is not None}
            second = {s: v[key] for s, v in second_judge_labels.items() if v.get(key) is not None}
            kappa[key] = cohens_kappa(first, second, categories=categories)

    on_topic_label = on_topic_seed_label(vocabularies)
    round_unscreenable = unscreenable_word(vocabularies)
    seed_counts = {
        subject: {
            "n": len(seeds),
            "n_on_topic": sum(1 for seed in seeds if seed["label"] == on_topic_label),
            "seeds": seeds,
        }
        for subject, seeds in sorted(seeds_by_subject.items())
        if seeds
    }
    seed_report = {
        "on_topic_label": on_topic_label,
        "min_on_topic": MIN_ON_TOPIC_SEEDS,
        "n_subjects": len(seed_counts),
        "n_seeds": sum(block["n"] for block in seed_counts.values()),
        "n_on_topic": sum(block["n_on_topic"] for block in seed_counts.values()),
        "by_subject": seed_counts,
        # Which subjects the round called unscreenable with fewer than
        # MIN_ON_TOPIC_SEEDS on-topic seeds behind it. Reported, never
        # refused: the rule is the round's own text and this is the number a
        # reader checks it against. A subject with NO seeds recorded is
        # listed with 0, because "no seed work recorded" is exactly the case
        # the bar exists to make visible.
        "unscreenable_below_bar": sorted(
            subject
            for subject, given in labels.items()
            if round_unscreenable in {given.get(REFERENCE_SETS[name]) for name in declared}
            and (seed_counts.get(subject) or {}).get("n_on_topic", 0) < MIN_ON_TOPIC_SEEDS
        ),
    }
    result = {
        "round_id": round_id,
        "batch_id": batch["batch_id"],
        "status": status,
        "caveats": caveats,
        "seeds": seed_report,
        # The instrument these rows were recorded under, carried into the
        # summary the round reads: a label distribution whose reference sets
        # are not named beside it is a distribution about an unknown
        # comparison.
        "judged_sets": list(declared),
        "plants": plants,
        "kappa": kappa,
        "prereg_compliant": prereg_compliant,
        "prereg_compliance": prereg_compliance,
        "prereg_compliance_detail": prereg_detail,
        "n_verdicts": len(written),
        "n_consolidated": len(consolidated),
        "n_consolidated_unjudged": len(consolidated_unjudged),
        "consolidated_unjudged": consolidated_unjudged,
        "consolidation_skipped": consolidation_skipped,
        "unlabelled_scope": unlabelled_scope,
        "unlabelled_scope_share": unlabelled_share,
        "superseded": superseded,
        "verdicts": written,
        "ts": now(),
    }
    if vocabularies is not None:
        # Which vocabulary these labels were returned in, beside the counts
        # they produced. Absent when the round declared none, so a default
        # round's summary is the summary it always was.
        result["labels_sha256"] = vocabularies["sha256"]
        result["label_vocabularies"] = vocabularies
    base = Path(out_dir) if out_dir is not None else round_dir(store.program_root, round_id)
    (base / "judged").mkdir(parents=True, exist_ok=True)
    (base / "judged" / f"{batch['batch_id']}-verdicts.json").write_text(
        json.dumps(
            {k: v for k, v in result.items() if k != "verdicts"}, indent=2, ensure_ascii=False, sort_keys=True
        ) + "\n",
        encoding="utf-8", newline="\n",
    )
    return result


# ---------------------------------------------------------------------------
# convergent discovery: the scheduled re-check
# ---------------------------------------------------------------------------

#: Stamped on every link this pass writes, so a later reader can tell which
#: procedure found a convergence and when the procedure itself changed.
CONVERGENT_RECHECK_VERSION = "convergent-recheck-v1"

#: The ONLY caller-supplied fields a convergent link carries, beside its own
#: key and the two this module stamps (``found_ts``,
#: ``procedure_version``). A whitelist rather than a pass-through: the link
#: lands on ``idea.convergent_with``, and a caller handing a ``status`` or a
#: ``label_inventory`` through would write judge-shaped keys onto the record
#: this pass is forbidden to re-score. "Nothing else is reachable from here"
#: is the property, so it is enforced rather than trusted.
CONVERGENT_LINK_FIELDS: tuple[str, ...] = (
    "reference_set", "chunk_id", "source_id", "similarity", "provider",
)


def _external_key(row: Mapping[str, Any]) -> str:
    """A stable key for one external (R5) hit. External records are not
    chunks in this corpus, so they have no ``chunk_id``; the provider's own
    identifier is the key, and its title is the last resort for a provider
    that returns neither an id nor a DOI."""
    for field in ("id", "arxiv_id", "paper_id", "doi", "url"):
        value = row.get(field)
        if value:
            return f"R5:{row.get('provider') or 'external'}:{value}"
    return f"R5:{row.get('provider') or 'external'}:{str(row.get('title') or '').strip().lower()}"


def _hit_key(row: Mapping[str, Any]) -> str:
    """One key per candidate hit, per reference set. R4 hits are chunks in
    this corpus and key on ``chunk_id``; R5 hits key on the provider's own
    identifier (:func:`_external_key`)."""
    if str(row.get("reference_set") or "") == "R4" or row.get("chunk_id"):
        return f"R4:{row.get('chunk_id')}"
    return _external_key(row)


def known_neighbour_keys(dossier: Mapping[str, Any] | None) -> set[str]:
    """Every candidate hit the dossier ALREADY recorded, as keys.

    This is what makes the re-check a re-check rather than a second screen:
    a neighbour the screen already saw is not a convergent discovery, and
    reporting it as one would turn every scheduled run into a pile of
    re-findings nobody can read past."""
    if not dossier:
        return set()
    hits = dossier.get("candidate_hits") or {}
    keys: set[str] = set()
    for reference_set in ("R4", "R5"):
        for row in hits.get(reference_set) or []:
            if isinstance(row, Mapping):
                keys.add(_hit_key({**row, "reference_set": reference_set}))
    return keys


def read_dossier(store: Store, *, round_id: str, idea_id: str, out_dir: Path | str | None = None) -> dict[str, Any] | None:
    """One idea's dossier as the screen wrote it, or ``None`` if it has
    none. The handover between phases is these files (see
    :func:`run_mechanical_screen`), so a pass that runs in a different
    sitting and a different process reads them rather than being handed
    anything."""
    base = Path(out_dir) if out_dir is not None else round_dir(store.program_root, round_id)
    path = _dossier_path(base, idea_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def record_convergent_links(
    store: Store,
    *,
    idea_id: str,
    links: Sequence[Mapping[str, Any]],
    launch_id: str | None = None,
    round_id: str | None = None,
) -> dict[str, Any]:
    """Write convergent-discovery links onto ``idea.convergent_with`` —
    and nothing else, ever.

    The design is explicit about the boundary and it is worth restating where
    the write happens: a convergence found AFTER a round closed is logged,
    never penalised. So this function does not touch ``status``, any label,
    any verdict row or any dossier field. An idea that turns out to have a
    twin in work ingested three months later is not a less novel idea than it
    was when it was judged — it is the same idea, with a new neighbour, and
    the record says so separately.

    Links are a union keyed on :func:`_hit_key`'s key, so a re-run of the
    same pass adds nothing, and the FIRST sighting's timestamp survives.

    The carried fields are a WHITELIST (:data:`CONVERGENT_LINK_FIELDS`), not
    a pass-through of whatever the caller handed over: a link is "this exists,
    here, this close", and a ``status`` or a ``label_inventory`` copied into
    ``idea.convergent_with`` would put judge-shaped keys on the very record
    this pass may not re-score."""
    row = read_idea(store, idea_id=idea_id)
    if row is None:
        raise NoveltyError(f"record_convergent_links: no idea {idea_id!r}")
    existing_raw = row.get("convergent_with") or []
    existing: dict[str, dict[str, Any]] = {}
    for entry in existing_raw if isinstance(existing_raw, list) else []:
        if isinstance(entry, Mapping) and entry.get("key"):
            existing[str(entry["key"])] = dict(entry)
        elif isinstance(entry, str):
            # A link written as a bare key by some earlier hand stays a link.
            existing[entry] = {"key": entry}
    added: list[dict[str, Any]] = []
    ts = now()
    for link in links:
        key = str(link.get("key") or "")
        if not key or key in existing:
            continue
        entry = {
            "key": key,
            "reference_set": link.get("reference_set"),
            "found_ts": ts,
            "procedure_version": CONVERGENT_RECHECK_VERSION,
            **{
                field: link[field]
                for field in CONVERGENT_LINK_FIELDS
                if field != "reference_set" and field in link
            },
        }
        existing[key] = entry
        added.append(entry)
    if added:
        store_update(
            store, "idea", pk_column="idea_id", pk_value=idea_id,
            changes={"convergent_with": json.dumps(list(existing.values()), ensure_ascii=False)},
        )
        append_event(
            store,
            event_type="idea_convergent_linked",
            payload={
                "idea_id": idea_id,
                "round_id": round_id or row.get("round_id"),
                "procedure_version": CONVERGENT_RECHECK_VERSION,
                "n_new": len(added),
                "keys": [a["key"] for a in added],
            },
            launch_id=launch_id,
        )
    return {"idea_id": idea_id, "added": added, "n_links": len(existing)}


def recheck_idea_convergence(
    store: Store,
    *,
    idea_id: str,
    dossier: Mapping[str, Any] | None = None,
    external: ExternalProvider | None = None,
    external_query_mode: str = "none",
    model_key: str | None = None,
    corpus_k: int = DEFAULT_CORPUS_K,
    corpus_mode: str = DEFAULT_CORPUS_MODE,
    external_k: int = DEFAULT_EXTERNAL_K,
    candidate_hit_similarity: float = DEFAULT_CANDIDATE_HIT_SIMILARITY,
    launch_id: str | None = None,
    out_dir: Path | str | None = None,
    write: bool = True,
    allow_unscreened: bool = False,
) -> dict[str, Any]:
    """Re-check ONE idea against the corpus as it stands now and, under a
    query mode that names one, against the external index — then write
    whatever is new as a ``convergent_with`` link.

    This is the scheduled half of §4.1's last sentence: "convergent
    discoveries found by the scheduled re-check ... are written as
    ``convergent_with`` links, never applied to the original label". Two
    properties make that true here rather than by convention:

    1. The only write is :func:`record_convergent_links`, which touches one
       column. No label, no status, no verdict row, no dossier field is
       reachable from this call.
    2. "New" is measured against what the idea's own dossier already
       recorded (:func:`known_neighbour_keys`), so a neighbour the screen
       saw at the time is not re-reported as a discovery.

    The retrieval is the SAME retrieval the screen ran — R4 through
    ``stratified_retrieve``, R5 through the provider behind the same
    ``external_query_mode`` — because a re-check that retrieved differently
    would be finding differences in the instrument, not in the corpus.

    **A record with no dossier on file is REFUSED.** "New" is measured
    against what the screen already recorded, so with nothing recorded every
    neighbour is new and the pass reports the whole neighbourhood as
    convergent discoveries — which is not a discovery, it is the screen being
    run late under another name. That is a raw record, or a consolidated one
    whose dossier is missing (itself a ``idea_missing_dossier`` finding).
    ``allow_unscreened=True`` takes that reading deliberately and says so;
    the handler carries the refusal into its checkpoint rather than failing
    the round."""
    if external_query_mode not in EXTERNAL_QUERY_MODES:
        raise NoveltyError(
            f"external_query_mode must be one of {list(EXTERNAL_QUERY_MODES)!r}, got {external_query_mode!r}"
        )
    if external_query_mode != "none" and external is None:
        raise NoveltyError(
            f"external_query_mode={external_query_mode!r} names a query this call has no provider to issue; "
            "pass external=<provider>, or run with external_query_mode='none'"
        )
    idea = read_idea(store, idea_id=idea_id)
    if idea is None:
        raise NoveltyError(f"recheck_idea_convergence: no idea {idea_id!r}")
    round_id = idea.get("round_id")
    if dossier is None and round_id:
        dossier = read_dossier(store, round_id=str(round_id), idea_id=idea_id, out_dir=out_dir)
    if dossier is None and not allow_unscreened:
        raise NoveltyError(
            f"recheck_idea_convergence: idea {idea_id!r} (status={str(idea.get('status') or '?')!r}) has no "
            "novelty dossier on file, so there is nothing to measure NEW against -- every neighbour would be "
            "reported as a convergent discovery, which is the screen run late under another name. Screen the "
            "record first, or pass allow_unscreened=True to take that reading deliberately"
        )
    known = known_neighbour_keys(dossier)

    # The backend comes from the program either way; ``model_key`` overrides
    # only the key the R4 vectors are read under, never the backend that
    # embeds this statement -- a caller naming a key it did not embed under
    # would otherwise silently compare vectors from two models.
    _require_query_embed_backend(store, corpus_mode=corpus_mode, action="convergent re-check")
    resolved_key, backend = retrieve_engine._resolve_embed_backend(store, side="query")
    model_key = model_key or resolved_key
    statement = _statement(idea)
    vector = backend.embed_batch([statement], kind="document")[0] if statement.strip() else None

    corpus = _corpus_neighbours(
        store, statement=statement, vector=vector, model_key=model_key, k_total=corpus_k,
        mode=corpus_mode, hit_similarity=candidate_hit_similarity,
    )
    external_hits = _external_neighbours(
        store, idea=idea, external=external, mode=external_query_mode, k=external_k,
        launch_id=launch_id, round_id=str(round_id or ""),
    )
    found = [{**row, "reference_set": "R4"} for row in corpus["hits"]] + [
        {**row, "reference_set": "R5"} for row in external_hits
    ]
    new_links: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in found:
        key = _hit_key(row)
        if key in known or key in seen:
            continue
        seen.add(key)
        new_links.append(
            {
                "key": key,
                "reference_set": row.get("reference_set"),
                # The ID and the number, never the text: a link says "this
                # exists, here", and the text it says that about lives where
                # it already lives.
                "chunk_id": row.get("chunk_id"),
                "source_id": row.get("source_id"),
                "similarity": row.get("similarity"),
                "provider": row.get("provider"),
            }
        )
    written = record_convergent_links(
        store, idea_id=idea_id, links=new_links, launch_id=launch_id, round_id=str(round_id) if round_id else None
    ) if (write and new_links) else {"idea_id": idea_id, "added": [], "n_links": len(known)}
    return {
        "idea_id": idea_id,
        "round_id": round_id,
        "n_known": len(known),
        "n_found": len(found),
        "new": [dict(link) for link in new_links],
        "written": bool(written["added"]),
        "had_dossier": dossier is not None,
    }
