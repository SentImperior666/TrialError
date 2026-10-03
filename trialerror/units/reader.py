"""Read one Claude Code transcript file (main, subagent, or workflow
journal) into a :class:`TranscriptSummary`. Design Section 2.2
(``reader.py``).

**The corrected reading rule for streamed messages** (design Section 1):
one message id spans several ``.jsonl`` lines. Early lines of
a streamed message can carry a PLACEHOLDER ``output_tokens`` (observed:
5 -> 730, 3 -> 346) -- the first line of a message is not its true usage.
Per message id:

1. If any line for that id has ``message.stop_reason`` set, take that
   line's usage. If several lines do (a forked/duplicated record), take the
   LAST one processed -- unless that winning line's own ``usage`` is
   ``null``/absent, in which case fall through to rule 2 (a stop_reason
   line with no usage dict is not evidence of a genuinely zero-cost
   message; "null usage tolerated" applies here too).
2. Otherwise take each usage field's maximum across every line for that id.
3. ``cache_creation.ephemeral_1h_input_tokens``/``ephemeral_5m_input_tokens``
   follow the identical rule.

Trap 6 (performance): this reads the file **line by line** and keeps only
one small running-state dict keyed by message id -- never the whole file, or
even a whole message's lines, in memory. Trap 2: only ids, keys, counts and
timestamps are ever read out of a record; ``message.content``/``text`` (the
actual conversation) is never touched.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["MessageUsage", "TranscriptSummary", "read_transcript"]

#: model name Claude Code writes for a line that carries no real usage (a
#: tool-only or system-synthesized turn) -- design Section 2.2: "Skip
#: model == '<synthetic>'".
SYNTHETIC_MODEL = "<synthetic>"

_ZERO6 = (0, 0, 0, 0, 0, 0)


@dataclass(frozen=True)
class MessageUsage:
    ts: str | None
    model: str | None
    input_tokens: int
    cache_write: int
    cache_read: int
    output: int
    cache_write_1h: int
    cache_write_5m: int


@dataclass
class TranscriptSummary:
    messages: dict[str, MessageUsage] = field(default_factory=dict)
    models: set[str] = field(default_factory=set)
    first_ts: str | None = None
    last_ts: str | None = None
    conversation_last_ts: str | None = None
    entrypoint: str | None = None
    version: str | None = None
    lines_read: int = 0
    lines_skipped: int = 0
    sha256: str = ""


def _int_or_zero(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _usage_six(usage: Any) -> tuple[int, int, int, int, int, int]:
    if not isinstance(usage, dict):
        return _ZERO6
    cache_creation = usage.get("cache_creation")
    cw1h = cw5m = 0
    if isinstance(cache_creation, dict):
        cw1h = _int_or_zero(cache_creation.get("ephemeral_1h_input_tokens"))
        cw5m = _int_or_zero(cache_creation.get("ephemeral_5m_input_tokens"))
    return (
        _int_or_zero(usage.get("input_tokens")),
        _int_or_zero(usage.get("cache_creation_input_tokens")),
        _int_or_zero(usage.get("cache_read_input_tokens")),
        _int_or_zero(usage.get("output_tokens")),
        cw1h,
        cw5m,
    )


class _MidState:
    __slots__ = (
        "has_stop", "stop_usage", "stop_usage_present", "stop_ts", "stop_model", "max_usage", "last_ts", "model",
    )

    def __init__(self) -> None:
        self.has_stop = False
        self.stop_usage: tuple[int, int, int, int, int, int] = _ZERO6
        #: N-3 fix: whether the WINNING stop_reason line's own ``usage`` was
        #: an actual dict, not merely absent/null. A stop_reason line with
        #: ``usage: null`` must not be read as "this message genuinely cost
        #: zero" -- it means the host recorded no usage on that particular
        #: line, and the per-field maximum across every other line for this
        #: id is the safer answer (matching the "null usage tolerated"
        #: principle already applied elsewhere in this reader).
        self.stop_usage_present = False
        self.stop_ts: str | None = None
        self.stop_model: str | None = None
        self.max_usage: list[int] = [0, 0, 0, 0, 0, 0]
        self.last_ts: str | None = None
        self.model: str | None = None


def read_transcript(path: Path | str) -> TranscriptSummary:
    """Stream ``path`` line by line and return its :class:`TranscriptSummary`.

    Tolerant throughout: a line that is not valid JSON, or not a JSON
    object, is counted in ``lines_skipped`` and otherwise ignored -- a
    single malformed line (a torn write mid-crash, a stray blank line) must
    never abort the whole scan.
    """
    summary = TranscriptSummary()
    states: dict[str, _MidState] = {}
    digest = hashlib.sha256()

    with open(path, "rb") as fh:
        for raw_bytes in fh:
            digest.update(raw_bytes)
            line = raw_bytes.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (ValueError, TypeError):
                summary.lines_skipped += 1
                continue
            if not isinstance(record, dict):
                summary.lines_skipped += 1
                continue
            summary.lines_read += 1

            ts = record.get("timestamp")
            if isinstance(ts, str) and ts:
                if summary.first_ts is None or ts < summary.first_ts:
                    summary.first_ts = ts
                if summary.last_ts is None or ts > summary.last_ts:
                    summary.last_ts = ts
                rtype = record.get("type")
                if rtype in ("user", "assistant"):
                    if summary.conversation_last_ts is None or ts > summary.conversation_last_ts:
                        summary.conversation_last_ts = ts

            if summary.entrypoint is None:
                ep = record.get("entrypoint")
                if isinstance(ep, str) and ep:
                    summary.entrypoint = ep
            if summary.version is None:
                ver = record.get("version")
                if isinstance(ver, str) and ver:
                    summary.version = ver

            if record.get("type") != "assistant":
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            model = message.get("model")
            if model == SYNTHETIC_MODEL:
                continue

            mid = message.get("id") or record.get("requestId") or record.get("uuid")
            if not mid or not isinstance(mid, str):
                continue

            if isinstance(model, str) and model:
                summary.models.add(model)

            st = states.setdefault(mid, _MidState())
            if isinstance(ts, str) and ts:
                st.last_ts = ts
            if isinstance(model, str) and model:
                st.model = model

            usage_six = _usage_six(message.get("usage"))
            st.max_usage = [max(a, b) for a, b in zip(st.max_usage, usage_six)]

            if message.get("stop_reason") is not None:
                st.has_stop = True
                st.stop_usage = usage_six
                st.stop_usage_present = isinstance(message.get("usage"), dict)
                st.stop_ts = ts if isinstance(ts, str) else None
                st.stop_model = model if isinstance(model, str) else None

    for mid, st in states.items():
        if st.has_stop and st.stop_usage_present:
            six = st.stop_usage
            resolved_ts = st.stop_ts or st.last_ts
            resolved_model = st.stop_model or st.model
        else:
            six = tuple(st.max_usage)
            resolved_ts = st.last_ts
            resolved_model = st.model
        summary.messages[mid] = MessageUsage(
            ts=resolved_ts,
            model=resolved_model,
            input_tokens=six[0],
            cache_write=six[1],
            cache_read=six[2],
            output=six[3],
            cache_write_1h=six[4],
            cache_write_5m=six[5],
        )

    summary.sha256 = digest.hexdigest()
    return summary
