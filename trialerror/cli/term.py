"""``trialerror term`` -- the lexicon term store CLI surface. Design of
record: ``docs/reviews/LANE_E_TERM_STORE_DESIGN.md`` Section 7 (verb list),
Section 3 (the lifecycle each verb wraps), build step E3.

Thin wrapper over ``trialerror.lexicon.api`` (the ``trialerror/cli/extract.py``
shape: ``_open``, structured errors, ``--by-launch``) -- the lifecycle logic
lives there, not here. The read-only listing verbs (``list``/``show``/
``status``/``review``) have no equivalent read function in ``lexicon.api``
shaped for a listing (its reads are all scoped to one term/sense/relation),
so those four build their own small SQL directly against ``store.knowledge``
-- the same "a second implementation of a small read is fine, a second
implementation of a write is not" convention
``trialerror.summarize.api.find_stale_or_missing_document_summaries``'s own
TRIALERROR-DEV-NOTE states for exactly this reason, and
``trialerror.feed_translate.checks``/``trialerror.lexicon.checks`` reuse
without sharing a module either. Every MUTATING verb requires ``--by-launch``
(design Section 7's closing sentence) except ``reindex`` (FTS maintenance;
``trialerror.lexicon.api.reindex_all`` takes no launch -- it changes no
belief, just the index built from one).

Design Section 5.2 registration rule: this module lives at
``trialerror/cli/term.py`` and is auto-discovered by
``trialerror.cli.discover_groups`` -- adding it never touches
``trialerror/cli/__init__.py``.

**Five verbs reach into the E2 modules through import guards, exactly like
``trialerror.lexicon.api``'s own two hooks (its module docstring, "Where the
seams to later steps are").** ``scan``'s conflict half
(``trialerror.lexicon.scan.conflicts_for_term``) and duplicate half
(``trialerror.lexicon.candidates.surface_candidates``); ``backfill-records``,
``backfill-claims`` and ``relink`` in ``trialerror.lexicon.backfill``. E3 was
built on a branch where none of those three modules existed yet (design
Section 10 runs E2/E3/E4 from E1 in parallel), so the three ``backfill``
function names here were this file's best-effort guess and were flagged for
whoever merged the two steps. **Reconciled at the lane merge:** E2 shipped
``backfill_records`` and ``backfill_claims`` under the guessed names, and
``relink_evidence_sources`` where this file had written ``relink`` -- that is
now the name imported, so ``trialerror term relink`` reaches a real function
instead of reporting itself unavailable.

The guards themselves stay. A partial install is a state a consumer of this
CLI should be able to read rather than crash on -- the same reason
``lexicon.api`` keeps its two -- so each affected verb still answers
``{"status": "unavailable", "reason": ...}`` rather than a traceback if the
module is ever absent.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence

from trialerror.lexicon import api as lexicon_api
from trialerror.lexicon import policy
from trialerror.lexicon.errors import LexiconError
from trialerror.lexicon.normalize import norm_lemma
from trialerror.stores.errors import StoreError, ValidationError, XidTargetMissingError
from trialerror.stores.store import Store, open_store
from trialerror.util.config import find_program_root
from trialerror.util.envelope import error_envelope, next_action, ok_envelope
from trialerror.util.timeutil import now

GROUP_NAME = "term"
HELP = "The lexicon term store: propose/accept/reject senses, decide conflicts and duplicates, merge, supersede, retire, scan, backfill, and browse."

#: The CLI's own default for an ideation-origin proposal's ``procedure_version``.
#: ``trialerror.lexicon.policy`` names one for ``manual``/``extract``/
#: ``record_import`` (the three routes an existing build step writes from);
#: ``ideation`` has none yet because no landed build step proposes from that
#: origin -- design Section 4's ideation-round paragraph names the CLI
#: invocation but not a procedure_version literal. Overridable with
#: ``--procedure-version``, same as every other origin.
_IDEATION_PROCEDURE_VERSION = "ideation-v1"

_PROCEDURE_VERSION_DEFAULTS: dict[str, str] = {
    "manual": policy.MANUAL_PROCEDURE_VERSION,
    "extract": policy.EXTRACT_PROCEDURE_VERSION,
    "record_import": policy.RECORD_IMPORT_PROCEDURE_VERSION,
    "ideation": _IDEATION_PROCEDURE_VERSION,
}


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    parser.add_argument("--program-root", default=argparse.SUPPRESS, help="override the program root (default: discover trialerror.toml upward from CWD)")
    parser.add_argument("--platform-root", default=argparse.SUPPRESS, help="override the platform root (mainly for tests)")
    actions = parser.add_subparsers(dest="action", metavar="<action>")

    def _common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--program-root", default=argparse.SUPPRESS, help="override the program root (default: discover trialerror.toml upward from CWD)")
        p.add_argument("--platform-root", default=argparse.SUPPRESS, help="override the platform root (mainly for tests)")

    p_propose = actions.add_parser("propose", help="propose one reading of one lemma, with its evidence")
    _common(p_propose)
    p_propose.add_argument("--lemma", required=True)
    p_propose.add_argument("--gloss", required=True)
    p_propose.add_argument("--origin", dest="origin_kind", choices=list(policy.ORIGIN_KINDS), default="manual")
    p_propose.add_argument("--origin-ref", dest="origin_ref", default=None)
    p_propose.add_argument("--evidence", action="append", default=[], help="repeatable '<kind>:<id>' token (kinds: anchor, record, claim, idea)")
    p_propose.add_argument("--granularity", choices=list(policy.GRANULARITIES), default=None)
    p_propose.add_argument("--tag", dest="tags", action="append", default=None)
    p_propose.add_argument("--alias", dest="aliases", action="append", default=None)
    p_propose.add_argument("--status", choices=["proposed", "current"], default="proposed")
    p_propose.add_argument("--confidence", type=float, default=None)
    p_propose.add_argument("--disambiguator", default=None)
    p_propose.add_argument("--procedure-version", dest="procedure_version", default=None)
    p_propose.add_argument("--entity-id", dest="entity_id", default=None)
    p_propose.add_argument("--by-launch", required=True, dest="by_launch")
    p_propose.set_defaults(handler=_run_propose)

    p_accept = actions.add_parser("accept", help="accept a proposed sense: proposed -> current")
    _common(p_accept)
    p_accept.add_argument("sense_id", metavar="SENSE_ID")
    p_accept.add_argument("--by-launch", required=True, dest="by_launch")
    p_accept.set_defaults(handler=_run_accept)

    p_reject = actions.add_parser("reject", help="reject a proposed sense")
    _common(p_reject)
    p_reject.add_argument("sense_id", metavar="SENSE_ID")
    p_reject.add_argument("--by-launch", required=True, dest="by_launch")
    p_reject.add_argument("--reason", default=None)
    p_reject.set_defaults(handler=_run_reject)

    p_decide = actions.add_parser("decide", help="resolve one pending term_relation (a conflict or a duplicate candidate)")
    _common(p_decide)
    p_decide.add_argument("rel_id", metavar="REL_ID")
    p_decide.add_argument("--decision", required=True, choices=list(policy.RELATION_DECISIONS))
    p_decide.add_argument(
        "--disambiguator", dest="disambiguators", action="append", default=None,
        help="repeatable 'SENSE_ID=text' pair; required for every member sense when --decision scoped",
    )
    p_decide.add_argument("--into", dest="into", default=None, help="the sense to keep, for --decision not_conflict")
    p_decide.add_argument("--canonical", dest="canonical", default=None, help="the term to keep as canonical, for --decision same_as|variant_of (default: the relation's destination term)")
    p_decide.add_argument("--reason", default=None)
    p_decide.add_argument("--by-launch", required=True, dest="by_launch")
    p_decide.set_defaults(handler=_run_decide)

    p_merge = actions.add_parser("merge", help="fold one term into another (opens and confirms a same_as decision in one step)")
    _common(p_merge)
    p_merge.add_argument("term_id", metavar="TERM_ID", help="the term to fold away")
    p_merge.add_argument("--into", dest="into", required=True, help="the term to keep as canonical")
    p_merge.add_argument("--alias-kind", dest="alias_kind", choices=list(policy.ALIAS_KINDS), default="former_lemma")
    p_merge.add_argument("--reason", default=None)
    p_merge.add_argument("--by-launch", required=True, dest="by_launch")
    p_merge.set_defaults(handler=_run_merge)

    p_supersede = actions.add_parser("supersede", help="a corrected reading: assert the replacement, expire the old one")
    _common(p_supersede)
    p_supersede.add_argument("sense_id", metavar="SENSE_ID")
    p_supersede.add_argument("--gloss", required=True)
    p_supersede.add_argument("--disambiguator", default=None)
    p_supersede.add_argument("--procedure-version", dest="procedure_version", default=None)
    p_supersede.add_argument("--confidence", type=float, default=None)
    p_supersede.add_argument("--reason", default=None)
    p_supersede.add_argument("--by-launch", required=True, dest="by_launch")
    p_supersede.set_defaults(handler=_run_supersede)

    p_retire = actions.add_parser("retire", help="the reading stopped being used (a SENSE id; see the command's own docstring for a TERM id)")
    _common(p_retire)
    p_retire.add_argument("id", metavar="SENSE_ID")
    p_retire.add_argument("--reason", default=None)
    p_retire.add_argument("--by-launch", required=True, dest="by_launch")
    p_retire.set_defaults(handler=_run_retire)

    p_review = actions.add_parser("review", help="list what needs a decision: pending conflicts, pending duplicates, stale senses")
    _common(p_review)
    p_review.add_argument("--kind", choices=["conflict", "duplicate", "stale"], default=None)
    p_review.set_defaults(handler=_run_review)

    p_mark_reviewed = actions.add_parser("mark-reviewed", help="\"I looked at this and it is still right\": push review_after out again")
    _common(p_mark_reviewed)
    p_mark_reviewed.add_argument("sense_id", metavar="SENSE_ID")
    p_mark_reviewed.add_argument("--by-launch", required=True, dest="by_launch")
    p_mark_reviewed.set_defaults(handler=_run_mark_reviewed)

    p_scan = actions.add_parser("scan", help="idempotent conflict + duplicate scan (one term, or every term); --rescan withdraws stale duplicate candidates")
    _common(p_scan)
    p_scan.add_argument("--term", dest="term_id", default=None)
    p_scan.add_argument(
        "--rescan",
        action="store_true",
        help="instead of scanning, re-evaluate the pending system-opened same_as queue under the "
        "current duplicate gate and withdraw the candidates it no longer opens (needs --by-launch)",
    )
    p_scan.add_argument(
        "--dry-run", dest="dry_run", action="store_true",
        help="with --rescan: report what would be withdrawn and write nothing",
    )
    p_scan.add_argument(
        "--by-launch", dest="by_launch", default=None,
        help="required with --rescan -- a withdrawal is a launch-attributed act; the plain scan "
        "opens system rows and needs none",
    )
    p_scan.set_defaults(handler=_run_scan)

    p_backfill_records = actions.add_parser("backfill-records", help="project knowledge.record rows into the lexicon (register import, ruling L-E2)")
    _common(p_backfill_records)
    p_backfill_records.add_argument("--register-key", dest="register_keys", action="append", default=None)
    p_backfill_records.add_argument("--family-map", dest="family_map", default=None, help="path to a {tag: {lemma, gloss}} JSON file (ruling L-E3; orchestrator-supplied)")
    p_backfill_records.add_argument("--by-launch", required=True, dest="by_launch")
    p_backfill_records.set_defaults(handler=_run_backfill_records)

    p_backfill_claims = actions.add_parser("backfill-claims", help="project live definition claims into the lexicon")
    _common(p_backfill_claims)
    p_backfill_claims.add_argument("--lemma-map", dest="lemma_map", default=None, help="path to a {claim_id: lemma} JSON file for claims with no explicit term")
    p_backfill_claims.add_argument("--by-launch", required=True, dest="by_launch")
    p_backfill_claims.set_defaults(handler=_run_backfill_claims)

    p_relink = actions.add_parser("relink", help="rewrite evidence source_key from a {register_key: source_id} map, once those sources are ingested")
    _common(p_relink)
    p_relink.add_argument("--map", dest="map_path", required=True, help="path to a {register_key: source_id} JSON file")
    p_relink.add_argument("--by-launch", required=True, dest="by_launch")
    p_relink.set_defaults(handler=_run_relink)

    p_reindex = actions.add_parser("reindex", help="rebuild term_fts from scratch")
    _common(p_reindex)
    p_reindex.set_defaults(handler=_run_reindex)

    p_list = actions.add_parser("list", help="list terms, optionally filtered")
    _common(p_list)
    p_list.add_argument("--state", dest="state", choices=list(policy.TERM_STATUSES), default=None, help="filters on term.status")
    p_list.add_argument("--granularity", choices=list(policy.GRANULARITIES), default=None)
    p_list.add_argument("--q", dest="q", default=None, help="substring match against the normalized lemma")
    p_list.set_defaults(handler=_run_list)

    p_show = actions.add_parser("show", help="show one term or sense in full (senses, evidence, relations)")
    _common(p_show)
    p_show.add_argument("id", metavar="TERM_ID|SENSE_ID")
    p_show.set_defaults(handler=_run_show)

    p_status = actions.add_parser("status", help="summary counts: terms/senses by status, pending conflicts/duplicates, stale, unlinked evidence")
    _common(p_status)
    p_status.set_defaults(handler=_run_status)

    parser.set_defaults(handler=_run_no_action)
    return parser


# ---------------------------------------------------------------------------
# shared plumbing
# ---------------------------------------------------------------------------


def _resolve_program_root(args: argparse.Namespace) -> Path | None:
    if getattr(args, "program_root", None):
        return Path(args.program_root)
    return find_program_root()


def _open(args: argparse.Namespace, cmd: str) -> tuple[Store | None, dict | None]:
    program_root = _resolve_program_root(args)
    if program_root is None:
        return None, error_envelope(
            cmd, "no_program_root", "no --program-root given and no trialerror.toml found walking up from CWD",
            next_actions=[next_action(["trialerror", "program", "init", "<name>", "--dir", "."], "scaffold a program in this directory first")],
        )
    return open_store(program_root, platform_root=getattr(args, "platform_root", None)), None


def _load_program_config(program_root: Path) -> dict:
    """The program's ``trialerror.toml`` as a plain dict, or ``{}``.

    The same best-effort private helper every other CLI group already
    carries (``trialerror.cli.ingest._load_program_config``,
    ``trialerror.cli.law``, ``trialerror.cli.memory``, ...), copied for the
    same reason they each state rather than consolidated: one shared helper
    is a refactor, and it is not this build step's lane.
    """
    from trialerror.util.config import CONFIG_FILENAME, load_config

    cfg_path = Path(program_root) / CONFIG_FILENAME
    if not cfg_path.is_file():
        return {}
    try:
        return load_config(cfg_path).raw
    except Exception:
        return {}


def _config(args: argparse.Namespace) -> dict:
    """The ``[lexicon]``-bearing config this group threads into
    ``trialerror.lexicon`` (build step 1d).

    **This group used to pass none at all.** Every knob in
    ``trialerror.lexicon.policy`` -- both gloss caps, the four review
    windows, all four duplicate-scan numbers -- is overridable per program
    in ``[lexicon]``, and until 1d not one of those overrides could be
    reached through ``trialerror term``: the library functions took a
    ``config`` argument and this file never built one to pass. It surfaced
    as an operator sweeping ``duplicate_informative_token_fraction`` across
    three values on a live program and watching
    ``term scan --rescan --dry-run`` report the same threshold every time.

    Resolved from the program root here rather than carried out of
    :func:`_open`, so :func:`_open`'s two-value contract stays exactly as
    every existing verb calls it and a verb that needs no config pays
    nothing.
    """
    program_root = _resolve_program_root(args)
    return _load_program_config(program_root) if program_root is not None else {}


def _run_no_action(_args: argparse.Namespace) -> dict:
    return error_envelope(
        "term", "no_action",
        "specify an action: propose|accept|reject|decide|merge|supersede|retire|review|mark-reviewed|"
        "scan|backfill-records|backfill-claims|relink|reindex|list|show|status",
        next_actions=[next_action(["trialerror", "term", "--help"], "list term actions")],
    )


def _lexicon_error_envelope(cmd: str, exc: Exception) -> dict:
    return error_envelope(cmd, type(exc).__name__, str(exc))


def _store_error_envelope(cmd: str, exc: Exception) -> dict:
    return error_envelope(cmd, "record_refused", str(exc))


def _resolve_procedure_version(args: argparse.Namespace) -> str:
    if getattr(args, "procedure_version", None):
        return args.procedure_version
    return _PROCEDURE_VERSION_DEFAULTS.get(args.origin_kind, policy.MANUAL_PROCEDURE_VERSION)


def _parse_kv_pairs(values: list[str] | None, *, cmd: str, flag: str) -> dict[str, str]:
    """``['SENSE-1=a reading', 'SENSE-2=another']`` -> ``{"SENSE-1": "a
    reading", "SENSE-2": "another"}``. Raises :class:`ValueError` (turned
    into a structured error by the caller) on a token with no ``=``."""
    out: dict[str, str] = {}
    for token in values or []:
        key, sep, value = token.partition("=")
        if not sep:
            raise ValueError(f"{flag} token {token!r} must be 'KEY=value'")
        out[key.strip()] = value.strip()
    return out


# ---------------------------------------------------------------------------
# propose / accept / reject / supersede / retire / mark-reviewed
# ---------------------------------------------------------------------------


def _run_propose(args: argparse.Namespace) -> dict:
    store, err = _open(args, "term.propose")
    if err is not None:
        return err
    try:
        result = lexicon_api.propose(
            store,
            lemma=args.lemma,
            gloss=args.gloss,
            origin_kind=args.origin_kind,
            origin_ref=args.origin_ref,
            evidence=args.evidence,
            by_launch=args.by_launch,
            procedure_version=_resolve_procedure_version(args),
            granularity=args.granularity,
            tags=args.tags,
            aliases=args.aliases or (),
            confidence=args.confidence,
            disambiguator=args.disambiguator,
            entity_id=args.entity_id,
            status=args.status,
            config=_config(args),
        )
    except LexiconError as exc:
        return _lexicon_error_envelope("term.propose", exc)
    except (ValidationError, XidTargetMissingError, StoreError) as exc:
        return _store_error_envelope("term.propose", exc)
    finally:
        store.close()
    return ok_envelope(
        "term.propose", result=result,
        next_actions=[next_action(["trialerror", "term", "show", result["term_id"]], "see the full term")],
    )


def _run_accept(args: argparse.Namespace) -> dict:
    store, err = _open(args, "term.accept")
    if err is not None:
        return err
    try:
        result = lexicon_api.accept_sense(store, args.sense_id, by_launch=args.by_launch, config=_config(args))
    except LexiconError as exc:
        return _lexicon_error_envelope("term.accept", exc)
    except (ValidationError, XidTargetMissingError, StoreError) as exc:
        return _store_error_envelope("term.accept", exc)
    finally:
        store.close()
    return ok_envelope("term.accept", result=result)


def _run_reject(args: argparse.Namespace) -> dict:
    store, err = _open(args, "term.reject")
    if err is not None:
        return err
    try:
        result = lexicon_api.reject_sense(store, args.sense_id, by_launch=args.by_launch, reason=args.reason)
    except LexiconError as exc:
        return _lexicon_error_envelope("term.reject", exc)
    except (ValidationError, XidTargetMissingError, StoreError) as exc:
        return _store_error_envelope("term.reject", exc)
    finally:
        store.close()
    return ok_envelope("term.reject", result=result)


def _run_supersede(args: argparse.Namespace) -> dict:
    store, err = _open(args, "term.supersede")
    if err is not None:
        return err
    try:
        result = lexicon_api.supersede_sense(
            store, args.sense_id,
            gloss=args.gloss, by_launch=args.by_launch, disambiguator=args.disambiguator,
            procedure_version=args.procedure_version, confidence=args.confidence, reason=args.reason,
            config=_config(args),
        )
    except LexiconError as exc:
        return _lexicon_error_envelope("term.supersede", exc)
    except (ValidationError, XidTargetMissingError, StoreError) as exc:
        return _store_error_envelope("term.supersede", exc)
    finally:
        store.close()
    return ok_envelope("term.supersede", result=result)


def _run_retire(args: argparse.Namespace) -> dict:
    """Retires a SENSE (``lexicon.api.retire_sense``). A TERM id is refused
    with a named error rather than guessed at: ``lexicon.api`` (E1, frozen
    for this build) exposes no term-level retire -- only a per-sense one --
    so there is no write path here to route a TERM id to without inventing
    mutation logic outside the module that owns the invariants (design's
    "the head layer is the only thing that updates in place, and every
    such update appends one type-keyed event" contract). Design Section 7
    names this verb ``retire <SENSE|TERM>``; this is a stated deviation,
    not an oversight -- see the E3 IMPL report."""
    store, err = _open(args, "term.retire")
    if err is not None:
        return err
    try:
        sense = lexicon_api.get_sense(store, args.id)
        if sense is not None:
            result = lexicon_api.retire_sense(store, args.id, by_launch=args.by_launch, reason=args.reason)
            return ok_envelope("term.retire", result=result)
        term = lexicon_api.get_term(store, args.id)
        if term is not None:
            return error_envelope(
                "term.retire", "term_retire_not_implemented",
                f"{args.id!r} is a term, not a sense; trialerror.lexicon.api exposes retire_sense only "
                "in this build -- retire each of its current/proposed senses individually "
                "('trialerror term retire <SENSE_ID>' per sense) instead of the whole term",
            )
        return error_envelope("term.retire", "not_found", f"no such term or sense: {args.id!r}")
    except LexiconError as exc:
        return _lexicon_error_envelope("term.retire", exc)
    except (ValidationError, XidTargetMissingError, StoreError) as exc:
        return _store_error_envelope("term.retire", exc)
    finally:
        store.close()


def _run_mark_reviewed(args: argparse.Namespace) -> dict:
    store, err = _open(args, "term.mark-reviewed")
    if err is not None:
        return err
    try:
        result = lexicon_api.mark_reviewed(store, args.sense_id, by_launch=args.by_launch, config=_config(args))
    except LexiconError as exc:
        return _lexicon_error_envelope("term.mark-reviewed", exc)
    except (ValidationError, XidTargetMissingError, StoreError) as exc:
        return _store_error_envelope("term.mark-reviewed", exc)
    finally:
        store.close()
    return ok_envelope("term.mark-reviewed", result=result)


# ---------------------------------------------------------------------------
# decide / merge
# ---------------------------------------------------------------------------


def _run_decide(args: argparse.Namespace) -> dict:
    store, err = _open(args, "term.decide")
    if err is not None:
        return err
    try:
        try:
            disambiguators = _parse_kv_pairs(args.disambiguators, cmd="term.decide", flag="--disambiguator")
        except ValueError as exc:
            return error_envelope("term.decide", "bad_input", str(exc))
        result = lexicon_api.decide_relation(
            store, args.rel_id,
            decision=args.decision, by_launch=args.by_launch,
            disambiguators=disambiguators or None, into=args.into, canonical=args.canonical, reason=args.reason,
        )
    except LexiconError as exc:
        return _lexicon_error_envelope("term.decide", exc)
    except (ValidationError, XidTargetMissingError, StoreError) as exc:
        return _store_error_envelope("term.decide", exc)
    finally:
        store.close()
    return ok_envelope("term.decide", result=result)


def _run_merge(args: argparse.Namespace) -> dict:
    store, err = _open(args, "term.merge")
    if err is not None:
        return err
    try:
        result = lexicon_api.merge_terms(
            store, args.into, args.term_id,
            by_launch=args.by_launch, alias_kind=args.alias_kind, reason=args.reason,
        )
    except LexiconError as exc:
        return _lexicon_error_envelope("term.merge", exc)
    except (ValidationError, XidTargetMissingError, StoreError) as exc:
        return _store_error_envelope("term.merge", exc)
    finally:
        store.close()
    return ok_envelope("term.merge", result=result)


# ---------------------------------------------------------------------------
# reindex
# ---------------------------------------------------------------------------


def _run_reindex(args: argparse.Namespace) -> dict:
    store, err = _open(args, "term.reindex")
    if err is not None:
        return err
    try:
        result = lexicon_api.reindex_all(store)
    finally:
        store.close()
    return ok_envelope("term.reindex", result=result)


# ---------------------------------------------------------------------------
# scan -- the E2 seam. Same two named functions lexicon.api already imports
# lazily (its own "Where the seams to later steps are" section).
# ---------------------------------------------------------------------------


def _scan_one_term(
    store: Store,
    term_id: str,
    *,
    config: Mapping[str, Any] | None = None,
    stats: Any = None,
    prefetch: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One term's two halves. ``prefetch`` is :func:`_scan_prefetch`'s
    whole-store maps, present when this verb is scanning every term: the
    conflict half then asks its three questions of those maps instead of the
    database (backlog item (c)), and the current-sense ids the duplicate half
    iterates come from the same source-set map, whose keys ARE
    ``senses_for_term(..., statuses=("current",))``'s senses in that
    function's own order."""
    pre = dict(prefetch or {})
    conflicts: dict[str, Any]
    try:
        from trialerror.lexicon.scan import conflicts_for_term
    except ImportError:
        conflicts = {"status": "unavailable", "reason": "trialerror.lexicon.scan is not importable"}
    else:
        conflicts = conflicts_for_term(
            store,
            term_id,
            term=(pre.get("terms") or {}).get(term_id),
            source_sets=pre.get("source_sets"),
            blocking=pre.get("blocking"),
        )

    duplicates: list[Any] | dict[str, Any]
    try:
        from trialerror.lexicon.candidates import surface_candidates
    except ImportError:
        duplicates = {"status": "unavailable", "reason": "trialerror.lexicon.candidates is not importable"}
    else:
        source_sets = pre.get("source_sets")
        if source_sets is not None:
            sense_ids = list(source_sets.get(term_id) or {})
        else:
            sense_ids = [
                s["sense_id"]
                for s in lexicon_api.senses_for_term(store, term_id, statuses=("current",))
            ]
        duplicates = [
            surface_candidates(store, term_id, sense_id=sense_id, config=config, stats=stats)
            for sense_id in sense_ids
        ]
    return {"term_id": term_id, "conflicts": conflicts, "duplicates": duplicates}


def _scan_prefetch(store: Store, term_ids: Sequence[str]) -> dict[str, Any] | None:
    """The three whole-store reads a scan over many terms takes once, or
    ``None`` if ``trialerror.lexicon.scan`` is not importable (this verb's own
    E2-seam posture: report, never crash).

    The same maps :func:`trialerror.lexicon.scan.scan_terms` builds, for the
    same reason -- this verb is the other caller of the same per-term
    function, and only one of the two getting the batched reads would leave
    the O(terms) pattern in the surface an operator actually types."""
    try:
        from trialerror.lexicon.scan import (
            blocking_conflicts_by_term,
            source_sets_for_terms,
            terms_by_id,
        )
    except ImportError:
        return None
    return {
        "terms": terms_by_id(store, term_ids),
        "source_sets": source_sets_for_terms(store, term_ids),
        "blocking": blocking_conflicts_by_term(store, term_ids),
    }


def _run_rescan(args: argparse.Namespace) -> dict:
    """``term scan --rescan`` -- the withdrawal pass (build step 1c, D4).

    A MODE, not an extra half of the ordinary scan: the operator running it
    is asking the store to take back guesses it already made, which is the
    opposite act from opening new ones and wants its own launch, its own
    dry run and its own counts. Running both is running the verb twice, in
    whichever order the operator means -- and after a withdrawal a plain
    scan stays quiet about those pairs, because the rows it would re-open
    are the ones it just closed.
    """
    store, err = _open(args, "term.scan")
    if err is not None:
        return err
    if not args.by_launch:
        store.close()
        return error_envelope(
            "term.scan", "missing_launch",
            "--rescan withdraws pending candidates and therefore needs --by-launch (ruling L-E4)",
            next_actions=[next_action(["trialerror", "term", "scan", "--rescan", "--dry-run", "--by-launch", "<LNCH-…>"], "measure the withdrawal first")],
        )
    try:
        try:
            from trialerror.lexicon.candidates import rescan_duplicate_candidates
        except ImportError:
            return ok_envelope(
                "term.scan",
                result={"status": "unavailable", "reason": "trialerror.lexicon.candidates is not importable"},
            )
        result = rescan_duplicate_candidates(
            store, by_launch=args.by_launch, config=_config(args), dry_run=bool(args.dry_run)
        )
    except LexiconError as exc:
        return _lexicon_error_envelope("term.scan", exc)
    except (ValidationError, XidTargetMissingError, StoreError) as exc:
        return _store_error_envelope("term.scan", exc)
    except sqlite3.OperationalError as exc:
        return error_envelope("term.scan", "not_initialized", f"the term store is not available yet: {exc}")
    finally:
        store.close()
    return ok_envelope("term.scan", result=result)


def _run_scan(args: argparse.Namespace) -> dict:
    if getattr(args, "rescan", False):
        return _run_rescan(args)
    store, err = _open(args, "term.scan")
    if err is not None:
        return err
    config = _config(args)
    try:
        if args.term_id:
            term = lexicon_api.get_term(store, args.term_id)
            if term is None:
                return error_envelope("term.scan", "not_found", f"no such term: {args.term_id!r}")
            term_ids = [args.term_id]
        else:
            term_ids = [r[0] for r in store.knowledge.execute("SELECT term_id FROM term ORDER BY term_id").fetchall()]
        # A whole-store pass measures what is a common word ONCE, the way
        # `trialerror.lexicon.scan.scan_terms` already does -- otherwise
        # every one of a store's terms rebuilds the frequency table over
        # every other one, which on the live program's 6,992 terms is 6,992
        # full passes over `term` + `term_alias` for a single verb. A
        # single-term scan deliberately keeps `None` and gets the freshest
        # possible view, the save-time behaviour
        # `trialerror.lexicon.candidates.token_stats` argues for.
        stats = None
        prefetch = None
        if not args.term_id:
            try:
                from trialerror.lexicon.candidates import token_stats
            except ImportError:
                stats = None
            else:
                stats = token_stats(store, config=config)
            # Same reasoning as `stats`, for the conflict half's own three
            # per-term questions (backlog item (c)). A single-term scan
            # deliberately keeps `None` here too: two statements for one term
            # is already the bounded read, and the freshest one.
            prefetch = _scan_prefetch(store, term_ids)
        results = [
            _scan_one_term(store, tid, config=config, stats=stats, prefetch=prefetch)
            for tid in term_ids
        ]
    except sqlite3.OperationalError as exc:
        return error_envelope("term.scan", "not_initialized", f"the term store is not available yet: {exc}")
    finally:
        store.close()
    return ok_envelope("term.scan", result={"scanned": len(results), "terms": results})


# ---------------------------------------------------------------------------
# backfill-records / backfill-claims / relink -- E2 seams. See module
# docstring for why these three names are this file's best-effort naming,
# not a pinned E2 contract.
# ---------------------------------------------------------------------------


def _run_backfill_records(args: argparse.Namespace) -> dict:
    store, err = _open(args, "term.backfill-records")
    if err is not None:
        return err
    try:
        try:
            from trialerror.lexicon.backfill import backfill_records
        except ImportError:
            return ok_envelope(
                "term.backfill-records",
                result={"status": "unavailable", "reason": "trialerror.lexicon.backfill is not importable"},
            )
        family_map = json.loads(Path(args.family_map).read_text(encoding="utf-8")) if args.family_map else None
        result = backfill_records(
            store, register_keys=args.register_keys, family_map=family_map, by_launch=args.by_launch,
            config=_config(args),
        )
    except LexiconError as exc:
        return _lexicon_error_envelope("term.backfill-records", exc)
    except (ValidationError, XidTargetMissingError, StoreError) as exc:
        return _store_error_envelope("term.backfill-records", exc)
    except (OSError, json.JSONDecodeError) as exc:
        return error_envelope("term.backfill-records", "bad_family_map", str(exc))
    finally:
        store.close()
    return ok_envelope("term.backfill-records", result=result)


def _run_backfill_claims(args: argparse.Namespace) -> dict:
    store, err = _open(args, "term.backfill-claims")
    if err is not None:
        return err
    try:
        try:
            from trialerror.lexicon.backfill import backfill_claims
        except ImportError:
            return ok_envelope(
                "term.backfill-claims",
                result={"status": "unavailable", "reason": "trialerror.lexicon.backfill is not importable"},
            )
        lemma_map = json.loads(Path(args.lemma_map).read_text(encoding="utf-8")) if args.lemma_map else None
        result = backfill_claims(store, lemma_map=lemma_map, by_launch=args.by_launch, config=_config(args))
    except LexiconError as exc:
        return _lexicon_error_envelope("term.backfill-claims", exc)
    except (ValidationError, XidTargetMissingError, StoreError) as exc:
        return _store_error_envelope("term.backfill-claims", exc)
    except (OSError, json.JSONDecodeError) as exc:
        return error_envelope("term.backfill-claims", "bad_lemma_map", str(exc))
    finally:
        store.close()
    return ok_envelope("term.backfill-claims", result=result)


def _run_relink(args: argparse.Namespace) -> dict:
    store, err = _open(args, "term.relink")
    if err is not None:
        return err
    try:
        try:
            from trialerror.lexicon.backfill import relink_evidence_sources
        except ImportError:
            return ok_envelope(
                "term.relink",
                result={"status": "unavailable", "reason": "trialerror.lexicon.backfill is not importable"},
            )
        source_map = json.loads(Path(args.map_path).read_text(encoding="utf-8"))
        result = relink_evidence_sources(store, source_map=source_map, by_launch=args.by_launch)
    except LexiconError as exc:
        return _lexicon_error_envelope("term.relink", exc)
    except (ValidationError, XidTargetMissingError, StoreError) as exc:
        return _store_error_envelope("term.relink", exc)
    except (OSError, json.JSONDecodeError) as exc:
        return error_envelope("term.relink", "bad_map", str(exc))
    finally:
        store.close()
    return ok_envelope("term.relink", result=result)


# ---------------------------------------------------------------------------
# list / show / status / review -- read-only, own small SQL (see module
# docstring for why these do not route through lexicon.api).
# ---------------------------------------------------------------------------


def _relation_member_sense_ids(rel: Mapping[str, Any]) -> list[str]:
    """Mirrors ``lexicon.api._relation_member_sense_ids`` (private there,
    not exported) for the same read: a term-scoped relation carries its
    member senses in ``evidence`` JSON; a sense-to-sense relation IS its
    two endpoints."""
    raw = rel.get("evidence")
    if raw:
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, Mapping):
            ids = payload.get("sense_ids")
            if isinstance(ids, (list, tuple)) and ids:
                return [str(i) for i in ids]
    if rel.get("src_kind") == "sense" and rel.get("dst_kind") == "sense":
        return [str(rel["src_id"]), str(rel["dst_id"])]
    return []


