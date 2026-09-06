"""Reading a list of links a human wrote.

``trialerror webfetch batch --list <file>`` takes a markdown file the
operator delivered — the wave_0 case is eight links, seven hosts, two GitHub
repos and one markdown-escaped ``deterministic\\_lockstep`` — and turns it
into enqueue instructions. This module is the parsing half of that, kept
separate from the handlers for one reason: a list file is *operator input*,
and operator input has the property that when it is misread, the operator is
the one who has to work out why. Everything here therefore keeps the line
number and the raw line beside every parsed URL, so ``batch`` can say "line
7: this one, and here is what I made of it" instead of dropping it.

**What a list line may contain** (design §5, batch):

* a markdown bullet with a link — ``- [title](https://example.org/a)``
* a bare URL, bulleted or not
* an autolink — ``<https://example.org/a>``
* trailing ``key=value`` tags: ``tier=open``, ``kind=git``
* markdown escapes inside the URL — ``deterministic\\_lockstep`` is a real
  line from the real list, and an underscore escaped for markdown's benefit
  is not part of the URL

**What it may not do**: name a host that is not approved, or reach anything.
Nothing in this module opens a socket or writes a job. It produces
:class:`ListEntry` values; the enqueue path decides what becomes of them, and
the sidecar's own allowlist decides what is fetchable — a URL in a delivered
list is the operator's *intent*, which is why the seven wave_0 hosts are
pre-approved at deploy time by ``te-webfetch.sh import-list`` (ruling L-A2)
rather than by anything the harness does when it reads this file.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator
from urllib.parse import urlsplit

from trialerror.webfetch import WebFetchError

__all__ = [
    "LIST_TAG_KEYS",
    "ListEntry",
    "ListParseError",
    "infer_kind",
    "iter_list_entries",
    "parse_link_list",
    "list_ref_for",
    "read_link_list",
    "distinct_hosts",
]

#: The only trailing tags a list line may carry. Anything else is refused
#: rather than ignored: a line reading ``tiers=open`` was meant to say
#: something, and silently fetching it under the default tier would be the
#: wrong kind of forgiving.
LIST_TAG_KEYS: frozenset[str] = frozenset({"tier", "kind"})

_MAX_LIST_BYTES = 1 * 1024 * 1024
_MAX_LIST_LINES = 5000

_MD_LINK_RE = re.compile(r"\[(?P<text>[^\]]{0,300})\]\((?P<url>[^)\s]{1,4096})\)")
_AUTOLINK_RE = re.compile(r"<(?P<url>https?://[^>\s]{1,4096})>")
_BARE_URL_RE = re.compile(r"(?P<url>https?://[^\s<>\]\"']{1,4096})")
_TAG_RE = re.compile(r"(?P<key>[a-z_]{1,16})=(?P<value>[A-Za-z0-9._:-]{1,64})")
#: Markdown escapes: a backslash before an ASCII punctuation character is
#: markdown's, not the URL's.
_MD_ESCAPE_RE = re.compile(r"\\([!-/:-@\[-`{-~])")

_TRAILING_PUNCT = ".,;:!?"


class ListParseError(WebFetchError):
    """The list file itself could not be read (absent, too large, not text).

    A line that yields no URL is NOT this: it is a comment, a heading or
    prose, and a delivered list is mostly prose. Only the file being
    unusable stops the verb."""


@dataclass(frozen=True)
class ListEntry:
    """One URL a list line named, with what the line said about it."""

    url: str
    line_no: int
    raw_line: str
    title: str | None = None
    license_tier: str | None = None
    kind: str | None = None

    @property
    def resolved_kind(self) -> str:
        """The line's own ``kind=`` if it gave one, else inferred."""
        return self.kind or infer_kind(self.url)


def _unescape_markdown(url: str) -> str:
    return _MD_ESCAPE_RE.sub(r"\1", url)


def _strip_trailing_punctuation(url: str) -> str:
    """Trim sentence punctuation a bare URL picked up from its prose.

    Only from a BARE url, and only where the character cannot plausibly be
    part of one. Parentheses get their own rule rather than a blanket trim,
    because both mistakes are real and they pull in opposite directions:
    ``/wiki/Lock_(computing)`` ends in a paren that belongs to the URL, and
    ``(see https://example.org/a)`` ends in one that belongs to the sentence.
    Balance decides. A trailing ``)`` with no ``(`` to match it is the
    sentence's; one that closes a paren inside the URL is the URL's.
    """
    while url:
        last = url[-1]
        if last in _TRAILING_PUNCT:
            url = url[:-1]
            continue
        if last == ")" and url.count(")") > url.count("("):
            url = url[:-1]
            continue
        break
    return url


def infer_kind(url: str) -> str:
    """``git`` for a GitHub repo URL, ``pdf`` for a ``.pdf``, else ``page``.

    A hint, not a verdict: ``kind`` decides which fetch path the sidecar
    takes, and the sidecar re-checks what actually arrived by sniffing magic
    numbers (design §4 T5, ``magic_mismatch``). A ``.pdf`` URL that serves
    HTML is caught there, not here.
    """
    lowered = url.lower()
    if re.match(r"^https?://(www\.)?github\.com/[^/]+/[^/]+", lowered):
        return "git"
    path = lowered.split("?", 1)[0].split("#", 1)[0]
    if path.endswith(".pdf"):
        return "pdf"
    return "page"


