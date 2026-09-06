"""The plain-register style contract, AS CODE.

``docs/reviews/AISPEAK_TRANSLATOR_DESIGN.md`` Section 4.5 writes the
contract as fifteen prose rules and calls it "an executable checklist".
This module is the executable half: :func:`check_style` runs every rule
that can be decided from the two texts alone (the original post body and
its candidate translation) and returns a list of typed
:class:`StyleViolation` rows. :data:`TRANSLATION_INSTRUCTION` in
:mod:`trialerror.feed_translate.api` is the prose half, carried verbatim in
the envelope a translator backend fills -- the two are deliberately the
same fifteen rules read twice, once for the writer and once for the
checker, and each rule's :attr:`StyleViolation.rule` id names which §4.5
line it enforces.

**Two severities, and the whole gate hangs off the split**
(:mod:`trialerror.feed_translate.gate` is what acts on them):

- ``"fidelity"`` -- §4.5 rules 8-11, the ones that paper calls
  "non-negotiable". A fidelity violation means the translation says
  something the original did not, or drops something the original did say:
  a missing booking/launch/campaign id, an invented number, a hedge
  promoted to a fact. These FAIL the gate, always, and a failed
  translation is never served (the original stays -- design Section 4.3.3:
  "a wrong translation of a gate verdict or a booking id is worse than no
  translation").
- ``"register"`` -- §4.5 rules 1-7 and 12-14, sentence shape and word
  choice. A long sentence is a worse rendering, not a false one. These are
  recorded on the row's ``gate_reasons`` and surfaced, but do not withhold
  the translation unless the program opts into
  ``[feed.translator] strict_style = true``. That split is STE-100's own
  stated behavior for a rule it cannot mechanically settle: "flag the
  trade-off instead of silently simplifying" (Section 1, the
  ``asd-ste100-skill`` row).

**One deliberate promotion from the design's own text.** §4.3.2 designs
the hedge-preservation scan as "advisory/flagged rather than a hard gate";
§4.5 rule 9 lists the SAME rule under "Fidelity ... non-negotiable". This
module resolves the contradiction in favour of §4.5, because the operator
requirement this feature exists to satisfy is that a reader can trust the
right-hand column, and "DEFERRED, not FAILED" rendered as "failed" is the
exact failure that would destroy that trust. What stays advisory is the
*per-sentence alignment* §4.3.2 actually worries about (which hedge covers
which claim -- not decidable without a parser); what is enforced is the
document-level invariant: every hedge FAMILY present in the original is
still present somewhere in the translation.

**What this module deliberately does NOT check**, so the omissions are
visible rather than silently assumed:

- §4.5 rule 6 (<=3-word noun clusters) needs part-of-speech tagging to
  distinguish a noun stack from an ordinary phrase; no tagger ships in
  this repo and a regex approximation would fire on ordinary English.
  Left to the writer-side instruction only.
- §4.5 rules 10-11 (no invented fact, cause, or mechanism; no invented
  rejected-alternative) are semantic, not lexical. The number/id/date
  invariants below are their mechanically-checkable *shadow*; the full
  check is the judged tier in :mod:`trialerror.feed_translate.gate`.
- §4.5 rule 15 (inline gloss vs glossary link) is inert until a glossary
  table exists (design Section 4.3.4) -- there is none in code today.
- (FT-3, fix pass) rule 8's number check is a MULTISET, not a full
  identity check: it catches a count that changed ("1 gate moved" said
  three times) but NOT the same numbers reassigned to different entities
  ("3 gates and 5 pools" -> "5 gates and 3 pools" passes -- same multiset,
  different claim). Catching that requires knowing WHICH noun a number
  binds to, which is semantic, not lexical.
- (FT-3, fix pass) neither tier checks for a DROPPED CLAIM -- a
  translation that keeps every id/number/date/hedge from one sentence but
  silently omits an entire OTHER sentence passes both rule 8 and rule 9,
  and would pass the judged tier too (Ragas-style faithfulness is
  precision-only: it decomposes the TRANSLATION and checks each piece
  against the original, so a piece that was never written in the first
  place is invisible to it). This is unenforced, not merely undetected by
  this module -- design Section 4.5's "a translation, not a summary" /
  C-0047's "full text" invariant has no automated check anywhere in this
  package today.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Sequence

__all__ = [
    "STYLE_MODES",
    "DEFAULT_STYLE_MODE",
    "MAX_SENTENCE_WORDS",
    "HEDGE_FAMILIES",
    "BANNED_PHRASAL_VERBS",
    "BANNED_MARKETING_WORDS",
    "BANNED_FILLER_PHRASES",
    "StyleViolation",
    "StyleReport",
    "extract_fidelity_tokens",
    "hedge_families_present",
    "split_sentences",
    "check_style",
]

#: ``feed_post_translation.style_mode``'s CHECK constraint, re-stated here
#: so this module refuses a bad mode before SQLite has to.
STYLE_MODES: frozenset[str] = frozenset({"strict", "flavored"})

#: Design Section 4.5's "Mode selection": Feed posts are explanatory status
#: prose, so ``flavored`` (structural rules enforced, lexical lockdown not)
#: is the default; ``strict`` is for a post whose body is itself a
#: procedure.
DEFAULT_STYLE_MODE = "flavored"

#: §4.5 rule 1: "<=25 words per sentence". ``strict`` mode takes STE-100's
#: tighter procedural limit (20) -- the same two numbers the source skill
#: itself splits on ("<=20/25-word sentences", Section 1).
MAX_SENTENCE_WORDS: dict[str, int] = {"flavored": 25, "strict": 20}

#: §4.5 rule 9 / §4.3.2's hedge set, grouped into FAMILIES rather than a
#: flat word list. A family is satisfied when ANY of its members survives
#: into the translation, so the intended plain-English rendering of a hedge
#: ("may have failed" -> "might have failed") passes, while dropping the
#: modality entirely ("failed") does not. The design's own nine words
#: (``may``, ``could``, ``provisional(ly)``, ``pending``, ``deferred``,
#: ``tentative(ly)``, ``possibly``, ``likely``, ``unconfirmed``) are all
#: here; each family adds only the plainer synonyms a good translation is
#: actively encouraged to reach for.
HEDGE_FAMILIES: dict[str, tuple[str, ...]] = {
    "possibility": ("may", "might", "could", "possibly", "perhaps", "potentially"),
    "provisional": ("provisional", "provisionally", "tentative", "tentatively", "preliminary", "for now"),
    "pending": ("pending", "awaiting", "not yet", "still open", "outstanding"),
    "deferred": ("deferred", "deferral", "postponed", "on hold", "put off"),
    "likelihood": ("likely", "unlikely", "probably", "probable", "expected", "we expect"),
    "unconfirmed": ("unconfirmed", "unverified", "not confirmed", "not verified", "unproven"),
}

#: §4.5 rule 5: "no phrasal verbs where a single plain verb exists". A
#: short, closed list of the ops-register offenders that actually show up
#: in Feed bodies -- not an attempt at English-wide phrasal-verb detection.
BANNED_PHRASAL_VERBS: tuple[str, ...] = (
    "spin up", "spun up", "dive into", "dive in", "reach out", "circle back",
    "drill down", "loop in", "kick off", "stand up", "ramp up", "double down",
    "unpack", "surface up", "dial in",
)

#: §4.5 rule 12: "no inflated-importance framing, no sales language". The
#: Humanizer skill's AI-tell vocabulary, trimmed to words that are always
#: wrong in a status rendering (a word that can be a legitimate fact --
#: "critical path", "blocked" -- is deliberately absent).
BANNED_MARKETING_WORDS: tuple[str, ...] = (
    "pivotal", "robust", "seamless", "cutting-edge", "game-changing", "revolutionize",
    "revolutionary", "best-in-class", "world-class", "state-of-the-art", "empower",
    "unlock", "delve", "tapestry", "testament to", "showcase", "paradigm shift",
    "leverages", "leveraging", "groundbreaking",
)

#: §4.5 rule 13: "no chatbot filler ... A Feed translation is a rendering
#: of the post, not a reply to it."
BANNED_FILLER_PHRASES: tuple[str, ...] = (
    "hope this helps", "let me know", "feel free", "great question", "happy to help",
    "in conclusion", "it's worth noting", "it is worth noting", "it's important to note",
    "it is important to note", "as an ai", "i hope",
)

#: §4.5 rule 14: "no em/en dashes unless the original itself uses them at
#: the same rate".
_DASH_RE = re.compile(r"[–—]")

#: ISO-8601-ish dates, matched BEFORE ids and numbers so ``2026-09-05``
#: stays one token instead of decomposing into 2026 / 09 / 05.
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?\b")

#: Typed ids, the shape ``trialerror.util.ids.new_id`` mints
#: (``LNCH-01J...``, ``POST-01J...``, ``XLAT-01J...``) AND the legacy
#: pinned styles a origin-project-derived program keeps (``C-0073``, ``CR-112``,
#: ``WKP-063``, ``G1`` is not matched -- it has no separator, see
#: :func:`extract_fidelity_tokens`'s docstring).
_ID_RE = re.compile(r"\b[A-Z][A-Z0-9]{0,15}-[0-9A-Za-z]{1,32}\b")

#: Bare numbers, including decimals/thousands separators and a trailing
#: percent sign, once dates and ids have been removed.
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*%?")

#: A leading ordered-list marker (``1.`` / ``2)`` / ``3 -``) at the start of
#: a line. §4.5 rule 7 actively ASKS the translator to turn a buried
#: sequence into a numbered list, so those markers must not count as
#: "numbers the original didn't have" (rule 8's inverse).
_LIST_MARKER_RE = re.compile(r"^[ \t]*\(?\d{1,2}[.)\-][ \t]+", re.MULTILINE)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])[\s\n]+")
_WORD_RE = re.compile(r"[A-Za-z0-9'@/_-]+")


@dataclass(frozen=True)
class StyleViolation:
    """One broken rule. ``rule`` is the §4.5 line it enforces (``"r8_ids"``
    reads as "Section 4.5 rule 8, the id half"), ``severity`` is
    ``"fidelity"`` or ``"register"`` (module docstring), ``detail`` names
    the exact offending text so a re-translation can be surgical rather
    than a blind retry."""

    rule: str
    severity: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"rule": self.rule, "severity": self.severity, "detail": self.detail}


@dataclass(frozen=True)
class StyleReport:
    """Every violation found, plus the two convenience predicates callers
    actually branch on. ``hedges_lost`` is broken out separately from
    ``violations`` (it is also IN ``violations``, as ``r9_hedge`` rows)
    because the gate records the lost families verbatim on the stored row
    -- an operator reading a withheld translation wants to see "the
    'deferred' hedge vanished", not to re-parse a violation list."""

    violations: list[StyleViolation] = field(default_factory=list)
    hedges_lost: list[str] = field(default_factory=list)
    style_mode: str = DEFAULT_STYLE_MODE

    @property
    def fidelity_violations(self) -> list[StyleViolation]:
        return [v for v in self.violations if v.severity == "fidelity"]

    @property
    def register_violations(self) -> list[StyleViolation]:
        return [v for v in self.violations if v.severity == "register"]

    @property
    def ok(self) -> bool:
        """No violation of ANY severity -- a translation that passes the
        whole checklist. The gate's own disposition is looser than this by
        default (register violations are advisory); this property exists
        for tests and for ``strict_style`` programs."""
        return not self.violations

    def as_dict(self) -> dict[str, Any]:
        return {
            "style_mode": self.style_mode,
            "violations": [v.as_dict() for v in self.violations],
            "hedges_lost": list(self.hedges_lost),
        }


