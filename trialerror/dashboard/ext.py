"""Dashboard extension-panel protocol -- program-scoped custom visualizations.

User ruling C-0070 (dashboard scope): custom visualizations are PER-PROJECT
extensions, not core TrialError surfaces. A program that wants a bespoke panel
drops one into its own repository at::

    <program_root>/trialerror_ext/panels/<name>/
        panel.toml    -- manifest: title, nav_group (KNOW|RUN), order,
                          description, min_schema (optional, advisory list
                          of table names the builder expects)
        builder.py     -- exposes build_panel(rostore, program_root) -> dict

Discovery roots at ``program_root`` -- the program the dashboard was told to
serve. There is no cross-program registry, no name-based opt-in, nothing to
toggle: a program with no ``trialerror_ext/panels/`` directory (or none open at
all, ``program_root=None``) surfaces zero extension panels. THIS is what
satisfies C-0070(a)'s "a per-project view loads only when that project is
active" -- by construction, not by a special case anywhere in this module or
in ``trialerror.dashboard.serve``/``trialerror.dashboard.export``. A panel authored for
one program and left sitting in TrialError's own repo by mistake is inert here:
nothing ever discovers it unless it is copied under an ACTIVE program's own
``trialerror_ext/panels/``.

SECURITY NOTE, stated honestly (no fake sandbox): a ``builder.py`` is
imported and executed as plain Python, in-process, with full interpreter
privileges -- exactly like any other module the active program's own
``trialerror.toml``/config already lets it point TrialError at (``[ingest.embed]``'s
``python_exe``/``module_dir``, ``[paths].*``, ...). This is the user's own
trusted repository code, not third-party or untrusted input, and this loader
does not attempt sandboxing: no restricted globals, no subprocess isolation,
no import allowlist. Treat ``trialerror_ext/panels/`` exactly as you would treat
any other script living in your own program repo -- because that is exactly
what it is.

What this module guarantees instead is CRASH ISOLATION, not sandboxing: a
manifest that fails to parse, a ``builder.py`` that fails to import, a
``build_panel`` with the wrong signature, or a ``build_panel`` call that
raises -- none of these ever propagate past this module. Every one of those
outcomes turns into a plain ``{"status": "ext_error", "message": ...}`` dict,
the same JSON shape every other panel builder in :mod:`trialerror.dashboard.data`
already returns for its own non-fatal states (``not_initialized``,
``invariant_violation``, ``never_run``) -- so a broken extension shows up as
one broken TAB, rendered by the SAME generic JSON renderer
``static/dashboard.html`` already uses for every core panel, never a 500 and
never a reason the rest of the dashboard fails to load.
"""

from __future__ import annotations

import importlib.util
import inspect
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from trialerror.dashboard.store_ro import RoStore

__all__ = [
    "EXT_PANELS_SUBDIR",
    "VALID_NAV_GROUPS",
    "ExtPanelManifest",
    "ExtPanelEntry",
    "load_ext_panel_entry",
    "discover_ext_panels",
    "find_ext_panel_entry",
    "list_ext_panels",
    "check_ext_panel_stages",
    "build_ext_panel",
    "build_all_ext_panels",
]

#: <program_root>/trialerror_ext/panels/<name>/ -- the fixed discovery layout.
EXT_PANELS_SUBDIR = ("trialerror_ext", "panels")
VALID_NAV_GROUPS = ("KNOW", "RUN")
_REQUIRED_MANIFEST_FIELDS = ("title", "nav_group", "order")


@dataclass(frozen=True)
class ExtPanelManifest:
    """A parsed, validated ``panel.toml``'s ``[panel]`` table."""

    title: str
    nav_group: str
    order: int
    description: str = ""
    #: advisory only -- table names the builder expects to find (typically
    #: in ``rostore.knowledge`` or ``rostore.ops``); nothing in this module
    #: enforces it. ``trialerror.dashboard.checks.check_ext_panels_valid`` does
    #: not check it either (that would require knowing which DB each name
    #: belongs to, which the manifest does not declare) -- it exists so a
    #: panel author can document intent, and so a future doctor check has
    #: somewhere to read it from without a manifest format change.
    min_schema: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ExtPanelEntry:
    """One discovered ``<name>/`` directory's manifest-parse outcome --
    the result of stage 1 (manifest) only. Stages 2 (import) and 3
    (signature) happen lazily, at build/check time (see
    :func:`build_ext_panel` / :func:`check_ext_panel_stages`), since they
    require actually executing the extension's own code -- something
    discovery itself should never do as a side effect."""

    name: str
    dir: Path
    manifest_status: str  # "ok" | "manifest_error"
    manifest: ExtPanelManifest | None
    manifest_error: str | None
    builder_path: Path


def _ext_panels_root(program_root: Path | str) -> Path:
    root = Path(program_root)
    for part in EXT_PANELS_SUBDIR:
        root = root / part
    return root


