"""M12's doctor checks: ``obs_exporter_reachable``, ``obs_span_drop_
counter``. Auto-discovered by ``trialerror.util.doctor.discover_and_register_
checks`` exactly like M2's ``trialerror/jobs/checks.py`` -- dropping this file
is the entire registration step (M2 integration contract: "doctor checks
in trialerror/obs/checks.py (exporter-reachable, span-drop counter)").

Both checks report ``skip`` -- never ``fail`` -- for every "tracing isn't
actually live right now" reason (deps absent, program_root not given,
Phoenix not running): the whole point of the obs seed is that NONE of that
is ever a hard failure elsewhere in the harness, and the doctor surface
that reports on it shouldn't contradict that by failing the build over an
optional local dev tool being off. ``warn`` is reserved for "tracing IS
configured to be live and isn't working" -- an actionable, non-fatal signal
the same way ``stale_lease``/``heartbeat_age`` use it.

build-v2-polish adds a third check, ``disk_growth`` (O-1 from the
accumulated flag list) -- visibility-only disk-size reporting for
``jobs_logs/``/``jobs_work/``/``phoenix_serve.log`` against a configurable
``[doctor].disk_warn_mb`` threshold, ``warn``/``pass`` only, no
auto-pruning. Unrelated in mechanism to the two OTel-specific checks above
(no ``tracer``/``state`` dependency) but kept in this file rather than a
new one -- it shares this module's doctor-registration boilerplate and its
one non-obs-owned surface (``phoenix_serve.log``) is still obs's own log.
"""

from __future__ import annotations

from pathlib import Path

from trialerror.obs import state, tracer
from trialerror.stores import paths
from trialerror.util.doctor import CheckResult, DoctorContext, register_check
from trialerror.util.timeutil import now

__all__ = ["check_exporter_reachable", "check_span_drop_counter", "check_disk_growth"]

#: Short -- this is a doctor health probe, not a trace emission; it must
#: never itself become the thing a slow `trialerror doctor` run is waiting on.
_PROBE_TIMEOUT_S = 0.5


@register_check("obs_exporter_reachable", category="obs")
def check_exporter_reachable(ctx: DoctorContext) -> CheckResult:
    """A plain TCP connect probe against the configured OTLP/HTTP endpoint
    host:port -- deliberately NOT an OTel/HTTP round trip (that would
    require the ``obs`` extra just to run a doctor check on it). Reachable
    only tells you Phoenix's collector port is accepting connections, not
    that a POSTed span will be accepted -- good enough for "is the local
    trace sink up", which is what this check exists to answer. The probe
    itself is :func:`trialerror.obs.tracer.probe_reachable` (build-v2-polish O-4:
    factored out so ``trialerror obs start-phoenix``'s own idempotent-start check
    shares this exact probe, not a second copy of it)."""
    if not tracer.is_available():
        return CheckResult(
            name="obs_exporter_reachable",
            category="obs",
            status="skip",
            message="opentelemetry-sdk / otlp-http exporter not installed (the 'obs' extra) -- tracing no-ops",
        )

    endpoint = tracer.resolve_endpoint()
    if not tracer.probe_reachable(endpoint, timeout_s=_PROBE_TIMEOUT_S):
        return CheckResult(
            name="obs_exporter_reachable",
            category="obs",
            status="warn",
            message=f"Phoenix OTLP endpoint {endpoint!r} not reachable; spans will drop silently (by design)",
            details={"endpoint": endpoint},
        )
    return CheckResult(
        name="obs_exporter_reachable",
        category="obs",
        status="pass",
        message=f"Phoenix OTLP endpoint {endpoint!r} reachable",
        details={"endpoint": endpoint},
    )


@register_check("obs_span_drop_counter", category="obs")
def check_span_drop_counter(ctx: DoctorContext) -> CheckResult:
    """Reads the persisted counter ``trialerror.obs.state.record_span_drop``
    writes on every export failure. See that module's docstring for why
    this needs a file at all (the writer and the reader are almost always
    different process invocations)."""
    if ctx.program_root is None:
        return CheckResult(
            name="obs_span_drop_counter",
            category="obs",
            status="skip",
            message="no program_root configured (top-level `trialerror doctor` doesn't pass one yet -- INTEGRATION_NOTES item 5)",
        )
    if not (ctx.program_root / "obs").exists():
        return CheckResult(
            name="obs_span_drop_counter",
            category="obs",
            status="skip",
            message="no obs/ state under this program root yet (tracing not configured with a program_root here, or never dropped a span)",
        )
    drop_state = state.read_span_drop_state(ctx.program_root)
    count = drop_state.get("count", 0)
    if count:
        return CheckResult(
            name="obs_span_drop_counter",
            category="obs",
            status="warn",
            message=f"{count} span(s) dropped; last at {drop_state.get('last_ts')}: {drop_state.get('last_reason')}",
            details=drop_state,
        )
    return CheckResult(
        name="obs_span_drop_counter", category="obs", status="pass", message="no dropped spans recorded", details=drop_state
    )


