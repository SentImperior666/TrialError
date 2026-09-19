"""``trialerror obs`` -- observability CLI surface (design Section 12 M12 row:
"OTel GenAI span emission (launch/retrieval/job), pinned semconv, Phoenix
setup + docs"). Registration rule (design Section 5.2 / lane safety): this
module lives at ``trialerror/cli/obs.py`` and is auto-discovered by
``trialerror.cli.discover_groups`` -- adding it never touches ``trialerror/cli/
__init__.py``.

Three subcommands, per the M12 build brief ("CLI group convention... trialerror
obs {status,start-phoenix,smoke}"):

- ``status``  -- are OTel/Phoenix deps installed, is the configured OTLP
  endpoint reachable, has anything been dropped (runs this package's two
  doctor checks directly, scoped).
- ``start-phoenix`` -- launch a detached local ``phoenix serve`` process
  (the SAME Windows ``DETACHED_PROCESS`` technique ``trialerror.jobs.worker.
  spawn_worker`` uses for workers -- reimplemented here, not imported, per
  this build's lane isolation: ``trialerror/jobs/`` is untouched and this
  module has no import-time dependency on it). Idempotent (O-4,
  build-v2-polish): probes the configured OTLP endpoint first
  (``tracer.probe_reachable``) and refuses to spawn a second process if
  something already answers there -- either outcome returns a clear,
  structured envelope (``result.already_running``/``result.message``).
- ``smoke``   -- emit one representative span of each of the design's four
  kinds (launch/retrieval/verification/job) against the configured
  endpoint, then flush+shutdown -- the manual "did a span really reach
  Phoenix" round trip this build's report uses as its live-Phoenix proof.
- ``audit-digest`` -- one deterministic, VERDICT-FREE digest of a day of
  autonomous agent activity (Claude Code transcripts, shell history, the
  program's event rows, the doctor, previous digests), for the
  ``/sandbox-audit`` skill's rubric to judge. The digest shape, the classifier
  tag glossary and the redaction rules live in :mod:`trialerror.obs.audit`;
  the rubric and the read-only rule live in
  ``plugin/skills/sandbox-audit/SKILL.md``. This verb decides nothing -- it
  counts, tags and masks.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from trialerror.obs import state, tracer
from trialerror.obs.spans import job_attempt_span, launch_span, retrieval_span, verification_span
from trialerror.stores import paths
from trialerror.util.doctor import DoctorContext, discover_and_register_checks, run_checks
from trialerror.util.envelope import error_envelope, next_action, ok_envelope
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now

GROUP_NAME = "obs"
HELP = "Observability: OTel GenAI span emission + local Phoenix trace sink status/setup."

_OBS_CHECK_NAMES = ["obs_exporter_reachable", "obs_span_drop_counter"]


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(GROUP_NAME, help=HELP)
    sub = parser.add_subparsers(dest="obs_cmd", metavar="<command>", required=True)

    def _common(p: argparse.ArgumentParser) -> None:
        # FX-12 (trialerror/cli/__init__.py TRIALERROR-DEV-NOTE): default=SUPPRESS so
        # an unset value here never overwrites the global --program-root/
        # --platform-root the top-level parser resolved.
        p.add_argument(
            "--program-root", default=argparse.SUPPRESS, help="program scaffold root, for the span-drop-counter check/state file"
        )
        p.add_argument("--platform-root", default=argparse.SUPPRESS, help="override the platform root (mainly for tests)")

    p_status = sub.add_parser("status", help="OTel/Phoenix availability, endpoint reachability, span-drop count")
    _common(p_status)
    p_status.set_defaults(handler=_cmd_status)

    p_start = sub.add_parser("start-phoenix", help="launch a detached local `phoenix serve` (SQLite, zero Docker)")
    _common(p_start)
    p_start.add_argument("--python-exe", default=None, help="override the python executable (default: sys.executable)")
    p_start.set_defaults(handler=_cmd_start_phoenix)

    p_smoke = sub.add_parser("smoke", help="emit one span of each kind against the configured endpoint, then flush")
    _common(p_smoke)
    p_smoke.add_argument("--endpoint", default=None, help="override the OTLP/HTTP endpoint (default: TRIALERROR_OBS_OTLP_ENDPOINT or localhost:6006)")
    p_smoke.set_defaults(handler=_cmd_smoke)

    p_audit = sub.add_parser(
        "audit-digest",
        help="deterministic, verdict-free digest of a day of agent activity (for the sandbox-audit skill)",
    )
    _common(p_audit)
    p_audit.add_argument(
        "--since", default="24h", help="window start: a relative span (24h, 90m, 7d) or an ISO-8601 instant"
    )
    p_audit.add_argument("--until", default=None, help="window end as an ISO-8601 instant (default: now)")
    p_audit.add_argument(
        "--transcripts",
        default=None,
        help="Claude Code transcripts directory, searched recursively (default: the current user's ~/.claude/projects)",
    )
    p_audit.add_argument("--history", default=None, help="shell history file (plain or the ': <epoch>:0;cmd' extended form)")
    p_audit.add_argument("--previous-dir", default=None, help="directory of earlier digest JSON files, for the volume medians")
    p_audit.add_argument("--out", default=None, help="write the digest JSON here (mode 600) instead of returning it inline")
    p_audit.add_argument(
        "--allowed-write-root",
        action="append",
        default=None,
        metavar="ROOT",
        help="repeatable: a root writes are expected to stay inside (no roots declared = writes are reported unjudged)",
    )
    p_audit.add_argument(
        "--sensitive-path",
        action="append",
        default=None,
        metavar="PATH",
        help="repeatable: a path fragment or glob that counts as sensitive (default: the audit module's own list)",
    )
    p_audit.add_argument(
        "--allowed-host",
        action="append",
        default=None,
        metavar="HOST",
        help="repeatable: a host network use is expected to stay within (no hosts declared = network use is reported unjudged)",
    )
    p_audit.set_defaults(handler=_cmd_audit_digest)

    return parser


def _resolve_platform_root(args: argparse.Namespace) -> Path:
    return Path(args.platform_root) if args.platform_root else paths.platform_root()


def _cmd_status(args: argparse.Namespace) -> dict:
    discover_and_register_checks()
    ctx = DoctorContext(program_root=Path(args.program_root) if args.program_root else None)
    results = run_checks(ctx, only=_OBS_CHECK_NAMES)
    by_name = {r.name: r.to_dict() for r in results}
    warned = [r for r in results if r.status == "warn"]
    result = {
        "otel_available": tracer.is_available(),
        "endpoint": tracer.resolve_endpoint(),
        "checks": by_name,
        "process_span_drop_count": state.process_drop_count(),
    }
    next_actions = []
    if not tracer.is_available():
        next_actions.append(next_action(["pip", "install", "trialerror[obs]"], "install the optional OTel/Phoenix deps"))
    elif by_name.get("obs_exporter_reachable", {}).get("status") == "warn":
        next_actions.append(next_action(["trialerror", "obs", "start-phoenix"], "launch the local Phoenix trace sink"))
    if warned:
        return error_envelope(
            "obs.status", "obs_degraded", f"{len(warned)} obs check(s) warned", details=result, next_actions=next_actions
        )
    return ok_envelope("obs.status", result=result, next_actions=next_actions)


def _cmd_start_phoenix(args: argparse.Namespace) -> dict:
    platform_root = _resolve_platform_root(args)

    # O-4 (accumulated flag list): probe before spawning a second Phoenix.
    # Same TCP-connect probe trialerror.obs.checks.check_exporter_reachable
    # already uses (tracer.probe_reachable) -- reachable here just as
    # plausibly means "already running" as it does "reachable" there, so a
    # second `phoenix serve` never gets spawned on top of the first.
    endpoint = tracer.resolve_endpoint()
    if tracer.probe_reachable(endpoint):
        return ok_envelope(
            "obs.start-phoenix",
            result={
                "already_running": True,
                "pid": None,
                "endpoint": endpoint,
                "url": "http://localhost:6006",
                "message": f"something is already listening on {endpoint!r} -- not spawning a second `phoenix serve`",
            },
            next_actions=[
                next_action(["trialerror", "obs", "status"], "confirm the existing instance is actually Phoenix and healthy")
            ],
        )

    log_dir = platform_root / "obs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "phoenix_serve.log"

    argv = [args.python_exe or sys.executable, "-m", "phoenix.server.main", "serve"]
    popen_kwargs: dict = {}
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        # POSIX equivalent of DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP:
        # setsid() detaches from the controlling terminal so the child
        # survives the parent shell and is not hit by its Ctrl-C. Exercised
        # on every Linux run -- no `pragma: no cover` here, or coverage
        # would hide the only branch that platform ever takes; the branch has
        # its own direct coverage in tests/test_posix_detach.py (which skips
        # cleanly on win32).
        popen_kwargs["start_new_session"] = True

    try:
        log_fh = open(log_path, "ab")
    except OSError as exc:
        return error_envelope("obs.start-phoenix", "log_open_failed", str(exc))
    try:
        proc = subprocess.Popen(
            argv, cwd=str(platform_root), stdin=subprocess.DEVNULL, stdout=log_fh, stderr=subprocess.STDOUT, **popen_kwargs
        )
    except FileNotFoundError as exc:
        return error_envelope(
            "obs.start-phoenix",
            "phoenix_not_installed",
            f"could not launch `phoenix serve` ({exc}) -- install the optional 'obs' extra: pip install trialerror[obs]",
        )
    finally:
        log_fh.close()

    return ok_envelope(
        "obs.start-phoenix",
        result={
            "already_running": False,
            "pid": proc.pid,
            "argv": argv,
            "log_path": str(log_path),
            "url": "http://localhost:6006",
            "message": f"spawned a detached `phoenix serve` (pid {proc.pid}), logging to {log_path}",
        },
        next_actions=[next_action(["trialerror", "obs", "status"], "check whether the endpoint is now reachable")],
    )


def _cmd_smoke(args: argparse.Namespace) -> dict:
    if not tracer.is_available():
        return error_envelope(
            "obs.smoke",
            "otel_not_installed",
            "opentelemetry-sdk / otlp-http exporter not installed -- install the optional 'obs' extra: pip install trialerror[obs]",
        )

    program_root = Path(args.program_root) if args.program_root else None
    tracer.configure(endpoint=args.endpoint, program_root=program_root)
    ts = now()
    launch_id = new_id("LNCH")
    job_id = new_id("JOB")

    with launch_span(launch_id=launch_id, agent_kind="obs-smoke", model="smoke-model", actual_tokens=42, start_ts=ts, end_ts=now()):
        pass
    with retrieval_span(query="obs smoke test query", tiers=["fts", "vec"], k=5):
        pass
    with verification_span(procedure="obs.smoke", subject_id=launch_id, verdict="PASS"):
        pass
    with job_attempt_span(job_id=job_id, kind="obs-smoke"):
        pass

    flushed = tracer.flush()
    tracer.shutdown()
    return ok_envelope(
        "obs.smoke",
        result={
            "endpoint": tracer.resolve_endpoint(args.endpoint),
            "spans_emitted": 4,
            "flushed": flushed,
            "launch_id": launch_id,
            "job_id": job_id,
        },
        next_actions=[next_action(["trialerror", "obs", "status"], "check reachability/drop-count after this run")],
    )


def _cmd_audit_digest(args: argparse.Namespace) -> dict:
    from trialerror.obs import audit

    try:
        opts = audit.AuditOptions(
            since=args.since,
            until=args.until,
            transcripts=args.transcripts,
            history=args.history,
            program_root=args.program_root,
            platform_root=args.platform_root,
            previous_dir=args.previous_dir,
            allowed_write_roots=args.allowed_write_root,
            sensitive_paths=args.sensitive_path,
            allowed_hosts=args.allowed_host,
        )
    except ValueError as exc:
        return error_envelope("obs.audit-digest", "bad_window", str(exc))

    digest = audit.build_digest(opts)
    coverage = digest["coverage"]
    absent = sorted(name for name, row in coverage.items() if not row.get("present"))

    # Verdict-free by construction: counts and coverage only. The rubric that
    # turns these into NOTIFY/ATTENTION/QUIET lives in the skill, never here.
    counts = {
        "sessions": digest["volume"]["total"]["sessions"],
        "tool_calls": digest["volume"]["total"]["tool_calls"],
        "shell_commands": digest["volume"]["total"]["shell_commands"],
        "file_writes": digest["volume"]["total"]["file_writes"],
        "file_writes_outside_allowed_roots": sum(
            1 for w in digest["file_writes"] if w.get("inside_allowed_roots") is False
        ),
        "sensitive_reads": len(digest["sensitive_reads"]),
        "network_calls": len(digest["network"]),
        "network_calls_not_allowed": sum(1 for n in digest["network"] if n.get("allowed") is False),
        "permission_flags": len(digest["permission_flags"]),
        "spawns_seen": digest["spawns"]["seen_in_transcripts"],
        "spawns_without_launch_id": digest["spawns"]["without_launch_id"],
        "gate_refusals": digest["spawns"]["gate_refusals"],
        "doctor_fail": digest["doctor"]["counts"].get("fail", 0),
        "doctor_warn": digest["doctor"]["counts"].get("warn", 0),
        "tagged_commands": sum(1 for c in digest["shell_commands"] if c.get("tags")),
    }

    result: dict = {
        "window": digest["window"],
        "digest_sha256": digest["digest_sha256"],
        "counts": counts,
        "coverage": {name: row.get("present", False) for name, row in coverage.items()},
        "sources_absent": absent,
        "out_path": None,
    }

    if args.out:
        try:
            written = audit.write_digest(digest, Path(args.out))
        except OSError as exc:
            return error_envelope(
                "obs.audit-digest", "out_write_failed", f"could not write {args.out}: {exc}",
                details={"digest_sha256": digest["digest_sha256"]},
            )
        result["out_path"] = str(written)
    else:
        # No --out: the digest itself rides in the envelope, which is what the
        # host wrapper reads off stdout.
        result["digest"] = digest

    next_actions = []
    if not coverage["transcripts"].get("present"):
        next_actions.append(
            next_action(
                ["trialerror", "obs", "audit-digest", "--since", str(args.since), "--transcripts", "<DIR>"],
                "no transcripts were found -- point the digest at the right directory (a missing source is itself a finding)",
            )
        )
    if not coverage["history"].get("present"):
        next_actions.append(
            next_action(
                ["trialerror", "obs", "audit-digest", "--since", str(args.since), "--history", "<FILE>"],
                "no shell history was read -- name the persisted history file",
            )
        )
    elif not coverage["history"].get("windowed", True):
        # Present but undated: --since could not be applied to a single line of
        # it, and everything past the tail was dropped. That is a PARTIAL source
        # and the reader has to know before treating it as coverage.
        next_actions.append(
            next_action(
                ["bash", "-lc", "export HISTTIMEFORMAT='%s '  # zsh: setopt EXTENDED_HISTORY"],
                "the shell history carries no timestamps, so the window could not be applied to it "
                "and only its most recent lines are in this digest -- date it on the machine that writes it",
            )
        )
    if not coverage["events"].get("present") or not coverage["doctor"].get("present"):
        next_actions.append(
            next_action(
                ["trialerror", "obs", "audit-digest", "--since", str(args.since), "--program-root", "<DIR>"],
                "event rows and the doctor need a program root",
            )
        )
    if opts.previous_dir is None:
        next_actions.append(
            next_action(
                ["trialerror", "obs", "audit-digest", "--since", str(args.since), "--previous-dir", "<DIR>"],
                "supply earlier digests so the volume medians the rubric compares against are populated",
            )
        )
    return ok_envelope("obs.audit-digest", result=result, next_actions=next_actions)


def run(args: argparse.Namespace) -> dict:
    """The ``obs`` group's own default handler -- reached only when no
    subcommand was given."""
    return error_envelope(
        "obs", "no_subcommand", "specify a subcommand: status, start-phoenix, smoke, audit-digest"
    )
