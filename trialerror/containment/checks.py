"""Lane L0-F's doctor check: ``mass_deletion``. Design section 14
("Detection"): "doctor check `mass_deletion` compares the live file count
and total bytes under `/workspace/origin-project/research`, `/workspace/origin-project/curriculum`,
`/workspace/origin-project-program/stores` against the last snapshot manifest and
FAILS on a drop greater than 1% of files or any store file missing; the
HOME banner and `te-status.sh` surface it; the next `te-mirror.sh` run
refuses to mirror while the check fails (so a destroyed working copy
never overwrites the hub even by fast-forward)."

Auto-discovered by :func:`trialerror.util.doctor.discover_and_register_checks`
exactly like every other subsystem's ``checks.py`` (see
``trialerror/jobs/checks.py``'s module docstring for how the discovery
walks direct subpackages of ``trialerror`` looking for a ``checks``
submodule) — dropping this file plus ``trialerror/containment/__init__.py``
is the entire registration step, no shared file touched.

Manifest schema (produced by ``deploy/sandbox/containment/te-snapshot.sh``,
written once per snapshot AND copied to the one fixed live path this check
reads, ``<platform_root>/snapshot_manifest.json``)::

    {
      "schema": 1,
      "snapshot_ts": "20260905T003908Z",
      "watched_paths": {
        "origin-project/research":       {"file_count": N, "total_bytes": N},
        "origin-project/curriculum":     {"file_count": N, "total_bytes": N},
        "origin-project-program/stores": {"file_count": N, "total_bytes": N}
      },
      "store_files": ["origin-project-program/stores/knowledge.db", "..."]
    }

Every path in the manifest is relative to the WORKSPACE root — the
``/workspace`` directory that holds ``origin-project/`` and ``origin-project-program/`` as
siblings (design section 1's topology diagram). This module resolves that
root as ``<platform_root>.parent``, since ``platform/`` is itself a direct
child of ``/workspace`` (``TRIALERROR_PLATFORM_ROOT=/workspace/platform``,
design section 2's Dockerfile ``ENV`` block). ``DoctorContext.platform_root``
of ``None`` falls back to :func:`trialerror.stores.paths.platform_root`
exactly like ``trialerror.budget.checks._platform_db_path`` does (fix-accept,
C-0064's honor-ctx-before-env-fallback convention).

TRIALERROR-DEV-NOTE: ``<platform_root>.parent`` is an INFERENCE from the
fixed container layout the design commits to (D8), not a configured path —
there is no ``[paths].workspace_root`` knob, and this check does not add
one. Outside the sandbox container (a scratch/test program whose
``platform_root`` isn't a child of a workspace with ``origin-project/``/``origin-project-program/``
siblings) the check simply finds no snapshot manifest, no prior manifest,
and no content under the watched paths, and reports ``skip`` — same as a
program that has never run ``te-snapshot.sh`` (see ``check_mass_deletion``
for when a missing manifest instead fails, per finding L0F-02 below).

Side effect (deliberate — design section 14 explicitly specs it, and the
only doctor check in this codebase with a filesystem write): on FAIL this
check WRITES ``<platform_root>/MASS_DELETION_DETECTED``, the flag file
``te-mirror.sh`` checks for before every fetch; on a clean/pass run it
REMOVES that flag file if one is present. The write/remove is best-effort
(an ``OSError`` is folded into the result's message rather than raised) —
one broken check must never crash the whole ``trialerror doctor`` run, the
same contract :func:`trialerror.util.doctor.run_checks` already documents
for a check that raises outright.

Known limitation, disclosed rather than silently assumed away: the FAIL
threshold is file-COUNT drop only (design's literal wording: "a drop
greater than 1% of files"). ``total_bytes`` is captured and reported in
``details`` for visibility but does not independently gate pass/fail — a
scenario that truncates every watched file's *contents* to empty without
changing the file count would not be caught by this check. The
reachability argument (design section 14's git-hub-not-mounted +
off-machine-snapshot proof) is what actually bounds that risk, not this
detector; this check is the fast, cheap, in-band signal, not the proof
itself.

Review finding L0F-02, disclosed rather than left implicit: everything
this module reads and writes — the manifest at
``<platform_root>/snapshot_manifest.json``, the flag at
``<platform_root>/MASS_DELETION_DETECTED`` — lives INSIDE the workspace
bind mount, i.e. inside the same filesystem an in-container agent
process can write to. Verified empirically against a real deployed run
during this finding's review:

1. **A missing manifest used to make this check go quiet, not loud.**
   Before this fix, a missing manifest returned ``skip`` unconditionally
   — so ``rm -rf /workspace/*`` (which deletes the manifest along with
   everything else) silenced the detector instead of tripping it. Fixed
   below: a missing manifest now returns ``fail`` whenever there is
   affirmative reason to believe this workspace should have one — either
   ``snapshot_manifest.prev.json`` is present (te-snapshot.sh has run
   here before; see point 3) or at least one watched path is non-empty
   right now. Only a workspace with NEITHER prior-manifest evidence NOR
   any watched-path content (a genuinely fresh scratch/test program, or
   the sandbox before ``te-snapshot.sh`` has ever run once) still
   returns ``skip``.
2. **The flag this check writes is agent-writable and must not be
   treated as the sole gate.** Nothing stops the same in-container
   process that caused the deletion from also running
   ``rm workspace/platform/MASS_DELETION_DETECTED`` to clear the alarm.
   Because of this, ``deploy/sandbox/containment/te-mirror.sh`` (see its
   own header comment) no longer relies on this in-mount flag as its
   only authority — it independently recomputes the same comparison,
   host-side, directly against the newest snapshot's own
   ``manifest.json`` under ``$SNAPSHOT_ROOT`` (a directory the container
   has no bind mount to at all) and writes its OWN flag under
   ``$SANDBOX_ROOT`` (also outside the mount — genuinely absent from the
   container's filesystem namespace, not merely denied by a rule). The
   flag this module writes/clears is still real and useful — it is what
   :mod:`trialerror.containment.dashboard_items` and the HOME banner
   surface to an operator inside the trust boundary — but it is now
   correctly understood as a **convenience/visibility surface**, not the
   tamper-resistant authority for whether ``te-mirror.sh`` mirrors.
3. **A self-heal is possible and is now made visible, not prevented.**
   ``te-snapshot.sh`` runs hourly and always recomputes the manifest from
   whatever is CURRENTLY live — correct behavior in general (a
   legitimate cleanup must not perpetually re-trigger this check) — but
   it means a disaster followed by one more snapshot cycle can silently
   move the baseline down to match the post-deletion state, after which
   both this check and ``te-mirror.sh``'s own host-side check (which
   shares the same "compare against the newest snapshot" logic) see "no
   drop" and clear cleanly. That is a real, disclosed, ≤60-minute window
   bounded by the same one-snapshot-interval loss the design's own risk
   register already accepts elsewhere — not eliminated by this fix.
   ``te-snapshot.sh`` now preserves the manifest it is about to
   overwrite as a same-directory sibling, ``snapshot_manifest.prev.json``,
   specifically so a self-heal leaves a visible trace (diff the two
   files) instead of vanishing without one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trialerror.stores import paths
from trialerror.util.doctor import CheckResult, DoctorContext, register_check

__all__ = [
    "WATCHED_PATHS",
    "MANIFEST_FILENAME",
    "PREV_MANIFEST_FILENAME",
    "FLAG_FILENAME",
    "MAX_DROP_FRACTION",
    "check_mass_deletion",
]

#: The three paths design section 14 names, relative to the workspace root
#: (the parent of ``platform/``). Order is display order only.
WATCHED_PATHS: tuple[str, ...] = ("origin-project/research", "origin-project/curriculum", "origin-project-program/stores")

MANIFEST_FILENAME = "snapshot_manifest.json"
#: Written by te-snapshot.sh immediately before it overwrites MANIFEST_FILENAME
#: with a fresh one (finding L0F-02, point 3) — the manifest this check saw
#: LAST time, kept as a same-directory sibling so a one-tick self-heal (the
#: baseline quietly dropping to match a post-deletion live count) leaves a
#: visible trace to diff against, even though this module cannot prevent it.
PREV_MANIFEST_FILENAME = "snapshot_manifest.prev.json"
FLAG_FILENAME = "MASS_DELETION_DETECTED"

#: Design section 14 Detection: "FAILS on a drop greater than 1% of files"
#: — strictly greater, so a drop of exactly 1% still passes.
MAX_DROP_FRACTION = 0.01


def _platform_root(ctx: DoctorContext) -> Path:
    # fix-accept (C-0064) convention, same as trialerror.budget.checks.
    # _platform_db_path: honor an explicit ctx.platform_root before
    # falling back to TRIALERROR_PLATFORM_ROOT/~/.trialerror.
    return ctx.platform_root if ctx.platform_root is not None else paths.platform_root()


def _workspace_root(ctx: DoctorContext) -> Path:
    return _platform_root(ctx).parent


def _count_dir(path: Path) -> tuple[int, int]:
    """``(file_count, total_bytes)`` for every regular file under ``path``,
    recursively. A missing directory counts as ``(0, 0)`` — deliberately
    indistinguishable from "everything in it was deleted", which is
    exactly the case this check exists to catch."""
    if not path.is_dir():
        return 0, 0
    count = 0
    total = 0
    for f in path.rglob("*"):
        if f.is_file():
            count += 1
            try:
                total += f.stat().st_size
            except OSError:
                # a file that vanishes between rglob's listing and stat()
                # (e.g. concurrent activity in the workspace) counts as
                # present-but-unmeasured, not as a phantom failure.
                pass
    return count, total


def _write_flag(flag_path: Path, message: str) -> str | None:
    try:
        flag_path.parent.mkdir(parents=True, exist_ok=True)
        flag_path.write_text(message, encoding="utf-8")
        return None
    except OSError as exc:
        return f"could not write flag file {flag_path}: {exc}"


def _clear_flag(flag_path: Path) -> str | None:
    try:
        if flag_path.exists():
            flag_path.unlink()
        return None
    except OSError as exc:
        return f"could not remove flag file {flag_path}: {exc}"


@register_check("mass_deletion", category="containment")
def check_mass_deletion(ctx: DoctorContext) -> CheckResult:
    platform_root = _platform_root(ctx)
    manifest_path = platform_root / MANIFEST_FILENAME
    flag_path = platform_root / FLAG_FILENAME

    if not manifest_path.is_file():
        # L0F-02(a): a missing manifest used to be an unconditional `skip` —
        # which meant deleting your own baseline silenced this check instead
        # of tripping it. Escalate to `fail` whenever there is affirmative
        # reason to believe this workspace SHOULD have a manifest: either a
        # previous one is still sitting next to it (te-snapshot.sh has run
        # here before) or one of the watched paths already has real content.
        # Only a workspace with neither signal — a genuinely fresh scratch
        # program, or the sandbox before te-snapshot.sh has ever run once —
        # still reports `skip`.
        workspace_root = _workspace_root(ctx)
        prev_manifest_path = platform_root / PREV_MANIFEST_FILENAME
        has_prev_manifest = prev_manifest_path.is_file()
        watched_live_counts = {rel: _count_dir(workspace_root / rel)[0] for rel in WATCHED_PATHS}
        has_watched_content = any(count > 0 for count in watched_live_counts.values())

        if not has_prev_manifest and not has_watched_content:
            return CheckResult(
                name="mass_deletion",
                category="containment",
                status="skip",
                message=f"no snapshot manifest yet at {manifest_path} (run te-snapshot.sh at least once)",
                details={"manifest_path": str(manifest_path)},
            )

        reasons = []
        if has_prev_manifest:
            reasons.append(f"a previous manifest exists at {prev_manifest_path} (te-snapshot.sh has run before)")
        if has_watched_content:
            nonzero = {rel: c for rel, c in watched_live_counts.items() if c > 0}
            reasons.append(f"watched path(s) are non-empty: {nonzero}")
        reason_text = "; ".join(reasons)

        details: dict[str, Any] = {
            "manifest_path": str(manifest_path),
            "workspace_root": str(workspace_root),
            "prev_manifest_exists": has_prev_manifest,
            "watched_path_live_counts": watched_live_counts,
        }
        flag_error = _write_flag(
            flag_path,
            "mass_deletion doctor check FAILED -- snapshot manifest is MISSING despite evidence it should "
            f"exist.\n{reason_text}\n",
        )
        message = (
            f"MASS DELETION SUSPECTED -- snapshot manifest missing at {manifest_path} even though "
            f"{reason_text} (deleting your own baseline does not silence this check)"
        )
        if flag_error:
            message += f" (WARNING: {flag_error})"
            details["flag_write_error"] = flag_error
        else:
            details["flag_path"] = str(flag_path)
        return CheckResult(
            name="mass_deletion", category="containment", status="fail", message=message, details=details
        )

    try:
        manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return CheckResult(
            name="mass_deletion",
            category="containment",
            status="fail",
            message=f"snapshot manifest at {manifest_path} is unreadable/corrupt: {exc}",
            details={"manifest_path": str(manifest_path)},
        )

    workspace_root = _workspace_root(ctx)
    manifest_watched: dict[str, Any] = manifest.get("watched_paths") or {}

    per_path: dict[str, dict[str, Any]] = {}
    offenders: list[str] = []
    for rel in WATCHED_PATHS:
        manifest_entry = manifest_watched.get(rel) or {"file_count": 0, "total_bytes": 0}
        manifest_count = int(manifest_entry.get("file_count", 0))
        live_count, live_bytes = _count_dir(workspace_root / rel)
        dropped = max(0, manifest_count - live_count)
        drop_fraction = (dropped / manifest_count) if manifest_count > 0 else 0.0
        over_threshold = drop_fraction > MAX_DROP_FRACTION
        per_path[rel] = {
            "manifest_file_count": manifest_count,
            "live_file_count": live_count,
            "live_total_bytes": live_bytes,
            "dropped_files": dropped,
            "drop_fraction": drop_fraction,
            "over_threshold": over_threshold,
        }
        if over_threshold:
            offenders.append(
                f"{rel}: {manifest_count} -> {live_count} files "
                f"({drop_fraction:.1%} drop, threshold {MAX_DROP_FRACTION:.0%})"
            )

    missing_stores: list[str] = [
        store_rel for store_rel in (manifest.get("store_files") or []) if not (workspace_root / store_rel).is_file()
    ]

    details: dict[str, Any] = {
        "manifest_path": str(manifest_path),
        "workspace_root": str(workspace_root),
        "snapshot_ts": manifest.get("snapshot_ts"),
        "watched_paths": per_path,
        "missing_store_files": missing_stores,
    }

    if offenders or missing_stores:
        flag_error = _write_flag(
            flag_path,
            "mass_deletion doctor check FAILED -- see `trialerror doctor --only mass_deletion`.\n"
            f"offenders: {offenders}\nmissing_store_files: {missing_stores}\n",
        )
        parts = list(offenders)
        if missing_stores:
            parts.append(f"missing store file(s): {', '.join(missing_stores)}")
        message = "MASS DELETION SUSPECTED -- " + "; ".join(parts)
        if flag_error:
            message += f" (WARNING: {flag_error})"
            details["flag_write_error"] = flag_error
        else:
            details["flag_path"] = str(flag_path)
        return CheckResult(
            name="mass_deletion", category="containment", status="fail", message=message, details=details
        )

    flag_error = _clear_flag(flag_path)
    message = "no mass deletion detected (within snapshot baseline)"
    if flag_error:
        message += f" (WARNING: {flag_error})"
        details["flag_clear_error"] = flag_error
    return CheckResult(name="mass_deletion", category="containment", status="pass", message=message, details=details)