def _run_list(args: argparse.Namespace) -> dict:
    store, err = _open(args, "term.list")
    if err is not None:
        return err
    try:
        sql = "SELECT * FROM term WHERE 1=1"
        params: list[Any] = []
        if args.state:
            sql += " AND status = ?"
            params.append(args.state)
        if args.granularity:
            sql += " AND granularity = ?"
            params.append(args.granularity)
        if args.q:
            sql += " AND lemma_norm LIKE ?"
            params.append(f"%{norm_lemma(args.q)}%")
        sql += " ORDER BY lemma_norm"
        rows = [dict(r) for r in store.knowledge.execute(sql, params).fetchall()]

        # Backlog item (c): TWO statements for the whole listing, whatever the
        # term count. This loop used to ask `senses_for_term` (one query) and,
        # for a term with a preferred sense, `get_sense` (one more) PER ROW --
        # the O(terms) shape the Lexicon panel was measured at 48.1 s for on
        # the live 7,260-term store. Only two facts are read out of those
        # rows, and each is one grouped question:
        #   * `sense_count` = len(senses_for_term(...)) with no status filter,
        #     which is exactly COUNT(*) over that term's `term_sense` rows;
        #   * `preferred_gloss` = the gloss of the sense `preferred_sense_id`
        #     names, or None when it names no row -- which is what a JOIN
        #     from `term` onto `term_sense` gives, absent key included.
        sense_counts = {
            r["term_id"]: r["n"]
            for r in store.knowledge.execute(
                "SELECT term_id, COUNT(*) AS n FROM term_sense GROUP BY term_id"
            ).fetchall()
        }
        preferred_glosses = {
            r["sense_id"]: r["gloss"]
            for r in store.knowledge.execute(
                "SELECT s.sense_id, s.gloss FROM term_sense s "
                "JOIN term t ON t.preferred_sense_id = s.sense_id"
            ).fetchall()
        }

        terms = []
        for row in rows:
            preferred = None
            if row.get("preferred_sense_id"):
                preferred = preferred_glosses.get(row["preferred_sense_id"])
            terms.append({
                "term_id": row["term_id"],
                "lemma": row["lemma"],
                "granularity": row["granularity"],
                "tags": json.loads(row["tags"]) if row.get("tags") else None,
                "status": row["status"],
                "preferred_gloss": preferred,
                "sense_count": sense_counts.get(row["term_id"], 0),
                "updated_ts": row["updated_ts"],
            })
    except sqlite3.OperationalError as exc:
        return error_envelope("term.list", "not_initialized", f"the term store is not available yet: {exc}")
    finally:
        store.close()
    return ok_envelope("term.list", result={"count": len(terms), "terms": terms})


