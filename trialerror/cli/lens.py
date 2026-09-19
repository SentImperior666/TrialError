"""``trialerror lens`` — AMENDMENT-3 ideation machinery, generalized (design
Section 5.2: "``lens`` | roster, stratify, assign, log | AMENDMENT-3
machinery generalized"; this build's brief additionally names ``export`` as
a convention example — implemented here as a fifth, separate action). Thin
CLI wrapper over ``trialerror.lens.*`` — all logic lives there; this module only
parses argv and shapes the AgentEnvelope (same split ``trialerror/cli/artifact.py``
documents for M10).

Flat, single-level subcommands throughout (matching ``trialerror/cli/budget.py``'s
own established convention, e.g. its ``pools`` action: "list pools, or
``--create`` a new one" — one action, a flag switches mode — rather than a
second nested subparser level, which no other group in this codebase uses):
``roster`` lists a round's roster by default, or adds one lens when
``--add`` is given alongside the lens fields.

Design Section 5.2 registration rule: "each CLI group lives in its own
module ``trialerror/cli/<group>.py``, auto-discovered at load — no
implementation lane ever edits a shared ``cli/__init__.py``." This file is
that drop-in; ``trialerror/cli/__init__.py`` is untouched by M13.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable, Sequence

from trialerror.jobs import ledger
from trialerror.lens.assign import list_assignments, run_assignment, slice_distances
from trialerror.lens.errors import LensError
from trialerror.lens.export import export_launch_bookable, lens_log
from trialerror.lens.ideas import IDEA_STATUSES, intake_records
from trialerror.lens.novelty import (
    DEFAULT_BATCH_FAIL_ON,
    DEFAULT_CORPUS_MODE,
    DEFAULT_JUDGED_SETS,
    DEFAULT_PARAPHRASE_BACKEND,
    PARAPHRASE_BACKENDS,
    EXTERNAL_QUERY_MODES,
    JUDGED_SAMPLE_FRACTION,
    NoveltyError,
    PLANTS_PER_BATCH,
    QueryEmbedBackendUnrunnableError,
    SECOND_JUDGE_FRACTION,
    baseline_distribution,
    build_calibration_batch,
    build_judged_batch,
    fill_idea_vector_cache,
    load_external_plants,
    load_label_vocabularies,
    normalize_judged_sets,
    record_calibration,
    record_novelty_verdicts,
    round_dir,
    run_mechanical_screen,
)
from trialerror.lens.roster import SEATS, add_lens, list_roster
from trialerror.lens.stratify import score_candidates, stratify
from trialerror.lens.vectors import fetch_doc_vectors
from trialerror.stores.errors import StoreError
from trialerror.stores.store import Store, open_store
from trialerror.util.config import ConfigError, find_program_root, load_config
from trialerror.verify.errors import VerifyError
from trialerror.retrieve import engine as retrieve_engine
from trialerror.util.envelope import error_envelope, next_action, ok_envelope

#: The R5 providers ``lens screen --external-provider`` can build. Both are
#: in-tree adapters over landed modules (``trialerror.arxiv_index``,
#: ``trialerror.litapi``) and both read their own ``[litapi...]`` config, so
#: the screen's external half is reachable from the CLI without this module
#: holding a connection or a key. ``none`` is the default because an egress
#: is an opt-in.
EXTERNAL_PROVIDERS: tuple[str, ...] = ("none", "arxiv-index", "litapi")

GROUP_NAME = "lens"
HELP = (
    "Ideation lens tooling: roster, stratify, assign, log, intake, export (AMENDMENT-3 generalized), "
    "screen (the novelty screen: mechanical per batch, judged at round end), and recheck (enqueue the "
    "scheduled convergent-discovery pass over a closed round)."
)


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    # FX-12 (trialerror/cli/__init__.py TRIALERROR-DEV-NOTE): default=SUPPRESS so an
    # unset value here never overwrites the global --program-root the
    # top-level parser resolved.
    parser.add_argument(
        "--program-root", default=argparse.SUPPRESS, help="program scaffold root (default: discover trialerror.toml upward from CWD)"
    )
    parser.set_defaults(handler=_run_no_action)
    sub = parser.add_subparsers(dest="action", metavar="<action>")

    roster = sub.add_parser("roster", help="list a round's roster, or --add one lens to it")
    roster.add_argument("--round-id", required=True)
    roster.add_argument("--add", action="store_true")
    roster.add_argument("--lens-name", default=None)
    roster.add_argument("--vantage", default=None)
    roster.add_argument("--model-class", default=None)
    roster.add_argument("--seat", default="standard", choices=list(SEATS))
    roster.add_argument(
        "--recipe-card", action="append", default=None, dest="recipe_cards", metavar="CARD",
        help="one card name in this lens's block; repeat IN SEEDED ORDER (order is preserved). A control seat takes none.",
    )
    roster.set_defaults(handler=_run_roster)

    stratify_p = sub.add_parser("stratify", help="dry-run: score + tercile-cut a candidate pool (no write)")
    _add_stratify_args(stratify_p)
    stratify_p.set_defaults(handler=_run_stratify)

    assign = sub.add_parser("assign", help="stratify + seeded quota draw + write lens_assignment rows")
    _add_stratify_args(assign)
    assign.add_argument("--round-id", required=True)
    assign.add_argument("--roster-id", action="append", default=None, dest="roster_ids", metavar="ROSTER_ID", help="restrict to these lenses (default: every lens in the round's roster)")
    assign.add_argument("--slices-per-lens", type=int, required=True)
    assign.add_argument("--seed", required=True)
    assign.add_argument("--weights", default="40,40,20", help="comma-separated near,moderate,far percentages")
    assign.add_argument("--far-floor", type=int, default=2)
    assign.add_argument(
        "--arm-per-lens", action="store_true",
        help="split the ROSTER across the arms (one arm per lens, whole slice from it) instead of "
             "splitting each lens's slice; --far-floor then counts far LENSES, not far slices",
    )
    assign.add_argument("--inter-cluster-mandate", action="store_true")
    assign.add_argument("--home-cluster", default=None)
    assign.add_argument("--launch-id", default=None)
    assign.set_defaults(handler=_run_assign)

    log = sub.add_parser("log", help="list logged lens_assignment rows for a round")
    log.add_argument("--round-id", required=True)
    log.set_defaults(handler=_run_log)

    distances = sub.add_parser(
        "slice-distances",
        help="READ-ONLY: per lens, which home its slice sits nearest and which slice document is "
             "farthest from that home -- the shape a pre-registered rule of the form 'the slice "
             "document farthest from the home medoid nearest to the slice' is evaluated from",
    )
    distances.add_argument("--round-id", required=True)
    distances.add_argument(
        "--home", action="append", default=[], dest="home_doc_ids", metavar="DOC_ID", required=True,
        help="a home document; repeat, or pass one comma-separated list. The nearest home is the one "
             "whose MEAN distance to the lens's slice is smallest, ties to the lower id",
    )
    distances.add_argument(
        "--lens", action="append", default=None, dest="lens_names", metavar="NAME",
        help="restrict to these lenses by name (default: every lens with an assignment in the round)",
    )
    distances.add_argument(
        "--model-key", required=True, dest="model_key",
        help="embedding model_key (matches vec_chunks__<model_key>); document vectors are pooled from "
             "its chunk vectors, the same way stratify cut the arms",
    )
    distances.set_defaults(handler=_run_slice_distances)

    intake = sub.add_parser(
        "intake",
        help="write a lens's returned records into the round as idea rows -- validated against the record "
             "schema, all of them or none of them",
    )
    intake.add_argument("--round-id", required=True)
    intake.add_argument("--records", required=True, metavar="FILE", help="JSON list of records (or an object with a 'records' list)")
    intake.add_argument("--author-launch", required=True, dest="author_launch", help="the lens's own launch id")
    intake.add_argument(
        "--assign-id", action="append", default=None, dest="assign_ids", metavar="ASSIGN_ID",
        help="the assignment row(s) these records were written under; repeat for a multi-slice lens",
    )
    intake.add_argument("--arm", default=None, help="the arm these records were written under")
    intake.add_argument(
        "--no-embed", action="store_true", dest="no_embed",
        help="do not fill the per-model idea-vector cache at intake. By default every record's "
             "statement is embedded here -- the first moment its vector can exist -- so the round's "
             "first screen does not pay for the lot inside itself. The screen fills the cache on its "
             "own read either way, so this flag costs time later rather than anything else",
    )
    intake.add_argument(
        "--status", default=None, choices=list(IDEA_STATUSES),
        help="the disposition every record in this file takes unless it names its own (default: raw). "
             "'archived' is the archive intake: prior rounds' candidates and request rows, written in so "
             "this round can be judged against them as reference set R2. An archived row is never "
             "consolidated, is never the survivor of a near-duplicate merge, and a live record that lands "
             "on one is FLAGGED (archive_hit) rather than folded into it",
    )
    intake.set_defaults(handler=_run_intake)

    export = sub.add_parser("export", help="launch-bookable rows for trialerror.budget.book_launch")
    export.add_argument("--round-id", required=True)
    export.set_defaults(handler=_run_export)

    screen = sub.add_parser(
        "screen",
        help="the novelty screen: --mechanical (no LLM, per batch), --judged-prep (build the judge's "
             "envelopes), --record-verdicts (take the judge's labels back)",
    )
    screen.add_argument("--round-id", required=True)
    screen.add_argument(
        "--mechanical", action="store_true",
        help="run phase 3a: merge near-duplicates, flag known mechanics, record distances, write dossiers",
    )
    screen.add_argument(
        "--judged-prep", action="store_true", dest="judged_prep",
        help="run phase 3b preparation over the dossiers already on file: judged scope, plants, envelopes",
    )
    screen.add_argument(
        "--record-verdicts", default=None, dest="record_verdicts", metavar="FILE",
        help="a JSON file of the judge's discrete labels, keyed by subject id; scores the plants, writes "
             "verdict rows and (unless a plant was missed) consolidates every survivor -- the judged ones "
             "under their labels, the rest under the mechanical no-close-neighbour/unjudged pair",
    )
    screen.add_argument(
        "--calibration", action="store_true",
        help="build a judged batch of PLANTS ONLY, before this round has a single record: the same "
             "envelopes, the same masking and the same per-kind scoring, over the plants --plants-file "
             "declares. Two judges label it and --record-calibration writes the card. Needs --seed and "
             "--plants-file; consolidates nothing",
    )
    screen.add_argument(
        "--baseline", action="store_true",
        help="READ-ONLY: every record of this round against the corpus, as a distribution of "
             "record->corpus NEAREST-NEIGHBOUR cosines (min/p50/p90/p95/max, the method named in the "
             "output, plus the nearest chunk and document per record). Unlike the calibration card's "
             "own baseline this one has no similarity floor and needs no dossiers, so it can be read "
             "off a round that is already ARCHIVED -- which is the round a later one wants the number "
             "from. Writes no verdict, dossier or idea row; it may fill the per-model idea-vector "
             "cache. Pair it with --corpus-mode vector for the true nearest neighbour",
    )
    screen.add_argument(
        "--status", default=None,
        help="with --baseline: restrict to records in this state (e.g. 'archived' for a closed round). "
             "Omitted, every record of the round is read",
    )
    screen.add_argument(
        "--where", action="append", default=None, dest="where", metavar="provenance.KEY=VALUE",
        help="with --baseline: keep only records whose resolved KEY equals VALUE; repeat to AND terms. "
             "Spelled provenance.<key> because a field lives in a schema column on a row written since "
             "the AIIF migration and inside the provenance JSON on one written before it, and this "
             "filter reads both the same way. A list-valued field matches if VALUE is one of its items",
    )
    screen.add_argument(
        "--record-calibration", action="store_true", dest="record_calibration",
        help="score two judges' sheets against a calibration batch: catch rate per kind and per set, "
             "Cohen's kappa per declared set, Pearson r between each rated pair's embedding cosine and its "
             "human rating, and the baseline cosine distribution. Needs --judge-sheet-a and --judge-sheet-b",
    )
    screen.add_argument(
        "--judge-sheet-a", default=None, dest="judge_sheet_a", metavar="FILE",
        help="with --record-calibration: the FIRST judge's labels, keyed by subject id (masked or real)",
    )
    screen.add_argument(
        "--judge-sheet-b", default=None, dest="judge_sheet_b", metavar="FILE",
        help="with --record-calibration: the SECOND judge's labels, the other half of the kappa",
    )
    screen.add_argument(
        "--judge-launch-a", default=None, dest="judge_launch_a", metavar="LAUNCH_ID",
        help="with --record-calibration: the launch the first judge ran under (default: --launch-id). A "
             "verdict row records WHO issued it, and two judges are two issuers",
    )
    screen.add_argument(
        "--judge-launch-b", default=None, dest="judge_launch_b", metavar="LAUNCH_ID",
        help="the same for the second judge",
    )
    screen.add_argument(
        "--pair-ratings", default=None, dest="pair_ratings", metavar="FILE",
        help='with --record-calibration: [{"a": id, "b": id, "human": 0..1}] -- a person\'s own '
             "similarity rating per pair, against which the embedding cosine is correlated (Pearson r). "
             "Omitted, the card says r is absent rather than assuming it",
    )
    screen.add_argument("--launch-id", default=None, dest="launch_id")
    screen.add_argument(
        "--judged-sets", default=None, dest="judged_sets", metavar="R2,R3,R4",
        help="which reference sets this round's judge is shown and labels against (default: "
             f"{','.join(DEFAULT_JUDGED_SETS)}, or [lens.novelty] judged_sets in trialerror.toml; this flag "
             "overrides the config). R2 is the archive of idea rows, R3 the inventory, R4 the corpus -- R5 "
             "is evidence for the R4 label, not a labelled set of its own. An undeclared set is ABSENT from "
             "every envelope, not empty, and no verdict row is written for it",
    )
    screen.add_argument(
        "--labels-file", default=None, dest="labels_file", metavar="FILE",
        help="the round's OWN label vocabularies and their canonical mapping onto the design's fixed ones, "
             'as {"R4": {"labels": [...], "canonical": {...}}, "extra": {"seed": [...]}, "unscreenable": '
             '"..."}. The judge is shown the round\'s spellings; each verdict row stores the round label '
             "and the canonical label beside it, so every downstream count keeps working. The mapping must "
             "be total over the round's labels. [lens.novelty] labels_file sits behind this flag; the flag "
             "wins. Pass it to --record-verdicts too and it must hash-match the batch's",
    )
    screen.add_argument(
        "--plants-file", default=None, dest="plants_file", metavar="FILE",
        help="the plants this ROUND seeds, as a JSON list of {plant_id, kind, statement, expected_labels "
             "{SET: [label, ...]}, donor_ref?, source_ref?, requirements?, home_mechanic?, probe?, "
             "class?, batch?}. KIND is how the plant was BUILT and is one of area, paraphrase, "
             "inventory, custom -- nothing else may be spelled there. A round's own class for a plant "
             "(a present/adjacent/absent battery, say) goes in 'class': free text up to 40 characters "
             "that groups the calibration card's by_class table and is never shown to a judge. 'batch' "
             "seeds a plant into one judged batch only. They are shuffled in through the same envelope "
             "builder, wear a donor record's missing fields and carry masked ids, exactly like the "
             "harness's own. Pair it with --plants 0 to seed ONLY the round's. [lens.novelty] plants_file "
             "sits behind the flag",
    )
    screen.add_argument(
        "--batch-fail-on", default=None, dest="batch_fail_on", metavar="KIND[,KIND]",
        help=f"which plant kinds' misses FAIL the batch (default: {','.join(DEFAULT_BATCH_FAIL_ON)} -- the "
             "rule the design stakes a batch on). A miss on any other kind is reported and counted, never "
             "hidden, and does not fail. [lens.novelty] batch_fail_on sits behind the flag",
    )
    screen.add_argument(
        "--reembed-archive", action="store_true", dest="reembed_archive",
        help="re-embed every R2 archive row instead of reading the per-model idea-vector cache. The cache "
             "is keyed by the statement's hash, so a changed statement is re-embedded anyway; this flag is "
             "for the case the key cannot see -- a backend whose weights or pooling changed under an "
             "unchanged model key",
    )
    screen.add_argument("--seed", default=None, help="required by --judged-prep: the round's own seed")
    screen.add_argument(
        "--batch-id", default=None, dest="batch_id",
        help="name this batch. It is also what a plants file's optional 'batch' key is matched against: "
             "a plant that declares a batch rides in THAT batch only, one that declares none rides in "
             "every batch (today's behaviour, and what an unchanged file keeps doing). A --batch-id no "
             "plant declares injects none of the batched ones and says so in the batch's warnings",
    )
    screen.add_argument(
        "--judge-envelopes-out", default=None, dest="judge_envelopes_out", metavar="DIR",
        help="with --judged-prep: write the MASKED judge views (one JSON per envelope, ids J-<n>) into "
             "this directory. Build the judge's prompt from these -- an envelope's own subject_id is "
             "PLANT-inventory-0, so a prompt builder that copies the batch hands the judge the answer key",
    )
    screen.add_argument("--idea-id", action="append", default=None, dest="idea_ids", metavar="IDEA_ID")
    screen.add_argument("--external-query-mode", default="none", dest="external_query_mode", choices=list(EXTERNAL_QUERY_MODES))
    screen.add_argument(
        "--external-provider", default="none", dest="external_provider", choices=list(EXTERNAL_PROVIDERS),
        help="which R5 index the chosen --external-query-mode issues its query to: the local all-arXiv "
             "semantic index, or the redundant literature-metadata client. Both read their own "
             "[litapi...] config; 'none' (default) issues nothing",
    )
    screen.add_argument("--alarms", default=None, help="JSON object of pre-registered collapse alarms; omit for descriptive-only")
    screen.add_argument("--sample-fraction", type=float, default=JUDGED_SAMPLE_FRACTION, dest="sample_fraction")
    screen.add_argument("--second-judge-fraction", type=float, default=SECOND_JUDGE_FRACTION, dest="second_judge_fraction")
    screen.add_argument("--plants", type=int, default=PLANTS_PER_BATCH, help="plants per kind in a judged batch")
    screen.add_argument(
        "--paraphrase-backend", default=DEFAULT_PARAPHRASE_BACKEND, dest="paraphrase_backend",
        choices=list(PARAPHRASE_BACKENDS),
        help="how a paraphrase plant is written (default: deterministic -- seeded, lossless "
             "transformations of the donor statement). 'llm' is declared and refused: a battery whose "
             "plants a model rewrote would make the audit depend on the class of system it audits",
    )
    screen.add_argument("--prereg-id", default=None, dest="prereg_id")
    screen.add_argument(
        "--executed-procedure", default=None, dest="executed_procedure", metavar="NAME",
        help="with --prereg-id: the procedure the round actually ran, so prereg_compliant can be stamped by "
             "recomputing the hash -- on --record-verdicts and --record-calibration alike. Omit it and the "
             "column stays NULL and the envelope says why",
    )
    screen.add_argument(
        "--executed-procedure-file", default=None, dest="executed_procedure_file", metavar="FILE",
        help="the same, read BYTE-EXACT from a file. Use this rather than --executed-procedure "
             "\"$(cat file)\": a shell strips the file's trailing newline, the recomputed hash differs "
             "from the committed one, and a procedure that WAS followed is stamped non-compliant",
    )
    screen.add_argument(
        "--executed-params", default=None, dest="executed_params", metavar="JSON",
        help="JSON object of the parameters the round actually ran, the other half of --executed-procedure",
    )
    screen.add_argument(
        "--supersede", action="store_true",
        help="allow a second recording for subjects that already carry a novelty-v2 verdict (or, with "
             "--record-calibration, a novelty-v2-calibration one); the new rows name the rows they "
             "supersede. Without it a re-scoring is refused (one submission per judge)",
    )
    screen.add_argument("--second-judge-file", default=None, dest="second_judge_file", metavar="FILE")
    screen.add_argument("--rescreen", action="store_true", help="re-screen ideas that already have a dossier")
    screen.add_argument(
        "--corpus-mode", default=DEFAULT_CORPUS_MODE, dest="corpus_mode", choices=["auto", "fts", "vector", "hybrid"],
        help=(
            "which retrieval tier builds the R4 prior-art bundle (default: "
            f"{DEFAULT_CORPUS_MODE}). 'fts' restricts R4 to the full-text tier and records itself in "
            "every dossier it produces -- use it deliberately, not to get past a refusal: it does not "
            "remove the screen's own need to embed each record's statement"
        ),
    )
    screen.set_defaults(handler=_run_screen)

    recheck = sub.add_parser(
        "recheck",
        help="enqueue the scheduled convergent-discovery re-check for one round: every idea row against the "
             "corpus as it stands now (and, under a query mode, the external index), writing convergent_with "
             "links and never re-scoring",
    )
    recheck.add_argument("--round-id", required=True)
    recheck.add_argument("--job-id", default=None, dest="job_id", help="name the job row (default: a fresh JOB- id)")
    recheck.add_argument(
        "--external-query-mode", default="none", dest="external_query_mode", choices=list(EXTERNAL_QUERY_MODES)
    )
    recheck.add_argument(
        "--external-provider", default="none", dest="external_provider", choices=list(EXTERNAL_PROVIDERS),
        help="which R5 index the chosen mode queries; the worker builds it through the same builder the "
             "screen uses",
    )
    recheck.add_argument(
        "--status", action="append", default=None, dest="statuses", metavar="STATUS",
        help="restrict to these idea statuses (default: every status except raw -- merged and eliminated rows "
             "stay in the reference sets and stay worth re-checking)",
    )
    recheck.add_argument(
        "--idea-id", action="append", default=None, dest="idea_ids", metavar="IDEA_ID",
        help="name records outright; the status filter still applies to them, because naming a record does "
             "not screen it",
    )
    recheck.add_argument(
        "--allow-unscreened", action="store_true", dest="allow_unscreened",
        help="re-check records with no novelty dossier on file, lifting the status filter too. With nothing "
             "recorded to measure NEW against, every neighbour is reported as a convergent discovery -- which "
             "is the screen run late under another name, so the reading is taken deliberately or not at all",
    )
    recheck.add_argument("--launch-id", default=None, dest="launch_id", help="carried into the R5 egress audit line")
    recheck.add_argument("--corpus-k", type=int, default=None, dest="corpus_k")
    recheck.add_argument("--external-k", type=int, default=None, dest="external_k")
    recheck.set_defaults(handler=_run_recheck)

    return parser


def _add_stratify_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--model-key", required=True, help="embedding model_key (matches vec_chunks__<model_key>)")
    p.add_argument("--home", action="append", default=[], dest="home_doc_ids", metavar="DOC_ID", required=True)
    p.add_argument("--candidate", action="append", default=[], dest="candidate_doc_ids", metavar="DOC_ID", required=True)
    p.add_argument("--cluster-of", default=None, help='JSON object string: {"doc_id": "cluster_id", ...}')


def _open_store(args: argparse.Namespace) -> tuple[Store | None, dict | None]:
    root = getattr(args, "program_root", None) or find_program_root()
    if root is None:
        return None, error_envelope(
            "lens", "program_root_not_found",
            "no trialerror.toml found upward from CWD; pass --program-root",
            next_actions=[
                next_action(["trialerror", "program", "init", "<name>", "--dir", "."], "scaffold a program in this directory first")
            ],
        )
    return open_store(Path(root)), None


#: The ``trialerror.toml`` table a round's judged-screen instrument is
#: declared in, when it is not declared on the command line.
NOVELTY_CONFIG_TABLE = "lens.novelty"


def _novelty_config(program_root: Path) -> dict:
    """``[lens.novelty]`` from the program's ``trialerror.toml``, or ``{}``.

    Same "read generically, tolerate absence" posture every other config
    consumer in this codebase states (``trialerror.cli.budget._load_policy``
    is the one it is modelled on): a program with no ``trialerror.toml``, or
    one whose config says nothing about the screen, gets the documented
    defaults rather than a CLI failure. An UNPARSEABLE config still raises
    through ``open_store``, which is where that stop condition belongs."""
    try:
        raw = load_config(program_root / "trialerror.toml").raw
    except ConfigError:
        return {}
    table = raw.get("lens")
    if not isinstance(table, dict):
        return {}
    novelty = table.get("novelty")
    return dict(novelty) if isinstance(novelty, dict) else {}


#: Which batch file each RECORDING phase reads when ``--batch-id`` is not
#: given. Named once because two places now have to agree on it: the phase
#: itself, and :func:`_judged_sets_from_batch`, which reads the same file
#: before the labels file is resolved.
RECORDING_BATCH_STEMS: tuple[tuple[str, str, str], ...] = (
    ("record_calibration", "--record-calibration", "calibration-0"),
    ("record_verdicts", "--record-verdicts", "judged-0"),
)


def recording_batch_file(args: argparse.Namespace, base: Path, stem: str) -> Path:
    """The batch file a recording phase reads: ``--batch-id`` if given, the
    phase's own default otherwise."""
    return base / "judged" / f"{getattr(args, 'batch_id', None) or stem}.json"


