# TrialError Dashboard V2 — backend API contract

<!-- Stage 1 of 2. This document is the ONLY thing the stage-2 frontend
     builder should need to read to wire the dashboard v2 frontend templates
     against real data. Every payload shape below is a REAL captured JSON
     example (pretty-printed, ids/timestamps real but from a throwaway
     fixture store — trialerror/dashboard/data.py + tests/_store_fixtures.py +
     tests/test_dashboard_data_v2.py are the source of truth if this
     document and the code ever disagree; the code wins). -->
<!-- builder: build-v2dash-writes, launch LNCH-2026-08-29T184618-0513-31b1 -->
<!-- Stage 3: section 12 (WRITES) below is that build's addition — every
     other section is read-only and unchanged. trialerror/dashboard/writes.py +
     tests/test_dashboard_writes.py + tests/test_dashboard_serve.py's
     write-guard/full-loop assertions are the source of truth if this
     section and the code ever disagree. -->

## 0. How to fetch anything

Two ways to get panel data, same as every pre-existing panel:

- `GET /dashboard/api/all` — every panel in one request: `{"meta": {...},
  "panels": {"<name>": {...}, ...}}`. What a freshly-loaded page fetches.
  Every new panel below always appears with its OWN DEFAULT SELECTION (e.g.
  `feed` picks the thread with the most recent post; `rooms` picks an open
  room over a converged one) — there is no way to ask `/all` for a specific
  thread/room/artifact; use the panel's own single-panel route with a query
  param for that.
- `GET /dashboard/api/<panel>[?param=value]` — one panel's JSON, unwrapped
  (no `{"meta":..., "panels":...}` envelope — just the panel object
  itself). Four of the seven new panels accept ONE optional query param to
  select what's active (table below); every other panel (including every
  panel that existed before this build) ignores the query string.

| Panel | Route | Optional query param | Selects |
|---|---|---|---|
| `feed` | `/dashboard/api/feed` | `thread_id` | which thread's post stream to return |
| `rooms` | `/dashboard/api/rooms` | `room_id` | which room's transcript/convergence to return |
| `determinations` | `/dashboard/api/determinations` | — | (always the whole queue) |
| `dossier` | `/dashboard/api/dossier` | `artifact_id` | which artifact's detail to return |
| `lexicon` | `/dashboard/api/lexicon` | — | (always the whole entity/claim set) |
| `course` | `/dashboard/api/course` | — | (always the whole criterion ladder) |
| `since_you_left` | `/dashboard/api/since_you_left` | `since` | ISO-8601 UTC timestamp; delta is everything strictly after it |
| `search` | `/dashboard/api/search` | `q`, `k`, `mode`, `source_ids`, `kind`, `license_tier`, `year` | see §8 |

An unknown panel name is a plain HTTP 404 (`self.send_error(404, ...)`),
same as before. A query param this route doesn't recognize is silently
ignored, never an error.

SSE (`GET /dashboard/events`) is unchanged by this build: `hello` once on
connect, `changed` whenever any watched store file's mtime moves (the new
`criterion`/`feed_post_translation` tables live inside `ops.db`, already a
watched file — no new SSE event type was needed or added), a heartbeat
comment every 15s otherwise. A client showing one of the seven new panels
should just re-fetch that panel's own route on `changed`, exactly like
every existing panel already does.

## 1. Absent/error-state conventions (read this before wiring any panel)

Every panel in this build follows the SAME "visible, not refused" contract
every pre-existing panel already uses — never a 500, never an unhandled
exception reaching the client:

| `status` value | Meaning | Which panels can return it |
|---|---|---|
| `"ok"` | Real data below (may still be an empty list/queue — empty is a real, valid state, not an error) | all |
| `"not_initialized"` | The store file this panel needs (`ops.db` for most; `knowledge.db` for `lexicon`/`search`) does not exist yet — a fresh/partially-initialized program | all |
| `"awaiting_migration"` | Only `course`: `ops.db` exists but hasn't been migrated to schema v4 yet (the `criterion` table doesn't exist) — see §7 | `course` only |
| `"invalid_mode"` | Only `search`: the `mode` query param isn't one of `auto`/`fts`/`vector`/`hybrid`/`graph`/`summary` | `search` only |

A panel with `status != "ok"` carries a `message` string explaining why —
render that message, don't try to read any other key on that response.

Every OTHER field described below is only present when `status == "ok"`.

## 2. Feed — `GET /dashboard/api/feed[?thread_id=THR-...]`

```json
{
  "status": "ok",
  "threads": [
    {
      "thread_id": "THR-01M178QK29NQT5WWMQBPFDH16D",
      "title": "test thread",
      "created_ts": "2026-08-29T17:22:59.785Z",
      "created_by_launch": "LNCH-01M178QK1SNYCKN55PTPS99PMX",
      "status": "active",
      "refs": null
    }
  ],
  "active_thread_id": "THR-01M178QK29NQT5WWMQBPFDH16D",
  "posts": [
    {
      "post_id": "POST-01M178QK2BB04BVB746XJXZ28C",
      "thread_id": "THR-01M178QK29NQT5WWMQBPFDH16D",
      "author": "launch:LNCH-01M178QK1SNYCKN55PTPS99PMX",
      "launch_id": "LNCH-01M178QK1SNYCKN55PTPS99PMX",
      "ts": "2026-08-29T17:22:59.787Z",
      "body": "test post body",
      "in_reply_to": null,
      "kind": "launch",
      "translation_state": "translated",
      "translation": {
        "translation_id": "XLAT-01M178QK2S946RDDWD4F8NSJA8",
        "body": "test translation body",
        "style_mode": "flavored",
        "translator_version": "1",
        "faithfulness_score": null,
        "created_ts": "2026-08-29T17:22:59.801Z",
        "gate_status": "pass",
        "gate_reasons": {"passed": true, "reasons": [], "score": null, "threshold": 0.8,
                          "judged": false, "style": {"style_mode": "flavored", "violations": [],
                                                     "hedges_lost": []}}
      }
    }
  ],
  "unread_directives": [
    {
      "item_id": "INBX-01M178QK2CD4MJ4VHH8Q8RCV30",
      "ts": "2026-08-29T17:22:59.788Z",
      "body": "test inbox item",
      "source": "user",
      "read_ts": null,
      "read_by_session": null
    }
  ],
  "translator_table_available": true,
  "translation_withheld_count": 0
}
```

Notes:

- `threads` — every thread, newest-created first (`trialerror.events.api.list_threads`, unchanged, limit 100). `refs`/`status` come from `thread`'s schema-v2 columns (`status` is `'active'`/`'archived'`; `refs` is free-form JSON or `null`).
- `active_thread_id` — the resolved selection: the `thread_id` you passed, or (default) the thread with the most recently-posted message, or (if no thread has any posts yet) the newest-created thread, or `null` if there are zero threads at all.
- `posts` — full-text posts in `active_thread_id`, oldest first (append order). `author` is server-derived and NEVER caller-settable (`trialerror.events.api._derive_author`) — always `"<agent_kind>:<launch_id>"` or `"orchestrator:<session_id>"`.
- `kind` — the text before the first `:` in `author`. Use this to badge a post (`orchestrator`, `lens`, `critic`, whatever `agent_kind` a launch actually used — this is real data, not a fixed enum, so render an unknown value neutrally rather than assuming a closed set).
- `translation_state` — **read this, not `translation != null`.** One of five values (`trialerror.dashboard.data._translation_slot`), matching the internal translator design notes' §4.4 right-column states (not in this export):
  - `"translated"` — a gated, PASSING translation. `translation.body` is the plain-English text.
  - `"ungated"` — a translation stored with no gate verdict (a row written before schema v6, or one hand-inserted outside `trialerror.feed_translate`). Served, but render the "not gated" note: it was never checked.
  - `"withheld"` — the faithfulness gate FAILED it. **`translation.body` is `null` — the withheld text is not in the payload at all**, deliberately (a body the UI must not render has no business crossing the wire). `translation.gate_reasons.reasons` carries the human-readable failure list; show that plus the original.
  - `"pending"` — a `feed_translate` job for this post is queued/claimed/running on the ledger. Render "translation pending".
  - `"absent"` — nothing has been asked for. Render the `TRANSLATE ▾` affordance (which posts `write/feed-translate`, §12.10).
- `translation` — `null` for `pending`/`absent`; otherwise the one `status='current'` row for that post, with `gate_status` (`pass`/`fail`/`ungated`), `gate_reasons` (a parsed JSON object, or `null`), and `faithfulness_score` (`null` unless the optional judged tier ran — the always-on deterministic tier produces no score).
- `unread_directives` — **NOT scoped to `active_thread_id`.** `inbox_item` (the operator directive channel) carries no `thread_id` column in the real schema — it is a program-wide channel. Render it as its own "operator inbox" surface, not inline in the thread's post stream (the `Feed.dc.html` mockup shows an inline "OPERATOR ... DIRECTIVE" card; that shape isn't backed by real per-thread data — build the directive UI as a separate list instead). Reading this list is a plain `SELECT ... WHERE read_ts IS NULL` — it does **not** mark anything read (`mark_read=False` is always passed).
- `translator_table_available` — `true`/`false`. Grey out or hide the `TRANSLATE ▾` affordance entirely on a program whose `ops.db` predates schema v4.
- `translation_withheld_count` — how many posts in this thread the gate withheld. Surface it near the panel chrome: a withheld translation is invisible by design, so without this number a systematically broken translator looks exactly like one nobody ran. `trialerror doctor` reports the program-wide figure as `feed_translation_failures`.

### 2.1 Threading (lane C item B)

Added by lane C, step C8, on the operator's walkthrough complaint: *"messages
are not structured by their relationships"*. `feed_post.in_reply_to` was on
every row and in no payload. **No migration** — the column, its self-FK and
`post_feed`'s `in_reply_to=` parameter all predate this; only the reading is
new.

Per post, beside the fields above:

```json
{
  "reply_to": "POST-01M178QK2BB04BVB746XJXZ28C",
  "reply_to_missing": false,
  "depth": 1,
  "root_post_id": "POST-01M178QK2BB04BVB746XJXZ28C",
  "reply_count": 2
}
```

Per panel:

```json
{ "order_threaded": ["POST-...root", "POST-...reply", "POST-...deeper", "POST-...root2"] }
```

and, on every entry of `threads[]`, `"thread_reply_count": 3`.

