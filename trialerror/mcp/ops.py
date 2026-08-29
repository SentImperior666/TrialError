"""``trialerror-ops`` — the Tool Orchestrator MCP server. Design Section 12 (M14
row): "12 tools (Section 5.1) over M3-M6, M10, Section 8 verdict recording."
Design Section 5.1's ``trialerror-ops`` table (side-effecting; structured errors,
never exceptions) pins the exact 12 tools and their landed-API mapping:

======================  ==========================================================
Tool                    Landed API wrapped
======================  ==========================================================
session_status          trialerror.sessions.lifecycle.session_status                (M6)
budget_status           trialerror.budget.pools.budget_status                       (M3)
book_launch             trialerror.budget.pools.book_launch                         (M3)
reconcile_launch        trialerror.budget.pools.reconcile_launch                    (M3)
append_event            trialerror.events.api.append_event                         (M5)
post_feed               trialerror.events.api.post_feed                            (M5)
read_inbox              trialerror.events.api.read_inbox                           (M5)
law_lookup              trialerror.law.service.{lookup_rulings,verify_pin,
                         diff_foreign}                                        (M4)
register_artifact       trialerror.artifacts.registry.register_artifact            (M10)
gate_advance             trialerror.artifacts.gates.advance_gate                    (M10)
prereg_commit           trialerror.verify.prereg.commit_prereg                     (M9)
record_verdict          trialerror.verify.verdicts.record_verdict                  (M9)
======================  ==========================================================

Every handler is a THIN wrapper (design's binding instruction to this
module): parse the MCP ``arguments`` dict, call the landed subsystem
function, shape the result as a ``trialerror.util.envelope`` dict. No policy
lives here — a caller that needs richer one-shot orchestration (creating an
artifact before it can be registered, opening a gate, submitting it,
recording a GATE's own PASS/FAIL verdict with edits, applying the union,
verifying an edit) uses the ``trialerror`` CLI directly (design Section 2's
composition rule: "one-shot structured operation -> CLI subcommand"); this
server exposes exactly the design's 12-tool table, no more, no fewer
(asserted by ``tests/test_m14_acceptance.py``).

**TRIALERROR-DEV-NOTE (``prereg_commit`` / ``record_verdict`` -- SUPERSEDED by
M9, as anticipated):** M14's own build brief predicted this exactly ("when
M9 lands, its own service functions should supersede these two handlers'
bodies; the MCP tool NAMES and ``inputSchema`` shapes should not need to
change") -- this build (M9) does precisely that and nothing more, per its
own build brief's authorized narrow edit: both handler BODIES below now
call ``trialerror.verify.prereg.commit_prereg``/``trialerror.verify.verdicts.record_verdict``
instead of writing through ``trialerror.stores.insert`` directly; every
observable behavior M14's own test suite (``tests/test_mcp_ops_tools.py``)
already locked in — tool names, ``inputSchema`` shapes, error codes
(``invalid_procedure``/``commit_refused``/``invalid_subject_kind``/
``record_refused``), and result-row shapes — is unchanged and still green
under the ORIGINAL M14 tests, unmodified. ``prereg_commit`` still does not
expose REVEAL (that is ``trialerror prereg reveal``, CLI-only, per the design's
composition rule — this server's own 12-tool table has no reveal tool);
``record_verdict`` still only RECORDS a label/evidence the caller already
computed (citecheck/contracrow/reproduction classification itself runs in
``trialerror.verify.{citecheck,hypothesis,reproduce}``, invoked via ``trialerror
verify ...`` or called directly by an agent, never by this MCP tool).

**Cross-cutting per-call log line (M15, INTEGRATION_NOTES.md item 13 --
parity with ``trialerror.mcp.knowledge``):** ``DESIGN_v0.md`` Appendix B: "per-
call log line (tool, input-hash, latency, output-size, error-code) ->
events". M14's own build brief shipped this server before M8's own
``trialerror/mcp/knowledge.py`` landed the pattern; this build ports it in
verbatim (:func:`_input_hash`/:func:`_log_call`, folded into :func:`_wrap`
exactly the way ``trialerror.mcp.knowledge._wrap`` does it) so both servers now
log the identical shape of ``mcp_tool_call`` event, thin and best-effort (a
logging failure never fails the tool call itself). Every M14 test
(``tests/test_mcp_ops_tools.py``, ``tests/test_mcp_ops_protocol.py``,
``tests/test_m14_acceptance.py`` -- 43 tests total) is unmodified and still
green: this addition only appends a new ``event`` row as a side effect,
touching no envelope shape, tool name, or error code any existing
assertion checks.

**TRIALERROR-DEV-NOTE (``gate_advance`` is the ONLY gate tool, by design):**
``trialerror.artifacts.gates`` also exports ``open_gate``/``submit_gate``/
``record_verdict``(gate)/``apply_union``/``verify_edit`` -- richer, named
wrappers over the same state machine. The Section 5.1 table names exactly
one gate-related ops tool (``gate_advance``, described as "state-machine
transition with evidence (refuses illegal)") which maps 1:1 to
``trialerror.artifacts.gates.advance_gate`` — "the generic, low-level entry
point" per that function's own docstring. The richer verbs stay CLI-only
(``trialerror gate open|submit|verdict|apply-union|verify-edit``), consistent
with the design's composition rule. This ops server's OWN ``record_verdict``
tool is a different thing entirely (see the note above): it writes the
generic Section 4.1 ``verdict`` table (subject_kind/procedure/label/evidence
anchors/prereg_compliant), never ``gate.verdict`` -- the tool description
("verdict row w/ evidence anchors + prereg compliance check") names fields
("evidence anchors", "prereg compliance") that only exist on that table, not
on ``gate``.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Mapping

from trialerror import __version__
from trialerror.artifacts.errors import GateEntryConditionError, IllegalTransitionError, RegistrationRefusedError
from trialerror.artifacts.gates import advance_gate
from trialerror.artifacts.registry import register_artifact as register_artifact_api
from trialerror.budget.errors import BudgetError, ModelPolicyViolationError, NoOpenSessionError, UnknownOverrideRulingError
from trialerror.budget.gate import resolve_open_session
from trialerror.budget.pools import book_launch as book_launch_api
from trialerror.budget.pools import budget_status as budget_status_api
from trialerror.budget.pools import reconcile_launch as reconcile_launch_api
from trialerror.events.api import append_event as append_event_api
from trialerror.events.api import post_feed as post_feed_api
from trialerror.events.api import read_inbox as read_inbox_api
from trialerror.law.service import diff_foreign, lookup_rulings, verify_pin
from trialerror.mcp.protocol import ToolServer, ToolSpec, serve_stdio
from trialerror.sessions.lifecycle import session_status as session_status_api
from trialerror.stores.errors import StoreError, ValidationError, XidTargetMissingError
from trialerror.stores.store import Store, open_store
from trialerror.util.config import ConfigError, load_config
from trialerror.util.envelope import error_envelope, next_action, ok_envelope
from trialerror.verify.errors import InvalidProcedureError, InvalidSubjectKindError
from trialerror.verify.prereg import commit_prereg as verify_commit_prereg
from trialerror.verify.verdicts import record_verdict as verify_record_verdict

__all__ = ["SERVER_NAME", "TOOL_COUNT", "build_tools", "build_server", "run_server"]

SERVER_NAME = "trialerror-ops"
SERVER_INSTRUCTIONS = (
    "Side-effecting operations over TrialError's session/budget/law/events/artifact/gate "
    "stores. Every tool returns a structured {ok, result|error} envelope -- never raise "
    "on a business refusal. See design docs/DESIGN_v0.md Section 5.1 for the full contract."
)
#: Design Section 12 (M14 acceptance): "tool-count <=12 asserted in test."
TOOL_COUNT = 12


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
# 2. budget_status (M3)
# ---------------------------------------------------------------------------


def _tool_budget_status(args: Mapping[str, Any], *, store: Store) -> dict[str, Any]:
    account_id = args.get("account_id")
    if not account_id:
        open_session = resolve_open_session(store)
        if open_session is None:
            return error_envelope(
                "budget_status",
                "no_open_session",
                "no account_id given and no OPEN session to derive it from "
                "(boot a session first: `trialerror session boot`)",
                next_actions=[next_action(["trialerror", "session", "boot"], "boot a session")],
            )
        account_id = open_session["account_id"]
    result = budget_status_api(store, account_id=account_id, model_class=args.get("model_class"))
    return ok_envelope("budget_status", result=result)


# ---------------------------------------------------------------------------
# 3. book_launch (M3) -- "no account_id param, derived; requires open session"
# ---------------------------------------------------------------------------


def _tool_book_launch(args: Mapping[str, Any], *, store: Store) -> dict[str, Any]:
    session_id, err = _resolve_session_id(store, args.get("session_id"))
    if err is not None:
        return error_envelope("book_launch", err["code"], err["message"],
                               next_actions=[next_action(["trialerror", "session", "boot"], "boot a session")])
    policy = _load_model_policy(store.program_root)
    try:
        result = book_launch_api(
            store,
            session_id=session_id,
            program_id=args["program_id"],
            agent_kind=args["agent_kind"],
            model_class=args["model_class"],
            model=args["model"],
            purpose=args["purpose"],
            est_tokens=int(args["est_tokens"]),
            booking_ttl_s=int(args["booking_ttl_s"]) if args.get("booking_ttl_s") is not None else 3600,
            parent_launch=args.get("parent_launch"),
            workpackage=args.get("workpackage"),
            attrs=args.get("attrs"),
            policy=policy,
            override_ruling_id=args.get("override_ruling_id"),
        )
    except NoOpenSessionError as exc:
        return error_envelope("book_launch", "no_open_session", str(exc))
    except (ModelPolicyViolationError, UnknownOverrideRulingError) as exc:
        return error_envelope("book_launch", "model_policy_violation", str(exc))

    payload = result.to_dict()
    if not result.ok:
        return error_envelope(
            "book_launch", f"book_{result.state.lower()}",
            result.reason or f"booking not created as PROVISIONAL (state={result.state})",
            details=payload,
        )
    return ok_envelope("book_launch", result=payload, meta={"prompt_fragment": f"launch_id: {result.launch_id}"})


# ---------------------------------------------------------------------------
# 4. reconcile_launch (M3)
# ---------------------------------------------------------------------------


def _tool_reconcile_launch(args: Mapping[str, Any], *, store: Store) -> dict[str, Any]:
    try:
        result = reconcile_launch_api(
            store,
            launch_id=args["launch_id"],
            actual_tokens=int(args["actual_tokens"]),
            reconcile_source=args.get("reconcile_source") or "manual",
        )
    except BudgetError as exc:
        return error_envelope("reconcile_launch", "reconcile_refused", str(exc))
    return ok_envelope("reconcile_launch", result=result)


# ---------------------------------------------------------------------------
# 5. append_event (M5) -- authorship: N/A (event has no author column; the
#    caller's launch_id/session_id land as-is, never coerced into a name)
# ---------------------------------------------------------------------------


def _tool_append_event(args: Mapping[str, Any], *, store: Store) -> dict[str, Any]:
    try:
        row = append_event_api(
            store,
            event_type=args["event_type"],
            payload=args.get("payload", {}),
            session_id=args.get("session_id"),
            launch_id=args.get("launch_id"),
            workpackage=args.get("workpackage"),
        )
    except (ValidationError, XidTargetMissingError) as exc:
        return error_envelope("append_event", "append_refused", str(exc))
    return ok_envelope("append_event", result=row)


# ---------------------------------------------------------------------------
# 6. post_feed (M5) -- author is DERIVED by trialerror.events.api._derive_author
#    from launch_id/session_id (C-0047); this tool never accepts an author
#    string, matching that contract exactly.
# ---------------------------------------------------------------------------


def _tool_post_feed(args: Mapping[str, Any], *, store: Store) -> dict[str, Any]:
    try:
        row = post_feed_api(
            store,
            thread_id=args["thread_id"],
            body=args["body"],
            launch_id=args.get("launch_id"),
            session_id=args.get("session_id"),
            in_reply_to=args.get("in_reply_to"),
        )
    except (ValidationError, XidTargetMissingError) as exc:
        return error_envelope("post_feed", "post_refused", str(exc))
    return ok_envelope("post_feed", result=row)


# ---------------------------------------------------------------------------
# 7. read_inbox (M5)
# ---------------------------------------------------------------------------


def _tool_read_inbox(args: Mapping[str, Any], *, store: Store) -> dict[str, Any]:
    mark_read = bool(args.get("mark_read", True))
    items = read_inbox_api(store, session_id=args.get("session_id"), mark_read=mark_read)
    return ok_envelope("read_inbox", result={"items": items, "count": len(items)})


# ---------------------------------------------------------------------------
# 8. law_lookup (M4) -- "ruling search + pin verify + foreign-entries diff",
#    one dedicated schema (not an action/params union, Appendix B): the
#    lookup filters always apply; `pin` additionally attaches a verify+diff
#    section when supplied.
# ---------------------------------------------------------------------------


def _tool_law_lookup(args: Mapping[str, Any], *, store: Store) -> dict[str, Any]:
    rows = lookup_rulings(
        store,
        ruling_id=args.get("ruling_id"),
        domain=args.get("domain"),
        status=args.get("status"),
        query=args.get("query"),
    )
    result: dict[str, Any] = {
        "rulings": rows,
        "count": len(rows),
        "pin_check": None,
        "foreign_since_pin": None,
        "foreign_since_pin_error": None,
    }
    pin = args.get("pin")
    if pin:
        result["pin_check"] = verify_pin(store, pin).to_dict()
        try:
            result["foreign_since_pin"] = diff_foreign(store, pin)
        except ValueError as exc:
            result["foreign_since_pin_error"] = str(exc)
    return ok_envelope("law_lookup", result=result)


# ---------------------------------------------------------------------------
# 9. register_artifact (M10)
# ---------------------------------------------------------------------------


def _tool_register_artifact(args: Mapping[str, Any], *, store: Store) -> dict[str, Any]:
    try:
        row = register_artifact_api(
            store,
            artifact_id=args["artifact_id"],
            by_launch=args["by_launch"],
            supersedes=args.get("supersedes"),
        )
    except RegistrationRefusedError as exc:
        return error_envelope("register_artifact", "registration_refused", str(exc))
    except (StoreError, ValueError) as exc:
        return error_envelope("register_artifact", "register_refused", str(exc))
    return ok_envelope("register_artifact", result=row)


# ---------------------------------------------------------------------------
# 10. gate_advance (M10) -- the generic, low-level transition entry point
#     (see module TRIALERROR-DEV-NOTE for why this is the one gate tool exposed)
# ---------------------------------------------------------------------------


def _tool_gate_advance(args: Mapping[str, Any], *, store: Store) -> dict[str, Any]:
    try:
        row = advance_gate(
            store,
            gate_id=args["gate_id"],
            to_state=args["to_state"],
            by_launch=args["by_launch"],
            evidence=args.get("evidence"),
        )
    except GateEntryConditionError as exc:
        return error_envelope("gate_advance", "entry_condition_failed", str(exc))
    except (IllegalTransitionError, StoreError, ValueError) as exc:
        return error_envelope("gate_advance", "transition_refused", str(exc))
    return ok_envelope("gate_advance", result=row)


# ---------------------------------------------------------------------------
# 11. prereg_commit -- real M9 API (trialerror.verify.prereg.commit_prereg); see
#     module TRIALERROR-DEV-NOTE ("SUPERSEDED by M9, as anticipated"). Hash-commits
#     {procedure, params} blind, escrowing the raw content under the
#     PLATFORM tree (design Section 4.2: "OUTSIDE the program repo, so the
#     blind is physical, not conventional") -- identical observable
#     behavior to the pre-M9 thin adapter this replaces.
# ---------------------------------------------------------------------------


def _tool_prereg_commit(args: Mapping[str, Any], *, store: Store) -> dict[str, Any]:
    try:
        written = verify_commit_prereg(store, title=args["title"], procedure=args["procedure"], params=args.get("params"))
    except InvalidProcedureError as exc:
        return error_envelope("prereg_commit", "invalid_procedure", str(exc))
    except (ValidationError, XidTargetMissingError) as exc:
        return error_envelope("prereg_commit", "commit_refused", str(exc))
    return ok_envelope("prereg_commit", result=written)


# ---------------------------------------------------------------------------
# 12. record_verdict -- real M9 API (trialerror.verify.verdicts.record_verdict);
#     see module TRIALERROR-DEV-NOTE ("SUPERSEDED by M9, as anticipated"). Writes
#     the generic Section 4.1 `verdict` table row; XID columns
#     (issued_by_launch -> platform.launch, prereg_id -> ops.prereg) are
#     validated by trialerror.stores.insert itself, reached via the M9 API.
# ---------------------------------------------------------------------------

#: Kept for this module's own ``inputSchema`` enum listings below (unchanged
#: from the pre-M9 adapter) -- the authoritative membership check itself now
#: lives in ``trialerror.verify.verdicts.{SUBJECT_KINDS,PROCEDURES}``, which these
#: mirror verbatim (same DDL CHECK constraint, design Section 4.1).
_VERDICT_SUBJECT_KINDS = frozenset({"hypothesis", "claim", "citation", "artifact"})
_VERDICT_PROCEDURES = frozenset({"citecheck", "contracrow", "gate", "reproduction", "custom"})


def _tool_record_verdict(args: Mapping[str, Any], *, store: Store) -> dict[str, Any]:
    try:
        written = verify_record_verdict(
            store,
            subject_kind=args["subject_kind"],
            subject_id=args["subject_id"],
            procedure=args["procedure"],
            procedure_version=args["procedure_version"],
            label=args["label"],
            evidence=args.get("evidence"),
            prereg_id=args.get("prereg_id"),
            prereg_compliant=args.get("prereg_compliant"),
            reproduction_ref=args.get("reproduction_ref"),
            issued_by_launch=args["issued_by_launch"],
        )
    except InvalidSubjectKindError as exc:
        return error_envelope("record_verdict", "invalid_subject_kind", str(exc))
    except InvalidProcedureError as exc:
        return error_envelope("record_verdict", "invalid_procedure", str(exc))
    except (ValidationError, XidTargetMissingError) as exc:
        return error_envelope("record_verdict", "record_refused", str(exc))
    return ok_envelope("record_verdict", result=written)


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
    """Build the exact 12-tool registry (design Section 5.1), each bound to
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
        "budget_status": w(
            "budget_status",
            "Pools, headroom, multiplier, DEFER advisories (tool #2, wraps "
            "trialerror.budget.pools.budget_status). account_id defaults to the open session's account.",
            {
                "type": "object",
                "properties": {
                    "account_id": {"type": "string"},
                    "model_class": {"type": "string", "enum": ["top", "mid", "small"]},
                },
            },
            _tool_budget_status,
        ),
        "book_launch": w(
            "book_launch",
            "Create a PROVISIONAL booking -> launch_id token (refuses over-cap); tool #3, wraps "
            "trialerror.budget.pools.book_launch. session_id defaults to the program's one open session; "
            "account_id is never accepted -- it is derived from that session.",
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
                    "override_ruling_id": {"type": "string"},
                },
                "required": ["program_id", "agent_kind", "model_class", "model", "purpose", "est_tokens"],
            },
            _tool_book_launch,
        ),
        "reconcile_launch": w(
            "reconcile_launch",
            "Settle actuals by launch_id (tool #4, wraps trialerror.budget.pools.reconcile_launch).",
            {
                "type": "object",
                "properties": {
                    "launch_id": {"type": "string"},
                    "actual_tokens": {"type": "integer"},
                    "reconcile_source": {"type": "string", "enum": ["transcript", "estimate", "manual"]},
                },
                "required": ["launch_id", "actual_tokens"],
            },
            _tool_reconcile_launch,
        ),
        "append_event": w(
            "append_event",
            "Type-keyed event append, auto-timestamped and secret-redacted (tool #5, wraps "
            "trialerror.events.api.append_event). Pass the caller's own launch_id/session_id through as-is "
            "-- this tool has no author field to spoof.",
            {
                "type": "object",
                "properties": {
                    "event_type": {"type": "string"},
                    "payload": {},
                    "session_id": {"type": "string"},
                    "launch_id": {"type": "string"},
                    "workpackage": {"type": "string"},
                },
                "required": ["event_type", "payload"],
            },
            _tool_append_event,
        ),
        "post_feed": w(
            "post_feed",
            "Post FULL TEXT into a feed thread (tool #6, wraps trialerror.events.api.post_feed, C-0047). "
            "author is DERIVED server-side from launch_id (or the open session if launch_id is omitted) "
            "-- this tool never accepts an author string.",
            {
                "type": "object",
                "properties": {
                    "thread_id": {"type": "string"},
                    "body": {"type": "string"},
                    "launch_id": {"type": "string", "description": "omit to post as the orchestrator"},
                    "session_id": {"type": "string", "description": "only used to resolve orchestrator authorship"},
                    "in_reply_to": {"type": "string"},
                },
                "required": ["thread_id", "body"],
            },
            _tool_post_feed,
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
        "law_lookup": w(
            "law_lookup",
            "Ruling search + (if 'pin' given) pin verify + foreign-entries-since-pin diff (tool #8, "
            "wraps trialerror.law.service.lookup_rulings/verify_pin/diff_foreign).",
            {
                "type": "object",
                "properties": {
                    "ruling_id": {"type": "string"},
                    "domain": {"type": "string"},
                    "status": {"type": "string", "enum": ["active", "superseded"]},
                    "query": {"type": "string"},
                    "pin": {"type": "string", "description": "'vNN@YYYY-MM-DD'; also runs verify+diff-foreign"},
                },
            },
            _tool_law_lookup,
        ),
        "register_artifact": w(
            "register_artifact",
            "Registry write; refuses ungated gated-types (tool #9, wraps "
            "trialerror.artifacts.registry.register_artifact). The artifact row itself must already exist "
            "(`trialerror artifact create`, CLI-only per the design's one-shot-op composition rule).",
            {
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string"},
                    "by_launch": {"type": "string"},
                    "supersedes": {"type": "string"},
                },
                "required": ["artifact_id", "by_launch"],
            },
            _tool_register_artifact,
        ),
        "gate_advance": w(
            "gate_advance",
            "State-machine transition with evidence; refuses illegal transitions (tool #10, wraps "
            "trialerror.artifacts.gates.advance_gate -- the generic entry point; submit/verdict/apply-union/"
            "verify-edit stay CLI-only, see module TRIALERROR-DEV-NOTE).",
            {
                "type": "object",
                "properties": {
                    "gate_id": {"type": "string"},
                    "to_state": {
                        "type": "string",
                        "enum": ["draft", "submitted", "gated", "union_applied", "registered", "failed"],
                    },
                    "by_launch": {"type": "string"},
                    "evidence": {},
                },
                "required": ["gate_id", "to_state", "by_launch"],
            },
            _tool_gate_advance,
        ),
        "prereg_commit": w(
            "prereg_commit",
            "Hash-commit a procedure+params blind; escrows the raw content under the platform tree, "
            "outside the program repo (tool #11; no landed M9 API yet -- thin adapter, see module "
            "TRIALERROR-DEV-NOTE). Reveal is not implemented by this tool.",
            {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "procedure": {"type": "string", "description": "the procedure text/spec, hashed verbatim"},
                    "params": {"type": "object", "description": "hashed as canonical (sorted-key) JSON"},
                },
                "required": ["title", "procedure"],
            },
            _tool_prereg_commit,
        ),
        "record_verdict": w(
            "record_verdict",
            "Verdict row w/ evidence anchors + prereg compliance check (tool #12; no landed M9 API yet "
            "-- thin adapter writing the Section 4.1 knowledge.verdict table directly, see module "
            "TRIALERROR-DEV-NOTE). Distinct from a gate's own PASS/FAIL field.",
            {
                "type": "object",
                "properties": {
                    "subject_kind": {"type": "string", "enum": sorted(_VERDICT_SUBJECT_KINDS)},
                    "subject_id": {"type": "string"},
                    "procedure": {"type": "string", "enum": sorted(_VERDICT_PROCEDURES)},
                    "procedure_version": {"type": "string"},
                    "label": {"type": "string"},
                    "evidence": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "[{anchor_id?, chunk_id?, stance?, note?}, ...]",
                    },
                    "prereg_id": {"type": "string"},
                    "prereg_compliant": {"type": "boolean"},
                    "reproduction_ref": {"type": "string"},
                    "issued_by_launch": {"type": "string"},
                },
                "required": ["subject_kind", "subject_id", "procedure", "procedure_version", "label", "issued_by_launch"],
            },
            _tool_record_verdict,
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
