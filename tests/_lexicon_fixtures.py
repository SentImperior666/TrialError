"""A synthetic register corpus for the lexicon backfill tests.

Sixty ``knowledge.record`` rows across four register keys, standing in for
the shape a real register import has: one row per named thing, a payload
carrying ``name`` / ``description`` / ``f_tags`` / ``cites_raw``, and a
``register_key`` that is the register's own id rather than a
``source.source_id`` (there is no ingested document behind these rows yet
-- that gap is exactly what ``trialerror term relink`` closes later).

**Entirely invented, and deliberately from a domain nothing here is about.**
The names below are instrument-handbook vocabulary written for this file;
they exercise the import's mechanics (a lemma, a paraphrase, tag codes, a
verbatim citation string, two registers using one name for two different
quantities) without carrying content from any real corpus.

The one property the tests actually depend on: **two lemmas appear twice,
each time under a different register key**, so each of them ends up with two
current senses standing on disjoint sources -- the precise definition of a
conflict (design §3). Everything else appears once. So a clean import of
this fixture yields 58 terms, 60 senses, 60 evidence rows and exactly two
pending ``conflicts_with`` items, and a second run of it yields nothing at
all.

**Two more generators live at the bottom of this file** (build step 1c),
both of them for cases the 60-row corpus above cannot express and the first
real import made unmissable:

* :func:`name_corpus` -- 216 names in the shape that floods a duplicate
  scan: a handful of category words carried by dozens of names each, plus a
  specific word shared by three. The 60-row fixture has almost no name
  overlap at all, so it makes any threshold look calibrated.
  :func:`name_corpus_glosses` (build step 1d) adds the second half of that
  shape: register prose that mentions other names' specifics in passing,
  which is what made the 1c gate leak at corpus scale.
* :func:`oversized_gloss_records` -- two register rows whose imported gloss
  is longer than a hand-written gloss is allowed to be, one on each side of
  the import route's own cap.
"""

from __future__ import annotations

import json
from typing import Any

from trialerror.lexicon.normalize import trigrams
from trialerror.stores import insert
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

__all__ = [
    "REGISTER_KEYS",
    "SHARED_LEMMAS",
    "LEMMAS",
    "RECORD_COUNT",
    "TAGS",
    "FAMILY_MAP",
    "synthetic_records",
    "install_records",
    "CATEGORY_WORDS",
    "SPECIFIC_COUNT",
    "NAMES_PER_SPECIFIC",
    "NAME_CORPUS_SIZE",
    "specific_words",
    "name_corpus",
    "GLOSS_MENTION_STRIDES",
    "GLOSS_QUOTES_WHOLE_NAME",
    "name_corpus_glosses",
    "install_name_corpus",
    "OVERSIZE_REGISTER_KEY",
    "OVERSIZE_GLOSS_WORDS",
    "oversized_gloss_records",
    "install_oversized_gloss_records",
]

#: Four registers, because the conflict rule counts *source* identity and a
#: single-register fixture could never produce a disjoint pair.
REGISTER_KEYS: tuple[str, ...] = (
    "handbook-alpha",
    "handbook-beta",
    "handbook-gamma",
    "handbook-delta",
)

#: The tag vocabulary the rows carry, cycled over the lemma list. Instance
#: terms keep these codes whether or not a family map is ever supplied
#: (ruling L-E3), which is why the import can run long before anyone
#: decides what the level-1 families are called.
TAGS: tuple[str, ...] = ("f-stability", "f-timing", "f-signal", "f-handling")

#: A level-1 map of the same shape ``backfill_records(family_map=...)``
#: takes on a real program -- there, hand-extracted by the orchestrator from
#: a document that never enters this repo; here, four lines of invention.
FAMILY_MAP: dict[str, dict[str, str]] = {
    "f-stability": {
        "lemma": "stability behaviours",
        "gloss": "readings that describe how far an instrument wanders while nothing is changing",
    },
    "f-timing": {
        "lemma": "timing behaviours",
        "gloss": "readings that describe how long an instrument takes to reach or leave a state",
    },
}

