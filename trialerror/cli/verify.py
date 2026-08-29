"""``trialerror verify`` — the verification suite CLI surface. Design Section
5.2 (``verify`` row): "citecheck, hypothesis, reproduce | pipelines ->
verdict rows (all owned by M9)." Thin wrapper over ``trialerror.verify.{citecheck,
hypothesis,faithfulness,reproduce}`` — all logic lives there; this module
only parses argv and shapes the AgentEnvelope (same convention as
``trialerror/cli/query.py``/``trialerror/cli/gate.py``). ``faithfulness`` is a v1
scope item (design Section 11: "hypothesis pipeline hardening ... Ragas
faithfulness port") added by this build, alongside the ``citecheck``/
``hypothesis``/``reproduce`` verbs already landed.

**Judgment, from the CLI (the LLM-judgment boundary, stated once in
``trialerror/verify/__init__.py``, applied here concretely):** this process
never calls an LLM. ``citecheck``'s ``--judgments-file``/``hypothesis``'s
``--judgments-file``/``faithfulness``'s ``--decomposition-file``+
``--judgments-file`` are optional-or-required JSON files the CALLER (an
agent that already ran the real judgment step out-of-band, or a test)
supplies -- ``{"<pair_id-or-chunk_id>": {"label": "...", "note": "..."}}``
(``faithfulness``'s decomposition file instead maps to ``{"claims":
[...]}``) -- turned into a plain dict-lookup ``judge`` callable. Omitting
it for ``citecheck`` runs the mechanical pass only (escalation candidates
come back ``escalation_not_sampled``/``escalation_selected``, never
judged); ``hypothesis``/``faithfulness`` REQUIRE their judgment file(s)
(every retrieved evidence chunk / decomposed claim needs a stance to
aggregate a status/score).

Design Section 5.2 registration rule: this module lives at
``trialerror/cli/verify.py`` and is auto-discovered by ``trialerror.cli.discover_groups``
-- adding it never touches ``trialerror/cli/__init__.py``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from trialerror.stores import get as store_get
from trialerror.stores.errors import StoreError, ValidationError, XidTargetMissingError
from trialerror.stores.store import Store, open_store
from trialerror.util.config import find_program_root
from trialerror.util.envelope import error_envelope, next_action, ok_envelope
from trialerror.verify.citecheck import run_citecheck
from trialerror.verify.errors import CitecheckError, ReproductionRefError, VerdictNotFoundError, VerifyError
from trialerror.verify.faithfulness import run_faithfulness
from trialerror.verify.hypothesis import DEFAULT_FAR_FLOOR, DEFAULT_WEIGHTS, run_hypothesis_verification
from trialerror.verify.reproduce import reproduce_verdict

GROUP_NAME = "verify"
HELP = "Verification suite: citation check, hypothesis-vs-literature, reproduction runner."


def _add_program_root_arg(p: argparse.ArgumentParser) -> None:
    # Registered on the `verify` parser AND on every action subparser (the
    # ``trialerror/cli/law.py`` convention): argparse only recognizes a
    # parent-only optional BEFORE the subcommand token, and the natural
    # ordering is `trialerror verify citecheck ... --program-root X` -- so both
    # positions need the flag registered. FX-12 (trialerror/cli/__init__.py
    # TRIALERROR-DEV-NOTE): default=SUPPRESS so an unset value here never
    # overwrites the global --program-root/--platform-root the top-level
    # parser resolved.
    p.add_argument(
        "--program-root", default=argparse.SUPPRESS, help="override the program root (default: discover trialerror.toml upward from CWD)"
    )
    p.add_argument("--platform-root", default=argparse.SUPPRESS, help="override the platform root (mainly for tests)")


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    _add_program_root_arg(parser)
    actions = parser.add_subparsers(dest="action", metavar="<action>")

    p_cc = actions.add_parser("citecheck", help="two-tier citation check: mechanical pass + deterministic LLM-escalation queue")
    _add_program_root_arg(p_cc)
    p_cc.add_argument("subject", help="an existing file path (markdown with [[cite:ANC-...]] markers), a JSON claim-set file (a pre-extracted pairs list), or an artifact_id")
    p_cc.add_argument("--by-launch", required=True, dest="issued_by_launch")
    p_cc.add_argument("--procedure-version", default="1", dest="procedure_version")
    p_cc.add_argument("--sample-rate", type=int, default=5, dest="sample_rate")
    p_cc.add_argument("--judgments-file", default=None, dest="judgments_file", help="JSON {pair_id: {label, note?}} for sampled escalation candidates")
    p_cc.set_defaults(handler=_run_citecheck)

    p_hyp = actions.add_parser("hypothesis", help="hypothesis-vs-literature: stratified retrieve -> contracrow classify -> aggregate -> verdict")
    _add_program_root_arg(p_hyp)
    hyp_group = p_hyp.add_mutually_exclusive_group(required=True)
    hyp_group.add_argument("--id", default=None, dest="hyp_id", help="an existing hypothesis row")
    hyp_group.add_argument("--text", default=None, dest="hypothesis_text", help="an inline hypothesis (no hypothesis row required)")
    p_hyp.add_argument("--by-launch", required=True, dest="issued_by_launch")
    p_hyp.add_argument("--query", default=None, help="defaults to the hypothesis text")
    p_hyp.add_argument("--judgments-file", required=True, dest="judgments_file", help="JSON {chunk_id: {label, note?}} covering every retrieved evidence chunk")
    p_hyp.add_argument("--k-total", type=int, default=6, dest="k_total")
    p_hyp.add_argument("--weights", default=None, help=f"comma-separated near,moderate,far (default {','.join(map(str, DEFAULT_WEIGHTS))})")
    p_hyp.add_argument("--far-floor", type=int, default=DEFAULT_FAR_FLOOR, dest="far_floor")
    p_hyp.add_argument("--mode", default="hybrid", choices=["auto", "fts", "vector", "hybrid", "graph"])
    p_hyp.add_argument("--procedure-version", default="1", dest="procedure_version")
    p_hyp.add_argument("--prereg", action="store_true", help="commit a blind prereg first (design: default for keystones)")
    p_hyp.add_argument("--prereg-title", default=None, dest="prereg_title")
    p_hyp.set_defaults(handler=_run_hypothesis)

    p_faith = actions.add_parser(
        "faithfulness", help="Ragas-pattern: decompose cited sentences into atomic claims, verify each against its own cited anchor"
    )
    _add_program_root_arg(p_faith)
    p_faith.add_argument("subject", help="an existing file path (markdown with [[cite:ANC-...]] markers), a JSON claim-set file (a pre-extracted pairs list), or an artifact_id -- same resolution as 'verify citecheck'")
    p_faith.add_argument("--by-launch", required=True, dest="issued_by_launch")
    p_faith.add_argument("--procedure-version", default="1", dest="procedure_version")
    p_faith.add_argument("--sample-rate", type=int, default=5, dest="sample_rate")
    p_faith.add_argument("--decomposition-file", required=True, dest="decomposition_file", help="JSON {pair_id: {claims: [...]}} -- one entry per cited sentence in 'subject'")
    p_faith.add_argument("--judgments-file", required=True, dest="judgments_file", help="JSON {claim_pair_id: {label, note?}} -- one entry per decomposed claim (pair_id '<sentence pair_id>::CLM-<n>')")
    p_faith.set_defaults(handler=_run_faithfulness)

    p_repro = actions.add_parser("reproduce", help="re-run a verdict's reproduction_ref script; byte-exact sha comparison")
    _add_program_root_arg(p_repro)
    p_repro.add_argument("verdict_id")
    p_repro.add_argument("--by-launch", required=True, dest="issued_by_launch")
    p_repro.add_argument("--gate-id", default=None, dest="gate_id", help="when given, also stamps gate.reproduction_status/reproduction_ref (the M10 coupling)")
    p_repro.add_argument("--cwd", default=None)
    p_repro.add_argument("--timeout", type=float, default=60.0)
    p_repro.set_defaults(handler=_run_reproduce)

    parser.set_defaults(handler=_run_no_action)
    return parser


def _resolve_program_root(args: argparse.Namespace) -> Path | None:
    if args.program_root:
        return Path(args.program_root)
    return find_program_root()


def _open(args: argparse.Namespace, cmd: str) -> tuple[Store | None, dict | None]:
    program_root = _resolve_program_root(args)
    if program_root is None:
        return None, error_envelope(
            cmd, "no_program_root", "no --program-root given and no trialerror.toml found walking up from CWD",
            next_actions=[
                next_action(["trialerror", "program", "init", "<name>", "--dir", "."], "scaffold a program in this directory first")
            ],
        )
    return open_store(program_root, platform_root=args.platform_root), None


def _run_no_action(_args: argparse.Namespace) -> dict:
    return error_envelope(
        "verify", "no_action", "specify an action: citecheck|hypothesis|faithfulness|reproduce",
        next_actions=[next_action(["trialerror", "verify", "--help"], "list verify actions")],
    )


def _load_judgments_file(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _judge_from_table(table: Mapping[str, Any], key_field: str):
    def judge(envelope: Mapping[str, Any]):
        key = envelope[key_field]
        if key not in table:
            raise VerifyError(f"no judgment supplied for {key_field}={key!r} in --judgments-file")
        return table[key]

    return judge


def _resolve_citecheck_input(store: Store, program_root: Path, subject: str) -> tuple[str | None, list | None]:
    """Returns ``(text, pairs)`` -- exactly one populated, per design's
    ``<artifact|file|claim-set>`` polymorphic positional argument. Tries,
    in order: an existing file that parses as a JSON list (a claim-set of
    pre-extracted pairs); an existing file read as raw text (markers
    extracted); an ``artifact_id`` looked up in this program's registry
    (its ``artifact.path``, resolved under ``program_root``, read as raw
    text)."""
    candidate = Path(subject)
    if not candidate.is_absolute():
        candidate = program_root / subject
    if candidate.is_file():
        raw = candidate.read_text(encoding="utf-8")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return raw, None
        if isinstance(parsed, list):
            return None, parsed
        return raw, None

    artifact = store_get(store, "artifact", pk_column="artifact_id", pk_value=subject)
    if artifact is None:
        raise CitecheckError(f"{subject!r} is not an existing file, a JSON claim-set, or a known artifact_id")
    artifact_path = Path(artifact["path"])
    if not artifact_path.is_absolute():
        artifact_path = program_root / artifact_path
    return artifact_path.read_text(encoding="utf-8"), None


def _run_citecheck(args: argparse.Namespace) -> dict:
    program_root = _resolve_program_root(args)
    if program_root is None:
        return error_envelope("verify.citecheck", "no_program_root", "no --program-root given and no trialerror.toml found walking up from CWD")
    store, err = _open(args, "verify.citecheck")
    if err is not None:
        return err
    try:
        text, pairs = _resolve_citecheck_input(store, program_root, args.subject)
        judgments = _load_judgments_file(args.judgments_file)
        judge = _judge_from_table(judgments, "pair_id") if judgments else None
        result = run_citecheck(
            store, subject_id=args.subject, text=text, pairs=pairs,
            procedure_version=args.procedure_version, issued_by_launch=args.issued_by_launch,
            judge=judge, sample_rate=args.sample_rate,
        )
    except (CitecheckError, VerifyError, OSError, json.JSONDecodeError) as exc:
        return error_envelope("verify.citecheck", "citecheck_refused", str(exc))
    except (ValidationError, XidTargetMissingError, StoreError) as exc:
        return error_envelope("verify.citecheck", "record_refused", str(exc))
    finally:
        store.close()
    return ok_envelope("verify.citecheck", result=result)


def _run_faithfulness(args: argparse.Namespace) -> dict:
    program_root = _resolve_program_root(args)
    if program_root is None:
        return error_envelope("verify.faithfulness", "no_program_root", "no --program-root given and no trialerror.toml found walking up from CWD")
    store, err = _open(args, "verify.faithfulness")
    if err is not None:
        return err
    try:
        text, pairs = _resolve_citecheck_input(store, program_root, args.subject)
        decomposition_table = _load_judgments_file(args.decomposition_file) or {}
        judgments_table = _load_judgments_file(args.judgments_file) or {}
        decompose_judge = _judge_from_table(decomposition_table, "pair_id")
        verify_judge = _judge_from_table(judgments_table, "pair_id")
        result = run_faithfulness(
            store, subject_id=args.subject, text=text, pairs=pairs, decompose_judge=decompose_judge,
            verify_judge=verify_judge, issued_by_launch=args.issued_by_launch,
            procedure_version=args.procedure_version, sample_rate=args.sample_rate,
        )
    except (CitecheckError, VerifyError, OSError, json.JSONDecodeError) as exc:
        return error_envelope("verify.faithfulness", "faithfulness_refused", str(exc))
    except (ValidationError, XidTargetMissingError, StoreError) as exc:
        return error_envelope("verify.faithfulness", "record_refused", str(exc))
    finally:
        store.close()
    return ok_envelope("verify.faithfulness", result=result)


def _run_hypothesis(args: argparse.Namespace) -> dict:
    store, err = _open(args, "verify.hypothesis")
    if err is not None:
        return err
    try:
        judgments = _load_judgments_file(args.judgments_file) or {}
        judge = _judge_from_table(judgments, "chunk_id")
        weights = tuple(int(x) for x in args.weights.split(",")) if args.weights else DEFAULT_WEIGHTS
        result = run_hypothesis_verification(
            store, hyp_id=args.hyp_id, hypothesis_text=args.hypothesis_text, query=args.query,
            judge=judge, issued_by_launch=args.issued_by_launch, k_total=args.k_total, weights=weights,
            far_floor=args.far_floor, mode=args.mode, procedure_version=args.procedure_version,
            prereg=args.prereg, prereg_title=args.prereg_title,
        )
    except (VerifyError, OSError, json.JSONDecodeError) as exc:
        return error_envelope("verify.hypothesis", "hypothesis_refused", str(exc))
    except (ValidationError, XidTargetMissingError, StoreError) as exc:
        return error_envelope("verify.hypothesis", "record_refused", str(exc))
    finally:
        store.close()
    return ok_envelope("verify.hypothesis", result=result)


def _run_reproduce(args: argparse.Namespace) -> dict:
    store, err = _open(args, "verify.reproduce")
    if err is not None:
        return err
    try:
        result = reproduce_verdict(
            store, verdict_id=args.verdict_id, gate_id=args.gate_id, issued_by_launch=args.issued_by_launch,
            cwd=args.cwd, timeout=args.timeout,
        )
    except VerdictNotFoundError as exc:
        return error_envelope("verify.reproduce", "not_found", str(exc))
    except ReproductionRefError as exc:
        return error_envelope("verify.reproduce", "bad_reproduction_ref", str(exc))
    except (ValidationError, XidTargetMissingError, StoreError) as exc:
        return error_envelope("verify.reproduce", "record_refused", str(exc))
    finally:
        store.close()
    return ok_envelope(
        "verify.reproduce", result=result,
        next_actions=(
            [next_action(["trialerror", "gate", "apply-union", "--id", args.gate_id, "--by-launch", args.issued_by_launch], "apply the gate union if reproduction matched")]
            if args.gate_id else []
        ),
    )
