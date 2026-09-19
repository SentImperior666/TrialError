"""Job-handler registry + auto-discovery. Mirrors
``trialerror.util.doctor``'s ``@register_check``/``discover_and_register_checks``
convention exactly (design Section 5.2's directory-convention
auto-discovery, applied here to job BODIES instead of doctor checks): a
subsystem drops a ``trialerror/<subsystem>/handlers.py`` that calls
``@register_handler`` and the worker loop finds it with zero shared-file
edits -- the mechanism M7 needs to register ``ocr``/``embed``/``index``/
``extract`` job bodies onto M2's ledger without either module importing
the other directly (per the build brief: "M7 (ingestion workers) ride
your ledger").

Deliberately mirrors, rather than reuses, ``trialerror.util.doctor``'s registry
machinery: doctor's registry is keyed by check *name* against a fixed
``category`` vocabulary that has nothing to do with a job's ``kind``, and
handlers additionally need an explicit-module escape hatch
(:func:`discover_and_register_handlers` is auto-discovery for SHIPPED
subsystems; the CLI's ``--handler-module`` flag and
``trialerror.jobs.worker.spawn_worker``'s ``extra_handler_modules`` are for
ad hoc/test handlers that must never live under ``trialerror/``) that doctor
checks have no equivalent need for.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from typing import TYPE_CHECKING, Callable

from trialerror.jobs.errors import UnknownHandlerError

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a runtime import cycle
    from trialerror.jobs.worker import JobContext

HandlerFn = Callable[["JobContext"], None]

__all__ = [
    "HandlerFn",
    "register_handler",
    "get_handler",
    "registered_handlers",
    "clear_registry",
    "discover_and_register_handlers",
]

_REGISTRY: dict[str, HandlerFn] = {}


def register_handler(name: str) -> Callable[[HandlerFn], HandlerFn]:
    """Decorator: register ``fn`` as the job handler named ``name``.

    Usage (in ``trialerror/<subsystem>/handlers.py``)::

        from trialerror.jobs.registry import register_handler

        @register_handler("embed")
        def run_embed(ctx):
            ...
    """

    def deco(fn: HandlerFn) -> HandlerFn:
        _REGISTRY[name] = fn
        return fn

    return deco


def get_handler(name: str) -> HandlerFn:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise UnknownHandlerError(
            f"no job handler registered for {name!r} (registered: {sorted(_REGISTRY)!r})"
        ) from None


def registered_handlers() -> dict[str, HandlerFn]:
    """A snapshot of the current registry: ``{name: fn}``."""
    return dict(_REGISTRY)


def clear_registry() -> None:
    """Test-only: reset the registry to empty."""
    _REGISTRY.clear()


def discover_and_register_handlers(root_package: str = "trialerror") -> list[str]:
    """Import ``<subpackage>.handlers`` for every direct subpackage of
    ``root_package`` that has one -- the same directory-convention
    auto-discovery ``trialerror.util.doctor.discover_and_register_checks`` uses
    for doctor checks, applied to job handlers. Idempotent by construction
    (a module already imported is *reloaded*, matching doctor's own
    behavior) so re-running it mid-process (a worker loop calling it once
    per claimed job, cheaply, is intentional -- see
    ``trialerror.jobs.worker.run_one``) never leaves a stale, partially-cleared
    registry.
    """
    imported: list[str] = []
    pkg = importlib.import_module(root_package)
    pkg_path = getattr(pkg, "__path__", None)
    if pkg_path is None:
        return imported
    for _finder, modname, ispkg in pkgutil.iter_modules(pkg_path, prefix=f"{root_package}."):
        if not ispkg:
            continue
        handlers_modname = f"{modname}.handlers"
        try:
            if handlers_modname in sys.modules:
                importlib.reload(sys.modules[handlers_modname])
            else:
                importlib.import_module(handlers_modname)
        except ModuleNotFoundError:
            continue
        imported.append(handlers_modname)
    return imported
