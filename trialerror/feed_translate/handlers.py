"""The ``feed_translate`` job handler.

**Translation is a JOB, never an inline call.** ``trialerror feed translate``
enqueues; a worker runs this handler; the dashboard shows "translation
pending" until a row exists. That is a deliberate departure from the
"click Translate and block" shape: a model call inside an HTTP request (or
inside ``post_feed``) couples an append-only write path to a network
round-trip, and ``docs/reviews/AISPEAK_TRANSLATOR_DESIGN.md`` Section 4.1
rejects both alternatives (option A: every poster pays whether or not the
post is ever read; option B: one booking per VIEW). Option C -- lazy,
cached, ledger-backed -- is what this handler implements.

Auto-discovered by ``trialerror.jobs.registry.discover_and_register_handlers``
purely because this file lives at ``trialerror/feed_translate/handlers.py``
(one direct subpackage of ``trialerror``, a ``handlers.py`` inside it) --
zero shared-file edits to wire it in, the same mechanism
``trialerror/summarize/handlers.py`` rides.

Per target post, in order:

1. Build the envelope (:func:`trialerror.feed_translate.api.build_translation_envelope`).
2. Get plain text for it, from the first source that has one:
   ``payload["judgments"][post_id]`` (an agent already authored it
   out-of-band -- the house ``--judgments-file`` contract), else the
   configured backend (:mod:`trialerror.feed_translate.backends`). A backend
   that returns ``None`` parks the envelope PENDING in the job checkpoint,
   resumable, exactly like ``run_summarize``.
3. Gate it (:func:`trialerror.feed_translate.gate.run_translation_gate`) --
   always, including for text that came from ``judgments``. A translation
   an agent hand-delivered gets the same fidelity check as one a backend
   produced; there is no trusted path around the gate. When the payload
   carries ``claim_decomposition``/``claim_judgments`` tables (FT-1, fix
   pass), the judged (tier-2, meaning-level) half of the gate runs too --
   see :func:`trialerror.feed_translate.gate.judge_from_claim_table`.
4. Store it (:func:`trialerror.feed_translate.api.store_translation`),
   pass or fail. A failed translation is recorded so
   ``feed_translation_failures`` can count it, and withheld so the
   dashboard never renders it.

**Budget law.** A backend that spends real tokens sets
``requires_booking = True``; this handler then refuses to call it unless
``payload["created_by_launch"]`` names a ``platform.launch`` row that is
STILL ``PROVISIONAL`` and inside its booking TTL -- naming a row that
merely EXISTS is not enough, since ``trialerror.budget.pools.book_launch``
inserts a row for a REFUSED or DEFERRED booking too (FT-2, fix pass: a
prior version of this check only verified the row existed, so a refused,
already-consumed, or expired booking would still be spent). This handler
performs the same atomic conditional ``UPDATE ... WHERE state='PROVISIONAL'
AND <TTL unexpired>`` :func:`trialerror.budget.gate.evaluate_spawn` uses for
the interactive spawn gate (duplicated at the SQL level rather than
imported -- that function's session-id/law-pin/model-policy checks are
specific to the PreToolUse spawn path, which a detached job worker has no
equivalent of; see :func:`_consume_booking`) -- so a booking already spent
by a previous run of this job cannot be spent twice: one booking = one job
run, structurally enforced here, not merely assumed. The refusal is a
LOGIC failure (the payload names an unusable booking; retrying it
unchanged cannot help), not an
:class:`~trialerror.jobs.worker.EnvironmentalFailure`.
"""

from __future__ import annotations

from typing import Any

from trialerror.feed_translate.api import (
    CURRENT_TRANSLATOR_VERSION,
    build_translation_envelope,
    find_untranslated_posts,
    store_translation,
)
from trialerror.feed_translate.backends import load_translator_backend
from trialerror.feed_translate.errors import FeedTranslateError
from trialerror.feed_translate.gate import (
    DEFAULT_FAITHFULNESS_MIN_SCORE,
    judge_from_claim_table,
    run_translation_gate,
)
from trialerror.feed_translate.style import DEFAULT_STYLE_MODE
from trialerror.jobs.registry import register_handler
from trialerror.stores.writer import get as store_get
from trialerror.util.config import CONFIG_FILENAME, load_config
from trialerror.util.timeutil import now

__all__ = ["run_feed_translate", "TRANSLATOR_CONFIG_TABLE"]

#: ``trialerror.toml`` table this handler reads its backend/threshold knobs
#: from. Absent table -> every default below applies (a fresh scaffold
#: translates nothing until asked, and costs nothing).
TRANSLATOR_CONFIG_TABLE = "feed.translator"


