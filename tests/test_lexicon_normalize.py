"""``trialerror.lexicon.normalize`` -- the one function that decides whether
two written forms are the same key.

The tests split into the two halves the rule has, and the second half is
the one that matters more. **What it folds** is ordinary Unicode hygiene
(case, compatibility forms, dashes, whitespace) and is easy to check.
**What it refuses to fold** is a design commitment: no stemming, ever, so
that "the store thinks these are one term" is always a decision someone
made and recorded in ``term_alias``, never a side effect of a tokenizer.
Those are written as their own tests, because the failure mode they guard
is silent and irreversible -- once two readings are filed under one term,
nothing downstream can tell they were ever different.
"""

from __future__ import annotations

import pytest

from trialerror.lexicon import normalize
from trialerror.lexicon.normalize import fts_text, norm_lemma, word_count


# ---------------------------------------------------------------------------
# what it folds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "written,expected,why",
    [
        ("Initiative Order", "initiative order", "case"),
        ("INITIATIVE", "initiative", "shouting"),
        ("  initiative   order  ", "initiative order", "leading/trailing/inner whitespace runs"),
        ("initiative\torder", "initiative order", "a tab is whitespace"),
        ("initiative\norder", "initiative order", "so is a newline"),
        ("ﬁxed cost", "fixed cost", "NFKC decomposes the fi ligature"),
        ("ＦＵＬＬＷＩＤＴＨ", "fullwidth", "NFKC folds fullwidth Latin"),
        ("re­roll", "reroll", "a soft hyphen is invisible, so it cannot be part of a key"),
        ("save throw", "save throw", "NFKC turns NBSP into a space, which then collapses"),
        ("MASSE", "masse", "casefold of a capital sharp-s spelling"),
        ("straße", "strasse", "casefold expands sharp s, and it is length-changing"),
    ],
)
def test_forms_that_are_the_same_string_written_differently_fold_together(written, expected, why):
    assert norm_lemma(written) == expected, why


@pytest.mark.parametrize(
    "dash",
    ["-", "‐", "‑", "‒", "–", "—", "―", "−", "﹘", "﹣", "－"],
)
def test_every_dash_a_typesetter_might_use_keys_the_same(dash):
    """Unicode does NOT do this for us: NFKC leaves U+2013 EN DASH and
    friends distinct from ASCII ``-``, so a lemma copied out of a typeset
    document would otherwise key differently from the same lemma typed at a
    keyboard."""
    assert norm_lemma(f"re{dash}roll") == "re-roll"


#: The regression set for fix-pass finding F4. Each pair is one name written
#: two ways -- once as a typeset document (or a PDF/HTML extractor) emits it,
#: once as somebody types it -- and every pair MUST key the same. Before the
#: fix, twelve of these split into two terms that no scan could bridge and no
#: reader could tell apart on screen, because the characters that separated
#: them render as nothing.
INVISIBLE_AND_TYPESET_PAIRS = [
    ("stress\u00adtrack", "stresstrack", "SOFT HYPHEN U+00AD -- what a PDF line break leaves behind"),
    ("stress\u200btrack", "stresstrack", "ZERO WIDTH SPACE U+200B"),
    ("stress\u200ctrack", "stresstrack", "ZERO WIDTH NON-JOINER U+200C"),
    ("stress\u200dtrack", "stresstrack", "ZERO WIDTH JOINER U+200D"),
    ("\ufeffstress track", "stress track", "BOM / ZWNBSP U+FEFF at the head of an extracted string"),
    ("stress\u2060track", "stresstrack", "WORD JOINER U+2060"),
    ("player\u2019s turn", "player's turn", "RIGHT SINGLE QUOTATION MARK -- every typeset possessive"),
    ("player\u02bcs turn", "player's turn", "MODIFIER LETTER APOSTROPHE U+02BC"),
    ("\u2018stress\u2019 track", "'stress' track", "curly single quotes"),
    ("\u201cstress\u201d track", '"stress" track', "curly double quotes"),
    ("initiative\u00a0order", "initiative order", "NBSP (already folded before the fix; pinned so it stays)"),
    ("re\u2013roll", "re-roll", "EN DASH (already folded before the fix; pinned so it stays)"),
]


