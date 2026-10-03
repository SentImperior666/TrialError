"""The probe registry and runner (design Section 3.1).

Copies :mod:`trialerror.util.doctor`'s decorator-registry pattern (and
:mod:`trialerror.jobs.registry`'s directory-convention auto-discovery), with
its own time-bounded runner: each probe runs on a daemon thread and is
joined with a per-probe timeout, because doctor's own "run everything, take
as long as it takes" contract is exactly what a probe must NOT inherit
(module docstring).
"""

from __future__ import annotations

import importlib
import json
import pkgutil
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from trialerror.util.build import build_id
from trialerror.util.timeutil import now

__all__ = [
    "ProbeResult",
    "ProbeContext",
    "ProbeFn",
    "ProbeRunRow",
    "register_probe",
    "registered_probes",
    "clear_registry",
    "discover_and_register_probes",
    "run_probes",
    "run_probe_inline",
]

_STATUSES = ("pass", "warn", "fail", "skip", "error")


@dataclass(frozen=True)
class ProbeResult:
    status: str
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in _STATUSES:
            raise ValueError(f"ProbeResult.status must be one of {_STATUSES!r}, got {self.status!r}")


@dataclass
class ProbeContext:
    """Everything a probe needs, matching design Section 3.1: "host,
    program_root (optional), store (optional), platform_store and the
    probes directory". ``store`` is the program's own :class:`~trialerror.
    stores.store.Store` (ops/knowledge/jobs), when the probe needs it;
    ``platform_store`` is separate because a conformance/canary result is
    recorded in ``platform.db`` regardless of whether a program store was
    even given (mirrors :mod:`trialerror.units.scan`'s own platform-only
    write path)."""

    host: str
    platform_store: Any
    program_root: Path | None = None
    program_id: str | None = None
    store: Any | None = None
    probes_dir: Path | None = None
    cc_version: str | None = None
    live: bool = False


ProbeFn = Callable[[ProbeContext], ProbeResult]


@dataclass(frozen=True)
class _ProbeSpec:
    name: str
    kind: str
    timeout_s: float
    fn: ProbeFn


@dataclass(frozen=True)
class ProbeRunRow:
    name: str
    kind: str
    host: str
    program_id: str | None
    cc_version: str | None
    build: str | None
    started_ts: str
    finished_ts: str | None
    status: str
    detail: dict[str, Any]


_REGISTRY: dict[str, _ProbeSpec] = {}


def register_probe(name: str, *, kind: str, timeout_s: float) -> Callable[[ProbeFn], ProbeFn]:
    """Decorator: register ``fn`` as the probe named ``name``.

    Usage (in ``trialerror/<subsystem>/probes.py``)::

        from trialerror.probes.registry import ProbeContext, ProbeResult, register_probe

        @register_probe("cc_version_seen", kind="conformance", timeout_s=2.0)
        def probe_cc_version_seen(ctx: ProbeContext) -> ProbeResult:
            ...
    """
    if kind not in ("conformance", "canary", "drill"):
        raise ValueError(f"register_probe: kind must be conformance/canary/drill, got {kind!r}")

    def deco(fn: ProbeFn) -> ProbeFn:
        _REGISTRY[name] = _ProbeSpec(name=name, kind=kind, timeout_s=timeout_s, fn=fn)
        return fn

    return deco


def registered_probes() -> dict[str, tuple[str, float]]:
    """A snapshot of the registry: ``{name: (kind, timeout_s)}``."""
    return {name: (spec.kind, spec.timeout_s) for name, spec in _REGISTRY.items()}


def clear_registry() -> None:
    """Test-only: reset the registry to empty."""
    _REGISTRY.clear()


#: Subpackages skipped by discovery even though they are direct subpackages
#: of ``trialerror`` (S-4, fix round). ``trialerror.cli`` has its own
#: ``trialerror/cli/probes.py`` -- the ``probes`` CLI GROUP module, not a
#: probe-definition module -- so a naive "import every subpackage's
#: ``.probes``" reloads the CLI's own caller module in place every time
#: discovery runs. ``importlib.reload`` re-executes the module and rebinds
#: every top-level name in it (``run_live_capture``, ``detect_cc_version``,
#: ...) to fresh function objects, silently undoing any ``monkeypatch.
#: setattr`` a test (or, in principle, a caller) had applied to them --
#: this is the confirmed root cause of the "a patch on run_live_capture did
#: not survive `_run_run`" anomaly from the previous fix round (deviation
#: 7). ``trialerror.probes`` is excluded too, defensively: it is this
#: registry's OWN package, and it must never import itself as a side effect
#: of discovery even though it has no ``probes.py`` submodule today.
_EXCLUDED_SUBPACKAGES = frozenset({"cli", "probes"})


def discover_and_register_probes(root_package: str = "trialerror") -> list[str]:
    """Import ``<subpackage>.probes`` for every direct subpackage of
    ``root_package`` that has one -- the same directory-convention
    auto-discovery :func:`trialerror.util.doctor.discover_and_register_checks`
    uses for doctor checks, applied to probe FUNCTIONS instead. Skips
    :data:`_EXCLUDED_SUBPACKAGES` (S-4): those names are infrastructure, not
    probe-definition homes, and importing/reloading them as a side effect of
    discovery is never correct."""
    imported: list[str] = []
    pkg = importlib.import_module(root_package)
    pkg_path = getattr(pkg, "__path__", None)
    if pkg_path is None:
        return imported
    for _finder, modname, ispkg in pkgutil.iter_modules(pkg_path, prefix=f"{root_package}."):
        if not ispkg:
            continue
        short_name = modname.rsplit(".", 1)[-1]
        if short_name in _EXCLUDED_SUBPACKAGES:
            continue
        probes_modname = f"{modname}.probes"
        try:
            if probes_modname in sys.modules:
                importlib.reload(sys.modules[probes_modname])
            else:
                importlib.import_module(probes_modname)
        except ModuleNotFoundError:
            continue
        imported.append(probes_modname)
    return imported


