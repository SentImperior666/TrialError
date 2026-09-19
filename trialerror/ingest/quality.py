"""Extraction quality: four numbers per document, measured before anything
is refused.

**The problem.** Every stage of this pipeline can succeed on a document
whose text is unusable. A mis-encoded text layer normalizes to control
bytes; a two-column scan OCRs into interleaved half-sentences; a broken
word-spacing model glues a page into one 4,000-character "token". Nothing
raises, the document reaches ``status = 'indexed'``, and the first thing
that notices is an agent reading nonsense out of a citation -- by which
point the bad text is chunked, embedded, indexed and quoted.

**The shape of the answer, in the order the operator asked for it.**
Measure first: four cheap, read-only numbers over the sanitised element
text every document already carries, reportable per document
(:func:`measure_document`), over the corpus (:func:`measure_corpus`), from
the doctor (``extraction_quality_suspect``) and from the CLI
(``trialerror ingest quality``). Refuse second, and only when a program
explicitly configures it (``[ingest.quality] refuse_below``): the measure
is a signal about a *corpus*, and a threshold that stops an ingest is a
posture choice a program makes on purpose, never a default that starts
rejecting documents the day this module lands.

**Each number's denominator, stated once (they are what makes the numbers
comparable across documents of wildly different length):**

======================  ==============================================
number                  denominator
======================  ==============================================
``glued_token_rate``    all whitespace tokens, counted by the CHUNKER's
                        own :func:`trialerror.ingest.chunker.estimate_tokens`
``unusable_char_count`` none -- an absolute count of characters that are
                        not text at all (a rate would hide a 50-character
                        burst of control bytes inside a long book)
``terminator_density``  1,000 characters of text
``chars_per_page_cv``   the mean characters-per-page over the document's
                        DECLARED ``page_count`` (a coefficient of variation
                        IS the normalisation; pages with no text count as
                        zeros), ``None`` when the document has no page
                        structure to compare
======================  ==============================================

Two deliberate non-decisions, so a later reader does not mistake them for
oversights:

* **A measure is never a verdict.** A glossary is legitimately terminator-
  poor; a table-heavy appendix is legitimately glued-token-rich. That is
  why the doctor check is WARN-only and why refusal is opt-in.
* **A document with no text is not a suspect document.** It has not been
  measured, it has been *not normalized yet* (or retracted), which the
  pipeline's own stage chain already reports. ``measurable = False`` rows
  are excluded from suspicion and from refusal.
* **A document too SMALL for a rate to mean anything is not a suspect
  document either** (``[ingest.quality] min_tokens``, default
  :data:`DEFAULT_MIN_TOKENS`). The first day's live measurement flagged
  8-to-31-token notes purely by denominator: one long URL in a 12-token note
  is a glued-token rate of 0.08, and a note with no full stop is a
  terminator density of 0.0, neither of which says anything about the
  extraction. Those rows are still MEASURED and still reported -- they carry
  ``below_min_tokens`` and are counted under that name by the doctor check
  and by ``ingest quality`` -- they are simply never counted ``suspect``.
  The floor is a property of the DENOMINATORS, not a threshold on quality,
  which is why it suppresses the verdict rather than becoming a fifth
  measure. It does not touch refusal: ``refuse_below`` is a posture a
  program states explicitly, and this module never quietly widens or
  narrows what an operator wrote there.
"""

from __future__ import annotations

import json
import random
import statistics
from typing import Any, Iterable, Mapping, Sequence

from trialerror.ingest.chunker import estimate_tokens
from trialerror.ingest.errors import QualityRefusalConfigError

# The ONE definition of "characters that are not text" in this codebase --
# imported, never re-derived. ``trialerror.ingest.normalize_djvu`` wrote it
# for the DjVu text-layer routing decision ("U+FFFD plus the C0/C1 control
# characters that are not whitespace ... They are not text"), and a second
# regex spelling the same intent is a second answer that drifts: the
# routing decision and the quality measure would disagree about the very
# same page, and nobody would know which one to believe.
from trialerror.ingest.normalize_djvu import _UNUSABLE_CHARS_RE

