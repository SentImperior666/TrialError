"""``trialerror-ops`` — the Tool Orchestrator MCP server. Design Section 12 (M14
row). Design Section 5.1's ``trialerror-ops`` table (side-effecting; structured
errors, never exceptions) named 12 tools. Nine of them were retired in Phase 0
(0 uses in the audit window); three remain:

======================  ==========================================================
Tool                    Landed API wrapped
======================  ==========================================================
session_status          trialerror.sessions.lifecycle.session_status                (M6)
book_launch             trialerror.budget.pools.book_launch                         (M3)
read_inbox              trialerror.events.api.read_inbox                           (M5)
======================  ==========================================================

Every handler is a THIN wrapper (design's binding instruction to this
module): parse the MCP ``arguments`` dict, call the landed subsystem
function, shape the result as a ``trialerror.util.envelope`` dict. No policy
lives here — everything else uses the ``trialerror`` CLI directly (design
Section 2's composition rule: "one-shot structured operation -> CLI
subcommand"). The exact three names are asserted by
``tests/test_mcp_ops_tools.py``.

**Cross-cutting per-call log line (M15, INTEGRATION_NOTES.md item 13 --
parity with ``trialerror.mcp.knowledge``):** ``DESIGN_v0.md`` Appendix B: "per-
call log line (tool, input-hash, latency, output-size, error-code) ->
events". :func:`_input_hash`/:func:`_log_call`, folded into :func:`_wrap`
exactly the way ``trialerror.mcp.knowledge._wrap`` does it, so both servers log
the identical shape of ``mcp_tool_call`` event, thin and best-effort (a
logging failure never fails the tool call itself).
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Mapping

from trialerror import __version__
from trialerror.budget.errors import (
    LensNameRefusedError,
    ModelPolicyViolationError,
    NoOpenSessionError,
    UnknownAssignmentError,
    UnknownOverrideRulingError,
)
from trialerror.budget.gate import resolve_booking_identity, resolve_open_session
from trialerror.budget.pools import book_launch as book_launch_api
from trialerror.budget.pools import check_booking_preconditions
from trialerror.events.api import append_event as append_event_api
from trialerror.events.api import read_inbox as read_inbox_api
from trialerror.mcp.protocol import ToolServer, ToolSpec, serve_stdio
from trialerror.sessions.lifecycle import session_status as session_status_api
from trialerror.stores.errors import StoreError
from trialerror.stores.store import Store, open_store
from trialerror.util.config import ConfigError, load_config
from trialerror.util.envelope import error_envelope, next_action, ok_envelope

__all__ = ["SERVER_NAME", "TOOL_COUNT", "build_tools", "build_server", "run_server"]

SERVER_NAME = "trialerror-ops"
SERVER_INSTRUCTIONS = (
    "Side-effecting operations over TrialError's session/budget/events stores. Every tool returns a structured {ok, result|error} envelope -- never raise "
    "on a business refusal. See design docs/DESIGN_v0.md Section 5.1 for the full contract."
)
#: Three tools. Nine (``budget_status``, ``reconcile_launch``, ``append_event``,
#: ``post_feed``, ``law_lookup``, ``register_artifact``, ``gate_advance``,
#: ``prereg_commit``, ``record_verdict``) were retired in Phase 0 (0 uses);
#: restorable from git.
TOOL_COUNT = 3


# ---------------------------------------------------------------------------
# small shared helpers (kept here, in-lane -- nothing in trialerror.cli is reused
# so this module never imports across a sibling CLI-group lane)
# ---------------------------------------------------------------------------


def _load_model_policy(program_root: Path) -> dict[str, str] | None:
    """Best-effort ``[models]`` policy load -- same "read generically,
    tolerate absence" convention every CLI group's own ``_load_policy``/
    ``_open_store`` helper uses (e.g. ``trialerror/cli/budget.py``); duplicated
    here rather than imported since CLI-group internals are another lane's
    private (leading-underscore) helpers, not a shared module."""
    try:
        config = load_config(program_root / "trialerror.toml")
    except ConfigError:
        return None
    return dict(config.models) if config.models else None


def _resolve_session_id(store: Store, given: str | None) -> tuple[str | None, dict[str, Any] | None]:
    """``given`` if supplied, else the program's one OPEN session --
    returns ``(session_id, error_details)``; ``error_details`` is not
    ``None`` iff no session_id could be resolved (mirrors
    ``trialerror.budget.gate.evaluate_spawn_for_open_session``'s own
    auto-resolution convenience for the identical "which session" question)."""
    if given:
        return given, None
    open_session = resolve_open_session(store)
    if open_session is None:
        return None, {
            "code": "no_open_session",
            "message": "no session_id given and no OPEN session in this program's ops.db "
            "(boot a session first: `trialerror session boot`)",
        }
    return open_session["session_id"], None


# ---------------------------------------------------------------------------
# 1. session_status (M6)
# ---------------------------------------------------------------------------


def _tool_session_status(args: Mapping[str, Any], *, store: Store) -> dict[str, Any]:
    result = session_status_api(store, session_id=args.get("session_id"))
    return ok_envelope("session_status", result=result)


# ---------------------------------------------------------------------------
# 3. book_launch (M3) -- "no account_id param, derived; requires open session"
# ---------------------------------------------------------------------------


def _read_assign_ids(value: Any) -> list[str] | None:
    """Coerce the ``assign_ids`` argument, refusing the shapes that would
    otherwise book something nobody asked for (fix pass B-1).

    A JSON caller that passes the single id as a string instead of a
    one-element array used to have it comprehended character by character
    into eight ids that name no row. Raises ``ValueError``, which this
    module's wrapper answers as ``bad_input``."""
    if value is None:
        return None
    if isinstance(value, (str, bytes)):
        raise ValueError(
            f"assign_ids must be an array of assign ids, not a single string ({value!r} would "
            "read as one id per character); pass [\"<assign_id>\"]"
        )
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"assign_ids must be an array of assign ids, got {type(value).__name__}")
    return [str(a) for a in value] or None


