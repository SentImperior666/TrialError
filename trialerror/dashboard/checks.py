"""Dashboard doctor checks. Auto-discovered by
``trialerror.util.doctor.discover_and_register_checks`` exactly like every other
subsystem's own ``checks.py`` (design Section 5.2 doctor row: "framework +
license-audit in M0; each module registers its own checks") -- dropping this
file is the entire registration step, no shared file touched.

One check, ``ext_panels_valid`` (``trialerror.dashboard.ext``, C-0070's
extension-panel protocol): validates every extension panel this program
declares WITHOUT executing its ``build_panel`` against a live store (see
``trialerror.dashboard.ext.check_ext_panel_stages`` -- manifest-parse, import,
and signature only). A broken extension panel is always ``warn``, NEVER
``fail`` -- the same "visible, not refused" contract
``trialerror.dashboard.ext.build_ext_panel`` itself upholds at request time: one
broken extension panel must never read as a reason the whole program (or
the whole dashboard) is unhealthy.
"""

from __future__ import annotations

from trialerror.dashboard.ext import check_ext_panel_stages, discover_ext_panels
from trialerror.util.doctor import CheckResult, DoctorContext, register_check

__all__ = ["check_ext_panels_valid"]


@register_check("ext_panels_valid", category="dashboard")
def check_ext_panels_valid(ctx: DoctorContext) -> CheckResult:
    if ctx.program_root is None:
        return CheckResult(
            name="ext_panels_valid",
            category="dashboard",
            status="skip",
            message="no program_root supplied -- extension panels are program-scoped (C-0070)",
        )

    entries = discover_ext_panels(ctx.program_root)
    if not entries:
        return CheckResult(
            name="ext_panels_valid",
            category="dashboard",
            status="skip",
            message="no trialerror_ext/panels/ directory under this program root (or it is empty)",
        )

    rows = []
    offenders = []
    for entry in entries:
        stage, message = check_ext_panel_stages(entry)
        row = {"name": entry.name, "stage": stage, "message": message}
        rows.append(row)
        if stage != "ok":
            offenders.append(row)

    status = "warn" if offenders else "pass"
    message = (
        f"{len(offenders)}/{len(entries)} extension panel(s) failed manifest/import/signature "
        "validation -- a broken extension panel is a warn, never a dashboard-wide failure"
        if offenders
        else f"{len(entries)} extension panel(s) valid (manifest, import, and build_panel signature)"
    )
    return CheckResult(
        name="ext_panels_valid",
        category="dashboard",
        status=status,
        message=message,
        details={"panels": rows},
    )
