/* ============================================================================
   TEEvidence -- the Evidence surface's renderers, in their own file.

   Same house rule and same contract as console_render.js (spec section 0):
   nothing here may touch `document`; every node comes from the element helper
   the caller INJECTS as `h`, whose contract is dashboard.html's own `el()`.
   That is what lets the shipped file run under Node against tests/_dom_shim.js.

   ONE ADDITION to that contract: `env.svg`. An SVG element cannot be made with
   createElement in a browser -- it needs createElementNS, or the nodes are
   HTMLUnknownElements that render nothing. So the page injects a SECOND helper
   with the same (tag, attrs, children) signature and an SVG namespace behind
   it. It is optional: with no `svg` given the renderer falls back to `h`, which
   is exactly what the Node harness wants (the tree still serializes as
   svg/line/circle/text and a test can assert on it) and is why the fallback is
   not a silent browser bug -- dashboard.html always passes one.

   WHAT IS HERE. The regions of design/dashboard-v2/Evidence.dc.html, over
   trialerror.dashboard.data.build_evidence_panel's payload:

     renderIndex(panel, ctx)   the claim rail, filterable
     renderDetail(panel, ctx)  header, the claim block, WHAT IT STANDS ON,
                               WHAT ARGUES WITH IT, NEIGHBOURHOOD, THE SAME
                               EDGES AS A TABLE, and the co-anchored claims
     render(targets, panel, ctx) both, into {index, detail}

   ctx (every field optional):
     filter          string typed into the rail's filter input
     onSelectClaim   function(claim_id) -- rail rows and co-anchored rows
     edgeSort        {key, dir} for the edges table
     onSortEdges     function(key) -- a header cell was activated

   SHARED PRIMITIVES ARE NOT COPIED HERE. `h2` and `rowButton` live in
   console_render.js and take `h` as their first argument precisely so this
   file can call them unbound (TEConsole.h2(h, ...)). It is a hard dependency
   with no local fallback: see primitives() for why a half-copy would be worse
   than a loud refusal.

   Loaded by dashboard.html from a `script src="evidence_render.js"` tag before
   the inline script, and inlined into the static export by export.py's
   _INLINE_SCRIPTS. Nothing here may contain a literal script end-tag.
   ============================================================================ */
