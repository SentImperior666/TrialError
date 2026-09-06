/* ============================================================================
   TEFeed -- the Feed surface's post stream, in its own file.

   WHY THIS FILE EXISTS (house rule, LANE_C_DASHBOARD_COMPLETION_SPEC section 0).
   The inline script in dashboard.html is ~3,000 lines and is edited by several
   parallel builds at once, so every NEW renderer gets its own file under
   static/ -- console_render.js, evidence_render.js, feed_render.js.

   HOW IT IS WRITTEN. Nothing here may touch the global element factory. Every
   node comes from an element helper the caller INJECTS (`h`), whose contract is
   dashboard.html's own `el()`:

       h(tag, attrs, children) -> element
         attrs:  "class" -> className, "text" -> textContent,
                 "on<event>": function -> addEventListener(<event>, fn),
                 anything else -> setAttribute(key, value)
         children: array of (string | node | null); strings become text nodes.

   That is what lets the same file run under Node against tests/_dom_shim.js --
   no browser, no npm dependency, real assertions on the tree a card produces.

   WHAT IS HERE. The post stream, in the two reading orders the operator can
   pick between (spec section 2.2; ruling L-C4 makes THREADED the default and
   puts the AS IT ARRIVED toggle in localStorage, which is the PAGE's job, not
   this file's -- this file is handed the order and renders it).

     THREADED   cards in the server's `order_threaded`, indented by
                min(depth, 4), each reply's head linking back to its parent,
                each root with replies carrying an `N REPLIES` collapse.
     ARRIVED    cards in `panel.posts` -- append order, the raw truth.

   WHAT IS DELIBERATELY NOT HERE. The post BODY. Lane b owns
   `buildPostBody(post, panel)` -- the AS POSTED / PLAIN ENGLISH split, the five
   translation_state readings, the withheld strip, the TRANSLATE button -- and
   this file calls it UNCHANGED for every card, in both orders, at every depth.
   The only thing threading does to a body is put `is-stacked` on the card at
   depth >= 3 so dashboard.css collapses the two columns into one; the body
   node itself is passed through exactly as lane b built it. A renderer that
   rebuilt the column here would be a second copy of the translator UI, drifting
   from the first.

   PUBLIC SHAPE
   ------------
     TEFeed.VERSION
     TEFeed.ORDERS             -- ["threaded", "arrived"]
     TEFeed.DEFAULT_ORDER      -- "threaded" (L-C4)
     TEFeed.MAX_INDENT_DEPTH   -- 4; deeper replies stop moving right
     TEFeed.STACK_DEPTH        -- 3; at and past this depth the body stacks
     TEFeed.normalizeOrder(order)
     TEFeed.orderedPosts(panel, order)      -- pure; no DOM
     TEFeed.subtreeCounts(panel)            -- pure; root id -> replies hidden
                                               by collapsing it
     TEFeed.create({h}) -> renderer:
         .h .clear
         .normalizeOrder / .orderedPosts / .subtreeCounts
         .renderStream(panel, ctx) -> one <div class="feed-stream"> node

   ctx (every field optional; a missing one degrades, never throws)
     order            "threaded" | "arrived"
     collapsed        {rootPostId: true} -- in-memory, per spec section 2.2
     replyToId        the post the composer is currently answering
     writesEnabled    false -> every REPLY IN THREAD is drawn disabled WITH a
                      reason (DASHBOARD_V2_API section 12.11), which is also
                      what makes the static export read-only by construction
     buildPostBody(post, panel) -> node      lane b's, injected
     chip(text, variant) -> node             the page's
     fmtHM(iso) -> string                    the page's
     onReply(post)                           set the composer's reply target
     onJumpToParent(parentPostId)            scroll to + flash the parent
     onToggleCollapse(rootPostId)            flip collapsed[rootPostId]

   Loaded by dashboard.html from a `script src="feed_render.js"` tag before the
   inline script, and INLINED into the static export by export.py's
   _INLINE_SCRIPTS. Nothing here may contain a literal script end-tag: the HTML
   tokenizer would end the element there once the file is inlined, and export.py
   refuses to build a snapshot if one appears.
   ============================================================================ */
