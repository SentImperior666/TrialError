"""Doctor: is every configured sidecar actually serving? (lane F-1b item 5)

Auto-discovered by :func:`trialerror.util.doctor.discover_and_register_checks`
-- this file landing is the whole registration, as for every other subsystem's
``checks.py``.

One check, ``sidecar_alive``, and its verdict scale is chosen the same way
``query_embed_backend_runnable``'s was: **warn, never fail.** A dead embedding
sidecar costs this program its vector tier, which every retrieval surface
already degrades from and says so; it does not corrupt a store, and on a
machine that has just booted it is the expected state for as long as it takes
somebody to run one verb. What it must never be is invisible.

Read-only by construction, and in both senses (VERIFY_f1b-sidecar.md V-4): it
asks ``status`` with restarts switched off AND with the heartbeat refresh
switched off, so it starts nothing and writes nothing. A doctor run that
silently restarted processes would be a doctor run nobody could use to find
out what was wrong; a doctor run that refreshed the heartbeat it reports would
be the only thing keeping that timestamp fresh, and its age would stop being
evidence.

The age IS reported (``heartbeat_age_s`` per sidecar) and is deliberately not
a verdict, which is where this check parts company with
``webfetch_sidecar_alive`` (:mod:`trialerror.webfetch.checks`, which fails
past ``SIDECAR_DEAD_S``): a webfetch heartbeat is written by a loop that is
supposed to be running, while a sidecar's is written by whoever last polled,
and in a program where nothing polls on a schedule an age threshold would fail
a perfectly healthy process. The live health probe is what answers "is it
serving", and that is what this check's verdict follows.
"""

from __future__ import annotations

from pathlib import Path

from trialerror.sidecar.supervisor import (
    SIDECARS_TABLE,
    SidecarConfigError,
    sidecar_names,
    sidecar_status,
)
from trialerror.util.doctor import CheckResult, DoctorContext, register_check

__all__ = ["check_sidecar_alive", "SIDECAR_ALIVE_CHECK"]

#: The check's name, spelled once so the messages that name it and the guide's
#: catalog row cannot drift.
SIDECAR_ALIVE_CHECK = "sidecar_alive"


def _program_config(program_root: Path | None) -> dict:
    """``trialerror.toml``'s raw dict, or ``{}`` -- the same best-effort read
    every other ``checks.py`` does, for the same reason: a doctor check must
    never itself be the thing that raises."""
    from trialerror.util.config import CONFIG_FILENAME, load_config

    if program_root is None:
        return {}
    cfg_path = Path(program_root) / CONFIG_FILENAME
    if not cfg_path.is_file():
        return {}
    try:
        return load_config(cfg_path).raw
    except Exception:  # noqa: BLE001 - a malformed config is another check's finding
        return {}


@register_check(SIDECAR_ALIVE_CHECK, category="sidecar")
def check_sidecar_alive(ctx: DoctorContext) -> CheckResult:
    """Every ``[sidecars.<name>]`` this program configures, and whether it is
    running and answering its ``health_url``.

    Statuses:

    - ``skip`` -- no program root, or no ``[sidecars]`` table. A program with
      no sidecars has nothing to be unhealthy.
    - ``warn`` -- one or more configured sidecars is not running, is running
      but failing its health check, or has a table this supervisor cannot read
      (a malformed ``command``, an unknown ``restart``). The detail names each
      one and the message names the verb that starts it.
    - ``pass`` -- every configured sidecar is running, and each one that
      declares a ``health_url`` answered it.

    A sidecar with no ``health_url`` can only be reported as "the process is
    alive", and that is what it is reported as -- ``health.configured: false``
    in the detail rather than a green light nobody earned. A sidecar that DOES
    configure one but is not running reads ``configured: true`` with
    ``skipped`` naming why nothing was probed: the two are different
    statements and the detail makes them look different (V-6).
    """
    if ctx.program_root is None:
        return CheckResult(
            name=SIDECAR_ALIVE_CHECK, category="sidecar", status="skip",
            message="no program root (pass --program-root to see program-scoped checks)",
        )
    config = _program_config(ctx.program_root)
    names = sidecar_names(config)
    if not names:
        return CheckResult(
            name=SIDECAR_ALIVE_CHECK, category="sidecar", status="skip",
            message=f"no [{SIDECARS_TABLE}] table in trialerror.toml -- this program supervises none",
            details={"configured": []},
        )

    rows: dict[str, dict] = {}
    unhealthy: list[str] = []
    for name in names:
        try:
            status = sidecar_status(
                ctx.program_root, name, config=config,
                restart_if_dead=False, refresh_heartbeat=False,
            )
        except SidecarConfigError as exc:
            rows[name] = {"state": "misconfigured", "error": str(exc)}
            unhealthy.append(name)
            continue
        except Exception as exc:  # noqa: BLE001 - one broken sidecar is not a broken doctor
            rows[name] = {"state": "unreadable", "error": f"{type(exc).__name__}: {exc}"}
            unhealthy.append(name)
            continue
        health = status.get("health") or {}
        rows[name] = {
            "state": status.get("state"),
            "running": status.get("running"),
            "pid": status.get("pid"),
            "health": health,
            "heartbeat_at": status.get("heartbeat_at"),
            "heartbeat_age_s": status.get("heartbeat_age_s"),
            "restart": (status.get("spec") or {}).get("restart"),
        }
        if not status.get("running") or health.get("ok") is False:
            unhealthy.append(name)

    details = {"configured": names, "sidecars": rows}
    if unhealthy:
        return CheckResult(
            name=SIDECAR_ALIVE_CHECK, category="sidecar", status="warn",
            message=(
                f"{len(unhealthy)} of {len(names)} configured sidecar(s) not serving: "
                f"{', '.join(unhealthy)}. Start one with `trialerror sidecar start <name>`; "
                f"`trialerror sidecar status` carries its log path"
            ),
            details=details,
        )
    return CheckResult(
        name=SIDECAR_ALIVE_CHECK, category="sidecar", status="pass",
        message=f"{len(names)} configured sidecar(s) running and healthy: {', '.join(names)}",
        details=details,
    )
