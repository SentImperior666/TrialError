"""``trialerror.toml`` loader. Design Section 3.2 (per-program scaffold):
"``trialerror.toml`` — program id, id-prefixes, model policy, license posture,
paths."

M0 ships the loader + program-root discovery only; the fields it exposes
are read generically (as a dict) since the modules that actually consume
``[models]``/``[license]``/``[id_prefixes]`` land later (M3 model policy,
M7 license posture, M1 id-prefix pinning). Uses stdlib ``tomllib``
(py>=3.11, matches the design's ``py>=3.11`` package requirement) — zero
new dependency for reading.
"""

from __future__ import annotations

import os
import platform
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "ConfigError",
    "ProgramConfig",
    "ProgramRootIsHarnessError",
    "load_config",
    "find_program_root",
    "resolve_configured_path",
    "configured_path_value",
    "foreign_absolute_kind",
    "resolve_program_id",
]

CONFIG_FILENAME = "trialerror.toml"

#: L8 part F (F1): the directory that holds the *running* ``trialerror``
#: package -- ``trialerror/util/config.py`` -> ``trialerror/util`` ->
#: ``trialerror`` -> its parent, the checkout (or worktree) root. Computed
#: once at import time from this module's own ``__file__`` (not
#: ``sys.modules["trialerror"].__file__``) so a test can monkeypatch it
#: directly to point at a fixture directory without needing to fake an
#: import.
_HARNESS_PACKAGE_PARENT = Path(__file__).resolve().parent.parent.parent

_PROGRAM_ROOT_ENV = "TRIALERROR_PROGRAM_ROOT"
_ALLOW_HARNESS_PROGRAM_ROOT_ENV = "TRIALERROR_ALLOW_HARNESS_PROGRAM_ROOT"


class ConfigError(Exception):
    """Raised for a missing, unreadable, or structurally invalid trialerror.toml."""


class ProgramRootIsHarnessError(ConfigError):
    """Raised by :func:`find_program_root` (L8 part F, F1) when no program
    root was given (no ``--program-root``, no ``TRIALERROR_PROGRAM_ROOT``)
    and the walked-up fallback lands on the repository that holds the
    running ``trialerror`` package -- its root, or a git worktree of it: a
    ``trialerror.toml`` next to ``trialerror/__init__.py``.

    DEV's ``research-harness/stores/ops.db``, the store behind the harness
    repository's own ``trialerror.toml``, was already at ops v13 before
    batch 6 landed -- something running a lane's code had opened it as its
    program root by accident. It was additive and did no harm, but the
    fallback is refused from here on."""

    code = "program_root_is_harness"

    def __init__(self, path: Path):
        self.path = path
        super().__init__(
            "no program root was given, and the fallback is the harness's own repository; "
            "pass --program-root or set TRIALERROR_PROGRAM_ROOT (set "
            "TRIALERROR_ALLOW_HARNESS_PROGRAM_ROOT=1 to use it on purpose)"
        )


@dataclass(frozen=True)
class ProgramConfig:
    program_id: str
    path: Path
    raw: dict[str, Any]

    @property
    def id_prefixes(self) -> dict[str, Any]:
        return self.raw.get("id_prefixes", {})

    @property
    def models(self) -> dict[str, Any]:
        return self.raw.get("models", {})

    @property
    def model_classes(self) -> dict[str, Any]:
        """``[model_classes]`` — model NAME to class, the other half of the
        policy pair. ``[models]`` says which class a purpose needs;
        ``[model_classes]`` says which class a given model IS, so the spawn
        gate can compare the model a subagent was actually spawned with
        against the class its booking claimed. Optional: entries here extend
        and override ``trialerror.budget.policy``'s built-in family map, which
        is exactly what a program running a model that map has never heard
        of needs — and is the escape hatch the gate's refusal names."""
        return self.raw.get("model_classes", {})

    @property
    def license_posture(self) -> dict[str, Any]:
        return self.raw.get("license", {})

    @property
    def paths(self) -> dict[str, Any]:
        return self.raw.get("paths", {})

    @property
    def budget(self) -> dict[str, Any]:
        """``[budget]`` — knobs for the money surfaces. Today:
        ``quota_max_age_s`` (lane FB-3 item 8), the freshness bar the plan-
        quota capture is judged against by ``budget quota``, the booking gate
        and the ``quota_capture_stale`` doctor check. Read generically, like
        every other table here; absence means "the documented default"."""
        return self.raw.get("budget", {})

    @property
    def packet(self) -> dict[str, Any]:
        """``[packet]`` — the weekly decision packet's knobs, all optional:
        ``dir`` (default ``packet/`` under the program root), ``max_minutes``
        (30), ``notify_cmd`` (a command list; the title and body are appended
        as its last two arguments), ``link`` (where the operator reads the
        packet), ``remind_after_days`` (3) and ``course_file``. Read
        generically, like ``budget``; ``trialerror.packet.store.packet_settings``
        applies the defaults and validates the values."""
        return self.raw.get("packet", {})