#: 58 distinct names; the first two of them are the ones that recur under a
#: second register further down.
LEMMAS: tuple[str, ...] = (
    "baseline drift",
    "settling time",
    "warm-up interval",
    "null reading",
    "span calibration",
    "zero offset",
    "sample interval",
    "dwell period",
    "gain stage",
    "noise floor",
    "clipping threshold",
    "recovery window",
    "duty ratio",
    "idle current",
    "peak deviation",
    "ramp rate",
    "hold time",
    "release curve",
    "stability margin",
    "reference cell",
    "ambient correction",
    "humidity factor",
    "probe seating",
    "contact resistance",
    "lead compensation",
    "bridge balance",
    "excitation level",
    "shield continuity",
    "guard ring",
    "leakage path",
    "self-heating",
    "thermal lag",
    "cold junction",
    "span error",
    "linearity band",
    "hysteresis loop",
    "repeatability spread",
    "resolution step",
    "quantisation edge",
    "rounding bias",
    "averaging window",
    "outlier trim",
    "median filter",
    "decimation factor",
    "aliasing guard",
    "anti-alias filter",
    "sample hold",
    "trigger level",
    "arming delay",
    "pre-trigger buffer",
    "sweep span",
    "marker offset",
    "trace persistence",
    "envelope detector",
    "log sweep",
    "burst count",
    "gate width",
    "blanking interval",
)

#: The two names that recur, and the register each recurrence lands in.
#: Chosen so the second reading is under a register the first one is not,
#: which is what makes the two source sets disjoint.
SHARED_LEMMAS: tuple[tuple[str, str], ...] = (
    ("baseline drift", "handbook-gamma"),
    ("settling time", "handbook-delta"),
)

#: 58 + 2.
RECORD_COUNT = len(LEMMAS) + len(SHARED_LEMMAS)


def _first_reading(lemma: str, register_key: str) -> str:
    return (
        f"what {register_key} records as {lemma}: the value its procedure reports for that "
        "name, in the units the procedure states"
    )


def _second_reading(lemma: str, register_key: str) -> str:
    return (
        f"what {register_key} records as {lemma}: a different quantity under the same name, "
        "measured between two consecutive passes rather than within one"
    )


def synthetic_records() -> list[dict[str, Any]]:
    """The 60 ``record`` rows, as insertable dicts, in a stable order.

    Deterministic end to end -- ids, register assignment, sequence numbers
    and text are all functions of the lists above -- so a test can assert
    exact counts and a re-run can assert exact zeros.
    """
    rows: list[dict[str, Any]] = []
    seq_by_register: dict[str, int] = {key: 0 for key in REGISTER_KEYS}
    stamp = now()

    def _row(lemma: str, register_key: str, description: str, index: int) -> dict[str, Any]:
        seq_by_register[register_key] += 1
        return {
            "record_id": f"REC-SYN-{index:04d}",
            "register_key": register_key,
            "artifact_id": None,
            "seq": seq_by_register[register_key],
            "payload": json.dumps(
                {
                    "row_id": f"REC-SYN-{index:04d}",
                    "name": lemma,
                    "description": description,
                    "f_tags": [TAGS[index % len(TAGS)]],
                    "register": register_key,
                    "cites_raw": f"[{register_key} p{100 + index}]",
                },
                ensure_ascii=False,
            ),
            "anchors": None,
            "created_ts": stamp,
        }

    for index, lemma in enumerate(LEMMAS):
        register_key = REGISTER_KEYS[index % len(REGISTER_KEYS)]
        rows.append(_row(lemma, register_key, _first_reading(lemma, register_key), index + 1))

    for offset, (lemma, register_key) in enumerate(SHARED_LEMMAS):
        index = len(LEMMAS) + offset + 1
        rows.append(_row(lemma, register_key, _second_reading(lemma, register_key), index))

    return rows


def install_records(store) -> dict[str, Any]:
    """Insert the fixture into ``knowledge.record`` and report what it is.

    Returns the counts a backfill test asserts against, so the expected
    numbers are derived from the fixture rather than restated as literals in
    every test that uses it -- a fixture and an expectation that can drift
    apart is a test that stops testing.
    """
    rows = synthetic_records()
    for row in rows:
        insert(store, "record", row)
    return {
        "record_ids": [r["record_id"] for r in rows],
        "records": len(rows),
        "distinct_lemmas": len(LEMMAS),
        "shared_lemmas": [lemma for lemma, _ in SHARED_LEMMAS],
        "register_keys": list(REGISTER_KEYS),
    }


