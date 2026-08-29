"""Reproduction runner. Design Section 8.3 (``trialerror verify reproduce
VRD-x``): "Re-runs a verdict's attached ``reproduction_ref`` script and
compares output sha to the recorded expectation (byte-exact bar from
SV-013 practice). ``gate advance -> registered`` consults this."

**The M10 coupling, stated exactly (per this build's integration brief --
reproduced verbatim from ``trialerror.artifacts.gates``'s own TRIALERROR-DEV-NOTE,
which names this as the CONTRACT M9 inherits):** the only way this module
talks to M10 is one call --
``trialerror.stores.update(store, "gate", pk_column="gate_id", pk_value=...,
changes={"reproduction_status": "match"|"mismatch"|"unrun",
"reproduction_ref": ...})`` -- made only when a caller supplies ``gate_id``
(this runner has no way to discover a gate on its own: a ``verdict`` row's
``subject_id`` is polymorphic, and nothing in the schema FK-links a
``verdict`` to a ``gate``). ``trialerror.artifacts.gates.apply_union``/
``advance_gate`` then read ``gate.reproduction_status`` exactly as stored,
unchanged by this build.

``reproduction_ref`` is expected to be a JSON object (design DDL comment:
"reproduction_ref? (script path + args + expected sha)"):
``{"script": "<path, relative to program_root unless absolute>",
"args": ["...", ...], "expected_sha256": "<64 hex chars>"}`` -- ``args``
defaults to ``[]`` if omitted.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from trialerror.stores import get as store_get
from trialerror.stores import update as store_update
from trialerror.stores.store import Store
from trialerror.verify.errors import ReproductionRefError, VerdictNotFoundError
from trialerror.verify.verdicts import record_verdict

__all__ = ["parse_reproduction_ref", "reproduce_verdict"]

_REQUIRED_REF_FIELDS = ("script", "expected_sha256")


def parse_reproduction_ref(reproduction_ref: str) -> dict[str, Any]:
    """Parse a ``verdict.reproduction_ref`` string into
    ``{"script", "args", "expected_sha256"}``. Raises
    :class:`~trialerror.verify.errors.ReproductionRefError` for anything that
    isn't a JSON object carrying at least ``script``/``expected_sha256``."""
    try:
        spec = json.loads(reproduction_ref)
    except json.JSONDecodeError as exc:
        raise ReproductionRefError(f"reproduction_ref is not valid JSON: {exc}") from exc
    if not isinstance(spec, dict):
        raise ReproductionRefError(f"reproduction_ref must be a JSON object, got {type(spec).__name__}")
    missing = [f for f in _REQUIRED_REF_FIELDS if not spec.get(f)]
    if missing:
        raise ReproductionRefError(f"reproduction_ref missing required field(s): {missing}")
    spec.setdefault("args", [])
    return spec


def reproduce_verdict(
    store: Store,
    *,
    verdict_id: str,
    gate_id: str | None = None,
    issued_by_launch: str,
    cwd: str | Path | None = None,
    timeout: float | None = 60.0,
    procedure_version: str = "1",
) -> dict[str, Any]:
    """Re-run ``verdict_id``'s ``reproduction_ref`` script and byte-compare
    its stdout's sha256 against the recorded ``expected_sha256``. The
    script is invoked as ``[sys.executable, script, *args]`` -- explicit
    interpreter, never relying on a shebang or PATH lookup (this codebase's
    standing Windows convention; see e.g. design Section 5.4's "hook
    command lines invoke ``python`` explicitly (Windows)").

    Always writes a NEW ``verdict`` row (``procedure=reproduction``,
    ``label='match'``/``'mismatch'``, same ``subject_kind``/``subject_id``
    as the original verdict being reproduced) documenting the run -- this
    happens regardless of whether ``gate_id`` is given. When ``gate_id``
    IS given, also performs the exact ``trialerror.stores.update`` call M10's
    gate state machine depends on (see module docstring).

    A script that fails to launch, times out, or exits non-zero is treated
    as ``'mismatch'`` (never silently ``'unrun'`` -- this function only
    ever writes ``'match'``/``'mismatch'``; ``'unrun'`` is the CHECK
    constraint's third value, meaning "never attempted", which is never
    this function's own outcome for a call that actually ran).

    Raises :class:`~trialerror.verify.errors.VerdictNotFoundError` if
    ``verdict_id`` doesn't exist, or
    :class:`~trialerror.verify.errors.ReproductionRefError` if it has no usable
    ``reproduction_ref`` (nothing to reproduce).
    """
    original = store_get(store, "verdict", pk_column="verdict_id", pk_value=verdict_id)
    if original is None:
        raise VerdictNotFoundError(f"no such verdict: {verdict_id!r}")
    if not original.get("reproduction_ref"):
        raise ReproductionRefError(f"verdict {verdict_id!r} has no reproduction_ref to run")
    spec = parse_reproduction_ref(original["reproduction_ref"])

    script_path = Path(spec["script"])
    if not script_path.is_absolute():
        script_path = Path(cwd) / script_path if cwd else store.program_root / script_path
    run_cwd = Path(cwd) if cwd else store.program_root

    argv = [sys.executable, str(script_path), *[str(a) for a in spec["args"]]]
    error_note: str | None = None
    try:
        proc = subprocess.run(argv, cwd=str(run_cwd), capture_output=True, timeout=timeout, check=False)
        stdout_bytes = proc.stdout or b""
        if proc.returncode != 0:
            error_note = f"exit code {proc.returncode}; stderr: {(proc.stderr or b'').decode('utf-8', errors='replace')[:500]}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        stdout_bytes = b""
        error_note = f"{type(exc).__name__}: {exc}"

    actual_sha256 = hashlib.sha256(stdout_bytes).hexdigest()
    expected_sha256 = spec["expected_sha256"]
    status = "match" if error_note is None and actual_sha256 == expected_sha256 else "mismatch"

    reproduction_ref_str = original["reproduction_ref"]
    new_verdict = record_verdict(
        store,
        subject_kind=original["subject_kind"],
        subject_id=original["subject_id"],
        procedure="reproduction",
        procedure_version=procedure_version,
        label=status,
        evidence=[
            {
                "note": error_note or f"expected_sha256={expected_sha256} actual_sha256={actual_sha256}",
                "stance": status,
            }
        ],
        reproduction_ref=reproduction_ref_str,
        issued_by_launch=issued_by_launch,
    )

    if gate_id is not None:
        store_update(
            store, "gate", pk_column="gate_id", pk_value=gate_id,
            changes={"reproduction_status": status, "reproduction_ref": reproduction_ref_str},
        )

    return {
        "original_verdict_id": verdict_id,
        "verdict": new_verdict,
        "gate_id": gate_id,
        "status": status,
        "actual_sha256": actual_sha256,
        "expected_sha256": expected_sha256,
        "error_note": error_note,
    }
