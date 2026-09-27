"""Citation-vs-record matching (lane SI item A4).

A DOI in a citation can be mistyped and still resolve -- to a different work.
:meth:`trialerror.litapi.client.LitApiClient.lookup_doi` returns whatever the
identifier resolves to; this module compares that record with what the citation
itself says (first-author surname, year, title when one is quoted) and names the
result: ``exact``, ``probable``, ``mismatch`` or ``none``.

Stdlib only (``difflib.SequenceMatcher``), deterministic, no I/O. The two
thresholds are keyword arguments here with the defaults the investigation
config will carry, and every :class:`MatchResult` echoes the pair it was judged
under.

Lane SI amendment B0: on an identifier route a first-author surname mismatch
is DECISIVE. A DOI that resolves to a work by other authors from an adjacent
year is exactly the wrong-identifier case this module exists to catch, and
under the original two-failures rule it read ``probable`` (the year sat inside
the tolerance and the surname alone decided nothing). Now it reads
``mismatch`` -- unless the citation quotes a title that reaches the floor
against the record's, in which case the label is at most ``probable`` and the
result carries a ``surname_mismatch`` note.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Sequence

from trialerror.litapi.citeparse import CitedWork
from trialerror.litapi.models import WorkRecord, normalize_arxiv_id, normalize_doi, normalize_title

__all__ = [
    "RESOLUTIONS",
    "DEFAULT_TITLE_FLOOR",
    "DEFAULT_YEAR_TOLERANCE",
    "SURNAME_MISMATCH_NOTE",
    "MatchResult",
    "title_similarity",
    "match_citation",
    "best_title_match",
]

RESOLUTIONS: tuple[str, ...] = ("exact", "probable", "mismatch", "none")
DEFAULT_TITLE_FLOOR = 0.90
DEFAULT_YEAR_TOLERANCE = 1
_VIAS = ("doi", "arxiv", "isbn", "title")
#: The one note :func:`match_citation` writes (amendment B0): the cited
#: first-author surname matches none of the record's authors.
SURNAME_MISMATCH_NOTE = "surname_mismatch"


@dataclass
class MatchResult:
    """``resolution`` is one of :data:`RESOLUTIONS`; ``reasons`` carries
    ``title_similarity`` (``None`` when the citation quotes no title),
    ``surname_hit`` (``None`` when the citation yields no surname),
    ``year_delta`` (record year minus cited year, ``None`` when either is
    missing) and ``via`` (``doi|arxiv|isbn|title``); ``thresholds`` the
    ``title_floor``/``year_tolerance`` in force; ``notes`` what the label alone
    does not say (today only :data:`SURNAME_MISMATCH_NOTE`, written whenever
    ``surname_hit`` is ``False``)."""

    resolution: str
    reasons: dict[str, Any] = field(default_factory=dict)
    thresholds: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "resolution": self.resolution,
            "reasons": dict(self.reasons),
            "thresholds": dict(self.thresholds),
            "notes": list(self.notes),
        }


def title_similarity(a: str | None, b: str | None) -> float:
    """``SequenceMatcher`` ratio of the two :func:`normalize_title` forms, also
    taken against ``b`` cut at its first ``:`` (a subtitle one side dropped);
    the larger of the two. ``0.0`` when either side has no title."""
    na = normalize_title(a)
    nb = normalize_title(b)
    if not na or not nb:
        return 0.0
    best = SequenceMatcher(None, na, nb).ratio()
    if b and ":" in b:
        nb_short = normalize_title(b.split(":", 1)[0])
        if nb_short:
            best = max(best, SequenceMatcher(None, na, nb_short).ratio())
    return best


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def _author_tokens(authors: Sequence[str]) -> set[str]:
    """Every name token of every author that is not an initial, folded
    (accents stripped, casefolded) -- so ``"Writer, C."`` and ``"C. Writer"``
    both yield ``writer``."""
    tokens: set[str] = set()
    for name in authors or []:
        normalized = normalize_title(_fold(name)) if name else None
        for token in (normalized or "").split():
            if len(token) >= 2:
                tokens.add(token)
    return tokens


def _surname_hit(cited: CitedWork, record: WorkRecord) -> bool | None:
    if not cited.surnames:
        return None
    tokens = _author_tokens(record.authors)
    if not tokens:
        return None
    for surname in cited.surnames:
        folded = normalize_title(_fold(surname))
        if folded and all(part in tokens for part in folded.split()):
            return True
    return False


def _year_delta(cited: CitedWork, record: WorkRecord) -> int | None:
    """Record year minus cited year; when the citation gives a reprint pair,
    the closer of ``year`` and ``first_pub_year`` (either edition is the
    cited work)."""
    if not isinstance(record.year, int):
        return None
    deltas = [record.year - y for y in (cited.year, cited.first_pub_year) if isinstance(y, int)]
    if not deltas:
        return None
    return min(deltas, key=abs)


def _infer_via(cited: CitedWork, record: WorkRecord) -> str:
    """How ``record`` was reached: through an identifier the citation carries
    and the record shares, else through a title search."""
    if cited.doi and normalize_doi(record.doi) == normalize_doi(cited.doi):
        return "doi"
    if cited.arxiv_id and normalize_arxiv_id(record.arxiv_id) == normalize_arxiv_id(cited.arxiv_id):
        return "arxiv"
    return "title"


def match_citation(
    cited: CitedWork,
    record: WorkRecord,
    *,
    title_floor: float = DEFAULT_TITLE_FLOOR,
    year_tolerance: int = DEFAULT_YEAR_TOLERANCE,
    via: str | None = None,
) -> MatchResult:
    """Judge ``record`` against ``cited``.

    Reached via an identifier (``doi``/``arxiv``/``isbn``): ``exact`` when the
    surname hits and the year is within tolerance (and, only when the citation
    quotes a title, the title reaches the floor). A first-author surname
    MISMATCH is decisive (amendment B0): ``mismatch``, unless the citation
    quotes a title reaching the floor, in which case the label is at most
    ``probable`` -- still ``mismatch`` when the year fails as well, because
    that is two failed checks. Otherwise ``mismatch`` when two of {surname,
    year, title-if-quoted} fail, else ``probable``. A component the citation or
    record does not give (no surname parsed, no year) neither hits nor fails,
    so a missing surname is never decisive.

    Reached via a title search: ``exact`` when the title reaches the floor AND
    the surname hits AND the year is within tolerance; ``probable`` when the
    title reaches the floor and one of the other two holds; ``none`` otherwise.
    A title search never answers ``mismatch``: a search result that is not the
    cited work is simply not found (``none``), and nothing was mis-identified.

    Either route: ``notes`` carries ``surname_mismatch`` whenever the cited
    surname matches none of the record's authors.

    ``via`` defaults to what the two share: ``doi`` when the record carries the
    cited DOI, ``arxiv`` when it carries the cited arXiv id, else ``title``.
    Pass it explicitly for an ISBN route (a :class:`WorkRecord` has no ISBN
    field to compare)."""
    if via is None:
        via = _infer_via(cited, record)
    if via not in _VIAS:
        raise ValueError(f"via must be one of {_VIAS}, got {via!r}")

    similarity = title_similarity(cited.title_hint, record.title) if cited.title_hint else None
    surname_hit = _surname_hit(cited, record)
    year_delta = _year_delta(cited, record)

    title_ok = None if similarity is None else similarity >= title_floor
    year_ok = None if year_delta is None else abs(year_delta) <= year_tolerance

    if via == "title":
        if title_ok and surname_hit is True and year_ok is True:
            resolution = "exact"
        elif title_ok and (surname_hit is True or year_ok is True):
            resolution = "probable"
        else:
            resolution = "none"
    else:
        checks = [surname_hit, year_ok] + ([title_ok] if title_ok is not None else [])
        failures = sum(1 for c in checks if c is False)
        if surname_hit is True and year_ok is True and title_ok is not False:
            resolution = "exact"
        elif surname_hit is False and title_ok is not True:
            # amendment B0: the surname alone decides, unless a quoted title
            # reaches the floor (then the two-failures rule below caps it).
            resolution = "mismatch"
        elif failures >= 2:
            resolution = "mismatch"
        else:
            resolution = "probable"

    return MatchResult(
        resolution=resolution,
        reasons={
            "title_similarity": None if similarity is None else round(similarity, 4),
            "surname_hit": surname_hit,
            "year_delta": year_delta,
            "via": via,
        },
        thresholds={"title_floor": title_floor, "year_tolerance": year_tolerance},
        notes=[SURNAME_MISMATCH_NOTE] if surname_hit is False else [],
    )


_RANK = {"exact": 0, "probable": 1, "mismatch": 2, "none": 3}


def best_title_match(
    cited: CitedWork,
    records: Sequence[WorkRecord],
    *,
    title_floor: float = DEFAULT_TITLE_FLOOR,
    year_tolerance: int = DEFAULT_YEAR_TOLERANCE,
) -> tuple[WorkRecord | None, MatchResult]:
    """The best of ``records`` (search results) judged ``via="title"``: the
    best resolution, ties broken by title similarity then by list order. The
    record is ``None`` when nothing reaches ``probable``; the
    :class:`MatchResult` returned is then the best candidate's (or an empty
    ``none`` when ``records`` is empty), so the caller can see how close it
    came."""
    best: tuple[WorkRecord, MatchResult] | None = None
    for record in records:
        result = match_citation(cited, record, title_floor=title_floor, year_tolerance=year_tolerance, via="title")
        if best is None:
            best = (record, result)
            continue
        current = best[1]
        key_new = (_RANK[result.resolution], -(result.reasons["title_similarity"] or 0.0))
        key_old = (_RANK[current.resolution], -(current.reasons["title_similarity"] or 0.0))
        if key_new < key_old:
            best = (record, result)
    if best is None:
        return None, MatchResult(
            resolution="none",
            reasons={"title_similarity": None, "surname_hit": None, "year_delta": None, "via": "title"},
            thresholds={"title_floor": title_floor, "year_tolerance": year_tolerance},
        )
    record, result = best
    if result.resolution not in ("exact", "probable"):
        return None, result
    return record, result