def load_config(path: str | Path) -> ProgramConfig:
    """Load and minimally validate a ``trialerror.toml`` file."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"trialerror.toml not found: {path}")
    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc

    program = raw.get("program")
    if not isinstance(program, dict) or not program.get("id"):
        raise ConfigError(f"{path}: missing required [program] table with an 'id' field")

    return ProgramConfig(program_id=str(program["id"]), path=path, raw=raw)


def _harness_repo_root() -> Path | None:
    """The checkout/worktree root that holds the running ``trialerror``
    package, when it looks like one (a ``trialerror/__init__.py`` under
    :data:`_HARNESS_PACKAGE_PARENT`). ``None`` for an installed package with
    no such sibling (nothing to ever match against)."""
    parent = _HARNESS_PACKAGE_PARENT
    if (parent / "trialerror" / "__init__.py").is_file():
        return parent
    return None


def find_program_root(start: str | Path | None = None, *, refuse_harness: bool = True) -> Path | None:
    """Walk up from ``start`` (default: CWD) looking for a ``trialerror.toml``.
    Returns the containing directory, or ``None`` if none is found before
    the filesystem root.

    ``TRIALERROR_PROGRAM_ROOT`` overrides the walk-up entirely and is
    returned as-is: it was *given*, not discovered, so it is never subject
    to the refusal below (lane brief rule 6).

    L8 part F (F1): when ``refuse_harness`` (the default) and the walked-up
    result is the repository that holds the running ``trialerror`` package,
    raises :class:`ProgramRootIsHarnessError` instead of returning it --
    unless ``TRIALERROR_ALLOW_HARNESS_PROGRAM_ROOT=1`` says this is on
    purpose. A caller for which a program root is optional (for example
    ``trialerror probes status``, which degrades gracefully with ``None``)
    passes ``refuse_harness=False`` to opt out: the fallback landing there is
    not itself a problem when nothing is written through it on that
    assumption alone."""
    env_override = os.environ.get(_PROGRAM_ROOT_ENV)
    if env_override:
        return Path(env_override)
    cur = Path(start if start is not None else Path.cwd()).resolve()
    found = None
    for candidate in (cur, *cur.parents):
        if (candidate / CONFIG_FILENAME).is_file():
            found = candidate
            break
    if found is None:
        return None
    if refuse_harness and os.environ.get(_ALLOW_HARNESS_PROGRAM_ROOT_ENV) != "1":
        harness_root = _harness_repo_root()
        if harness_root is not None and found == harness_root.resolve():
            raise ProgramRootIsHarnessError(found)
    return found


def resolve_program_id(program_root: Path | str) -> str:
    """``program_root``'s own declared ``[program].id`` (``trialerror.toml``
    directly under it), or ``str(program_root)`` as a fallback when there is
    no ``trialerror.toml`` there or it cannot be parsed.

    L3 fix round, B-2: a probe result recorded in the machine-wide
    ``platform.db`` needs to say WHICH program it is about (``probe_run.
    program_id``) so the answer stamp can scope "is search degraded" to one
    program instead of averaging every program a host happens to run --
    this is the one place that resolution happens, so every caller
    (``trialerror.hooks.session_start``, ``trialerror.retrieve.handlers``,
    ``trialerror.cli.probes``) agrees on the same id for the same root."""
    program_root = Path(program_root)
    cfg_path = program_root / CONFIG_FILENAME
    if cfg_path.is_file():
        try:
            return load_config(cfg_path).program_id
        except ConfigError:
            pass
    return str(program_root)


#: the import-design notes (internal, not in this export) Sec 2/5 (C-0067(c)(i)): the ``[paths]`` table in a
#: shipped ``trialerror.toml`` was dead code for 6 of the design's 7 scaffold
#: output dirs -- only ``trialerror.ingest.pipeline.resolve_ingest_roots``'s own
#: ``[paths].ingest_roots`` handling actually read it. The two helpers below
#: are the ONE place "accept an absolute override, or a program-root-relative
#: one, falling back to the current hardcoded literal" lives, so every other
#: knob (``stores_dir``, ``archive_dir``, ``law_digest_path``,
#: ``handoffs_dir``, ``requests_path``, ``memory_dir``) can share this
#: instead of re-deriving ``resolve_ingest_roots``'s per-item logic. ``config``
#: is always the plain ``ProgramConfig.raw`` dict (or ``None``) -- never a
#: ``ProgramConfig`` instance -- matching every other consumer in this
#: codebase (``trialerror.ingest.pipeline``, ``trialerror.budget.policy``, ...).


def configured_path_value(config: Mapping[str, Any] | None, key: str, default: str) -> str:
    """The raw ``[paths].<key>`` trialerror.toml string, or ``default`` when the
    table/key is absent -- exactly as the user wrote it (absolute or
    program-root-relative), unresolved. Callers that need a value to STORE
    (e.g. a DB row's own ``rel_path``-style column, so a later read that
    joins it onto ``program_root`` stays correct even for an absolute
    override -- pathlib's own ``Path.__truediv__`` treats an absolute
    right-hand operand as replacing the left) want this raw string, not a
    pre-joined :func:`Path`.

    "Absolute", though, is decided by the *running* platform, which is the
    subtlety :func:`resolve_configured_path` has to defend against -- see
    its docstring."""
    paths_cfg = (config or {}).get("paths", {}) or {}
    return str(paths_cfg.get(key, default))


#: A drive-letter path (``C:\\x``, ``C:/x``) or a UNC share (``\\\\host\\share``).
_WINDOWS_ABSOLUTE_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def foreign_absolute_kind(value: str) -> str | None:
    """Name the platform a value is absolute *for* when that is not the
    platform we are running on; ``None`` when the value is fine here.

    This exists because :class:`pathlib.Path` resolves "is this absolute?"
    against the host, so a path written for the other OS is not merely
    unusable -- it is silently reclassified as *relative* and joined onto
    the program root. There is no error, just a wrong path.
    """
    if os.name == "nt":
        # A POSIX-absolute value on Windows: PureWindowsPath("/srv/x") has no
        # drive, so .is_absolute() is False and the join silently proceeds.
        if value.startswith("/") and not _WINDOWS_ABSOLUTE_RE.match(value):
            return "POSIX"
        return None
    if _WINDOWS_ABSOLUTE_RE.match(value):
        return "Windows"
    return None


def resolve_configured_path(
    program_root: Path | str, config: Mapping[str, Any] | None, key: str, default: str
) -> Path:
    """:func:`configured_path_value` joined onto ``program_root`` -- for a
    caller that just needs a concrete filesystem :class:`Path` (a directory
    to write into, an env default), not a string to persist.

    Refuses a value that is absolute for the *other* platform. Without this
    check the failure is silent and confusing: on Linux,
    ``Path("C:/research/corpus").is_absolute()`` is ``False``, so a config
    copied from the Windows-era docs resolved to
    ``<program_root>/C:/research/corpus`` -- a real directory, created
    without complaint, in the wrong place. Raising names the problem at the
    one point where it is still cheap to fix.
    """
    raw = configured_path_value(config, key, default)
    foreign = foreign_absolute_kind(raw)
    if foreign is not None:
        raise ConfigError(
            f"[paths].{key} = {raw!r} is an absolute {foreign} path, but this is "
            f"{platform.system() or os.name}. Joining it onto the program root would "
            f"silently produce a wrong path under {program_root}. Use a path that is "
            f"absolute on this platform, or a program-root-relative one."
        )
    return Path(program_root) / raw
