"""Supervised long-lived helper processes, in-container (lane F-1b item 4).

A sidecar here is a process this program needs RUNNING in order to work, but
does not own the code of: today that is the embedding server the query-side
``llama_server`` backend talks to, which loads a multi-gigabyte model once and
answers over loopback for the rest of the day.

Three properties are the whole design:

- **The command comes from config, never from an argument.** ``[sidecars.<name>]``
  in ``trialerror.toml`` carries the argv, the working directory and the
  environment; ``trialerror sidecar start <name>`` takes a NAME. An agent can
  therefore start the sidecar an operator configured, and cannot start
  something else -- the same reading ``webfetch sidecar`` takes of its policy
  directory.
- **No host act, and no tmux.** The verb owns the lifecycle: it starts the
  process detached (``start_new_session`` / ``DETACHED_PROCESS``, the same
  technique :mod:`trialerror.jobs.worker` and ``dashboard serve`` already
  use), records it under the program's ``run/`` directory, and can report on
  it and stop it from any later process. Running it inside a tmux window is
  then a convenience, not a dependency.
- **Supervision is a POLL, and says so.** Nothing here is a daemon watching a
  daemon. ``restart = "always"`` means "``status`` restarts this if it finds
  it dead", and the thing that makes restarts happen is whatever already runs
  on a loop in this container calling ``trialerror sidecar status``. A
  supervisor that claimed to watch, from a process that exits, would be a
  worse lie than no supervisor at all.
"""

from __future__ import annotations

from trialerror.sidecar.supervisor import (
    SIDECARS_TABLE,
    SidecarConfigError,
    SidecarSpec,
    SidecarStartBusy,
    health_probe,
    heartbeat_age_s,
    load_sidecar_spec,
    sidecar_lock_path,
    sidecar_names,
    sidecar_state_path,
    sidecar_status,
    start_sidecar,
    stop_sidecar,
)

__all__ = [
    "SIDECARS_TABLE",
    "SidecarConfigError",
    "SidecarSpec",
    "SidecarStartBusy",
    "health_probe",
    "heartbeat_age_s",
    "load_sidecar_spec",
    "sidecar_lock_path",
    "sidecar_names",
    "sidecar_state_path",
    "sidecar_status",
    "start_sidecar",
    "stop_sidecar",
]