__all__ = [
    "GLUED_TOKEN_MIN_CHARS",
    "TERMINATORS",
    "DEFAULT_THRESHOLDS",
    "DEFAULT_SAMPLE",
    "DEFAULT_SAMPLE_SEED",
    "QUALITY_REFUSAL_REGISTER_KEY",
    "MEASURE_KEYS",
    "DEFAULT_MIN_TOKENS",
    "glued_token_rate",
    "unusable_char_count",
    "terminator_density",
    "chars_per_page_cv",
    "measure_elements",
    "measure_document",
    "measure_corpus",
    "sample_doc_ids",
    "corpus_doc_ids",
    "thresholds_from_config",
    "refusal_thresholds_from_config",
    "below_min_tokens",
    "suspect_reasons",
    "refusal_reasons",
    "is_suspect",
    "severity",
    "worst_first",
    "quality_refusal_record",
]

#: A whitespace token longer than this many characters is not a word. The
#: longest words in ordinary technical English are ~20 characters, and a
#: URL or a chemical name is allowed to be longer -- which is exactly why
#: the measure is a RATE over all tokens rather than a flag on any one.
GLUED_TOKEN_MIN_CHARS = 25

#: Sentence terminators, ASCII plus the CJK/fullwidth closing forms a
#: mixed-language corpus actually contains. The reading-order proxy: text
#: whose sentences were shredded by a two-column scan read in the wrong
#: order still HAS its terminators, but text that came out of a broken
#: layout pass as a stream of fragments does not.
TERMINATORS: frozenset[str] = frozenset(".?!。？！…")

#: The size floor below which the four measures have no denominator worth
#: judging. A document with fewer than this many tokens is measured and
#: reported like any other, but is never counted ``suspect`` -- see the
#: module docstring's third non-decision for what the first day of live
#: measurement looked like without it (tiny notes of 8-31 tokens, flagged by
#: arithmetic rather than by bad extraction). ``0`` disables the floor.
DEFAULT_MIN_TOKENS = 200

#: Conservative by construction -- a WARN an operator learns to ignore is
#: worse than no check at all. Each value is roughly an order of magnitude
#: away from what ordinary prose measures (glued tokens are ~0.1-1% of
#: tokens, terminator density ~8-20 per 1,000 characters), so a document
#: that trips one of these is not a stylistic outlier.
DEFAULT_THRESHOLDS: dict[str, Any] = {
    "glued_token_rate_max": 0.10,
    "unusable_chars_max": 200,
    "terminator_density_min": 1.0,
    "chars_per_page_cv_max": 1.5,
    "min_tokens": DEFAULT_MIN_TOKENS,
    "worst_n": 10,
}

#: How many documents one doctor run measures. The check SAMPLES: a full
#: corpus scan on every ``trialerror doctor`` invocation would make the
#: cheapest health command in the system proportional to the corpus, and a
#: sampled WARN is worth the same as a complete one (the operator's next
#: move is ``trialerror ingest quality --all`` either way).
DEFAULT_SAMPLE = 50

#: The sample is SEEDED, so two doctor runs against an unchanged corpus
#: report the same documents. An unseeded sample would make the check's
#: own output look like a corpus that changes every time it is read.
DEFAULT_SAMPLE_SEED = 0

#: ``record.register_key`` for the refusal ledger written by the normalize
#: stage when ``[ingest.quality] refuse_below`` is configured -- the same
#: generic register (and the same reason for using it rather than a new
#: column) as ``trialerror.ingest.retract``'s ``ingest.retraction``: this
#: lane owns no schema, and a register row is a real indexed table rather
#: than a provenance column abused as a flag.
QUALITY_REFUSAL_REGISTER_KEY = "ingest.quality_refusal"

#: The four measures, in the order everything reports them.
MEASURE_KEYS: tuple[str, ...] = (
    "glued_token_rate",
    "unusable_char_count",
    "terminator_density",
    "chars_per_page_cv",
)

