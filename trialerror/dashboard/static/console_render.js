/* ============================================================================
   TEConsole -- the Console surface's renderers, in their own file.

   WHY THIS FILE EXISTS (house rule, LANE_C_DASHBOARD_COMPLETION_SPEC section 0;
   sweep section 3.0). The inline script in dashboard.html is ~2,600 lines and is
   edited by several parallel builds at once. Every NEW renderer therefore gets
   its own file under static/ -- console_render.js here, evidence_render.js and
   feed_render.js beside it -- so that two builds touching two surfaces do not
   touch the same hunk.

   WHAT THE CONSOLE IS (design/dashboard-v2/Console.dc.html): "THE MACHINE --
   everything the old landing page used to be, on one screen you open on
   purpose". Eight cards, each a pure function of the /dashboard/api/all
   bundle, so the static export renders the same readings as the live page
   from the same dict. Nothing here fetches, and nothing here writes.

   HOW IT IS WRITTEN. Nothing in this file may reach for the page's global
   node factory. Every node is built through an element helper the caller
   INJECTS (`h`), whose contract is dashboard.html's own `el()`:

       h(tag, attrs, children) -> element
         attrs:  "class" -> className, "text" -> textContent,
                 "on<event>": function -> addEventListener(<event>, fn),
                 anything else -> setAttribute(key, value)
         children: array of (string | node | null); strings become text nodes.

   `hs` is the same contract in the SVG namespace (the timeline's bars). It
   defaults to `h`, which is what the Node DOM shim wants -- the shim has one
   namespace, and a test asserting on a <rect>'s x and width does not care
   which one it came from. A real browser very much does: an <svg> built by
   the HTML factory is an unknown element that renders nothing at all, which
   is why the page passes a real namespaced helper.

   That one rule is what lets the same code run under Node against the DOM
   shim in tests/_dom_shim.js (sweep section 3.11) -- no browser, no npm
   dependency, real assertions on the tree a card produces.

   PURE FUNCTIONS ARE THE POINT. Everything that decides something --
   `computeHealth`, `compressIdleGaps`, `jobsSnapshotOf`, every formatter --
   takes data and returns data, with no element in sight. They are on the
   module object AND on what create() returns, so a test can call them
   directly and a card can use them without a second copy.

   HALIDE rules that bind this file (dashboard.css header, Tokens.dc.html):
   four semantic hues, and `--live` never means "fine"; status is glyph +
   word, colour third; zero is a reading, not an absence; truncation reports
   itself ("N more not shown"); meters share one 0-100 ramp with a tick at
   the trigger, and stay FLAT (the 101-step gradient is a credited idea this
   build deliberately did not adopt).

   PUBLIC SHAPE
   ------------
     TEConsole.VERSION          -- bumped when the create() contract changes
     TEConsole.CARD_ORDER       -- the eight Console cards, in grid order
     TEConsole.jobsSnapshot     -- the previous JOBS render, for the up/delta/down
                                   markers (sweep section 3.5); the renderer owns it
     TEConsole.h2 / .rowButton  -- shared primitives, h first (Evidence + Feed
                                   reuse them without calling create())
     TEConsole.computeHealth(panels)          -> {level, label, crit[], warn[]}
     TEConsole.compressIdleGaps(win, spans, instants, opts) -> layout
     TEConsole.timelineX(layout, tMs)         -> x in the layout's own units
     TEConsole.jobsSnapshotOf(jobsPanel)      -> {job_id: {...}}
     TEConsole.create({h, hs, now})           -- binds the helpers and returns
       the renderer: .cards / .registerCard / .renderInto / .clear, every card
       renderer by name (renderSessionCard, renderPoolsCard, ...), and every
       pure function above.

   Loaded by dashboard.html from a script tag before the inline script, and
   INLINED into the static export by export.py's _INLINE_SCRIPTS (a file://
   snapshot has no sibling files to fetch). Nothing in this file may contain a
   literal script end-tag: the HTML tokenizer would end the element there once
   the file is inlined. export.py refuses to build a snapshot if one appears.
   ============================================================================ */