def _sense_payload(store: Store, sense: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "sense_id": sense["sense_id"],
        "gloss": sense["gloss"],
        "disambiguator": sense["disambiguator"],
        "status": sense["status"],
        "origin_kind": sense["origin_kind"],
        "origin_ref": sense["origin_ref"],
        "procedure_version": sense["procedure_version"],
        "needs_review": lexicon_api.needs_review(sense),
        "review_after": sense["review_after"],
        "source_keys": lexicon_api.source_keys_for_sense(store, sense["sense_id"]),
        "evidence": lexicon_api.evidence_for_sense(store, sense["sense_id"]),
    }


def _run_show(args: argparse.Namespace) -> dict:
    store, err = _open(args, "term.show")
    if err is not None:
        return err
    try:
        term = lexicon_api.get_term(store, args.id)
        if term is not None:
            senses = [_sense_payload(store, s) for s in lexicon_api.senses_for_term(store, term["term_id"])]
            aliases = [
                dict(r) for r in store.knowledge.execute(
                    "SELECT alias, alias_norm, kind FROM term_alias WHERE term_id = ? ORDER BY created_ts, alias_id",
                    (term["term_id"],),
                ).fetchall()
            ]
            relations = lexicon_api.relations_for_term(store, term["term_id"])
            result = {"term": term, "senses": senses, "aliases": aliases, "relations": relations}
            return ok_envelope("term.show", result=result)

        sense = lexicon_api.get_sense(store, args.id)
        if sense is not None:
            parent = lexicon_api.get_term(store, sense["term_id"])
            result = {"sense": _sense_payload(store, sense), "term": parent}
            return ok_envelope("term.show", result=result)

        return error_envelope("term.show", "not_found", f"no such term or sense: {args.id!r}")
    except sqlite3.OperationalError as exc:
        return error_envelope("term.show", "not_initialized", f"the term store is not available yet: {exc}")
    finally:
        store.close()