#: O-1 sane default warn threshold (MB), used when a program has no
#: ``[doctor].disk_warn_mb`` configured -- generous enough that a normal
#: dev cycle's jobs_logs/jobs_work/phoenix_serve.log growth won't nag,
#: tight enough to catch a runaway (a stuck OCR loop re-writing the same
#: work dir, a Phoenix log nobody ever rotated).
_DEFAULT_DISK_WARN_MB = 500.0


def _disk_warn_mb(ctx: DoctorContext) -> float:
    """Best-effort ``[doctor].disk_warn_mb`` read from
    ``<program_root>/trialerror.toml`` -- same private-per-module loader
    convention every other doctor ``checks.py`` uses for its own config
    reads (e.g. ``trialerror.ingest.checks._active_model_key``,
    ``trialerror.stores.checks._load_paths_config``). Missing/invalid
    ``trialerror.toml``, or no ``program_root`` at all -> the sane default."""
    if ctx.program_root is not None:
        from trialerror.util.config import CONFIG_FILENAME, load_config

        cfg_path = ctx.program_root / CONFIG_FILENAME
        if cfg_path.is_file():
            try:
                raw = load_config(cfg_path).raw
                return float(raw.get("doctor", {}).get("disk_warn_mb", _DEFAULT_DISK_WARN_MB))
            except Exception:
                pass
    return _DEFAULT_DISK_WARN_MB


def _path_size_bytes(path: Path) -> int:
    """Total bytes under ``path`` -- 0 if it doesn't exist, the file's own
    size if it's a plain file (``phoenix_serve.log``), or a recursive sum
    over every regular file if it's a directory (``jobs_logs/``,
    ``jobs_work/``). A file that vanishes mid-walk (a race against a live
    worker) is skipped, not fatal -- this is a visibility check, not an
    integrity one."""
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                continue
    return total


@register_check("disk_growth", category="obs")
def check_disk_growth(ctx: DoctorContext) -> CheckResult:
    """O-1: visibility-only (never ``fail``, only ``warn``/``pass``) size
    report for the three unbounded-growth surfaces this harness writes
    during normal operation -- ``<program_root>/jobs_logs/`` (per-worker
    attempt logs, ``trialerror.jobs.worker.spawn_worker``'s own default
    ``log_dir``), ``<program_root>/jobs_work/`` (per-document scratch space,
    e.g. ``trialerror.ingest.handlers``'s own OCR work dir), and
    ``<platform_root>/obs/phoenix_serve.log`` (this package's own ``trialerror
    obs start-phoenix`` log -- platform-scoped, like the rest of Phoenix's
    ``obs/`` state, not per-program). Compared against
    ``[doctor].disk_warn_mb`` (:data:`_DEFAULT_DISK_WARN_MB` if unconfigured
    or no ``program_root`` given). NO auto-pruning anywhere in this module
    -- a human decides what, if anything, to delete."""
    warn_mb = _disk_warn_mb(ctx)
    platform_root = ctx.platform_root if ctx.platform_root is not None else paths.platform_root()

    surfaces: dict[str, dict] = {}
    if ctx.program_root is not None:
        surfaces["jobs_logs"] = {"path": str(ctx.program_root / "jobs_logs")}
        surfaces["jobs_work"] = {"path": str(ctx.program_root / "jobs_work")}
    surfaces["phoenix_serve_log"] = {"path": str(platform_root / "obs" / "phoenix_serve.log")}

    over_threshold: list[str] = []
    for name, info in surfaces.items():
        p = Path(info["path"])
        size_mb = round(_path_size_bytes(p) / (1024 * 1024), 3)
        info["exists"] = p.exists()
        info["size_mb"] = size_mb
        if size_mb > warn_mb:
            over_threshold.append(name)

    status = "warn" if over_threshold else "pass"
    message = (
        f"{len(over_threshold)} disk surface(s) over the {warn_mb}MB warn threshold: {', '.join(over_threshold)}"
        if over_threshold
        else f"all tracked disk surfaces under the {warn_mb}MB warn threshold"
    )
    return CheckResult(
        name="disk_growth",
        category="obs",
        status=status,
        message=message,
        details={"warn_threshold_mb": warn_mb, "checked_ts": now(), "surfaces": surfaces},
    )