def _tool_book_launch(args: Mapping[str, Any], *, store: Store) -> dict[str, Any]:
    from trialerror.budget.quota import booking_quota_reading, booking_refusal

    # Lane FB-7 item 6: the same defaults `trialerror budget book` takes,
    # from the same function -- this is the surface an orchestrator actually
    # books through, and two verbs that resolve "which session, which
    # program" differently is how a booking lands under the wrong one.
    try:
        session_id, program_id, resolved_from = resolve_booking_identity(
            store, session_id=args.get("session_id"), program_id=args.get("program_id")
        )
    except NoOpenSessionError as exc:
        return error_envelope(
            "book_launch", "no_open_session", str(exc),
            next_actions=[next_action(["trialerror", "session", "boot"], "boot a session")],
        )
    except RuntimeError as exc:
        return error_envelope(
            "book_launch", "multiple_open_sessions",
            f"{exc} -- pass session_id explicitly, or close one",
        )
    except ValueError as exc:
        return error_envelope("book_launch", "program_id_unresolved", str(exc))
    policy = _load_model_policy(store.program_root)
    # Fix pass V-2: every argument is read and coerced HERE, before the quota
    # gate. A bad `est_tokens` raises ValueError/TypeError/KeyError, which
    # `_wrap` turns into `bad_input` -- the answer the caller needs. Parsing
    # it after the gate meant a malformed call came back
    # `stale_quota_capture`, i.e. a diagnosis of the machine for a fault in
    # the request.
    booking = {
        "program_id": program_id,
        "agent_kind": args["agent_kind"],
        "model_class": args["model_class"],
        "model": args["model"],
        "purpose": args["purpose"],
        "est_tokens": int(args["est_tokens"]),
        "booking_ttl_s": int(args["booking_ttl_s"]) if args.get("booking_ttl_s") is not None else 3600,
        "parent_launch": args.get("parent_launch"),
        "workpackage": args.get("workpackage"),
        "override_ruling_id": args.get("override_ruling_id"),
        # The lens-launch link (lane FB-4 item 5): which lens_assignment rows
        # this booking covers. Read here with the rest of the arguments, so a
        # malformed value answers `bad_input` rather than a diagnosis of the
        # quota capture. Fix pass B-1: a bare string is refused here rather
        # than comprehended into one id per character.
        "assign_ids": _read_assign_ids(args.get("assign_ids")),
        # Lane R0-B items 1/3, kept on all three surfaces: who this launch
        # IS (recorded as launch.attrs.lens_name, which is the identity a
        # room counts turns by), and which binding of that lens this is.
        # Both judged inside `book_launch` before anything is written.
        "lens_name": args.get("lens_name"),
        "phase": args.get("phase"),
    }
    try:
        # ... and the session/policy rungs of book_launch's own ladder before
        # it too, so a booking from a closed session says so.
        check_booking_preconditions(
            store,
            session_id=session_id,
            purpose=booking["purpose"],
            model_class=booking["model_class"],
            policy=policy,
            override_ruling_id=booking["override_ruling_id"],
        )
    except NoOpenSessionError as exc:
        return error_envelope("book_launch", "no_open_session", str(exc))
    except (ModelPolicyViolationError, UnknownOverrideRulingError) as exc:
        return error_envelope("book_launch", "model_policy_violation", str(exc))
    # Lane FB-3 item 8. The same guard `trialerror budget book` applies, from
    # the same function: this is the surface an orchestrator actually books
    # through, and a refusal that fired on only the CLI would be one nothing
    # ever met.
    quota_reading = booking_quota_reading(store.program_root)
    refusal = booking_refusal(quota_reading, flag="allow_stale_quota: true")
    allow_stale = bool(args.get("allow_stale_quota"))
    if refusal and not allow_stale:
        return error_envelope(
            "book_launch", "stale_quota_capture", refusal, details=quota_reading,
            next_actions=[next_action(["trialerror", "budget", "quota"], "re-read the capture")],
        )
    attrs = dict(args.get("attrs") or {})
    if refusal and allow_stale:
        attrs["allowed_stale_quota"] = quota_reading
    try:
        result = book_launch_api(
            store,
            session_id=session_id,
            attrs=attrs or None,
            policy=policy,
            **booking,
        )
    except NoOpenSessionError as exc:
        return error_envelope("book_launch", "no_open_session", str(exc))
    except (ModelPolicyViolationError, UnknownOverrideRulingError) as exc:
        return error_envelope("book_launch", "model_policy_violation", str(exc))
    except UnknownAssignmentError as exc:
        return error_envelope("book_launch", "unknown_assignment", str(exc))
    except LensNameRefusedError as exc:
        return error_envelope("book_launch", "lens_name_refused", str(exc))

    payload = result.to_dict()
    payload["resolved_from"] = resolved_from
    if not result.ok:
        return error_envelope(
            "book_launch", f"book_{result.state.lower()}",
            result.reason or f"booking not created as PROVISIONAL (state={result.state})",
            details=payload,
        )
    return ok_envelope("book_launch", result=payload, meta={"prompt_fragment": f"launch_id: {result.launch_id}"})


