"""trialerror.util — platform-skeleton primitives shared by every other subsystem.

Modules:
    ids       ULIDs with human-readable typed prefixes (design Section 4).
    timeutil  ``now()`` — the ONE clock every store write uses (ISO-8601 UTC).
    atomic    ``os.replace``-based atomic file writes (survive kill-mid-write).
    envelope  the AgentEnvelope shape every CLI command emits (design Section 5.2).
    config    ``trialerror.toml`` loader + program-root discovery (design Section 3.2).
    doctor    the per-module check-registry framework (design Section 5.2/10).
    checks    M0's own doctor checks (currently: ``license_audit``).
"""
