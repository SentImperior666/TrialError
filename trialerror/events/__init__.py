"""``trialerror.events`` — the shared events/feed/inbox API. Design Section 12
(M5 row): "append APIs, type-keyed export (workpackage/session-scoped),
redaction, threads/posts/inbox incl. ``inbox post`` + orchestrator-author
fallback." Generalizes origin-project's per-agent JSONL event logs + feed threads +
user inbox (``docs/the origin-project requirements notes`` Sections 1.5/1.7)
into one API-enforced surface (design Section 9.9: "API-enforced
authorship; full text; boot reads inbox").

**Authorship binding (design Section 4.2 ``feed_post`` + the F15 rule
cited in the M5 build brief):** ``author`` is DERIVED by this module from
the caller's own ``launch_id`` (looked up against ``platform.launch`` —
the same cross-store XID target ``trialerror.stores.xid`` already validates —
to read that launch's real ``agent_kind``), or, when no ``launch_id`` is
given, from the currently OPEN session in ``ops.session`` (the
orchestrator has no launch of its own). No function in this module accepts
an ``author`` argument at all — there is no parameter through which a
caller can inject an arbitrary display name, which is what makes "an agent
cannot post as another agent" true structurally rather than by prompt
obedience: the only lever a caller has is *which* ``launch_id`` it passes,
and whatever launch that id names is who the post is attributed to.

**Redaction (design Section 4.2 ``event`` table):** every write in this
module goes through ``trialerror.stores.insert``/``update`` — the M1-built
secret-redaction pass for ``event.payload`` lives there
(``trialerror.stores.writer._apply_event_redaction`` / ``trialerror.stores.redact``)
and this module calls it, deliberately, rather than reimplementing it.

Public surface:

- :func:`append_event` — type-keyed, auto-timestamped event append.
- :func:`tail_events` / :func:`export_events` / :func:`export_jsonl` /
  :func:`render_jsonl` — read-back and jsonl rendering (byte-stable: fixed
  key order, deterministic sort, no nondeterministic content added).
- :func:`create_thread` / :func:`post_feed` / :func:`list_threads` /
  :func:`get_thread_posts` — the feed (full-text, author-bound).
- :func:`post_inbox` / :func:`read_inbox` — the user inbox (``inbox post``
  is "the one API-backed inbox writer", design Section 4.2).
"""

from __future__ import annotations

from trialerror.events.api import (
    append_event,
    create_thread,
    export_events,
    export_jsonl,
    get_thread_posts,
    list_threads,
    post_feed,
    post_inbox,
    read_inbox,
    record_hook_alive_once,
    render_jsonl,
    tail_events,
)

__all__ = [
    "append_event",
    "record_hook_alive_once",
    "tail_events",
    "export_events",
    "export_jsonl",
    "render_jsonl",
    "create_thread",
    "post_feed",
    "list_threads",
    "get_thread_posts",
    "post_inbox",
    "read_inbox",
]