@pytest.mark.parametrize("typeset,typed,why", INVISIBLE_AND_TYPESET_PAIRS)
def test_a_typeset_spelling_keys_the_same_as_the_typed_one(typeset, typed, why):
    """Finding F4's whole population. The consequence of a split here is not
    a near-miss: two terms are created for one name, ``find_term`` bridges
    neither, and the duplicate scan opens nothing -- an exact key cannot see
    an invisible character and neither can a trigram -- so the split is
    permanent, unqueued, and invisible on screen."""
    assert norm_lemma(typeset) == norm_lemma(typed), why


@pytest.mark.parametrize("typeset,typed,why", INVISIBLE_AND_TYPESET_PAIRS)
def test_every_pair_is_still_idempotent_after_folding(typeset, typed, why):
    for written in (typeset, typed):
        once = norm_lemma(written)
        assert norm_lemma(once) == once, why


@pytest.mark.parametrize(
    "invisible",
    ["\u200b", "\u200c\u200d", "\ufeff", "\u00ad", "\u2060\u200b \u00ad"],
)
def test_a_lemma_made_only_of_format_characters_keys_as_empty(invisible):
    """The rider on F4: ``norm_lemma`` used to return a truthy string of
    invisible characters, so ``propose``'s ``if not lemma_norm`` guard passed
    and a term whose every rendered cell is blank could be created through the
    public API. Emptiness is now decided on the post-strip key."""
    assert norm_lemma(invisible) == ""


def test_the_output_is_not_promised_to_be_nfkc_normal_only_idempotent():
    """Casefold can undo NFKC, so the second rider on F4 is a docstring
    correction rather than a code change: what callers rely on is idempotence
    and that both sides of a comparison went through this same function."""
    import unicodedata

    key = norm_lemma("\u0390")
    assert unicodedata.normalize("NFKC", key) != key
    assert norm_lemma(key) == key


def test_normalization_is_idempotent():
    for written in ("Initiative  Order", "ＦＵＬＬ–WIDTH", "  straße  ", "ﬁxed"):
        once = norm_lemma(written)
        assert norm_lemma(once) == once


def test_an_empty_or_blank_lemma_normalizes_rather_than_raising():
    """Totality is deliberate. "Is this lemma empty" is the caller's
    validation to make and report with its own named error
    (``InvalidTermInputError``); a normalizer that raised would make every
    call site wrap it."""
    assert norm_lemma("") == ""
    assert norm_lemma("   \t\n ") == ""
    assert norm_lemma(None) == ""  # type: ignore[arg-type]


def test_non_string_input_is_coerced_rather_than_crashing():
    assert norm_lemma(12) == "12"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# what it refuses to fold -- the design commitment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "a,b,why",
    [
        ("save", "saves", "a plural is an ALIAS decision, not a normalization"),
        ("die", "dice", "irregular plurals are exactly what a stemmer gets wrong"),
        ("initiative", "initiatives", "porter would collapse these; the lexicon must not"),
        ("colour", "color", "spelling variants are aliases, recorded by whoever decided so"),
        ("re-roll", "reroll", "hyphenation is a spelling, and spellings are aliases"),
        ("order", "ordering", "derivational morphology is a different word"),
    ],
)
def test_different_words_stay_different_keys(a, b, why):
    assert norm_lemma(a) != norm_lemma(b), why


def test_the_stemming_line_is_where_chunk_retrieval_and_the_lexicon_differ():
    """``chunk_fts`` is porter-stemmed because recall over prose is worth a
    little precision. A lexicon key is the identity of a named thing, and a
    stemmer collapsing two named things into one identity is not a recall
    win -- it is a wrong answer no later step can undo."""
    assert norm_lemma("saving") != norm_lemma("save")
    assert norm_lemma("running") != norm_lemma("run")