(function () {
  "use strict";

  var VERSION = 2;

  /* The eight cards of the Console, in the order sweep section 3.9's grid lays
     them out: "session pools ledger" / "timeline" / "jobs diagnostics" /
     "gates corpus". renderInto paints in this order, so a card list read off a
     rendered page matches the canvas. */
  var CARD_ORDER = ["session", "pools", "ledger", "timeline", "jobs", "diagnostics", "gates", "corpus"];

  /* Sweep section 3.5: a heartbeat older than 2x the worker's own interval is
     late. 300 s is HEARTBEAT_INTERVAL_S in trialerror/jobs. */
  var HEARTBEAT_LATE_S = 600;

  /* Sweep section 3.4: idle stretches longer than 5% of the session (never
     less than a minute) collapse to a fixed-width marker, so the busy parts
     get the pixels. The layout is computed in a 1000-unit space and scaled by
     CSS, which is what keeps it resolution-independent and testable. */
  var TIMELINE_WIDTH = 1000;
  var GAP_PX = 14;
  var IDLE_FRACTION = 0.05;
  var IDLE_FLOOR_S = 60;
  var INSTANT_HALO_S = 30;
  var MAX_LANES = 24;

  var GLYPHS = {
    live: "●", settled: "✓", warn: "▲", crit: "✗",
    pending: "○", paused: "⬣", stale: "□", "new": "◆",
    fenced: "■"
  };

  /* Status vocabularies (sweep section 3.8). Each entry is [key, label, kind].
     Declared order is WORST FIRST wherever the values carry severity -- the
     same ruling section 3.6 makes for the DIAGNOSTICS rows, for the same
     reason: a failure must never sit below a long list of successes. */
  var VOCABS = {
    JOB_STATE: [
      ["running", "RUNNING", "live"], ["claimed", "CLAIMED", "live"],
      ["abandoned", "ABANDONED", "crit"], ["failed", "FAILED", "warn"],
      ["pending", "PENDING", "pending"], ["paused", "PAUSED", "paused"],
      ["complete", "COMPLETE", "settled"]
    ],
    LAUNCH_STATE: [
      ["RUNNING", "RUNNING", "live"], ["PROVISIONAL", "PROVISIONAL", "warn"],
      ["ABANDONED", "ABANDONED", "pending"], ["REFUSED", "REFUSED", "pending"],
      ["DEFERRED", "DEFERRED", "pending"], ["RECONCILED", "RECONCILED", "settled"]
    ],
    GATE_STATE: [
      ["failed", "FAILED", "crit"], ["draft", "DRAFT", "pending"],
      ["submitted", "SUBMITTED", "pending"], ["gated", "GATED", "pending"],
      ["union_applied", "UNION APPLIED", "settled"], ["registered", "REGISTERED", "settled"]
    ],
    GATE_VERDICT: [
      ["FAIL", "FAIL", "crit"], ["PASS_WITH_EDITS", "PASS WITH EDITS", "warn"],
      ["PASS", "PASS", "settled"]
    ],
    REPRO: [
      ["mismatch", "MISMATCH", "crit"], ["unrun", "UNRUN", "pending"],
      ["match", "MATCH", "settled"]
    ],
    REQUEST_STATE: [],
    DOC_STATUS: [["failed", "FAILED", "warn"], ["pending", "PENDING", "pending"],
                 ["ingested", "INGESTED", "settled"]],
    LICENSE_TIER: []
  };

  /* A panel status is never just "not ok" (console-8): an expected empty
     state must not alarm, and a design-invariant violation must escalate. */
  var STATUS_TIERS = {
    not_initialized: "neutral", never_run: "neutral", awaiting_data: "neutral",
    awaiting_migration: "neutral", static_no_engine: "neutral",
    ext_error: "warn", error: "warn", search_error: "warn",
    invariant_violation: "crit"
  };

  /* ------------------------------------------------------------------------
     Pure formatters and computations. No element, no injected helper -- these
     are reachable by name from the Node harness and from any other renderer.
     ------------------------------------------------------------------------ */

  function isArray(v) { return Object.prototype.toString.call(v) === "[object Array]"; }

  function parseTs(iso) {
    if (iso === null || iso === undefined || iso === "") return NaN;
    var ms = Date.parse(iso);
    return isNaN(ms) ? NaN : ms;
  }

  /** "4s ago" / "3m ago" / "2h 14m ago" / "3d ago"; a future stamp reads
   * "in 2h 10m"; anything unparseable comes back as the raw string, because a
   * stamp this cannot read is still information and an em dash is not. */
  function fmtAgoText(iso, nowMs) {
    var ms = parseTs(iso);
    if (isNaN(ms)) return iso === null || iso === undefined ? "—" : String(iso);
    var reference = typeof nowMs === "number" ? nowMs : Date.now();
    var delta = reference - ms;
    var future = delta < 0;
    var s = Math.floor(Math.abs(delta) / 1000);
    var unit;
    if (s < 60) unit = s + "s";
    else if (s < 3600) unit = Math.floor(s / 60) + "m";
    else if (s < 86400) unit = Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m";
    else unit = Math.floor(s / 86400) + "d";
    return future ? "in " + unit : unit + " ago";
  }

  /** "88s" / "2m 30s" / "4h 0m" / "2d 3h" -- how long something TOOK, which is
   * a different question from how long ago it was and reads differently. The
   * seconds cutoff is two minutes rather than one: the canvas's own example
   * is a job that took 88 s, and "1m 28s" is harder to compare against the
   * row above it than "88s" is. */
  function fmtDuration(seconds) {
    if (typeof seconds !== "number" || isNaN(seconds)) return "—";
    var s = Math.max(0, Math.floor(seconds));
    if (s < 120) return s + "s";
    if (s < 3600) return Math.floor(s / 60) + "m " + (s % 60) + "s";
    if (s < 86400) return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m";
    return Math.floor(s / 86400) + "d " + Math.floor((s % 86400) / 3600) + "h";
  }

  /** Three significant figures with a magnitude suffix: 3,569,548 -> "3.57M",
   * 1,800,000 -> "1.8M", 340,000 -> "340k". The full integer always travels in
   * `title=`, so the compact form loses nothing. */
  function fmtCompact(n) {
    if (typeof n !== "number" || isNaN(n)) return n === null || n === undefined ? "—" : String(n);
    var sign = n < 0 ? "-" : "";
    var a = Math.abs(n);
    function trim(x, digits) {
      var s = x.toFixed(digits);
      if (s.indexOf(".") !== -1) s = s.replace(/0+$/, "").replace(/\.$/, "");
      return s;
    }
    if (a >= 1e9) return sign + trim(a / 1e9, 2) + "B";
    if (a >= 1e6) return sign + trim(a / 1e6, 2) + "M";
    if (a >= 1000) return sign + trim(a / 1000, 0) + "k";
    return sign + trim(a, a < 10 && a !== Math.floor(a) ? 2 : 0);
  }

  var fmtTokens = fmtCompact;

  /** "LNCH-01M1R8J31R24P6781WT1G05PZC" -> "LNCH-...5PZC". Twenty of these fit
   * on one screen only in this form; the full id is one hover away. */
  function shortId(value, n) {
    n = n || 8;
    var text = value === null || value === undefined ? "" : String(value);
    if (text.length <= n) return text;
    var dash = text.indexOf("-");
    if (dash > 0 && dash <= 6) return text.slice(0, dash) + "-…" + text.slice(-n);
    return "…" + text.slice(-n);
  }

  function pad2(n) { return (n < 10 ? "0" : "") + n; }

  /** UTC wall-clock, because every stamp in the harness is UTC and a console
   * that silently localises them cannot be compared against a log. */
  function fmtClockMs(ms, withSeconds) {
    if (typeof ms !== "number" || isNaN(ms)) return "—";
    var d = new Date(ms);
    var text = pad2(d.getUTCHours()) + ":" + pad2(d.getUTCMinutes());
    return withSeconds ? text + ":" + pad2(d.getUTCSeconds()) : text;
  }

  function fmtClock(iso, withSeconds) { return fmtClockMs(parseTs(iso), withSeconds); }

  /** HH:MM:SS since a stamp -- the SESSION card's OPEN reading, which ticks. */
  function fmtElapsedClock(iso, nowMs) {
    var ms = parseTs(iso);
    if (isNaN(ms)) return "—";
    var reference = typeof nowMs === "number" ? nowMs : Date.now();
    var s = Math.max(0, Math.floor((reference - ms) / 1000));
    return pad2(Math.floor(s / 3600)) + ":" + pad2(Math.floor((s % 3600) / 60)) + ":" + pad2(s % 60);
  }

  /** The client-side safety net for a JSON-text column the server forgot to
   * decode. The builders decode (sweep section 3.10); this exists so a stale
   * server never puts a brace into a table cell. */
  function jsonish(value) {
    if (typeof value !== "string") return value;
    var t = value.trim();
    if (!t || (t.charAt(0) !== "{" && t.charAt(0) !== "[")) return value;
    try { return JSON.parse(t); } catch (e) { return value; }
  }

  function vocabEntry(vocabName, key) {
    var list = VOCABS[vocabName] || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i][0] === key) return { label: list[i][1], kind: list[i][2], order: i };
    }
    if (key === "__null__") {
      // console-9: every gate starts life with a NULL verdict. The sentinel is
      // a JSON-transport detail; "unset" is the reading.
      return { label: "unset", kind: "pending", order: list.length + 1 };
    }
    return {
      label: String(key).toUpperCase().replace(/_/g, " "),
      kind: "pending",
      order: list.length
    };
  }

  /* ---- rail health (sweep section 3.7; console-2, M-CON-2) --------------- */

  /** The one at-a-glance indicator on the rail, over the WHOLE bundle. It used
   * to read OK in the most common bad state this harness has: an open session
   * that cannot close, with PROVISIONAL launches under it.
   *
   * Deliberately NOT counted: `feed.translation_withheld_count`. A gate
   * refusing to publish an unverified translation is the system working
   * (spec section 3 item iii). */
  function computeHealth(panels) {
    panels = panels || {};
    var crit = [], warn = [];

    var session = panels.session || {};
    if (session.status === "invariant_violation") {
      crit.push("session invariant violation: " + (session.message || "more than one open session"));
    }
    var readiness = (session.open_session || {}).close_readiness;
    if (readiness && readiness.ready === false) {
      var problems = readiness.problems || [];
      if (problems.length) {
        problems.forEach(function (pr) {
          warn.push(typeof pr === "string" ? pr : ((pr && pr.message) || "session cannot close"));
        });
      } else {
        warn.push("the open session cannot close");
      }
    }

    var budget = panels.budget || {};
    var dangling = (budget.dangling_bookings || []).length;
    if (dangling) crit.push(dangling + " dangling launch booking(s)");
    var quota = budget.plan_quota || {};
    if (quota.available && !quota.fresh) warn.push("plan quota snapshot is stale");
    (budget.accounts || []).forEach(function (a) {
      (((a || {}).budget_status || {}).pools || []).forEach(function (pool) {
        if (pool.over_hard) crit.push("pool " + pool.pool_id + " projected over hard cap");
        else if (pool.over_soft) warn.push("pool " + pool.pool_id + " projected over soft cap");
      });
    });

    var doctor = panels.doctor || {};
    if (doctor.status === "ok" && doctor.last_run && isArray(doctor.last_run.checks)) {
      doctor.last_run.checks.forEach(function (c) {
        if (c.status === "fail") crit.push("doctor FAIL: " + (c.name || "check"));
        else if (c.status === "warn") warn.push("doctor WARN: " + (c.name || "check"));
      });
    }

    var jobs = panels.jobs || {};
    var stale = (jobs.stale_leases || []).length;
    if (stale) crit.push(stale + " job lease(s) expired");
    var awaiting = (jobs.offload || {}).awaiting || 0;
    if (awaiting) warn.push(awaiting + " job(s) awaiting the offload worker");

    var corpus = panels.corpus || {};
    if ((corpus.stale_anchors || 0) > 0) crit.push(corpus.stale_anchors + " stale anchor(s)");

    var pendingEdits = ((panels.gates || {}).pending_edits || []).length;
    if (pendingEdits) warn.push(pendingEdits + " unverified gate edit(s)");

    // M-LU-2's client half: a builder that raised is reported as a panel with
    // status "error". A console whose own data is missing is not OK.
    Object.keys(panels).forEach(function (name) {
      var p = panels[name];
      if (p && typeof p === "object" && p.status === "error") {
        crit.push(name + " panel failed to build: " + (p.message || "no message"));
      }
    });

    return {
      level: crit.length ? "crit" : warn.length ? "warn" : "ok",
      label: crit.length ? crit.length + " CRIT" : warn.length ? warn.length + " WARN" : "OK",
      crit: crit,
      warn: warn
    };
  }

  /* ---- idle-gap compression (sweep section 3.4) -------------------------- */

  function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }

  /** Collapse the waiting so the working gets the pixels.
   *
   * A session is hours of nothing punctuated by bursts. Drawn linearly it is
   * one thin smear against an empty field; drawn with the gaps removed
   * entirely it lies about when things happened. So gaps longer than
   * max(5% of the window, 60 s) become a fixed-width marker that SAYS how much
   * time it stands for, every other second is linear, and the axis ticks carry
   * the real clock times at each boundary -- the operator can still read where
   * the time went.
   *
   * Returns data, not pixels-in-a-node: `{t0, t1, width, gapPx, thresholdMs,
   * segments: [{t0, t1, x0, x1, collapsed, label}], ticks, collapsedCount}`.
   * The mapping x(t) is fully determined by `segments`, which is what lets a
   * test verify monotonicity without calling back into JavaScript. */
  function compressIdleGaps(win, spans, instants, opts) {
    opts = opts || {};
    win = win || {};
    var width = typeof opts.width === "number" ? opts.width : TIMELINE_WIDTH;
    var gapPx = typeof opts.gapPx === "number" ? opts.gapPx : GAP_PX;
    var nowMs = typeof opts.nowMs === "number" ? opts.nowMs : Date.now();

    var t0 = parseTs(win.start_ts);
    if (isNaN(t0)) t0 = nowMs;
    var t1 = parseTs(win.end_ts);
    if (isNaN(t1)) t1 = nowMs;
    if (t1 <= t0) t1 = t0 + 1000;

    var haloMs = (typeof opts.instantHaloS === "number" ? opts.instantHaloS : INSTANT_HALO_S) * 1000;
    var occupied = [];
    (spans || []).forEach(function (s) {
      var a = parseTs(s.start_ts);
      var b = parseTs(s.end_ts);
      if (isNaN(a)) return;
      if (isNaN(b)) b = nowMs;
      occupied.push([clamp(a, t0, t1), clamp(b, t0, t1)]);
    });
    (instants || []).forEach(function (i) {
      var a = parseTs(i.ts);
      if (isNaN(a)) return;
      occupied.push([clamp(a - haloMs, t0, t1), clamp(a + haloMs, t0, t1)]);
    });
    occupied.sort(function (a, b) { return a[0] - b[0] || a[1] - b[1]; });

    var merged = [];
    occupied.forEach(function (iv) {
      var last = merged[merged.length - 1];
      if (last && iv[0] <= last[1]) { last[1] = Math.max(last[1], iv[1]); return; }
      merged.push([iv[0], iv[1]]);
    });

    var thresholdMs = Math.max(IDLE_FRACTION * (t1 - t0), IDLE_FLOOR_S * 1000);
    if (typeof opts.thresholdMs === "number") thresholdMs = opts.thresholdMs;

    // Cut [t0, t1] into busy stretches and idle gaps, with no hole between
    // them: a segment list that does not cover the window would make x(t)
    // ambiguous exactly where the operator is looking.
    var segments = [];
    var cursor = t0;
    merged.forEach(function (iv) {
      if (iv[0] > cursor) segments.push({ t0: cursor, t1: iv[0], collapsed: (iv[0] - cursor) > thresholdMs });
      if (iv[1] > iv[0]) segments.push({ t0: Math.max(cursor, iv[0]), t1: iv[1], collapsed: false });
      cursor = Math.max(cursor, iv[1]);
    });
    if (cursor < t1) segments.push({ t0: cursor, t1: t1, collapsed: (t1 - cursor) > thresholdMs });
    if (!segments.length) segments.push({ t0: t0, t1: t1, collapsed: false });

    // Collapsing makes room for the busy parts; with nothing busy there is
    // nothing to make room FOR, and squeezing the whole window into a 14-unit
    // marker would say "all idle" in the least readable way available. An
    // all-idle window is drawn linearly and the emptiness speaks for itself.
    if (segments.length && segments.every(function (s) { return s.collapsed; })) {
      segments.forEach(function (s) { s.collapsed = false; });
    }

    var collapsedCount = 0;
    var linearMs = 0;
    segments.forEach(function (s) {
      if (s.collapsed) collapsedCount += 1;
      else linearMs += (s.t1 - s.t0);
    });
    var linearWidth = Math.max(0, width - collapsedCount * gapPx);
    var perMs = linearMs > 0 ? linearWidth / linearMs : 0;

    var x = 0;
    segments.forEach(function (s) {
      s.x0 = x;
      x += s.collapsed ? gapPx : (s.t1 - s.t0) * perMs;
      s.x1 = x;
      if (s.collapsed) s.label = fmtDuration((s.t1 - s.t0) / 1000) + " idle";
    });
    segments[segments.length - 1].x1 = width;

    var ticks = [{ t: t0, x: 0, label: fmtClockMs(t0) }];
    segments.forEach(function (s) { ticks.push({ t: s.t1, x: s.x1, label: fmtClockMs(s.t1) }); });

    return {
      t0: t0, t1: t1, width: width, gapPx: gapPx,
      thresholdMs: thresholdMs, collapsedCount: collapsedCount,
      segments: segments, ticks: ticks
    };
  }

  /** x(t) over a layout from compressIdleGaps -- piecewise linear, monotonic
   * by construction, and clamped at both ends. */
  function timelineX(layout, t) {
    if (!layout || !layout.segments || !layout.segments.length) return 0;
    if (t <= layout.t0) return 0;
    if (t >= layout.t1) return layout.width;
    var segs = layout.segments;
    for (var i = 0; i < segs.length; i++) {
      var s = segs[i];
      if (t >= s.t0 && t <= s.t1) {
        if (s.t1 === s.t0) return s.x0;
        return s.x0 + (s.x1 - s.x0) * ((t - s.t0) / (s.t1 - s.t0));
      }
    }
    return layout.width;
  }

  /* ---- jobs deltas (sweep section 3.5, the k9s pattern) ------------------ */

  function progressKey(job) {
    var cp = jsonish(job.checkpoint);
    if (cp === null || cp === undefined) return "";
    if (typeof cp !== "object") return String(cp);
    try { return JSON.stringify(cp); } catch (e) { return ""; }
  }

  /** What the previous render knew about each job, as plain JSON -- so the
   * next render can say what CHANGED while you were looking away. */
  function jobsSnapshotOf(jobsPanel) {
    var out = {};
    (((jobsPanel || {}).recent_jobs) || []).forEach(function (job) {
      out[job.job_id] = {
        state: job.state,
        attempts: job.attempts,
        heartbeat_ts: job.heartbeat_ts,
        settled_ts: job.settled_ts,
        progress_key: progressKey(job)
      };
    });
    return out;
  }

  var SETTLED_STATES = { complete: true, abandoned: true };

  /** One row's marker against the previous snapshot: an up arrow for "started
   * since your last look", a down arrow for "finished", a delta for anything
   * else that moved, and nothing at all when there IS no previous render -- a
   * first paint that flags every row as new says nothing. */
  function jobDelta(job, snapshot) {
    if (!snapshot) return { marker: "", cells: {} };
    var before = snapshot[job.job_id];
    if (!before) return { marker: "↑", cells: {}, isNew: true };
    if (SETTLED_STATES[job.state] && !SETTLED_STATES[before.state]) {
      return { marker: "↓", cells: { state: true } };
    }
    var cells = {};
    if (job.state !== before.state) cells.state = true;
    if ((job.attempts || 0) !== (before.attempts || 0)) cells.state = true;
    if (job.heartbeat_ts !== before.heartbeat_ts) cells.heartbeat = true;
    if (job.settled_ts !== before.settled_ts) cells.lease = true;
    if (progressKey(job) !== before.progress_key) cells.progress = true;
    return { marker: Object.keys(cells).length ? "Δ" : "", cells: cells };
  }

  /* ------------------------------------------------------------------------
     Shared primitives that take `h` first, so evidence_render.js and
     feed_render.js reuse them without calling create() (spec section 3 item i).
     ------------------------------------------------------------------------ */

  /** A card title as a real heading element (M-VA-1: the app ships no <h*>
   * today, and a screen reader cannot skim a page of spans). `.title` already
   * carries the type scale, so <h2 class="title"> is visually identical to the
   * span it replaces -- dashboard.css resets the element's own margin and
   * size. */
  function h2(h, label, opts) {
    opts = opts || {};
    var attrs = {
      "class": opts.className ? "title " + opts.className : "title",
      text: label === null || label === undefined ? "" : String(label)
    };
    if (opts.attrs) {
      Object.keys(opts.attrs).forEach(function (k) { attrs[k] = opts.attrs[k]; });
    }
    return h("h2", attrs);
  }

  /** A table/list row that is itself the control (the A1 primitive: a clickable
   * row must be focusable and operable from the keyboard, which a div with an
   * onclick is not).
   *
   *   rowButton(h, "OPEN", {onClick: fn})
   *   rowButton(h, ["contradicts", "C-0004", "same anchor"], {onClick: fn})
   *   rowButton(h, "SEND TO DETERMINATIONS", {disabled: "no callable exists yet"})
   *
   * `content` is a string, a node, or an array of either -- an array becomes one
   * `.row-button__cell` span per entry, which is what makes a row of cells line
   * up under a header row.
   *
   * `opts.disabled` may be `true` or, better, the REASON as a string: a control
   * drawn disabled must say why it is disabled (DASHBOARD_V2_API section 12.11),
   * so the reason becomes the title unless an explicit `title` is given. A
   * disabled row is never given a click handler -- not even a no-op. */
  function rowButton(h, content, opts) {
    opts = opts || {};
    var cls = "row-button";
    if (opts.className) cls += " " + opts.className;
    if (opts.selected) cls += " is-selected";

    var attrs = { type: "button", "class": cls };
    var title = opts.title;
    if (opts.disabled) {
      attrs.disabled = "disabled";
      attrs["aria-disabled"] = "true";
      if (!title && typeof opts.disabled === "string") title = opts.disabled;
    } else if (typeof opts.onClick === "function") {
      attrs.onclick = opts.onClick;
    }
    if (title) attrs.title = title;
    if (opts.ariaLabel) attrs["aria-label"] = opts.ariaLabel;
    if (opts.selected) attrs["aria-current"] = "true";
    if (opts.dataset) {
      Object.keys(opts.dataset).forEach(function (k) { attrs["data-" + k] = opts.dataset[k]; });
    }

    var children;
    if (isArray(content)) {
      children = content.map(function (cell) {
        if (cell === null || cell === undefined) return h("span", { "class": "row-button__cell" });
        if (typeof cell === "string" || typeof cell === "number") {
          return h("span", { "class": "row-button__cell", text: String(cell) });
        }
        return h("span", { "class": "row-button__cell" }, [cell]);
      });
    } else if (content === null || content === undefined) {
      children = [];
    } else {
      children = [content];
    }
    return h("button", attrs, children);
  }

  /* ------------------------------------------------------------------------ */

  function create(env) {
    env = env || {};
    var h = env.h;
    if (typeof h !== "function") {
      throw new TypeError("TEConsole.create: env.h must be the element helper (tag, attrs, children) -> node");
    }
    // The SVG-namespace helper. Defaults to `h` for the DOM shim, which has
    // one namespace; a real page MUST pass a namespaced one or the timeline
    // renders as nothing at all.
    var hs = typeof env.hs === "function" ? env.hs : h;
    var nowFn = typeof env.now === "function" ? env.now : function () { return Date.now(); };

    var cards = {};

    function clear(node) {
      if (!node) return node;
      while (node.firstChild) node.removeChild(node.firstChild);
      return node;
    }

    /* ---- small node builders ------------------------------------------- */

    function div(cls, children) { return h("div", { "class": cls }, children || []); }
    function span(cls, text) { return h("span", { "class": cls, text: text }); }
    function line(cls, text) { return h("div", { "class": cls, text: text }); }

    /** Glyph + word, colour third (the HALIDE status rule). An empty label
     * gets an aria-label so a glyph-only badge still has a name. */
    function statusNode(kind, label, opts) {
      opts = opts || {};
      var attrs = { "class": "status status--" + kind };
      if (!label) attrs["aria-label"] = opts.ariaLabel || kind;
      if (opts.title) attrs.title = opts.title;
      return h("span", attrs, [
        h("span", { "class": "g" + (opts.pulse ? " pulse" : ""), text: GLYPHS[kind] || GLYPHS.pending }),
        label ? " " + label : ""
      ]);
    }

    function chip(text, variant, opts) {
      opts = opts || {};
      var attrs = { "class": "chip" + (variant ? " chip--" + variant : ""), text: text };
      if (opts.title) attrs.title = opts.title;
      return h("span", attrs);
    }

    /** A stamp that knows how to re-read itself: `data-ts` is what
     * `tickAges()` looks for on the one-second tick while Console is the
     * active panel (console-7 / LU-11 -- an ops console cannot ask the
     * operator to subtract ISO strings, and a frozen age is worse than none). */
    function fmtAgo(iso, opts) {
      opts = opts || {};
      var attrs = {
        "class": "ago" + (opts.className ? " " + opts.className : ""),
        text: fmtAgoText(iso, opts.nowMs)
      };
      if (iso) { attrs["data-ts"] = String(iso); attrs.title = String(iso); }
      return h("span", attrs);
    }

    /** label + value, and the value is printed even when it is zero -- zero is
     * a reading, not an absence. */
    function readingRow(label, value, opts) {
      opts = opts || {};
      var cls = "reading-row" + (opts.kind ? " status--" + opts.kind : "");
      var children = [];
      if (opts.kind && opts.glyph !== false) {
        children.push(h("span", { "class": "g" + (opts.pulse ? " pulse" : ""), text: GLYPHS[opts.kind] || GLYPHS.pending }));
        children.push(" ");
      }
      children.push(span("k", label));
      children.push(" ");
      if (value === null || value === undefined) children.push(span("v", "—"));
      else if (typeof value === "string" || typeof value === "number") children.push(span("v", String(value)));
      else children.push(h("span", { "class": "v" }, [value]));
      var attrs = { "class": cls };
      if (opts.title) attrs.title = opts.title;
      return h("div", attrs, children);
    }

    /** Counts with their meaning attached (console-6). Order is the vocab's;
     * anything the vocab does not know lands last rather than being dropped. */
    function tallyRows(counts, vocabName, opts) {
      opts = opts || {};
      counts = counts || {};
      var keys = Object.keys(counts);
      keys.sort(function (a, b) {
        var ea = vocabEntry(vocabName, a), eb = vocabEntry(vocabName, b);
        return ea.order - eb.order || (a < b ? -1 : a > b ? 1 : 0);
      });
      var rows = keys.map(function (key) {
        var entry = vocabEntry(vocabName, key);
        var attrs = { "class": "tally-row status--" + entry.kind };
        if (typeof opts.onFilter === "function") attrs.onclick = function () { opts.onFilter(key); };
        return h("div", attrs, [
          h("span", { "class": "g", text: GLYPHS[entry.kind] || GLYPHS.pending }),
          " ",
          span("k", entry.label),
          " ",
          span("n", String(counts[key]))
        ]);
      });
      if (!rows.length) rows = [line("empty", opts.emptyText || "nothing counted yet")];
      return h("div", { "class": "tally-rows" }, rows);
    }

    /** A meter on the one 0-100 ramp, flat by design, with a tick at whatever
     * this particular meter's trigger is. */
    function meterRow(label, pct, valueText, opts) {
      opts = opts || {};
      var p = Math.max(0, Math.min(100, typeof pct === "number" && !isNaN(pct) ? pct : 0));
      var fillCls = "meter__fill" + (opts.crit ? " meter__fill--crit" : opts.warn ? " meter__fill--warn" : opts.live ? " meter__fill--live" : "");
      var trackChildren = [h("div", { "class": fillCls, style: "width: " + p + "%" })];
      if (typeof opts.tick === "number" && !isNaN(opts.tick)) {
        trackChildren.push(h("div", {
          "class": "meter__tick", style: "left: " + Math.max(0, Math.min(100, opts.tick)) + "%"
        }));
      }
      return div("meter-block", [
        div("meter-row", [span("k", label), h("span", { "class": "v" }, isArray(valueText) ? valueText : [String(valueText)])]),
        h("div", { "class": "meter" + (opts.thin ? " meter--thin" : "") }, trackChildren)
      ]);
    }

    function callout(tier, headText, bodyNodes) {
      var cls = tier === "crit" ? "callout--crit" : tier === "warn" ? "warn-callout" : "callout--neutral";
      var children = [h("div", { "class": "head", text: headText })];
      (bodyNodes || []).forEach(function (n) {
        if (n === null || n === undefined) return;
        children.push(typeof n === "string" ? h("div", { "class": "body", text: n }) : n);
      });
      return h("div", { "class": cls }, children);
    }

    function disclosure(summaryText, bodyNode, opts) {
      opts = opts || {};
      var attrs = { "class": "disclosure" + (opts.className ? " " + opts.className : "") };
      if (opts.open) attrs.open = "open";
      return h("details", attrs, [
        h("summary", { "class": "disclosure__summary", text: summaryText }),
        bodyNode
      ]);
    }

    /** One `key: value` line per remaining field. Objects and arrays are
     * printed compactly -- this is the FALLBACK surface (ext panels, a panel
     * whose builder raised), where showing the payload is the whole point
     * (M-P-2), not a Console card cell. */
    function payloadLines(payload, skipKeys) {
      var skip = {};
      (skipKeys || []).forEach(function (k) { skip[k] = true; });
      var out = [];
      Object.keys(payload || {}).forEach(function (key) {
        if (skip[key]) return;
        var value = payload[key];
        var text;
        if (value === null || value === undefined) text = "—";
        else if (typeof value === "object") {
          try { text = JSON.stringify(value); } catch (e) { text = String(value); }
        } else text = String(value);
        out.push(div("kv-line", [span("k", key), " ", span("v", text)]));
      });
      return out;
    }

    /** Tiered by status (console-8): an expected empty state is neutral, an
     * error is a warning, a broken design invariant is the crit band -- and
     * whatever else the payload carries is rendered underneath instead of
     * being thrown away. */
    function statusCallout(panel) {
      panel = panel || {};
      var tier = STATUS_TIERS[panel.status] || "neutral";
      var glyph = tier === "crit" ? GLYPHS.crit : tier === "warn" ? GLYPHS.warn : GLYPHS.pending;
      var head = glyph + " " + String(panel.status || "unknown").toUpperCase().replace(/_/g, " ");
      var body = [];
      if (panel.message) body.push(String(panel.message));
      var rest = payloadLines(panel, ["status", "message", "panel"]);
      if (rest.length) body.push(h("div", { "class": "callout-payload" }, rest));
      return callout(tier, head, body);
    }

    function disabledBtn(label, reason) {
      return h("button", {
        type: "button", "class": "btn btn--secondary", disabled: "disabled",
        "aria-disabled": "true", title: reason, text: label
      });
    }

    function actionBtn(label, onClick, opts) {
      opts = opts || {};
      if (typeof onClick !== "function") return disabledBtn(label, opts.disabledReason || "no handler wired");
      var attrs = { type: "button", "class": "btn " + (opts.className || "btn--secondary"), text: label, onclick: onClick };
      if (opts.title) attrs.title = opts.title;
      return h("button", attrs);
    }

    function truncationNote(shown, total, what) {
      if (!(total > shown)) return null;
      return line("trunc-note", (total - shown) + " more " + (what || "not shown"));
    }

    function nowMsOf(opts) {
      return typeof (opts || {}).nowMs === "number" ? opts.nowMs : nowFn();
    }

    /* ====================================================================
       CARD 1 -- SESSION (sweep section 3.1)
       ==================================================================== */

    function sessionHead(panel) {
      panel = panel || {};
      if (panel.status === "invariant_violation") return statusNode("crit", "INVARIANT VIOLATION");
      if (panel.status !== "ok") return statusNode("pending", String(panel.status || "UNKNOWN").toUpperCase().replace(/_/g, " "));
      return panel.open_session ? statusNode("settled", "ONE OPEN") : statusNode("pending", "NONE OPEN");
    }

    function renderSessionCard(panel, opts) {
      opts = opts || {};
      var nowMs = nowMsOf(opts);
      panel = panel || {};
      var body = div("card-stack");
      if (panel.status !== "ok") { body.appendChild(statusCallout(panel)); return body; }

      var open = panel.open_session;
      if (!open) {
        body.appendChild(callout("neutral", GLYPHS.pending + " NONE OPEN", [
          "open one with `trialerror session open`"
        ]));
        body.appendChild(sessionHistory(panel.recent_sessions));
        return body;
      }

      // 1. Close-readiness FIRST -- this is the card's reason to exist. The
      // answer to "why can't I close?" is a plain-English string the backend
      // already computed, and it used to render last, three levels deep.
      var readiness = open.close_readiness || {};
      var dangling = readiness.dangling_launches || [];
      if (readiness.ready) {
        body.appendChild(readingRow("READY TO CLOSE", "", { kind: "settled" }));
      } else {
        body.appendChild(callout("warn", GLYPHS.warn + " " + dangling.length + " IN FLIGHT",
          (readiness.problems || []).map(function (p) { return typeof p === "string" ? p : String(p); })));
        dangling.forEach(function (l) { body.appendChild(danglingRow(l, nowMs)); });
      }
      var pin = readiness.pin_check;
      if (pin && pin.valid === false) {
        body.appendChild(line("crit-line status--crit", "law pin: " + (pin.reason || "stale")));
      }

      // 2. Readings grid.
      var stats = open.boot_bundle_stats || {};
      var grid = div("reading-grid");
      grid.appendChild(readingRow("ID", shortId(open.session_id), { title: open.session_id }));
      grid.appendChild(readingRow("OPEN", h("span", {
        "class": "ago", "data-ts": String(open.opened_ts || ""), "data-fmt": "elapsed",
        text: fmtElapsedClock(open.opened_ts, nowMs), title: String(open.opened_ts || "")
      })));
      grid.appendChild(readingRow("ACCOUNT", shortId(open.account_id), { title: open.account_id }));
      if (stats.boot_pin_version) {
        var pinValid = !pin || pin.valid !== false;
        grid.appendChild(readingRow("BOOT PIN", stats.boot_pin_version + (pinValid ? " FRESH" : " STALE"),
          { kind: pinValid ? "settled" : "crit" }));
      } else {
        // M-CON-5b: null and absent both used to render an em dash. A session
        // with no law pin is a law-pin GAP, which is a warning, not a blank.
        grid.appendChild(readingRow("BOOT PIN", "NO PIN", { kind: "warn", title: "this session booted without a law pin" }));
      }
      grid.appendChild(readingRow("BUNDLE", stats.boot_bundle_sha ? String(stats.boot_bundle_sha).slice(0, 12) : "—",
        { title: stats.boot_bundle_sha || "no boot bundle recorded" }));
      var queue = jsonish(stats.queue);
      grid.appendChild(readingRow("QUEUE", (isArray(queue) ? queue.length : 0) + " queued"));
      grid.appendChild(readingRow("INBOX", (open.unread_inbox_count || 0) + " UNREAD",
        { kind: (open.unread_inbox_count || 0) > 0 ? "warn" : "settled" }));
      grid.appendChild(readingRow("HOOKS", String(open.hook_alive_count || 0),
        { kind: (open.hook_alive_count || 0) > 0 ? "settled" : "warn",
          title: (open.hook_alive_count || 0) > 0 ? "" : "no hook_alive events under this session" }));
      grid.appendChild(readingRow("ACTIVE JOBS", String(open.active_jobs_count || 0)));
      body.appendChild(grid);

      // 3. Actions -- drawn, disabled, and saying why (12.11).
      body.appendChild(div("btn-row", [
        disabledBtn("CLOSE SESSION", "session close is a CLI ritual with a course-check -- not a dashboard write in this build"),
        disabledBtn("RENDER HANDOFF", "the handoff renderer is a CLI verb; the dashboard has no callable for it")
      ]));

      body.appendChild(sessionHistory(panel.recent_sessions));
      return body;
    }

    function danglingRow(launch, nowMs) {
      var kind = launch.state === "RUNNING" ? "live" : "warn";
      var stateLabel = launch.state === "PROVISIONAL" ? "BOOKED, NOT YET SPAWNED" : String(launch.state || "");
      var purpose = String(launch.purpose || "");
      return div("dangling-row", [
        span("agent", String(launch.agent_kind || "—")),
        h("span", { "class": "purpose", text: purpose.length > 72 ? purpose.slice(0, 71) + "…" : purpose, title: purpose }),
        statusNode(kind, stateLabel, { pulse: kind === "live" }),
        span("k", "booked"),
        fmtAgo(launch.booked_ts, { nowMs: nowMs }),
        h("span", { "class": "v", text: fmtCompact(launch.est_tokens), title: String(launch.est_tokens) })
      ]);
    }

    function sessionHistory(recent) {
      recent = recent || [];
      var body = div("history-list", recent.map(function (s) {
        return div("history-row", [
          h("span", { "class": "id", text: shortId(s.session_id), title: s.session_id }),
          statusNode(s.status === "open" ? "live" : "settled", String(s.status || "").toUpperCase()),
          span("v", String(s.opened_ts || "").slice(0, 10) + " → " + (s.closed_ts ? String(s.closed_ts).slice(0, 10) : "open"))
        ]);
      }));
      if (!recent.length) body.appendChild(line("empty", "no sessions recorded yet"));
      return disclosure("HISTORY (" + recent.length + ")", body);
    }

    /* ====================================================================
       CARD 2 -- POOLS (sweep section 3.2)
       ==================================================================== */

    var WINDOW_LABELS = {
      five_hour: "SESSION, 5H WINDOW",
      seven_day: "WEEKLY, ALL MODELS"
    };

    function windowLabel(key) {
      if (WINDOW_LABELS[key]) return WINDOW_LABELS[key];
      if (String(key).indexOf("seven_day") === 0) return "WEEKLY, TOP TIER";
      return String(key).toUpperCase().replace(/_/g, " ");
    }

    function renderPoolsCard(panel, opts) {
      opts = opts || {};
      var nowMs = nowMsOf(opts);
      panel = panel || {};
      var body = div("card-stack");
      if (panel.status !== "ok") { body.appendChild(statusCallout(panel)); return body; }

      // 1. Quota freshness FIRST (console-10): it is the trust flag for every
      // other number on this card, and it used to render last.
      var quota = panel.plan_quota || {};
      if (!quota.available) {
        body.appendChild(callout("neutral", GLYPHS.pending + " NO PLAN QUOTA CAPTURED",
          [String(quota.note || "no statusline quota has been captured for this account")]));
      } else if (!quota.fresh) {
        body.appendChild(callout("warn", GLYPHS.warn + " QUOTA STALE",
          ["captured " + fmtAgoText(quota.captured_ts, nowMs) +
           (typeof quota.age_s === "number" ? " (" + fmtDuration(quota.age_s) + " old)" : "")]));
      } else {
        body.appendChild(line("quota-fresh", "quota as of " + fmtAgoText(quota.captured_ts, nowMs) +
          (quota.model ? " · " + quota.model : "")));
      }

      // 2. Plan-window meters.
      var windows = quota.windows || {};
      Object.keys(windows).forEach(function (key) {
        var win = windows[key] || {};
        var pct = typeof win.used_percentage === "number" ? win.used_percentage : 0;
        var value = [Math.round(pct) + "% · resets " + fmtAgoText(win.resets_at, nowMs)];
        if (pct >= 95) value.push(chip("AT TRIGGER", "crit"));
        body.appendChild(meterRow(windowLabel(key), pct, value, { tick: 95, warn: pct >= 80, crit: pct >= 95 }));
      });

      // 3. Pools, per account.
      var accounts = panel.accounts || [];
      if (!accounts.length) {
        body.appendChild(readingRow("NO ACCOUNTS", "", { kind: "pending" }));
        return body;
      }
      accounts.forEach(function (entry) {
        var account = entry.account || {};
        var status = entry.budget_status || {};
        var pools = status.pools || [];
        if (accounts.length > 1) {
          body.appendChild(line("section-sub", String(account.label || account.account_id || "")));
        }
        if (!pools.length) {
          body.appendChild(line("empty", "no pools configured for " + (account.label || shortId(account.account_id))));
        }
        pools.forEach(function (pool) {
          var cap = pool.cap_tokens || 0;
          var projected = pool.projected_billed_tokens || 0;
          var pct = cap > 0 ? (projected / cap) * 100 : 0;
          var value = [fmtCompact(projected) + " / " + fmtCompact(cap) + " · headroom " + fmtCompact(pool.headroom_tokens)];
          if (pool.over_hard) value.push(chip("OVER HARD", "crit"));
          body.appendChild(meterRow(
            String(pool.model_class) + " · " + String(pool.period), pct, value,
            { tick: cap > 0 ? (pool.soft_cap / cap) * 100 : undefined, warn: !!pool.over_soft, crit: !!pool.over_hard }
          ));
          body.appendChild(line("meter-sub",
            "spent " + fmtCompact(pool.spent_visible_tokens) +
            " · committed " + fmtCompact(pool.committed_visible_tokens) +
            " · ×" + String(pool.billed_multiplier)));
        });
        (status.defer_advisories || []).forEach(function (adv) {
          body.appendChild(readingRow("DEFER " + String(adv.model_class), String(adv.reason || ""), { kind: "warn" }));
        });
      });
      return body;
    }

    /* ====================================================================
       CARD 3 -- LAUNCH LEDGER (sweep section 3.3)
       ==================================================================== */

    function renderLedgerCard(panel, opts) {
      opts = opts || {};
      var nowMs = nowMsOf(opts);
      panel = panel || {};
      var body = div("card-stack");
      if (panel.status !== "ok") { body.appendChild(statusCallout(panel)); return body; }

      var counts = panel.launch_state_counts_total || {};
      var dangling = panel.dangling_bookings || [];
      var running = counts.RUNNING || 0;
      var provisional = counts.PROVISIONAL || 0;
      var reconciled = counts.RECONCILED || 0;

      // Fixed order, zeros printed. "A dangling booking is the one thing that
      // blocks a close; zero is a reading" (the canvas's own footnote).
      body.appendChild(readingRow("RUNNING", running, { kind: running ? "live" : "pending", pulse: !!running }));
      body.appendChild(readingRow("BOOKED, NOT YET SPAWNED", provisional, { kind: provisional ? "warn" : "settled" }));
      body.appendChild(readingRow("RECONCILED", reconciled, { kind: "settled" }));
      body.appendChild(readingRow("DANGLING", dangling.length, {
        kind: dangling.length ? "crit" : "settled",
        title: "a dangling booking is the one thing that blocks a close; zero is a reading"
      }));
      ["ABANDONED", "REFUSED", "DEFERRED"].forEach(function (state) {
        if (counts[state]) body.appendChild(readingRow(state, counts[state], { kind: "pending" }));
      });

      dangling.forEach(function (l) {
        var purpose = String(l.purpose || "");
        body.appendChild(div("dangling-row", [
          h("span", { "class": "id", text: shortId(l.launch_id), title: l.launch_id }),
          span("agent", String(l.agent_kind || "")),
          h("span", { "class": "purpose", text: purpose.length > 60 ? purpose.slice(0, 59) + "…" : purpose, title: purpose }),
          span("k", "booked"),
          fmtAgo(l.booked_ts, { nowMs: nowMs }),
          span("v", "ttl " + fmtDuration(l.booking_ttl_s)),
          span("v", String(l.state || ""))
        ]));
      });

      var accounts = panel.accounts || [];
      if (accounts.length > 1) {
        accounts.forEach(function (entry) {
          var account = entry.account || {};
          body.appendChild(line("section-sub", String(account.label || account.account_id || "")));
          body.appendChild(tallyRows(entry.launch_state_counts, "LAUNCH_STATE"));
        });
      }
      return body;
    }

    function ledgerHead(panel) {
      panel = panel || {};
      if (panel.status !== "ok") return null;
      var counts = panel.launch_state_counts_total || {};
      var running = counts.RUNNING || 0;
      if (running) return statusNode("live", running + " RUNNING", { pulse: true });
      var total = 0;
      Object.keys(counts).forEach(function (k) { total += counts[k]; });
      return statusNode(total && (counts.RECONCILED || 0) === total ? "settled" : "pending",
        (counts.RECONCILED || 0) + " / " + total + " RECONCILED");
    }

    /* ====================================================================
       CARD 4 -- THIS SESSION, END TO END (sweep section 3.4)
       ==================================================================== */

    var BAR_CLASS = {
      running: "bar--running", complete: "bar--complete", retried: "bar--retried",
      booked: "bar--booked", failed: "bar--failed", abandoned: "bar--abandoned",
      frozen: "bar--frozen"
    };

    function pctOf(x, width) {
      if (!width) return 0;
      return Math.round((x / width) * 10000) / 100;
    }

    function renderTimelineCard(timeline, opts) {
      opts = opts || {};
      var nowMs = nowMsOf(opts);
      var body = div("card-stack timeline");
      if (!timeline) {
        body.appendChild(line("empty", "no open session — the timeline starts at the next `trialerror session open`"));
        return body;
      }
      var spans = timeline.spans || [];
      var instants = timeline.instants || [];
      var layout = compressIdleGaps(timeline.window || {}, spans, instants, { nowMs: nowMs });

      body.appendChild(timelineAxis(layout));

      var lanes = [];
      var byLane = {};
      function laneOf(name) {
        if (!Object.prototype.hasOwnProperty.call(byLane, name)) {
          byLane[name] = { name: name, spans: [], instants: [] };
          lanes.push(byLane[name]);
        }
        return byLane[name];
      }
      spans.forEach(function (s) { laneOf(String(s.lane || s.kind || "—")).spans.push(s); });
      instants.forEach(function (i) { laneOf(String(i.lane || i.kind || "—")).instants.push(i); });

      if (!lanes.length) {
        body.appendChild(line("empty",
          "session open " + fmtDuration((nowMs - layout.t0) / 1000) + " · nothing launched yet"));
        body.appendChild(timelineLegend());
        return body;
      }

      var shown = lanes.slice(0, MAX_LANES);
      shown.forEach(function (lane) { body.appendChild(timelineLane(lane, layout, nowMs, opts)); });
      var note = truncationNote(shown.length, lanes.length, "lanes not shown");
      if (note) body.appendChild(note);
      body.appendChild(timelineLegend());

      var dropped = (timeline.truncated || {}).spans_dropped || 0;
      if (dropped) body.appendChild(line("trunc-note", dropped + " earlier spans not shown"));
      return body;
    }

    function timelineAxis(layout) {
      var children = [];
      layout.ticks.forEach(function (tick) {
        children.push(h("span", {
          "class": "axis-tick", style: "left: " + pctOf(tick.x, layout.width) + "%", text: tick.label
        }));
      });
      layout.segments.forEach(function (seg) {
        if (!seg.collapsed) return;
        children.push(h("span", {
          "class": "gap-marker", style: "left: " + pctOf(seg.x0, layout.width) + "%",
          text: "║ " + seg.label, title: "collapsed idle: " + seg.label
        }));
      });
      return h("div", { "class": "timeline-axis" }, children);
    }

    function refText(ref) {
      if (!ref) return "";
      var keys = Object.keys(ref);
      if (!keys.length) return "";
      return keys[0] + "=" + String(ref[keys[0]]);
    }

    function timelineLane(lane, layout, nowMs, opts) {
      var bars = [];
      lane.spans.forEach(function (s) {
        var a = parseTs(s.start_ts);
        if (isNaN(a)) return;
        var b = parseTs(s.end_ts);
        if (isNaN(b)) b = nowMs;
        var x0 = timelineX(layout, a), x1 = timelineX(layout, b);
        var w = Math.max(1, x1 - x0);
        bars.push(hs("rect", {
          "class": "bar " + (BAR_CLASS[s.status] || "bar--booked") + (s.status === "running" ? " pulse" : ""),
          x: String(Math.round(x0 * 100) / 100), y: "2",
          width: String(Math.round(w * 100) / 100), height: "8",
          "data-ref": refText(s.ref), "data-span": String(s.id || "")
        }));
      });
      lane.instants.forEach(function (i) {
        var t = parseTs(i.ts);
        if (isNaN(t)) return;
        bars.push(hs("rect", {
          "class": "bar bar--instant", x: String(Math.round(timelineX(layout, t) * 100) / 100), y: "0",
          width: "2", height: "12", "data-instant": String(i.kind || "")
        }));
      });
      var svg = hs("svg", {
        "class": "lane-bars", viewBox: "0 0 " + layout.width + " 12", preserveAspectRatio: "none"
      }, bars);

      var first = lane.spans[0] || lane.instants[0] || {};
      var onRef = (opts || {}).onRef;
      var nameNode = (typeof onRef === "function" && first.ref && Object.keys(first.ref).length)
        ? rowButton(h, lane.name, { className: "lane-name", title: lane.name, onClick: function () { onRef(first.ref); } })
        : h("span", { "class": "lane-name", text: lane.name, title: lane.name });
      return h("div", { "class": "lane" }, [nameNode, svg]);
    }

    function timelineLegend() {
      return div("timeline-legend", [
        statusNode("live", "RUNNING NOW"),
        statusNode("settled", "COMPLETED"),
        statusNode("warn", "RETRIED"),
        h("span", { "class": "status status--pending" }, [h("span", { "class": "g", text: "║" }), " COLLAPSED IDLE"])
      ]);
    }

    /* ====================================================================
       CARD 5 -- JOBS (sweep section 3.5, the k9s two-layer colorer)
       ==================================================================== */

    var JOBS_COLUMNS = ["ID", "KIND AND SUBJECT", "STATE", "HEARTBEAT", "LEASE / DURATION", "PROGRESS", "Δ"];

    var JOB_ORDER_RANK = {
      running: 0, claimed: 0, pending: 3, paused: 4, failed: 5, abandoned: 6, complete: 7
    };

    function isAwaitingGpu(job) {
      return job.state === "pending" && typeof job.last_error === "string" &&
        job.last_error.indexOf("awaiting DEV GPU") === 0;
    }

    function jobRank(job) {
      if (isAwaitingGpu(job)) return 1;
      if (job.state === "pending" && job.next_attempt_ts && job.failure_class) return 2;
      var rank = JOB_ORDER_RANK[job.state];
      return typeof rank === "number" ? rank : 8;
    }

    /** Layer 1 is the ledger state; layer 2 overrides it from domain facts, in
     * the priority order the sweep sets. Without layer 2 a claimed job whose
     * lease expired twenty minutes ago reads exactly like a healthy one. */
    function jobStateCell(job, nowMs) {
      var live = job.state === "running" || job.state === "claimed";
      var leaseMs = parseTs(job.lease_expires_ts);
      if (live && !isNaN(leaseMs) && leaseMs < nowMs) return statusNode("crit", "LEASE EXPIRED");
      var hbMs = parseTs(job.heartbeat_ts);
      if (live && !isNaN(hbMs) && (nowMs - hbMs) / 1000 > HEARTBEAT_LATE_S) return statusNode("warn", "HEARTBEAT LATE");
      if (isAwaitingGpu(job)) return statusNode("paused", "AWAITING DEV GPU");
      if (job.state === "pending" && job.next_attempt_ts && job.failure_class) {
        var backoff = parseTs(job.next_attempt_ts);
        var suffix = isNaN(backoff) ? "" : " · backoff " + fmtDuration(Math.max(0, (backoff - nowMs) / 1000));
        return statusNode("warn", "RETRY " + (job.attempts || 0) + "/" + (job.max_attempts || 0) + suffix);
      }
      if (job.state === "complete") {
        return statusNode("settled", (job.attempts || 0) > 1 ? "DONE · " + ((job.attempts || 1) - 1) + " retries" : "DONE");
      }
      if (job.state === "failed") return statusNode("warn", "FAILED " + (job.attempts || 0) + "/" + (job.max_attempts || 0));
      if (job.state === "abandoned") return statusNode("crit", "ABANDONED");
      if (live) return statusNode("live", String(job.state).toUpperCase(), { pulse: true });
      if (job.state === "paused") return statusNode("paused", "PAUSED");
      return statusNode("pending", String(job.state || "—").toUpperCase());
    }

    var CHECKPOINT_KEYS = [
      ["rows_ingested", " rows"], ["records_seen_in_current_member", " records"],
      ["current_member", ""]
    ];

    /** Never a raw JSON string in a cell (M-CON-3). A checkpoint with a
     * numeric progress becomes a thin meter; anything else becomes the
     * well-known keys, compactly; an unrecognised object becomes its key
     * count, which is a reading rather than a wall. */
    function jobProgressCell(job, offload, nowMs) {
      var cell = div("progress-cell");
      if (isAwaitingGpu(job)) {
        var entry = ((offload || {}).jobs || {})[job.job_id];
        if (!entry) cell.appendChild(line("v", "offload: not yet queued"));
        else if (entry.state === "claimed") {
          cell.appendChild(h("div", { "class": "v" }, [
            "claimed by " + String(entry.worker_id || "a worker") + " · hb ",
            fmtAgo(entry.heartbeat_ts, { nowMs: nowMs })
          ]));
        } else if (entry.state === "failed") {
          cell.appendChild(line("v status--crit", "offload: failed " + (entry.offload_attempts || 0) + "/3"));
        } else {
          cell.appendChild(line("v", "offload: " + String(entry.state)));
        }
        return cell;
      }

      var cp = jsonish(job.checkpoint);
      if (cp === null || cp === undefined || cp === "") {
        if (job.state === "failed" || job.state === "abandoned") { cell.appendChild(jobErrorDisclosure(job)); return cell; }
        cell.appendChild(line("v", "—"));
        return cell;
      }
      if (typeof cp !== "object") { cell.appendChild(line("v wrap", String(cp))); return cell; }

      if (typeof cp.progress_pct === "number") {
        cell.appendChild(meterRow("", cp.progress_pct, Math.round(cp.progress_pct) + "%", { thin: true }));
      } else if (typeof cp.done === "number" && typeof cp.total === "number" && cp.total > 0) {
        cell.appendChild(meterRow("", (cp.done / cp.total) * 100, cp.done + " / " + cp.total, { thin: true }));
      } else {
        var parts = [];
        CHECKPOINT_KEYS.forEach(function (spec) {
          var value = cp[spec[0]];
          if (value === undefined || value === null) return;
          parts.push((typeof value === "number" ? fmtCompact(value) : String(value)) + spec[1]);
        });
        if (isArray(cp.members_done)) parts.push(cp.members_done.length + " members done");
        if (!parts.length) parts.push(Object.keys(cp).length + " checkpoint field(s)");
        if (cp.updated_ts) {
          cell.appendChild(h("div", { "class": "v" }, [parts.join(" · ") + " · ", fmtAgo(cp.updated_ts, { nowMs: nowMs })]));
        } else {
          cell.appendChild(line("v", parts.join(" · ")));
        }
      }
      if (job.state === "failed" || job.state === "abandoned") cell.appendChild(jobErrorDisclosure(job));
      return cell;
    }

    function jobErrorDisclosure(job) {
      var first = String(job.last_error || "no error text recorded").split("\n")[0];
      var body = div("error-body", [
        line("wrap", first),
        job.failure_class ? chip(String(job.failure_class).toUpperCase(), "warn") : null
      ]);
      return disclosure("▸ error", body);
    }

    function td(name, children, delta) {
      var cls = "cell-" + name + ((delta.cells || {})[name] ? " cell-changed" : "");
      return h("td", { "class": cls }, children);
    }

    function jobRow(job, snapshot, offload, nowMs) {
      var delta = jobDelta(job, snapshot);
      var live = job.state === "running" || job.state === "claimed";
      var leaseMs = parseTs(job.lease_expires_ts);
      var hbMs = parseTs(job.heartbeat_ts);

      var heartbeat;
      if (!live || isNaN(hbMs)) heartbeat = span("v", "—");
      else {
        var kind = (!isNaN(leaseMs) && leaseMs < nowMs) ? "crit"
          : ((nowMs - hbMs) / 1000 > HEARTBEAT_LATE_S ? "warn" : "settled");
        heartbeat = h("span", { "class": "status status--" + kind }, [fmtAgo(job.heartbeat_ts, { nowMs: nowMs })]);
      }

      var lease;
      if (live && !isNaN(leaseMs)) {
        lease = leaseMs < nowMs
          ? statusNode("crit", "expired " + fmtDuration((nowMs - leaseMs) / 1000) + " ago")
          : span("v", "lease ends in " + fmtDuration((leaseMs - nowMs) / 1000));
      } else if (typeof job.duration_s === "number") {
        lease = span("v", fmtDuration(job.duration_s));
      } else {
        lease = span("v", "—");
      }

      var subject = job.subject ? String(job.kind) + " · " + String(job.subject) : String(job.kind || "—");
      return h("tr", { "class": "job-row" + (delta.isNew ? " is-new" : "") }, [
        td("id", [h("span", { "class": "id", text: shortId(job.job_id), title: job.job_id })], delta),
        td("subject", [h("span", { "class": "v", text: subject, title: subject })], delta),
        td("state", [jobStateCell(job, nowMs)], delta),
        td("heartbeat", [heartbeat], delta),
        td("lease", [lease], delta),
        td("progress", [jobProgressCell(job, offload, nowMs)], delta),
        td("delta", [span("delta-marker", delta.marker)], delta)
      ]);
    }

    function renderJobsCard(panel, opts) {
      opts = opts || {};
      var nowMs = nowMsOf(opts);
      panel = panel || {};
      var body = div("card-stack");
      if (panel.status !== "ok") { body.appendChild(statusCallout(panel)); return body; }

      var counts = panel.state_counts || {};
      var offload = panel.offload || {};
      var rows = (panel.recent_jobs || []).slice();

      if (offload.awaiting) {
        // LANE0 section 4's own reading, minus the host name: nothing in a
        // shipped string names a particular machine (C-0078).
        body.appendChild(callout("warn", GLYPHS.warn + " " + offload.awaiting + " AWAITING DEV GPU",
          [offload.awaiting + " job" + (offload.awaiting === 1 ? "" : "s") +
           " wait" + (offload.awaiting === 1 ? "s" : "") + " for the DEV GPU — run the GPU worker"]));
      }

      // The state tally always prints, zeros included -- an empty ledger is a
      // reading ("0 RUNNING"), not a blank card.
      var running = (counts.running || 0) + (counts.claimed || 0);
      body.appendChild(div("tally-row-inline", [
        span("v", running + " RUNNING"), span("k", "·"),
        span("v", (counts.pending || 0) + " PENDING"), span("k", "·"),
        span("v", (counts.complete || 0) + " COMPLETE"), span("k", "·"),
        span("v", (counts.failed || 0) + " FAILED")
      ]));

      if (!rows.length) {
        body.appendChild(line("empty", "no jobs in the ledger yet — `trialerror jobs enqueue` or an ingest starts one"));
        return body;
      }

      rows.sort(function (a, b) {
        var d = jobRank(a) - jobRank(b);
        if (d) return d;
        var sa = String(a.settled_ts || a.created_ts || ""), sb = String(b.settled_ts || b.created_ts || "");
        return sa < sb ? 1 : sa > sb ? -1 : 0;
      });

      var snapshot = Object.prototype.hasOwnProperty.call(opts, "snapshot") ? opts.snapshot : TEConsole.jobsSnapshot;
      var table = h("table", { "class": "rows-table" }, [
        h("thead", {}, [h("tr", {}, JOBS_COLUMNS.map(function (c) { return h("th", { text: c }); }))]),
        h("tbody", {}, rows.map(function (job) { return jobRow(job, snapshot, offload, nowMs); }))
      ]);
      body.appendChild(h("div", { "class": "table-scroll" }, [table]));

      var total = 0;
      Object.keys(counts).forEach(function (k) { total += counts[k]; });
      var note = truncationNote(rows.length, total, "not shown");
      if (note) body.appendChild(note);

      body.appendChild(div("delta-legend", [
        span("k", "↑ STARTED SINCE YOUR LAST LOOK"),
        span("k", "Δ CHANGED"),
        span("k", "↓ FINISHED")
      ]));
      body.appendChild(line("note-strip", "HEARTBEAT AND LEASE ARE SEPARATE FACTS"));

      if (!opts.keepSnapshot) TEConsole.jobsSnapshot = jobsSnapshotOf(panel);
      return body;
    }

    function jobsHead(panel) {
      panel = panel || {};
      if (panel.status !== "ok") return null;
      var counts = panel.state_counts || {};
      var offload = panel.offload || {};
      var running = (counts.running || 0) + (counts.claimed || 0);
      var stale = (panel.stale_leases || []).length;
      var children = [statusNode(running ? "live" : "pending", running + " RUNNING", { pulse: !!running })];
      if (offload.awaiting) children.push(chip(offload.awaiting + " AWAITING DEV GPU", "warn"));
      children.push(statusNode(stale ? "crit" : "settled", "STALE LEASES " + stale));
      return h("span", { "class": "head-readings" }, children);
    }

    /* ====================================================================
       CARD 6 -- DIAGNOSTICS (sweep section 3.6)
       ==================================================================== */

    var CHECK_KIND = { fail: "crit", warn: "warn", skip: "pending", pass: "settled" };
    var CHECK_ORDER = { fail: 0, warn: 1, skip: 2, pass: 3 };

    /** A check's `details` is free-form, but three list shapes recur
     * (offenders / jobs / mismatched_sessions). A list is printed as its
     * count plus the first three entries -- the whole list belongs in the
     * CLI, and a card that tries to hold it holds nothing else. */
    function detailLines(details) {
      return Object.keys(details).map(function (key) {
        var value = details[key];
        var text;
        if (isArray(value)) {
          var firstThree = value.slice(0, 3).map(function (v) {
            return typeof v === "object" ? JSON.stringify(v) : String(v);
          });
          text = value.length + (value.length > 3
            ? " (first 3: " + firstThree.join(", ") + ")"
            : (value.length ? ": " + firstThree.join(", ") : ""));
        } else if (value && typeof value === "object") {
          try { text = JSON.stringify(value); } catch (e) { text = String(value); }
        } else text = String(value);
        return div("kv-line", [span("k", key), " ", span("v", text)]);
      });
    }

    function checkRow(check) {
      var kind = CHECK_KIND[check.status] || "pending";
      var row = div("check-row status--" + kind, [
        div("check-head", [
          statusNode(kind, "", { ariaLabel: String(check.status || "unknown") }),
          span("name", String(check.name || "")),
          chip(String(check.category || "uncategorised"), "neutral"),
          span("msg", String(check.message || ""))
        ])
      ]);
      var details = check.details;
      if (details && typeof details === "object" && Object.keys(details).length) {
        row.appendChild(disclosure("▸ details", div("check-details", detailLines(details))));
      }
      return row;
    }

    function runSweepButton(opts) {
      return actionBtn("RUN THE CHECK SWEEP", (opts || {}).onRunDoctor, {
        disabledReason: "the sweep is a write action; this snapshot has no server to run it against"
      });
    }

    /** A tally entry that is also the filter for its own status (sweep
     * §3.6). Clicking the one already selected clears it, so there is no
     * separate "all" control to go looking for. Without a filter callback
     * -- a static snapshot -- it is a plain reading, not a dead button. */
    function tallyChip(kind, count, label, statusKey, filter, onFilter) {
      var selected = filter.status === statusKey;
      var node = statusNode(kind, count + " " + label);
      if (!onFilter) return node;
      return h("button", {
        type: "button",
        "class": "tally-chip" + (selected ? " is-selected" : ""),
        "aria-pressed": selected ? "true" : "false",
        title: selected ? "showing only " + label.toLowerCase() + " -- click to clear"
                        : "show only " + label.toLowerCase(),
        onclick: function () { onFilter({ status: selected ? null : statusKey, category: filter.category || "" }); }
      }, [node]);
    }

    function categoryFilterInput(filter, onFilter) {
      return h("input", {
        type: "text", "class": "write-input check-filter",
        placeholder: "filter by category",
        "aria-label": "filter diagnostics by category",
        value: filter.category || "",
        oninput: function (ev) {
          onFilter({ status: filter.status || null, category: (ev && ev.target ? ev.target.value : "") });
        }
      });
    }

    function matchesFilter(check, filter) {
      if (filter.status && check.status !== filter.status) return false;
      var text = String(filter.category || "").trim().toLowerCase();
      if (!text) return true;
      return String(check.category || "").toLowerCase().indexOf(text) !== -1
        || String(check.name || "").toLowerCase().indexOf(text) !== -1;
    }

    function renderDiagnosticsCard(panel, opts) {
      opts = opts || {};
      var nowMs = nowMsOf(opts);
      panel = panel || {};
      var body = div("card-stack");

      if (panel.status === "never_run") {
        // console-8: an expected empty state must not alarm. The sweep is
        // expensive and runs only when asked; saying so IS the reading.
        body.appendChild(callout("neutral", GLYPHS.pending + " NOT RUN YET",
          ["the sweep runs only when you ask, because it is expensive"]));
        body.appendChild(div("btn-row", [runSweepButton(opts)]));
        return body;
      }
      if (panel.status !== "ok") { body.appendChild(statusCallout(panel)); return body; }

      var run = panel.last_run || {};
      var checks = (run.checks || []).slice();
      var summary = run.summary || {};
      var filter = opts.doctorFilter || {};
      var onFilter = typeof opts.onDoctorFilter === "function" ? opts.onDoctorFilter : null;

      // The tally always reports the WHOLE run, filtered or not: a summary
      // that moves with the filter cannot be used to check the filter.
      body.appendChild(div("tally-row-inline", [
        tallyChip("crit", summary.failed || 0, "FAILED", "fail", filter, onFilter),
        tallyChip("warn", summary.warned || 0, "WARNED", "warn", filter, onFilter),
        tallyChip("pending", summary.skipped || 0, "SKIPPED", "skip", filter, onFilter),
        tallyChip("settled", summary.passed || 0, "PASSED", "pass", filter, onFilter)
      ]));
      if (onFilter) body.appendChild(categoryFilterInput(filter, onFilter));

      // Design ruling (this brief amends the canvas): FAIL leads. The canvas
      // text said "warn first, then fail"; a failure must never sit below a
      // long warn list.
      checks.sort(function (a, b) {
        var ra = CHECK_ORDER[a.status] === undefined ? 4 : CHECK_ORDER[a.status];
        var rb = CHECK_ORDER[b.status] === undefined ? 4 : CHECK_ORDER[b.status];
        if (ra !== rb) return ra - rb;
        return String(a.name) < String(b.name) ? -1 : String(a.name) > String(b.name) ? 1 : 0;
      });

      var kept = checks.filter(function (c) { return matchesFilter(c, filter); });
      if (kept.length !== checks.length) {
        body.appendChild(line("trunc-note",
          "showing " + kept.length + " of " + checks.length + " checks · filtered"));
      }
      var visible = [], passing = [];
      kept.forEach(function (c) { (c.status === "pass" ? passing : visible).push(c); });
      body.appendChild(div("check-list", visible.map(checkRow)));
      if (passing.length) {
        body.appendChild(disclosure("▸ " + passing.length + " PASS", div("check-list", passing.map(checkRow)),
          { open: filter.status === "pass" }));
      }
      body.appendChild(div("btn-row", [runSweepButton(opts)]));
      if (run.ran_ts) {
        body.appendChild(h("div", { "class": "note-strip" }, ["LAST RUN ", fmtAgo(run.ran_ts, { nowMs: nowMs })]));
      }
      return body;
    }

    function diagnosticsHead(panel) {
      panel = panel || {};
      if (panel.status !== "ok") return null;
      var checks = (panel.last_run || {}).checks || [];
      var categories = {};
      checks.forEach(function (c) { categories[c.category || "uncategorised"] = true; });
      return span("sub", checks.length + " CHECKS, " + Object.keys(categories).length + " CATEGORIES");
    }

    /* ====================================================================
       CARDS 7 + 8 -- GATES and CORPUS (compact readings)

       Neither is on the canvas -- S9 moved gates to Dossier/Determinations
       and corpus counts to Home -- but operators use both from here, so they
       stay as compact readings rather than being deleted out from under them.
       ==================================================================== */

    function renderGatesCard(panel, opts) {
      panel = panel || {};
      var body = div("card-stack");
      if (panel.status !== "ok") { body.appendChild(statusCallout(panel)); return body; }

      var pending = panel.pending_edits || [];
      var unverified = 0;
      pending.forEach(function (g) { unverified += (g.unverified_count || 0); });
      body.appendChild(readingRow("EDITS", unverified + " unverified", {
        kind: unverified ? "warn" : "settled",
        title: "an unverified edit blocks the union that would apply it"
      }));
      pending.forEach(function (g) {
        var title = String(g.title || g.artifact_id || "");
        body.appendChild(div("gate-row", [
          h("span", { "class": "id", text: shortId(g.gate_id), title: g.gate_id }),
          h("span", { "class": "v", text: title, title: title }),
          chip(String(g.state || "").toUpperCase(), g.state === "failed" ? "crit" : "neutral"),
          span("k", (g.unverified_count || 0) + " / " + (isArray(g.edits) ? g.edits.length : 0) + " unverified")
        ]));
      });
      body.appendChild(line("section-sub", "GATE STATE"));
      body.appendChild(tallyRows(panel.gate_state_counts, "GATE_STATE"));
      body.appendChild(line("section-sub", "VERDICT"));
      body.appendChild(tallyRows(panel.gate_verdict_counts, "GATE_VERDICT"));
      body.appendChild(line("section-sub", "REPRODUCTION"));
      body.appendChild(tallyRows(panel.reproduction_status_counts, "REPRO"));
      return body;
    }

    function renderCorpusCard(panel, opts) {
      panel = panel || {};
      var body = div("card-stack");
      if (panel.status !== "ok") { body.appendChild(statusCallout(panel)); return body; }

      var counts = panel.counts || {};
      var grid = div("reading-grid");
      ["sources", "documents", "chunks", "quote_anchors"].forEach(function (key) {
        grid.appendChild(readingRow(key.toUpperCase().replace(/_/g, " "), fmtCompact(counts[key] || 0),
          { title: String(counts[key] || 0) }));
      });
      body.appendChild(grid);

      var stale = panel.stale_anchors || 0;
      body.appendChild(readingRow("STALE ANCHORS", stale, {
        kind: stale ? "crit" : "settled",
        title: "an anchor whose document has been re-ingested no longer points at the text it quoted"
      }));

      var pendingRecords = (panel.extract_coverage || {}).pending_records || 0;
      body.appendChild(readingRow("EXTRACTION QUEUE", pendingRecords + " candidates await accept/reject",
        { kind: pendingRecords ? "warn" : "settled" }));

      var summary = panel.summary_coverage || {};
      var withSummary = summary.documents_with_current_summary || 0;
      var totalDocs = summary.total_documents || 0;
      body.appendChild(meterRow("SUMMARY COVERAGE", totalDocs ? (withSummary / totalDocs) * 100 : 0,
        withSummary + " / " + totalDocs, { thin: true }));

      body.appendChild(line("section-sub", "LICENSE TIER"));
      body.appendChild(tallyRows(panel.license_tier_counts, "LICENSE_TIER"));
      return body;
    }

    /* ---- registry -------------------------------------------------------- */

    function registerCard(name, fn) {
      if (CARD_ORDER.indexOf(name) === -1) {
        throw new RangeError("TEConsole.registerCard: unknown card " + JSON.stringify(name) +
          " -- add it to TEConsole.CARD_ORDER (and to the grid in dashboard.css) first");
      }
      if (typeof fn !== "function") {
        throw new TypeError("TEConsole.registerCard: " + name + " renderer must be a function(bundle, ctx)");
      }
      cards[name] = fn;
      return fn;
    }

    function panelsOf(bundle) { return (bundle || {}).panels || {}; }

    registerCard("session", function (bundle, ctx) {
      var panel = panelsOf(bundle).session;
      return { body: renderSessionCard(panel, ctx), head: sessionHead(panel) };
    });
    registerCard("pools", function (bundle, ctx) {
      return { body: renderPoolsCard(panelsOf(bundle).budget, ctx), head: span("sub", "TRIGGER AT 95%") };
    });
    registerCard("ledger", function (bundle, ctx) {
      var panel = panelsOf(bundle).budget;
      return { body: renderLedgerCard(panel, ctx), head: ledgerHead(panel) };
    });
    registerCard("timeline", function (bundle, ctx) {
      var session = panelsOf(bundle).session || {};
      return { body: renderTimelineCard((session.open_session || {}).timeline || null, ctx), head: null };
    });
    registerCard("jobs", function (bundle, ctx) {
      var panel = panelsOf(bundle).jobs;
      return { body: renderJobsCard(panel, ctx), head: jobsHead(panel) };
    });
    registerCard("diagnostics", function (bundle, ctx) {
      var panel = panelsOf(bundle).doctor;
      return { body: renderDiagnosticsCard(panel, ctx), head: diagnosticsHead(panel) };
    });
    registerCard("gates", function (bundle, ctx) {
      return { body: renderGatesCard(panelsOf(bundle).gates, ctx), head: null };
    });
    registerCard("corpus", function (bundle, ctx) {
      return { body: renderCorpusCard(panelsOf(bundle).corpus, ctx), head: null };
    });

    /** Paint every card that HAS a renderer into its container.
     *
     * `targets` is {cardName: element} or {cardName: {body, head}}; a name with
     * no element, or with no registered renderer, is skipped and its container
     * left exactly as it was. That is the whole reason the caller gets the
     * painted list back: whoever owns the page decides what to do with a card
     * this file does not draw.
     *
     * A renderer that returns null has drawn nothing on purpose; the container
     * is still cleared, because a stale render is worse than an empty card. */
    function renderInto(targets, bundle, opts) {
      var painted = [];
      if (!targets) return painted;
      CARD_ORDER.forEach(function (name) {
        var target = targets[name];
        var render = cards[name];
        if (!target || typeof render !== "function") return;
        var bodyEl = target.body || (target.nodeType ? target : null);
        var headEl = target.head || null;
        if (!bodyEl) return;
        clear(bodyEl);
        if (headEl) clear(headEl);
        var out = render(bundle, opts || {});
        if (out && out.nodeType) bodyEl.appendChild(out);
        else if (out) {
          if (out.body) bodyEl.appendChild(out.body);
          if (headEl && out.head) headEl.appendChild(out.head);
        }
        painted.push(name);
      });
      return painted;
    }

    var api = {
      VERSION: VERSION,
      CARD_ORDER: CARD_ORDER,
      h: h,
      hs: hs,
      clear: clear,
      cards: cards,
      registerCard: registerCard,
      renderInto: renderInto,
      h2: function (label, opts) { return h2(h, label, opts); },
      rowButton: function (content, opts) { return rowButton(h, content, opts); },

      // pure, reachable by name from the Node harness
      computeHealth: computeHealth,
      compressIdleGaps: compressIdleGaps,
      timelineX: timelineX,
      jobsSnapshotOf: jobsSnapshotOf,
      jobDelta: jobDelta,
      fmtAgoText: fmtAgoText,
      fmtDuration: fmtDuration,
      fmtCompact: fmtCompact,
      fmtTokens: fmtTokens,
      fmtClock: fmtClock,
      fmtElapsedClock: fmtElapsedClock,
      shortId: shortId,
      jsonish: jsonish,

      // node builders the page and the tests both use
      statusNode: statusNode,
      chip: chip,
      fmtAgo: fmtAgo,
      readingRow: readingRow,
      tallyRows: tallyRows,
      meterRow: meterRow,
      statusCallout: statusCallout,
      disclosure: disclosure,

      // one card at a time, for the harness -- bodies and the head slots
      sessionHead: sessionHead,
      ledgerHead: ledgerHead,
      jobsHead: jobsHead,
      diagnosticsHead: diagnosticsHead,
      renderSessionCard: renderSessionCard,
      renderPoolsCard: renderPoolsCard,
      renderLedgerCard: renderLedgerCard,
      renderTimelineCard: renderTimelineCard,
      renderJobsCard: renderJobsCard,
      renderDiagnosticsCard: renderDiagnosticsCard,
      renderGatesCard: renderGatesCard,
      renderCorpusCard: renderCorpusCard
    };
    return api;
  }

  var TEConsole = {
    VERSION: VERSION,
    CARD_ORDER: CARD_ORDER,
    /* The previous JOBS render, keyed by job id -- the JOBS card diffs against
       it to draw the up/delta/down markers (sweep section 3.5, test 11). Null
       means "no previous render", which must show zero markers, not a page of
       "new" rows. The card renderer owns every write to this slot. */
    jobsSnapshot: null,
    create: create,
    h2: h2,
    rowButton: rowButton,
    computeHealth: computeHealth,
    compressIdleGaps: compressIdleGaps,
    timelineX: timelineX,
    jobsSnapshotOf: jobsSnapshotOf,
    jobDelta: jobDelta,
    fmtAgoText: fmtAgoText,
    fmtDuration: fmtDuration,
    fmtCompact: fmtCompact,
    fmtTokens: fmtTokens,
    fmtClock: fmtClock,
    fmtElapsedClock: fmtElapsedClock,
    shortId: shortId,
    jsonish: jsonish,
    VOCABS: VOCABS,
    GLYPHS: GLYPHS,
    HEARTBEAT_LATE_S: HEARTBEAT_LATE_S,
    GAP_PX: GAP_PX,
    MAX_LANES: MAX_LANES
  };

  /* Attach to whatever global this file was loaded into: `window` in the
     browser and in the static export, the vm context's global under the Node
     test harness. module.exports is there so a plain `require()` works too. */
  var root = typeof globalThis !== "undefined" ? globalThis : this;
  root.TEConsole = TEConsole;
  if (typeof window !== "undefined" && window !== root) window.TEConsole = TEConsole;
  if (typeof module !== "undefined" && module && module.exports) module.exports = TEConsole;
})();