def split_sentences(text: str) -> list[str]:
    """Sentences, for the length rule. Same
    ``(?<=[.!?])\\s+`` split :mod:`trialerror.verify.citecheck` uses for
    citation binding -- one convention for "what is a sentence" across the
    codebase, not two."""
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text or "") if s.strip()]


def extract_fidelity_tokens(text: str, *, strip_list_markers: bool = False) -> dict[str, list[str]]:
    """The §4.5 rule 8 token inventory: ``{"dates": [...], "ids": [...],
    "numbers": [...]}``, each list deduplicated and sorted so two calls on
    the same text compare cleanly.

    Extraction is ordered date -> id -> number, each pass blanking its own
    matches out of the working text, so ``2026-09-05`` is one date (not
    three numbers) and ``LNCH-01JXYZ`` is one id (not a number ``01``).

    ``strip_list_markers=True`` first removes a leading ordered-list marker
    from every line -- used ONLY on the translation side, because §4.5 rule
    7 explicitly asks the translator to introduce numbered lists, and those
    markers must not read as invented numbers under rule 8's inverse.

    A bare alphanumeric token with no separator (``G1``, ``v2``) is
    deliberately NOT treated as an id: it is indistinguishable from an
    ordinary word, and false-flagging one would fail honest translations.
    Such tokens still surface through the ``numbers`` list when they carry
    digits.
    """
    working = text or ""
    if strip_list_markers:
        working = _LIST_MARKER_RE.sub("", working)

    dates = _DATE_RE.findall(working)
    working = _DATE_RE.sub(" ", working)

    ids = _ID_RE.findall(working)
    working = _ID_RE.sub(" ", working)

    numbers = _NUMBER_RE.findall(working)

    return {
        "dates": sorted(set(dates)),
        "ids": sorted(set(ids)),
        "numbers": sorted(set(numbers)),
    }