def _translator_config(ctx) -> dict[str, Any]:
    """``[feed.translator]`` from the program's ``trialerror.toml``, or
    ``{}``. Best-effort and never fatal -- the same private-per-module
    convention ``trialerror.cli.ingest._load_program_config`` and
    ``trialerror.dashboard.store_ro._load_paths_config`` already use."""
    program_root = getattr(ctx.store, "program_root", None)
    if program_root is None:
        return {}
    cfg_path = program_root / CONFIG_FILENAME
    if not cfg_path.is_file():
        return {}
    try:
        raw = load_config(cfg_path).raw
    except Exception:
        return {}
    table = raw.get("feed", {})
    return dict(table.get("translator", {})) if isinstance(table, dict) else {}


def _consume_booking(store, launch_id: str) -> None:
    """Refuse to spend a backend's budget against ``launch_id`` unless the
    booking is STILL spendable, and consume it atomically so this job run
    is the only one that ever gets to.

    Three refusal cases, in order (FT-2, fix pass):

    1. no row at all -- never booked, or a copy-pasted/typo'd id.
    2. a row exists but is not ``PROVISIONAL`` -- it was REFUSED or
       DEFERRED at booking time (``trialerror.budget.pools.book_launch``
       inserts a row in EITHER case, so row-existence alone was never
       evidence of an affordable booking), or it is RUNNING/RECONCILED/
       ABANDONED because a previous job run (or an interactive spawn)
       already consumed it -- one booking = one job run.
    3. the booking's TTL has expired since it was made.

    The atomic step mirrors :func:`trialerror.budget.gate.evaluate_spawn`'s
    own conditional ``UPDATE`` (PROVISIONAL -> RUNNING iff still
    PROVISIONAL and TTL-unexpired) rather than calling that function: this
    is a detached job worker, not the interactive PreToolUse spawn path,
    and has no "currently open session" to match the booking's
    ``session_id`` against, no law-pin freshness to check, no per-purpose
    model-class policy to enforce -- the ONE piece that does transfer is
    the atomic claim, duplicated here at the SQL level the same way
    ``trialerror.budget.checks``/``trialerror.dashboard.data`` already each
    carry their own TTL-expiry query rather than sharing ``gate.py``'s.
    """
    row = store_get(store, "launch", pk_column="launch_id", pk_value=launch_id)
    if row is None:
        raise FeedTranslateError(
            f"feed_translate: created_by_launch ({launch_id!r}) does not name a row in platform.launch. "
            "Book one with 'trialerror budget book' before enqueuing (the spawn law: one booking = one spawn)."
        )

    ts = now()
    with store.platform:
        cur = store.platform.execute(
            "UPDATE launch SET state = 'RUNNING' "
            "WHERE launch_id = :launch_id AND state = 'PROVISIONAL' "
            "AND julianday(:now) <= julianday(booked_ts) + (booking_ttl_s / 86400.0)",
            {"launch_id": launch_id, "now": ts},
        )
        consumed = cur.rowcount == 1
    if consumed:
        return

    fresh = store_get(store, "launch", pk_column="launch_id", pk_value=launch_id) or row
    if fresh["state"] != "PROVISIONAL":
        raise FeedTranslateError(
            f"feed_translate: created_by_launch ({launch_id!r}) is in state {fresh['state']!r}, not "
            "PROVISIONAL -- it was refused or deferred at booking time, or already consumed by a "
            "previous run (one booking = one job run). Book a fresh launch_id."
        )
    raise FeedTranslateError(
        f"feed_translate: created_by_launch ({launch_id!r})'s booking TTL "
        f"({fresh['booking_ttl_s']}s from {fresh['booked_ts']}) has expired -- book a fresh launch_id."
    )