def _tags_of(line: str, url: str) -> tuple[dict[str, str], list[str]]:
    """``key=value`` tags AFTER the URL. Returns ``(tags, unknown_keys)``.

    Only after: a URL's own query string is full of ``key=value`` pairs, and
    reading ``?tier=premium`` out of one as an instruction to this harness
    would be letting the page decide its own license tier.
    """
    tail = line.split(url, 1)[-1] if url in line else ""
    tags: dict[str, str] = {}
    unknown: list[str] = []
    for match in _TAG_RE.finditer(tail):
        key = match.group("key")
        if key in LIST_TAG_KEYS:
            tags[key] = match.group("value")
        else:
            unknown.append(key)
    return tags, unknown


def iter_list_entries(text: str) -> Iterator[ListEntry]:
    """Yield one :class:`ListEntry` per URL found, in file order.

    A line may name more than one URL and each becomes its own entry; the
    same URL twice becomes two entries, because deduplication is the enqueue
    path's job and doing it here would hide from the operator that their list
    repeats itself.
    """
    for line_no, raw_line in enumerate(text.splitlines()[:_MAX_LIST_LINES], start=1):
        line = raw_line.strip()
        if not line or line.startswith((">", "#")):
            # A quoted line or a heading is commentary. Headings especially:
            # a delivered list is a document, and "## Papers" is not a URL.
            continue

        seen_spans: list[tuple[int, int]] = []
        for match in _MD_LINK_RE.finditer(line):
            seen_spans.append(match.span())
            url = _unescape_markdown(match.group("url").strip())
            if not url.lower().startswith(("http://", "https://")):
                continue
            tags, _unknown = _tags_of(line, match.group("url"))
            yield ListEntry(
                url=url,
                line_no=line_no,
                raw_line=raw_line,
                title=(match.group("text") or "").strip() or None,
                license_tier=tags.get("tier"),
                kind=tags.get("kind"),
            )
        for match in _AUTOLINK_RE.finditer(line):
            if any(start <= match.start() < end for start, end in seen_spans):
                continue
            seen_spans.append(match.span())
            url = _unescape_markdown(match.group("url").strip())
            tags, _unknown = _tags_of(line, match.group("url"))
            yield ListEntry(
                url=url,
                line_no=line_no,
                raw_line=raw_line,
                license_tier=tags.get("tier"),
                kind=tags.get("kind"),
            )
        for match in _BARE_URL_RE.finditer(line):
            if any(start <= match.start() < end for start, end in seen_spans):
                continue
            seen_spans.append(match.span())
            raw_url = match.group("url")
            url = _strip_trailing_punctuation(_unescape_markdown(raw_url.strip()))
            if not url:
                continue
            tags, _unknown = _tags_of(line, raw_url)
            yield ListEntry(
                url=url,
                line_no=line_no,
                raw_line=raw_line,
                license_tier=tags.get("tier"),
                kind=tags.get("kind"),
            )


def parse_link_list(text: str) -> list[ListEntry]:
    return list(iter_list_entries(text))


def read_link_list(path: str | Path) -> tuple[list[ListEntry], str]:
    """Read a list file. Returns ``(entries, list_ref)``.

    The ``list_ref`` is computed over the file's BYTES, before parsing, so
    two batches of the same file are provably the same batch and an edited
    file is provably a different one — which is what makes ``webfetch status
    --list`` able to say "this is the list you ran" rather than "a list with
    this name".
    """
    path = Path(path)
    if not path.is_file():
        raise ListParseError(f"link list {path} does not exist")
    size = path.stat().st_size
    if size > _MAX_LIST_BYTES:
        raise ListParseError(
            f"link list {path} is {size} bytes (cap {_MAX_LIST_BYTES}); this verb reads a "
            "delivered list of links, not a corpus"
        )
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ListParseError(f"could not read link list {path}: {exc}") from exc
    return parse_link_list(text), list_ref_for(path)


def list_ref_for(path: str | Path, program_root: str | Path | None = None) -> str:
    """``sha256:<digest>/<name>`` — the ``web_fetch.list_ref`` value.

    The digest is the file's content; the name is its path relative to the
    program root when it is inside one, and its bare filename otherwise.
    Never an absolute host path: this string is a corpus column, and a
    corpus column that records ``/home/<someone>/...`` has made the corpus
    machine-specific for no gain.
    """
    path = Path(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    name = path.name
    if program_root is not None:
        try:
            name = path.resolve().relative_to(Path(program_root).resolve()).as_posix()
        except (OSError, ValueError):
            name = path.name
    return f"sha256:{digest}/{name}"


def distinct_hosts(entries: Iterable[ListEntry]) -> list[str]:
    """The hosts a list names, in first-seen order.

    What ``te-webfetch.sh import-list`` prints for the operator to approve.

    Deliberately tolerant of a URL this build would refuse. A list line
    reading ``https://127.0.0.1/x`` will never fetch — ``urlcheck`` refuses
    every IP literal, whatever the allowlist says — but silently dropping it
    here would leave the operator counting seven hosts against eight links
    and wondering which one vanished. Showing a host is not approving it, and
    approving one this build refuses changes nothing: the refusal happens at
    fetch time, in the sidecar, on its own reason.
    """
    from trialerror.webfetch.urlcheck import peek_host

    ordered: list[str] = []
    for entry in entries:
        try:
            host = peek_host(entry.url)
        except WebFetchError:
            host = (urlsplit(entry.url).hostname or "").strip()
        if host and host not in ordered:
            ordered.append(host)
    return ordered