def _number_counts(text: str, *, strip_list_markers: bool = False) -> Counter:
    """FT-3 (fix pass): the same date/id/number extraction pipeline as
    :func:`extract_fidelity_tokens`, but preserving MULTIPLICITY for
    numbers rather than deduplicating them into a set.

    ``extract_fidelity_tokens``'s own contract (a deduplicated, sorted
    list) is kept stable for its existing callers -- this is a separate,
    private extraction used only by :func:`check_style`'s rule 8, because
    a set comparison cannot tell "1 gate moved" from "1 gate moved. 1 gate
    moved. 1 gate moved.": both have the number set ``{'1'}``, but the
    second one asserts something the original never said. A REPEATED count
    is a distinct claim from a single one; ids and dates are left on the
    set-based check (a repeated id is not a distinct claim the same way a
    repeated count is)."""
    working = text or ""
    if strip_list_markers:
        working = _LIST_MARKER_RE.sub("", working)
    working = _DATE_RE.sub(" ", working)
    working = _ID_RE.sub(" ", working)
    return Counter(_NUMBER_RE.findall(working))


def hedge_families_present(text: str) -> set[str]:
    """Which :data:`HEDGE_FAMILIES` appear in ``text``. Whole-word matching
    for single words (so ``"maybe"`` does not satisfy the ``may`` family by
    prefix, and ``"deferred"`` does not fire on ``"undeferred"``);
    substring matching for multi-word members (``"not yet"``), which cannot
    collide with a longer word."""
    lowered = (text or "").lower()
    words = set(_WORD_RE.findall(lowered))
    present: set[str] = set()
    for family, members in HEDGE_FAMILIES.items():
        for member in members:
            if " " in member:
                if member in lowered:
                    present.add(family)
                    break
            elif member in words:
                present.add(family)
                break
    return present


