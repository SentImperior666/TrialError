"""Conformance probes, canaries and answer stamps (F5). Design
``design/L3_unit-cost-probes-canaries.md`` Section 3.

A "probe" notices when Claude Code -- or this program's own search index --
has changed under us: which hook payload fields arrive, whether subagent
hooks fire and leave a transcript, whether a session is writing its
conversation at all, and whether a full-text/vector canary still finds a
known passage. Every probe result lands in ``platform.probe_run``.

Deliberately a SEPARATE registry from :mod:`trialerror.util.doctor` (design
Section 3.1): a full doctor reading can take 7-8 minutes, and a probe must
stay inside a small, per-probe timeout instead of inheriting the doctor's
"read everything" budget.
"""

from __future__ import annotations
