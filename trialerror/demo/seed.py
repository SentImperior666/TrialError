"""Build a fully-populated demo program: the mechanics behind ``trialerror demo seed``.

Why this exists
===============
A freshly scaffolded program is empty, and an empty program makes the
dashboard look broken rather than new. Every panel renders its zero state --
"no posts yet", "NO ROOM OPEN", "0 of 0 criteria discharged", "NEVER RUN" --
so a first-time operator cannot tell which surfaces are unimplemented, which
are waiting on data, and which are the actual product. There was no way to
see what the dashboard does without running a real research program for a
week first.

This module writes one, in a few seconds, telling a small coherent story
(see :mod:`trialerror.demo.content`) that touches every panel.

Real code paths, not fixture rows
=================================
The rule this module follows: **go through the same API the CLI goes
through.** Sessions are booted by :func:`boot_session`, launches booked by
:func:`book_launch`, documents ingested by :func:`add_document` and processed
by a real worker draining the real job queue, gates driven through the real
state machine. A demo that hand-wrote rows could easily produce states the
system cannot actually reach, which would make the dashboard a liar -- and
would quietly stop catching regressions the moment a real code path changed
underneath it.

There is exactly one deliberate exception, and it is called out at its call
site: :func:`_seed_criteria`. The ``criterion`` table has **no writer
anywhere in the codebase** -- only the schema definition and the dashboard's
two reads of it. Until a `trialerror course` group exists, a direct insert is
the only way to populate the course panel at all.

Idempotency
===========
Seeding is not idempotent and does not try to be: it refuses to run against a
program that already has an account, and points at ``--force`` (which
recreates the stores from scratch). Making it merge into an existing program
would mean reasoning about half-seeded states for no benefit -- a demo
program is disposable by construction.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from trialerror.artifacts.gates import open_gate, record_verdict, submit_gate
from trialerror.artifacts.registry import create_artifact, register_artifact
from trialerror.artifacts.template_seed import seed_builtin_templates
from trialerror.budget.pools import book_launch, create_pool, reconcile_launch
from trialerror.demo import content
from trialerror.events.api import create_thread, post_feed, post_inbox, record_hook_alive_once
from trialerror.dashboard.doctor_run import run_doctor_and_persist
from trialerror.ingest.extract import accept_candidate, list_pending, run_extract_document
from trialerror.ingest.pipeline import add_document, register_source
from trialerror.jobs.worker import make_worker_id, run_loop
from trialerror.memory.api import put_item
from trialerror.retrieve.wrap import UNTRUSTED_CLOSE, UNTRUSTED_OPEN
from trialerror.rooms.api import create_room, freeze_room, post_message, score_dp
from trialerror.sessions.lifecycle import boot_session, close_session
from trialerror.stores import insert
from trialerror.stores.store import Store, open_store
from trialerror.util.ids import new_id
from trialerror.util.timeutil import now, now_dt

__all__ = [
    "DemoSeedResult",
    "seed_demo_program",
    "SeedRefused",
    "default_platform_root",
    "DEMO_PLATFORM_DIRNAME",
]

#: The program's account label, and the id written to trialerror.toml.
DEMO_ACCOUNT_LABEL = "demo-operator"

#: The demo keeps its platform store INSIDE the program directory rather than
#: using the real one (``TRIALERROR_PLATFORM_ROOT`` or ``~/.trialerror``).
#:
#: This matters more than it looks. ``account`` and ``budget_pool`` live in
#: platform.db, which is shared by every program under one platform root -- so
#: a demo seeded against the default would add a "demo-operator" account and
#: two fake budget pools to the operator's real ledger, and ``--force`` would
#: be deleting their actual budget state. Scoping the demo's platform root
#: into the program makes the whole thing one throwaway directory: delete it
#: and nothing of yours is touched.
DEMO_PLATFORM_DIRNAME = ".platform"


def default_platform_root(program_root: Path) -> Path:
    return Path(program_root) / DEMO_PLATFORM_DIRNAME


def _is_demo_owned_platform_root(program_root: Path, platform_root: Path) -> bool:
    """True only for a platform root the demo itself created inside the
    program -- so ``--force`` can never delete a real one, however it was
    passed in."""
    try:
        return platform_root.resolve().parent == program_root.resolve() and (
            platform_root.name == DEMO_PLATFORM_DIRNAME
        )
    except OSError:  # pragma: no cover - unresolvable path
        return False

#: Booked launches are all reconciled except one, deliberately left
#: PROVISIONAL so the budget panel's dangling-booking list (and the CRIT
#: health chip that reads it) has something real to report.
_DANGLING_PURPOSE = "abandoned ideation sweep"


class SeedRefused(Exception):
    """Raised when the target program is not safe to seed into."""


def _arm_hooks(store: Store, session_id: str) -> None:
    """Record the ``hook_alive`` markers Claude Code's hooks would have left.

    Not decoration -- ``close_session`` refuses without them, and the refusals
    are worth understanding because they are the enforcement working:

    - no marker at all -> ``hooks_disabled`` ("hooks were disabled or never
      armed").
    - a ``session_start`` marker but no ``spawn_gate`` one, on a session that
      consumed a launch -> ``hooks_partial`` (FX-8): launches were spent while
      the PreToolUse gate was off, so those spawns may have run ungated.

    The demo books and reconciles launches, so it is exactly the case
    ``hooks_partial`` exists to catch. Recording the markers the hooks would
    have written is honest; passing an override ruling would have been faster
    and would have taught the reader that the check is a formality.
    """
    for hook_name in ("session_start", "spawn_gate", "post_task"):
        record_hook_alive_once(store, session_id=session_id, hook_name=hook_name)


@dataclass
class DemoSeedResult:
    program_root: Path
    program_id: str
    account_id: str
    open_session_id: str
    closed_session_id: str
    counts: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# budget
# ---------------------------------------------------------------------------
def _ago_ts(*, hours: float) -> str:
    """An ISO timestamp ``hours`` in the past, in the format the stores use."""
    return (now_dt() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{(now_dt() - timedelta(hours=hours)).microsecond // 1000:03d}Z"
    )


def _book(store: Store, *, session_id: str, agent_kind: str, purpose: str,
          model_class: str = "mid", model: str = "sonnet", est: int = 4000,
          program_id: str = "demo", now_ts: str | None = None) -> str:
    result = book_launch(
        store,
        session_id=session_id,
        program_id=program_id,
        agent_kind=agent_kind,
        model_class=model_class,
        model=model,
        purpose=purpose,
        est_tokens=est,
        now_ts=now_ts,
    )
    if not result.launch_id:
        raise SeedRefused(f"book_launch refused ({result.code}): {result.message}")
    return result.launch_id


def _seed_pools(store: Store, account_id: str) -> None:
    """Two pools, both with real spend against them after reconciliation.

    A pool per model class is what turns the budget panel from a bare account
    row into meters -- with no ``budget_pool`` row the ribbon just says
    "0 TRACKED" and the panel has nothing to draw.
    """
    create_pool(store, account_id=account_id, model_class="top", period="weekly", cap_tokens=400_000)
    create_pool(store, account_id=account_id, model_class="mid", period="weekly", cap_tokens=1_200_000)


# ---------------------------------------------------------------------------
# corpus
# ---------------------------------------------------------------------------
def _seed_corpus(store: Store, program_root: Path, launch_id: str) -> dict[str, int]:
    raw_dir = program_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    documents = 0
    for filename, source_kwargs, body in content.DOCUMENTS:
        source = register_source(store, registered_by_launch=launch_id, **source_kwargs)
        raw_path = raw_dir / filename
        raw_path.write_text(body, encoding="utf-8")
        add_document(
            store,
            program_root=program_root,
            source_id=source["source_id"],
            raw_path=raw_path,
            created_by_launch=launch_id,
        )
        documents += 1

    # Sources with no document: these are the acquisition queue.
    for source_kwargs in content.PENDING_SOURCES:
        register_source(store, registered_by_launch=launch_id, **source_kwargs)

    return {
        "documents": documents,
        "sources": documents + len(content.PENDING_SOURCES),
    }


def _drain_jobs(store: Store) -> int:
    """Run the real worker over the real queue.

    Ingest enqueues normalize -> chunk -> embed -> index per document, and
    those stages are what populate the FTS index and the embedding table. Skip
    this and the corpus panel shows documents that are permanently 'pending',
    and ASK returns nothing -- which is a worse demo than no demo.
    """
    results = run_loop(store, worker_id=make_worker_id(), poll_interval_s=0.01, max_idle_polls=2)
    return sum(1 for r in results if r.get("status") == "complete")


# ---------------------------------------------------------------------------
# knowledge-graph extraction -- what fills the lexicon
# ---------------------------------------------------------------------------
def _extraction_judge(envelope: Any) -> dict[str, list]:
    """Stand in for the extraction model, deterministically.

    ``run_extract_chunk`` calls a judge rather than a model itself, which is
    what lets the demo replay a fixed extraction. The hard constraint is
    grounding: every claim's ``quote`` must be a verbatim substring of the
    chunk text, or :func:`_resolve_quote_anchor_draft` refuses it. So rather
    than inventing quotes, this scans the chunk for terms the demo corpus
    actually contains and lifts the real sentence each one appears in.

    That also means it degrades honestly -- a chunk mentioning none of the
    terms yields nothing, instead of a fabricated anchor.
    """
    # The envelope hands the judge UNTRUSTED-WRAPPED text (the same defensive
    # posture retrieval applies to any corpus text served downstream). The
    # wrapper is not part of the chunk, so a quote lifted with it attached is
    # correctly refused as ungrounded -- strip it before doing anything else.
    text = str(envelope.get("text") or "")
    if UNTRUSTED_OPEN in text and UNTRUSTED_CLOSE in text:
        text = text.split(UNTRUSTED_OPEN, 1)[1].rsplit(UNTRUSTED_CLOSE, 1)[0]

    entities: list[dict] = []
    claims: list[dict] = []

    for term, entity_type, kind in content.EXTRACTION_TERMS:
        idx = text.find(term)
        if idx < 0:
            continue
        entities.append({"name": term, "entity_type": entity_type, "confidence": 0.9})

        # Widen to the sentence containing the term, then verify the slice is
        # genuinely present before offering it as an anchor.
        start = text.rfind(".", 0, idx)
        start = 0 if start < 0 else start + 1
        end = text.find(".", idx)
        end = len(text) if end < 0 else end + 1
        quote = text[start:end].strip()
        if not quote or quote not in text:
            continue
        claims.append({"text": quote, "kind": kind, "quote": quote, "confidence": 0.8})

    return {"entities": entities, "relations": [], "claims": claims}


def _seed_extraction(store: Store, launch_id: str) -> dict[str, int]:
    """Run extraction over the ingested documents, then accept most of the
    candidates.

    Two panels depend on this. The lexicon reads ``entity`` rows, and its
    definition section inner-joins ``claim`` (kind='definition') to
    ``quote_anchor`` -- so an accepted definition claim with a real anchor is
    the only thing that lights it. And whatever is left PENDING becomes the
    kg_merge items in the DECIDE queue, which is the never-silent-auto-merge
    posture the design is built around: extraction candidates land as
    proposals, never straight into the graph.
    """
    doc_ids = [
        row["doc_id"]
        for row in store.knowledge.execute("SELECT doc_id FROM document ORDER BY doc_id").fetchall()
    ]

    accepted = 0
    # Extract and review ONE DOCUMENT AT A TIME, rather than extracting
    # everything and then accepting in bulk. That ordering is the whole point:
    # run_extract_chunk marks a candidate as a dedup of an existing entity
    # only when that entity is already CONFIRMED, and accepting is what
    # confirms it. Extract-all-then-accept-all leaves nothing confirmed during
    # extraction, so the demo corpus -- which mentions the same terms across
    # all three documents -- produced zero merge proposals and an empty
    # kg_merge queue, quietly hiding the never-silent-auto-merge behaviour
    # this project is built around.
    for index, doc_id in enumerate(doc_ids):
        run_extract_document(
            store, doc_id, judge=_extraction_judge, created_by_launch=launch_id
        )
        pending = list_pending(store, doc_id=doc_id, limit=500).get("candidates", [])
        # The last document's tail is left unreviewed on purpose, so the
        # review queue is not empty -- an always-empty queue would
        # misrepresent how extraction actually lands.
        if index == len(doc_ids) - 1:
            pending = pending[: max(0, len(pending) - content.EXTRACTION_LEFT_PENDING)]
        for record in pending:
            accept_candidate(store, record["record_id"], by_launch=launch_id)
            accepted += 1

    left = list_pending(store, limit=500).get("candidates", [])
    proposals = list_pending(store, limit=500).get("merge_proposals", [])
    return {
        "extract_accepted": accepted,
        "extract_pending": len(left),
        "merge_proposals": len(proposals),
    }


# ---------------------------------------------------------------------------
# artifacts + the critique gate
# ---------------------------------------------------------------------------
def _seed_artifacts_and_gate(store: Store, program_root: Path, launch_id: str,
                             critic_launch: str) -> dict[str, str]:
    artifacts_dir = program_root / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    # artifact.type_key is an FK to template, and a fresh scaffold has no
    # template rows -- so this has to happen before the first create_artifact
    # or the insert fails on the foreign key. It also gives the dossier
    # panel's type-filter chips something to filter by.
    seed_builtin_templates(store)

    def _write(name: str, text: str) -> tuple[str, str]:
        path = artifacts_dir / name
        path.write_text(text, encoding="utf-8")
        import hashlib

        return str(Path("artifacts") / name), hashlib.sha256(text.encode("utf-8")).hexdigest()

    methods_path, methods_sha = _write(
        "methods-note-v1.md",
        "# Methods note (demo fixture)\n\nHow the two retention fixtures were "
        "compared, and on what dimensions.\n",
    )
    methods_v1 = create_artifact(
        store, type_key="methods-note", title="Methods note (v1)",
        path=methods_path, sha256=methods_sha, by_launch=launch_id,
        purpose="Record how the two sources were compared.",
    )
    register_artifact(store, artifact_id=methods_v1["artifact_id"], by_launch=launch_id)

    # A superseding version, so the dossier's version chain has more than one
    # link to draw.
    methods_v2_path, methods_v2_sha = _write(
        "methods-note-v2.md",
        "# Methods note (demo fixture, v2)\n\nAdds the delayed post-test interval "
        "to the comparison dimensions.\n",
    )
    methods_v2 = create_artifact(
        store, type_key="methods-note", title="Methods note (v2)",
        path=methods_v2_path, sha256=methods_v2_sha, by_launch=launch_id,
        purpose="Adds the post-test interval dimension.",
    )
    register_artifact(
        store, artifact_id=methods_v2["artifact_id"], by_launch=launch_id,
        supersedes=methods_v1["artifact_id"],
    )

    draft_path, draft_sha = _write(
        "findings-draft.md",
        "# Findings draft (demo fixture)\n\nThe interleaving advantage is robust "
        "across populations.\n",
    )
    draft = create_artifact(
        store, type_key="paper-export", title="Findings draft",
        path=draft_path, sha256=draft_sha, by_launch=launch_id,
        purpose="First pass at the retention findings.",
    )

    # Drive the real gate state machine: open -> submit -> verdict. The
    # verdict carries a blocking, unverified edit, which is exactly the state
    # the DECIDE queue and the gates panel's pending_edits both key on.
    gate = open_gate(store, artifact_id=draft["artifact_id"])
    submit_gate(store, gate_id=gate["gate_id"], by_launch=launch_id)
    record_verdict(
        store,
        gate_id=gate["gate_id"],
        verdict="PASS_WITH_EDITS",
        critic_launch=critic_launch,
        edits=content.GATE_EDITS,
    )

    return {
        "draft_artifact_id": draft["artifact_id"],
        "methods_v1": methods_v1["artifact_id"],
        "methods_v2": methods_v2["artifact_id"],
        "gate_id": gate["gate_id"],
    }


# ---------------------------------------------------------------------------
# feed + inbox
# ---------------------------------------------------------------------------
def _seed_feed(store: Store, session_id: str, launches_by_kind: dict[str, str]) -> int:
    thread = create_thread(
        store, title=content.FEED_THREAD_TITLE, launch_id=launches_by_kind["orchestrator"]
    )
    for agent_kind, body in content.FEED_POSTS:
        post_feed(
            store,
            thread_id=thread["thread_id"],
            body=body,
            launch_id=launches_by_kind[agent_kind],
            session_id=session_id,
        )
    return len(content.FEED_POSTS)


def _seed_inbox(store: Store) -> None:
    """Left unread on purpose -- read_ts stays NULL, which is what the boot
    bundle's unread counter and the feed panel's directives list both read."""
    post_inbox(store, body=content.INBOX_ITEM)


# ---------------------------------------------------------------------------
# rooms
# ---------------------------------------------------------------------------
def _seed_rooms(store: Store, launches_by_kind: dict[str, str]) -> dict[str, Any]:
    moderator = launches_by_kind["orchestrator"]
    participants = ["lens:measurement", "lens:population", "critic:methods"]

    room = create_room(
        store,
        topic=content.ROOM_TOPIC,
        discussion_points=content.ROOM_DISCUSSION_POINTS,
        participants=participants,
        rounds_per_dp=3,
        by_launch=moderator,
        enforce_participant_range=False,
    )
    dps = room["dps"] if isinstance(room.get("dps"), list) else json.loads(room["dps"])["discussion_points"]
    dp_ids = [dp["dp_id"] for dp in dps]

    for dp_index, agent_kind, body in content.ROOM_TURNS:
        post_message(
            store, room_id=room["room_id"], launch_id=launches_by_kind[agent_kind],
            dp_id=dp_ids[dp_index], body=body,
        )

    # score_dp takes a judge callable rather than making a model call itself,
    # which is what lets the demo replay a fixed, believable agreement
    # trajectory. Each call emits a room_dp_scored event, and the dashboard
    # builds the trajectory chart from those events -- so the number of calls
    # here is the number of points on the line.
    for dp_index, series in enumerate(content.ROOM_AGREEMENT_SERIES):
        for pct in series:
            score_dp(
                store, room_id=room["room_id"], dp_id=dp_ids[dp_index],
                judge=lambda _envelope, _pct=pct: {"agreement_pct": _pct},
                by_launch=moderator,
            )

    frozen = create_room(
        store,
        topic=content.FROZEN_ROOM_TOPIC,
        discussion_points=content.FROZEN_ROOM_DISCUSSION_POINTS,
        participants=participants,
        by_launch=moderator,
        enforce_participant_range=False,
    )
    frozen_dps = (
        frozen["dps"] if isinstance(frozen.get("dps"), list)
        else json.loads(frozen["dps"])["discussion_points"]
    )
    for pct in (44.0, 41.0):
        score_dp(
            store, room_id=frozen["room_id"], dp_id=frozen_dps[0]["dp_id"],
            judge=lambda _envelope, _pct=pct: {"agreement_pct": _pct},
            by_launch=moderator,
        )
    freeze_room(
        store, room_id=frozen["room_id"], by_launch=moderator,
        reason=content.FROZEN_ROOM_REASON,
    )

    return {"open_room": room["room_id"], "frozen_room": frozen["room_id"]}


# ---------------------------------------------------------------------------
# course criteria -- the one direct-insert path
# ---------------------------------------------------------------------------
def _seed_criteria(store: Store, discharging_artifact: str | None) -> int:
    """Insert ``criterion`` rows directly.

    THIS IS THE ONE PLACE THIS MODULE BYPASSES A PUBLIC API, and it is not a
    shortcut: ``criterion`` has no writer anywhere in the codebase. The table
    is created by the ops schema and read twice by the dashboard's course
    panel, and nothing in between ever inserts a row. Without this the course
    panel is permanently "0 of 0 criteria discharged" -- it cannot be
    populated through any supported path.

    When a `trialerror course` CLI group lands, this function should be the
    first caller deleted.
    """
    for label, phase, state in content.CRITERIA:
        insert(
            store,
            "criterion",
            {
                "criterion_id": new_id("CRIT"),
                "label": label,
                "phase": phase,
                "state": state,
                "discharged_by_artifact": (
                    discharging_artifact if state == "discharged" else None
                ),
            },
        )
    return len(content.CRITERIA)


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------
def _seed_memory(store: Store, account_id: str) -> int:
    for key, tier, kind, body in content.MEMORY_ITEMS:
        put_item(
            store, key=key, tier=tier, kind=kind, body=body,
            account_id=account_id, l0_abstract=content.L0_ABSTRACTS.get(key),
        )
    return len(content.MEMORY_ITEMS)


# ---------------------------------------------------------------------------
# the orchestration
# ---------------------------------------------------------------------------
def seed_demo_program(
    program_root: Path,
    *,
    platform_root: Path | None = None,
    program_id: str = "demo",
    force: bool = False,
) -> DemoSeedResult:
    """Populate ``program_root`` with the demo program.

    ``program_root`` must already be scaffolded (``trialerror program init``);
    the CLI wrapper does that step. Refuses a program that already has an
    account unless ``force``, which wipes the stores directory first.
    """
    program_root = Path(program_root).resolve()
    stores_dir = program_root / "stores"
    platform_root = Path(platform_root) if platform_root else default_platform_root(program_root)

    if force:
        if stores_dir.exists():
            shutil.rmtree(stores_dir)
        # The demo's platform root is inside the program by default, so
        # --force has to clear it too -- otherwise the account and budget
        # pools from the previous seed survive and the program comes back
        # with two operators.
        if platform_root.exists() and _is_demo_owned_platform_root(program_root, platform_root):
            shutil.rmtree(platform_root)

    store = open_store(program_root, platform_root=platform_root)
    try:
        # Deliberately checks `session` (ops.db, per-program) rather than
        # `account` (platform.db, shared across every program under one
        # platform root). An account row says "this operator exists
        # somewhere"; a session row says "this program has been worked in",
        # which is the thing seeding must not trample.
        existing = store.ops.execute("SELECT COUNT(*) AS n FROM session").fetchone()
        if existing and existing["n"] and not force:
            raise SeedRefused(
                f"{program_root} already has {existing['n']} session(s) -- refusing to seed "
                "into a program that is already in use. Pass --force to recreate its stores "
                "from scratch, or point --dir at a new directory."
            )

        notes: list[str] = []
        counts: dict[str, int] = {}

        # --- session 1: booted, worked in, then closed. Gives the session
        # panel history rather than a single row, and its course_check is the
        # only source the course panel's drift log has.
        first = boot_session(store, create_account_label=DEMO_ACCOUNT_LABEL)
        account_id = first.account_id
        closed_session_id = first.session_id

        _arm_hooks(store, closed_session_id)
        _seed_pools(store, account_id)

        setup_launch = _book(
            store, session_id=closed_session_id, agent_kind="orchestrator",
            purpose="corpus acquisition", program_id=program_id,
        )
        counts.update(_seed_corpus(store, program_root, setup_launch))
        counts["jobs_completed"] = _drain_jobs(store)
        counts.update(_seed_extraction(store, setup_launch))
        reconcile_launch(store, launch_id=setup_launch, actual_tokens=3820)

        counts["memory_items"] = _seed_memory(store, account_id)

        closed = close_session(
            store, session_id=closed_session_id,
            course_check=content.COURSE_CHECK_CLOSED,
            notes="Corpus acquisition session.",
        )
        if not closed.ok:
            # Checked rather than assumed: close_session REFUSES (it does not
            # raise) when the session is not close-ready. Swallowing that left
            # the demo with one still-open session masquerading as two, and a
            # course panel with no drift log, with nothing on stderr to say so.
            raise SeedRefused(
                f"could not close the demo's first session ({closed.code}): {closed.message}"
            )

        # --- session 2: the CURRENT session, left OPEN.
        #
        # This ordering is load-bearing. `since_you_left` (which powers the
        # dashboard's whole home lane) defaults its `since` cursor to the most
        # recent session.closed_ts. Seed everything and THEN close a session
        # and every row predates the cursor, so home renders "nothing since"
        # on a program full of data. Leaving session 2 open keeps the cursor at
        # session 1's close, so all the work below lands after it.
        second = boot_session(store, account_id=account_id)
        open_session_id = second.session_id

        launches_by_kind = {
            "orchestrator": _book(
                store, session_id=open_session_id, agent_kind="orchestrator",
                purpose="synthesis", model_class="top", model="claude-opus", est=9000,
                program_id=program_id,
            ),
            "lens": _book(
                store, session_id=open_session_id, agent_kind="lens",
                purpose="ideation", program_id=program_id,
            ),
            "critic": _book(
                store, session_id=open_session_id, agent_kind="critic",
                purpose="critique", program_id=program_id,
            ),
        }
        for launch_id, spent in (
            (launches_by_kind["orchestrator"], 8450),
            (launches_by_kind["lens"], 3110),
            (launches_by_kind["critic"], 2740),
        ):
            reconcile_launch(store, launch_id=launch_id, actual_tokens=spent)

        # Left PROVISIONAL on purpose -- the budget panel's dangling-booking
        # list and the CRIT health chip both read exactly this state.
        #
        # Backdated, because "dangling" is not just PROVISIONAL: the check is
        # `booked_ts + booking_ttl_s < now`. A booking made a second ago has
        # 59 minutes of TTL left and is perfectly healthy, so seeding it at
        # the current time produced a launch that LOOKED abandoned in the
        # ledger while every panel correctly reported zero dangling bookings.
        _book(
            store, session_id=open_session_id, agent_kind="lens",
            purpose=_DANGLING_PURPOSE, program_id=program_id,
            now_ts=_ago_ts(hours=3),
        )
        notes.append(
            "One launch is left PROVISIONAL on purpose, so the budget panel has a "
            "dangling booking to report."
        )

        artifact_ids = _seed_artifacts_and_gate(
            store, program_root, launches_by_kind["orchestrator"], launches_by_kind["critic"]
        )
        counts["feed_posts"] = _seed_feed(store, open_session_id, launches_by_kind)
        _seed_inbox(store)
        room_ids = _seed_rooms(store, launches_by_kind)
        counts["criteria"] = _seed_criteria(store, artifact_ids["methods_v2"])

        _arm_hooks(store, open_session_id)

        # The doctor panel reads a sidecar file, not the DB, and `trialerror
        # doctor` does not write it -- only the dashboard's own run endpoint
        # and `dashboard export --run-doctor` do. Without this the
        # DIAGNOSTICS ribbon reads "NEVER RUN" on a fully-seeded program.
        try:
            run_doctor_and_persist(
                repo_root=Path(__file__).resolve().parents[2],
                program_root=program_root,
                platform_root=platform_root,
            )
        except Exception as exc:  # noqa: BLE001 - a failed sweep must not fail the seed
            notes.append(f"doctor sweep did not complete: {exc}")

        counts["rooms"] = 2
        counts["artifacts"] = 3
        counts["gates"] = 1

        notes.append(
            "The current session is left OPEN so the dashboard's home lane has a "
            "'since you left' window to report against."
        )

        return DemoSeedResult(
            program_root=program_root,
            program_id=program_id,
            account_id=account_id,
            open_session_id=open_session_id,
            closed_session_id=closed_session_id,
            counts=counts,
            notes=notes,
        )
    finally:
        store.close()