def _find_phrases(text: str, phrases: Sequence[str]) -> list[str]:
    lowered = (text or "").lower()
    return [p for p in phrases if p in lowered]


def check_style(
    original: str,
    translation: str,
    *,
    style_mode: str = DEFAULT_STYLE_MODE,
) -> StyleReport:
    """Run the whole checklist over one ``(original, translation)`` pair.

    Never raises for a bad translation -- a broken rule is data, not an
    exception (the gate decides what to do with it). Raises only for a
    ``style_mode`` this contract does not define, which is a caller bug.
    """
    if style_mode not in STYLE_MODES:
        raise ValueError(f"style_mode must be one of {sorted(STYLE_MODES)!r}, got {style_mode!r}")

    violations: list[StyleViolation] = []
    translation = translation or ""

    # ---- rule 8 (fidelity): every id/number/date survives, and nothing
    # numeric is invented. Checked in BOTH directions -- a dropped booking
    # id and a hallucinated count are the same class of harm.
    src = extract_fidelity_tokens(original)
    dst = extract_fidelity_tokens(translation, strip_list_markers=True)
    for kind in ("dates", "ids"):
        missing = [t for t in src[kind] if t not in dst[kind]]
        for token in missing:
            violations.append(
                StyleViolation(
                    rule=f"r8_{kind}_dropped",
                    severity="fidelity",
                    detail=f"the original's {kind[:-1]} {token!r} does not appear in the translation",
                )
            )
        invented = [t for t in dst[kind] if t not in src[kind]]
        for token in invented:
            violations.append(
                StyleViolation(
                    rule=f"r8_{kind}_invented",
                    severity="fidelity",
                    detail=f"the translation introduces {kind[:-1]} {token!r}, which the original does not contain",
                )
            )

    # ---- rule 8, numbers only (FT-3, fix pass): a MULTISET comparison,
    # not a set one -- see _number_counts's own docstring for why. A count
    # that DROPS (source has more of a number than the translation) is the
    # same "the original's number does not appear" violation as before; a
    # count that GROWS (translation has more than the source, including a
    # number the source never had at all, at count 0) is "invented". NOTE
    # this still cannot see a translation that keeps the same SET/multiset
    # of numbers but reassigns them to different entities ("3 gates and 5
    # pools" -> "5 gates and 3 pools") -- that is a semantic re-binding, not
    # a lexical one, and is out of scope for this tier (the judged tier in
    # :mod:`trialerror.feed_translate.gate`, when wired, is what would catch
    # it; see that module's own gaps list).
    src_numbers = _number_counts(original)
    dst_numbers = _number_counts(translation, strip_list_markers=True)
    for token in sorted(set(src_numbers) | set(dst_numbers)):
        src_n, dst_n = src_numbers.get(token, 0), dst_numbers.get(token, 0)
        if dst_n < src_n:
            violations.append(
                StyleViolation(
                    rule="r8_numbers_dropped",
                    severity="fidelity",
                    detail=(
                        f"the original's number {token!r} appears {src_n} time(s) but the translation "
                        f"only {dst_n}"
                    ),
                )
            )
        elif dst_n > src_n:
            violations.append(
                StyleViolation(
                    rule="r8_numbers_invented",
                    severity="fidelity",
                    detail=(
                        f"the translation's number {token!r} appears {dst_n} time(s) but the original "
                        f"only {src_n}"
                    ),
                )
            )

    # ---- rule 9 (fidelity): never promote a hedge to a fact.
    src_hedges = hedge_families_present(original)
    dst_hedges = hedge_families_present(translation)
    hedges_lost = sorted(src_hedges - dst_hedges)
    for family in hedges_lost:
        violations.append(
            StyleViolation(
                rule="r9_hedge",
                severity="fidelity",
                detail=(
                    f"the original hedges with the {family!r} family "
                    f"({', '.join(HEDGE_FAMILIES[family])}) and the translation carries none of it"
                ),
            )
        )

    # ---- rule 1 (register): sentence length.
    cap = MAX_SENTENCE_WORDS[style_mode]
    for sentence in split_sentences(translation):
        count = len(_WORD_RE.findall(sentence))
        if count > cap:
            violations.append(
                StyleViolation(
                    rule="r1_sentence_length",
                    severity="register",
                    detail=f"{count}-word sentence exceeds the {cap}-word {style_mode} cap: {sentence[:80]!r}",
                )
            )

    # ---- rule 4 (register): no semicolons.
    if ";" in translation:
        violations.append(
            StyleViolation(
                rule="r4_semicolon",
                severity="register",
                detail=f"{translation.count(';')} semicolon(s) -- split into separate sentences instead",
            )
        )

    # ---- rule 5 (register): no phrasal verbs where a plain verb exists.
    for phrase in _find_phrases(translation, BANNED_PHRASAL_VERBS):
        violations.append(
            StyleViolation(rule="r5_phrasal_verb", severity="register", detail=f"phrasal verb {phrase!r}")
        )

    # ---- rule 12 (register): no inflated-importance / sales language.
    for word in _find_phrases(translation, BANNED_MARKETING_WORDS):
        violations.append(
            StyleViolation(rule="r12_marketing", severity="register", detail=f"inflated/sales word {word!r}")
        )

    # ---- rule 13 (register): no chatbot filler.
    for phrase in _find_phrases(translation, BANNED_FILLER_PHRASES):
        violations.append(
            StyleViolation(rule="r13_filler", severity="register", detail=f"chatbot filler {phrase!r}")
        )

    # ---- rule 14 (register): em/en dashes only at the original's own rate.
    src_dashes = len(_DASH_RE.findall(original or ""))
    dst_dashes = len(_DASH_RE.findall(translation))
    if dst_dashes > src_dashes:
        violations.append(
            StyleViolation(
                rule="r14_dashes",
                severity="register",
                detail=f"{dst_dashes} em/en dash(es) vs the original's {src_dashes} -- split into two sentences instead",
            )
        )

    return StyleReport(violations=violations, hedges_lost=hedges_lost, style_mode=style_mode)