# ---------------------------------------------------------------------------
# the calibration corpus (build step 1c) -- a name distribution that floods
# ---------------------------------------------------------------------------
#
# The 60-row fixture above is a good register import and a useless
# calibration target: 58 names, almost all of them unlike each other, so the
# duplicate scan looks well-behaved on it whatever its thresholds are. The
# first real import was 6,975 names, and the shape that broke the scan was
# not size -- it was that most names are `<something specific> <a word from a
# small pool of category words>`, so thousands of pairs share the category
# word and nothing else, and a trigram index finds every one of them.
#
# This generator reproduces that shape at a size a test can run: eight
# category words, each carried by dozens of names, and a specific word that
# three names share. Nothing here is drawn from any corpus -- the specifics
# are letters from a fixed pseudo-random sequence and the categories are the
# most generic English nouns available.

#: The small pool of generic words the names end in. Eight, because the
#: failure is about a FEW words being carried by MANY names -- that ratio is
#: the fixture's whole content.
CATEGORY_WORDS: tuple[str, ...] = (
    "procedure",
    "table",
    "index",
    "profile",
    "interval",
    "factor",
    "stage",
    "record",
)

#: How many distinct specifics, and how many names each appears in. 72 x 3 =
#: 216 names: a few hundred, as the calibration decision asks, and enough
#: that a category word lands on 27 of them (nowhere near rare) while a
#: specific lands on 3 (rare under any threshold this store would set).
SPECIFIC_COUNT = 72
NAMES_PER_SPECIFIC = 3
NAME_CORPUS_SIZE = SPECIFIC_COUNT * NAMES_PER_SPECIFIC

_CONSONANTS = "bcdfgklmnprstvz"
_VOWELS = "aeiou"

#: Seeds the specific-word sequence. Fixed, so the corpus is byte-identical
#: on every machine and every run -- a calibration fixture whose contents
#: moved would make every number measured against it unrepeatable.
_SPECIFIC_SEED = 20260907


def specific_words(count: int = SPECIFIC_COUNT) -> tuple[str, ...]:
    """``count`` invented eight-letter words, mutually unlike by
    construction.

    Alternating consonants and vowels drawn from a fixed
    linear-congruential sequence -- pronounceable, obviously not words, and
    identical on every run. Two rules reject a candidate:

    * it may share **no trigram** with any specific already accepted, and
    * it may not end in the same **two letters** as one.

    Together those mean the only thing two names built from different
    specifics have in common is the category word and the space in front of
    it. That is what makes the calibration test's claim precise: a pair the
    gate surfaces was surfaced for a reason this fixture put there on
    purpose, never for an accidental letter overlap between two invented
    words.
    """
    state = _SPECIFIC_SEED
    out: list[str] = []
    used_trigrams: set[str] = set()
    used_tails: set[str] = set()
    while len(out) < count:
        letters: list[str] = []
        for position in range(8):
            state = (state * 1103515245 + 12345) % (2**31)
            pool = _CONSONANTS if position % 2 == 0 else _VOWELS
            letters.append(pool[state % len(pool)])
        candidate = "".join(letters)
        grams = trigrams(candidate)
        if (grams & used_trigrams) or candidate[-2:] in used_tails:
            continue
        out.append(candidate)
        used_trigrams |= grams
        used_tails.add(candidate[-2:])
    return tuple(out)


def name_corpus() -> list[tuple[str, str]]:
    """``[(lemma, specific), ...]`` -- the whole synthetic distribution, in a
    stable order.

    Each specific gets :data:`NAMES_PER_SPECIFIC` names under consecutive
    category words, so every specific is shared by exactly that many names
    and every category word by ``NAME_CORPUS_SIZE // len(CATEGORY_WORDS)``.
    The specific is handed back alongside the lemma so a test can say which
    pairs are the intended ones without re-parsing the name.
    """
    out: list[tuple[str, str]] = []
    for index, specific in enumerate(specific_words()):
        for offset in range(NAMES_PER_SPECIFIC):
            category = CATEGORY_WORDS[(index + offset) % len(CATEGORY_WORDS)]
            out.append((f"{specific} {category}", specific))
    return out