def _run_status(args: argparse.Namespace) -> dict:
    store, err = _open(args, "term.status")
    if err is not None:
        return err
    try:
        conn = store.knowledge
        by_term_status = {
            r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM term GROUP BY status").fetchall()
        }
        by_sense_status = {
            r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM term_sense GROUP BY status").fetchall()
        }
        conflicts_pending = conn.execute(
            "SELECT COUNT(*) AS n FROM term_relation WHERE verb = 'conflicts_with' AND status = 'pending'"
        ).fetchone()["n"]
        duplicates_pending = conn.execute(
            "SELECT COUNT(*) AS n FROM term_relation WHERE verb = 'same_as' AND status = 'pending'"
        ).fetchone()["n"]
        stale = conn.execute(
            "SELECT COUNT(*) AS n FROM term_sense WHERE status = 'current' AND review_after IS NOT NULL AND review_after < ?",
            (now(),),
        ).fetchone()["n"]
        unlinked = conn.execute(
            "SELECT COUNT(DISTINCT source_key) AS n FROM term_sense_evidence "
            "WHERE retracted_ts IS NULL AND source_key NOT IN (SELECT source_id FROM source)"
        ).fetchone()["n"]
        result = {
            "terms_by_status": by_term_status,
            "senses_by_status": by_sense_status,
            "conflicts_pending": conflicts_pending,
            "duplicates_pending": duplicates_pending,
            "senses_needing_review": stale,
            "evidence_source_keys_unlinked": unlinked,
        }
    except sqlite3.OperationalError as exc:
        return error_envelope("term.status", "not_initialized", f"the term store is not available yet: {exc}")
    finally:
        store.close()
    return ok_envelope("term.status", result=result)