def _parse_manifest(toml_path: Path) -> tuple[ExtPanelManifest | None, str | None]:
    try:
        with toml_path.open("rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        return None, f"panel.toml not found: {toml_path}"
    except tomllib.TOMLDecodeError as exc:
        return None, f"invalid TOML in {toml_path}: {exc}"
    except OSError as exc:
        return None, f"could not read {toml_path}: {exc}"

    section = raw.get("panel")
    if not isinstance(section, dict):
        return None, f"{toml_path}: missing required [panel] table"

    missing = [k for k in _REQUIRED_MANIFEST_FIELDS if k not in section]
    if missing:
        return None, f"{toml_path}: missing required [panel] field(s): {', '.join(missing)}"

    title = section["title"]
    if not isinstance(title, str) or not title.strip():
        return None, f"{toml_path}: [panel].title must be a non-empty string"

    nav_group = section["nav_group"]
    if nav_group not in VALID_NAV_GROUPS:
        return None, f"{toml_path}: [panel].nav_group must be one of {VALID_NAV_GROUPS}, got {nav_group!r}"

    order = section["order"]
    if not isinstance(order, int) or isinstance(order, bool):
        return None, f"{toml_path}: [panel].order must be an integer, got {order!r}"

    description = section.get("description", "")
    if not isinstance(description, str):
        return None, f"{toml_path}: [panel].description must be a string if present"

    min_schema = section.get("min_schema", [])
    if not isinstance(min_schema, list) or not all(isinstance(x, str) for x in min_schema):
        return None, f"{toml_path}: [panel].min_schema must be a list of table-name strings if present"

    return (
        ExtPanelManifest(
            title=title, nav_group=nav_group, order=order, description=description, min_schema=list(min_schema)
        ),
        None,
    )


def load_ext_panel_entry(name: str, panel_dir: Path | str) -> ExtPanelEntry:
    """Manifest-parse (stage 1) exactly one panel directory, independent of
    the ``trialerror_ext/panels/`` discovery convention -- lets a caller validate
    a specific directory directly (the doctor check re-uses this per
    discovered entry, for any directory not itself nested under a
    program's ``trialerror_ext/panels/``)."""
    panel_dir = Path(panel_dir)
    toml_path = panel_dir / "panel.toml"
    builder_path = panel_dir / "builder.py"

    manifest, error = _parse_manifest(toml_path)
    if manifest is None:
        return ExtPanelEntry(
            name=name, dir=panel_dir, manifest_status="manifest_error",
            manifest=None, manifest_error=error, builder_path=builder_path,
        )
    if not builder_path.is_file():
        return ExtPanelEntry(
            name=name, dir=panel_dir, manifest_status="manifest_error",
            manifest=manifest, manifest_error=f"builder.py not found: {builder_path}",
            builder_path=builder_path,
        )
    return ExtPanelEntry(
        name=name, dir=panel_dir, manifest_status="ok",
        manifest=manifest, manifest_error=None, builder_path=builder_path,
    )


def discover_ext_panels(program_root: Path | str | None) -> list[ExtPanelEntry]:
    """Every ``<program_root>/trialerror_ext/panels/<name>/`` directory,
    manifest-parsed and sorted by ``(order, name)``. ``program_root=None``
    (no program open) and "no such directory" both return ``[]`` -- neither
    is an error, both are simply "this program declares no extension
    panels", the same "visible, not refused" shape every panel builder in
    :mod:`trialerror.dashboard.data` already uses for a missing store file."""
    if program_root is None:
        return []
    root = _ext_panels_root(program_root)
    if not root.is_dir():
        return []

    entries = [
        load_ext_panel_entry(child.name, child)
        for child in sorted(root.iterdir())
        if child.is_dir()
    ]
    entries.sort(key=lambda e: (e.manifest.order if e.manifest is not None else 0, e.name))
    return entries


def find_ext_panel_entry(program_root: Path | str | None, name: str) -> ExtPanelEntry | None:
    """The one discovered entry named ``name``, or ``None`` (no such
    extension panel -- callers turn that into a 404, never a 500)."""
    for entry in discover_ext_panels(program_root):
        if entry.name == name:
            return entry
    return None


def list_ext_panels(program_root: Path | str | None) -> list[dict[str, Any]]:
    """The "panel listing" -- one row per discovered extension panel, cheap
    enough to compute on every request (manifest-parse only, stage 1; it
    does not import ``builder.py``). Used for ``meta["ext_panels"]`` (every
    ``/dashboard/api/all`` response and the export snapshot) and the
    ``/dashboard/api/ext`` listing endpoint -- so a client can discover what
    extension panels exist, and their nav placement, without fetching every
    panel's full data."""
    rows: list[dict[str, Any]] = []
    for entry in discover_ext_panels(program_root):
        row: dict[str, Any] = {"name": entry.name, "manifest_status": entry.manifest_status}
        if entry.manifest is not None:
            row.update(
                title=entry.manifest.title,
                nav_group=entry.manifest.nav_group,
                order=entry.manifest.order,
                description=entry.manifest.description,
                min_schema=entry.manifest.min_schema,
            )
        if entry.manifest_error:
            row["error"] = entry.manifest_error
        rows.append(row)
    return rows


def _import_builder_module(entry: ExtPanelEntry):
    """Stage 2: import ``builder.py`` fresh (no ``sys.modules`` caching --
    matches this dashboard's whole "read fresh every request" design, see
    ``trialerror.dashboard.serve``'s module docstring). Raises on any failure;
    callers decide how to report it (see the module docstring's SECURITY
    NOTE -- this is a plain, unsandboxed import)."""
    spec = importlib.util.spec_from_file_location(f"trialerror_ext_panel__{entry.name}", entry.builder_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not create a module spec for {entry.builder_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _resolve_build_panel_fn(module) -> Callable[[RoStore, Any], dict]:
    """Stage 3: ``build_panel`` must exist, be callable, and accept two
    positional arguments -- extra keyword-with-default params or a trailing
    ``*args``/``**kwargs`` are fine; anything that can't bind
    ``(rostore, program_root)`` positionally is a signature error."""
    fn = getattr(module, "build_panel", None)
    if fn is None:
        raise AttributeError(f"{module.__name__} does not define build_panel(rostore, program_root)")
    if not callable(fn):
        raise TypeError(f"{module.__name__}.build_panel is not callable")
    try:
        inspect.signature(fn).bind(None, None)
    except TypeError as exc:
        raise TypeError(f"build_panel(rostore, program_root) signature mismatch: {exc}") from exc
    return fn


def check_ext_panel_stages(entry: ExtPanelEntry) -> tuple[str, str]:
    """Run stages 1 (already done, carried on ``entry``), 2 (import), and 3
    (signature) WITHOUT calling ``build_panel`` -- what
    ``trialerror.dashboard.checks.check_ext_panels_valid`` needs (a doctor check
    should not need a live store or actually execute the panel's own query
    logic to report "this extension is wired correctly"). Returns
    ``(stage, message)`` where ``stage`` is one of ``"ok"``,
    ``"manifest_error"``, ``"import_error"``, ``"signature_error"``."""
    if entry.manifest_status != "ok":
        return "manifest_error", entry.manifest_error or "invalid extension panel manifest"
    try:
        module = _import_builder_module(entry)
    except Exception as exc:  # noqa: BLE001 - deliberate: isolate the extension's own import-time errors
        return "import_error", f"{type(exc).__name__}: {exc}"
    try:
        _resolve_build_panel_fn(module)
    except Exception as exc:  # noqa: BLE001 - deliberate: isolate the extension's own errors
        return "signature_error", f"{type(exc).__name__}: {exc}"
    return "ok", "manifest, import, and build_panel signature all valid"


def build_ext_panel(entry: ExtPanelEntry, rostore: RoStore, program_root: Path | str) -> dict[str, Any]:
    """Build one extension panel's data -- the live, per-request path
    (:func:`build_all_ext_panels`, ``/dashboard/api/ext/<name>``,
    ``trialerror.dashboard.export``). A broken manifest, a builder that fails to
    import, a wrong ``build_panel`` signature, an exception raised INSIDE
    ``build_panel``, or a non-dict return value all become the same
    ``{"status": "ext_error", "message": ...}`` shape -- NEVER an exception
    that reaches the caller. This is the guarantee the whole protocol rests
    on: a broken extension panel is one broken tab, never a broken
    dashboard."""
    if entry.manifest_status != "ok":
        return {"status": "ext_error", "message": entry.manifest_error or "invalid extension panel manifest"}
    try:
        module = _import_builder_module(entry)
        fn = _resolve_build_panel_fn(module)
        result = fn(rostore, program_root)
    except Exception as exc:  # noqa: BLE001 - deliberate: an extension crash must never break the dashboard
        return {"status": "ext_error", "message": f"{type(exc).__name__}: {exc}"}
    if not isinstance(result, dict):
        return {
            "status": "ext_error",
            "message": f"build_panel must return a dict, got {type(result).__name__}",
        }
    return result


def build_all_ext_panels(program_root: Path | str | None, rostore: RoStore) -> dict[str, dict[str, Any]]:
    """Every discovered extension panel's data, keyed by name -- what
    ``/dashboard/api/all`` nests under ``panels["ext"]`` and
    ``trialerror.dashboard.export`` embeds the same way. Returns ``{}`` (an
    empty dict, never a missing key at this layer -- callers decide whether
    to omit the ``"ext"`` key entirely when empty, matching every other
    panel's "always present" convention only once there is something to be
    present) when the program declares no extension panels."""
    rostore_program_root = program_root
    return {
        entry.name: build_ext_panel(entry, rostore, rostore_program_root)
        for entry in discover_ext_panels(program_root)
    }