#: How far along the name list a gloss reaches for the specifics it
#: mentions. Two strides, both coprime with :data:`NAME_CORPUS_SIZE`, so
#: every name's gloss mentions two OTHER specifics and every specific is
#: mentioned by the same number of glosses -- a uniform leak rather than a
#: hotspot.
#:
#: This is the shape of a real register gloss, and reproducing it is what
#: build step 1d needed the fixture to do. A register defines a thing by
#: naming the neighbours it is measured against, so a gloss is twenty to a
#: hundred and sixty words that mention several other named things in
#: passing. A gate that counts "one word of this name appears somewhere in
#: that paragraph" as a shared word therefore fires on nearly every pair --
#: which is how the live rescan kept 25,651 of 33,870 candidates.
GLOSS_MENTION_STRIDES: tuple[int, int] = (7, 13)

#: The one gloss in the corpus that quotes another term's **whole name**
#: rather than a word of it: ``(quoting name index, quoted name index)``.
#: The case the ``name_in_text`` route exists for, and the recall property
#: the 1d rescoping had to keep -- a term whose gloss contains another
#: term's name stays reachable from that other term.
GLOSS_QUOTES_WHOLE_NAME: tuple[int, int] = (0, 3)


def name_corpus_glosses() -> list[str]:
    """One gloss per name of :func:`name_corpus`, in the same order.

    Written in a single register voice -- as a real register's rows are, and
    as the 60-row fixture above already imitates -- so what the glosses have
    in common is function words, and what distinguishes them is the
    specifics they mention. Every word between the mentions is a function
    word or a verb and **never a category word**, so the only whole NAME any
    gloss contains is the one :data:`GLOSS_QUOTES_WHOLE_NAME` puts there on
    purpose. Without that discipline the two things this fixture has to keep
    apart -- "a word of a name is in the prose" and "a name is in the prose"
    -- would blur together and neither measurement would mean anything.
    """
    rows = name_corpus()
    names = [lemma for lemma, _ in rows]
    specifics = [specific for _, specific in rows]
    out: list[str] = []
    for index in range(len(rows)):
        first = specifics[(index + GLOSS_MENTION_STRIDES[0]) % len(rows)]
        second = specifics[(index + GLOSS_MENTION_STRIDES[1]) % len(rows)]
        gloss = (
            f"the reading this register gives, measured against {first} and reported "
            f"with {second} alongside it"
        )
        if index == GLOSS_QUOTES_WHOLE_NAME[0]:
            gloss += f", exactly as {names[GLOSS_QUOTES_WHOLE_NAME[1]]} is"
        out.append(gloss)
    return out


