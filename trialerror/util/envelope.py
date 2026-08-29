"""AgentEnvelope — the one output shape every ``trialerror`` CLI command emits.

Design Section 5.2: "Every command emits the pdf-brain ``AgentEnvelope``
shape: ``{ok, command, protocolVersion, result|error,
nextActions[{kind:"shell",argv,description}], meta}`` (NDJSON-compatible;
``--format text`` for humans)."

Ported close to verbatim from the pdf-brain pattern (docs/mining/
G20-docstruct-1__pdf-brain.md): a versioned, discriminated-union envelope
so an agent parsing CLI output never has to guess the shape, plus a
HATEOAS-style ``nextActions`` array suggesting the literal next shell
command — scaffolding that reduces subagent flailing per the mining note.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "PROTOCOL_VERSION",
    "NextAction",
    "next_action",
    "make_envelope",
    "ok_envelope",
    "error_envelope",
    "to_json_line",
    "render_text",
    "emit",
]

#: Bump on any breaking change to the envelope shape itself. Reported at
#: MCP ``initialize`` too (design Section 5.1 cross-cutting rule) so both
#: surfaces share one version number.
PROTOCOL_VERSION = "1.0.0"


@dataclass(frozen=True)
class NextAction:
    kind: str  # currently always "shell"
    argv: Sequence[str]
    description: str | None = None

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"kind": self.kind, "argv": list(self.argv)}
        if self.description is not None:
            d["description"] = self.description
        return d


def next_action(argv: Sequence[str], description: str | None = None) -> NextAction:
    """Build a ``{kind:"shell", argv, description?}`` next-action entry."""
    return NextAction(kind="shell", argv=list(argv), description=description)


def _coerce_next_actions(
    next_actions: Iterable[NextAction | Mapping[str, Any]] | None,
) -> list[dict]:
    if not next_actions:
        return []
    out = []
    for na in next_actions:
        out.append(na.to_dict() if isinstance(na, NextAction) else dict(na))
    return out


def make_envelope(
    *,
    ok: bool,
    command: str,
    result: Any = None,
    error: Mapping[str, Any] | None = None,
    next_actions: Iterable[NextAction | Mapping[str, Any]] | None = None,
    meta: Mapping[str, Any] | None = None,
) -> dict:
    """Build an envelope dict. Exactly one of ``result``/``error`` applies,
    selected by ``ok`` — the other must not be supplied (discriminated
    union, enforced at construction so a caller can't accidentally emit a
    ``ok:true`` envelope carrying an ``error`` block or vice versa)."""
    if ok and error is not None:
        raise ValueError("ok=True envelopes must not carry an error block")
    if not ok and result is not None:
        raise ValueError("ok=False envelopes must not carry a result block")
    if not ok and not error:
        raise ValueError("ok=False envelopes require an error block")

    env: dict[str, Any] = {
        "ok": ok,
        "command": command,
        "protocolVersion": PROTOCOL_VERSION,
    }
    if ok:
        env["result"] = result if result is not None else {}
    else:
        env["error"] = dict(error)  # type: ignore[arg-type]
    env["nextActions"] = _coerce_next_actions(next_actions)
    env["meta"] = dict(meta) if meta else {}
    return env


def ok_envelope(
    command: str,
    result: Any = None,
    *,
    next_actions: Iterable[NextAction | Mapping[str, Any]] | None = None,
    meta: Mapping[str, Any] | None = None,
) -> dict:
    """Shorthand for ``make_envelope(ok=True, ...)``."""
    return make_envelope(
        ok=True, command=command, result=result, next_actions=next_actions, meta=meta
    )


def error_envelope(
    command: str,
    code: str,
    message: str,
    *,
    details: Mapping[str, Any] | None = None,
    next_actions: Iterable[NextAction | Mapping[str, Any]] | None = None,
    meta: Mapping[str, Any] | None = None,
) -> dict:
    """Shorthand for ``make_envelope(ok=False, ...)`` with a structured
    ``{code, message, details?}`` error (design Section 5.1 cross-cutting
    rule: "errors returned as structured content ... never exceptions")."""
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = dict(details)
    return make_envelope(
        ok=False, command=command, error=error, next_actions=next_actions, meta=meta
    )


def to_json_line(envelope: Mapping[str, Any]) -> str:
    """Render an envelope as one compact, NDJSON-compatible line."""
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))


def render_text(envelope: Mapping[str, Any]) -> str:
    """Human-readable rendering for ``--format text``."""
    lines: list[str] = []
    status = "OK" if envelope.get("ok") else "FAIL"
    lines.append(f"[{status}] {envelope.get('command')} (protocol {envelope.get('protocolVersion')})")
    if envelope.get("ok"):
        result = envelope.get("result")
        lines.append(f"result: {json.dumps(result, ensure_ascii=False, indent=2)}")
    else:
        error = envelope.get("error") or {}
        lines.append(f"error [{error.get('code')}]: {error.get('message')}")
        if error.get("details"):
            lines.append(f"details: {json.dumps(error['details'], ensure_ascii=False, indent=2)}")
    next_actions = envelope.get("nextActions") or []
    if next_actions:
        lines.append("next actions:")
        for na in next_actions:
            argv_str = " ".join(str(a) for a in na.get("argv", []))
            desc = f"  # {na['description']}" if na.get("description") else ""
            lines.append(f"  $ {argv_str}{desc}")
    return "\n".join(lines)


def emit(envelope: Mapping[str, Any], fmt: str = "json") -> None:
    """Print an envelope to stdout in the requested format."""
    if fmt == "text":
        print(render_text(envelope))
    else:
        print(to_json_line(envelope))