(function () {
  "use strict";

  var VERSION = 1;

  /* Above this many nodes the inline diagram stops being a reading and starts
     being a hairball -- spec section 1.3's scale ladder. The table below it is
     always drawn, so nothing is lost by refusing to draw the picture. */
  var SVG_NODE_CEILING = 100;

  /* trialerror.retrieve.wrap's two delimiters, verbatim. Only a script END-TAG
     would close this element early once export.py inlines the file, and these
     are not one, so they are safe to write out whole. */
  var UNTRUSTED_OPEN = "<untrusted-document-content>";
  var UNTRUSTED_CLOSE = "</untrusted-document-content>";

  /** The body of an untrusted-wrapped field. The server wraps `claim.text` and
   * every `fact_text` (trialerror.retrieve.wrap) to mark them as DATA for
   * whatever reads them; a human reading the page wants the text, not the
   * delimiters. Stripping is all this does -- the text still reaches the page
   * as a TEXT NODE and is never injected as markup, which is the property the
   * wrapper exists to protect and the reason the DOM shim refuses `{html}`. */
  function stripUntrusted(text) {
    if (text === null || text === undefined) return "";
    var s = String(text);
    if (s.indexOf(UNTRUSTED_OPEN) !== 0) return s;
    var end = s.lastIndexOf(UNTRUSTED_CLOSE);
    if (end === -1) return s;
    return s.slice(UNTRUSTED_OPEN.length, end).replace(/^\s+|\s+$/g, "");
  }

  /** The shared primitives, off the global console_render.js put them on.
   *
   * There is deliberately NO local fallback. A half-copy of `rowButton` would
   * be a second implementation of "a clickable row is a real button", and the
   * two would drift silently -- which is the exact failure the house rule
   * ("do not copy them into a second file") names. Missing primitives is a
   * LOAD-ORDER bug, so it refuses loudly at create() time and dashboard.html
   * paints its "the renderer did not load" callout instead of a page whose
   * rows quietly stopped being focusable. */
  function primitives() {
    var root = typeof globalThis !== "undefined" ? globalThis : this;
    var shared = root && root.TEConsole;
    if (!shared || typeof shared.h2 !== "function" || typeof shared.rowButton !== "function") {
      throw new TypeError(
        "TEEvidence needs console_render.js loaded first -- it owns the shared h2/rowButton primitives"
      );
    }
    return shared;
  }

  function h2(h, label, opts) { return primitives().h2(h, label, opts); }
  function rowButton(h, content, opts) { return primitives().rowButton(h, content, opts); }

  function create(env) {
    env = env || {};
    var h = env.h;
    if (typeof h !== "function") {
      throw new TypeError("TEEvidence.create: env.h must be the element helper (tag, attrs, children) -> node");
    }
    primitives();  // fail at wiring time, not halfway through a render
    /* No namespace helper -> plain elements. Correct under the Node shim,
       and dashboard.html always supplies the real one. */
    var svg = typeof env.svg === "function" ? env.svg : h;

    function chip(label, tone, title) {
      var attrs = { "class": "ev-chip ev-chip--" + (tone || "neutral"), text: label };
      if (title) attrs.title = title;
      return h("span", attrs);
    }

    function card(title, sub, body, opts) {
      opts = opts || {};
      var head = [h2(h, title)];
      if (sub) head.push(h("span", { "class": "sub", text: sub }));
      var kids = [h("div", { "class": "card-header" }, head), h("div", { "class": "card-body" }, body || [])];
      if (opts.footer) kids.push(h("div", { "class": "card-footer", text: opts.footer }));
      return h("div", { "class": "card" + (opts.className ? " " + opts.className : "") }, kids);
    }

    function empty(reading) {
      return h("div", { "class": "empty", text: reading });
    }

    /* ---------------------------------------------------------------- rail */

    /** The claim index. Filtered CLIENT-side over what the server already
     * sent: the rail is capped at 100 rows by the builder, so a filter that
     * re-asked the server would be answering a different question than the
     * one the count above it reports. When the cap bit, the rail says so --
     * a filter over a truncated list that did not admit it would read as
     * "no such claim". */
    function renderIndex(panel, ctx) {
      ctx = ctx || {};
      var wrap = h("div", { "class": "ev-index" });
      if (!panel || panel.status !== "ok") {
        wrap.appendChild(empty(panel && panel.message ? panel.message : "no data"));
        return wrap;
      }
      var rows = panel.index || [];
      var needle = (ctx.filter || "").toLowerCase().replace(/^\s+|\s+$/g, "");
      var shown = !needle ? rows : rows.filter(function (r) {
        return [r.claim_id, r.text_short, r.source_id, r.source_title, r.kind]
          .some(function (f) { return f && String(f).toLowerCase().indexOf(needle) !== -1; });
      });

      wrap.appendChild(h("div", {
        "class": "ev-index-count",
        text: shown.length + " OF " + (panel.index_total || 0) + " CLAIMS"
      }));
      if (panel.index_truncated) {
        wrap.appendChild(h("div", {
          "class": "ev-index-truncated",
          text: "▲ SHOWING THE NEWEST " + rows.length + " OF " + panel.index_total +
            " LIVE CLAIMS · FILTER SEARCHES ONLY THESE"
        }));
      }
      if (!shown.length) {
        wrap.appendChild(empty(needle ? "no claim in this page of the index matches that filter" : "no live claims yet"));
        return wrap;
      }
      shown.forEach(function (r) {
        var meta = [r.kind, r.anchor_count + (r.anchor_count === 1 ? " ANCHOR" : " ANCHORS")];
        if (r.source_title) meta.push(r.source_title);
        if (r.superseded) meta.push("SUPERSEDED");
        wrap.appendChild(rowButton(h, [
          h("span", { "class": "ev-index-id", text: r.claim_id }),
          h("span", { "class": "ev-index-text", text: r.text_short || "" }),
          h("span", { "class": "ev-index-meta", text: meta.join(" · ") })
        ], {
          className: "ev-index-row",
          selected: r.claim_id === panel.active_claim_id,
          onClick: ctx.onSelectClaim ? function () { ctx.onSelectClaim(r.claim_id); } : undefined,
          disabled: ctx.onSelectClaim ? false : "this snapshot has no server to fetch another claim from",
          dataset: { claim: r.claim_id }
        }));
      });
      return wrap;
    }

    /* -------------------------------------------------------------- header */

    function renderHeader(panel) {
      var claim = panel.claim;
      var n = panel.neighbourhood || {};
      var primary = (panel.anchors || [])[0];
      var trail = ["ASK", (primary && primary.source_title) || "NO SOURCE RESOLVED", claim.claim_id];
      var bits = [h("span", { "class": "t", text: trail.join(" › ") })];
      bits.push(h("span", {
        "class": "m",
        text: "HOPS " + (n.hops_reached || 0) + " · CEILING " + (n.hop_limit || 0)
      }));
      if (n.truncated) {
        bits.push(chip(
          "▲ RESULT TRUNCATED AT " + n.hop_limit + " EDGES",
          "warn",
          "at least one hop hit the engine's per-hop LIMIT guard, so edges past it were not read"
        ));
      }
      if (n.seeds_dropped) {
        bits.push(chip(
          "▲ " + n.seeds_dropped + " SEED ENTITIES NOT EXPANDED",
          "warn",
          "one graph query per seed; the rest are listed under THE SAME EDGES AS A TABLE only if an expanded seed reached them"
        ));
      }
      bits.push(h("span", { "class": "fill" }));
      return h("div", { "class": "subbar" }, bits);
    }

    function renderClaimBlock(panel) {
      var claim = panel.claim;
      var meta = h("div", { "class": "ev-claim-meta" }, [
        chip(String(claim.kind || "").toUpperCase(), "neutral"),
        chip("CONFIDENCE " + (claim.confidence === null || claim.confidence === undefined ? "—" : claim.confidence), "neutral"),
        chip("VALID SINCE " + (claim.valid_at || claim.created_at || "—"), "neutral"),
        claim.fenced ? chip("■ FENCED", "warn", "this claim's primary source is commercial_restricted: the text is capped at 20 words") : null
      ]);
      var body = [meta, h("div", { "class": "ev-claim-text", text: stripUntrusted(claim.text) })];
      if (claim.superseded_by) {
        body.push(h("div", {
          "class": "warn-callout",
          text: "▲ SUPERSEDED BY " + claim.superseded_by
        }));
      }
      if (claim.expired_at || claim.invalid_at) {
        body.push(h("div", {
          "class": "warn-callout",
          text: "▲ NOT IN THE LIVE VIEW · " +
            (claim.expired_at ? "EXPIRED " + claim.expired_at : "") +
            (claim.expired_at && claim.invalid_at ? " · " : "") +
            (claim.invalid_at ? "INVALIDATED " + claim.invalid_at : "")
        }));
      }
      var lineage = panel.lineage || {};
      if (lineage.supersedes && lineage.supersedes.length) {
        body.push(h("div", { "class": "ev-lineage", text: "SUPERSEDES " + lineage.supersedes.join(", ") }));
      }
      return card("THE CLAIM", claim.claim_id, body);
    }

    /* ------------------------------------------------ WHAT IT STANDS ON */

    /** The two hash chips answer different questions and are drawn as two
     * chips for that reason (spec section 1.3):
     *   doc_sha_matches   -- has the DOCUMENT moved since this anchor was cut?
     *   quote_sha_matches -- does the stored quote still hash to what was
     *                        recorded? `null` is a third state (no quote_text
     *                        was ever stored), not a failure. */
    function anchorChips(a) {
      var out = [];
      if (a.fenced) out.push(chip("■ FENCED", "warn", "commercial_restricted source: at most a 20-word verbatim excerpt is served"));
      out.push(a.doc_sha_matches
        ? chip("✓ DOC SHA MATCHES", "ok", "the document still hashes to what this anchor recorded")
        : chip("▲ STALE, DOCUMENT RE-INGESTED", "crit",
               "the document no longer hashes to what this anchor recorded -- its character offsets may now point at different bytes"));
      if (a.quote_sha_matches === null || a.quote_sha_matches === undefined) {
        out.push(chip("○ QUOTE NOT RE-CHECKABLE", "neutral", "this anchor stored no quote_text, so its quote hash cannot be recomputed here"));
      } else if (a.quote_sha_matches === false) {
        // Not one of the artboard's three chips, and it has to exist: an
        // anchor whose stored quote no longer hashes to quote_sha256 is a
        // louder finding than a moved document, and silence would read as
        // "checked, fine".
        out.push(chip("▲ QUOTE HASH MISMATCH", "crit", "the stored quote_text does not hash to quote_sha256"));
      }
      return out;
    }

    function renderStandsOn(panel) {
      var anchors = panel.anchors || [];
      if (!anchors.length) return card("WHAT IT STANDS ON", null, [empty("this claim resolves to no anchor rows at all")]);
      var rows = anchors.map(function (a) {
        if (a.missing) {
          return h("div", { "class": "ev-anchor ev-anchor--missing" }, [
            h("div", { "class": "warn-callout", text: "▲ ANCHOR " + a.anchor_id + " IS NAMED BY THIS CLAIM BUT HAS NO ROW" })
          ]);
        }
        var coords = [
          a.anchor_id,
          a.source_id || "NO SOURCE",
          a.doc_id || "NO DOC",
          a.page === null || a.page === undefined ? "PAGE —" : "PAGE " + a.page,
          "CHARS " + a.char_start + "–" + a.char_end
        ].join(" · ");
        return h("div", { "class": "ev-anchor ev-anchor--" + a.role }, [
          h("div", { "class": "ev-anchor-quote", text: a.quote || "(no quote text stored)" }),
          h("div", { "class": "ev-anchor-coords", text: coords }),
          h("div", { "class": "ev-anchor-chips" }, [chip(a.role.toUpperCase(), "neutral")].concat(anchorChips(a)))
        ]);
      });
      return card("WHAT IT STANDS ON", anchors.length + (anchors.length === 1 ? " ANCHOR" : " ANCHORS"), rows);
    }

    /* --------------------------------------------- WHAT ARGUES WITH IT */

    function renderArgues(panel, ctx) {
      var argues = panel.argues || {};
      var body = [];
      var verdicts = argues.verdicts || [];
      if (verdicts.length) {
        verdicts.forEach(function (v) {
          body.push(h("div", { "class": "ev-verdict" }, [
            h("div", { "class": "ev-verdict-head" }, [
              // The label is carried verbatim -- a verdict's own word for what
              // it found, never re-tiered by this page.
              h("span", { "class": "ev-verdict-label", text: v.label }),
              chip(v.procedure + " v" + v.procedure_version, "neutral"),
              v.prereg_compliant === 1 ? chip("PREREG COMPLIANT", "ok") : null,
              v.prereg_compliant === 0 ? chip("▲ NOT PREREG COMPLIANT", "warn") : null
            ]),
            h("div", { "class": "ev-verdict-meta", text: v.verdict_id + " · " + v.ts + " · " + v.issued_by_launch })
          ]));
        });
      }
      var edges = (argues.contradicts || []).concat(argues.supports || []);
      edges.forEach(function (e) {
        body.push(rowButton(h, [e.role.toUpperCase(), e.src_id, e.dst_id, e.ts], {
          className: "ev-prov-row",
          disabled: "a prov_edge row is a reading only -- nothing in this codebase writes or resolves one yet"
        }));
      });
      if (!verdicts.length && !edges.length) {
        body.push(empty("0 EDGES · THE GENERAL PROVENANCE GRAPH IS EMPTY"));
      }
      if (argues.note) body.push(h("div", { "class": "note-strip", text: argues.note }));

      // 12.11: a control drawn disabled says why. No callable exists for
      // either verb, so neither is given a handler -- not even a no-op.
      body.push(h("div", { "class": "btn-row" }, [
        rowButton(h, "SEND TO DETERMINATIONS", {
          className: "ev-action",
          disabled: "no callable exists: the determination queue has no 'claim needs a look' kind, and inventing one here would put a row in a queue nothing drains"
        }),
        rowButton(h, "OPEN A ROOM ON IT", {
          className: "ev-action",
          disabled: "no callable exists: rooms.api.create_room needs discussion points and a real launch identity, neither of which this page can supply"
        })
      ]));

      var omitted = panel.term_conflicts_omitted;
      if (omitted) {
        // L-C5's two-step: the region is not drawn, and the page says which
        // read is missing rather than showing an empty box that reads
        // "no term conflicts".
        body.push(h("div", { "class": "note-strip", text: "TERM-SENSE CONFLICTS: " + omitted.message }));
      } else if (panel.term_conflicts) {
        (panel.term_conflicts.conflicts || []).forEach(function (c) {
          body.push(rowButton(h, [c.term || "", c.reason || ""], { className: "ev-term-conflict", disabled: true }));
        });
      }
      return card("WHAT ARGUES WITH IT", null, body);
    }

    /* -------------------------------------------------- NEIGHBOURHOOD */

    /** A ring of entities around the claim at the centre. Deliberately not a
     * force layout: a deterministic ring is readable at a glance, reproducible
     * between renders (so a diagram that MOVED means the data moved), and
     * costs no animation frame on a page an operator leaves open. */
    function renderGraph(n) {
      var nodes = n.nodes || [];
      var w = 640, hgt = 320, cx = w / 2, cy = hgt / 2, r = Math.min(cx, cy) - 46;
      var root = svg("svg", { viewBox: "0 0 " + w + " " + hgt, width: "100%", height: "320", "class": "ev-graph" });
      var pos = {};
      var ring = nodes.filter(function (x) { return x.kind !== "claim"; });
      var claimNode = nodes.filter(function (x) { return x.kind === "claim"; })[0] || null;
      if (claimNode) pos[claimNode.id] = [cx, cy];
      ring.forEach(function (node, i) {
        var angle = (2 * Math.PI * i) / Math.max(ring.length, 1) - Math.PI / 2;
        pos[node.id] = [cx + r * Math.cos(angle), cy + r * Math.sin(angle)];
      });

      var seeds = {};
      (n.seed_entities || []).forEach(function (s) { seeds[s.entity_id] = s.via_anchor; });
      // Dashed spokes from the claim to its SEEDS only: the claim is not a
      // node in the relation graph, and a solid line would say it was.
      Object.keys(seeds).forEach(function (eid) {
        if (!pos[eid] || !claimNode) return;
        root.appendChild(svg("line", {
          x1: cx, y1: cy, x2: pos[eid][0], y2: pos[eid][1],
          "class": "ev-graph-spoke", "stroke-dasharray": "3 3"
        }));
      });
      (n.edges || []).forEach(function (e) {
        if (!pos[e.src] || !pos[e.dst]) return;
        root.appendChild(svg("line", {
          x1: pos[e.src][0], y1: pos[e.src][1], x2: pos[e.dst][0], y2: pos[e.dst][1],
          "class": "ev-graph-edge" + (e.fenced ? " is-fenced" : "")
        }));
      });
      nodes.forEach(function (node) {
        var p = pos[node.id];
        if (!p) return;
        root.appendChild(svg("circle", {
          cx: p[0], cy: p[1], r: node.kind === "claim" ? 7 : 5,
          "class": "ev-graph-node ev-graph-node--" + node.kind + (seeds[node.id] ? " is-seed" : "")
        }));
        root.appendChild(svg("text", {
          x: p[0] + 9, y: p[1] + 4, "class": "ev-graph-label", text: node.label
        }));
      });
      return root;
    }

    function renderNeighbourhood(panel) {
      var n = panel.neighbourhood || {};
      var body = [];
      var count = n.node_count || 0;
      if (!count || count <= 1) {
        body.push(empty("NO LIVE RELATION IS ANCHORED ON THIS CLAIM'S EVIDENCE · NOTHING TO DRAW"));
      } else if (count <= SVG_NODE_CEILING) {
        body.push(renderGraph(n));
      } else {
        body.push(h("div", {
          "class": "note-strip",
          text: count + " NODES, DRAWN AS A TABLE ONLY"
        }));
      }
      var seedLine = (n.seed_entities || []).map(function (s) {
        return (s.name || s.entity_id) + " (via " + s.via_anchor + ")";
      }).join(" · ");
      if (seedLine) body.push(h("div", { "class": "ev-seed-line", text: "SEEDS · " + seedLine }));
      return card("NEIGHBOURHOOD", "MAX HOPS " + (n.max_hops || 0) + " · " + count + " NODES", body);
    }

    /* ------------------------------------ THE SAME EDGES AS A TABLE */

    var EDGE_COLUMNS = [
      { key: "role", label: "ROLE", value: function (e) { return e.rel_type || ""; } },
      { key: "target", label: "TARGET", value: function (e) { return e.dst || ""; } },
      { key: "why", label: "WHY", value: function (e) { return stripUntrusted(e.fact_text); } },
      { key: "source", label: "SOURCE", value: function (e) { return e.evidence_anchor || ""; } }
    ];

    function sortEdges(edges, sort) {
      if (!sort || !sort.key) return edges.slice();
      var column = EDGE_COLUMNS.filter(function (c) { return c.key === sort.key; })[0];
      if (!column) return edges.slice();
      var dir = sort.dir === "desc" ? -1 : 1;
      return edges.slice().sort(function (a, b) {
        var av = String(column.value(a)), bv = String(column.value(b));
        return av < bv ? -dir : (av > bv ? dir : 0);
      });
    }

    function renderEdgeTable(panel, ctx) {
      ctx = ctx || {};
      var n = panel.neighbourhood || {};
      var edges = n.edges || [];
      var body = [];
      var sort = ctx.edgeSort || {};
      body.push(h("div", { "class": "ev-edge-head" }, EDGE_COLUMNS.map(function (c) {
        var arrow = sort.key === c.key ? (sort.dir === "desc" ? " ▼" : " ▲") : "";
        return rowButton(h, c.label + arrow, {
          className: "ev-edge-headcell",
          selected: sort.key === c.key,
          onClick: ctx.onSortEdges ? function () { ctx.onSortEdges(c.key); } : undefined,
          disabled: ctx.onSortEdges ? false : "sorting needs a handler this render was not given"
        });
      })));
      if (!edges.length) {
        body.push(empty("0 EDGES"));
      } else {
        sortEdges(edges, sort).forEach(function (e) {
          body.push(rowButton(h, EDGE_COLUMNS.map(function (c) { return c.value(e); }), {
            className: "ev-edge-row" + (e.fenced ? " is-fenced" : ""),
            disabled: "a relation row is a reading -- this build wires no verb on one",
            dataset: { rel: e.rel_id }
          }));
        });
      }
      var listed = n.edges_listed || 0, total = n.edge_count || 0;
      var footer = total > listed ? (total - listed) + " OF " + total + " EDGES NOT LISTED" : listed + " EDGES";
      return card("THE SAME EDGES AS A TABLE", null, body, { footer: footer });
    }

    /* ------------------------------------------------ co-anchored claims */

    var SHARED_READING = {
      anchor: "the same anchor",
      chunk: "the same chunk",
      document: "the same document"
    };

    function renderCoAnchored(panel, ctx) {
      ctx = ctx || {};
      var rows = panel.co_anchored_claims || [];
      var body = [];
      if (!rows.length) {
        body.push(empty("NO OTHER LIVE CLAIM STANDS ON THIS EVIDENCE"));
      } else {
        rows.forEach(function (c) {
          body.push(rowButton(h, [
            c.claim_id,
            c.kind,
            SHARED_READING[c.shared] || c.shared,
            c.text_short || ""
          ], {
            className: "ev-coanchor-row",
            onClick: ctx.onSelectClaim ? function () { ctx.onSelectClaim(c.claim_id); } : undefined,
            disabled: ctx.onSelectClaim ? false : "this snapshot has no server to fetch another claim from",
            dataset: { claim: c.claim_id, shared: c.shared }
          }));
        });
      }
      return card("THE SAME EVIDENCE, ELSEWHERE", rows.length + " CLAIMS", body);
    }

    /* -------------------------------------------------------------- detail */

    function renderDetail(panel, ctx) {
      ctx = ctx || {};
      var wrap = h("div", { "class": "ev-detail" });
      if (!panel || panel.status !== "ok") {
        wrap.appendChild(h("div", { "class": "warn-callout" }, [
          h("div", { "class": "head", text: String((panel && panel.status) || "NO DATA").toUpperCase() }),
          h("div", { "class": "body", text: (panel && panel.message) || "" })
        ]));
        return wrap;
      }
      if (panel.not_found) {
        wrap.appendChild(h("div", { "class": "warn-callout" }, [
          h("div", { "class": "head", text: "▲ NOTHING HERE YET" }),
          h("div", {
            "class": "body",
            text: "no live claim resolves from " + panel.not_found.kind + " " + panel.not_found.id +
              " — the anchor exists in the corpus, but nothing has made a claim on it. " +
              "Pick a claim from the index."
          })
        ]));
      }
      if (!panel.claim) {
        if (!panel.not_found) wrap.appendChild(empty("no claim selected"));
        return wrap;
      }
      wrap.appendChild(renderHeader(panel));
      var body = h("div", { "class": "panel-body-pad" }, [
        renderClaimBlock(panel),
        renderStandsOn(panel),
        renderArgues(panel, ctx),
        renderNeighbourhood(panel),
        renderEdgeTable(panel, ctx),
        renderCoAnchored(panel, ctx)
      ]);
      wrap.appendChild(body);
      return wrap;
    }

    function clear(node) {
      if (!node) return node;
      while (node.firstChild) node.removeChild(node.firstChild);
      return node;
    }

    /** Paint both regions. `targets` is {index, detail}; a name with no
     * element is skipped and the painted list comes back, the same contract
     * TEConsole.renderInto uses. */
    function render(targets, panel, ctx) {
      var painted = [];
      if (!targets) return painted;
      if (targets.index) {
        clear(targets.index).appendChild(renderIndex(panel, ctx));
        painted.push("index");
      }
      if (targets.detail) {
        clear(targets.detail).appendChild(renderDetail(panel, ctx));
        painted.push("detail");
      }
      return painted;
    }

    return {
      VERSION: VERSION,
      SVG_NODE_CEILING: SVG_NODE_CEILING,
      EDGE_COLUMNS: EDGE_COLUMNS,
      h: h,
      clear: clear,
      stripUntrusted: stripUntrusted,
      sortEdges: sortEdges,
      renderIndex: renderIndex,
      renderDetail: renderDetail,
      render: render
    };
  }

  var TEEvidence = {
    VERSION: VERSION,
    SVG_NODE_CEILING: SVG_NODE_CEILING,
    stripUntrusted: stripUntrusted,
    create: create
  };

  var root = typeof globalThis !== "undefined" ? globalThis : this;
  root.TEEvidence = TEEvidence;
  if (typeof window !== "undefined" && window !== root) window.TEEvidence = TEEvidence;
  if (typeof module !== "undefined" && module && module.exports) module.exports = TEEvidence;
})();
