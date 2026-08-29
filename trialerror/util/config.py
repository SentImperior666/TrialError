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

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "ConfigError",
    "ProgramConfig",
    "load_config",
    "find_program_root",
    "resolve_configured_path",
    "configured_path_value",
]

CONFIG_FILENAME = "trialerror.toml"


class ConfigError(Exception):
    """Raised for a missing, unreadable, or structurally invalid trialerror.toml."""


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
    def license_posture(self) -> dict[str, Any]:
        return self.raw.get("license", {})

    @property
    def paths(self) -> dict[str, Any]:
        return self.raw.get("paths", {})


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


def find_program_root(start: str | Path | None = None) -> Path | None:
    """Walk up from ``start`` (default: CWD) looking for a ``trialerror.toml``.
    Returns the containing directory, or ``None`` if none is found before
    the filesystem root."""
    cur = Path(start if start is not None else Path.cwd()).resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / CONFIG_FILENAME).is_file():
            return candidate
    return None


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
    override -- pathlib's own ``Path.__truediv__`` already treats an
    absolute right-hand operand as replacing the left, no branch needed at
    the call site) want this raw string, not a pre-joined :func:`Path`."""
    paths_cfg = (config or {}).get("paths", {}) or {}
    return str(paths_cfg.get(key, default))


def resolve_configured_path(
    program_root: Path | str, config: Mapping[str, Any] | None, key: str, default: str
) -> Path:
    """:func:`configured_path_value` joined onto ``program_root`` -- for a
    caller that just needs a concrete filesystem :class:`Path` (a directory
    to write into, an env default), not a string to persist."""
    return Path(program_root) / configured_path_value(config, key, default)
