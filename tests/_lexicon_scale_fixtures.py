"""Not a test module (pytest only collects ``test_*.py``) -- a deterministic
term-store corpus at the scale the Lexicon panel actually has to serve:
5,000 terms by default, two senses each, roughly one relation each.

**Entirely invented vocabulary, from no domain at all.** The lemmas are
syllable pairs assembled from a fixed consonant/vowel table plus the row
index (``_lemma``), so every lemma is unique, obviously not a word, and
carries no content from any corpus -- the same "invented, and deliberately
from a domain nothing here is about" rule ``tests/_lexicon_fixtures.py``
states for its own register fixture.

Bulk ``executemany`` straight into ``store.knowledge`` (bypassing the
validated ``trialerror.stores.writer.insert`` per-row API) purely for
fixture-build SPEED -- the ``tests/_graph_scale_fixtures.py`` precedent for
its own 50k-entity latency fixture, applied to the five lexicon tables
instead. Two consequences worth stating rather than discovering:

* ``term_fts`` is NOT maintained here (``trialerror.lexicon.api`` maintains it
  on every real write). Nothing this fixture exists for reads it -- the
  Lexicon panel never touches the index -- and a doctor run over this
  corpus would therefore, correctly, report the parity check as failing.
* The launch/account/session triple IS written through the real ``insert``
  (three rows, negligible cost), so ``created_by_launch``/
  ``proposed_by_launch`` name a launch row that exists, as they do in life.

What the generated shape is FOR (each feature is something the panel's
index half computes, so a batched rewrite that got it wrong would show up
as a wrong number, not merely a slow one):

* ``granularity`` cycles ``family`` / ``instance`` / ``NULL`` -- the
  ``by_granularity`` rollup including its ``__null__`` bucket.
* one term in ``_RETIRED_STRIDE`` is ``retired`` and one in
  ``_PROPOSED_STRIDE`` is ``proposed`` -- the head-layer ``status`` branch of
  the derived state, which outranks both computed states.
* relations cycle, by term index mod 5: a term-scoped pending
  ``conflicts_with`` (0), a pending term-to-term ``same_as`` that names TWO
  terms and so must be grouped under both (1), a ``rejected``
  ``conflicts_with`` that must NOT count as open (2), a pending
  sense-to-sense ``conflicts_with`` -- the endpoint-is-a-sense-id path (3),
  and no relation at all (4).
* the second sense of a term in ``_STALE_STRIDE`` is ``current`` with a
  ``review_after`` in the past (everything else is 2099) -- the computed
  ``needs_review`` flag.
* every term in ``_PREFERRED_STRIDE`` sets ``preferred_sense_id`` to its
  SECOND sense, whose gloss therefore wins over the first current sense's.
* one extra RETRACTED evidence row per sense in ``_RETRACTED_STRIDE``,
  under a source key no live row carries -- it must not reach
  ``source_count``.
* ``updated_ts`` is distinct per term (one minute apart, ascending with the
  index), so the panel's ``last_revised``-descending sort has something to
  order.

:func:`build_term_scale_corpus` returns the expectations it generated (counts
and a sample of terms with their own expected per-term fields) so a test
asserts against the fixture's ground truth instead of restating literals
that can drift away from it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from trialerror.lexicon.normalize import norm_lemma
from trialerror.stores.store import Store
from trialerror.stores.writer import insert
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

__all__ = ["N_TERMS_SCALE", "SENSES_PER_TERM", "build_term_scale_corpus"]

#: Default term count. The live program that exposed the O(terms) panel had
#: 7,260; 5,000 is the brief's floor for this test and keeps the fixture
#: build well under a second while still being three orders of magnitude
#: past the one-term shared fixture the panel's other tests use.
N_TERMS_SCALE = 5_000

#: Two senses per term, one live evidence row each.
SENSES_PER_TERM = 2

#: Invented syllable table -- pronounceable, unmistakably not words.
_ONSETS = ("vol", "nem", "tark", "zub", "quil", "dref", "pash", "gorn")
_CODAS = ("ath", "ira", "ulo", "eska", "ond", "yrr")

#: Synthetic source identities the evidence rows carry (a register key is a
#: legal ``source_key`` -- see the ``term_sense_evidence`` schema note).
_SOURCE_KEYS = ("REG-alpha", "REG-beta", "REG-gamma", "REG-delta")

_GRANULARITIES = ("family", "instance", None)

_RETIRED_STRIDE = 97
_PROPOSED_STRIDE = 89
_STALE_STRIDE = 11
_PREFERRED_STRIDE = 7
_RETRACTED_STRIDE = 13
#: One term in this many shares ONE source key across both its senses
#: (``source_count`` 1 instead of 2).
_SINGLE_SOURCE_STRIDE = 5

_PAST_REVIEW = "2000-01-01T00:00:00.000Z"
_FUTURE_REVIEW = "2099-01-01T00:00:00.000Z"
_EPOCH = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _stamp(offset_minutes: int) -> str:
    """:func:`trialerror.util.timeutil.now`'s exact format, at a fixed offset from
    a fixed epoch -- so the fixture's ordering is a function of the index,
    not of when the test ran."""
    dt = _EPOCH + timedelta(minutes=offset_minutes)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _lemma(i: int) -> str:
    return f"{_ONSETS[i % len(_ONSETS)]}{_CODAS[(i // len(_ONSETS)) % len(_CODAS)]} {i:05d}"


def _bootstrap_launch(store: Store) -> str:
    account_id = new_id("ACC")
    insert(store, "account", {"account_id": account_id, "label": "term-scale fixture", "created_ts": now()})
    session_id = new_id("SESS")
    insert(store, "session", {"session_id": session_id, "account_id": account_id, "opened_ts": now(), "status": "open"})
    launch_id = new_id("LNCH")
    insert(
        store, "launch",
        {
            "launch_id": launch_id, "account_id": account_id, "program_id": "PROG-term-scale",
            "session_id": session_id, "agent_kind": "tester", "model_class": "top", "model": "sonnet",
            "purpose": "term-scale fixture", "est_tokens": 100, "booked_ts": now(), "state": "PROVISIONAL",
        },
    )
    return launch_id


def build_term_scale_corpus(store: Store, *, n_terms: int = N_TERMS_SCALE) -> dict[str, Any]:
    """Bulk-insert ``n_terms`` terms (two senses and their evidence each,
    ~0.8 relations each) into ``store.knowledge`` and return the ground
    truth about what was written.

    Returned keys: ``launch_id``, ``n_terms``, ``n_senses``, ``n_evidence``
    (live rows only), ``n_relations``, ``conflicts_open`` (distinct pending
    ``conflicts_with`` rel ids), ``needs_review`` (terms with a stale current
    sense), ``by_granularity``, ``newest_term_id`` / ``oldest_term_id`` (the
    ``last_revised`` sort's two ends), ``split_open`` (active terms carrying a
    pending conflict), ``retired``/``proposed`` (head-layer status counts) and
    ``samples`` -- a list of ``{term_id, lemma, granularity, status,
    sense_count, source_count, gloss, last_revised}`` dicts, one per sampled
    term index, carrying what the panel's own row for that term must say.
    """
    launch_id = _bootstrap_launch(store)

    term_rows: list[tuple[Any, ...]] = []
    sense_rows: list[tuple[Any, ...]] = []
    evidence_rows: list[tuple[Any, ...]] = []
    relation_rows: list[tuple[Any, ...]] = []

    by_granularity: dict[str, int] = {}
    conflict_rel_ids: set[str] = set()
    needs_review_terms = 0
    split_open = 0
    retired = 0
    proposed = 0
    n_live_evidence = 0
    sample_indices = {0, 1, 2, 3, 4, _PREFERRED_STRIDE, _STALE_STRIDE, _RETRACTED_STRIDE, n_terms - 1}
    samples: list[dict[str, Any]] = []

    for i in range(n_terms):
        term_id = f"TERM-SCALE{i:06d}"
        sense_ids = [f"SENSE-SCALE{i:06d}-{k}" for k in range(SENSES_PER_TERM)]
        lemma = _lemma(i)
        granularity = _GRANULARITIES[i % len(_GRANULARITIES)]
        created_at = _stamp(i)
        updated_ts = _stamp(i + n_terms)  # distinct per term, ascending with i

        if i % _RETIRED_STRIDE == 0:
            status = "retired"
            retired += 1
        elif i % _PROPOSED_STRIDE == 0:
            status = "proposed"
            proposed += 1
        else:
            status = "active"

        # --- senses: the first is always current; the second is current
        # only on the preferred/stale strides (so the gloss-selection
        # branches both get exercised), proposed otherwise.
        stale = i % _STALE_STRIDE == 0
        preferred = i % _PREFERRED_STRIDE == 0
        second_current = stale or preferred
        sense_statuses = ["current", "current" if second_current else "proposed"]
        sense_reviews = [_FUTURE_REVIEW, _PAST_REVIEW if stale else _FUTURE_REVIEW]
        glosses = [
            f"the first reading recorded for {lemma}, in this fixture's own words",
            f"a second, narrower reading recorded for {lemma}, in this fixture's own words",
        ]
        if stale and second_current:
            needs_review_terms += 1

        for k, sense_id in enumerate(sense_ids):
            sense_rows.append(
                (
                    sense_id, term_id, glosses[k], None, "manual", None, 0.9, "fixture-v1",
                    sense_statuses[k], created_at, None, created_at, None, None,
                    launch_id, launch_id, created_at, sense_reviews[k], None,
                )
            )

        # --- evidence: one live row per sense. Both senses share one source
        # key on the single-source stride, otherwise they carry two.
        single_source = i % _SINGLE_SOURCE_STRIDE == 0
        live_keys: set[str] = set()
        for k, sense_id in enumerate(sense_ids):
            key_index = i if single_source else i + k
            source_key = _SOURCE_KEYS[key_index % len(_SOURCE_KEYS)]
            live_keys.add(source_key)
            evidence_rows.append(
                (
                    f"TSE-SCALE{i:06d}-{k}", sense_id, "record", None, f"REC-SCALE{i:06d}-{k}", source_key,
                    f"{source_key} row {i}", f"what {source_key} records under this name", launch_id,
                    created_at, None, None,
                )
            )
            n_live_evidence += 1
            if i % _RETRACTED_STRIDE == 0:
                evidence_rows.append(
                    (
                        f"TSE-SCALE{i:06d}-{k}-r", sense_id, "record", None, f"REC-SCALE{i:06d}-{k}-r",
                        "REG-retracted", f"REG-retracted row {i}", "a reading taken back", launch_id,
                        created_at, updated_ts, "retracted by the fixture on purpose",
                    )
                )

        # --- relations, by i mod 5 (see the module docstring).
        has_open_conflict = False
        bucket = i % 5
        if bucket == 0:
            rel_id = f"TREL-SCALE{i:06d}-c"
            relation_rows.append(
                (
                    rel_id, "term", term_id, "term", term_id, "conflicts_with", None, "pending",
                    "two readings on disjoint sources", json.dumps({"sense_ids": sense_ids, "shared_sources": []}),
                    0.5, "system", None, "fixture-model", created_at, None, None, None,
                )
            )
            conflict_rel_ids.add(rel_id)
            has_open_conflict = True
        elif bucket == 1 and i + 1 < n_terms:
            relation_rows.append(
                (
                    f"TREL-SCALE{i:06d}-d", "term", term_id, "term", f"TERM-SCALE{i + 1:06d}", "same_as",
                    None, "pending", "near-identical lemmas", None, 0.4, "system", None, "fixture-model",
                    created_at, None, None, None,
                )
            )
        elif bucket == 2:
            relation_rows.append(
                (
                    f"TREL-SCALE{i:06d}-x", "term", term_id, "term", term_id, "conflicts_with", "not_conflict",
                    "rejected", "looked at and judged not a conflict", json.dumps({"sense_ids": sense_ids}),
                    0.5, "system", None, "fixture-model", created_at, launch_id, updated_ts, None,
                )
            )
        elif bucket == 3:
            rel_id = f"TREL-SCALE{i:06d}-s"
            relation_rows.append(
                (
                    rel_id, "sense", sense_ids[0], "sense", sense_ids[1], "conflicts_with", None, "pending",
                    "the two senses stand on disjoint sources", None, 0.6, "system", None, "fixture-model",
                    created_at, None, None, None,
                )
            )
            conflict_rel_ids.add(rel_id)
            has_open_conflict = True

        if status == "active" and has_open_conflict:
            split_open += 1

        gkey = granularity or "__null__"
        by_granularity[gkey] = by_granularity.get(gkey, 0) + 1

        term_rows.append(
            (
                term_id, lemma, norm_lemma(lemma), granularity, json.dumps([f"t-{gkey}"]), None, status,
                sense_ids[1] if preferred else None, None, launch_id, created_at, updated_ts,
            )
        )

        if i in sample_indices:
            if preferred:
                gloss = glosses[1]
            else:
                gloss = glosses[0]  # the first CURRENT sense's gloss
            samples.append(
                {
                    "term_id": term_id, "lemma": lemma, "granularity": granularity, "status": status,
                    "sense_count": SENSES_PER_TERM, "source_count": len(live_keys), "gloss": gloss,
                    "last_revised": updated_ts,
                }
            )

    with store.knowledge:
        store.knowledge.executemany(
            "INSERT INTO term (term_id,lemma,lemma_norm,granularity,tags,entity_id,status,"
            "preferred_sense_id,merged_into,created_by_launch,created_at,updated_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            term_rows,
        )
        store.knowledge.executemany(
            "INSERT INTO term_sense (sense_id,term_id,gloss,disambiguator,origin_kind,origin_ref,"
            "confidence,procedure_version,status,created_at,expired_at,valid_at,invalid_at,"
            "superseded_by,proposed_by_launch,decided_by_launch,decided_ts,review_after,reviewed_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            sense_rows,
        )
        store.knowledge.executemany(
            "INSERT INTO term_sense_evidence (evidence_id,sense_id,evidence_kind,anchor_id,ref_id,"
            "source_key,cite_raw,excerpt,created_by_launch,created_ts,retracted_ts,retracted_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            evidence_rows,
        )
        store.knowledge.executemany(
            "INSERT INTO term_relation (rel_id,src_kind,src_id,dst_kind,dst_id,verb,decided_verb,status,"
            "reason,evidence,confidence,marked_by_kind,marked_by_launch,marked_by_model,marked_ts,"
            "decided_by_launch,decided_ts,superseded_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            relation_rows,
        )

    return {
        "launch_id": launch_id,
        "n_terms": n_terms,
        "n_senses": len(sense_rows),
        "n_evidence": n_live_evidence,
        "n_relations": len(relation_rows),
        "conflicts_open": len(conflict_rel_ids),
        "needs_review": needs_review_terms,
        "by_granularity": by_granularity,
        "split_open": split_open,
        "retired": retired,
        "proposed": proposed,
        "newest_term_id": f"TERM-SCALE{n_terms - 1:06d}",
        "oldest_term_id": "TERM-SCALE000000",
        "samples": samples,
    }
