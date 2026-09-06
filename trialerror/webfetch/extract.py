"""``readability_lite`` — turn one fetched page into corpus input.

This module runs in the research container, which has **no egress** (design
§1 P1). That is the whole reason it exists as a separate step from the fetch:
the process that parses hostile bytes must not be the process that can open
sockets. Everything here is stdlib (``html.parser``, ``urllib.parse``), so
the parser that meets attacker-controlled markup is the same one the rest of
the harness already trusts, and no new dependency enters the sandbox lock.

**What comes out** (design §2.2, the html branch):

* ``clean.html`` — the article subtree, stripped of chrome, scripts and
  everything invisible, wrapped in a head we wrote ourselves. This, not the
  markdown, is the ingest input: ``trialerror.ingest.normalizers.normalize_html``
  already turns it into ``Title``/``ListItem``/``Table``/``NarrativeText``
  elements, and the chunker's table isolation and header-row repeat only fire
  on ``Table`` elements. A markdown round-trip would quietly lose that.
* ``<fetch_id>.md`` — the same content rendered for humans and for HOME
  links. An output shape, never an intermediate.
* the extraction signals of §2.2 — title, author, published date, canonical
  link, declared license, language, word count, recorded links, the thin/JS
  verdict and any TDM opt-out the page carries.

**Three rules that are not negotiable**, because each is a threat control:

1. **Hidden text never reaches an element.** Script, style, comments,
   ``display:none``, ``aria-hidden``, zero-height text, off-screen
   positioning, ``alt``/``title`` attributes — all dropped before
   serialization (T3). Visible prose that says "ignore previous
   instructions" is kept *verbatim*: it is data, it is what the page said,
   and censoring it would be lying to the corpus. The distinction this
   module draws is visible/invisible, never benign/hostile.
2. **Links are recorded, never followed.** ``links`` is a data column. There
   is no code path from a link to a job (design §1 P3: URLs enter through
   the CLI, one door). ``<meta http-equiv="refresh">`` is dropped with the
   rest of ``<meta>`` and is never acted on.
3. **Metadata is data too.** A ``<title>`` lands in ``REQUESTS.md`` tables
   and HOME items, so it is flattened to one line, stripped of pipes,
   stripped of invisible codepoints, and capped (T3, "metadata as data").
   Same for every other signal.

**Rule 1 is best-effort, and saying so is part of it.** The element-level
drops (``script``, ``style``, comments, ``hidden``, ``aria-hidden``) are
structural and complete. The *inline-style* half is a list of patterns
(:data:`_HIDDEN_STYLE_RE`), and CSS has unboundedly many ways to render text
as nothing — most obviously a class defined in a stylesheet this extractor
never fetches. So "what is stored is what a human reader sees" is a property
this module works towards, not one it can promise. What holds regardless is
downstream: web chunks are served through ``untrusted_wrap`` with
``source.kind='web'``, so prose that does get through is data, never
instruction. Read the pattern list as defence in depth; extend it when a new
trick is observed.
"""

from __future__ import annotations

import html.parser
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping
from urllib.parse import urljoin, urlsplit

__all__ = [
    "EXTRACTOR_VERSION",
    "THIN_CONTENT_WORDS",
    "MAX_LINKS",
    "MAX_TITLE_CHARS",
    "JS_MARKERS",
    "Extraction",
    "decode_html",
    "extract_html",
    "license_tier_for",
    "render_markdown",
]

#: Stamped onto ``web_fetch.extractor_version``. Bump it when the OUTPUT
#: shape changes — a different clean-HTML serialization, a different main
#: block choice — so a re-extraction is distinguishable from a re-fetch.
#: The same discipline ``NORMALIZER_VERSION`` and ``SANITIZER_VERSION``
#: apply one layer down.
EXTRACTOR_VERSION = "readability_lite/1"

#: Design §5 settlement table: under this many extracted words the page is
#: "thin". Thin *plus* a JS marker means the article never rendered without a
#: browser and the operator is asked to deliver it (``needs_render``); thin
#: *without* one is simply a short page, and a short page is still data.
THIN_CONTENT_WORDS = 200

#: Design §2.2: at most this many links are recorded. A page that lists ten
#: thousand links is a directory, not an article, and the column exists so a
#: human can pick the next thing to fetch — not to mirror a site map.
MAX_LINKS = 500

#: A recorded link longer than this is dropped rather than truncated: a
#: truncated URL is a *wrong* URL, which is worse than a missing one.
MAX_LINK_CHARS = 2048

#: Titles land in operator-facing markdown tables (T3).
MAX_TITLE_CHARS = 200

#: Other single-line signals (author, published, canonical, license, lang).
MAX_SIGNAL_CHARS = 512

#: How deep the tree builder will nest. Beyond this, opening tags are still
#: parsed but their content is attached to the deepest allowed ancestor, so
#: a megabyte of ``<div><div><div>…`` costs memory bounded by the body cap
#: and nothing else — and every recursive walk below is depth-bounded and
#: therefore safe from a RecursionError on hostile input (T5).
MAX_DEPTH = 200

# ---------------------------------------------------------------------------
# tag vocabularies
# ---------------------------------------------------------------------------

#: Dropped whole, content included (design §2.2). Two groups, both here for
#: one reason each: ``script``/``style``/``template``/``noscript``/``svg`` and
#: the embedding tags carry text that a reader never sees (T3); ``nav``/
#: ``footer``/``aside``/``header``/``form`` are site chrome, and chrome in the
#: corpus is noise that every later retrieval pays for.
DROP_TAGS: frozenset[str] = frozenset(
    {
        "script",
        "style",
        "template",
        "noscript",
        "iframe",
        "object",
        "embed",
        "svg",
        "canvas",
        "form",
        "nav",
        "footer",
        "aside",
        "header",
        "head",
        "meta",
        "link",
        "base",
    }
)