# ---------------------------------------------------------------------------
# 7. read_inbox (M5)
# ---------------------------------------------------------------------------


def _tool_read_inbox(args: Mapping[str, Any], *, store: Store) -> dict[str, Any]:
    mark_read = bool(args.get("mark_read", True))
    items = read_inbox_api(store, session_id=args.get("session_id"), mark_read=mark_read)
    return ok_envelope("read_inbox", result={"items": items, "count": len(items)})


# ---------------------------------------------------------------------------
# tool registry + server assembly
# ---------------------------------------------------------------------------


def _input_hash(arguments: Mapping[str, Any]) -> str:
    """Verbatim port of ``trialerror.mcp.knowledge._input_hash`` (M15,
    INTEGRATION_NOTES.md item 13 -- see module TRIALERROR-DEV-NOTE above)."""
    encoded = json.dumps(dict(arguments), sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _log_call(store: Store, *, name: str, arguments: Mapping[str, Any], envelope: Mapping[str, Any], elapsed_ms: float) -> None:
    """Verbatim port of ``trialerror.mcp.knowledge._log_call`` (M15,
    INTEGRATION_NOTES.md item 13): Appendix B cross-cutting rule "per-call
    log line (tool, input-hash, latency, output-size, error-code) ->
    events". Best-effort -- a logging failure (e.g. a redaction-pass
    surprise) must never turn a successful tool call into a failed one."""
    try:
        error_code = None if envelope.get("ok") else (envelope.get("error") or {}).get("code")
        append_event_api(
            store,
            event_type="mcp_tool_call",
            payload={
                "server": SERVER_NAME,
                "tool": name,
                "input_hash": _input_hash(arguments),
                "latency_ms": elapsed_ms,
                "output_size": len(json.dumps(envelope, default=str, ensure_ascii=False)),
                "error_code": error_code,
            },
        )
    except Exception:  # noqa: BLE001 -- logging is never allowed to break a tool call
        pass


def _wrap(name: str, description: str, input_schema: dict[str, Any], fn, *, program_root: Path, platform_root: Path | None):
    """Bind one handler to a fresh :class:`~trialerror.stores.store.Store` per
    call (opened and closed exactly once per ``tools/call`` — the same
    per-invocation lifecycle every ``trialerror`` CLI command uses, e.g.
    ``trialerror/cli/budget.py``'s ``_open_store``/``store.close()`` pairing),
    turn any leaked :class:`~trialerror.stores.errors.StoreError` into a
    structured envelope rather than letting it become an unhandled
    exception at the transport layer (belt-and-suspenders on top of
    ``trialerror.mcp.protocol.serve_stdio``'s own catch-all), and log the
    per-call event line (see :func:`_log_call`)."""

    def handler(arguments: Mapping[str, Any]) -> dict[str, Any]:
        # FX-3 (IMPL_REVIEW_C_ops.md N-2, same pattern as trialerror.mcp.knowledge):
        # the store MUST close on every exit path, including a handler
        # exception of a type not listed below -- `with` guarantees
        # Store.__exit__/close() runs even when that exception propagates
        # past this function entirely, so no path can strand the 4 WAL
        # connections in this long-lived server.
        with open_store(program_root, platform_root=platform_root) as store:
            t0 = time.perf_counter()
            try:
                envelope = fn(arguments, store=store)
            except StoreError as exc:
                envelope = error_envelope(name, "store_error", str(exc))
            except (ValueError, TypeError, KeyError) as exc:
                # A malformed argument that made it past trialerror.mcp.protocol's
                # required-field check (a bad TYPE, or a caller invoking this
                # handler directly rather than through tools/call) must still
                # come back structured -- design Section 5.1 cross-cutting
                # rule applied at this layer too, not just at the transport.
                envelope = error_envelope(name, "bad_input", f"{type(exc).__name__}: {exc}")
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
            _log_call(store, name=name, arguments=arguments, envelope=envelope, elapsed_ms=elapsed_ms)
            return envelope

    return ToolSpec(name=name, description=description, input_schema=input_schema, handler=handler)


def build_tools(*, program_root: Path, platform_root: Path | None = None) -> dict[str, ToolSpec]:
    """Build the exact 3-tool registry (design Section 5.1, less the nine retired in Phase 0), each bound to
    ``program_root``/``platform_root`` for the lifetime of one server
    process (design's own worked example: ``trialerror mcp ops``, scoped to one
    program, mirroring every CLI group's ``--program-root``)."""
    w = lambda *a, **kw: _wrap(*a, **kw, program_root=program_root, platform_root=platform_root)  # noqa: E731

    tools = {
        "session_status": w(
            "session_status",
            "Open session, queue, dangling launches, pin state (design Section 5.1 tool #1, "
            "wraps trialerror.sessions.lifecycle.session_status). Read-only; no side effects.",
            {
                "type": "object",
                "properties": {"session_id": {"type": "string", "description": "default: the currently open session"}},
            },
            _tool_session_status,
        ),
        "book_launch": w(
            "book_launch",
            "Create a PROVISIONAL booking -> launch_id token (refuses over-cap); tool #3, wraps "
            "trialerror.budget.pools.book_launch. session_id defaults to the program's one open session; "
            "account_id is never accepted -- it is derived from that session. Refuses when the plan-quota "
            "capture is older than [budget] quota_max_age_s unless allow_stale_quota is true, which is "
            "recorded on the launch. assign_ids links a lens booking to the lens_assignment rows it "
            "covers, which is how the retrieval scope and the citation audit resolve its slice; the "
            "first binding of an assignment row is never overwritten, so a lens booked again for a "
            "later phase keeps the first launch's join and gains its own. lens_name declares who this "
            "launch is (launch.attrs.lens_name -- the identity a room counts turns by, so a seat "
            "spawned per turn is still one author); with assign_ids the assignment rows stay "
            "authoritative and a disagreeing name refuses the booking. phase labels this binding and "
            "needs assign_ids to label. "
            "session_id defaults to the program's single OPEN session and program_id to [program] id "
            "in trialerror.toml; the result says which of them was resolved rather than given.",
            {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "program_id": {"type": "string"},
                    "agent_kind": {"type": "string"},
                    "model_class": {"type": "string", "enum": ["top", "mid", "small"]},
                    "model": {"type": "string"},
                    "purpose": {"type": "string"},
                    "est_tokens": {"type": "integer"},
                    "booking_ttl_s": {"type": "integer"},
                    "parent_launch": {"type": "string"},
                    "workpackage": {"type": "string"},
                    "attrs": {"type": "object"},
                    "assign_ids": {"type": "array", "items": {"type": "string"}},
                    "lens_name": {"type": "string"},
                    "phase": {"type": "string"},
                    "override_ruling_id": {"type": "string"},
                    "allow_stale_quota": {"type": "boolean"},
                },
                "required": ["agent_kind", "model_class", "model", "purpose", "est_tokens"],
            },
            _tool_book_launch,
        ),
        "read_inbox": w(
            "read_inbox",
            "Unread user inbox items; marks them read by default (tool #7, wraps "
            "trialerror.events.api.read_inbox).",
            {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "mark_read": {"type": "boolean", "description": "default true"},
                },
            },
            _tool_read_inbox,
        ),
    }
    assert len(tools) == TOOL_COUNT, f"trialerror-ops must expose exactly {TOOL_COUNT} tools, got {len(tools)}"
    return tools


def build_server(*, program_root: Path, platform_root: Path | None = None) -> ToolServer:
    return ToolServer(
        name=SERVER_NAME,
        version=__version__,
        tools=build_tools(program_root=program_root, platform_root=platform_root),
        instructions=SERVER_INSTRUCTIONS,
    )


def run_server(
    *,
    program_root: Path | str,
    platform_root: Path | str | None = None,
    stdin=None,
    stdout=None,
    stderr=None,
) -> None:
    """Entry point for ``trialerror mcp ops`` (design's own worked example,
    Section 12 M14 row). Blocks serving stdio until stdin hits EOF."""
    server = build_server(
        program_root=Path(program_root),
        platform_root=Path(platform_root) if platform_root is not None else None,
    )
    serve_stdio(server, stdin=stdin, stdout=stdout, stderr=stderr)