@register_handler("feed_translate")
def run_feed_translate(ctx) -> None:  # ctx: trialerror.jobs.worker.JobContext
    """Job payload shape (a flat JSON dict, every ``trialerror`` job's own
    convention)::

        {"handler": "feed_translate",
         "post_ids": [POST-..., ...]        -- explicit targets, or
         "thread_id": "THR-..."             -- every untranslated post in one thread, or
         (neither)                          -- every untranslated post, program-wide,
         "translator_version": "1",
         "style_mode": "flavored"|"strict",
         "created_by_launch": "LNCH-..."    -- optional; null = orchestrator identity,
         "judgments": {post_id: plain_text} -- optional precomputed answers,
         "claim_decomposition": {pair_id: {claims: [...]}}    -- optional, FT-1
         "claim_judgments": {claim_pair_id: {label, note?}}   -- optional, FT-1}

    Idempotent and resumable: the target list is re-derived from the STORE
    on every call when it was auto-discovered (an already-translated post
    simply no longer appears), and an explicit target that already has a
    current translation at this ``translator_version`` is skipped unless
    the caller supplied a fresh judgment for it.

    ``claim_decomposition``/``claim_judgments`` are the JSON-serializable
    tables :func:`trialerror.cli.feed.run_translate`'s
    ``--claim-decomposition-file``/``--claim-judgments-file`` load off
    disk -- the same ``{pair_id: judgment}`` shape
    ``trialerror.cli.verify``'s ``--decomposition-file``/``--judgments-file``
    already use for ``verify faithfulness``, keyed by the pair ids
    :func:`trialerror.feed_translate.gate.run_translation_gate`'s judged
    tier mints (``<post_id>::S-<n>`` per sentence,
    ``<post_id>::S-<n>::CLM-<m>`` per decomposed claim). Supplying both
    turns on the JUDGED (meaning-level) half of the gate for this run, via
    :func:`trialerror.feed_translate.gate.judge_from_claim_table` --
    reconstituting a judge callable from a plain dict, not carrying a
    callable through the payload, so this crosses the job queue's
    process boundary the same way ``judgments`` (the translation text
    itself) already does.
    """
    payload = ctx.payload
    store = ctx.store
    translator_version = str(payload.get("translator_version", CURRENT_TRANSLATOR_VERSION))
    style_mode = payload.get("style_mode", DEFAULT_STYLE_MODE)
    created_by_launch = payload.get("created_by_launch")
    judgments: dict[str, str] = dict(payload.get("judgments") or {})
    claim_decomposition = payload.get("claim_decomposition")
    claim_judgments = payload.get("claim_judgments")
    decompose_judge = judge_from_claim_table(claim_decomposition) if claim_decomposition else None
    verify_judge = judge_from_claim_table(claim_judgments) if claim_judgments else None

    config = _translator_config(ctx)
    backend = load_translator_backend(config)
    strict_style = bool(config.get("strict_style", False))
    require_score = bool(config.get("require_faithfulness_score", False))
    min_score = float(config.get("faithfulness_min_score", DEFAULT_FAITHFULNESS_MIN_SCORE))

    post_ids = payload.get("post_ids")
    if not post_ids:
        post_ids = find_untranslated_posts(
            store, thread_id=payload.get("thread_id"), translator_version=translator_version
        )

    if backend.requires_booking:
        if not created_by_launch:
            raise FeedTranslateError(
                f"feed_translate: backend {backend.name!r} spends budget and requires a booked launch, but "
                "the job payload's created_by_launch is empty. Book one with 'trialerror budget book' before "
                "enqueuing (the spawn law: one booking = one spawn)."
            )
        _consume_booking(store, created_by_launch)

    written: dict[str, str] = {}
    withheld: dict[str, str] = {}
    pending: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    def _checkpoint(i: int) -> None:
        ctx.set_checkpoint(
            {
                "processed": i,
                "total": len(post_ids),
                "written": dict(written),
                "withheld": dict(withheld),
                "pending": len(pending),
                "skipped": len(skipped),
            }
        )

    for i, post_id in enumerate(post_ids):
        try:
            envelope = build_translation_envelope(
                store, post_id=post_id, style_mode=style_mode, translator_version=translator_version
            )
        except FeedTranslateError as exc:
            skipped.append({"post_id": post_id, "error": str(exc)})
            _checkpoint(i + 1)
            continue

        body = judgments.get(post_id)
        if body is None:
            body = backend.translate(envelope)
        if body is None or not str(body).strip():
            pending.append(envelope)
            _checkpoint(i + 1)
            continue

        gate = run_translation_gate(
            store,
            post_id=post_id,
            original_body=envelope["original_body"],
            translation_body=body,
            style_mode=style_mode,
            strict_style=strict_style,
            require_faithfulness_score=require_score,
            min_score=min_score,
            decompose_judge=decompose_judge,
            verify_judge=verify_judge,
            issued_by_launch=created_by_launch,
        )
        row = store_translation(
            store, envelope=envelope, body=body, gate=gate.as_row(), created_by_launch=created_by_launch
        )
        if gate.passed:
            written[post_id] = row["translation_id"]
        else:
            withheld[post_id] = row["translation_id"]
        _checkpoint(i + 1)

    ctx.set_checkpoint(
        {
            "processed": len(post_ids),
            "total": len(post_ids),
            "written": written,
            "withheld": withheld,
            "pending_envelopes": pending,
            "skipped": skipped,
        }
    )