#: Which threshold key bounds which measure, and from which side. The one
#: table every comparison in this module reads, so the doctor check, the
#: CLI and the refusal cannot drift into three slightly different notions
#: of "worse".
_BOUNDS: tuple[tuple[str, str, str], ...] = (
    ("glued_token_rate", "glued_token_rate_max", "max"),
    ("unusable_char_count", "unusable_chars_max", "max"),
    ("terminator_density", "terminator_density_min", "min"),
    ("chars_per_page_cv", "chars_per_page_cv_max", "max"),
)


# ---------------------------------------------------------------------------
# the four measures
# ---------------------------------------------------------------------------
def glued_token_rate(text: str) -> float:
    """Fraction of whitespace tokens longer than
    :data:`GLUED_TOKEN_MIN_CHARS` characters.

    **Denominator: the chunker's own token count.** It is
    :func:`trialerror.ingest.chunker.estimate_tokens` -- literally called,
    not re-implemented -- because a rate whose denominator disagrees with
    the number the chunker uses to cut this same text into 1,024-token
    chunks is a rate about a different document than the one that gets
    stored. The numerator walks the same ``str.split()`` that function
    counts (``tests/test_ingest_quality.py`` pins the two together, so a
    future chunker that re-defines ``estimate_tokens`` fails loudly here
    instead of quietly reporting a rate over mismatched units).

    ``0.0`` on empty text: no tokens, nothing glued.
    """
    total = estimate_tokens(text or "")
    if total <= 0:
        return 0.0
    glued = sum(1 for token in (text or "").split() if len(token) > GLUED_TOKEN_MIN_CHARS)
    return glued / total


def unusable_char_count(text: str) -> int:
    """How many characters in ``text`` are not text: U+FFFD plus the
    non-whitespace C0/C1 control bytes.

    **Denominator: none, deliberately.** This is an absolute count. A rate
    would divide a mis-decoded page's burst of replacement characters by a
    whole book's length and report a reassuring 0.0001 -- and it is the
    burst, not the ratio, that tells an operator a text layer was decoded
    under the wrong encoding.

    The regex is :data:`trialerror.ingest.normalize_djvu._UNUSABLE_CHARS_RE`,
    imported (see this module's import comment).
    """
    return len(_UNUSABLE_CHARS_RE.findall(text or ""))


def terminator_density(text: str) -> float:
    """Sentence terminators (:data:`TERMINATORS`) per 1,000 characters --
    the reading-order proxy.

    **Denominator: 1,000 characters of text.** Ordinary prose lands around
    8-20; a stream of layout fragments, a table dumped as text, or a
    column-interleaved scan lands near zero, because the thing that was
    lost is sentence structure rather than characters.

    ``0.0`` on empty text (no characters, so no density to report -- and
    an unmeasurable document is excluded from suspicion by
    :func:`measure_elements`'s ``measurable`` flag, not by a sentinel
    here).
    """
    chars = len(text or "")
    if chars <= 0:
        return 0.0
    hits = sum(1 for ch in text if ch in TERMINATORS)
    return hits * 1000.0 / chars


def chars_per_page_cv(pages: Sequence[str] | Sequence[int] | Iterable[Any]) -> float | None:
    """Coefficient of variation (population stdev / mean) of characters per
    page.

    ``pages`` is one entry per page, either the page's text or its
    character count. **Denominator: the mean characters-per-page** -- a CV
    is already normalised, which is what makes it comparable between a
    12-page paper and a 900-page book.

    ``None`` when there is nothing to compare: fewer than two pages, or a
    mean of zero. A caller that knows the document has no ``page_count``
    reports ``None`` for the same reason (:func:`measure_elements`), which
    is also the caller that pads this vector out to the document's declared
    page count -- a page with no text is a zero, not an absent entry.

    Population stdev, not sample: these pages are the whole document, not
    a sample drawn from a larger one.
    """
    counts = [len(p) if isinstance(p, str) else int(p) for p in pages]
    if len(counts) < 2:
        return None
    mean = statistics.fmean(counts)
    if mean <= 0:
        return None
    return statistics.pstdev(counts) / mean


