/* ============================================================================
   TEConsole -- the Console surface's renderers, in their own file.

   WHY THIS FILE EXISTS (house rule, LANE_C_DASHBOARD_COMPLETION_SPEC section 0;
   sweep section 3.0). The inline script in dashboard.html is ~2,500 lines and is
   edited by several parallel builds at once. Every NEW renderer therefore gets
   its own file under static/ -- console_render.js here, evidence_render.js and
   feed_render.js beside it -- so that two builds touching two surfaces do not
   touch the same hunk.

   HOW IT IS WRITTEN. Nothing in this file may touch `document`. Every node is
   built through an element helper the caller INJECTS (`h`), whose contract is
   dashboard.html's own `el()`:

       h(tag, attrs, children) -> element
         attrs:  "class" -> className, "text" -> textContent,
                 "on<event>": function -> addEventListener(<event>, fn),
                 anything else -> setAttribute(key, value)
         children: array of (string | node | null); strings become text nodes.

   That one rule is what lets the same code run under Node against the DOM shim
   in tests/_dom_shim.js (sweep section 3.11) -- no browser, no npm dependency,
   real assertions on the tree a card produces.

   WHAT IS HERE TODAY. The shell: the two shared primitives the rest of the lane
   builds on (`h2`, `rowButton`) and the card registry the Console renderer fills.
   No card is registered yet, and `renderInto` leaves a container untouched when
   nothing is registered for it -- so loading this file changes no pixel. The
   Console renderer (sweep sections 3.1-3.9) registers its eight cards here.

   PUBLIC SHAPE
   ------------
     TEConsole.VERSION          -- bumped when the create() contract changes
     TEConsole.CARD_ORDER       -- the eight Console cards, in grid order
     TEConsole.jobsSnapshot     -- the previous JOBS render, for the up/delta/down
                                   markers (sweep section 3.5); the renderer owns it
     TEConsole.h2(h, label, opts)
     TEConsole.rowButton(h, content, opts)
     TEConsole.create({h})      -- binds h once and returns the renderer:
         .h                     -- the injected helper, for card code
         .h2(label, opts) / .rowButton(content, opts)
         .cards                 -- name -> render(bundle, ctx); read-only by convention
         .registerCard(name, fn)
         .renderInto(targets, bundle) -> [names painted]
         .clear(node)

   Loaded by dashboard.html from a `script src="console_render.js"` tag before
   the inline script, and INLINED into the static export by export.py's
   _INLINE_SCRIPTS (a file:// snapshot has no sibling files to fetch). Nothing
   in this file may contain a literal script end-tag: the HTML tokenizer would
   end the element there once the file is inlined. export.py refuses to build
   a snapshot if one appears.
   ============================================================================ */
(function () {
  "use strict";

  var VERSION = 1;

  /* The eight cards of the Console, in the order sweep section 3.9's grid lays
     them out: "session pools ledger" / "timeline" / "jobs diagnostics" /
     "gates corpus". renderInto paints in this order, so a card list read off a
     rendered page matches the canvas. */
  var CARD_ORDER = ["session", "pools", "ledger", "timeline", "jobs", "diagnostics", "gates", "corpus"];

  /* ------------------------------------------------------------------------
     Shared primitives. Both take the injected `h` as their first argument so
     they are usable from a file that has not called create() (evidence_render.js
     and feed_render.js reuse them -- spec section 3 item (i)); create() returns
     h-bound versions for card code, which is the ergonomic form.
     ------------------------------------------------------------------------ */

  /** A card title as a real heading element (M-VA-1: the app ships no <h*>
   * today, and a screen reader cannot skim a page of <span>s). `.title` already
   * carries the type scale, so <h2 class="title"> is visually identical to the
   * <span class="title"> it replaces -- dashboard.css resets the element's own
   * margin and size. */
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
   * row must be focusable and operable from the keyboard, which a <div onclick>
   * is not).
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
    if (Object.prototype.toString.call(content) === "[object Array]") {
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

    var cards = {};

    function clear(node) {
      if (!node) return node;
      while (node.firstChild) node.removeChild(node.firstChild);
      return node;
    }

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

    /** Paint every card that HAS a renderer into its container.
     *
     * `targets` is {cardName: element}; a name with no element, or with no
     * registered renderer, is skipped and its container left exactly as it was.
     * That is the whole reason the caller gets the painted list back: whoever
     * owns the page decides what to do with a card this file does not draw yet
     * (dashboard.html keeps the generic renderer on those, and hides the two
     * cards that have no generic form at all).
     *
     * A renderer that returns null has drawn nothing on purpose; the container
     * is still cleared, because a stale render is worse than an empty card. */
    function renderInto(targets, bundle) {
      var painted = [];
      if (!targets) return painted;
      CARD_ORDER.forEach(function (name) {
        var target = targets[name];
        var render = cards[name];
        if (!target || typeof render !== "function") return;
        clear(target);
        var node = render(bundle, api);
        if (node) target.appendChild(node);
        painted.push(name);
      });
      return painted;
    }

    var api = {
      VERSION: VERSION,
      CARD_ORDER: CARD_ORDER,
      h: h,
      clear: clear,
      cards: cards,
      registerCard: registerCard,
      renderInto: renderInto,
      h2: function (label, opts) { return h2(h, label, opts); },
      rowButton: function (content, opts) { return rowButton(h, content, opts); }
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
    rowButton: rowButton
  };

  /* Attach to whatever global this file was loaded into: `window` in the
     browser and in the static export, the vm context's global under the Node
     test harness. module.exports is there so a plain `require()` works too. */
  var root = typeof globalThis !== "undefined" ? globalThis : this;
  root.TEConsole = TEConsole;
  if (typeof window !== "undefined" && window !== root) window.TEConsole = TEConsole;
  if (typeof module !== "undefined" && module && module.exports) module.exports = TEConsole;
})();