#: Never nest: no end tag is expected and none is honored if one appears.
VOID_TAGS: frozenset[str] = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

#: Opening one of these implicitly closes any of the others that is still
#: open. Real pages leave ``<p>`` and ``<li>`` unclosed constantly; without
#: this the tree would nest every paragraph inside its predecessor and the
#: markdown render would emerge as one deeply indented list.
_IMPLICIT_CLOSE: dict[str, frozenset[str]] = {
    "p": frozenset({"p"}),
    "li": frozenset({"li"}),
    "dt": frozenset({"dt", "dd"}),
    "dd": frozenset({"dt", "dd"}),
    "tr": frozenset({"tr", "td", "th"}),
    "td": frozenset({"td", "th"}),
    "th": frozenset({"td", "th"}),
    "option": frozenset({"option"}),
    "thead": frozenset({"tr", "td", "th", "thead", "tbody", "tfoot"}),
    "tbody": frozenset({"tr", "td", "th", "thead", "tbody", "tfoot"}),
    "tfoot": frozenset({"tr", "td", "th", "thead", "tbody", "tfoot"}),
}

#: Where an implicit close stops looking. Without these, ``<li>a<ul><li>b``
#: would close the OUTER ``li`` (the first match found scanning up the stack)
#: and hoist the nested list's items to the outer level — a nested table of
#: contents would come out flat. A new ``<li>`` may only close an ``<li>`` in
#: the same list; a new ``<tr>`` only one in the same table section.
_IMPLICIT_CLOSE_BARRIERS: dict[str, frozenset[str]] = {
    "li": frozenset({"ul", "ol", "menu"}),
    "dt": frozenset({"dl"}),
    "dd": frozenset({"dl"}),
    "tr": frozenset({"table", "thead", "tbody", "tfoot"}),
    "td": frozenset({"table", "tr"}),
    "th": frozenset({"table", "tr"}),
    "p": frozenset(
        {"div", "section", "article", "main", "blockquote", "li", "td", "th", "form", "figure"}
    ),
    "option": frozenset({"select", "datalist", "optgroup"}),
    "thead": frozenset({"table"}),
    "tbody": frozenset({"table"}),
    "tfoot": frozenset({"table"}),
}

#: Candidates for "the main block". Descent is restricted to containers so
#: the density walk can never end up choosing an ``<a>`` or a ``<span>`` — an
#: inline element that happens to hold most of a paragraph's characters is
#: not the article.
_CONTAINER_TAGS: frozenset[str] = frozenset(
    {"html", "body", "main", "article", "div", "section", "td", "blockquote", "dl", "ul", "ol"}
)

#: Fraction of a container's text a single child must hold before the walk
#: descends into it (design §2.2: "the highest text-density block holding
#: ≥ 60 % of body text").
_DESCEND_RATIO = 0.60

_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_BLOCK_LEVEL = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "main",
        "li",
        "ul",
        "ol",
        "dl",
        "dt",
        "dd",
        "table",
        "tr",
        "blockquote",
        "pre",
        "figure",
        "figcaption",
        "hr",
        "br",
        *_HEADINGS,
    }
)

#: The ONLY attribute that survives serialization, and only on ``<a>``.
#: Everything else is dropped, which is the cheap way to be sure no
#: invisible-text channel (``alt``, ``title``, ``aria-label``, ``data-*``)
#: survives. Nothing downstream reads an attribute: ``normalize_html`` throws
#: even ``text_as_html``'s attributes away. ``href`` is kept so a human
#: reading ``clean.html`` can follow a citation, and it is what the recorded
#: link list is built from.
_KEEP_ATTRS: dict[str, frozenset[str]] = {"a": frozenset({"href"})}

