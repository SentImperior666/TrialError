"""HOME's "what needs a human" lines for web ingestion (design §3.4).

Two items, and neither of them is a status readout — the determinations
panel is for things that will not move until a person does something:

``webfetch_proposals``
    hosts an agent asked for and nobody has approved. This one is the whole
    point of ruling L-A2. The research container can *record* a proposal and
    can do nothing else with it; the approval is a command on the operator's
    own machine, against a file this container cannot see. So a proposal sits
    in the queue indefinitely, invisible, unless something puts it in front
    of a human — this is that something. Not ``blocking``: the rest of the
    system is fine, one URL is waiting on a decision.

``webfetch_sidecar_down``
    the fetch process has stopped and URLs are waiting. ``blocking``: no
    amount of patience moves a queued fetch while the process that drains it
    is gone, and every downstream stage of those documents is parked behind
    it.

Shape matches every other determinations-panel item source in
``trialerror/dashboard/data.py`` (``_gate_edit_items``, ``_kg_merge_items``,
``_acquisition_items``, ``_prereg_reveal_items``, ``_room_escalation_items``,
``_memory_conflict_items``, the containment lane's ``mass_deletion_items``
and the offload lane's ``offload_backlog_items``): a
``*_items(rostore) -> list[dict]`` with ``kind``/``id``/``blocking``/
``consequence`` keys.

**This module reads the queue, never the fetched bytes.** ``proposals.jsonl``
is written by the process on the other side of the trust boundary, so every
field taken out of it is treated as hostile text: the host name is bounded
and stripped of anything that is not a host character, the example URL is
truncated, and nothing from a fetched page reaches this panel at all
(C-0007). A dashboard render is a place where a hostile string would be read
by a human *and* by whatever agent summarizes HOME, which is precisely the
combination design §4 T3 exists to prevent.

Refusals that a human could lawfully fix already reach HOME through the
existing ``_acquisition_items`` source — they are ``source`` rows in
``request_state='wanted'``, which that function already unions in. Nothing
here duplicates them.
"""

from __future__ import annotations

import json
from typing import Any

from trialerror.dashboard.store_ro import RoStore
from trialerror.webfetch.checks import SIDECAR_DEAD_S
from trialerror.webfetch.config import WebFetchConfigError, load_webfetch_config
from trialerror.webfetch.protocol import Queue

__all__ = ["webfetch_items", "MAX_PROPOSAL_HOSTS"]

#: How many distinct proposed hosts one panel item names. A determinations
#: panel is a list a person reads; past a handful the actionable fact is
#: "there are a lot", and the rest is in ``trialerror webfetch proposals``.
MAX_PROPOSAL_HOSTS = 10

#: Characters a host may contribute to this panel. Anything else becomes
#: nothing — a proposal line is untrusted text, and a host that arrives
#: carrying markup, a newline or a pipe is a host name that was never
#: produced by the URL checker in the first place.
_HOST_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_")

_MAX_HOST_LEN = 253
_MAX_URL_LEN = 200


def _safe_host(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = "".join(c for c in value if c in _HOST_OK)[:_MAX_HOST_LEN]
    return cleaned or None


def _safe_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = "".join(c for c in value if 32 <= ord(c) < 127 and c not in "|<>")
    cleaned = cleaned[:_MAX_URL_LEN]
    return cleaned or None


def _queue_for(rostore: RoStore) -> Queue | None:
    """The queue directory this program's config points at, or ``None``.

    Everything that can go wrong here — no program root, no config, web
    fetching switched off, a config the loader refuses, a queue directory
    that does not exist — means the same thing for a dashboard: there is no
    web-ingestion line to draw. A panel builder never raises.
    """
    program_root = getattr(rostore, "program_root", None)
    if program_root is None:
        return None

    from trialerror.util.config import CONFIG_FILENAME, load_config

    config_path = program_root / CONFIG_FILENAME
    raw: dict[str, Any] = {}
    if config_path.is_file():
        try:
            raw = load_config(config_path).raw
        except Exception:  # noqa: BLE001 - a broken config is doctor's story, not HOME's
            return None
    try:
        config = load_webfetch_config(raw)
    except WebFetchConfigError:
        return None
    if not config.enabled:
        return None
    root = config.queue_path(program_root)
    if not root.is_dir():
        return None
    return Queue(root)


def _proposal_hosts(queue: Queue) -> tuple[list[str], dict[str, str], int]:
    """``(hosts, example_url_by_host, line_count)`` from ``proposals.jsonl``.

    Deduplicated by host and ordered by first appearance: the operator
    approves a *host*, not a line, so ten proposals for one host are one
    decision.
    """
    path = queue.proposals_path
    if not path.is_file():
        return [], {}, 0
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:  # pragma: no cover - defensive
        return [], {}, 0

    hosts: list[str] = []
    examples: dict[str, str] = {}
    counted = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        host = _safe_host(record.get("host"))
        if host is None:
            continue
        counted += 1
        if host not in examples:
            hosts.append(host)
            url = _safe_url(record.get("example_url"))
            if url:
                examples[host] = url
    return hosts, examples, counted


def webfetch_items(rostore: RoStore) -> list[dict[str, Any]]:
    """Zero to two items: hosts awaiting approval, and a stopped fetch
    process with work queued behind it."""
    queue = _queue_for(rostore)
    if queue is None:
        return []

    items: list[dict[str, Any]] = []

    hosts, examples, lines = _proposal_hosts(queue)
    if hosts:
        shown = hosts[:MAX_PROPOSAL_HOSTS]
        listed = ", ".join(shown)
        if len(hosts) > len(shown):
            listed += f", +{len(hosts) - len(shown)} more"
        items.append(
            {
                "kind": "webfetch_proposals",
                "id": "webfetch_proposals",
                "hosts": shown,
                "host_count": len(hosts),
                "proposal_lines": lines,
                "examples": {h: examples[h] for h in shown if h in examples},
                "blocking": False,
                "summary": f"{len(hosts)} host(s) await approval before they can be fetched",
                "consequence": (
                    f"{len(hosts)} host(s) were asked for and cannot be fetched until a human "
                    f"approves them: {listed}. Approval is not available from in here by design "
                    "(ruling L-A2) — the allowlist is a file on the operator's own machine, which "
                    "is what stops an injected agent naming a host to send data to. Run "
                    "`te-webfetch.sh review` there (or the WebFetch Review launcher) to see each "
                    "host with the launch that asked for it and decide y/n. Until then the URLs "
                    "sit refused with `host_not_allowed`, which is a recorded result, not a retry "
                    "loop."
                ),
            }
        )

    age = queue.sidecar_heartbeat_age_s()
    pending = len(queue.pending_job_ids())
    if pending and (age is None or age > SIDECAR_DEAD_S):
        waited = "has never run against this queue" if age is None else f"went quiet {age / 60:.0f} minutes ago"
        items.append(
            {
                "kind": "webfetch_sidecar_down",
                "id": "webfetch_sidecar_down",
                "pending": pending,
                "heartbeat_age_s": age,
                "blocking": True,
                "summary": f"{pending} URL(s) wait on a fetch process that is not running",
                "consequence": (
                    f"The fetch process {waited}, and {pending} URL(s) are queued behind it. "
                    "Nothing is lost — each job parks and retries rather than failing — but no "
                    "page reaches the corpus until it is back, and every stage after it "
                    "(extract, chunk, embed, index) waits with them. On the deployment that "
                    "means bringing the fetch container back up; on a workstation it means "
                    "`trialerror webfetch sidecar --foreground` in its own terminal."
                ),
            }
        )

    return items