- `posts` is **unchanged**: still arrival order (`ts ASC, rowid ASC`), still the append-only truth. Threading is a second reading of the same list, never a reshuffle of it — which is what makes the AS IT ARRIVED view a render of `posts` verbatim rather than a second server query.
- `order_threaded` — DFS pre-order: roots by `(ts, rowid)`, each post's children by `(ts, rowid)`. Every post in `posts` appears exactly once. `[]` for a thread with no posts. Render the threaded view by walking this list and indenting each card by its own `depth`; you never need to build the tree client-side.
- `reply_to` — the parent id, an alias of the raw `in_reply_to` column (which is still there). Read `reply_to`.
- `depth` — 0 for a root, +1 per parent **that is present in this thread**. A client that indents should cap the visual indent (the shipped renderer caps at 4 rails) while still reporting the true depth.
- `root_post_id` — the top of this post's chain; its own id for a root. This is the key a collapse control toggles on: hiding a root's subtree is `depth > 0 && collapsed[root_post_id]`.
- `reply_count` — **direct children only**, inside this thread. Not the subtree. A card's own head says how many posts answer *it*; a collapse control that hides a whole subtree must count the subtree itself (the shipped renderer derives that client-side from `root_post_id`, where the visible set is known).
- `reply_to_missing` — `true` when `reply_to` is set but that post is **not in this thread**. Two causes, reported identically because the client's answer is the same for both:
  - a **cross-thread parent** — `feed_post.in_reply_to` is a plain self-FK with no same-thread constraint, so this row is entirely legal;
  - a **cycle** — SQLite enforces that the parent EXISTS, never that the graph is acyclic, so a hand-written row or a restore that renumbered ids can close a loop. Every post in (or leading into) a cycle is cut loose and reported this way, which is what makes the traversal terminate by construction rather than by a depth cap.

  Such a post gets `depth: 0` and is its own `root_post_id`, and **it is in `order_threaded`**. Ruling L-C4 is explicit: render it at root level with the flag visible, **never drop it**. `reply_to` still carries the id it pointed at — the flag explains, it does not erase.
- `thread_reply_count` (on each `threads[]` entry) — posts in that thread with a non-null `in_reply_to`, from one `GROUP BY` over the whole table rather than a query per rail row. Counts the RAW column, so a cross-thread or cycle parent still counts here even though the active thread reports it as `reply_to_missing`: the honest reading of a thread nobody has opened is "posts written as answers".

**Client contract (`static/feed_render.js`, `window.TEFeed`).** The reading
order is a per-viewer browser preference in `localStorage`
(`trialerror.dashboard.feed.order`), **THREADED by default** (L-C4) — never
server state, and never a query parameter. `GET /dashboard/api/feed` has no
`order` parameter and will not get one: both orders are the same payload read
two ways.

A bundle exported before this build has `posts` and no `order_threaded`; the
renderer degrades to arrival order rather than to an empty page, and reads
`in_reply_to` when `reply_to` is absent. Do the same in any other client.

## 3. Rooms — `GET /dashboard/api/rooms[?room_id=ROOM-...]`

```json
{
  "status": "ok",
  "rooms": [
    {
      "room_id": "ROOM-01M178QK42TRWA4F79QM4RHV5N",
      "topic": "does IDEA-1 cover the family",
      "dps": "{\"discussion_points\": [{\"dp_id\": \"DP1\", \"prompt\": \"does it cover?\", \"idea_id\": null}], \"participants\": [\"p1\", \"p2\"], \"rounds_per_dp\": 2, \"convergence_bar_pct\": 90.0}",
      "state": "open",
      "created_ts": "2026-08-29T17:22:59.842Z",
      "deliverable_artifact_id": null,
      "participant_count": 2,
      "discussion_point_count": 1
    }
  ],
  "active_room_id": "ROOM-01M178QK42TRWA4F79QM4RHV5N",
  "active_room": { "...(same row shape as one entry in `rooms`)...": true },
  "freeze_reason": null,
  "turns": [
    {
      "room_id": "ROOM-01M178QK42TRWA4F79QM4RHV5N",
      "seq": 1,
      "author_launch": "LNCH-01M178QK1SNYCKN55PTPS99PMX",
      "dp_ref": "ROOM-01M178QK42TRWA4F79QM4RHV5N::DP1",
      "body": "round 1 position: partial coverage only",
      "ts": "2026-08-29T17:22:59.846Z"
    },
    { "seq": 2, "body": "round 2 position: agree with the extension clause reading", "...": "..." }
  ],
  "convergence": {
    "room_id": "ROOM-01M178QK42TRWA4F79QM4RHV5N",
    "convergence_bar_pct": 90.0,
    "all_scored": true,
    "all_converged": true,
    "per_dp": [
      {"dp_id": "DP1", "dp_ref": "ROOM-01M178QK42TRWA4F79QM4RHV5N::DP1", "agreement_pct": 93.0, "converged": true}
    ]
  },
  "convergence_bar_pct": 90.0,
  "dp_agreement_series": {
    "DP1": [
      {"ts": "2026-08-29T17:22:59.849Z", "agreement_pct": 62.0, "note": "still disagreement on the luck pool", "converged": false},
      {"ts": "2026-08-29T17:22:59.855Z", "agreement_pct": 93.0, "note": null, "converged": true}
    ]
  },
  "moderator_events": [
    {"event_id": "EVT-...", "ts": "2026-08-29T17:22:59.842Z", "type": "room_created", "launch_id": "LNCH-...", "payload": {"room_id": "...", "topic": "...", "dp_ids": ["DP1"], "participants": ["p1","p2"], "rounds_per_dp": 2}},
    {"event_id": "EVT-...", "ts": "...846Z", "type": "room_turn", "launch_id": "LNCH-...", "payload": {"room_id": "...", "dp_id": "DP1", "dp_ref": "...", "seq": 1}},
    {"event_id": "EVT-...", "ts": "...849Z", "type": "room_dp_scored", "launch_id": "LNCH-...", "payload": {"room_id": "...", "dp_id": "DP1", "dp_ref": "...", "agreement_pct": 62.0, "note": "...", "converged": false}}
  ],
  "detail_error": null
}
```

Notes — **this is the panel the V2 design leans on hardest, read carefully:**

- `rooms` — every room, most-recently-created first (`room.created_ts`; a pre-schema-v3 room has `created_ts: null` and sorts last). `participant_count`/`discussion_point_count` are parsed from `room.dps` JSON.
- `active_room_id` default selection: an **open** room over a **converged/frozen** one (first by recency), falling back to the most recent room of any state, or `null` if there are zero rooms.
- `dp_agreement_series` — **THIS is the trajectory the V2 Rooms board draws, not `convergence.per_dp`.** `room_score` (and therefore `convergence.per_dp`) only ever holds the LATEST agreement score per discussion point — it's an upsert (`trialerror.rooms.api.score_dp`). The full history of every scoring round is reconstructed from the append-only `room_dp_scored` event trail instead, keyed by `dp_id`, each entry `{ts, agreement_pct, note, converged}` in chronological order. **Draw the line chart from `dp_agreement_series[dp_id]`; use `convergence.per_dp[i].agreement_pct` only for the "current score" badge next to each DP in the ladder.**
- `moderator_events` — every lifecycle/turn/scoring event for the active room, oldest first, one of `room_created` / `room_turn` / `room_dp_scored` / `room_converged` / `room_frozen` / `room_deliverable_registered`. This is both "the moderator events" the brief asked for AND the raw source `dp_agreement_series` was built from — you don't need to derive the series yourself, it's already split out, but the full event log is here too for a timeline/ticker view.
- `freeze_reason` — only non-`null` when `active_room.state == "frozen"`.
- `detail_error` — **honesty escape hatch.** A room's `dps` JSON is supposed to always be `{"discussion_points": [...], "participants": [...], "rounds_per_dp": N, "convergence_bar_pct": 90.0}` (written by `trialerror.rooms.api.create_room`), but a row that reached the table some other way (a raw fixture insert, a future migration bug) can violate that shape. Rather than 500, this builder catches `TypeError`/`KeyError`/`ValueError` while computing `turns`/`convergence`/`dp_agreement_series`/`moderator_events` and reports the exception string here, leaving those four fields at their empty defaults (`[]`/`null`/`{}`/`[]`). **Render this as a small "couldn't read this room's discussion points" notice, not a blank panel** — `active_room` itself is still populated from the plain `room` row scan, which never needs to parse `dps`.

## 4. Determinations — `GET /dashboard/api/determinations`

One flat, unioned queue. No selection param — always the whole thing.

```json
{
  "status": "ok",
  "items": [
    {
      "kind": "gate_edit", "id": "CR-.../E1", "gate_id": "CR-...", "edit_id": "E1",
      "artifact_id": "ART-...", "artifact_title": "test artifact", "artifact_type": "note",
      "text": "fix the tally", "blocking": true,
      "raised_by_launch": "LNCH-...", "raised_ts": "2026-08-29T17:22:59.837Z",
      "consequence": "This is the last blocking edit on CR-.... Verifying it clears the way for union_applied, and then registration of ART-... ('test artifact')."
    },
    {
      "kind": "kg_merge", "id": "MRG-...", "canonical_entity": "ENT-...",
      "members": ["ENT-...", "ENT-..."], "reason": "test merge", "proposed_by_launch": "LNCH-...",
      "blocking": false,
      "consequence": "Accepting merges 2 entity row(s) into ENT-...; rejecting leaves every member entity as its own row, unchanged."
    },
    {
      "kind": "acquisition", "id": "SRC-...", "title": "wanted paper",
      "request_state": "wanted", "source_kind": "paper", "blocking": false,
      "consequence": "Transitioning this source unblocks: rejected, requested."
    },
    {
      "kind": "prereg_reveal", "id": "PREG-...", "title": "test prereg",
      "committed_ts": "2026-08-29T17:22:59.789Z", "blocking": false,
      "consequence": "Revealing unseals the committed procedure/params hash so the pre-registered result can be checked against them."
    },
    {
      "kind": "room_escalation", "id": "ROOM-...", "topic": "test room",
      "reason": "deadlocked on DP1", "blocking": true,
      "consequence": "This room stays frozen until an operator turn resolves it (freeze-and-escalate)."
    },
    {
      "kind": "memory_conflict", "id": "G1", "key": "some-rule", "version_count": 2,
      "blocking": false,
      "consequence": "Resolving keeps one version of 'some-rule' active and marks the other superseded."
    }
  ],
  "counts_by_kind": {"gate_edit": 1, "kg_merge": 1, "acquisition": 1, "prereg_reveal": 1, "room_escalation": 1, "memory_conflict": 1},
  "blocking_count": 2,
  "total": 6
}
```

Notes:

- **Six kinds, not four** — the brief's four (gate edits, KG merges, acquisitions, prereg/room escalations) plus `memory_conflict` (REDESIGN finding S26: "queue kind, not drawn" — surfaced here as data even though no artboard draws it). Every item has `kind`, `id` (unique per item, but its FORMAT differs by kind — don't parse it, just use it as a React/DOM key), `blocking` (bool), and `consequence` (a plain-English sentence naming what resolving THIS item unblocks — pure string derivation over gate/artifact/criterion linkage, never an LLM call).
- `gate_edit` — **one row per unverified BLOCKING edit**, not one row per gate (a gate with 3 blocking edits produces 3 items). `consequence` names either "N more blocking edits remain" or, on the last one, whether reproduction still blocks union_applied or registration is next.
- `kg_merge` — every `merge_proposal` row at `status='draft'` (`trialerror.ingest.extract.list_pending`). `members` is already parsed to a list of entity ids (not a JSON string).
- `acquisition` — every `source` row whose `request_state` is `wanted`/`requested`/`delivered`/`verifying` (terminal states `indexed`/`rejected`/`failed` are excluded — nothing to decide on those). `consequence` lists the legal next states from `trialerror.ingest.requests.TRANSITIONS`.
- `prereg_reveal` — every `prereg` row at `status='committed'` (awaiting the reveal action that unseals its escrowed procedure/params hash).
- `room_escalation` — every `room` at `state='frozen'`, with its freeze reason resolved from the `room_frozen` event trail.
- `memory_conflict` — every open (`status='needs_merge'`) memory-sync conflict group, from `trialerror.memory.merge.list_conflicts`. `key` is the memory item's key both sides disagree on; `version_count` is normally 2 (`::left`/`::right`).

## 5. Dossier — `GET /dashboard/api/dossier[?artifact_id=ART-...]`

```json
{
  "status": "ok",
  "registry": [ "...(trialerror.artifacts.registry.list_artifacts rows, newest first, limit 200, UNCHANGED shape)..." ],
  "type_filters": [{"type_key": "note", "title": "Note", "gated": 0}],
  "active_artifact_id": "ART-01M178QK23GV80HF6PS46F5N1R",
  "artifact": { "...(the full `artifact` row)...": true },
  "context_frame": null,
  "gate": null,
  "gate_history": [],
  "verdicts": [],
  "version_chain": [
    {"artifact_id": "ART-01M178QK23GV80HF6PS46F5N1R", "title": "test artifact", "status": "draft", "registered_ts": null, "supersedes": null}
  ],
  "lineage": {
    "produced_by_launch": {"launch_id": "LNCH-...", "agent_kind": "tester", "purpose": "fixture", "session_id": "SESS-..."},
    "in_session": "SESS-...",
    "supersedes": null,
    "superseded_by": [],
    "registers_records": 1,
    "discharges_criteria": [{"criterion_id": "G-01", "label": "test criterion", "phase": "test-phase"}],
    "note": "Assembled only from the launch ledger, gate history, artifact.supersedes and record/criterion links. knowledge.prov_edge has zero writers in this codebase, so no general consumed-source provenance graph is drawn here."
  }
}
```

Notes:

- `registry` — the rail: every artifact, unchanged shape from the pre-existing `trialerror.artifacts.registry.list_artifacts`.
- `type_filters` — every `template` row (`type_key`, `title`, `gated`) for the rail's type-filter chips. There is no `list_templates()` in the codebase; this is a plain query. `gated` is `0`/`1` (SQLite has no bool type), not a JSON boolean.
- `active_artifact_id` default: the most recently-inserted artifact (`registry[0]`), or `null` if the registry is empty.
- `gate` — `null` whenever `artifact.gate_id IS NULL` (an artifact that never had a gate opened — this is common and correct, not a bug; e.g. every ungated template type). When present, it's the full `gate` row PLUS `edits_parsed` (the `edits` JSON column, pre-parsed to a list — never parse `gate.edits` yourself).
- `gate_history` — every `gate_transition` row for that gate, oldest first. `[]` whenever `gate` is `null`.
- `verdicts` — every `knowledge.verdict` row with `subject_kind='artifact'` and `subject_id=active_artifact_id`, newest first. **Different verdict procedures (`citecheck`/`contracrow`/`gate`/`reproduction`/`custom`) write completely different `label` vocabularies** (`"PASS"` vs `"match"` vs a bare confidence number as a string) — render each row's `procedure` and `label` together, never assume one shared scale across rows.
- `version_chain` — every artifact reachable from `active_artifact_id` by walking `supersedes` in EITHER direction (older versions it supersedes, and any newer version that later superseded it), oldest-registered first. This is the only version-chain data the schema carries — there's no separate version-chain table.
- `context_frame` — **almost always `null` today.** `artifact.context_frame` (REDESIGN §5.3 item 9: goal / prior-state / what-changed / why-it-matters) is not a real column yet — this reads `artifact.attrs.context_frame` best-effort (only non-null if some future producer happens to stash one there under `attrs`). Don't build UI that assumes this is normally populated; treat it exactly like `Dossier.dc.html`'s own "WHERE THIS CAME FROM" block would need to — as an honest empty state until the real column ships.
- `lineage.note` — **always render this note wherever lineage is shown.** `knowledge.prov_edge` (the general consumed-source provenance graph) has zero writers anywhere in this codebase (confirmed again in this build) — lineage here is assembled ONLY from the launch ledger (`produced_by_launch`/`in_session`), `artifact.supersedes`/reverse-lookup, `record.artifact_id` (`registers_records`, a count — not a list, to keep the payload small; drill into `knowledge.record` separately if a list is ever needed), and the new `criterion.discharged_by_artifact` link (`discharges_criteria`). This is the exact set REDESIGN's own Dossier mockup (`Dossier.dc.html`'s amber "△" lineage-note strip) asks to be stated on the card, verbatim.

## 6. Lexicon — `GET /dashboard/api/lexicon`