(function () {
  "use strict";

  var VERSION = 1;

  /* GROUPED BY GOAL is a third chip on the page, still disabled: it needs
     goal-grouping logic no API stage provides. It is not an order this file
     can render, so it is not in this list. */
  var ORDERS = ["threaded", "arrived"];
  var DEFAULT_ORDER = "threaded";

  /* Indent stops at four levels. A 14px rail per level is legible; an
     unbounded one turns a long argument into a diagonal line one word wide.
     Past the cap the head's parent link is what carries the relationship. */
  var MAX_INDENT_DEPTH = 4;

  /* At this depth the AS POSTED / PLAIN ENGLISH columns stop sitting side by
     side and stack instead: two columns inside a card already indented three
     rails deep are each about twenty characters, which is not a reading. */
  var STACK_DEPTH = 3;

  var REPLY_DISABLED_REASON =
    "live mode + a write token are required (static snapshots are read-only)";

  function normalizeOrder(order) {
    return ORDERS.indexOf(order) === -1 ? DEFAULT_ORDER : order;
  }

  function isThreaded(order) { return normalizeOrder(order) === "threaded"; }

  function postsOf(panel) { return (panel && panel.posts) || []; }

  function depthOf(post) {
    var d = Number(post && post.depth);
    return isFinite(d) && d > 0 ? Math.floor(d) : 0;
  }

  function parentIdOf(post) {
    if (!post) return null;
    /* `reply_to` is the field this build added; `in_reply_to` is the raw
       column, which a snapshot exported before this build still carries and
       nothing else. Reading both means an old bundle threads too. */
    var v = post.reply_to === undefined ? post.in_reply_to : post.reply_to;
    return v === undefined ? null : v;
  }

  function kindOf(post) {
    if (post && post.kind) return String(post.kind);
    var author = (post && post.author) || "";
    var i = author.indexOf(":");
    return i === -1 ? author : author.slice(0, i);
  }

  /** Posts in the requested reading order.
   *
   * THREADED follows the server's `order_threaded`. Two guards, both of which
   * exist because dropping a post is the one thing this surface must never do:
   * an id in the sequence with no post is skipped, and a post the sequence
   * never named is appended at the end rather than lost. The second is the
   * live case -- a static snapshot exported before this build has posts and no
   * `order_threaded` at all, and it must still render every one of them. */
  function orderedPosts(panel, order) {
    var posts = postsOf(panel);
    if (!isThreaded(order)) return posts.slice();
    var sequence = (panel && panel.order_threaded) || null;
    if (!sequence || !sequence.length) return posts.slice();

    var byId = {};
    posts.forEach(function (p) { byId[p.post_id] = p; });
    var out = [];
    var taken = {};
    sequence.forEach(function (pid) {
      var p = byId[pid];
      if (p && !taken[pid]) { taken[pid] = true; out.push(p); }
    });
    posts.forEach(function (p) {
      if (!taken[p.post_id]) { taken[p.post_id] = true; out.push(p); }
    });
    return out;
  }

  /** root post id -> how many posts collapsing that root would hide.
   *
   * The whole subtree, not `reply_count`'s direct children: the number on the
   * control has to be the number of cards that disappear when it is pressed,
   * or the control is lying about what it does. The server's `reply_count`
   * stays what it is -- "posts answering THIS one" -- and is the honest number
   * for a card's own head. */
  function subtreeCounts(panel) {
    var counts = {};
    postsOf(panel).forEach(function (p) {
      if (depthOf(p) > 0 && p.root_post_id) {
        counts[p.root_post_id] = (counts[p.root_post_id] || 0) + 1;
      }
    });
    return counts;
  }

  /* ---- fallbacks ----------------------------------------------------------
     The page injects its own versions of all three. These exist so that a
     renderer under test (and a page whose inline script changed shape) draws
     something honest rather than throwing -- NOT as a second implementation
     anyone should rely on. The body fallback in particular is the plain text
     only: it deliberately does not attempt lane b's translation column.
     ------------------------------------------------------------------------ */

  function fallbackChip(h, text, variant) {
    return h("span", { "class": "chip" + (variant ? " chip--" + variant : ""), text: text });
  }

  function fallbackHM(iso) {
    if (!iso) return "—";
    var s = String(iso);
    var t = s.indexOf("T");
    return t === -1 ? s.slice(0, 16) : s.slice(t + 1, t + 6);
  }

  function fallbackBody(h, post) {
    return h("div", { "class": "post-body-single" }, [
      h("div", { "class": "text", text: (post && post.body) || "" })
    ]);
  }

  /* ------------------------------------------------------------------------ */

  function parentLink(h, post, parent, ctx, hm) {
    var label = "↳ " + (parent ? kindOf(parent) : "post") + " · " + hm(parent ? parent.ts : null);
    var attrs = {
      type: "button",
      "class": "reply-head-link",
      "data-role": "feed-parent-link",
      "data-parent-post-id": parentIdOf(post),
      title: "jump to the post this answers",
      text: label
    };
    if (typeof ctx.onJumpToParent === "function") {
      attrs.onclick = function () { ctx.onJumpToParent(parentIdOf(post)); };
    }
    return h("button", attrs);
  }

  function orphanFlag(h, post) {
    var parent = parentIdOf(post);
    return h("span", {
      "class": "note-strip orphan-flag",
      "data-role": "feed-orphan-flag",
      title: parent
        ? "this post answers " + parent + ", which is not in this thread"
        : "this post answers a post that is not in this thread",
      text: "↳ replying to a post outside this thread"
    });
  }

  function replyButton(h, post, ctx) {
    var attrs = {
      type: "button",
      "class": "btn feed-reply-btn",
      "data-role": "feed-reply-btn",
      "data-post-id": post.post_id,
      text: "REPLY IN THREAD ▸"
    };
    if (!ctx.writesEnabled) {
      attrs.disabled = "disabled";
      attrs["aria-disabled"] = "true";
      attrs.title = REPLY_DISABLED_REASON;
    } else {
      attrs.title = "answer this post; the reply lands under it";
      if (typeof ctx.onReply === "function") {
        attrs.onclick = function () { ctx.onReply(post); };
      }
    }
    return h("button", attrs);
  }

  function collapseButton(h, post, n, collapsed, ctx) {
    var attrs = {
      type: "button",
      "class": "thread-collapse" + (collapsed ? " is-collapsed" : ""),
      "data-role": "feed-collapse-btn",
      "data-root-post-id": post.post_id,
      "aria-expanded": collapsed ? "false" : "true",
      title: collapsed ? "show the replies under this post" : "hide the replies under this post",
      /* Singular/plural rather than the spec's literal "N REPLIES": the
         control is read aloud by a screen reader and "1 REPLIES" is a bug
         report waiting to be filed. */
      text: (collapsed ? "▸ " : "▾ ") + n + (n === 1 ? " REPLY" : " REPLIES")
    };
    if (typeof ctx.onToggleCollapse === "function") {
      attrs.onclick = function () { ctx.onToggleCollapse(post.post_id); };
    }
    return h("button", attrs);
  }

  function renderCard(h, post, panel, ctx, env) {
    var chip = ctx.chip || function (t, v) { return fallbackChip(h, t, v); };
    var hm = ctx.fmtHM || fallbackHM;
    var depth = env.threaded ? depthOf(post) : 0;
    var indent = Math.min(depth, MAX_INDENT_DEPTH);

    var cls = "post-card";
    if (indent > 0) cls += " depth-" + indent;
    if (depth >= STACK_DEPTH) cls += " is-stacked";
    if (ctx.replyToId && ctx.replyToId === post.post_id) cls += " is-reply-target";

    var attrs = {
      "class": cls,
      "data-role": "feed-post-card",
      "data-post-id": post.post_id,
      "data-depth": String(depth)
    };
    if (post.root_post_id) attrs["data-root-post-id"] = post.root_post_id;

    var head = [
      chip(kindOf(post).toUpperCase(), "settled"),
      h("span", { text: post.author || "" })
    ];

    /* The parent line is drawn in BOTH orders, not only in THREADED. In AS IT
       ARRIVED the indent is gone by definition, so the head link is the only
       thing left saying what a post answers -- and "no structure at all" is
       the complaint this whole item exists to close. */
    if (post.reply_to_missing) {
      head.push(orphanFlag(h, post));
    } else {
      var parentId = parentIdOf(post);
      if (parentId) head.push(parentLink(h, post, env.byId[parentId], ctx, hm));
    }

    head.push(h("span", { "class": "fill", style: "flex-grow:1;" }));
    head.push(h("span", { text: hm(post.ts) + " UTC" }));
    head.push(replyButton(h, post, ctx));

    var hidden = env.threaded && depth === 0 ? (env.subtreeCount[post.post_id] || 0) : 0;
    if (hidden) {
      head.push(collapseButton(h, post, hidden, !!(ctx.collapsed || {})[post.post_id], ctx));
    }

    /* Lane b's renderer, called unchanged. Its node is appended as-is; the
       stacking at depth >= STACK_DEPTH is a class on the CARD, so the split
       the translator built survives threading untouched. */
    var body = typeof ctx.buildPostBody === "function" ? ctx.buildPostBody(post, panel) : null;
    if (!body) body = fallbackBody(h, post);

    return h("div", attrs, [h("div", { "class": "post-card-head" }, head), body]);
  }

  /** The whole stream as one node.
   *
   * One element rather than a list of cards so the harness can serialize the
   * result and the page can swap the stream in a single append; dashboard.css
   * gives `.feed-stream` the column layout `.feed-posts` used to own. */
  function renderStream(h, panel, ctx) {
    ctx = ctx || {};
    var order = normalizeOrder(ctx.order);
    var threaded = isThreaded(order);
    var posts = orderedPosts(panel, order);
    var collapsed = ctx.collapsed || {};

    var byId = {};
    postsOf(panel).forEach(function (p) { byId[p.post_id] = p; });

    var env = { threaded: threaded, byId: byId, subtreeCount: subtreeCounts(panel) };
    var stream = h("div", {
      "class": "feed-stream",
      "data-role": "feed-stream",
      "data-order": order
    });

    if (!posts.length) {
      stream.appendChild(h("div", { "class": "empty", text: "no posts in this thread yet" }));
      return stream;
    }

    var hiddenCount = 0;
    posts.forEach(function (post) {
      if (threaded && depthOf(post) > 0 && collapsed[post.root_post_id]) {
        hiddenCount += 1;
        return;
      }
      stream.appendChild(renderCard(h, post, panel, ctx, env));
    });

    if (hiddenCount) {
      /* Say it out loud. A collapsed thread and a thread that lost its
         replies to a rendering bug look identical otherwise, and this page's
         whole posture is that an absence is reported, never implied. */
      stream.appendChild(h("div", {
        "class": "note-strip",
        "data-role": "feed-collapsed-note",
        text: hiddenCount + (hiddenCount === 1 ? " REPLY IS" : " REPLIES ARE") + " COLLAPSED"
      }));
    }
    return stream;
  }

  /* ------------------------------------------------------------------------ */

  function create(env) {
    env = env || {};
    var h = env.h;
    if (typeof h !== "function") {
      throw new TypeError("TEFeed.create: env.h must be the element helper (tag, attrs, children) -> node");
    }

    function clear(node) {
      if (!node) return node;
      while (node.firstChild) node.removeChild(node.firstChild);
      return node;
    }

    return {
      VERSION: VERSION,
      ORDERS: ORDERS,
      DEFAULT_ORDER: DEFAULT_ORDER,
      MAX_INDENT_DEPTH: MAX_INDENT_DEPTH,
      STACK_DEPTH: STACK_DEPTH,
      h: h,
      clear: clear,
      normalizeOrder: normalizeOrder,
      orderedPosts: orderedPosts,
      subtreeCounts: subtreeCounts,
      renderStream: function (panel, ctx) { return renderStream(h, panel, ctx); }
    };
  }

  var TEFeed = {
    VERSION: VERSION,
    ORDERS: ORDERS,
    DEFAULT_ORDER: DEFAULT_ORDER,
    MAX_INDENT_DEPTH: MAX_INDENT_DEPTH,
    STACK_DEPTH: STACK_DEPTH,
    REPLY_DISABLED_REASON: REPLY_DISABLED_REASON,
    create: create,
    normalizeOrder: normalizeOrder,
    orderedPosts: orderedPosts,
    subtreeCounts: subtreeCounts
  };

  /* Attach to whatever global this file was loaded into: the browser window in
     a page and in the static export, the vm context's global under the Node
     test harness. module.exports is there so a plain require() works too. */
  var root = typeof globalThis !== "undefined" ? globalThis : this;
  root.TEFeed = TEFeed;
  if (typeof window !== "undefined" && window !== root) window.TEFeed = TEFeed;
  if (typeof module !== "undefined" && module && module.exports) module.exports = TEFeed;
})();
