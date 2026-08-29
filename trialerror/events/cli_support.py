"""Shared argv/store-resolution plumbing for the M5 CLI groups
(``trialerror/cli/events.py``, ``trialerror/cli/feed.py``, ``trialerror/cli/inbox.py``).
Lives under ``trialerror/events/`` (in-lane) rather than duplicated three times
across the CLI group files, and rather than touching the shared
``trialerror/cli/__init__.py`` (design Section 5.2 CLI registration rule).

Program-root resolution mirrors ``trialerror.util.config.find_program_root``'s
own convention (walk up from CWD for ``trialerror.toml``); platform-root
resolution is deliberately left to ``trialerror.stores.paths.platform_root``'s
``TRIALERROR_PLATFORM_ROOT`` env var rather than a new CLI flag, the same
"env var, not argv" choice that module itself documents (mirrored by M1's
``trialerror/stores/checks.py`` doctor-context handling, which resolves
platform.db independently of any per-check flag too).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from trialerror.stores import open_store
from trialerror.stores.store import Store
from trialerror.util.config import find_program_root

__all__ = ["program_root_argument", "open_program_store", "ProgramRootNotFoundError"]


class ProgramRootNotFoundError(LookupError):
    """Raised by :func:`open_program_store` when no ``--program-root`` was
    given and none could be discovered by walking up from CWD."""


def program_root_argument(parser: argparse.ArgumentParser) -> None:
    """Add the ``--program-root`` flag every action in these CLI groups
    accepts (the sub-subcommand contract: each action parser calls this
    once, mirroring ``trialerror/cli/doctor.py``'s own ``--repo-root`` /
    ``--vendored-root`` override flags).

    FX-12 (``trialerror/cli/__init__.py``'s TRIALERROR-DEV-NOTE): ``default=SUPPRESS``
    so an unset value here never overwrites the global ``--program-root``
    the top-level parser resolved (`trialerror --program-root X events append
    ...` now works the same as the historical `trialerror events append
    --program-root X ...`)."""
    parser.add_argument(
        "--program-root",
        default=argparse.SUPPRESS,
        help="program scaffold root (default: discovered by walking up from CWD for trialerror.toml)",
    )


def open_program_store(program_root_arg: str | None) -> Store:
    """Resolve ``program_root`` (explicit arg, else discovered) and open
    its :class:`~trialerror.stores.store.Store`. Raises
    :class:`ProgramRootNotFoundError` — never a bare ``sqlite3``/path
    exception — so CLI handlers can turn it into a clean
    ``error_envelope`` (design Section 5.1 cross-cutting rule: "errors
    returned as structured content ... never exceptions" applies just as
    much to the CLI envelope surface as to MCP)."""
    root = Path(program_root_arg) if program_root_arg else find_program_root()
    if root is None:
        raise ProgramRootNotFoundError(
            "no trialerror.toml found walking up from the current directory, and --program-root not given"
        )
    return open_store(root)