No selection param — the honest v1 read is a flat entity/claim listing, not a per-term drill-down route (there's no term store to drill into yet — see `seam_note`).

```json
{
  "status": "ok",
  "entities": [
    {"entity_id": "ENT-...", "name": "Test Entity", "entity_type": "concept", "aliases": null, "summary": null, "resolution": "draft", "merge_group": null, "relation_count": 1}
  ],
  "definition_claims": [],
  "claim_kind_counts": {"finding": 1},
  "merge_proposals_draft": [
    {"prop_id": "MRG-...", "canonical_entity": "ENT-...", "members": "[\"ENT-...\", \"ENT-...\"]", "reason": "test merge", "status": "draft", "proposed_by_launch": "LNCH-...", "decided_by": null, "decided_ts": null}
  ],
  "contradiction_edges": [],
  "seam_note": "No dedicated term/term_sense/term_sense_evidence store exists yet (REDESIGN_V2_RATIONALE.md Section 5.3 item 7). Entities and definition-kind claims are read as a v1 proxy -- they give deduplication signal (entity.aliases, draft merge_proposal rows), not senses. contradiction_edges is always empty today: knowledge.prov_edge has zero writers anywhere in this codebase."
}
```

Notes:

- `entities` — every `entity` row, alphabetical by name, plus a computed `relation_count` (live relations touching it, either direction).
- `definition_claims` — every LIVE `claim` with `kind='definition'`, newest first, joined to its grounding anchor (`quote_text`/`page_number`/`doc_id`). **This is the closest thing to a "term definition" today** — there is no `term`/`term_sense` table (see `seam_note`), so a real term-split view (`Lexicon.dc.html`'s "SENSE A vs SENSE B" split) cannot be built from this data alone yet; render what exists (a flat list of quote-grounded definitions) rather than fabricating a two-sense layout.
- `merge_proposals_draft` — draft `merge_proposal` rows; `members` is the raw JSON STRING here (unlike the determinations panel's `kg_merge` items, which pre-parse it) — parse it client-side if needed.
- `contradiction_edges` — **always `[]` today, on every real program**, not just this fixture. It reads `knowledge.prov_edge WHERE role='contradicts'`, and that table has zero writers anywhere in the codebase. Do not render "0 conflicts" as if it were a measured finding — render it as an honest "not tracked yet" state, or simply omit the conflict-count chip entirely until a writer exists.
- `seam_note` — a ready-to-render string explaining the above; safe to show directly in a "this surface needs N new tables" callout (`Lexicon.dc.html`'s own amber panel already sketches exactly this).

## 7. Course — `GET /dashboard/api/course`

```json
{
  "status": "ok",
  "criteria": [
    {"criterion_id": "G-01", "label": "test criterion", "phase": "test-phase", "state": "discharged", "discharged_by_artifact": "ART-...", "discharged_by_artifact_title": "test artifact"},
    {"criterion_id": "G-05", "label": "hole viability", "phase": "ideation", "state": "open", "discharged_by_artifact": null, "discharged_by_artifact_title": null}
  ],
  "phases": [
    {"phase": "test-phase", "total": 1, "open": 0, "blocked": 0, "discharged": 1},
    {"phase": "ideation", "total": 1, "open": 1, "blocked": 0, "discharged": 0}
  ],
  "drift_log": [
    {"source": "session_close", "ts": null, "session_id": "SESS-...", "course_check": {"on_course": true, "note": "round-3 lens set traces to CH-001 section 4"}}
  ]
}
```

Notes — **this is the smallest of the seven seams, read the scope carefully:**

- This build adds exactly ONE new table, `criterion (criterion_id, label, phase, state, discharged_by_artifact)` — deliberately narrower than REDESIGN §5.3 item 6's full three-table wishlist (`charter_criterion`/`course_dimension`/`course_phase`). **There is no separate phase table or dimension table.** "Mission phases" (`phases` below) are DERIVED by grouping `criteria` on their own `phase` string — a free-form scoping column, like `launch.workpackage`, not a foreign key to anything.
- `status == "awaiting_migration"` — this program's `ops.db` predates schema v4 (no `criterion` table yet). This is expected and common right after this stage lands: only a write path (any CLI command that opens the store) applies the migration, and `trialerror dashboard` never migrates anything itself (read-only by design). Render this exactly like `not_initialized` — a plain "not ready yet" state, `message` explains why.
- `criteria` — every row, in insertion order (not alphabetical, not phase-grouped — that's what `phases` is for). `state` is one of `open`/`blocked`/`discharged`. `discharged_by_artifact_title` is resolved for convenience (`null` unless `discharged_by_artifact` is set AND that artifact still exists).
- `phases` — one entry per DISTINCT `phase` value, in the order that phase FIRST appears among `criteria` (not alphabetical — this preserves whatever narrative order criteria were seeded in, matching the "phase spine" reading order `Course.dc.html` draws left-to-right). `total`/`open`/`blocked`/`discharged` are exact counts, always summing to `total`.
- **No coverage/theory/validation percentage rollups.** `Course.dc.html`'s "COVERAGE 71/93 SYSTEMS" / "THEORY 8/13 HOLES CLOSED" / "VALIDATION 7/13 CRITERIA" dimension bars need census and hole-register tables this build does not add (out of the brief's "MINIMAL designed seam" scope) — do not fabricate those numbers from `phases`; `phases`' `total`/`discharged` counts are the only honestly-computable rollup that exists today, and they answer a DIFFERENT question (how many criteria per phase, not how much of the corpus/theory is covered).
- `drift_log` — the UNION of two sources, newest first: (1) `session.course_check` (the JSON blob a session close writes — CLAUDE.md's boot protocol: "a session cannot close without one"), tagged `"source": "session_close"`; (2) any `event` row with `type='course_check'`, tagged `"source": "event"`, present ONLY if some future producer starts emitting one (none does today — this build adds no event producer, only the read path, per the brief). Both shapes carry the SAME `course_check` field (whatever JSON was recorded — render it verbatim, "quoted from the session close, never editorialised" per `Course.dc.html`'s own copy) and `session_id` (`null` for a bare event with no session scoping).

## 8. Since you left — `GET /dashboard/api/since_you_left[?since=2026-01-01T00:00:00.000Z]`

```json
{
  "status": "ok",
  "since": "2026-01-01T00:00:00.000Z",
  "since_source": "given",
  "items": [
    {"kind": "room_dp_scored", "ts": "2026-08-29T17:22:59.855Z", "summary": "Room ROOM-... DP DP1 converged at 93.0%.", "ref": {"room_id": "ROOM-..."}},
    {"kind": "room_turn", "ts": "...", "summary": "Room ROOM-...: room_turn", "ref": {"room_id": "ROOM-..."}},
    {"kind": "feed_post", "ts": "...", "summary": "launch:LNCH-... posted in thread THR-...: \"test post body\"", "ref": {"post_id": "POST-...", "thread_id": "THR-..."}},
    {"kind": "gate_transition", "ts": "...", "summary": "Gate CR-... moved draft -> submitted (by LNCH-...).", "ref": {"gate_id": "CR-..."}},
    {"kind": "artifact_registered", "ts": "...", "summary": "ART-... (note) registered: some title", "ref": {"artifact_id": "ART-..."}},
    {"kind": "ingest_complete", "ts": "...", "summary": "Job JOB-... (embed) completed.", "ref": {"job_id": "JOB-..."}}
  ],
  "count": 8
}
```

Notes:

- `since` omitted -> **default is the last CLOSED session's `closed_ts`** (`since_source: "last_session_close"`); if no session has ever closed, falls back to 24 hours before now (`since_source: "24h_fallback"`). Passing an explicit `since` reports `since_source: "given"`.
- `items` — **newest first** (the brief: "ordered newest-first"), each a `{kind, ts, summary, ref}` tuple. `summary` is a plain factual ONE-LINE template sentence built straight from row data — **no LLM call anywhere in this builder**, by design (the brief's own constraint). Six kinds today: `feed_post` (new posts), `gate_transition` (gate state moves), `room_created`/`room_turn`/`room_dp_scored`/`room_converged`/`room_frozen`/`room_deliverable_registered` (room lifecycle/scoring events — `room_turn`'s summary is intentionally terse, "Room X: room_turn", since a turn's own body text belongs on the Rooms/Feed surfaces, not repeated here), `artifact_registered` (newly-registered artifacts), `ingest_complete` (jobs of kind `ocr`/`embed`/`index`/`extract`/`ingest_batch`/`normalize`/`chunk` that reached `state='complete'`).
- `ref` — a small object naming the id(s) needed to deep-link to the item's own surface (a `post_id`+`thread_id` for Feed, a `room_id` for Rooms, a `gate_id`/`artifact_id` for Dossier/Determinations, a `job_id` for Console's jobs table). Shape varies by `kind` — switch on `kind` before reading `ref`'s fields.
- There is deliberately **no `document`/ingest-doc-level completion kind** — `document` carries no timestamp column in the real schema, so "this document finished indexing" cannot be honestly dated; `ingest_complete` (job-level) is the closest honestly-computable proxy and is what's reported instead.

## 9. Search — `GET /dashboard/api/search?q=...&k=...&mode=...`

Wires the pre-existing, fully-built `trialerror.retrieve.engine.search` (design's own R1: "built, tested, never surfaced") over the live knowledge store. Read-only and capped: `k` is clamped server-side to **50 regardless of what's requested**; `q=""`/omitted returns a well-formed empty result (never an error).

Query params: `q` (required in spirit, optional in practice — blank is legal), `k` (int, default 12, hard max 50), `mode` (one of `auto`/`fts`/`vector`/`hybrid`/`graph`/`summary`, default `auto` — **interactive typing MUST use `auto` or `hybrid`, never bare `mode=vector`**, per the design doc's own §5.4 constraint: unfiltered `vector` mode can build an oversized `IN (...)` clause), `source_ids`/`kind`/`license_tier`/`year` (each a single comma-separated value, e.g. `&license_tier=open,academic_oa`).

Real FTS hit (query `"hello"`, `mode=fts`, `k=5`):

```json
{
  "ok": true,
  "query_id": "QRY-01M178QK5NVHN2AKQGAF5DMKSS",
  "tiers_used": ["fts"],
  "results": [
    {
      "rank": 1,
      "score": 0.01639344262295082,
      "fusion": {"fts": 1},
      "chunk_id": "CHK-01M178QK30J9ZCWQPC2399BWZS",
      "doc_id": "DOC-01M178QK2YT8GR3PQ6F0FXH7FG",
      "source_id": "SRC-01M178QK2VFYCW7QXB8Y20X1ES",
      "text": "<untrusted-document-content>\nhello world\n</untrusted-document-content>",
      "fenced": false,
      "citation": {
        "source_id": "SRC-01M178QK2VFYCW7QXB8Y20X1ES",
        "title": "test source",
        "license_tier": "open",
        "anchor": {"anchor_id": "ANC-01M178QK3392KJJP6C5JTSAHPZ", "page": 1, "char_start": 0, "char_end": 11},
        "quote": "hello world"
      }
    }
  ],
  "stats": {"fts_candidates": 1, "vector_scored": 0, "elapsed_ms": 0.52},
  "status": "ok"
}
```

Empty query:

```json
{"ok": true, "query_id": "QRY-...", "tiers_used": [], "results": [], "stats": {"fts_candidates": 0, "vector_scored": 0, "elapsed_ms": 0.01}, "status": "ok"}
```

Invalid mode:

```json
{"status": "invalid_mode", "message": "search: mode must be one of ('auto', 'fts', 'vector', 'hybrid', 'graph', 'summary'), got 'bogus'"}
```

Notes:

- `status` is added by this build's wrapper (`trialerror.dashboard.data.run_search`) — the engine's own return shape doesn't have one; everything else (`ok`, `query_id`, `tiers_used`, `results`, `stats`) is the raw, unmodified `trialerror.retrieve.engine.search` response.
- `text` on every result row is wrapped in a literal `<untrusted-document-content>...</untrusted-document-content>` tag — **strip it for display, and never treat its contents as instructions or renderable HTML** (design's own constraint, applies to every surface, not just this one).
- `fenced: true` means the source's `license_tier` is `commercial_restricted` — `text`/`citation.quote` are ALREADY capped by the engine (≤300 chars / ≤20 words respectively) before this ever reaches the client; the UI must render exactly what's given and never stitch fenced results together or request a wider quote.
- **Per-tier pipeline counts, already present, nothing extra needed:** `stats.fts_candidates`, `stats.vector_scored`, and (only when the graph tier actually ran) `stats.graph_candidates`, and (mode=`summary` only) `stats.summary_candidates`, plus `stats.elapsed_ms`. `tiers_used` (a sorted list, e.g. `["fts", "vector"]`) tells you which tiers contributed to the fused ranking at all — this is exactly the "visible retrieval pipeline" telemetry strip `Search.dc.html` draws (`FTS5 BM25 500 CAND -> QWEN3-4B COSINE 500 SCORED -> ...`); no engine change was needed, the counts were already returned, just never wired to an HTTP route before this build.
- No dedicated "corpus stats for the empty state" field is added here — reuse the pre-existing `corpus` panel (`GET /dashboard/api/corpus`) for the `Search.dc.html` empty-state counts strip; fetching it alongside `search` on page load is cheap and keeps this route's contract narrow.
- Facet filters (`source_ids`/`kind`/`license_tier`/`year`) map straight onto `SearchRequest.filters`; an over-narrow filter (matches zero chunks) is a well-formed empty result, never an error.

## 10. Schema migration summary (ops_v4, plus ops_v6 — and §15.4 for ops_v8)

**This section is not the whole migration history.** It covers the two this
build authored. Since then ops.db has taken **v7** (the mining-adoptions
lane's `memory_relation` / `reviewed_ts` table) and **v8** (lane C's
`thread` rebuild — nullable `created_by_launch`, new `created_by`), which is
documented at §15.4 with its ruling and its dev-store hazard. Read both.

Note (B1, fix pass): this migration was authored as "ops_v5" against this
lane's branch point, but master independently landed its own, unrelated
ops v5 first (FU-14's `ops_v5_meta_kv`, a small key/value side table).
Renumbered to v6 here so the two migrations merge as a visible conflict
rather than a silently-shadowed Python constant — see
`trialerror/stores/schema/ops.py`'s TRIALERROR-DEV-NOTE at `_V6`.

`trialerror/stores/schema/ops.py`'s `Migration(version=4, name="ops_v4_criterion_and_feed_post_translation", ...)` — purely additive, two new tables, zero column changes to any existing table:

```sql
CREATE TABLE criterion (
    criterion_id            TEXT PRIMARY KEY,
    label                   TEXT NOT NULL,
    phase                   TEXT NOT NULL,
    state                   TEXT NOT NULL CHECK (state IN ('open','blocked','discharged')),
    discharged_by_artifact  TEXT REFERENCES artifact(artifact_id)
);
CREATE INDEX idx_criterion_phase ON criterion(phase);
CREATE INDEX idx_criterion_state ON criterion(state);

CREATE TABLE feed_post_translation (
    translation_id            TEXT PRIMARY KEY,
    post_id                   TEXT NOT NULL REFERENCES feed_post(post_id),
    translator_version        TEXT NOT NULL,
    style_mode                TEXT NOT NULL CHECK (style_mode IN ('strict','flavored')),
    body                      TEXT NOT NULL,
    original_sha256           TEXT NOT NULL,
    faithfulness_score        REAL,
    faithfulness_verdict_id   TEXT,
    glossary_links            TEXT,
    status                    TEXT NOT NULL CHECK (status IN ('current','superseded')),
    supersedes                TEXT REFERENCES feed_post_translation(translation_id),
    created_by_launch         TEXT,
    created_ts                TEXT NOT NULL
);
CREATE INDEX idx_feed_post_translation_post ON feed_post_translation(post_id, translator_version, status);
```

**ops_v6 (lane-b-translator)** then adds the gate's verdict to that same
table — two additive columns, no rebuild:

```sql
ALTER TABLE feed_post_translation ADD COLUMN gate_status TEXT NOT NULL DEFAULT 'ungated'
    CHECK (gate_status IN ('pass','fail','ungated'));
ALTER TABLE feed_post_translation ADD COLUMN gate_reasons TEXT;
CREATE INDEX idx_feed_post_translation_gate ON feed_post_translation(gate_status);
```

Pre-v6 rows backfill to `'ungated'`, never to `'fail'`: a translation
stored before the guard existed was not CHECKED, and reporting it as
FAILED would both withhold it from the panel and count it against the
translator in doctor.

`feed_post_translation`'s shape is verbatim from the internal translator design notes §4.2 (not in this export). `created_by_launch` (→ `platform.launch`) and `faithfulness_verdict_id` (→ `knowledge.verdict`) are registered as cross-store XIDs in `trialerror/stores/xid.py`; `post_id` is a same-file FK (both tables live in `ops.db`), not an XID.

Both tables are picked up automatically the next time anything opens the store for writing (`trialerror.stores.store.open_store`, which every CLI command already calls); **`trialerror dashboard` itself never migrates anything — it is read-only by construction** (`trialerror/dashboard/store_ro.py`'s own module docstring). This is exactly why `course` has its own `awaiting_migration` status (§7) and why `feed`'s `translator_table_available` flag exists (§2): a dashboard pointed at a not-yet-migrated program must degrade visibly, not silently show stale/wrong data or crash.

## 11. ext-panel listing (unchanged)

Not touched by this build. `meta.ext_panels` (present on every `/dashboard/api/all` response and the SSE `hello` event) and the `GET /dashboard/api/ext` / `GET /dashboard/api/ext/<name>` routes are exactly as documented in `trialerror/dashboard/ext.py`'s own module docstring — C-0070's per-project extension-panel protocol. Nothing in this build changes that surface.

## 12. Writes (Stage 3: operator write actions)

Everything above this section is unchanged by Stage 3 and stays true. This
section is new: a small set of `POST` routes that let the operator take the
legitimate subset of actions the V2 design drew as buttons. Every write goes
through the SAME module function the equivalent `trialerror <group>` CLI verb
already calls (`trialerror.dashboard.writes` is a thin dispatch table, never raw
SQL — see that module's own docstring) — this section documents the HTTP
shape; the module docstring documents the design reasoning (authority model,
why `feed-post` never opens a new thread, etc).

### 12.0 The token guard

The server is loopback-only, but a malicious page open in the SAME browser
could still blind-POST to it, so every write additionally requires a
per-serve-process random token (`secrets.token_hex(20)`, generated once in
`trialerror.dashboard.serve.main` and never persisted) on the
`X-TrialError-Dashboard-Token` request header. The token is embedded into the
live-served page as a `<meta name="dashboard-write-token" content="...">`
tag (`GET /`/`GET /dashboard.html`, injected in flight by
`serve.py`'s `_serve_index` — the ONE static asset this build rewrites; every
other static file, `dashboard.css` included, is served byte-for-byte
unchanged). A `trialerror dashboard export` snapshot NEVER carries this tag —
`export.py` builds its HTML through a completely separate code path that
never touches `_serve_index` — so `writesEnabled()` is `false` on every
static snapshot and every write control stays disabled, by construction,
not by a convention that could drift (proven by
`tests/test_dashboard_export.py::
test_export_snapshot_has_no_write_token_and_every_write_button_disabled`).

A request missing the header, or carrying the wrong value, gets:

```json
{"ok": false, "status": "forbidden", "message": "missing or invalid X-TrialError-Dashboard-Token"}
```

with HTTP status `403`, before any store is ever opened.

### 12.1 Response envelope

Every write route (including `doctor/run`, §12.9) returns one shape,
`Content-Type: application/json`, almost always HTTP `200` — a clean
BUSINESS refusal (a bad state transition, a missing edit, an already-decided
merge proposal, …) is still a `200` with `"ok": false`, not an HTTP error;
the refusing module's own `str(exc)` is reported VERBATIM as `message`,
never a generic "failed" (design constraint):

```json
{"ok": true, "result": { "...": "the business-logic call's own return value, unmodified" }}
```

```json
{"ok": false, "status": "IllegalRoomTransitionError", "message": "the refusing module's own message, verbatim"}
```

`status` on a refusal is the raising exception's class name (`ValueError`
included) for `dispatch`-level refusals it's one of `unknown_action` /
`no_program_root` / `missing_fields` (§12.2). An HTTP-layer refusal (bad/
missing token, unknown route, malformed JSON body) is a REAL HTTP error
status (`403`/`404`/`400`) with a small JSON body of the same
`{"ok": false, ...}` shape where practical.

### 12.2 Client-side (pre-store) refusals

Two refusals never open a store connection at all:

- No program selected (`trialerror dashboard serve` with no `--program-root`):
  `{"ok": false, "status": "no_program_root", "message": "..."}`.
- A required field is missing/blank in the request body:
  `{"ok": false, "status": "missing_fields", "message": "missing required field(s) for '<action>': a, b, c"}`
  — every missing field is named, not just the first.

### 12.3 `POST /dashboard/api/write/verify-edit`

Wraps `trialerror.artifacts.gates.verify_edit` (the `trialerror gate verify-edit`
CLI's own business logic). NOT a gate-state transition — marks one
`edits[]` entry `applied=true, verified=true`. Refuses unless the gate is
currently `state='gated'`.

Body: `{"gate_id", "edit_id", "by_launch"}` required; `"verified_note"`
optional. Success `result` is the full, updated `gate` row (`edits` is the
JSON-string column, same shape the `gates`/`dossier`/`determinations`
panels already parse).

### 12.4 `POST /dashboard/api/write/merge-accept` / `merge-reject`

Wrap `trialerror.ingest.extract.accept` / `.reject`, called with the
`kg_merge` determination item's own `id` (a `PROP-...` merge-proposal id —
`determinations`' `_kg_merge_items` already reports it pre-parsed; do not
pass a raw `RCD-...` extraction-candidate id here, this route only exercises
the merge-proposal half of that dispatching function, matching what the
determinations panel actually draws).

Body: `{"prop_id", "by_launch"}` required. Success `result`:
`{"prop_id", "status": "confirmed"|"rejected", "canonical_entity"?, "members"}`.

### 12.5 `POST /dashboard/api/write/acquisition-delivered`

Wraps `trialerror.ingest.requests.transition(..., to_state="delivered")` — the
ONE request-queue transition this build wires (matching the determinations
panel's own `"ACQUISITIONS · ONLY YOU CAN DELIVER THESE"` framing: a human
physically/digitally delivering a requested source is the one step that is
genuinely the operator's job). `trialerror.ingest.requests.TRANSITIONS` itself
still enforces the legal-from-state rule — this only ever succeeds from
`request_state='requested'`; any other starting state refuses with
`InvalidRequestTransitionError`, verbatim. Every OTHER transition
(reject/archive/index/retry) stays CLI-only (`trialerror ingest request --to
<state>`) — not wired here, disabled in the UI with that note.

Body: `{"source_id"}` required; `"launch_id"`/`"note"` optional. Success
`result` is the full, updated `source` row.

### 12.6 `POST /dashboard/api/write/room-turn`

Wraps `trialerror.rooms.api.post_message`. **Authority model**: `trialerror.rooms.api`
has no separate "operator" identity and no participant-membership check
(module docstring TRIALERROR-DEV-NOTE item 1) — an operator posting through the
dashboard is, to this subsystem, simply another launch, exactly as
legitimate a participant as any agent, PROVIDED they name a real
`launch_id` that already exists in `platform.launch`. There is no
no-launch/orchestrator fallback here (unlike `feed-post`, §12.9) — the
dashboard has nothing to substitute, so `launch_id` is always required, the
same as the CLI's own `--launch-id`.

Body: `{"room_id", "launch_id", "dp_id", "body"}` all required. Refuses
(`ValueError`) if the room is not `state='open'`, or `dp_id` names no
discussion point in the room; refuses (`OwnershipConflictError`) under the
NEITHER-ownership invariant if `launch_id` authored the idea the discussion
point exists to vet. Success `result`:
`{"room_id", "seq", "author_launch", "dp_ref", "body", "ts"}`.

### 12.7 `POST /dashboard/api/write/room-score`

Wraps `trialerror.rooms.api.score_dp`, the same no-LLM `judge` pass-through the
CLI's `trialerror room score --agreement-pct` uses — the caller (here: the
operator, via the dashboard form) already produced the number.
`score_dp` has NO room-state restriction (unlike `room-turn`/`room-freeze`)
— it can be called on an open, converged, OR frozen room; this route does
not add one either.

Body: `{"room_id", "dp_id", "agreement_pct", "by_launch"}` required
(`agreement_pct` a number, 0–100 — an out-of-range or non-numeric value
refuses with `ValueError`); `"note"` optional. Success `result`:
`{"room_id", "dp_id", "agreement_pct", "frozen", "note", "converged"}`.

### 12.8 `POST /dashboard/api/write/room-freeze`

Wraps `trialerror.rooms.api.freeze_room` — origin-project's freeze-and-escalate path.
Refuses (`IllegalRoomTransitionError`) unless the room is currently
`state='open'`.

Body: `{"room_id", "by_launch", "reason"}` all required (`reason` is
required by the underlying function too — "a freeze with no stated reason
defeats the point of escalating to a human"). Success `result` is the full,
updated `room` row.

### 12.9 `POST /dashboard/api/write/feed-post`

Wraps `trialerror.events.api.post_feed` into an EXISTING thread only —
**opening a NEW thread is deliberately not offered**: `create_thread`
requires a real `launch_id` (`thread.created_by_launch NOT NULL`, a schema
constraint this lane has no license to relax), which an operator posting
through the dashboard has none of. This route always calls `post_feed` with
`launch_id=None`, so the post lands as `orchestrator:<the currently open
session>` — **authorship is server-derived and NEVER caller-settable**; any
`launch_id` present in the request body is silently ignored, matching
`trialerror.events.api._derive_author`'s own contract, which this route does not
and must not work around. Refuses (`ValidationError`) if no session is
currently open.

Body: `{"thread_id", "body"}` required; `"session_id"`/`"in_reply_to"`
optional. Success `result`: `{"post_id", "thread_id", "author", "ts"}` —
`author` always starts `"orchestrator:"`.

`in_reply_to` is REPLY IN THREAD's entire effect on the wire (§2.1, lane C
C8): pass the parent's `post_id` to land the new post under it, or `null` /
omit it for a plain thread post. It is stored verbatim; the reply structure
every reader sees is derived from it at read time, so nothing here needs to
know about `depth` or `order_threaded`. The value is not validated against the
target thread — a cross-thread parent is legal, and the feed panel reports it
as `reply_to_missing` rather than refusing the write.

### 12.10 `POST /dashboard/api/write/feed-translate`

Added by lane-b-translator. ENQUEUES a `feed_translate` job on the M2
ledger (`trialerror.jobs.ledger.enqueue`, `kind="custom"`,
`payload["handler"] = "feed_translate"`) and returns immediately — **it
never translates inline and never calls a model from the HTTP process.**
That is the design's own option C (internal translator design notes §4.1, not in
this export); its rejected option B was "book a
launch per VIEW", which this route exists to avoid.

Body: exactly ONE of `"post_id"` / `"thread_id"`; `"style_mode"`
(`flavored` default, or `strict`) optional. Giving both, or neither,
refuses cleanly. Success `result`: `{"job_id", "state", "kind", "target"}`.

`created_by_launch` is always `null`: a dashboard operator has no launch
identity (the same reason `feed-post` always passes `launch_id=None`), so
the resulting translation is stored under the orchestrator's no-launch
identity. A program configured with a budget-spending translator backend
(`[feed.translator] backend = "model"`) therefore REFUSES such a job at
the worker rather than running unbooked — book a launch and use
`trialerror feed translate --by-launch ...` for that case.

After a successful enqueue the affected post's `translation_state` reads
`"pending"` on the next `GET /dashboard/api/feed` until a worker lands a
row; re-fetch the panel rather than optimistically rendering anything.

### 12.11 `POST /dashboard/api/doctor/run`

Was `GET` before this build (`trialerror.dashboard.doctor_run.run_doctor_and_persist`
WRITES a sidecar state file — `<program_root>/.trialerror_dashboard/doctor_state.json`
— so it belongs under the same write guard as every action above). A bare
`GET` on this route now returns `405 Method Not Allowed` with an `Allow:
POST` header. No request body is read. Response shape: unchanged from
before — the doctor panel's own `{"status": "ok", "last_run": {...}}`
(§ the `doctor` panel; not itself one of the seven Stage-1/2 panels, but
present in every `/dashboard/api/all` response).

### 12.12 What stays disabled, and why

> **Four of the rows below are superseded by section 15 (lane C, C7).** SEND
> BACK on a gate edit, both buttons on a `prereg_reveal`, both on a
> `memory_conflict`, and "opening a NEW thread" are all wired now — the last
> of them by **ops v8**, which removed the `NOT NULL` this table cites as the
> reason. The rows are kept rather than deleted, so the reasoning that held
> until C7 stays readable; section 15 is the current contract. REJECT
> specifically was NOT wired (ruling L-C6) and is drawn nowhere.

Every button the V2 design drew that this build does NOT wire stays
disabled in the UI with a `title` naming the reason, per action kind:

| UI control | Reason |
|---|---|
| Determinations: `SEND BACK / REJECT` on a `gate_edit` item | `trialerror.artifacts.gates` has no reject/send-back callable for a blocking edit — only `verify_edit` exists. |
| Determinations: `OTHER TRANSITION` on an `acquisition` item | Only the `delivered` transition is wired (§12.5); every other `source.request_state` transition stays CLI-only. |
| Determinations: both buttons on a `prereg_reveal` item | `trialerror.verify.prereg` has a real `reveal` callable, but pre-registration reveal wasn't in this build's named write-action list. |
| Determinations: both buttons on a `room_escalation` item | Resolve from the Rooms panel instead (post a turn, or converge-check) — there is no direct "resolve escalation" callable; a frozen room is unfrozen only by a new turn/converge action, not a queue decision. |
| Determinations: both buttons on a `memory_conflict` item | `trialerror.memory.merge.resolve_conflict` exists and IS a legitimate callable, but memory-conflict resolution wasn't in this build's named write-action list — a real candidate for a future stage, not a missing capability. |
| Rooms: `EXPORT TRANSCRIPT` | `trialerror.rooms.api.export_room` writes to an arbitrary path on the SERVER's own filesystem, chosen by the caller — there is no safe way for a browser to pick a server-side output path; use `trialerror room export --id ... --out ...`. |
| Feed: opening a NEW thread | See §12.9 — `thread.created_by_launch NOT NULL`, and an operator post has no `launch_id` to satisfy it. |

Registration of artifacts stays orchestrator-only by law (C-0006) — no
register button was ever drawn as enabled-pending in the V1/V2 design, and
none is wired here.

### 12.13 Eventing (verified per action)

Every write action's underlying module already writes its own
audit trail; none of the routes above add a second one:

| Action | Event(s) written by the underlying module |
|---|---|
| `verify-edit` | None — `verify_edit` is deliberately NOT a state transition (module docstring: "writes NO `gate_transition` row"); the mutation itself (the `edits` JSON column) IS the durable record. |
| `merge-accept` / `merge-reject` | `merge_proposal_accepted` / `merge_proposal_rejected` (`trialerror.ingest.extract`, via `append_event`). |
| `acquisition-delivered` | `ingest_request_transition` (`trialerror.ingest.requests.transition`, a plain `event` insert). |
| `room-turn` | `room_turn` (`trialerror.rooms.api._emit_room_event`). |
| `room-score` | `room_dp_scored` (same). |
| `room-freeze` | `room_frozen` (same). |
| `feed-post` | None dedicated — `feed_post` itself IS the durable, queryable row (same posture as `verify-edit`: the mutation is its own record; nothing else in this codebase treats "a row was inserted" as needing a second event mirror). |
| `feed-translate` | None dedicated — the enqueued `job` row and its `job_event` trail (`trialerror.jobs.ledger.enqueue` writes an `enqueued` job event) ARE the record; the translation row it eventually produces carries its own `gate_status`/`gate_reasons` audit. |
| `doctor/run` | None — writes only its own sidecar state file (`trialerror.dashboard.doctor_run`), never the program's real stores. |

---

## 13. Console panel additions (lane C step C5 / bug-sweep batch K4)

Everything in this section is **additive**. No field named anywhere above
changed shape or meaning, and every existing per-field test kept passing
without an edit — which is the property that lets the Console be rewritten
without a flag day.

The rule these fields exist to satisfy: **every card is a pure function of
the `/dashboard/api/all` bundle.** A reading the page computes from rows the
bundle does not carry is a reading the static export cannot draw, so
anything the Console needs is a field, not a client convention.

### 13.1 `jobs` panel — decoded columns, subject, duration, offload

`recent_jobs[]` gains four things (`build_jobs_panel`, `data.py`):

| field | shape | notes |
|---|---|---|
| `payload` | object (was a JSON **string**) | `_decode_json_text`: unparseable text stays a string, so a column holding a plain note survives untouched. |
| `checkpoint` | object (was a JSON **string**) | same rule. The ingest checkpoint (`rows_ingested`, `current_member`, …) used to reach a table cell as one long JSON string — the single least readable thing on the page (console-3). |
| `subject` | string \| null | `_job_subject(payload)`: `handler` → `doc_id` → `source_id` → the basename of `zip_path`/`path`/`file` → `"N field(s)"`. `kind` alone says `custom` for every handler-dispatched job. |
| `duration_s` | number \| null | `settled_ts - created_ts`, **settled rows only**. A settled job has no live lease; how long it took is the reading that belongs in that column instead. |

Panel-level, new:

```
offload: {available: bool,
          counts: {pending, claimed, done, failed},
          awaiting: n,
          jobs: {<job_id>: {state: "pending"|"claimed"|"done"|"failed",
                            worker_id: str|null, heartbeat_ts: iso|null,
                            offload_attempts: int|null}}}
```

Read from two independent sources and unioned by job id:

- the queue directory `<program_root>/offload/{pending,claimed/<worker_id>,done,failed}`
  (`trialerror.offload.protocol`) — `available: false` when the directory is
  absent, which is every program that has never parked work;
- the ledger rows themselves: a job whose `last_error` starts with
  `awaiting DEV GPU` (the prefix `trialerror.offload.stage` raises with) is
  parked, and says so from the moment it is parked.

`awaiting` is the size of the union, so the count is right both before the
queue directory exists and after. `counts` is the directory alone.

### 13.2 `session` panel — decoded queue, and the timeline

`open_session.boot_bundle_stats.queue` is decoded (it was a JSON string;
M-CON-3). The SESSION card prints its length.

`open_session.timeline` is new, and is `null` when no session is open:

```
timeline: {
  window: {start_ts, end_ts|null},          // opened_ts -> closed_ts, or null = still open
  spans: [{id, kind: "launch"|"job"|"room", lane, label,
           start_ts, end_ts|null,
           status: "running"|"complete"|"retried"|"booked"|"failed"|"abandoned"|"frozen",
           ref: {launch_id|job_id|room_id: "..."}}],
  instants: [{ts, kind: "hook_alive"|"room_dp_scored"|"gate_transition",
              lane, label, ref}],
  truncated: {spans_dropped, instants_dropped, events_scan_limited}
}
```

A pure derivation over rows that already exist — no new table, no new
writer:

- **launches**: `platform.launch WHERE session_id = ?`; start `booked_ts`,
  end `reconciled_ts`; `PROVISIONAL → booked`, `RUNNING → running`,
  `RECONCILED → complete`, `ABANDONED`/`REFUSED`/`DEFERRED → abandoned` (all
  three are bookings that will never run, and the bar says so rather than
  implying work in flight); lane = `agent_kind`, label = `purpose`.
- **jobs**: `jobs.job` rows created inside the window; start = the first
  `job_event` of type `claimed` (one `GROUP BY`), else `created_ts`; end =
  `settled_ts`; `retried` when `attempts > 1` or a `reclaimed` job_event
  exists; lane = `kind · subject`.
- **rooms**: `ops.event WHERE session_id = ?` — `room_created` opens a span
  per `payload.room_id`, `room_frozen`/`room_converged` closes it as
  `frozen`/`complete`; a room still open has `end_ts: null`.
- **instants**: `hook_alive` and `room_dp_scored` from the same scan;
  `ops.gate_transition` rows inside the window.

Caps: 200 spans and 200 instants, newest kept, with the dropped counts in
`truncated` (the house rule: truncation reports itself). The event scan is
bounded at 5,000 rows and says so via `truncated.events_scan_limited`.

Timestamps are compared as strings, not parsed per row: every stamp in the
harness is written by `trialerror.util.timeutil.now()` in one fixed format,
so a lexicographic compare IS a chronological compare.

### 13.3 `gates` panel — decoded edits (landed in C1/C3, documented here)

`pending_edits[].edits` is a decoded array, and each entry carries
`unverified_count` — the number the Console's GATES card and the rail badge
both want, computed once, server-side.

### 13.4 What the client does with all of it

`static/console_render.js` (`window.TEConsole`) holds every Console
renderer. It never touches the page's global node factory: nodes come from
an element helper the caller injects (`h`, and `hs` for the SVG namespace),
which is what lets the same shipped file run under Node against
`tests/_dom_shim.js`. The pure decisions — `computeHealth`,
`compressIdleGaps`, `timelineX`, `jobsSnapshotOf`, every formatter — are
data-in/data-out and are exported on the module for reuse and for testing.

Two client behaviours are worth knowing about from the server side:

- **the one-second tick.** Every rendered stamp carries `data-ts`; while
  Console is the active panel an interval re-reads them, and re-lays the
  timeline when a span has no `end_ts`. It is cleared on the way out of the
  panel. Nothing refetches: the tick re-reads what the page already holds.
- **`jobsSnapshot`.** The JOBS card diffs against its own previous render to
  draw `↑` / `Δ` / `↓`. It is in-memory, per page life, and a render with no
  previous snapshot shows no markers — a page that flags every row as new on
  load says nothing.

### 13.5 Console DOM hooks

Each of the eight cards has **two** `data-role` hooks: `console-body-<name>`
and `console-head-<name>`, where `<name>` is the card's own name except
`diagnostics`, whose hooks are `console-*-doctor` (the card is named for what
it reports, the hook for the panel that feeds it). The head slot carries that
card's one-glance status reading. `RUN THE CHECK SWEEP` is drawn by the
DIAGNOSTICS card, not by the subbar; in a static snapshot it is drawn
disabled with its reason, per §12.12's convention.

---

<!-- builder: lane C (dashboard completion), step C6, launch LNCH-01M1R8J31R24P6781WT1G05PZC
     spec of record: docs/reviews/LANE_C_DASHBOARD_COMPLETION_SPEC.md section 1 + ruling L-C5.
     Sections 13, 14 and 15 are lane C's additions (13 is C5's Console); every
     section above is lane b's and earlier, unchanged.
     trialerror/dashboard/data.py::build_evidence_panel +
     tests/test_dashboard_evidence.py + tests/test_dashboard_evidence_render.py
     are the source of truth if this document and the code disagree. -->

## 14. Evidence — `GET /dashboard/api/evidence[?claim_id=CLM-…|?anchor_id=ANC-…|?chunk_id=CHK-…]`

The claim-trace surface. Until lane C this tab carried a `gap-notice` saying it
had no backing route; this is that route.

### 14.1 Selecting a claim

Three selectors, resolved **in this order** by the builder (never by the route
— see 14.2):

| param | what it means | who sends it |
|---|---|---|
| `claim_id` | this exact claim | the rail, a co-anchored row, a page refresh |
| `anchor_id` | the newest live claim standing on that anchor, primary **or** extra | `TRACE ▸` on a search-result row (`results[].citation.anchor.anchor_id`) |
| `chunk_id` | the same trace one level coarser: any anchor on that chunk | a future chunk-level entry point |
| *(none)* | the newest live claim | first load |

An id that resolves to nothing is **200 with a reading**, never a 404:

```json
{"status": "ok", "active_claim_id": null, "claim": null,
 "not_found": {"kind": "anchor_id", "id": "ANC-…"},
 "index": ["… the rail still renders …"]}
```

A young corpus has anchors nobody has made a claim on yet, and a TRACE onto one
of them is an ordinary event, not an error.

A claim named **explicitly** by `claim_id` is returned even when it is expired
or invalidated — its own `expired_at`/`invalid_at` say so on the row. The rail
(`index`) is always the LIVE view.

### 14.2 `PANEL_QUERY_PARAMS` is a tuple of pairs

`serve.PANEL_QUERY_PARAMS["evidence"]` is
`(("claim_id","claim_id"), ("anchor_id","anchor_id"), ("chunk_id","chunk_id"))`.
`build_one_panel` passes through **every** param that is present and non-blank;
the builder owns the precedence between them. A blank value is treated as
absent, so a builder never has to tell "not asked" from "asked for nothing".

### 14.3 Payload

```
{status: "ok",
 index: [{claim_id, kind, text_short, confidence, created_at,
          source_id, source_title, anchor_count, superseded}],   <=100, newest first
 index_total, index_truncated,
 active_claim_id,
 not_found: {kind, id}                                           only when a selector missed
 claim: {claim_id, kind, text, fenced, confidence, created_at, valid_at,
         expired_at, invalid_at, superseded_by, created_by_launch},
 anchors: [{anchor_id, role: "primary"|"extra", doc_id, chunk_id, source_id,
            source_title, license_tier, page, char_start, char_end, quote,
            fenced, doc_sha_matches, quote_sha_matches, missing}],
 argues: {contradicts: [prov_edge...], supports: [prov_edge...],
          verdicts: [{verdict_id, procedure, procedure_version, label, ts,
                      issued_by_launch, prereg_compliant}],
          note},
 co_anchored_claims: [{claim_id, kind, text_short, shared: "anchor"|"chunk"|"document"}],
 neighbourhood: {seed_entities: [{entity_id, name, entity_type, via_anchor}],
                 nodes: [{id, kind: "claim"|"entity", label}],
                 edges: [{rel_id, src, dst, rel_type, fact_text, fenced, evidence_anchor}],
                 max_hops, hops_reached, hop_limit, truncated,
                 node_count, edge_count, edges_listed, seeds_dropped},
 lineage: {superseded_by, supersedes: [claim_id...]},
 term_conflicts_omitted: {reason: "awaiting_migration", message}}   see 14.7
```

### 14.4 Fencing and the untrusted wrapper — which fields, and why

Four different treatments, and the differences are deliberate:

| field | treatment |
|---|---|
| `claim.text` | `citation_quote(text, fenced=<primary anchor's source tier>)` **then** `untrusted_wrap`. It is a free-text body; the client strips the wrapper and renders a text node. |
| `neighbourhood.edges[].fact_text` | already fenced **and** wrapped by the engine (`retrieve.engine._fence_relation_edges`). This builder does not redo it. |
| `anchors[].quote` | `citation_quote` only, **not** wrapped — the same treatment `get_chunk`/`resolve_quote` give their own per-anchor `quote` field. |
| `index[].text_short` | fence-capped, then truncated to 140 chars, **not** wrapped. Truncating a wrapped string can cut its closing delimiter off, which is exactly the forged-close hazard `untrusted_wrap` exists to prevent. |

`fenced: true` means the source is `commercial_restricted`, and the quote is
then capped at 20 words (D-COC-1) by the engine's own `citation_quote` — this
module calls that function rather than re-implementing the cap.

### 14.5 The two hash chips

They answer different questions and are reported separately:

- `doc_sha_matches` — `quote_anchor.doc_sha256` against `document.sha256`.
  `false` means the document was re-ingested after this anchor was cut, so its
  character offsets may now point at different bytes. (The same predicate the
  corpus panel's stale-anchor count uses.)
- `quote_sha_matches` — the stored `quote_text` re-hashed against
  `quote_sha256`. **`null`, not `false`**, when no `quote_text` was stored:
  "not re-checkable here" is a third reading, and collapsing it into `false`
  would accuse an anchor that is merely terse.

`missing: true` marks an anchor id a claim names that has no `quote_anchor` row
— a broken FK, reported on the row rather than dropped.

### 14.6 Bounds, all reported

| field | bound | what the client shows |
|---|---|---|
| `index` | 100 live claims | `index_truncated` → the rail says how many it is not showing, and that the filter searches only these |
| `neighbourhood.seed_entities` | 5 | `seeds_dropped` → a header chip |
| `neighbourhood.edges` | 100 | `edges_listed` vs `edge_count` → "N OF M EDGES NOT LISTED" |
| `neighbourhood` traversal | the engine's own `max_hops` / `hop_limit` | `truncated` → "RESULT TRUNCATED AT n EDGES" |
| `co_anchored_claims` candidates | 200 scanned | *(not surfaced; a stated bound in the builder's docstring)* |

The inline SVG is drawn only when `node_count <= 100`
(`TEEvidence.SVG_NODE_CEILING`); past that the edges table stands alone and the
card says `N NODES, DRAWN AS A TABLE ONLY`. The table is present either way.

### 14.7 What is NOT here

- **`prov_edge` is read, and reported empty.** The table has zero writers
  anywhere in this codebase; `argues.note` says so. `verdict(subject_kind=
  'claim', procedure='contracrow')` is the live contradiction signal.
- **Term-sense conflicts are omitted, with the reason stated** (ruling L-C5).
  Lane e (E4) adds `lexicon.api.conflicts_for_claim` and this builder picks it
  up behind an import guard. Until then the payload carries
  `term_conflicts_omitted: {reason: "awaiting_migration", message}` and the
  renderer prints that message — never an empty box that reads "no conflicts".
  When lane e lands, the key becomes `term_conflicts: {status, conflicts}`.
- **SEND TO DETERMINATIONS / OPEN A ROOM ON IT are drawn disabled**, with their
  reasons in `title` (section 12.11's convention): no callable exists for
  either verb.

### 14.8 Renderer

`static/evidence_render.js` → `window.TEEvidence`. Same contract as
`console_render.js`: no `document`, every node from an injected `h`. It takes a
**second** injected helper, `svg`, because an SVG child needs `createElementNS`
in a browser; with no `svg` given it falls back to `h`, which is what the Node
harness uses.

It is a hard dependency on `console_render.js` (the shared `h2` / `rowButton`
primitives) and refuses at `create()` time if that file did not load — there is
no local half-copy. Both are listed in `export.py::_INLINE_SCRIPTS`, in load
order, and the template loads them in that order too.

---

<!-- builder: lane C (dashboard completion), step C7.
     spec of record: docs/reviews/LANE_C_DASHBOARD_COMPLETION_SPEC.md section 4 +
     rulings L-C1 (ops v8), L-C2 (identity), L-C3 (reveal), L-C6 (no REJECT).
     trialerror/dashboard/writes.py + tests/test_dashboard_writes.py +
     test_dashboard_serve.py's full-loop assertions are the source of truth if
     this document and the code disagree. -->

## 15. Writes, part two — the four actions lane C wired (C7)

Section 12's contract is unchanged: same route shape, same token header, same
`{ok, result}` / `{ok, status, message}` envelope, same
`_validate_fields` type table. Four actions join the nine already there
(thirteen live), and four of section 12.12's "what stays disabled, and why"
rows are superseded — see the note at the head of that section.

| action | body | module called | refusals (verbatim from the module) | audit |
|---|---|---|---|---|
| `prereg-reveal` | `{prereg_id}` + optional `session_id` | `verify.prereg.reveal_prereg` | `PreregNotFoundError`, `PreregVoidedError`, `PreregTamperedError` (voids the row as a side effect), `ValidationError` (unknown `session_id`) | `prereg_revealed`, written by the module |
| `memory-resolve` | `{group_id, keep}` — `keep ∈ left/right/both` | `memory.merge.resolve_conflict` | `ValueError` (unknown group, already resolved, bad `keep`) | `memory_conflict_resolved`, written by the module |
| `gate-send-back` | `{gate_id, edit_id, by_launch, note}` — all four required | `artifacts.gates.send_back_edit` | `ValueError` (state, unknown edit, already verified, empty note), `XidTargetMissingError` (unknown launch) | `gate_edit_sent_back`, in the same transaction as the mutation |
| `thread-create` | `{title, body}` + optional `session_id` | `events.api.create_thread` then `post_feed` | `ValidationError` (no open session) | the `thread` + `feed_post` rows |

### 15.1 `prereg-reveal` — the irreversible one

Three guards, and none of them is the browser's to relax.

- **`dest_dir` is never read from the body.** The reveal always lands under
  `program_root/prereg/revealed/`. An HTTP caller naming a write path is a
  path-traversal primitive, not a feature — the same reason EXPORT TRANSCRIPT
  stays disabled (12.12).
- **Hashes only, until revealed.** The determinations item carries
  `procedure_sha256`, `params_sha256` and `escrow_present`, and never the
  sealed content (REDESIGN 5.4). `escrow_present: false` means a reveal will
  refuse *and void the commitment*, and the item's `consequence` says so — the
  operator should not learn that by pressing the button.
- **Two clicks in the browser** (ruling L-C3). The button becomes
  `CONFIRM REVEAL — IRREVERSIBLE` for 5 seconds, then disarms. This is a
  CLIENT guard and is deliberately not duplicated on the wire: a confirm token
  would be one more thing to forge, and the endpoint's real protection is the
  write token.

The `prereg_revealed` event is written by `reveal_prereg` itself, not by this
layer, so a CLI reveal and a browser reveal leave the identical record. Its
payload is `{prereg_id, revealed_path, procedure_sha256, params_sha256}` — the
committed hashes, never the revealed content, because an event log is not the
place to un-blind a procedure a second time. `session_id` names the sitting
that broke the blind; it must be a real `ops.session` row (`event.session_id`
is a same-file FK) and is **checked before anything is written**, so an
attribution mistake refuses instead of leaving a broken blind with no audit
row. Omitted, it resolves to the newest open session, or `null` when nothing
is open.

A tampered escrow is a **finding**, not a server fault: `VerifyError` is in
`_EXPECTED_ERRORS`, so it reaches the client as a clean `ok: false` with the
module's own message, and never as a 500.

**A fourth refusal, added in the C9 fix pass (finding F2): a revealed
pre-registration cannot be revealed again.** `PreregAlreadyRevealedError` (a
`VerifyError`, so also a clean `ok: false`) names the id and the first
reveal's timestamp. A reveal is the one irreversible act here and happens
exactly once; the second call used to succeed, re-copying the escrow,
overwriting `revealed_ts` with the later time and appending a second
`prereg_revealed` event — so the moment the blind actually broke survived
only in the log. The Determinations queue drops the item after the first
reveal, so only a direct CLI or HTTP call reaches this.

### 15.2 `memory-resolve`

`keep` is validated by `resolve_conflict`, not by this layer — one copy of the
rule. Resolution is **one-shot per group**: a second call with a different
answer is refused, so a double-click cannot quietly change the outcome.

The determinations item now carries `versions: [{side, memory_item_id, tier,
kind, account_id, updated_ts, l0_abstract, body}]`. `version_count` alone made
this the one queue kind an operator could not decide from the page — "2
versions of X disagree" is not something you can choose between.

### 15.3 `gate-send-back`

The non-destructive counterpart to `verify-edit`, and **not** a state
transition: it mutates one entry of the `edits` JSON array and writes no
`gate_transition` row (12.12's posture for `verify_edit`, unchanged). The
entry becomes `applied=False, verified=False, sent_back=True` plus
`sent_back_note` / `sent_back_by_launch` / `sent_back_ts`.

A sent-back edit is an **unverified** edit, so it still blocks
`union_applied`, with the same `unverified_blocking` refusal, and it **stays
in the determinations queue** — with the objection visible on it, so the next
operator does not verify it blind. Sending back is a request for work, not a
way around the gate.

`note` is required. A send-back with no stated objection is the
freeze-without-reason case.

Unlike a verification — whose record is the JSON entry itself — this emits
`gate_edit_sent_back`, in the same transaction as the mutation: an objection
has to reach whoever must now do the work, and nothing else in the schema
would carry it.

**Identity (interim rule L-C2).** `by_launch` stays free text on the wire, and
an id with no `platform.launch` row **fails with `XidTargetMissingError`**. It
never falls back to another identity — an unattributable objection is worse
than a refused one. The same rule binds `verify-edit`. A platform-level
operator identity is a separate lane after the handover.

**No REJECT** (ruling L-C6). `gated → failed` is legal in the state machine
and is not wired here; it stays a CLI verdict path (`gate verdict`) until the
identity ruling lands, because a destructive verb driven by a free-text
identity is not auditable. No control anywhere on Decide carries the word.

### 15.4 `thread-create` — and ops v8

Opens a thread AND posts the first message into it. A first post is required:
an empty thread is a room with nobody in it.

Authorship is server-derived and never caller-settable (`launch_id=None`, like
`feed-post`), so the result's `author` is `orchestrator:<open session>` and
`thread.created_by_launch` is NULL.

Section 12's own text used to explain why this action could not exist:
`thread.created_by_launch` was `NOT NULL` and an orchestrator identity has no
launch. **ops v8** (`ops_v8_thread_created_by_nullable_and_author`, ruling
L-C1) changes two things on `thread`:

- `created_by_launch` becomes **nullable**. Its XID registry entry is
  unchanged and still enforced for every non-null value — `_validate_xids`
  already skips a NULL, so "null allowed, non-null still validated" needed no
  new code.
- `created_by` is **new and nullable**: the derived author string, the same
  shape `feed_post.author` carries. Pre-v8 rows have `NULL` here, because
  deriving one needs a `platform.launch` lookup and platform is a different
  file — readers fall back to `created_by_launch` for those, which is what
  they had before.

It is a table rebuild (dropping a NOT NULL is not an ALTER SQLite has), and
`feed_post.thread_id` is a same-file FK child with rows in it — which is
exactly what `stores/migrate.py`'s `PRAGMA foreign_keys` OFF/ON bracketing
exists for. v2's `status`/`refs` columns and their CHECK are carried through.

`trialerror feed post --new-thread` no longer requires `--launch-id`; the
refusal moved to `create_thread`, which raises when there is no launch *and*
no open session.

**Numbering.** v7 is the mining-adoptions lane's `memory_relation` migration,
which merged first; lane c takes v8. `MIGRATIONS` is contiguous 1..8, and each
constant is named with its own number (`_V8`) — two branches binding one name
to different DDL is a footgun Python will not report.

### 15.5 Determinations item enrichments (C7)

| kind | new fields |
|---|---|
| `prereg_reveal` | `procedure_sha256`, `params_sha256`, `escrow_present` |
| `memory_conflict` | `versions: [{side, memory_item_id, tier, kind, account_id, updated_ts, l0_abstract, body}]` |
| `gate_edit` | `sent_back`, `sent_back_note`, `sent_back_by_launch`, `sent_back_ts` |

The Decide detail no longer calls the generic object renderer for
`prereg_reveal` or `memory_conflict` — those two are the last call sites
outside the ext panels. The rest of the kinds keep it; their shapes read
perfectly well as a key/value table.

### 15.6 What section 12.12 still says

`room_escalation`, `memory_conflict_candidate` and `memory_stale` stay
unwired, each with its own reason in the control's `title`. The generic
disabled arm now draws ONE control labelled `NO ACTION WIRED HERE` rather than
two named after verbs those kinds do not have — a disabled button naming a
verb the subsystem cannot perform is a promise the page cannot keep.
## 16. The C9 fix pass — two contract changes worth knowing about

The adversarial verification of stage 2 closed twelve findings. Ten were
tests, comments or internals; these two change what a caller sees.

### 16.1 A write refuses BEFORE it mutates, not after (finding F1)

Every write action that carries a launch id now validates it against
`platform.launch` **before** it touches any state. Three did not:
`merge-accept`, `merge-reject` and `acquisition-delivered` mutated first and
wrote their audit `event` row second, and since `event.launch_id` is an XID
column, an unknown launch was refused by the audit insert — after the
proposal had already moved to `confirmed`/`rejected`, or the source to
`delivered`. The client saw `{"ok": false}`, the store disagreed, no audit
row explained it, and the retry hit "is not draft" with no way back.

What changed for a caller: nothing about the message (the refusal is the same
`XidTargetMissingError` text, `status: "XidTargetMissingError"`), and
everything about the state afterwards — **a refused write has written
nothing, and the same call with a real launch id then succeeds.** That was
already true of `verify-edit`, `gate-send-back`, `room-turn`, `room-score`
and `room-freeze`; it is now true of all of them. Ruling L-C2's letter — a
named error, never an identity fallback — is unchanged.

The same guard covers `extract.accept()` / `reject()`'s candidate arm
(`RCD-` ids), which shares the dispatch surface `merge-accept` calls.

### 16.2 Keyboard: V and B on the Decide gate arm (finding F6)

Spec section 4 asked for them and C7 shipped only the buttons. `V` presses
VERIFY EDIT, `B` presses SEND BACK, both scoped to the selected item's
detail pane, both no-ops when the control is disabled, and both inert while
focus is in an input, a textarea or a select (so typing a note that contains
"b" cannot send an edit back). Alt/Ctrl/Meta combinations are left to the
browser.

**No keyboard path reaches an irreversible verb.** The handler refuses any
control carrying `data-confirm-label` — the attribute `requireTwoClicks`
stamps on a destructive button (today only REVEAL). That is a structural
guard, not a safe-list: a future arm that arms a destructive button gets no
key binding to it for free, and ruling L-C3's "two clicks, from the one
listener that submits" keeps holding. The synthetic click is dispatched on
the button so the submit runs through `wireWriteAction` — required-field
check, disabled state, message strip and the WA-2 reload included — and it
is the page's only synthetic click, which a test pins.