# ---------------------------------------------------------------------------
# word_count -- what the gloss cap is measured in
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        (None, 0),
        ("", 0),
        ("   ", 0),
        ("one", 1),
        ("one two three", 3),
        ("one   two\tthree\nfour", 4),
        ("a hyphenated-word counts once", 4),
    ],
)
def test_word_count_agrees_with_counting_words_on_screen(text, expected):
    assert word_count(text) == expected


# ---------------------------------------------------------------------------
# fts_text
# ---------------------------------------------------------------------------


def test_fts_text_joins_and_normalizes_so_index_and_query_agree():
    assert fts_text("Initiative Order", "the sequence in which actors act") == (
        "initiative order the sequence in which actors act"
    )


def test_fts_text_drops_empty_parts_rather_than_leaving_gaps():
    assert fts_text("Save", None, "", "  ", "a roll to avoid an effect") == (
        "save a roll to avoid an effect"
    )


def test_fts_text_normalizes_dashes_so_a_typed_query_hits_a_typeset_lemma():
    assert fts_text("re–roll") == fts_text("re-roll") == "re-roll"


# ---------------------------------------------------------------------------
# the two views of a name the duplicate gate measures (build step 1c)
# ---------------------------------------------------------------------------


class TestNameTokens:
    """``name_tokens`` is the unit the informative-token gate is defined
    over, and its whole contract is that it is ``norm_lemma`` split on
    spaces -- no second tokenizer, so the gate and the key function can
    never disagree about what a word is."""

    def test_it_is_the_normalized_key_split_on_spaces(self):
        assert normalize.name_tokens("  Settling   TIME ") == ("settling", "time")
        assert normalize.name_tokens("re–roll") == ("re-roll",)

    def test_a_repeated_word_counts_once(self):
        """Document frequency counts names, not occurrences: a name that
        says a word twice is one name carrying it."""
        assert normalize.name_tokens("time after time") == ("time", "after")

    def test_it_is_total(self):
        assert normalize.name_tokens("") == ()
        assert normalize.name_tokens(None) == ()
        assert normalize.name_tokens("   ") == ()


class TestTrigramSimilarity:
    """The number every system-opened candidate now records as its
    confidence (decision D1)."""

    def test_the_windows_are_the_ones_the_index_tokenizes(self):
        assert normalize.trigrams("abcd") == {"abc", "bcd"}
        assert normalize.trigrams("a b") == {"a b"}

    def test_a_name_too_short_to_have_a_window_is_its_own(self):
        """Two one-letter names are either the same name or not; an empty
        set for both would make them 0.0 alike either way."""
        assert normalize.trigrams("ab") == {"ab"}
        assert normalize.trigram_similarity("ab", "ab") == 1.0
        assert normalize.trigram_similarity("ab", "cd") == 0.0

    def test_identical_names_are_one_and_disjoint_ones_zero(self):
        assert normalize.trigram_similarity("settling time", "settling time") == 1.0
        assert normalize.trigram_similarity("settling time", "kzqw") == 0.0

    def test_it_is_symmetric_and_normalized_first(self):
        """A candidate is a pair; which side was proposed second is an
        accident, so the score cannot depend on it. And both sides go
        through the same fold as every lookup key."""
        left, right = "Settling  Time", "settling time"
        assert normalize.trigram_similarity(left, right) == 1.0
        assert normalize.trigram_similarity(right, left) == 1.0

    def test_it_is_bounded_rounded_and_deterministic(self):
        value = normalize.trigram_similarity("settling time", "settling period")
        assert value == 0.4118
        assert value == normalize.trigram_similarity("settling time", "settling period")
        assert 0.0 <= value <= 1.0
        assert round(value, 4) == value, "the stored number and the compared number are one value"

    def test_an_empty_name_is_alike_to_nothing(self):
        assert normalize.trigram_similarity("", "settling time") == 0.0
        assert normalize.trigram_similarity(None, None) == 0.0