def _executed_procedure(args: argparse.Namespace) -> str | None:
    """The procedure text a recording will recompute its prereg hash over.

    Read as bytes-on-disk and decoded without any stripping when a FILE was
    named: the hash is over the file exactly as it was committed, and
    ``--executed-procedure "$(cat file)"`` is not -- a shell strips the
    trailing newline and a procedure that was followed comes back
    non-compliant.

    One function rather than two copies (lane FB-8b item 5): both
    ``--record-verdicts`` and ``--record-calibration`` stamp compliance now,
    and a second inlined copy of this is how one of them ends up hashing a
    stripped string."""
    if getattr(args, "executed_procedure_file", None):
        return Path(args.executed_procedure_file).read_text(encoding="utf-8")
    return args.executed_procedure


def _judged_sets_from_batch(
    args: argparse.Namespace, base: Path, declared_by_command: Any
) -> tuple[dict | None, list[str] | None]:
    """``(refusal, judged_sets)`` for a run that RECORDS against a batch.

    The batch file is the authority, and that is not a preference between
    two equal sources: the declared sets are what the judge was actually
    shown, they are written into the batch when it is built, and the labels
    file's hash is computed OVER them. Resolving a labels file against the
    CLI's default while recording an R2,R4 batch therefore refused a file
    that matched the batch exactly -- and the only way through was to repeat
    ``--judged-sets R2,R4`` on every recording command, which is a flag
    whose only job is to restate what the file already says.

    ``--judged-sets`` (or ``[lens.novelty] judged_sets``) is still read, and
    a disagreement is refused BY NAME rather than silently preferring one of
    the two: a round that thinks it is recording R3 answers against an R2
    batch has a problem no default can fix.

    ``(None, None)`` when this run records nothing, when no batch is on file
    (the phase itself refuses that, by name and with the path) or when the
    file cannot be read -- every one of those is a condition the phase
    reports better than a pre-check could."""
    resolved: tuple[str, ...] | None = None
    named_by: str = ""
    for attr, flag, stem in RECORDING_BATCH_STEMS:
        if not getattr(args, attr, None):
            continue
        path = recording_batch_file(args, base, stem)
        if not path.is_file():
            continue
        try:
            declared_then = normalize_judged_sets(
                json.loads(path.read_text(encoding="utf-8")).get("judged_sets")
            )
        except (OSError, ValueError, NoveltyError, AttributeError):
            continue
        if resolved is not None and declared_then != resolved:
            return (
                error_envelope(
                    "lens screen", "judged_sets_disagree",
                    f"{named_by} reads a batch built against {list(resolved)!r} and {flag} one built "
                    f"against {list(declared_then)!r}. One invocation cannot record both against one "
                    "labels file, whose hash is computed over the declared sets. Run them separately",
                ),
                None,
            )
        resolved, named_by = declared_then, flag
    if resolved is None:
        return None, None
    if declared_by_command is not None:
        try:
            declared_now = normalize_judged_sets(declared_by_command)
        except NoveltyError:
            return None, None  # the refusal the flag deserves comes from the phase
        if declared_now != resolved:
            return (
                error_envelope(
                    "lens screen", "judged_sets_disagree",
                    f"this run declares reference sets {list(declared_now)!r} but the batch {named_by} "
                    f"reads was built against {list(resolved)!r}, which is what its judge was actually "
                    "shown. Record against the batch -- drop --judged-sets (the batch is read for them "
                    f"now) or pass its own sets (--judged-sets {','.join(resolved)}) -- or re-prep the "
                    "batch under the new sets",
                ),
                None,
            )
    return None, list(resolved)


