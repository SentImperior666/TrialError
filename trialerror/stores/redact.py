"""Secret-redaction pass. Design Section 4.2 (event table): "Append-only;
the store API applies a secret-redaction pass (key/token regex set) before
write." Design Section 12 (M1 row): "redaction pass" is part of M1's own
deliverable — this module is that pass; ``trialerror.stores.writer.insert``
calls it automatically for the ``event`` table's ``payload`` column.

Deliberately conservative (biased toward over-redacting): this is a
best-effort net against a copy-pasted API key ending up in an event log
that later gets rendered to a markdown export or read by a subagent, not a
cryptographic guarantee. The pattern set is intentionally small and
reviewable rather than an exhaustive vendor list — new patterns are added
here as they're discovered, one regex at a time.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["REDACTED_PLACEHOLDER", "redact_text", "redact_payload"]

REDACTED_PLACEHOLDER = "[REDACTED]"

#: Each pattern matches a *whole secret token* it's confident about (not a
#: key name alone) so the placeholder swap can't eat surrounding prose.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9]{20,}"),  # OpenAI-style secret keys
    re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}"),  # Anthropic API keys
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key ids
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub tokens (ghp_/gho_/ghu_/ghs_/ghr_)
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),  # Slack tokens
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # JWTs
    # generic "key/secret/token/password: <value>" assignments — captures
    # only the value so the label stays legible in the redacted output.
    re.compile(
        r"(?i)\b(api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*"
        r"['\"]?(?P<value>[A-Za-z0-9._\-/+=]{8,})['\"]?"
    ),
)


def redact_text(text: str) -> tuple[str, int]:
    """Replace every secret-shaped substring of ``text`` with the
    placeholder. Returns ``(redacted_text, count)``."""
    count = 0

    def _sub(pattern: re.Pattern[str], s: str) -> str:
        nonlocal count

        def _repl(m: re.Match[str]) -> str:
            nonlocal count
            count += 1
            if "value" in m.groupdict() and m.group("value") is not None:
                return m.group(0).replace(m.group("value"), REDACTED_PLACEHOLDER)
            return REDACTED_PLACEHOLDER

        return pattern.sub(_repl, s)

    out = text
    for pattern in _SECRET_PATTERNS:
        out = _sub(pattern, out)
    return out, count


def redact_payload(payload: Any) -> tuple[Any, int]:
    """Recursively walk a JSON-like structure (``dict``/``list``/``str``/
    scalars) and redact every string leaf. Returns ``(redacted, count)``
    with ``count`` summed across the whole structure — this is the number
    stored in ``event.redactions``."""
    total = 0
    if isinstance(payload, str):
        redacted, n = redact_text(payload)
        return redacted, n
    if isinstance(payload, dict):
        out_dict: dict[Any, Any] = {}
        for k, v in payload.items():
            r, n = redact_payload(v)
            out_dict[k] = r
            total += n
        return out_dict, total
    if isinstance(payload, list):
        out_list = []
        for v in payload:
            r, n = redact_payload(v)
            out_list.append(r)
            total += n
        return out_list, total
    return payload, 0