def _run_review(args: argparse.Namespace) -> dict:
    store, err = _open(args, "term.review")
    if err is not None:
        return err
    try:
        conn = store.knowledge
        result: dict[str, Any] = {}

        if args.kind in (None, "conflict"):
            rows = [
                dict(r) for r in conn.execute(
                    "SELECT * FROM term_relation WHERE verb = 'conflicts_with' AND status = 'pending' "
                    "ORDER BY marked_ts, rel_id"
                ).fetchall()
            ]
            result["conflicts"] = [
                {
                    "rel_id": r["rel_id"], "marked_ts": r["marked_ts"], "marked_by_kind": r["marked_by_kind"],
                    "member_sense_ids": _relation_member_sense_ids(r),
                }
                for r in rows
            ]

        if args.kind in (None, "duplicate"):
            rows = [
                dict(r) for r in conn.execute(
                    "SELECT * FROM term_relation WHERE verb = 'same_as' AND status = 'pending' "
                    "ORDER BY marked_ts, rel_id"
                ).fetchall()
            ]
            result["duplicates"] = [
                {
                    "rel_id": r["rel_id"], "marked_ts": r["marked_ts"], "marked_by_kind": r["marked_by_kind"],
                    "src": [r["src_kind"], r["src_id"]], "dst": [r["dst_kind"], r["dst_id"]],
                }
                for r in rows
            ]

        if args.kind in (None, "stale"):
            rows = [
                dict(r) for r in conn.execute(
                    "SELECT * FROM term_sense WHERE status = 'current' AND review_after IS NOT NULL "
                    "AND review_after < ? ORDER BY review_after, sense_id",
                    (now(),),
                ).fetchall()
            ]
            result["stale"] = [
                {
                    "sense_id": r["sense_id"], "term_id": r["term_id"], "origin_kind": r["origin_kind"],
                    "review_after": r["review_after"],
                }
                for r in rows
            ]
    except sqlite3.OperationalError as exc:
        return error_envelope("term.review", "not_initialized", f"the term store is not available yet: {exc}")
    finally:
        store.close()
    return ok_envelope("term.review", result=result)