# ---------------------------------------------------------------------------
# composing them over a document
# ---------------------------------------------------------------------------
def _int_or_none(value: Any) -> int | None:
    """``value`` as an ``int``, or ``None`` when it is not one -- the
    document's ``page_count`` column, read defensively because a measure
    must never be the thing that raises on a hand-written row."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _round(value: Any, digits: int) -> Any:
    return None if value is None else round(float(value), digits)


def _element_text(elements: Sequence[Mapping[str, Any]]) -> str:
    ordered = sorted(elements, key=lambda e: (e["seq"] if e.get("seq") is not None else 0))
    return "\n".join((e.get("text") or "") for e in ordered)


def measure_elements(
    elements: Sequence[Mapping[str, Any]], *, page_count: int | None = None
) -> dict[str, Any]:
    """The four measures over one document's element rows (already
    sanitised -- ``_finish_normalize_stage`` sanitises every element's text
    before it is inserted, so nothing here re-sanitises or second-guesses
    stored text).

    Pure: takes rows, touches no store. This is the function the normalize
    stage calls on the drafts it has just written (so a refusal needs no
    second read) and the one :func:`measure_document` calls after loading
    them.

    ``page_count`` is the document's own column, and it is the CV's
    DENOMINATOR: the per-page character vector is padded with zeros up to
    it, so a page that carried no text counts as the zero it is (fix pass
    V-4). ``chars_per_page_cv`` is ``None`` unless ``page_count`` is known
    AND at least one page carries text -- "this document has no page
    structure" and "these pages are uneven" are different statements and
    must not share a number.
    """
    text = _element_text(elements)
    tokens = estimate_tokens(text)

    by_page: dict[int, int] = {}
    for el in elements:
        page = el.get("page_number")
        if page is None:
            continue
        by_page[int(page)] = by_page.get(int(page), 0) + len(el.get("text") or "")

    # Fix pass V-4: the CV's denominator is the document's OWN page count,
    # not "the pages that happen to carry element rows". A page with no
    # element row carried no text, which is a real zero and the single worst
    # per-page extraction failure there is: a long scan whose route produced
    # text for a handful of pages measured a perfectly even 0.0 here, while
    # the same document measured grossly suspect if the extractor happened
    # to emit empty element rows for the blank pages. Padding to page_count
    # makes those two cases -- the same document, two extractor habits --
    # report the same number.
    cv: float | None = None
    pages_declared = _int_or_none(page_count)
    if pages_declared is not None and by_page:
        counts = [by_page[p] for p in sorted(by_page)]
        counts += [0] * max(pages_declared - len(counts), 0)
        cv = chars_per_page_cv(counts)

    return {
        "elements": len(elements),
        "chars": len(text),
        "tokens": tokens,
        "page_count": page_count,
        "pages_with_text": len(by_page),
        # `measurable` is the "has this document been extracted at all?"
        # flag every consumer branches on before judging anything. A
        # registered-but-not-normalized document, and a retracted one,
        # both land here with no tokens -- and a terminator density of
        # 0.0 on no text must never read as "suspect extraction".
        "measurable": tokens > 0,
        "glued_token_rate": _round(glued_token_rate(text), 6),
        "unusable_char_count": unusable_char_count(text),
        "terminator_density": _round(terminator_density(text), 3),
        "chars_per_page_cv": _round(cv, 4),
    }


def _conn(store: Any):
    """The knowledge.db connection, from either a
    :class:`~trialerror.stores.store.Store` or a bare connection.

    Both callers are real and neither should have to construct the other's
    handle: the CLI and the normalize handler hold a ``Store``; the doctor
    check holds the read-only connection it opened itself
    (``trialerror.ingest.checks`` deliberately never opens a writer).
    """
    return store.knowledge if hasattr(store, "knowledge") else store


def measure_document(store: Any, doc_id: str) -> dict[str, Any]:
    """The four measures for one document, read-only.

    ``store`` is a :class:`~trialerror.stores.store.Store` or a
    knowledge.db connection. Raises
    :class:`~trialerror.ingest.errors.DocumentNotFoundError` for an unknown
    ``doc_id`` -- a measurement of a document that does not exist is not a
    zero, it is a caller bug.
    """
    from trialerror.ingest.errors import DocumentNotFoundError

    conn = _conn(store)
    doc = conn.execute(
        "SELECT doc_id, media_type, page_count, status FROM document WHERE doc_id = ?", (doc_id,)
    ).fetchone()
    if doc is None:
        raise DocumentNotFoundError(f"no such document: {doc_id!r}")
    doc = dict(doc)
    elements = [
        dict(r)
        for r in conn.execute(
            "SELECT seq, text, page_number FROM element WHERE doc_id = ? ORDER BY seq", (doc_id,)
        ).fetchall()
    ]
    row = measure_elements(elements, page_count=doc.get("page_count"))
    return {
        "doc_id": doc_id,
        "media_type": doc.get("media_type"),
        "status": doc.get("status"),
        **row,
    }


def corpus_doc_ids(store: Any, *, include_retracted: bool = False) -> list[str]:
    """Every document id in the corpus, ordered, retractions excluded by
    default.

    A retracted document's elements are gone, so it measures as
    unmeasurable -- harmless, but it would pad every sample with rows that
    can never say anything. Excluded the same way
    ``trialerror.ingest.checks`` excludes them from its counts, through
    the one indirection that will become the ``status = 'retracted'``
    migration.
    """
    conn = _conn(store)
    ids = [r["doc_id"] for r in conn.execute("SELECT doc_id FROM document ORDER BY doc_id").fetchall()]
    if include_retracted:
        return ids
    from trialerror.ingest.retract import retracted_doc_ids

    retracted = retracted_doc_ids(conn)
    return [d for d in ids if d not in retracted]


def sample_doc_ids(doc_ids: Sequence[str], sample: int | None, seed: int | None = None) -> list[str]:
    """A SEEDED subset of ``doc_ids``, in corpus order.

    The one sampler: :func:`measure_corpus` and the
    ``extraction_quality_suspect`` doctor check both draw through it, so
    "the sampled documents" means the same thing whichever surface an
    operator reads. ``sample`` of ``None`` (or one that covers the corpus)
    returns everything, unsampled.

    A non-positive ``sample`` also returns everything, and both configured
    callers refuse to ask for that: ``[ingest.quality] sample`` is clamped
    to at least 1 by :func:`thresholds_from_config` and ``--sample`` is
    refused below 1 by the CLI (fix pass V-6), because a doctor check that
    full-scans is the one thing that check promises never to do. "Measure
    everything" is spelled ``--all`` with no ``--sample`` at all.
    """
    ids = list(doc_ids)
    if sample is None or sample < 0 or sample >= len(ids):
        return ids
    rng = random.Random(DEFAULT_SAMPLE_SEED if seed is None else seed)
    return sorted(rng.sample(ids, sample))


def measure_corpus(
    store: Any,
    *,
    sample: int | None = None,
    seed: int | None = None,
    include_retracted: bool = False,
) -> list[dict[str, Any]]:
    """One measurement row per document (:func:`measure_document`).

    ``sample`` measures a SEEDED random subset instead of the whole corpus
    -- what the doctor check runs, so its cost does not grow with the
    corpus. ``seed`` defaults to :data:`DEFAULT_SAMPLE_SEED`, so an
    unchanged corpus reports the same documents twice running; the sampled
    ids are returned in corpus order, not draw order, so the output of two
    runs is comparable line by line.
    """
    doc_ids = sample_doc_ids(corpus_doc_ids(store, include_retracted=include_retracted), sample, seed)
    return [measure_document(store, doc_id) for doc_id in doc_ids]


# ---------------------------------------------------------------------------
# thresholds, suspicion, refusal
# ---------------------------------------------------------------------------
#: Sentinel for "this config value is not a number at all", so
#: :func:`refusal_thresholds_from_config` can tell an unreadable value from a
#: readable one without borrowing a default it must never substitute.
_UNREADABLE = object()


def _as_number(value: Any, fallback: Any) -> Any:
    """A config value coerced to a number, or ``fallback``.

    Neither the doctor nor an ingest job may crash on a mistyped TOML
    value here: the whole point of this module is a health signal, and a
    health signal that dies on its own configuration is worse than absent.
    (This is NOT the fail-closed case ``_load_config`` guards -- a bad
    quality threshold cannot write a wrong vector into the corpus; it can
    only make a WARN read against the default.)
    """
    if isinstance(value, bool):
        return fallback
    if isinstance(value, (int, float)):
        number: float = float(value)
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return fallback
    else:
        return fallback
    # The default's own type decides: a count stays a count, a rate stays a
    # rate, so `worst_n = 10.9` cannot make a slice expression raise later.
    return int(number) if isinstance(fallback, int) else number


def _quality_cfg(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    ingest_cfg = (config or {}).get("ingest") or {}
    if not isinstance(ingest_cfg, Mapping):
        return {}
    quality_cfg = ingest_cfg.get("quality") or {}
    return quality_cfg if isinstance(quality_cfg, Mapping) else {}


def thresholds_from_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """``[ingest.quality]``'s thresholds over :data:`DEFAULT_THRESHOLDS`,
    plus the doctor check's ``sample``/``seed``. Unknown keys are ignored;
    an uncoercible value falls back to the default (see :func:`_as_number`).

    ``sample`` is CLAMPED to at least one document (fix pass V-6). It is not
    a threshold, it is the size of the work the doctor check does, and the
    two non-positive spellings each broke one of that check's own promises:
    ``sample = -1`` reached :func:`sample_doc_ids` as "no sample" and made
    the check full-scan the corpus on every ``trialerror doctor`` run, and
    ``sample = 0`` returned a PASS whose message said no document in the
    corpus had extracted text yet -- measured from a sample of none. A
    program that wants the exhaustive pass has a verb for it
    (``trialerror ingest quality --all``).
    """
    cfg = _quality_cfg(config)
    out = dict(DEFAULT_THRESHOLDS)
    for key, default in DEFAULT_THRESHOLDS.items():
        if key in cfg:
            out[key] = _as_number(cfg[key], default)
    # The size floor is a count of tokens: a negative one is meaningless and
    # is read as "no floor" rather than as an error (this is a WARN-side knob,
    # and `_as_number` has already coerced it through the default's int type).
    if out["min_tokens"] is None or int(out["min_tokens"]) < 0:
        out["min_tokens"] = 0
    else:
        out["min_tokens"] = int(out["min_tokens"])
    sample = _as_number(cfg.get("sample", DEFAULT_SAMPLE), DEFAULT_SAMPLE)
    out["sample"] = DEFAULT_SAMPLE if sample is None or int(sample) < 1 else int(sample)
    out["seed"] = _as_number(cfg.get("seed", DEFAULT_SAMPLE_SEED), DEFAULT_SAMPLE_SEED)
    return out


def refusal_thresholds_from_config(config: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """``[ingest.quality] refuse_below``, or ``None`` for "never refuse".

    **Absent by default, and an empty table is still absent.** This is the
    one knob in this module that can stop a document's pipeline, so it
    only ever does what a program's config literally says: only the
    threshold keys actually present are compared (a ``refuse_below`` that
    names one measure refuses on that measure alone), and a table with no
    usable key at all reads as unconfigured rather than as "refuse
    everything".

    **Fail-closed on a value it cannot read** (fix pass V-2). Every other
    coercion in this module falls back to a default, because a mistyped WARN
    bound can only make a health signal read against the default. Here the
    fallback WAS the warn default, which meant ``refuse_below =
    { terminator_density_min = "eight" }`` silently installed 1.0 as a
    REFUSAL bound and the normalize stage began writing
    ``document.status = 'failed'`` against a number nobody wrote. A
    threshold that stops an ingest is only ever one the operator states, so
    an unreadable value raises
    :class:`~trialerror.ingest.errors.QualityRefusalConfigError` naming the
    key and the value -- the same direction as
    :func:`trialerror.ingest.handlers._load_config`'s fail-closed read (D13),
    and for the same reason: the intent exists and could not be read. A
    numeric string is still read as the number it spells.
    """
    cfg = _quality_cfg(config)
    if "refuse_below" not in cfg:
        return None
    raw = cfg["refuse_below"]
    if not isinstance(raw, Mapping):
        raise QualityRefusalConfigError(
            f"[ingest.quality] refuse_below must be a table of threshold keys, not {type(raw).__name__} "
            f"({raw!r}) -- refusing to read it as 'no refusal configured', which would silently stop this "
            "program refusing anything. Write it as a table, e.g. "
            "refuse_below = { glued_token_rate_max = 0.35 }."
        )
    out: dict[str, Any] = {}
    for _measure, key, _side in _BOUNDS:
        if key not in raw:
            continue
        if _as_number(raw[key], _UNREADABLE) is _UNREADABLE:
            raise QualityRefusalConfigError(
                f"[ingest.quality] refuse_below.{key} = {raw[key]!r} is not a number this program can read. "
                "This is the one threshold that can stop an ingest, so it is never replaced by a default: "
                "write a number (or remove the key to refuse on nothing)."
            )
        # Readable: coerced through the default's own type, so a count stays
        # a count and a rate stays a rate (see :func:`_as_number`).
        out[key] = _as_number(raw[key], DEFAULT_THRESHOLDS[key])
    return out or None


def _violations(row: Mapping[str, Any], limits: Mapping[str, Any]) -> list[str]:
    """Every bound in ``limits`` that ``row`` is on the wrong side of, as
    human-readable strings. Unmeasurable rows violate nothing (see the
    module docstring's second non-decision); a ``None`` measure -- a
    document with no page structure -- is not compared at all."""
    if not row.get("measurable"):
        return []
    out: list[str] = []
    for measure, key, side in _BOUNDS:
        if key not in limits:
            continue
        limit = limits[key]
        value = row.get(measure)
        if value is None or limit is None:
            continue
        if side == "max" and value > limit:
            out.append(f"{measure} {value} > {key} {limit}")
        elif side == "min" and value < limit:
            out.append(f"{measure} {value} < {key} {limit}")
    return out


def below_min_tokens(row: Mapping[str, Any], thresholds: Mapping[str, Any] | None = None) -> bool:
    """Is this row too small for its own denominators to mean anything?

    ``True`` only for a MEASURABLE document with fewer than
    ``thresholds['min_tokens']`` tokens (default
    :data:`DEFAULT_MIN_TOKENS`). An unmeasurable row is ``False``: "not
    normalized yet" and "a genuinely tiny note" are different statements and
    must not share a flag any more than they share ``suspect`` (see the
    module docstring's last two non-decisions). A floor of ``0`` -- what a
    program gets by writing ``min_tokens = 0`` -- is ``False`` for
    everything, which is the floor switched off.
    """
    # V-2 (lane FB-1b verify): a caller-supplied PARTIAL mapping names only
    # the bounds it wants, exactly as ``_violations`` reads it -- so the floor
    # applies only when the mapping carries ``min_tokens`` (the config reader
    # always does; ``None`` means the defaults).
    limits = DEFAULT_THRESHOLDS if thresholds is None else thresholds
    floor = limits.get("min_tokens")
    if floor is None or not row.get("measurable"):
        return False
    tokens = row.get("tokens")
    if tokens is None:
        return False
    try:
        return int(tokens) < int(floor)
    except (TypeError, ValueError):  # a row from somewhere that did not count tokens
        return False


def suspect_reasons(row: Mapping[str, Any], thresholds: Mapping[str, Any] | None = None) -> list[str]:
    """Why this measurement row is suspect, or ``[]``. WARN vocabulary --
    nothing here refuses anything.

    A row under the size floor returns ``[]`` however bad its numbers look:
    the four measures are rates and counts whose denominator is the
    document's own text, and below :data:`DEFAULT_MIN_TOKENS` tokens they
    report arithmetic rather than extraction. The row is still measured and
    still reported (``below_min_tokens``), just never called suspect. This
    is the ONE place the floor is applied, so the doctor check, ``ingest
    quality``, ``ingest status`` and :func:`worst_first` cannot disagree
    about which documents are suspect.
    """
    limits = thresholds or DEFAULT_THRESHOLDS
    if below_min_tokens(row, limits):
        return []
    return _violations(row, limits)


def is_suspect(row: Mapping[str, Any], thresholds: Mapping[str, Any] | None = None) -> bool:
    return bool(suspect_reasons(row, thresholds))


def refusal_reasons(row: Mapping[str, Any], refuse_below: Mapping[str, Any] | None) -> list[str]:
    """Why the normalize stage must refuse this document, or ``[]``.

    ``refuse_below = None`` (the default posture) always returns ``[]`` --
    the un-configured program never refuses.
    """
    if not refuse_below:
        return []
    return _violations(row, refuse_below)


def severity(row: Mapping[str, Any], thresholds: Mapping[str, Any] | None = None) -> float:
    """How far past its thresholds this row is, as one number.

    **An ordering key, never a threshold.** Its only job is to put the
    worst documents first in a bounded report; no decision in this module
    is taken on it, so its exact scale carries no meaning beyond "bigger
    is worse". Zero for a clean or unmeasurable row.
    """
    limits = thresholds or DEFAULT_THRESHOLDS
    if not row.get("measurable") or below_min_tokens(row, limits):
        # Zero below the size floor for the same reason as for an
        # unmeasurable row: a row that can never be suspect must not be able
        # to head a worst-first report, or the floor would remove the verdict
        # and leave the noise.
        return 0.0
    total = 0.0
    for measure, key, side in _BOUNDS:
        limit = limits.get(key)
        value = row.get(measure)
        if value is None or limit in (None, 0):
            continue
        if side == "max" and value > limit:
            total += (float(value) - float(limit)) / abs(float(limit))
        elif side == "min" and value < limit:
            total += (float(limit) - float(value)) / abs(float(limit))
    return total


def worst_first(
    rows: Sequence[Mapping[str, Any]], *, thresholds: Mapping[str, Any] | None = None, limit: int | None = None
) -> list[dict[str, Any]]:
    """``rows`` ordered worst-first by :func:`severity`, each carrying its
    own ``suspect``/``below_min_tokens``/``reasons``/``severity``, truncated
    to ``limit``.

    Ties break on ``doc_id`` so a report is stable between runs.
    """
    limits = thresholds or DEFAULT_THRESHOLDS
    annotated = []
    for row in rows:
        reasons = suspect_reasons(row, limits)
        annotated.append(
            {
                **dict(row),
                "suspect": bool(reasons),
                "below_min_tokens": below_min_tokens(row, limits),
                "reasons": reasons,
                "severity": round(severity(row, limits), 4),
            }
        )
    annotated.sort(key=lambda r: (-r["severity"], r.get("doc_id") or ""))
    return annotated if limit is None else annotated[: max(0, int(limit))]


# ---------------------------------------------------------------------------
# the refusal ledger (written by the normalize stage, read by ingest status)
# ---------------------------------------------------------------------------
def quality_refusal_record(conn, doc_id: str) -> dict[str, Any] | None:
    """The quality-refusal payload for ``doc_id`` (the four numbers, the
    reasons, the thresholds, the launch and the timestamp), or ``None``.

    Reads the generic ``record`` register, exactly as
    :func:`trialerror.ingest.retract.retraction_record` reads its own --
    including the "a program whose ``record`` table predates this feature
    simply has no such rows" degrade, because a reader of this must never
    be the thing that breaks ``ingest status``.
    """
    try:
        rows = conn.execute(
            "SELECT payload FROM record WHERE register_key = ? ORDER BY seq DESC",
            (QUALITY_REFUSAL_REGISTER_KEY,),
        ).fetchall()
    except Exception:  # noqa: BLE001 - an unreadable register is "no record"
        return None
    for row in rows:
        try:
            payload = json.loads(row["payload"] if hasattr(row, "keys") else row[0])
        except (TypeError, ValueError):
            continue
        if isinstance(payload, Mapping) and payload.get("doc_id") == doc_id:
            return dict(payload)
    return None