def _run_one(spec: _ProbeSpec, ctx: ProbeContext) -> tuple[str, dict[str, Any]]:
    """Run ``spec`` on a daemon thread, joined with ``spec.timeout_s``.

    A timed-out probe's THREAD is not killed -- Python has no supported way
    to do that -- it is merely no longer waited on. The daemon thread keeps
    running in the background, on the SAME ``ctx`` (and its connections)
    the caller goes on to use for whatever comes next (S-1: SessionStart
    reads the boot bundle and closes the store while a timed-out canary
    thread may still be mid-query on that same connection). This is
    harmless for today's probes -- every one of them is read-only -- but a
    future WRITE-performing probe would need its own connection, not
    ``ctx``'s, or this runner would need real cancellation."""
    outcome: dict[str, Any] = {}

    def _target() -> None:
        try:
            outcome["result"] = spec.fn(ctx)
        except Exception as exc:  # noqa: BLE001 - isolate one probe's failure from the rest
            outcome["exc"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(spec.timeout_s)

    if thread.is_alive():
        return "error", {"reason": "timeout", "timeout_s": spec.timeout_s}
    if "exc" in outcome:
        exc = outcome["exc"]
        return "error", {"reason": f"{type(exc).__name__}: {exc}"}
    result = outcome.get("result")
    if result is None:
        return "error", {"reason": "probe returned no result"}
    return result.status, result.detail


def _record(ctx: ProbeContext, row: ProbeRunRow) -> None:
    from trialerror.stores.writer import insert

    insert(
        ctx.platform_store,
        "probe_run",
        {
            "name": row.name,
            "kind": row.kind,
            "host": row.host,
            "program_id": row.program_id,
            "cc_version": row.cc_version,
            "build": row.build,
            "started_ts": row.started_ts,
            "finished_ts": row.finished_ts,
            "status": row.status,
            "detail": json.dumps(row.detail, ensure_ascii=False),
        },
    )


def run_probe_inline(ctx: ProbeContext, name: str) -> ProbeRunRow:
    """Run ONE registered probe directly on the CALLING thread -- no timeout
    thread, so no requirement that ``ctx``'s connections tolerate
    cross-thread use (S-1/B-1's job-handler fix). For a caller that already
    bounds its own runtime some other way -- a job's own lease/heartbeat
    timeout (:mod:`trialerror.retrieve.handlers`'s ``probe_vector_canary``
    handler) -- rather than a caller wanting the registry's own per-probe
    timeout (:func:`run_probes`, used by ``SessionStart``'s synchronous
    full-text canary, which DOES need that timeout).

    Raises :class:`KeyError` for an unregistered ``name`` -- a caller here
    already knows exactly which probe it wants to run and a typo should be
    loud, unlike :func:`run_probes`'s "no matching probes" empty-list
    contract for a ``kind``/``names`` filter that may legitimately match
    nothing."""
    spec = _REGISTRY.get(name)
    if spec is None:
        raise KeyError(f"no such registered probe: {name!r} (registered: {sorted(_REGISTRY)!r})")

    started = now()
    try:
        result = spec.fn(ctx)
        status, detail = result.status, result.detail
    except Exception as exc:  # noqa: BLE001 - mirrors _run_one's isolation
        status, detail = "error", {"reason": f"{type(exc).__name__}: {exc}"}
    finished = now()

    row = ProbeRunRow(
        name=spec.name, kind=spec.kind, host=ctx.host, program_id=ctx.program_id,
        cc_version=ctx.cc_version, build=build_id(), started_ts=started, finished_ts=finished,
        status=status, detail=detail,
    )
    if ctx.platform_store is not None:
        _record(ctx, row)
    return row


def run_probes(
    ctx: ProbeContext, *, kind: str | None = None, names: list[str] | None = None
) -> list[ProbeRunRow]:
    """Run every registered probe matching ``kind``/``names`` (default: every
    registered probe), each under its own timeout, and record one
    ``probe_run`` row per probe when ``ctx.platform_store`` is given."""
    names_set = set(names) if names else None
    specs = [
        s
        for s in _REGISTRY.values()
        if (kind is None or s.kind == kind) and (names_set is None or s.name in names_set)
    ]
    specs.sort(key=lambda s: s.name)

    rows: list[ProbeRunRow] = []
    build = build_id()
    for spec in specs:
        started = now()
        status, detail = _run_one(spec, ctx)
        finished = now()
        row = ProbeRunRow(
            name=spec.name, kind=spec.kind, host=ctx.host, program_id=ctx.program_id,
            cc_version=ctx.cc_version, build=build, started_ts=started, finished_ts=finished,
            status=status, detail=detail,
        )
        rows.append(row)
        if ctx.platform_store is not None:
            _record(ctx, row)
    return rows
