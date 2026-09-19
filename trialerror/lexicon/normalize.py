"""Lemma normalization -- the one function that decides whether two written
forms are the same key (design §2, ``term.lemma_norm``).

The rule, and what it deliberately does NOT do:

    NFKC -> dash-unify -> casefold -> whitespace-collapse.  **No stemming.**

Every step above folds together forms that are the same string written
differently -- a full-width character, an en dash typed where a hyphen was
meant, a double space, a capital at the start of a sentence. None of them
folds together forms that are different *words*. That is the line, and it
is drawn on purpose: plurals, spellings and abbreviations are **aliases**,
recorded in ``term_alias`` where a human decided they belong together, not
silently merged by a stemmer that cannot tell "dice" from "die" or
"saves" from "save" without knowing the term.

Chunk retrieval makes the opposite trade (``chunk_fts`` is porter-stemmed)
because recall over prose is worth a little precision. A lexicon key is
the identity of a named thing; a stemmer collapsing two named things into
one identity is not a recall win, it is a wrong answer that no later step
can undo -- the two senses have already been filed under one term.

Unicode notes worth stating, since every one of them is easy to get subtly
wrong:

* **NFKC before casefold, casefold before whitespace-collapse.** NFKC can
  introduce spaces (U+00A0 and the other compatibility spaces normalize to
  U+0020), and casefold can change length (``ß`` -> ``ss``); collapsing
  first would leave the spaces NFKC produces uncollapsed.
* **Dash unification is not something Unicode does for us.** NFKC leaves
  U+2010 HYPHEN, U+2013 EN DASH, U+2212 MINUS SIGN and friends distinct
  from ASCII ``-``, so a lemma copied out of a typeset document would key
  differently from the same lemma typed at a keyboard. The
  :data:`_DASHES` table is the explicit fix.
* **Neither is format-character removal, and that one was worse.** NFKC
  leaves every ``Cf`` format character in place and Python's whitespace
  class does not match the zero-width ones, so a SOFT HYPHEN (U+00AD), a
  ZERO WIDTH SPACE
  (U+200B), a ZWNJ/ZWJ (U+200C/D) or a BOM (U+FEFF) used to survive into
  ``lemma_norm`` and split one name into two terms. Those characters are
  exactly what PDF and HTML extraction emit -- the routes this store's
  evidence comes from -- and because they render as nothing, the two rows
  looked identical on screen while neither half of the duplicate scan could
  bridge them (an exact key cannot, and a trigram over an invisible
  character cannot either). Stripping category ``Cf`` between the NFKC and
  the dash step is the fix; U+00AD is already ``Cf``, so one rule covers
  all of them. A lemma made only of format characters therefore normalizes
  to ``""`` and is refused by the caller's own emptiness check, exactly like
  an empty one.
* **Typeset quotes fold to their typed spelling.** A possessive copied out
  of a typeset page carries U+2019 (or U+02BC, or a curly double quote);
  the :data:`_QUOTES` table maps that block onto ASCII ``'`` and ``"`` on
  precisely the argument :data:`_DASHES` is written on.
* **The output is not guaranteed to be NFKC-normal**, because casefold can
  undo NFKC (``ΐ`` is the standing example). This is harmless -- every
  lookup, every index write and every query goes through this function, so
  both sides of any comparison have had the same thing done to them -- but
  it is why the ordering argument above is about what gets *folded*, not
  about the output landing in a named normal form. ``norm_lemma`` is still
  idempotent, which is the property callers actually rely on.

One caller needs the same folding *and* the offsets it folded away.
:func:`norm_with_offsets` is that caller's function (design §5's
``match_terms_in_text``, which reports spans into the text a reader sees,
not into a normalized copy of it). It runs the identical pipeline and is
contracted to return exactly ``norm_lemma``'s string, so the two can never
disagree about what matches -- the difference is only that it also hands
back, per normalized character, the slice of the ORIGINAL text that
produced it. That is why the pipeline runs per combining-cluster there
rather than over the whole string: see the function's own docstring for why
those two are the same fold.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = [
    "norm_lemma",
    "norm_with_offsets",
    "word_count",
    "fts_text",
    "name_tokens",
    "trigrams",
    "trigram_similarity",
]

#: Every code point this module treats as an ASCII hyphen. Compatibility
#: forms (U+FE58/U+FE63/U+FF0D) are listed even though NFKC already folds
#: them, so the table stays correct if the NFKC step is ever reordered.
_DASHES = {
    "‐": "-",  # HYPHEN
    "‑": "-",  # NON-BREAKING HYPHEN
    "‒": "-",  # FIGURE DASH
    "–": "-",  # EN DASH
    "—": "-",  # EM DASH
    "―": "-",  # HORIZONTAL BAR
    "−": "-",  # MINUS SIGN
    "﹘": "-",  # SMALL EM DASH
    "﹣": "-",  # SMALL HYPHEN-MINUS
    "－": "-",  # FULLWIDTH HYPHEN-MINUS
}
_DASH_TABLE = str.maketrans(_DASHES)

#: Every code point this module treats as an ASCII apostrophe or double
#: quote. Same reasoning as :data:`_DASHES`, and the same evidence for it:
#: a typeset possessive carries U+2019 while the same lemma typed at a
#: keyboard carries U+0027, and nothing in NFKC or casefold brings the two
#: together. U+02BC MODIFIER LETTER APOSTROPHE is included because some
#: extraction pipelines emit it for the same glyph.
_QUOTES = {
    "‘": "'",  # LEFT SINGLE QUOTATION MARK
    "’": "'",  # RIGHT SINGLE QUOTATION MARK (the typeset possessive)
    "‚": "'",  # SINGLE LOW-9 QUOTATION MARK
    "‛": "'",  # SINGLE HIGH-REVERSED-9 QUOTATION MARK
    "ʼ": "'",  # MODIFIER LETTER APOSTROPHE
    "“": '"',  # LEFT DOUBLE QUOTATION MARK
    "”": '"',  # RIGHT DOUBLE QUOTATION MARK
    "„": '"',  # DOUBLE LOW-9 QUOTATION MARK
    "‟": '"',  # DOUBLE HIGH-REVERSED-9 QUOTATION MARK
}
_QUOTE_TABLE = str.maketrans(_QUOTES)

_WHITESPACE_RUN = re.compile(r"\s+")


def _strip_format_characters(text: str) -> str:
    """Drop every Unicode ``Cf`` (format) code point.

    Written as a category test rather than as a table because the table
    would be a list of exactly the invisible characters someone remembered:
    U+00AD, U+200B-U+200F, U+2060-U+2064, U+FEFF and the rest all share one
    property, and it is the property that matters -- they carry no glyph, so
    two lemmas differing only in them are the same name written twice.
    """
    if text.isascii():  # the overwhelmingly common case, and Cf is non-ASCII
        return text
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def norm_lemma(text: str) -> str:
    """The normalized lookup key for ``text``.

    NFKC -> strip ``Cf`` -> dash/quote-unify -> casefold ->
    whitespace-collapse. Deterministic, idempotent
    (``norm_lemma(norm_lemma(x)) == norm_lemma(x)`` for every input), and
    total -- an empty, whitespace-only or entirely-invisible input
    normalizes to ``""`` rather than raising, because "is this lemma empty"
    is the *caller's* validation to make and report with its own named
    error, not a surprise exception from a normalizer.

    The ``Cf`` strip runs AFTER NFKC and BEFORE the dash step: NFKC can
    introduce nothing of category ``Cf``, and stripping first would leave a
    soft hyphen embedded in a sequence NFKC was about to rewrite. It runs
    before the dash step so that a soft-hyphenated ``re-roll`` and a plainly
    typed one reach the dash table looking the same.
    """
    if text is None:  # defensive: a NULL lemma is a caller bug, keyed as empty
        return ""
    folded = unicodedata.normalize("NFKC", str(text))
    folded = _strip_format_characters(folded)
    folded = folded.translate(_DASH_TABLE)
    folded = folded.translate(_QUOTE_TABLE)
    folded = folded.casefold()
    return _WHITESPACE_RUN.sub(" ", folded).strip()


#: The three Unicode categories of combining mark. A mark belongs to the
#: cluster of the character it sits on -- that grouping is what makes the
#: per-cluster fold in :func:`norm_with_offsets` identical to the
#: whole-string one.
_MARK_CATEGORIES = frozenset({"Mn", "Mc", "Me"})


def _clusters(text: str) -> list[tuple[int, int]]:
    """``text`` cut into (start, end) slices of one starter plus the
    combining marks that belong to it.

    A leading mark with no starter in front of it forms its own cluster
    rather than being dropped or attached backwards -- it is what the text
    says, and NFKC treats it the same way.
    """
    if not text:
        return []
    out: list[tuple[int, int]] = []
    start = 0
    for i in range(1, len(text)):
        ch = text[i]
        if not ch.isascii() and unicodedata.category(ch) in _MARK_CATEGORIES:
            continue  # a mark stays with the character it sits on
        out.append((start, i))
        start = i
    out.append((start, len(text)))
    return out


def norm_with_offsets(text: str) -> tuple[str, list[tuple[int, int]]]:
    """:func:`norm_lemma`'s output, plus where every character came from.

    Returns ``(normalized, spans)`` where ``normalized ==
    norm_lemma(text)`` and ``spans[k]`` is the ``(start, end)`` slice of
    the ORIGINAL ``text`` that produced ``normalized[k]``. A match found at
    ``normalized[i:j]`` therefore covers the original text from
    ``spans[i][0]`` to ``spans[j - 1][1]``.

    **Why the caller needs this at all.** Normalization is not
    length-preserving in either direction: a soft hyphen and a BOM vanish,
    ``ß`` becomes two characters, ``ﬁ`` becomes two, a run of newlines and
    tabs becomes one space, a curly apostrophe becomes a straight one that
    happens to be the same width and a full-width digit becomes an ASCII
    one that is not. Any of those between the start of a document and a
    matched term shifts every later offset, so a span computed against the
    normalized copy and reported against the original is off by an amount
    nobody can reconstruct downstream. Highlighting the wrong words is the
    visible failure; the invisible one is a glossary link that lands inside
    a quotation it changes the meaning of.

    **The mapping is per cluster, not per character**, and one cluster's
    whole slice is what every character it produces points back at. Mapping
    a sub-character offset would be a fiction -- there is no "first half"
    of ``ﬁ`` in the original -- so a match starting inside a ligature
    reports the ligature's own span. In practice this is invisible: word
    boundaries are where matches start.

    **Per-cluster folding is the same fold as norm_lemma's whole-string
    one**, which is the property that lets this function promise the
    equality above rather than approximate it. Canonical composition only
    ever binds a starter to the combining marks that follow it, and this
    function keeps exactly those together; every compatibility mapping NFKC
    applies (a ligature, a full-width form, a compatibility space) is a
    single character's own expansion and cannot reach across a cluster
    boundary. Casefolding is per character. Whitespace-collapsing is the
    one step deliberately done globally here, over the folded stream, which
    is where ``norm_lemma`` does it too. The equality is asserted directly
    in ``tests/test_lexicon_reads.py`` over the awkward cases rather than
    only argued for here.
    """
    if not text:
        return "", []
    out: list[str] = []
    spans: list[tuple[int, int]] = []
    for start, end in _clusters(text):
        piece = unicodedata.normalize("NFKC", text[start:end])
        piece = _strip_format_characters(piece)
        piece = piece.translate(_DASH_TABLE)
        piece = piece.translate(_QUOTE_TABLE)
        piece = piece.casefold()
        for ch in piece:
            if _WHITESPACE_RUN.match(ch):
                if not out or out[-1] == " ":
                    continue  # collapse the run; a leading one is stripped
                out.append(" ")
            else:
                out.append(ch)
            spans.append((start, end))
    if out and out[-1] == " ":  # the trailing strip, same as norm_lemma's
        out.pop()
        spans.pop()
    return "".join(out), spans


def word_count(text: str | None) -> int:
    """Whitespace-delimited word count -- what the gloss cap is measured in.

    Deliberately naive, and deliberately the same naive rule the summary
    tier's own word cap uses: a cap exists to stop a paraphrase turning
    into a transcription, and any counter that agrees with a human counting
    the words on screen is accurate enough for that. A tokenizer would make
    the number depend on the model of the day.
    """
    if not text:
        return 0
    return len(text.split())


def fts_text(*parts: str | None) -> str:
    """Join ``parts`` into one ``term_fts`` document, dropping empties.

    Normalized the same way lookups are, so a trigram query built from a
    user's typed lemma and the indexed text agree about dashes and case
    without either side remembering to normalize first.
    """
    normed = [norm_lemma(p) for p in parts if p]
    return " ".join(p for p in normed if p)


# ---------------------------------------------------------------------------
# the two views of a name the duplicate gate measures (build step 1c)
# ---------------------------------------------------------------------------


def name_tokens(text: str | None) -> tuple[str, ...]:
    """The word units of a name, normalized and de-duplicated, in order.

    "The normalize.py word units" the informative-token gate
    (:mod:`trialerror.lexicon.candidates`) is defined over: exactly what
    :func:`norm_lemma` produces, split on the single spaces it collapsed
    every run of whitespace into. There is no second tokenizer here on
    purpose -- a gate that counted tokens one way and an index that keyed
    them another would disagree about which names share a word, and the
    disagreement would be invisible.

    De-duplicated because a token repeated inside one name is still one
    thing that name is about: document frequency counts names, not
    occurrences.
    """
    seen: dict[str, None] = {}
    for token in norm_lemma(text or "").split():
        seen.setdefault(token, None)
    return tuple(seen)


def trigrams(text: str | None) -> frozenset[str]:
    """The character trigrams of a normalized name -- the same 3-character
    windows ``term_fts``'s ``tokenize='trigram'`` indexes, spaces included.

    A name shorter than three characters has no window, so it contributes
    itself as its single "trigram" rather than an empty set: two one-letter
    names are either the same name or not, and returning nothing for both
    would make :func:`trigram_similarity` call them 0.0 alike either way.
    """
    normed = norm_lemma(text or "")
    if not normed:
        return frozenset()
    if len(normed) < 3:
        return frozenset({normed})
    return frozenset(normed[i : i + 3] for i in range(len(normed) - 2))


def trigram_similarity(left: str | None, right: str | None) -> float:
    """Jaccard overlap of the two names' trigram sets, in ``[0, 1]``,
    rounded to four decimals.

    **Why Jaccard over trigrams and not a normalized bm25 rank** (build step
    1c, decision D1). bm25 is a ranking score, not a similarity: it is
    negative, unbounded, and computed against the rest of the index, so the
    same two names score differently in a store with 60 terms and in one
    with 7,000, and a re-scan changes a recorded number without either name
    changing. A confidence written into ``term_relation.confidence`` is read
    months later by a reviewer and by a report; it has to mean the same
    thing then as now. Jaccard over the tokenizer's own unit is:

    * **deterministic** -- a pure function of the two strings, identical on
      every machine and every re-run;
    * **bounded and comparable** -- 0.0 to 1.0, so a floor can be stated as
      a policy constant rather than tuned per corpus;
    * **symmetric** -- ``sim(a, b) == sim(b, a)``, which matters because a
      candidate is a pair and the direction it was opened from is an
      accident of who was proposed second;
    * **the same unit the index matched on** -- the reason the pair surfaced
      at all was a trigram overlap, so the number that explains it should be
      measured in trigrams.

    Rounding is part of the definition rather than a display choice: the
    stored number, the number the gate compares against its floor and the
    number a reason string quotes are one value, so a candidate can never
    read as "0.5, below the 0.5 floor".
    """
    a = trigrams(left)
    b = trigrams(right)
    if not a or not b:
        return 0.0
    return round(len(a & b) / len(a | b), 4)
