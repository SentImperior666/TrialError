"""Free-text citation parsing (lane SI item A3).

A cited work usually arrives as one line of prose -- ``"A. Author, J. Widg. 17,
1991, doi:10.1016/0167-6423(91)90036-W"`` -- not as a bare identifier.
:func:`trialerror.litapi.models.looks_like_identifier` recognises a DOI or arXiv
id only once it has been isolated; this module isolates it, and pulls out the
other handles a lookup or a matcher needs (ISBN, years, first-author surname,
a quoted title).

Every field is conservative: a field the text does not plainly give is left
empty rather than guessed, and anything dropped on purpose (an ISBN whose
checksum fails) is named in :attr:`CitedWork.notes`. Pure functions, stdlib
only, no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from trialerror.litapi.models import normalize_arxiv_id, normalize_doi

__all__ = ["CitedWork", "parse_citation", "parse_seed_list", "isbn_to_13", "text_without_identifiers"]

#: A DOI starts at ``10.<registrant>/`` and runs to the next whitespace; the
#: trailing punctuation a sentence adds is trimmed afterwards (see
#: :func:`_trim_doi`).
_DOI_START_RE = re.compile(r"(?<![\d.])10\.\d{4,9}/\S+")
_TRAILING_PUNCT = ".,;:"
#: Closing quote marks a DOI never ends with but a quoted citation often does.
_TRAILING_QUOTES = "\"'”’»"
_BRACKETS = {")": "(", "]": "[", "}": "{", ">": "<"}

_ARXIV_PREFIXED_RE = re.compile(
    r"arxiv\s*:\s*(\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+(?:\.[a-z]{2})?/\d{7}(?:v\d+)?)", re.IGNORECASE
)
_ARXIV_URL_RE = re.compile(
    r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+(?:\.[a-z]{2})?/\d{7}(?:v\d+)?)", re.IGNORECASE
)
#: A bare new-style id (``YYMM.NNNNN``), month-checked so a decimal number is
#: not read as one.
_ARXIV_BARE_NEW_RE = re.compile(r"(?<![\w.])(\d{2}(?:0[1-9]|1[0-2])\.\d{4,5}(?:v\d+)?)(?![\w.]*\d)")
#: A bare old-style id (``archive[.SC]/YYMMNNN``).
_ARXIV_BARE_OLD_RE = re.compile(r"(?<![\w/.])([a-z]+(?:-[a-z]+)?(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)(?![\w])")

#: An ISBN candidate: digits and hyphens, 10-17 characters, ending in a digit
#: or ``X``; the digit count (10 or 13) and the checksum decide.
_ISBN_CANDIDATE_RE = re.compile(r"(?<![\w/.\-])(\d[\d\-]{8,15}[\dXx])(?![\w\-])")

_YEAR_RE = re.compile(r"(?<!\d)(1[5-9]\d\d|20\d\d)(?!\d)")
_REPRINT_RE = re.compile(r"(?<!\d)(1[5-9]\d\d|20\d\d)\s*/\s*(1[5-9]\d\d|20\d\d)(?!\d)")

_TITLE_PATTERNS = (
    re.compile(r"“([^”]+)”"),
    re.compile(r"\"([^\"]+)\""),
    re.compile(r"\*([^*]+)\*"),
)

#: Where the first author's name ends: a comma, ``&``, ``and``, ``et al``, an
#: opening parenthesis, or the first digit (``A. Author 1986``).
_AUTHOR_GROUP_END_RE = re.compile(r",|&|\band\b|\bet\s+al\b|\(|\d", re.IGNORECASE)
_NAME_TOKEN_RE = re.compile(r"[^\W\d_][\w'’\-]*\.?")
#: A first-author segment longer than this many name tokens is more likely a
#: title than a name; conservative answer: no surnames.
_MAX_AUTHOR_TOKENS = 4


@dataclass
class CitedWork:
    """What one free-text citation plainly says. ``doi``/``arxiv_id`` are
    normalised the same way :class:`~trialerror.litapi.models.WorkRecord`'s are
    (lower-cased DOI; arXiv id without prefix or version); ``isbn`` is always an
    ISBN-13 (an ISBN-10 is converted)."""

    raw: str
    doi: str | None = None
    arxiv_id: str | None = None
    isbn: str | None = None
    surnames: list[str] = field(default_factory=list)
    year: int | None = None
    first_pub_year: int | None = None
    title_hint: str | None = None
    #: Anything deliberately dropped or doubtful, one sentence each (e.g. an
    #: ISBN whose checksum failed).
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "doi": self.doi,
            "arxiv_id": self.arxiv_id,
            "isbn": self.isbn,
            "surnames": list(self.surnames),
            "year": self.year,
            "first_pub_year": self.first_pub_year,
            "title_hint": self.title_hint,
            "notes": list(self.notes),
        }


# -- identifiers ----------------------------------------------------------------


def _trim_doi(candidate: str) -> str:
    """Trim trailing ``.,;:``, closing quote marks and UNBALANCED closing
    brackets, repeatedly; a balanced ``(...)`` inside the DOI is kept."""
    while candidate:
        last = candidate[-1]
        if last in _TRAILING_PUNCT or last in _TRAILING_QUOTES:
            candidate = candidate[:-1]
            continue
        if last in _BRACKETS and candidate.count(last) > candidate.count(_BRACKETS[last]):
            candidate = candidate[:-1]
            continue
        break
    return candidate


def _find_doi(text: str) -> tuple[str | None, tuple[int, int] | None]:
    match = _DOI_START_RE.search(text)
    if not match:
        return None, None
    trimmed = _trim_doi(match.group(0))
    if not re.fullmatch(r"10\.\d{4,9}/.+", trimmed):
        return None, None
    return normalize_doi(trimmed), (match.start(), match.start() + len(trimmed))


def _outside(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    return all(end <= s or start >= e for s, e in spans)


def _find_arxiv(text: str, spans: list[tuple[int, int]]) -> tuple[str | None, tuple[int, int] | None]:
    for pattern in (_ARXIV_PREFIXED_RE, _ARXIV_URL_RE, _ARXIV_BARE_NEW_RE, _ARXIV_BARE_OLD_RE):
        for match in pattern.finditer(text):
            if _outside(spans, match.start(), match.end()):
                return normalize_arxiv_id(match.group(1)), (match.start(), match.end())
    return None, None


def _isbn10_valid(digits: str) -> bool:
    if len(digits) != 10 or not digits[:9].isdigit() or not (digits[9].isdigit() or digits[9] in "Xx"):
        return False
    total = sum((10 - i) * int(d) for i, d in enumerate(digits[:9]))
    total += 10 if digits[9] in "Xx" else int(digits[9])
    return total % 11 == 0


def _isbn13_valid(digits: str) -> bool:
    if len(digits) != 13 or not digits.isdigit():
        return False
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(digits))
    return total % 10 == 0


def isbn_to_13(isbn: str) -> str | None:
    """A checksum-valid ISBN-10 or ISBN-13 (hyphens allowed) as ISBN-13
    digits; ``None`` when it is neither."""
    digits = isbn.replace("-", "")
    if _isbn13_valid(digits):
        return digits
    if _isbn10_valid(digits):
        core = "978" + digits[:9]
        check = (10 - sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(core)) % 10) % 10
        return core + str(check)
    return None


def _find_isbn(
    text: str, spans: list[tuple[int, int]], notes: list[str]
) -> tuple[str | None, list[tuple[int, int]]]:
    found: str | None = None
    isbn_spans: list[tuple[int, int]] = []
    for match in _ISBN_CANDIDATE_RE.finditer(text):
        if not _outside(spans, match.start(), match.end()):
            continue
        candidate = match.group(1)
        digits = candidate.replace("-", "")
        if len(digits) not in (10, 13):
            continue
        isbn_spans.append((match.start(), match.end()))
        converted = isbn_to_13(candidate)
        if converted is None:
            notes.append(f"ISBN candidate {candidate!r} dropped: checksum invalid")
            continue
        if found is None:
            found = converted
    return found, isbn_spans


# -- years, title, surnames -----------------------------------------------------------


def _find_years(text: str, spans: list[tuple[int, int]]) -> tuple[int | None, int | None]:
    years = [int(m.group(1)) for m in _YEAR_RE.finditer(text) if _outside(spans, m.start(), m.end())]
    year = years[-1] if years else None
    first_pub_year: int | None = None
    for m in _REPRINT_RE.finditer(text):
        if _outside(spans, m.start(), m.end()):
            pair = (int(m.group(1)), int(m.group(2)))
            earliest = min(pair)
            first_pub_year = earliest if first_pub_year is None else min(first_pub_year, earliest)
    return year, first_pub_year


def _find_title_hint(text: str) -> str | None:
    for pattern in _TITLE_PATTERNS:
        match = pattern.search(text)
        if match and match.group(1).strip():
            return match.group(1).strip()
    return None


def _find_surnames(text: str, spans: list[tuple[int, int]]) -> list[str]:
    """The FIRST author's surname: the last capitalised, non-initial name token
    of the text before the first comma, ``&``, ``and``, ``et al.``, ``(`` or
    digit -- ``"A. Author"``, ``"Author, A."``, ``"Ann van Author"`` and
    ``"Author AB"`` all give ``["Author"]``; a given name is never returned as a
    surname. Only the first author is read: past the first comma a citation
    runs into more names, a title or a journal abbreviation (``J. Studies``),
    and the text alone does not say which. Conservative: a segment that holds a
    quote/emphasis mark, a ``:``, an identifier, or more than
    :data:`_MAX_AUTHOR_TOKENS` tokens reads as "not a name" and gives ``[]``."""
    end_match = _AUTHOR_GROUP_END_RE.search(text)
    segment_end = end_match.start() if end_match else len(text)
    if spans and min(s for s, _ in spans) < segment_end:
        return []
    segment = text[:segment_end].strip()
    if not segment or any(ch in segment for ch in "\"*“”:"):
        return []
    tokens = _NAME_TOKEN_RE.findall(segment)
    if not tokens or len(tokens) > _MAX_AUTHOR_TOKENS:
        return []
    names: list[str] = []
    for token in tokens:
        bare = token.rstrip(".")
        if token.endswith(".") and len(bare) <= 2:
            continue  # an initial ("A.") or a two-letter abbreviation
        if len(bare) < 2 or not bare[0].isupper() or bare.isupper():
            continue  # lower-case particle, single letter, or all-caps initials ("AB")
        names.append(bare)
    return names[-1:]


# -- public API -------------------------------------------------------------------------


def parse_citation(text: str) -> CitedWork:
    """Parse one free-text citation. See :class:`CitedWork` for the fields and
    the module docstring for the conservatism rule."""
    raw = text if isinstance(text, str) else ""
    cited = CitedWork(raw=raw)
    spans: list[tuple[int, int]] = []

    cited.doi, doi_span = _find_doi(raw)
    if doi_span:
        spans.append(doi_span)
    cited.arxiv_id, arxiv_span = _find_arxiv(raw, spans)
    if arxiv_span:
        spans.append(arxiv_span)
    cited.isbn, isbn_spans = _find_isbn(raw, spans, cited.notes)
    spans.extend(isbn_spans)

    cited.year, cited.first_pub_year = _find_years(raw, spans)
    cited.title_hint = _find_title_hint(raw)
    cited.surnames = _find_surnames(raw, spans)
    return cited


#: The labels that introduce an identifier in running text, removed together
#: with the identifier they label.
_ID_LABEL_RE = re.compile(r"(?:\bdoi\s*:?|\bisbn(?:-1[03])?\s*:?|https?://(?:dx\.)?doi\.org/)\s*$", re.IGNORECASE)


def text_without_identifiers(text: str) -> str:
    """``text`` with its DOI, arXiv id and ISBN candidates (the same spans
    :func:`parse_citation` finds, checksum-failed ISBNs included) cut out,
    together with a ``doi:``/``ISBN``/``https://doi.org/`` label right before
    one, and whitespace and dangling separators tidied. What is left is the
    prose of the citation -- names, title, venue, year -- which is what a
    title search can use when the citation quotes no title (lane SI part B)."""
    raw = text if isinstance(text, str) else ""
    spans: list[tuple[int, int]] = []
    _, doi_span = _find_doi(raw)
    if doi_span:
        spans.append(doi_span)
    for pattern in (_ARXIV_PREFIXED_RE, _ARXIV_URL_RE, _ARXIV_BARE_NEW_RE, _ARXIV_BARE_OLD_RE):
        for match in pattern.finditer(raw):
            if _outside(spans, match.start(), match.end()):
                spans.append((match.start(), match.end()))
    _, isbn_spans = _find_isbn(raw, spans, [])
    spans.extend(isbn_spans)

    pieces: list[str] = []
    cursor = 0
    for start, end in sorted(spans):
        if start < cursor:
            continue
        before = raw[cursor:start]
        label = _ID_LABEL_RE.search(before)
        if label:
            before = before[: label.start()]
        pieces.append(before)
        cursor = end
    pieces.append(raw[cursor:])
    joined = " ".join(" ".join(pieces).split())
    # a separator left dangling where an identifier was ("..., 1986, ." -> "..., 1986")
    joined = re.sub(r"\s+([,.;:])", r"\1", joined)
    joined = re.sub(r"([,;:])(?:\s*[,;:.])+", r"\1", joined)
    return joined.strip(" ,;:").strip()


def parse_seed_list(text: str) -> list[CitedWork]:
    """Split ``text`` into citations on ``;`` at the top level only -- not
    inside ``"..."``, ``“...”``, ``*...*``, brackets, or a DOI -- and
    parse each. A ``;`` inside a DOI is one not followed by whitespace or the
    end of the text (``...CO;2-O``); one followed by whitespace ends the DOI and
    splits. Empty pieces are dropped."""
    if not text:
        return []
    pieces: list[str] = []
    current: list[str] = []
    depth = 0
    in_straight_quote = False
    in_curly_quote = False
    in_emphasis = False
    doi_spans = [(m.start(), m.end()) for m in _DOI_START_RE.finditer(text)]

    def inside_doi(i: int) -> bool:
        return any(s <= i < e for s, e in doi_spans)

    for i, ch in enumerate(text):
        if ch == '"':
            in_straight_quote = not in_straight_quote
        elif ch == "“":
            in_curly_quote = True
        elif ch == "”":
            in_curly_quote = False
        elif ch == "*":
            in_emphasis = not in_emphasis
        elif ch in "([{":
            depth += 1
        elif ch in ")]}" and depth > 0:
            depth -= 1
        elif ch == ";" and not (in_straight_quote or in_curly_quote or in_emphasis or depth > 0):
            next_ch = text[i + 1] if i + 1 < len(text) else ""
            if not (inside_doi(i) and next_ch and not next_ch.isspace()):
                pieces.append("".join(current))
                current = []
                continue
        current.append(ch)
    pieces.append("".join(current))
    return [parse_citation(p.strip()) for p in pieces if p.strip()]