def _configured_plants(args: argparse.Namespace) -> Any:
    """``[lens.novelty] plants_file``, read before the store is open so
    ``--calibration``'s "the plants are the whole batch" refusal can be
    answered from the arguments and the config alone."""
    root = getattr(args, "program_root", None) or find_program_root()
    return _novelty_config(Path(root)).get("plants_file") if root else None


def _config_path(program_root: Path, value: Any) -> Path | None:
    """A file named on the command line or in ``[lens.novelty]``, resolved.

    An absolute path stands; a relative one is read against the PROGRAM
    ROOT, not the current directory, because a config row is a property of
    the program and a round run from two different shells must read the same
    file."""
    if not value:
        return None
    path = Path(str(value))
    return path if path.is_absolute() else program_root / path


def _run_no_action(args: argparse.Namespace) -> dict:
    return error_envelope(
        "lens", "no_action", "specify an action: roster|stratify|assign|log|intake|export|screen|recheck",
        next_actions=[next_action(["trialerror", "lens", "--help"], "list lens actions")],
    )


def _run_roster(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        if args.add:
            missing = [f for f in ("lens_name", "vantage", "model_class") if getattr(args, f) is None]
            if missing:
                return error_envelope(
                    "lens roster", "missing_fields",
                    f"--add requires {['--' + m.replace('_', '-') for m in missing]!r}",
                )
            row = add_lens(
                store, round_id=args.round_id, lens_name=args.lens_name, vantage=args.vantage,
                model_class=args.model_class, seat=args.seat,
                recipe_cards=getattr(args, "recipe_cards", None),
            )
            return ok_envelope(
                "lens roster", result=row,
                next_actions=[next_action(["trialerror", "lens", "roster", "--round-id", args.round_id], "see the round's roster")],
            )
        rows = list_roster(store, round_id=args.round_id)
        return ok_envelope("lens roster", result={"roster": rows, "count": len(rows)})
    except (StoreError, ValueError) as exc:
        return error_envelope("lens roster", "roster_refused", str(exc))
    finally:
        store.close()


def _fetch_candidates_and_home(store: Store, args: argparse.Namespace):
    home = fetch_doc_vectors(store, model_key=args.model_key, doc_ids=args.home_doc_ids)
    candidates = fetch_doc_vectors(store, model_key=args.model_key, doc_ids=args.candidate_doc_ids)
    cluster_of = json.loads(args.cluster_of) if getattr(args, "cluster_of", None) else None
    return home, candidates, cluster_of


def _run_stratify(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        home, candidates, cluster_of = _fetch_candidates_and_home(store, args)
        scores = score_candidates(candidates, home)
        stratified = stratify(scores, cluster_of=cluster_of)
    except (LensError, json.JSONDecodeError) as exc:
        return error_envelope("lens stratify", "stratify_refused", str(exc))
    finally:
        store.close()
    return ok_envelope(
        "lens stratify",
        result={"candidates": [sc.to_dict() for sc in stratified], "count": len(stratified)},
    )


def _run_assign(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        roster = list_roster(store, round_id=args.round_id)
        if args.roster_ids:
            wanted = set(args.roster_ids)
            roster = [r for r in roster if r["roster_id"] in wanted]
        if not roster:
            return error_envelope(
                "lens assign", "empty_roster",
                f"round_id={args.round_id!r} has no matching roster rows to assign",
                next_actions=[next_action(["trialerror", "lens", "roster", "--add"], "add a lens to this round first")],
            )
        cluster_of = json.loads(args.cluster_of) if args.cluster_of else None
        weights = tuple(int(w) for w in args.weights.split(","))
        result = run_assignment(
            store,
            round_id=args.round_id,
            model_key=args.model_key,
            home_doc_ids=args.home_doc_ids,
            candidate_doc_ids=args.candidate_doc_ids,
            lenses=[
                {
                    "roster_id": r["roster_id"],
                    "seat": r.get("seat"),
                    "recipe_cards": r.get("recipe_cards"),
                }
                for r in roster
            ],
            slices_per_lens=args.slices_per_lens,
            seed=args.seed,
            weights=weights,
            far_floor=args.far_floor,
            arm_mode="per_lens" if getattr(args, "arm_per_lens", False) else "per_slice",
            inter_cluster_mandate=args.inter_cluster_mandate,
            cluster_of=cluster_of,
            home_cluster=args.home_cluster,
            launch_id=args.launch_id,
        )
    except LensError as exc:
        return error_envelope("lens assign", "assign_refused", str(exc))
    except (StoreError, ValueError, json.JSONDecodeError) as exc:
        return error_envelope("lens assign", "assign_error", str(exc))
    finally:
        store.close()
    return ok_envelope(
        "lens assign",
        result={
            "rows": result["rows"],
            "count": len(result["rows"]),
            "arm_mode": result["plan"]["arm_mode"],
            "roster_quota": result["plan"]["roster_quota"],
        },
        next_actions=[next_action(["trialerror", "lens", "log", "--round-id", args.round_id], "see the logged assignment")],
    )


def _run_slice_distances(args: argparse.Namespace) -> dict:
    """Lane FB-7 item 7. Reads; writes nothing.

    ``--home`` accepts repetition AND one comma-separated value, because a
    rule written down as "home = DOC-a,DOC-b" should be typeable as it was
    written. Duplicates collapse; order does not matter, since the nearest
    home is chosen by distance with an id tie-break."""
    homes: list[str] = []
    for raw in getattr(args, "home_doc_ids", None) or []:
        homes.extend(part.strip() for part in str(raw).split(",") if part.strip())
    if not homes:
        return error_envelope(
            "lens slice-distances", "no_home",
            "--home names the home set the rule is defined against; pass at least one document id",
        )
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        result = slice_distances(
            store,
            round_id=args.round_id,
            home_doc_ids=homes,
            model_key=args.model_key,
            lens_names=getattr(args, "lens_names", None),
        )
    except LensError as exc:
        return error_envelope("lens slice-distances", "slice_distances_refused", str(exc))
    finally:
        store.close()
    next_actions = []
    if result["unvectorized"]:
        next_actions.append(next_action(
            ["trialerror", "ingest", "embed"],
            "embed the documents that have no vector under this model_key before reading the rule off this",
        ))
    return ok_envelope("lens slice-distances", result=result, next_actions=next_actions)


def _run_log(args: argparse.Namespace) -> dict:
    """The round's assignment rows AND its per-lens reconciliation.

    ``rows``/``n_lenses``/``offenders`` are what the gate suite's
    ``lens_log_reconciled`` check consumes: without them the check had only
    a row count to read, found no offender in it, and PASSED a round where
    one lens of three had posted. ``assignments``/``count`` stay for every
    caller that was reading the raw rows."""
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        rows = list_assignments(store, round_id=args.round_id)
        log = lens_log(store, round_id=args.round_id)
    finally:
        store.close()
    result = {"assignments": rows, "count": len(rows), **log}
    next_actions = []
    if log["offenders"]:
        next_actions.append(next_action(
            ["trialerror", "lens", "export", "--round-id", args.round_id],
            "re-book or chase the lenses that never posted before gating this round",
        ))
    return ok_envelope("lens log", result=result, next_actions=next_actions)


def _run_intake(args: argparse.Namespace) -> dict:
    """One record per idea row, or nothing at all.

    The gap this closes: nothing told the intake caller what a record must
    carry. The judge envelope reads ``provenance.docs`` and the screen's
    distribution card reads the two-axis operation, while the only feedback
    a malformed record produced was a bare sqlite "type 'list' is not
    supported" from a ``requirements`` list. Every refusal here names the
    record's position and the field."""
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        records = json.loads(Path(args.records).read_text(encoding="utf-8"))
        rows = intake_records(
            store, round_id=args.round_id, records=records, author_launch=args.author_launch,
            assign_ids=args.assign_ids, arm=args.arm, status=args.status,
        )
        # Lane FB-7 item 8a: fill the per-model idea-vector cache NOW, which
        # is the first moment these vectors can exist and the moment the
        # operator is sitting here. The screen's own fill stays as the
        # fallback, so this is an optimisation and is treated as one: a
        # parked or absent backend warns and the records still land.
        embedded = (
            {"skipped": "--no-embed"} if getattr(args, "no_embed", False)
            else fill_idea_vector_cache(store, idea_ids=[row["idea_id"] for row in rows])
        )
    except (ValueError, json.JSONDecodeError) as exc:
        return error_envelope(
            "lens intake", "record_refused", str(exc),
            details={"records_file": str(args.records), "written": 0},
            next_actions=[next_action(
                ["trialerror", "lens", "intake", "--round-id", args.round_id, "--records", str(args.records),
                 "--author-launch", args.author_launch],
                "fix the named record and re-run the whole file",
            )],
        )
    except (StoreError, OSError) as exc:
        return error_envelope("lens intake", "intake_error", f"{type(exc).__name__}: {exc}")
    finally:
        store.close()
    return ok_envelope(
        "lens intake",
        result={
            "round_id": args.round_id,
            "idea_ids": [row["idea_id"] for row in rows],
            "count": len(rows),
            "author_launch": args.author_launch,
            "status": args.status or "raw",
            "idea_vectors": embedded,
        },
        next_actions=[next_action(
            ["trialerror", "lens", "screen", "--round-id", args.round_id, "--mechanical"],
            "run the mechanical screen once every lens has been intaken",
        )],
    )


def _run_export(args: argparse.Namespace) -> dict:
    store, err = _open_store(args)
    if err is not None:
        return err
    try:
        rows = export_launch_bookable(store, round_id=args.round_id)
    finally:
        store.close()
    return ok_envelope(
        "lens export", result={"bookable": rows, "count": len(rows)},
        next_actions=[next_action(["trialerror", "budget", "book"], "book each row via trialerror.budget.book_launch")],
    )


def _load_dossiers(base) -> dict:
    """Every dossier on file for a round, read back off disk.

    ``--judged-prep`` and ``--record-verdicts`` run in SEPARATE invocations
    from ``--mechanical`` (often in a different sitting, always in a
    different launch), so the dossiers cannot be handed along in memory. The
    files the mechanical half wrote are the handover, which is also what
    makes the handover auditable."""
    novelty_dir = base / "novelty"
    if not novelty_dir.is_dir():
        return {}
    out = {}
    for path in sorted(novelty_dir.glob("*.json")):
        out[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    return out


def _build_external_provider(kind: str, program_root: Path) -> tuple[object, Callable[[], None]]:
    """Build the R5 provider ``--external-provider`` names, and the closer
    for whatever it opened.

    A module-level function rather than a branch inlined into
    :func:`_run_screen`, specifically so a test can monkeypatch
    ``trialerror.cli.lens._build_external_provider`` and inject a
    :class:`~trialerror.lens.novelty.StaticExternalProvider` instead of
    opening a real index or making a live call -- the same seam
    ``trialerror.cli.lit._build_query_encoder`` exists for.

    Raises :class:`ValueError` with the operator's next step in the message
    when the named provider cannot be built (no index on disk, no key
    configured); the caller turns that into one refusal envelope rather than
    a traceback."""
    from trialerror.cli.lit import _build_query_encoder, _load_program_config_raw, _resolve_arxiv_db_path
    from trialerror.lens.novelty import ArxivIndexProvider, LitApiSearchProvider
    from trialerror.litapi.config import load_litapi_config

    litapi_cfg = load_litapi_config(_load_program_config_raw(program_root))
    if kind == "arxiv-index":
        from trialerror.arxiv_index.store import open_arxiv_index_db

        db_path = _resolve_arxiv_db_path(program_root, litapi_cfg)
        if not db_path.is_file():
            raise ValueError(
                f"--external-provider arxiv-index: no index db at {db_path}; build it with "
                "`trialerror lit arxiv-index build --zip <path>` or point "
                "[litapi.arxiv_index].db_path at one"
            )
        encoder = _build_query_encoder(litapi_cfg, program_root)
        conn = open_arxiv_index_db(db_path)
        return ArxivIndexProvider(conn=conn, encoder=encoder), conn.close
    if kind == "litapi":
        from trialerror.litapi.client import LitApiClient, build_default_providers

        providers = build_default_providers(litapi_cfg, program_root=program_root)
        if not providers:
            raise ValueError(
                "--external-provider litapi: no providers are configured under [litapi] in trialerror.toml"
            )
        return LitApiSearchProvider(client=LitApiClient(providers)), lambda: None
    raise ValueError(f"--external-provider: unknown provider {kind!r}")


def _parse_where(values: Sequence[str] | None) -> tuple[dict[str, str] | None, dict | None]:
    """``--where provenance.key=value`` terms into a mapping, or a refusal.

    The ``provenance.`` prefix is required and stripped: a filter that
    accepted a bare column name would read as a promise that it filters on
    THAT column, and it deliberately does not -- see
    :func:`trialerror.lens.novelty._matches_where`."""
    if not values:
        return None, None
    where: dict[str, str] = {}
    for raw in values:
        term = str(raw)
        if "=" not in term:
            return None, error_envelope(
                "lens screen", "where_malformed",
                f"--where {term!r} has no '=': a term is provenance.<key>=<value>",
            )
        key, _sep, value = term.partition("=")
        key = key.strip()
        if not key.startswith("provenance."):
            return None, error_envelope(
                "lens screen", "where_malformed",
                f"--where {term!r}: the key must be spelled provenance.<key> (got {key!r}). The filter "
                "reads a record's resolved fields, which is a column on a row written since the AIIF "
                "migration and a provenance JSON key on one written before it; the one spelling covers "
                "both, and a bare column name would promise something narrower",
            )
        field = key[len("provenance.") :].strip()
        if not field:
            return None, error_envelope(
                "lens screen", "where_malformed", f"--where {term!r}: provenance. names no key"
            )
        where[field] = value
    return where, None


def _run_screen(args: argparse.Namespace) -> dict:
    calibration = getattr(args, "calibration", False)
    record_calibration_mode = getattr(args, "record_calibration", False)
    baseline_mode = getattr(args, "baseline", False)
    if not (
        args.mechanical or args.judged_prep or args.record_verdicts
        or calibration or record_calibration_mode or baseline_mode
    ):
        return error_envelope(
            "lens screen", "no_phase",
            "specify a phase: --mechanical, --judged-prep, --record-verdicts <file>, --calibration, "
            "--record-calibration or --baseline",
            next_actions=[next_action(["trialerror", "lens", "screen", "--help"], "see the screen's phases")],
        )
    where, where_refusal = _parse_where(getattr(args, "where", None))
    if where_refusal is not None:
        return where_refusal
    if (where or getattr(args, "status", None)) and not baseline_mode:
        return error_envelope(
            "lens screen", "baseline_only_flag",
            "--status and --where select the population a --baseline distribution is read over; no other "
            "phase reads them, and silently ignoring a filter someone passed is how a number gets "
            "reported for the wrong set of records",
        )
    if calibration and not args.seed:
        return error_envelope(
            "lens screen", "seed_required",
            "--calibration needs --seed: the plants' bundles, the envelope order and the judge mask are "
            "all seeded draws, and a calibration that cannot reproduce them cannot be reported",
        )
    if calibration and not (getattr(args, "plants_file", None) or _configured_plants(args)):
        return error_envelope(
            "lens screen", "plants_file_required",
            "--calibration builds a batch of PLANTS ONLY, so the plants are the whole batch: pass "
            "--plants-file (or set [lens.novelty] plants_file). There is nothing to calibrate a judge "
            "against otherwise",
        )
    if record_calibration_mode and not (
        getattr(args, "judge_sheet_a", None) and getattr(args, "judge_sheet_b", None)
    ):
        return error_envelope(
            "lens screen", "two_judges_required",
            "--record-calibration compares TWO judges: pass --judge-sheet-a and --judge-sheet-b. A kappa "
            "needs two raters, and one judge's catch rate on its own is a number the calibration already "
            "reports for every real batch",
        )
    # The mode and the provider are two halves of one decision, and a
    # mismatch used to surface as either a refusal no flag could clear or a
    # silent no-op. Both halves are argument-only, so they are answered
    # before anything opens.
    if args.external_query_mode != "none" and args.external_provider == "none":
        return error_envelope(
            "lens screen", "external_provider_required",
            f"--external-query-mode {args.external_query_mode} names a query, but --external-provider is "
            "'none', so there is nothing to issue it to. Name the provider that holds your external index",
            next_actions=[next_action(
                ["trialerror", "lens", "screen", "--round-id", args.round_id, "--mechanical",
                 "--external-query-mode", args.external_query_mode, "--external-provider", "arxiv-index"],
                "issue the query against the local arXiv semantic index",
            )],
        )
    if args.executed_procedure and getattr(args, "executed_procedure_file", None):
        return error_envelope(
            "lens screen", "executed_procedure_ambiguous",
            "--executed-procedure and --executed-procedure-file both name the procedure that ran, and a "
            "compliance hash computed over two different byte strings is a compliance claim about neither. "
            "Pass one",
        )
    if getattr(args, "paraphrase_backend", DEFAULT_PARAPHRASE_BACKEND) != DEFAULT_PARAPHRASE_BACKEND:
        return error_envelope(
            "lens screen", "paraphrase_backend_unimplemented",
            f"--paraphrase-backend {args.paraphrase_backend} is declared but not implemented, and this "
            "screen will not silently fall back to the deterministic one: a batch's plants are the only "
            "thing that makes its labels trustworthy, so which backend wrote them is not a detail to "
            "guess at. Run without the flag for the seeded, lossless deterministic paraphrase",
        )
    if args.external_query_mode == "none" and args.external_provider != "none":
        return error_envelope(
            "lens screen", "external_mode_required",
            f"--external-provider {args.external_provider} builds a provider, but --external-query-mode is "
            "'none', which issues no query at all -- R5 would be empty and the dossiers would not say why. "
            "Name the mode whose text you are willing to send",
        )
    store, err = _open_store(args)
    if err is not None:
        return err
    base = round_dir(store.program_root, args.round_id)
    result: dict = {"round_id": args.round_id}
    close_external: Callable[[], None] | None = None
    # The instrument: the flag wins over the config, the config over the
    # documented default. Resolved ONCE, here, so `--judged-prep` and
    # `--record-verdicts` in the same invocation cannot disagree.
    config = _novelty_config(store.program_root)
    judged_sets = getattr(args, "judged_sets", None) or config.get("judged_sets")
    # ...unless a BATCH is being recorded against, in which case the batch is
    # the authority and the flag is only checked for agreement (lane FB-6
    # item 3). The sets a judge was shown are a property of the batch file,
    # not of the command that reads it back, and the labels file's hash is
    # computed OVER the declared sets -- so resolving a labels file against
    # the CLI's default while recording an R2,R4 batch refused a file that
    # matched the batch exactly, until --judged-sets was repeated by hand on
    # every recording command.
    #
    # SCOPE, recorded deliberately rather than stumbled into (stage-3 finding
    # N2): this resolution is the INVOCATION's, not the recording phase's. In
    # the one shape where that is visible -- a single `lens screen` that both
    # PREPS and RECORDS, with no `--judged-sets` and no `[lens.novelty]
    # judged_sets` anywhere -- the batch being recorded against therefore also
    # supplies the sets the new batch is built under, where before FB-6 the
    # prep would have taken the CLI default. That is the coherent reading of
    # the brief's "the batch is the authority": one invocation ends up with
    # ONE declaration rather than silently mixing two, which is the same
    # argument the resolution is here for at all. The labels file, the plants
    # file and both phases' vocabularies are resolved once, from it. Nothing
    # explicit is ever overridden: a flag or a config row that disagrees with
    # the batch is refused by name below, so the only run this can steer is
    # one that declared nothing.
    refusal, from_batch = _judged_sets_from_batch(args, base, judged_sets)
    if refusal is not None:
        store.close()
        return refusal
    if from_batch is not None:
        judged_sets = from_batch
    labels_path = _config_path(
        store.program_root, getattr(args, "labels_file", None) or config.get("labels_file")
    )
    try:
        label_vocabularies = (
            load_label_vocabularies(
                json.loads(labels_path.read_text(encoding="utf-8")), judged_sets=judged_sets,
            )
            if labels_path is not None
            else None
        )
    except (NoveltyError, ValueError, json.JSONDecodeError, OSError) as exc:
        store.close()
        return error_envelope(
            "lens screen", "labels_file_refused",
            f"{labels_path}: {exc}",
            details={"labels_file": str(labels_path)},
        )
    plants_path = _config_path(
        store.program_root, getattr(args, "plants_file", None) or config.get("plants_file")
    )
    batch_fail_on = getattr(args, "batch_fail_on", None) or config.get("batch_fail_on")
    try:
        external_plants = (
            json.loads(plants_path.read_text(encoding="utf-8")) if plants_path is not None else None
        )
        if external_plants is not None:
            # Validated HERE as well as inside build_judged_batch, so a
            # malformed file refuses before the screen embeds a single
            # statement and the refusal names the plant's position.
            load_external_plants(
                external_plants, judged_sets=judged_sets, label_vocabularies=label_vocabularies,
            )
    except (NoveltyError, ValueError, json.JSONDecodeError, OSError) as exc:
        store.close()
        return error_envelope(
            "lens screen", "plants_file_refused",
            f"{plants_path}: {exc}",
            details={"plants_file": str(plants_path)},
        )
    try:
        if baseline_mode:
            # Read-only and FIRST, so a `--baseline` passed alongside a
            # writing phase reports the distribution as it stood BEFORE that
            # phase changed the round -- which is the only reading of "the
            # baseline" that means anything.
            result["baseline"] = baseline_distribution(
                store,
                round_id=args.round_id,
                status=getattr(args, "status", None),
                where=where,
                corpus_mode=args.corpus_mode,
                reembed=getattr(args, "reembed_archive", False),
            )

        if args.mechanical:
            alarms = json.loads(args.alarms) if args.alarms else None
            external = None
            if args.external_provider != "none":
                try:
                    external, close_external = _build_external_provider(
                        args.external_provider, store.program_root
                    )
                except (ValueError, OSError, ImportError) as exc:
                    return error_envelope("lens screen", "external_provider_unavailable", str(exc))
            mechanical = run_mechanical_screen(
                store,
                round_id=args.round_id,
                launch_id=args.launch_id,
                idea_ids=args.idea_ids,
                batch_id=args.batch_id,
                external=external,
                external_query_mode=args.external_query_mode,
                alarms=alarms,
                rescreen=args.rescreen,
                corpus_mode=args.corpus_mode,
            )
            # The CLI reports the batch's shape, not every dossier: a
            # terminal envelope carrying N full dossiers is unreadable, and
            # the dossiers are on disk where a reader can open one.
            result["mechanical"] = {
                "batch_id": mechanical["batch_id"],
                "n_screened": mechanical["n_screened"],
                "n_merged": mechanical["n_merged"],
                "merged": mechanical["merged"],
                "flagged": sorted(i for i, d in mechanical["dossiers"].items() if d["known_mechanic"]),
                "distribution": mechanical["distribution"],
                "collapse": mechanical["collapse"],
                "reference_snapshot": mechanical["reference_snapshot"],
                "dossier_dir": str(base / "novelty"),
            }

        if args.judged_prep:
            if not args.seed:
                return error_envelope(
                    "lens screen", "seed_required",
                    "--judged-prep needs --seed: the judged scope, the plants and the envelope order are "
                    "all seeded draws, and a round that cannot reproduce them cannot report them",
                )
            dossiers = _load_dossiers(base)
            if not dossiers:
                return error_envelope(
                    "lens screen", "no_dossiers",
                    f"no dossiers on file for round {args.round_id!r}; run --mechanical first",
                    next_actions=[next_action(
                        ["trialerror", "lens", "screen", "--round-id", args.round_id, "--mechanical"],
                        "run the mechanical half first",
                    )],
                )
            batch = build_judged_batch(
                store,
                round_id=args.round_id,
                dossiers=dossiers,
                seed=args.seed,
                sample_fraction=args.sample_fraction,
                second_judge_fraction=args.second_judge_fraction,
                plants_per_kind=args.plants,
                batch_id=args.batch_id,
                corpus_mode=args.corpus_mode,
                judged_sets=judged_sets,
                label_vocabularies=label_vocabularies,
                external_plants=external_plants,
                batch_fail_on=batch_fail_on,
                reembed_archive=getattr(args, "reembed_archive", False),
            )
            result["judged_prep"] = {
                "batch_id": batch["batch_id"],
                "judged_sets": batch["judged_sets"],
                "batch_fail_on": batch["batch_fail_on"],
                "scope": batch["scope"],
                "n_envelopes": len(batch["envelopes"]),
                "n_plants": len(batch["plants"]),
                # Which of the ROUND's own plants this batch got, and
                # anything the per-batch filter declined to do quietly
                # (lane FB-7 item 5). Surfaced in the envelope as well as
                # on the batch file: a warning nobody sees is a warning
                # that did not happen.
                "plants_injected": batch["plants_injected"],
                "warnings": batch["warnings"],
                "second_judge": batch["second_judge"],
                "batch_file": str(base / "judged" / f"{batch['batch_id']}.json"),
            }
            if args.judge_envelopes_out:
                out_dir = Path(args.judge_envelopes_out)
                out_dir.mkdir(parents=True, exist_ok=True)
                for view in batch["judge_views"]:
                    (out_dir / f"{view['subject_id']}.json").write_text(
                        json.dumps(view, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                        encoding="utf-8", newline="\n",
                    )
                result["judged_prep"]["judge_envelopes_dir"] = str(out_dir)
                result["judged_prep"]["n_judge_envelopes"] = len(batch["judge_views"])

        if calibration:
            # Dossiers are OPTIONAL here, unlike --judged-prep: a calibration
            # exists precisely for the moment before a round has records. When
            # they are on file they supply donor fields and the baseline
            # cosine distribution the card reports.
            batch = build_calibration_batch(
                store,
                round_id=args.round_id,
                external_plants=external_plants,
                seed=args.seed,
                dossiers=_load_dossiers(base),
                judged_sets=judged_sets,
                label_vocabularies=label_vocabularies,
                batch_fail_on=batch_fail_on,
                batch_id=args.batch_id,
                corpus_mode=args.corpus_mode,
                reembed_archive=getattr(args, "reembed_archive", False),
            )
            result["calibration"] = {
                "batch_id": batch["batch_id"],
                "judged_sets": batch["judged_sets"],
                "batch_fail_on": batch["batch_fail_on"],
                "n_plants": len(batch["plants"]),
                "n_envelopes": len(batch["envelopes"]),
                "plants_injected": batch["plants_injected"],
                "warnings": batch["warnings"],
                "baseline_cosine_distribution": batch["baseline_cosine_distribution"],
                "batch_file": str(base / "judged" / f"{batch['batch_id']}.json"),
            }
            if args.judge_envelopes_out:
                out_dir = Path(args.judge_envelopes_out)
                out_dir.mkdir(parents=True, exist_ok=True)
                for view in batch["judge_views"]:
                    (out_dir / f"{view['subject_id']}.json").write_text(
                        json.dumps(view, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                        encoding="utf-8", newline="\n",
                    )
                result["calibration"]["judge_envelopes_dir"] = str(out_dir)
                result["calibration"]["n_judge_envelopes"] = len(batch["judge_views"])

        if record_calibration_mode:
            if not args.launch_id:
                return error_envelope(
                    "lens screen", "launch_id_required",
                    "--record-calibration needs --launch-id: a verdict row records WHO issued it",
                )
            batch_file = recording_batch_file(args, base, "calibration-0")
            if not batch_file.is_file():
                return error_envelope(
                    "lens screen", "no_calibration_batch",
                    f"no calibration batch at {batch_file}; run --calibration first (or pass --batch-id)",
                    next_actions=[next_action(
                        ["trialerror", "lens", "screen", "--round-id", args.round_id, "--calibration",
                         "--seed", "<seed>", "--plants-file", "<file>"],
                        "build the calibration batch first",
                    )],
                )
            card = record_calibration(
                store,
                round_id=args.round_id,
                batch=json.loads(batch_file.read_text(encoding="utf-8")),
                labels_a=json.loads(Path(args.judge_sheet_a).read_text(encoding="utf-8")),
                labels_b=json.loads(Path(args.judge_sheet_b).read_text(encoding="utf-8")),
                issued_by_launch=args.launch_id,
                launch_a=args.judge_launch_a,
                launch_b=args.judge_launch_b,
                pair_ratings=(
                    json.loads(Path(args.pair_ratings).read_text(encoding="utf-8"))
                    if args.pair_ratings else None
                ),
                label_vocabularies=label_vocabularies,
                batch_fail_on=batch_fail_on,
                prereg_id=args.prereg_id,
                # Lane FB-8b item 5: the same two arguments --record-verdicts
                # takes, read the same way (byte-exact from the file when a
                # file is named), so a calibration recorded under a prereg
                # says whether the procedure was followed instead of leaving
                # the column NULL.
                executed_procedure=_executed_procedure(args),
                executed_params=json.loads(args.executed_params) if args.executed_params else None,
                # One submission per calibration batch, the round's own rule:
                # the same flag lifts it here as at --record-verdicts.
                supersede=args.supersede,
            )
            result["record_calibration"] = {
                k: v for k, v in card.items() if k != "verdicts"
            } | {
                "verdict_ids": [v["verdict_id"] for v in card["verdicts"]],
                "card_file": str(base / "judged" / f"{card['batch_id']}-card.json"),
            }

        if args.record_verdicts:
            if not args.launch_id:
                return error_envelope(
                    "lens screen", "launch_id_required",
                    "--record-verdicts needs --launch-id: a verdict row records WHO issued it",
                )
            batch_file = recording_batch_file(args, base, "judged-0")
            if not batch_file.is_file():
                return error_envelope(
                    "lens screen", "no_judged_batch",
                    f"no judged batch at {batch_file}; run --judged-prep first (or pass --batch-id)",
                )
            batch = json.loads(batch_file.read_text(encoding="utf-8"))
            # The batch IS the declaration: it is what the judge was shown,
            # and `_judged_sets_from_batch` has already resolved `judged_sets`
            # off this very file (and refused a --judged-sets that disagreed
            # with it) before the labels file was read.
            labels = json.loads(Path(args.record_verdicts).read_text(encoding="utf-8"))
            second = (
                json.loads(Path(args.second_judge_file).read_text(encoding="utf-8"))
                if args.second_judge_file else None
            )
            recorded = record_novelty_verdicts(
                store,
                round_id=args.round_id,
                batch=batch,
                labels=labels,
                issued_by_launch=args.launch_id,
                prereg_id=args.prereg_id,
                executed_procedure=_executed_procedure(args),
                executed_params=json.loads(args.executed_params) if args.executed_params else None,
                second_judge_labels=second,
                supersede=args.supersede,
                label_vocabularies=label_vocabularies,
                batch_fail_on=batch_fail_on,
            )
            result["record_verdicts"] = {
                k: v for k, v in recorded.items() if k != "verdicts"
            } | {"verdict_ids": [v["verdict_id"] for v in recorded["verdicts"]]}
    except QueryEmbedBackendUnrunnableError as exc:
        # lane F-1 item D: a screen refused for want of a query-side embed
        # backend gets its own code and the doctor check that reports it, so
        # an agent reading the envelope can act rather than retry. Branched on
        # the exception TYPE and its declared `code` -- the way `verify
        # hypothesis` and `query search` do it -- never on the prose of the
        # refusal message.
        return error_envelope(
            "lens screen", exc.code or "screen_refused", str(exc),
            details={
                "doctor_check": retrieve_engine.QUERY_EMBED_DOCTOR_CHECK,
                "config_table": retrieve_engine.QUERY_EMBED_TABLE,
            },
            next_actions=[next_action(
                retrieve_engine.query_embed_next_action_argv(store.program_root),
                "check the query-side embed backend",
            )],
        )
    except NoveltyError as exc:
        return error_envelope("lens screen", exc.code or "screen_refused", str(exc))
    # VerifyError is in the tuple because of this lane's probe (c): a
    # `--prereg-id` naming a prereg that does not exist came back out of the
    # screen as an uncaught `PreregNotFoundError` traceback rather than an
    # envelope -- on the round path too, from the day compliance was first
    # stamped. A CLI that answers a bad argument with a stack trace is a CLI
    # an agent cannot act on, and this refusal now arrives before any row is
    # written rather than out of the verdict table's own XID check.
    except (
        LensError, StoreError, VerifyError, ValueError, json.JSONDecodeError, OSError
    ) as exc:
        return error_envelope("lens screen", "screen_error", f"{type(exc).__name__}: {exc}")
    finally:
        if close_external is not None:
            close_external()
        store.close()

    next_actions = []
    if calibration and not record_calibration_mode:
        next_actions.append(next_action(
            ["trialerror", "lens", "screen", "--round-id", args.round_id, "--record-calibration",
             "--judge-sheet-a", "<a.json>", "--judge-sheet-b", "<b.json>", "--launch-id", "<launch>"],
            "score both judges against the calibration batch and write the card",
        ))
    if args.mechanical and not args.judged_prep:
        next_actions.append(next_action(
            ["trialerror", "lens", "screen", "--round-id", args.round_id, "--judged-prep", "--seed", "<seed>"],
            "build the judged batch once every lens has posted",
        ))
    return ok_envelope("lens screen", result=result, next_actions=next_actions)


def _run_recheck(args: argparse.Namespace) -> dict:
    """Enqueue the convergent-discovery re-check as a ledger job rather than
    running it inline.

    A re-check is a SCHEDULED pass over a whole round, it reaches an external
    index when a mode names one, and it is meant to be resumable -- which is
    what the job ledger is for, and what a CLI process that exits is not. So
    this verb books the work and hands back the command that runs it; the
    worker resolves the handler from the payload (jobs/handlers.py)."""
    if args.external_query_mode != "none" and args.external_provider == "none":
        return error_envelope(
            "lens recheck", "external_provider_required",
            f"--external-query-mode {args.external_query_mode} names a query, but --external-provider is "
            "'none', so the worker would have nothing to issue it to",
        )
    if args.external_query_mode == "none" and args.external_provider != "none":
        return error_envelope(
            "lens recheck", "external_mode_required",
            f"--external-provider {args.external_provider} builds a provider, but --external-query-mode is "
            "'none', which issues no query at all -- R5 would be silently idle",
        )
    store, err = _open_store(args)
    if err is not None:
        return err
    payload: dict = {
        "handler": "convergent_recheck",
        "round_id": args.round_id,
        "external_query_mode": args.external_query_mode,
        "external_provider": args.external_provider,
    }
    for key, value in (
        ("statuses", args.statuses), ("idea_ids", args.idea_ids), ("launch_id", args.launch_id),
        ("corpus_k", args.corpus_k), ("external_k", args.external_k),
        ("allow_unscreened", args.allow_unscreened),
    ):
        if value:
            payload[key] = value
    try:
        job = ledger.enqueue(store, kind="custom", payload=payload, job_id=args.job_id)
    except (StoreError, ValueError) as exc:
        return error_envelope("lens recheck", "enqueue_refused", str(exc))
    finally:
        store.close()
    return ok_envelope(
        "lens recheck", result={"job": job, "payload": payload},
        next_actions=[next_action(
            ["trialerror", "jobs", "start-worker", "--job-id", job["job_id"], "--foreground", "--mode", "once"],
            "run the re-check now (or leave it for an open-queue worker)",
        )],
    )
