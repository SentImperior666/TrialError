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
      "translation": {
        "translation_id": "XLAT-01M178QK2S946RDDWD4F8NSJA8",
        "body": "test translation body",
        "style_mode": "flavored",
        "translator_version": "1",
        "faithfulness_score": null,
        "created_ts": "2026-08-29T17:22:59.801Z"
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
  "translator_table_available": true
}
```

Notes:

- `threads` — every thread, newest-created first (`trialerror.events.api.list_threads`, unchanged, limit 100). `refs`/`status` come from `thread`'s schema-v2 columns (`status` is `'active'`/`'archived'`; `refs` is free-form JSON or `null`).
- `active_thread_id` — the resolved selection: the `thread_id` you passed, or (default) the thread with the most recently-posted message, or (if no thread has any posts yet) the newest-created thread, or `null` if there are zero threads at all.
- `posts` — full-text posts in `active_thread_id`, oldest first (append order). `author` is server-derived and NEVER caller-settable (`trialerror.events.api._derive_author`) — always `"<agent_kind>:<launch_id>"` or `"orchestrator:<session_id>"`.
- `kind` — the text before the first `:` in `author`. Use this to badge a post (`orchestrator`, `lens`, `critic`, whatever `agent_kind` a launch actually used — this is real data, not a fixed enum, so render an unknown value neutrally rather than assuming a closed set).
- `translation` — **`null` if this post has never been translated, or if the `feed_post_translation` table doesn't exist yet on this program** (see `translator_table_available`). This build creates only the TABLE seam (internal design notes, not in this export) — no job handler, no CLI verb, no translator exists yet, so **every real program's posts will show `translation: null` until that future feature ships and a translation job actually runs.** When present, it is always the one `status='current'` row for that post — `faithfulness_score` is `null` until a faithfulness gate has actually scored it (§4.3 of that design doc; not built in this stage either).
- `unread_directives` — **NOT scoped to `active_thread_id`.** `inbox_item` (the operator directive channel) carries no `thread_id` column in the real schema — it is a program-wide channel. Render it as its own "operator inbox" surface, not inline in the thread's post stream (the `Feed.dc.html` mockup shows an inline "OPERATOR ... DIRECTIVE" card; that shape isn't backed by real per-thread data — build the directive UI as a separate list instead). Reading this list is a plain `SELECT ... WHERE read_ts IS NULL` — it does **not** mark anything read (`mark_read=False` is always passed).
- `translator_table_available` — `true`/`false`. Useful to grey out or hide the "Translate ▾" affordance entirely on a program whose `ops.db` predates schema v4.

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

## 10. Schema migration summary (ops_v4)

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

`feed_post_translation`'s shape is verbatim from the internal translator design notes (not in this export; this build creates the TABLE seam only — no job handler, no CLI verb, no translator logic). `created_by_launch` (→ `platform.launch`) and `faithfulness_verdict_id` (→ `knowledge.verdict`) are registered as cross-store XIDs in `trialerror/stores/xid.py`; `post_id` is a same-file FK (both tables live in `ops.db`), not an XID.

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

### 12.10 `POST /dashboard/api/doctor/run`

Was `GET` before this build (`trialerror.dashboard.doctor_run.run_doctor_and_persist`
WRITES a sidecar state file — `<program_root>/.trialerror_dashboard/doctor_state.json`
— so it belongs under the same write guard as every action above). A bare
`GET` on this route now returns `405 Method Not Allowed` with an `Allow:
POST` header. No request body is read. Response shape: unchanged from
before — the doctor panel's own `{"status": "ok", "last_run": {...}}`
(§ the `doctor` panel; not itself one of the seven Stage-1/2 panels, but
present in every `/dashboard/api/all` response).

### 12.11 What stays disabled, and why

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

### 12.12 Eventing (verified per action)

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
| `doctor/run` | None — writes only its own sidecar state file (`trialerror.dashboard.doctor_run`), never the program's real stores. |