def install_name_corpus(store, *, by_launch: str, glosses: bool = False) -> dict[str, Any]:
    """Insert the corpus as bare ``term`` rows and build ``term_fts`` over
    them.

    Rows go in directly rather than through
    :func:`trialerror.lexicon.api.propose` because the subject here is the
    SCAN, not the proposal route: 216 proposals would add 216 senses, 216
    evidence rows, 216 save-time scans and their events to a test that is
    about which pairs a gate surfaces. ``reindex_all`` afterwards is the
    API-maintained index being maintained -- without it the trigram half
    would see none of these terms (``trialerror.lexicon.candidates``, "index
    visibility").

    ``glosses=True`` adds one ``current`` ``term_sense`` per term carrying
    :func:`name_corpus_glosses` (build step 1d). **Off by default, and that
    is a measurement decision rather than a convenience**: the numbers the
    1c calibration is stated in were measured over names alone, and a
    fixture that quietly changed underneath them would make the whole table
    unreadable. The gloss corpus is a second measurement of the same name
    distribution with the register's prose added, and the tests that use it
    say so.

    Those senses carry no evidence rows, which is a state the write API
    would refuse -- the same liberty, for the same reason, that the bare
    ``term`` rows above already take: the subject is the gate, and the
    grounding law has its own tests and its own doctor check.
    """
    from trialerror.lexicon import api

    stamp = now()
    rows = name_corpus()
    gloss_text = name_corpus_glosses() if glosses else None
    term_ids: dict[str, str] = {}
    for index, (lemma, _specific) in enumerate(rows):
        term_id = new_id("TERM")
        insert(
            store,
            "term",
            {
                "term_id": term_id,
                "lemma": lemma,
                "lemma_norm": lemma,
                "granularity": "instance",
                "tags": None,
                "entity_id": None,
                "status": "active",
                "preferred_sense_id": None,
                "merged_into": None,
                "created_by_launch": by_launch,
                "created_at": stamp,
                "updated_ts": stamp,
            },
        )
        term_ids[lemma] = term_id
        if gloss_text is not None:
            insert(
                store,
                "term_sense",
                {
                    "sense_id": new_id("SENSE"),
                    "term_id": term_id,
                    "gloss": gloss_text[index],
                    "origin_kind": "record_import",
                    "procedure_version": "register-import-v1",
                    "status": "current",
                    "created_at": stamp,
                    "proposed_by_launch": by_launch,
                },
            )
    indexed = api.reindex_all(store)
    return {
        "term_ids": term_ids,
        "names": [lemma for lemma, _ in rows],
        "specific_of": {lemma: specific for lemma, specific in rows},
        "glosses": {lemma: gloss_text[i] for i, (lemma, _) in enumerate(rows)} if gloss_text else {},
        "terms": len(rows),
        "indexed": indexed,
    }


# ---------------------------------------------------------------------------
# two register rows whose glosses are longer than a gloss is meant to be
# ---------------------------------------------------------------------------

#: Which register the oversize rows land in -- one of the four above, so a
#: backfill scoped to :data:`REGISTER_KEYS` picks them up without the test
#: having to widen its scope.
OVERSIZE_REGISTER_KEY = REGISTER_KEYS[0]

#: The two lengths, and why each is here. 100 words is the shape the real
#: import actually refused (the measured band was 82-93): a long but genuine
#: register reading, over the hand-written cap and well under the import one.
#: 200 words is the case the import cap still has to refuse -- a payload that
#: is not a gloss at all.
OVERSIZE_GLOSS_WORDS: tuple[int, int] = (100, 200)


def oversized_gloss_records() -> list[dict[str, Any]]:
    """Two extra ``record`` rows, deliberately NOT part of
    :func:`synthetic_records`.

    Kept separate so the 60-row fixture's exact counts -- 58 terms, 60
    senses, two conflicts -- stay exactly what every other backfill test
    asserts. A test that wants the cap cases installs both.
    """
    stamp = now()
    rows: list[dict[str, Any]] = []
    for offset, words in enumerate(OVERSIZE_GLOSS_WORDS):
        index = 900 + offset
        lemma = f"oversize reading {offset + 1}"
        rows.append(
            {
                "record_id": f"REC-SYN-{index:04d}",
                "register_key": OVERSIZE_REGISTER_KEY,
                "artifact_id": None,
                "seq": index,
                "payload": json.dumps(
                    {
                        "row_id": f"REC-SYN-{index:04d}",
                        "name": lemma,
                        "description": " ".join(["word"] * words),
                        "f_tags": [TAGS[offset % len(TAGS)]],
                        "register": OVERSIZE_REGISTER_KEY,
                        "cites_raw": f"[{OVERSIZE_REGISTER_KEY} p{index}]",
                    },
                    ensure_ascii=False,
                ),
                "anchors": None,
                "created_ts": stamp,
            }
        )
    return rows


def install_oversized_gloss_records(store) -> dict[str, Any]:
    """Insert the two rows above and report their ids and word counts."""
    rows = oversized_gloss_records()
    for row in rows:
        insert(store, "record", row)
    return {
        "record_ids": [r["record_id"] for r in rows],
        "word_counts": list(OVERSIZE_GLOSS_WORDS),
        "register_key": OVERSIZE_REGISTER_KEY,
    }