_WS_RE = re.compile(r"[ \t\r\f\v]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_WORD_RE = re.compile(r"\S+")
_CHARSET_META_RE = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:+-]+)""", re.IGNORECASE
)
_CONTENT_TYPE_CHARSET_RE = re.compile(r"charset\s*=\s*\"?([A-Za-z0-9_.:+-]+)", re.IGNORECASE)

#: CSS comments, stripped from a style attribute before it is matched.
#: ``display:/*c*/none`` is one declaration to a browser and two unrelated
#: strings to a naive regex, which is the cheapest way past the list below.
_STYLE_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

#: Hidden by inline style. Whitespace inside the declaration is normalized
#: away and comments are removed before matching, so ``display : none`` and
#: ``display:/*c*/none`` are both caught.
#:
#: **This is a best-effort list, not a closed set**, and it should be read
#: that way. CSS has unboundedly many ways to make text render as nothing
#: (a class defined in a stylesheet this extractor never fetches, for one),
#: so what this buys is defence in depth for the common inline tricks, not a
#: guarantee that stored text equals visible text. The guarantee the design
#: does keep is downstream: web chunks are served through ``untrusted_wrap``
#: with ``source.kind='web'``, so hidden prose that gets through is still
#: data rather than instruction. Add to the list when a new trick is
#: observed; do not read the list as proof none remain.
_HIDDEN_STYLE_RE = re.compile(
    r"(display:none)"
    r"|(visibility:hidden)"
    # Zero opacity in every spelling, but not `opacity:0.5`.
    r"|(opacity:0(\.0+)?%?(?![.\d%]))"
    # Zero font-size in any unit, plus sub-pixel absolute sizes. Fractional
    # em/rem/% are left alone deliberately: `font-size:0.9rem` is ordinary
    # body text, and a false positive here deletes real content.
    r"|(font-size:0(\.0+)?(px|pt|pc|in|cm|mm|em|rem|ex|ch|%|vw|vh)?(?![.\d]))"
    r"|(font-size:0\.\d+(px|pt|pc|%)(?![.\d]))"
    r"|(height:0(\.0+)?(px|pt|em|rem|%|vh)?(?![.\d]))"
    # Pushed off-canvas in any direction.
    r"|(text-indent:-\d{3,})"
    r"|(left:-\d{3,})"
    r"|(top:-\d{3,})"
    r"|(right:-\d{3,})"
    r"|(bottom:-\d{3,})"
    # Clipped to nothing: the legacy property and its replacement.
    r"|(clip:rect\(0)"
    r"|(clip-path:inset\()"
    r"|(clip-path:circle\(0)"
    r"|(clip-path:polygon\(0px0px,0px0px)"
    # Scaled to nothing.
    r"|(transform:[^;]*scale3?d?\(0(\.0+)?[,)])",
    re.IGNORECASE,
)

#: Colour declarations, for the same-colour-as-the-background check. Anchored
#: on a declaration boundary so ``background-color:`` is not read as
#: ``color:``.
_FG_COLOR_RE = re.compile(r"(?:^|;)color:([^;]+)", re.IGNORECASE)
_BG_COLOR_RE = re.compile(r"(?:^|;)background(?:-color)?:([^;]+)", re.IGNORECASE)
_COLOR_TOKEN_RE = re.compile(
    r"#[0-9a-f]{3,8}|rgba?\([^)]*\)|hsla?\([^)]*\)|[a-z]{3,20}", re.IGNORECASE
)
_COLOR_WORDS = {"white": "#ffffff", "black": "#000000"}


def _colour_token(style: str, pattern: re.Pattern[str]) -> str | None:
    """The first colour value of one declaration, canonicalized enough to
    compare against another one."""
    declaration = pattern.search(style)
    if declaration is None:
        return None
    token = _COLOR_TOKEN_RE.search(declaration.group(1))
    if token is None:
        return None
    value = token.group(0).lower()
    if value.startswith("#") and len(value) == 4:
        value = "#" + "".join(character * 2 for character in value[1:])
    return _COLOR_WORDS.get(value, value)


def _same_as_background(style: str) -> bool:
    """``color:#fff;background:#fff`` — text painted onto itself.

    Only the exact-match case, and only when both are named in the *same*
    inline style. Inheriting a background from an ancestor is a stylesheet
    question this extractor cannot answer, so the check is deliberately the
    narrow one rather than a guess.
    """
    foreground = _colour_token(style, _FG_COLOR_RE)
    if foreground is None:
        return False
    return foreground == _colour_token(style, _BG_COLOR_RE)

#: Markers that a page's real content is assembled by a browser (design §5).
#: A closed list: each entry is something a *specific* framework or a
#: standard "turn JavaScript on" interstitial leaves in the served HTML, so
#: a false positive costs a page a ``wanted`` row rather than silently
#: dropping it.
JS_MARKERS: tuple[str, ...] = (
    "__NEXT_DATA__",
    "__NUXT__",
    "__remixContext",
    "data-reactroot",
    "ng-app",
    "enable javascript",
    "enable js",
    "javascript is required",
    "requires javascript",
    "please turn on javascript",
)

#: ``rel`` tokens and URL fragments that declare an open license. Detection
#: only — the *tier* decision is :func:`license_tier_for`, and the operator's
#: explicit ``--license-tier`` always outranks both.
_LICENSE_URL_MARKERS: tuple[str, ...] = (
    "creativecommons.org/licenses/",
    "creativecommons.org/publicdomain/",
    "opensource.org/licenses/",
    "spdx.org/licenses/",
    "gnu.org/licenses/",
)

#: TDM opt-out vocabulary (design §4 T4). Recorded on every fetch; enforced
#: only when ``[webfetch] honor_tdm_optout`` is true (ruling L-A3).
_TDM_OPTOUT_TOKENS: frozenset[str] = frozenset({"noai", "noimageai", "notrain", "noml"})


# ---------------------------------------------------------------------------
# the tiny DOM
# ---------------------------------------------------------------------------


@dataclass
class _Node:
    """One element. Children are ``_Node`` objects and ``str`` text nodes."""

    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list = field(default_factory=list)

    def iter_elements(self) -> Iterator["_Node"]:
        for child in self.children:
            if isinstance(child, _Node):
                yield child
                yield from child.iter_elements()


class _TreeBuilder(html.parser.HTMLParser):
    """Build a ``_Node`` tree, dropping comments as they arrive.

    Deliberately forgiving in one direction only: an end tag with no matching
    open tag is ignored, and an unclosed tag is closed by its parent's end —
    the same "never raise on bad markup" posture ``_BlockTextExtractor``
    takes. It is *not* forgiving about depth: past :data:`MAX_DEPTH` new
    elements are attached to the deepest allowed ancestor rather than nested
    further, so hostile nesting cannot turn a later recursive walk into a
    stack overflow.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#document")
        self._stack: list[_Node] = [self.root]
        #: Every comment's text, kept only so the C-injection test can prove
        #: the comment WAS in the input and is NOT in the output.
        self.comments: list[str] = []

    # -- stack helpers ---------------------------------------------------
    @property
    def _current(self) -> _Node:
        return self._stack[-1]

    def _close_implicit(self, tag: str) -> None:
        closes = _IMPLICIT_CLOSE.get(tag)
        if not closes:
            return
        barriers = _IMPLICIT_CLOSE_BARRIERS.get(tag, frozenset())
        cut: int | None = None
        for index in range(len(self._stack) - 1, 0, -1):
            current = self._stack[index].tag
            if current in barriers:
                break
            if current in closes:
                # Keep going: the OUTERMOST match inside the barrier is the
                # one to close (a new <tr> closes the open <tr>, not just the
                # <td> that happens to be on top of it).
                cut = index
        if cut is not None:
            del self._stack[cut:]

    def _open(self, tag: str, attrs: list[tuple[str, str | None]]) -> _Node:
        node = _Node(tag=tag, attrs={k.lower(): (v or "") for k, v in attrs})
        self._current.children.append(node)
        return node

    # -- HTMLParser hooks ------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        self._close_implicit(tag)
        node = self._open(tag, attrs)
        if tag not in VOID_TAGS and len(self._stack) < MAX_DEPTH:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open(tag.lower(), attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in VOID_TAGS:
            return
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self._current.children.append(data)

    def handle_comment(self, data: str) -> None:
        # Not appended to the tree at all: a comment is invisible text, and
        # invisible text is the injection channel this module exists to close.
        self.comments.append(data)


# ---------------------------------------------------------------------------
# decoding
# ---------------------------------------------------------------------------


def decode_html(raw: bytes, *, content_type: str | None = None) -> tuple[str, str]:
    """Decode a fetched body to text. Returns ``(text, encoding_used)``.

    Order (design §2.2): the ``Content-Type`` header's charset, then a
    ``<meta charset>`` in the first 4 KiB, then UTF-8. Always with
    ``errors="replace"``: a page whose bytes do not match its own declared
    encoding is a page we still want the readable 99 % of, and a decode
    exception here would turn a mildly broken page into a lost one.
    """
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8", errors="replace"), "utf-8"

    candidates: list[str] = []
    if content_type:
        match = _CONTENT_TYPE_CHARSET_RE.search(content_type)
        if match:
            candidates.append(match.group(1))
    meta = _CHARSET_META_RE.search(raw[:4096])
    if meta:
        candidates.append(meta.group(1).decode("ascii", errors="replace"))
    candidates.append("utf-8")

    for candidate in candidates:
        name = candidate.strip().strip("\"'").lower()
        if not name or name in ("utf8",):
            name = "utf-8"
        try:
            return raw.decode(name, errors="replace"), name
        except (LookupError, ValueError):
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8"


# ---------------------------------------------------------------------------
# visibility
# ---------------------------------------------------------------------------


def _is_hidden(node: _Node) -> bool:
    """Design §2.2's hidden-element list, in one predicate."""
    attrs = node.attrs
    if "hidden" in attrs:
        return True
    if attrs.get("aria-hidden", "").strip().lower() == "true":
        return True
    if attrs.get("type", "").strip().lower() == "hidden" and node.tag == "input":
        return True
    style = attrs.get("style")
    if style:
        collapsed = _WS_RE.sub("", _STYLE_COMMENT_RE.sub("", style))
        if _HIDDEN_STYLE_RE.search(collapsed):
            return True
        if _same_as_background(collapsed):
            return True
    return False


def _visible_children(node: _Node) -> Iterator:
    for child in node.children:
        if isinstance(child, str):
            yield child
        elif child.tag not in DROP_TAGS and not _is_hidden(child):
            yield child


def _text_of(node: _Node, depth: int = 0) -> str:
    """Visible text of a subtree, with block boundaries as spaces."""
    if depth > MAX_DEPTH:
        return ""
    parts: list[str] = []
    for child in _visible_children(node):
        if isinstance(child, str):
            parts.append(child)
        else:
            parts.append(" ")
            parts.append(_text_of(child, depth + 1))
            parts.append(" ")
    return "".join(parts)


def _word_count(text: str) -> int:
    return len(_WORD_RE.findall(text))


# ---------------------------------------------------------------------------
# main-block selection
# ---------------------------------------------------------------------------


def _find_first(root: _Node, tag: str) -> _Node | None:
    for node in root.iter_elements():
        if node.tag == tag and not _is_hidden(node):
            return node
    return None


def _densest_block(start: _Node) -> _Node:
    """Walk down while one child container still holds most of the text.

    This is the whole of "readability" here, and it is deliberately the
    boring version: no scoring heuristics over class names, no punctuation
    ratios, no machine learning. Descend while a single container child holds
    at least :data:`_DESCEND_RATIO` of the text, stop when the text has
    spread out. On a normal article that lands on the wrapper div around the
    body copy; on a page with no dominant block it stops at ``<body>`` and
    keeps everything, which is the safe direction to fail in — too much text
    is a chunking cost, too little is a lost document.
    """
    node = start
    for _ in range(MAX_DEPTH):
        total = len(_text_of(node).strip())
        if total == 0:
            return node
        best: _Node | None = None
        best_len = 0
        for child in _visible_children(node):
            if isinstance(child, str) or child.tag not in _CONTAINER_TAGS:
                continue
            length = len(_text_of(child).strip())
            if length > best_len:
                best, best_len = child, length
        if best is None or best_len < total * _DESCEND_RATIO:
            return node
        node = best
    return node


def _pick_main(root: _Node) -> _Node:
    """``<main>`` > ``<article>`` > the densest block under ``<body>``."""
    for tag in ("main", "article"):
        found = _find_first(root, tag)
        if found is not None and _text_of(found).strip():
            return found
    body = _find_first(root, "body")
    return _densest_block(body if body is not None else root)


# ---------------------------------------------------------------------------
# signals
# ---------------------------------------------------------------------------


def strip_invisible(value: str) -> str:
    """Remove the codepoints that render as nothing.

    Zero-width spaces and joiners, the BOM, the word joiner, the soft
    hyphen, the bidi controls (U+202E is the Trojan-Source one), variation
    selectors and the tag block. The *same* set the ingest sanitizer strips,
    by calling it — two lists that are meant to agree and are maintained
    apart is how one of them ends up shorter.

    Two artefacts on this side of the pipeline need it. ``<fetch_id>.md`` is
    written for a human and linked from HOME, and it does not pass through
    normalize, so the sanitizer never sees it; and the ``title`` reaches
    ``REQUESTS.md`` tables. Both are places where "what is stored is what a
    reader sees" has to hold, and an invisible codepoint is precisely a
    character that breaks it.
    """
    from trialerror.ingest.sanitizer import sanitize

    return sanitize(value)[0]


def _one_line(value: str | None, *, cap: int) -> str | None:
    """Flatten to a single capped line with no pipes (T3, metadata as data).

    Pipes go because these values are rendered into markdown tables in
    ``REQUESTS.md`` and HOME; newlines go because a value that spans lines
    can forge a second table row; invisible codepoints go because a title an
    operator cannot fully see is a title they cannot review.
    """
    if value is None:
        return None
    flat = " ".join(strip_invisible(value).replace("|", "/").split())
    if not flat:
        return None
    return flat[:cap].strip() or None


def _meta_map(root: _Node) -> dict[str, str]:
    """``name``/``property``/``http-equiv`` → ``content``, lowercased keys.

    ``<meta>`` is dropped from the OUTPUT; it is read here first, because
    that is where the honest provenance signals live (author, publication
    date, TDM reservations). Reading a value and refusing to serialize it is
    the point, not a contradiction.
    """
    out: dict[str, str] = {}
    for node in root.iter_elements():
        if node.tag != "meta":
            continue
        content = node.attrs.get("content")
        if content is None:
            continue
        for key in ("name", "property", "http-equiv", "itemprop"):
            label = node.attrs.get(key)
            if label:
                out.setdefault(label.strip().lower(), content)
    return out


def _first_meta(metas: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = metas.get(name)
        if value and value.strip():
            return value
    return None


def _detect_title(root: _Node, metas: Mapping[str, str]) -> str | None:
    title_node = _find_first(root, "title")
    raw = _text_of(title_node) if title_node is not None else None
    if not (raw and raw.strip()):
        raw = _first_meta(metas, "og:title", "twitter:title", "dc.title")
    if not (raw and raw.strip()):
        heading = _find_first(root, "h1")
        raw = _text_of(heading) if heading is not None else None
    return _one_line(raw, cap=MAX_TITLE_CHARS)


def _detect_author(root: _Node, metas: Mapping[str, str]) -> str | None:
    raw = _first_meta(metas, "author", "article:author", "dc.creator", "citation_author")
    if raw is None:
        for node in root.iter_elements():
            if "author" in node.attrs.get("rel", "").lower():
                raw = _text_of(node)
                break
    return _one_line(raw, cap=MAX_SIGNAL_CHARS)


def _detect_published(root: _Node, metas: Mapping[str, str]) -> str | None:
    raw = _first_meta(
        metas,
        "article:published_time",
        "citation_publication_date",
        "date",
        "dc.date",
        "og:updated_time",
    )
    if raw is None:
        for node in root.iter_elements():
            if node.tag == "time" and node.attrs.get("datetime"):
                raw = node.attrs["datetime"]
                break
    return _one_line(raw, cap=MAX_SIGNAL_CHARS)


def _detect_canonical(root: _Node, metas: Mapping[str, str], base_url: str) -> str | None:
    href: str | None = None
    for node in root.iter_elements():
        if node.tag == "link" and "canonical" in node.attrs.get("rel", "").lower():
            href = node.attrs.get("href")
            break
    if href is None:
        href = _first_meta(metas, "og:url")
    if not href:
        return None
    return _one_line(_absolutize(href, base_url), cap=MAX_SIGNAL_CHARS)


def _detect_license(root: _Node, metas: Mapping[str, str], base_url: str) -> str | None:
    """A declared license, as the page declares it.

    Preference order is deliberate: an explicit ``rel="license"`` is the
    machine-readable statement the page chose to make, so it wins over a
    Creative Commons URL that merely appears somewhere in the markup (a link
    to a CC page in a footer's list of "sites we like" is not a license
    grant).
    """
    for node in root.iter_elements():
        if node.tag in ("link", "a") and "license" in node.attrs.get("rel", "").lower():
            href = node.attrs.get("href")
            if href:
                return _one_line(_absolutize(href, base_url), cap=MAX_SIGNAL_CHARS)
    declared = _first_meta(metas, "dc.rights", "dcterms.license", "license")
    if declared:
        return _one_line(declared, cap=MAX_SIGNAL_CHARS)
    for node in root.iter_elements():
        href = node.attrs.get("href", "")
        lowered = href.lower()
        if any(marker in lowered for marker in _LICENSE_URL_MARKERS):
            return _one_line(_absolutize(href, base_url), cap=MAX_SIGNAL_CHARS)
    return None


def license_tier_for(license_detected: str | None) -> str | None:
    """Map a detected license signal to a ``source.license_tier``.

    Returns ``"open"`` for a recognizable open-license declaration and
    ``None`` for everything else — including a license this function does not
    recognize. ``None`` means "say nothing", and the caller falls back to the
    program's configured default (``unknown``). Deliberately never returns
    ``commercial_restricted``: a fence that strong is the operator's call
    (design §4 T4), never an inference from markup.
    """
    if not license_detected:
        return None
    lowered = license_detected.lower()
    if any(marker in lowered for marker in _LICENSE_URL_MARKERS):
        return "open"
    if re.search(r"\b(cc[ -]by|cc0|public domain|mit licen|apache licen)\b", lowered):
        return "open"
    return None


def _detect_lang(root: _Node) -> str | None:
    html_node = _find_first(root, "html")
    if html_node is not None:
        lang = html_node.attrs.get("lang") or html_node.attrs.get("xml:lang")
        if lang:
            return _one_line(lang, cap=32)
    return None


def _detect_tdm(metas: Mapping[str, str], root: _Node, headers: Mapping[str, Any] | None) -> dict:
    """Record every TDM / AI-training opt-out signal the page carries.

    Recorded on every fetch regardless of the knob (ruling L-A3), because the
    posture may change later and a fetch that did not write the signal down
    cannot be re-judged without re-fetching.
    """
    signals: dict[str, Any] = {}
    robots_meta = _first_meta(metas, "robots", "googlebot")
    if robots_meta:
        signals["meta_robots"] = _one_line(robots_meta, cap=MAX_SIGNAL_CHARS)
    header_value = None
    if headers:
        for key, value in headers.items():
            if str(key).lower() == "x-robots-tag" and value:
                header_value = str(value)
                break
    if header_value:
        signals["x_robots_tag"] = _one_line(header_value, cap=MAX_SIGNAL_CHARS)

    reservation = _first_meta(metas, "tdm-reservation")
    policy = _first_meta(metas, "tdm-policy")
    for node in root.iter_elements():
        rel = node.attrs.get("rel", "").lower()
        if node.tag == "link" and "tdm-reservation" in rel:
            reservation = reservation or node.attrs.get("content") or node.attrs.get("href")
        if node.tag == "link" and "tdm-policy" in rel:
            policy = policy or node.attrs.get("href")
    if reservation:
        signals["tdm_reservation"] = _one_line(str(reservation), cap=MAX_SIGNAL_CHARS)
    if policy:
        signals["tdm_policy"] = _one_line(str(policy), cap=MAX_SIGNAL_CHARS)

    haystack = " ".join(
        str(v).lower() for v in (signals.get("meta_robots"), signals.get("x_robots_tag")) if v
    )
    tokens = {t.strip() for t in re.split(r"[,\s;]+", haystack) if t.strip()}
    optout = bool(tokens & _TDM_OPTOUT_TOKENS)
    if str(signals.get("tdm_reservation", "")).strip() == "1":
        optout = True
    signals["optout"] = optout
    return signals


def _detect_js_markers(raw_text: str, root: _Node) -> list[str]:
    """Evidence that the served HTML is a shell a browser was meant to fill."""
    found: list[str] = []
    lowered = raw_text.lower()
    for marker in JS_MARKERS:
        # A ``__``-prefixed marker is a framework's exact identifier, so it is
        # matched case-sensitively; the rest are prose a page writes however
        # it likes ("Enable JavaScript", "ENABLE JAVASCRIPT").
        hit = marker in raw_text if marker.startswith("__") else marker.lower() in lowered
        if hit:
            found.append(marker)
    for node in root.iter_elements():
        if node.attrs.get("id", "").strip().lower() in ("root", "app", "__next"):
            if not _text_of(node).strip():
                found.append(f"empty #{node.attrs['id'].strip().lower()}")
    # Stable, de-duplicated, and bounded: this list is a column, not a log.
    return sorted(set(found))[:16]


# ---------------------------------------------------------------------------
# links
# ---------------------------------------------------------------------------


def _absolutize(href: str, base_url: str) -> str:
    href = href.strip()
    if not href:
        return ""
    try:
        return urljoin(base_url, href)
    except ValueError:
        return ""


def _http_url(candidate: str) -> str | None:
    if not candidate or len(candidate) > MAX_LINK_CHARS:
        return None
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return candidate


def _collect_links(node: _Node, base_url: str) -> list[str]:
    """Every http(s) link in the kept subtree, in document order.

    **Recorded, never enqueued.** There is no caller of this function that
    creates a job; ``trialerror webfetch links <fetch_id>`` prints the list so
    a human or an agent can choose one and pass it back through ``add`` —
    which re-runs every policy check from the start (design §1 P3).
    """
    seen: dict[str, None] = {}
    stack: list[tuple[_Node, int]] = [(node, 0)]
    ordered: list[str] = []
    while stack:
        current, depth = stack.pop()
        if depth > MAX_DEPTH:
            continue
        if current.tag == "a":
            url = _http_url(_absolutize(current.attrs.get("href", ""), base_url))
            if url is not None and url not in seen:
                seen[url] = None
                ordered.append(url)
                if len(ordered) >= MAX_LINKS:
                    return ordered
        children = [c for c in _visible_children(current) if isinstance(c, _Node)]
        for child in reversed(children):
            stack.append((child, depth + 1))
    return ordered


# ---------------------------------------------------------------------------
# serialization
# ---------------------------------------------------------------------------


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_attr(value: str) -> str:
    return _escape(value).replace('"', "&quot;")


def _serialize(node: _Node, base_url: str, depth: int = 0) -> str:
    """The cleaned subtree as HTML, attributes stripped to :data:`_KEEP_ATTRS`."""
    if depth > MAX_DEPTH:
        return ""
    parts: list[str] = []
    for child in _visible_children(node):
        if isinstance(child, str):
            parts.append(_escape(child))
            continue
        tag = child.tag
        keep = _KEEP_ATTRS.get(tag, frozenset())
        rendered: list[str] = []
        for name in sorted(keep):
            value = child.attrs.get(name)
            if not value:
                continue
            if name == "href":
                absolute = _http_url(_absolutize(value, base_url))
                if absolute is None:
                    continue
                value = absolute
            rendered.append(f' {name}="{_escape_attr(value)}"')
        opening = f"<{tag}{''.join(rendered)}>"
        if tag in VOID_TAGS:
            parts.append(opening)
            continue
        parts.append(opening)
        parts.append(_serialize(child, base_url, depth + 1))
        parts.append(f"</{tag}>")
    return "".join(parts)


def _clean_document(body_html: str, *, title: str | None, lang: str | None) -> str:
    """Wrap the cleaned subtree in a head this module wrote.

    The page's own ``<head>`` is dropped whole. What comes back is ours: a
    UTF-8 declaration (the file is always written UTF-8, and a human opening
    it in a browser should see that), the normalized title, and the detected
    language. ``normalize_html`` skips ``<head>`` entirely, so the title here
    is metadata for people and never becomes a corpus element — the elements
    are exactly the article's own markup, with nothing synthesized.
    """
    lang_attr = f' lang="{_escape_attr(lang)}"' if lang else ""
    title_html = _escape(title) if title else ""
    return (
        "<!DOCTYPE html>\n"
        f"<html{lang_attr}>\n"
        "<head>\n"
        '<meta charset="utf-8">\n'
        f"<title>{title_html}</title>\n"
        "</head>\n"
        "<body>\n"
        f"{body_html}\n"
        "</body>\n"
        "</html>\n"
    )


# ---------------------------------------------------------------------------
# markdown
# ---------------------------------------------------------------------------


def _inline_markdown(
    node: _Node, base_url: str, depth: int = 0, skip: frozenset[str] = frozenset()
) -> str:
    if depth > MAX_DEPTH:
        return ""
    parts: list[str] = []
    for child in _visible_children(node):
        if isinstance(child, str):
            parts.append(child)
            continue
        tag = child.tag
        if tag in skip:
            continue
        inner = _inline_markdown(child, base_url, depth + 1, skip)
        if tag == "a":
            href = _http_url(_absolutize(child.attrs.get("href", ""), base_url))
            text = " ".join(inner.split()) or href or ""
            parts.append(f"[{text}]({href})" if href else text)
        elif tag in ("strong", "b"):
            parts.append(f"**{inner.strip()}**" if inner.strip() else "")
        elif tag in ("em", "i"):
            parts.append(f"*{inner.strip()}*" if inner.strip() else "")
        elif tag == "code":
            parts.append(f"`{inner.strip()}`" if inner.strip() else "")
        elif tag == "br":
            parts.append("\n")
        else:
            parts.append(inner)
    return "".join(parts)


def _clean_line(text: str) -> str:
    return _WS_RE.sub(" ", text.replace("\n", " ")).strip()


def _table_markdown(node: _Node, base_url: str) -> list[str]:
    rows: list[list[str]] = []
    for row in node.iter_elements():
        if row.tag != "tr" or _is_hidden(row):
            continue
        cells = [
            _clean_line(_inline_markdown(cell, base_url)).replace("|", "\\|")
            for cell in row.children
            if isinstance(cell, _Node) and cell.tag in ("td", "th") and not _is_hidden(cell)
        ]
        if cells:
            rows.append(cells)
    if not rows:
        return []
    width = max(len(r) for r in rows)
    lines = ["| " + " | ".join((r + [""] * width)[:width]) + " |" for r in rows]
    lines.insert(1, "|" + "|".join(["---"] * width) + "|")
    return lines


_LIST_TAGS = frozenset({"ul", "ol"})


def _list_markdown(node: _Node, base_url: str, out: list[str], depth: int, indent: int) -> None:
    """Render one list, recursing into nested lists with an indent.

    Nesting is followed rather than flattened because a nested list is often
    the actual structure of a docs page (a table of contents, an option tree),
    and a flattened one reads as a single run-on line.
    """
    ordered = node.tag == "ol"
    prefix = "  " * indent
    index = 1
    for item in _visible_children(node):
        if isinstance(item, str) or item.tag != "li":
            continue
        text = _clean_line(_inline_markdown(item, base_url, skip=_LIST_TAGS))
        if text:
            out.append(f"{prefix}{index}. {text}" if ordered else f"{prefix}- {text}")
            index += 1
        for nested in _visible_children(item):
            if isinstance(nested, _Node) and nested.tag in _LIST_TAGS and depth < MAX_DEPTH:
                _list_markdown(nested, base_url, out, depth + 1, indent + 1)


def _block_markdown(node: _Node, base_url: str, out: list[str], depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        return
    for child in _visible_children(node):
        if isinstance(child, str):
            text = _clean_line(child)
            if text:
                out.append(text)
                out.append("")
            continue
        tag = child.tag
        if tag in _HEADINGS:
            text = _clean_line(_inline_markdown(child, base_url))
            if text:
                out.append(f"{'#' * _HEADINGS[tag]} {text}")
                out.append("")
        elif tag in _LIST_TAGS:
            before = len(out)
            _list_markdown(child, base_url, out, depth + 1, 0)
            if len(out) > before:
                out.append("")
        elif tag == "table":
            lines = _table_markdown(child, base_url)
            if lines:
                out.extend(lines)
                out.append("")
        elif tag == "pre":
            code = _text_of(child).strip("\n")
            if code.strip():
                out.append("```")
                out.append(code)
                out.append("```")
                out.append("")
        elif tag == "blockquote":
            inner: list[str] = []
            _block_markdown(child, base_url, inner, depth + 1)
            for line in inner:
                out.append(f"> {line}" if line else ">")
            out.append("")
        elif tag == "hr":
            out.append("---")
            out.append("")
        elif tag in ("p", "figcaption", "dt", "dd", "li"):
            text = _clean_line(_inline_markdown(child, base_url))
            if text:
                out.append(text)
                out.append("")
        elif tag in _BLOCK_LEVEL or tag in _CONTAINER_TAGS:
            _block_markdown(child, base_url, out, depth + 1)
        else:
            text = _clean_line(_inline_markdown(child, base_url))
            if text:
                out.append(text)
                out.append("")


def _frontmatter(values: Mapping[str, Any]) -> list[str]:
    """``key: "json-quoted string"`` lines (design §2.2).

    JSON quoting rather than YAML for one reason: nothing in this harness
    parses YAML, and a title containing a colon, a quote or a newline must
    not be able to invent a second key. ``json.dumps`` on the value is
    exactly the escaping that makes that impossible.
    """
    lines = ["---"]
    for key, value in values.items():
        if value is None or value == [] or value == {}:
            continue
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False, sort_keys=True)}")
    lines.append("---")
    lines.append("")
    return lines


def render_markdown(main: _Node, *, base_url: str, frontmatter: Mapping[str, Any]) -> str:
    body: list[str] = []
    _block_markdown(main, base_url, body)
    text = "\n".join(_frontmatter(frontmatter) + body).rstrip() + "\n"
    # The one artefact on this path an operator actually reads. The clean
    # HTML is sanitized downstream at element insert; this file is not on
    # that path, so the strip happens here or nowhere.
    return strip_invisible(_MULTI_NEWLINE_RE.sub("\n\n", text))


# ---------------------------------------------------------------------------
# the result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Extraction:
    """Everything one page yields. Ids and counts, never page text, are what
    a caller is allowed to put in front of an agent (C-0007, design §4 T3)."""

    clean_html: str
    markdown: str
    text: str
    title: str | None
    author: str | None
    published: str | None
    canonical_link: str | None
    license_detected: str | None
    lang: str | None
    extracted_words: int
    links: tuple[str, ...]
    thin_content: bool
    js_markers: tuple[str, ...]
    tdm_signals: dict
    encoding: str
    extractor_version: str = EXTRACTOR_VERSION

    @property
    def needs_render(self) -> bool:
        """Design §5: thin **and** JS-marked means a browser was required.

        Thin without a marker is just a short page, and a short page is still
        data — it is ingested with ``thin_content`` set rather than sent to
        the operator as unfetchable."""
        return self.thin_content and bool(self.js_markers)

    @property
    def license_tier(self) -> str | None:
        return license_tier_for(self.license_detected)

    def signals(self) -> dict[str, Any]:
        """The extraction signals as the ``web_fetch`` row stores them."""
        return {
            "title": self.title,
            "author": self.author,
            "published": self.published,
            "canonical_link": self.canonical_link,
            "license_detected": self.license_detected,
            "lang": self.lang,
            "extracted_words": self.extracted_words,
            "thin_content": 1 if self.thin_content else 0,
            "links_json": json.dumps(list(self.links), ensure_ascii=False),
            "tdm_signals_json": json.dumps(self.tdm_signals, ensure_ascii=False, sort_keys=True),
            "extractor_version": self.extractor_version,
        }


def extract_html(
    raw: bytes | str,
    *,
    final_url: str,
    content_type: str | None = None,
    headers: Mapping[str, Any] | None = None,
    fetched_ts: str | None = None,
    fetch_id: str | None = None,
    extra_frontmatter: Mapping[str, Any] | None = None,
) -> Extraction:
    """Extract one HTML page (design §2.2, the html branch).

    ``raw`` is the fetched body — bytes preferred, so the encoding sniff can
    do its job. ``final_url`` is the post-redirect URL and is what relative
    links resolve against; using the *requested* URL there would silently
    misattribute every link on a page that redirected.
    """
    if isinstance(raw, bytes):
        text, encoding = decode_html(raw, content_type=content_type)
    else:
        text, encoding = raw, "utf-8"

    builder = _TreeBuilder()
    builder.feed(text)
    builder.close()
    root = builder.root

    metas = _meta_map(root)
    title = _detect_title(root, metas)
    lang = _detect_lang(root)
    main = _pick_main(root)

    body_html = _serialize(main, final_url)
    visible = " ".join(_text_of(main).split())
    words = _word_count(visible)

    extraction_frontmatter: dict[str, Any] = {
        "url": final_url,
        "title": title,
        "author": _detect_author(root, metas),
        "published": _detect_published(root, metas),
        "canonical": _detect_canonical(root, metas, final_url),
        "license": _detect_license(root, metas, final_url),
        "lang": lang,
        "fetched": fetched_ts,
        "fetch_id": fetch_id,
        "words": words,
        "extractor": EXTRACTOR_VERSION,
    }
    if extra_frontmatter:
        extraction_frontmatter.update(dict(extra_frontmatter))

    return Extraction(
        clean_html=_clean_document(body_html, title=title, lang=lang),
        markdown=render_markdown(main, base_url=final_url, frontmatter=extraction_frontmatter),
        text=visible,
        title=title,
        author=extraction_frontmatter["author"],
        published=extraction_frontmatter["published"],
        canonical_link=extraction_frontmatter["canonical"],
        license_detected=extraction_frontmatter["license"],
        lang=lang,
        extracted_words=words,
        links=tuple(_collect_links(main, final_url)),
        thin_content=words < THIN_CONTENT_WORDS,
        js_markers=tuple(_detect_js_markers(text, root)),
        tdm_signals=_detect_tdm(metas, root, headers),
        encoding=encoding,
    )
