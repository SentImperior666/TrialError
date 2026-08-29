"""Doctor framework: a per-module check registry. Design Section 5.2
(doctor row): "(top-level) store integrity, XID referential scan, stale
heartbeats, index freshness, anchors_dangling, ``--license-audit`` |
framework + license-audit in M0; each module registers its own checks."

M0 ships this framework plus exactly one check (``license_audit``, in
``trialerror.util.checks``). Later modules (M1's store-integrity scan, M2's
stale-heartbeat scan, M7's ``anchors_dangling``, ...) add their own checks
by dropping a ``checks.py`` submodule into their own package
(``trialerror/<subsystem>/checks.py``) that calls :func:`register_check` — the
SAME auto-discovery-by-convention mechanism ``trialerror.cli`` uses for command
groups, so growing the doctor surface never requires editing this file or
any other shared one.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

__all__ = [
    "DoctorContext",
    "CheckResult",
    "CheckFn",
    "register_check",
    "registered_checks",
    "clear_registry",
    "discover_and_register_checks",
    "run_checks",
]


@dataclass
class DoctorContext:
    """Everything a check needs to know about where to look. Individual
    checks are free to ignore fields that don't apply to them."""

    repo_root: Path = field(default_factory=Path.cwd)
    program_root: Path | None = None
    platform_root: Path | None = None
    vendored_root: Path | None = None

    def resolve_vendored_root(self) -> Path:
        return self.vendored_root if self.vendored_root is not None else self.repo_root / "vendored"


@dataclass
class CheckResult:
    name: str
    category: str
    status: str  # "pass" | "fail" | "warn" | "skip"
    message: str
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "category": self.category,
            "status": self.status,
            "message": self.message,
            "details": self.details,
        }


CheckFn = Callable[[DoctorContext], CheckResult]

_REGISTRY: dict[str, tuple[str, CheckFn]] = {}


def register_check(name: str, *, category: str = "general") -> Callable[[CheckFn], CheckFn]:
    """Decorator: register ``fn`` as the doctor check named ``name``.

    Usage (in ``trialerror/<subsystem>/checks.py``)::

        from trialerror.util.doctor import register_check, CheckResult

        @register_check("store_integrity", category="stores")
        def check_store_integrity(ctx):
            ...
            return CheckResult(name="store_integrity", category="stores",
                                status="pass", message="...")
    """

    def deco(fn: CheckFn) -> CheckFn:
        _REGISTRY[name] = (category, fn)
        return fn

    return deco


def registered_checks() -> dict[str, tuple[str, CheckFn]]:
    """A snapshot of the current registry: ``{name: (category, fn)}``."""
    return dict(_REGISTRY)


def clear_registry() -> None:
    """Test-only: reset the registry to empty."""
    _REGISTRY.clear()


def discover_and_register_checks(root_package: str = "trialerror") -> list[str]:
    """Import ``<subpackage>.checks`` for every direct subpackage of
    ``root_package`` that has one, triggering each module's
    ``@register_check`` decorators as a side effect of import. Returns the
    list of check-module names actually imported.

    This is the same directory-convention auto-discovery ``trialerror.cli`` uses
    for command groups (see ``trialerror/cli/__init__.py``): a new subsystem
    registers checks by adding a file, never by editing this function.

    Idempotent by construction: a checks module already present in
    ``sys.modules`` is *reloaded* rather than silently skipped, so calling
    this after :func:`clear_registry` (long-lived-process reload scenario,
    or simply a second call in the same interpreter) re-runs every
    ``@register_check`` decorator instead of leaving the registry empty.
    """
    imported: list[str] = []
    pkg = importlib.import_module(root_package)
    pkg_path = getattr(pkg, "__path__", None)
    if pkg_path is None:
        return imported
    for _finder, modname, ispkg in pkgutil.iter_modules(pkg_path, prefix=f"{root_package}."):
        if not ispkg:
            continue
        checks_modname = f"{modname}.checks"
        try:
            if checks_modname in sys.modules:
                importlib.reload(sys.modules[checks_modname])
            else:
                importlib.import_module(checks_modname)
        except ModuleNotFoundError:
            continue
        imported.append(checks_modname)
    return imported


def run_checks(ctx: DoctorContext, only: list[str] | None = None) -> list[CheckResult]:
    """Run registered checks (default: all of them, sorted by name) and
    return their results. A check that raises is caught and turned into a
    ``fail`` result — one broken check must never crash the whole doctor
    run."""
    names = list(only) if only else sorted(_REGISTRY)
    results: list[CheckResult] = []
    for name in names:
        if name not in _REGISTRY:
            results.append(
                CheckResult(
                    name=name,
                    category="unknown",
                    status="fail",
                    message=f"no such registered check: {name!r}",
                )
            )
            continue
        category, fn = _REGISTRY[name]
        try:
            result = fn(ctx)
        except Exception as exc:  # noqa: BLE001 - deliberate: isolate check failures
            result = CheckResult(
                name=name,
                category=category,
                status="fail",
                message=f"check raised {type(exc).__name__}: {exc}",
            )
        results.append(result)
    return results
