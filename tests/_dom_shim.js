/* ============================================================================
   A DOM small enough to read in one sitting (sweep section 3.11).

   The dashboard's renderers live in their own files (static/*_render.js) and
   build every node through an injected element helper, never through `document`
   directly. That makes them testable without a browser -- but only if something
   supplies the handful of DOM methods the helper itself uses. This is that
   something: a plain-object tree with createElement / createTextNode /
   appendChild / setAttribute / classList / textContent / querySelector(All),
   plus serialize() so a Python test can assert on the result as JSON.

   It is deliberately NOT a DOM implementation. No layout, no events fired, no
   namespaces, no innerHTML. If a renderer needs something this shim does not
   have, the honest answer is usually that the renderer should not need it --
   and if it genuinely should, add the method here rather than reaching for a
   dependency (zero npm packages is a hard rule for this harness).

   CommonJS on purpose: the repo declares no package.json, so Node treats .js as
   CommonJS and `node tests/_console_render_harness.js` just runs.
   ============================================================================ */
"use strict";

function TextNode(text) {
  this.nodeType = 3;
  this.tag = "#text";
  this.nodeValue = String(text);
  this.parentNode = null;
  this.childNodes = [];
}
Object.defineProperty(TextNode.prototype, "textContent", {
  get: function () { return this.nodeValue; },
  set: function (v) { this.nodeValue = String(v); }
});

function ClassList(element) {
  this._el = element;
}
ClassList.prototype._read = function () {
  var raw = this._el._attrs["class"];
  if (!raw) return [];
  return String(raw).split(/\s+/).filter(function (c) { return c.length > 0; });
};
ClassList.prototype._write = function (list) {
  this._el._attrs["class"] = list.join(" ");
};
ClassList.prototype.add = function () {
  var list = this._read();
  Array.prototype.slice.call(arguments).forEach(function (c) {
    if (list.indexOf(c) === -1) list.push(c);
  });
  this._write(list);
};
ClassList.prototype.remove = function () {
  var drop = Array.prototype.slice.call(arguments);
  this._write(this._read().filter(function (c) { return drop.indexOf(c) === -1; }));
};
ClassList.prototype.contains = function (c) { return this._read().indexOf(c) !== -1; };
ClassList.prototype.toggle = function (c, force) {
  var has = this.contains(c);
  var want = force === undefined ? !has : !!force;
  if (want) this.add(c); else this.remove(c);
  return want;
};
Object.defineProperty(ClassList.prototype, "length", {
  get: function () { return this._read().length; }
});

function Element(tag) {
  this.nodeType = 1;
  this.tag = String(tag).toLowerCase();
  this._attrs = {};
  this._listeners = {};
  this.childNodes = [];
  this.parentNode = null;
  this.classList = new ClassList(this);
  this.style = {};
}

Object.defineProperty(Element.prototype, "className", {
  get: function () { return this._attrs["class"] || ""; },
  set: function (v) { this._attrs["class"] = String(v); }
});

Object.defineProperty(Element.prototype, "firstChild", {
  get: function () { return this.childNodes.length ? this.childNodes[0] : null; }
});

Object.defineProperty(Element.prototype, "children", {
  get: function () { return this.childNodes.filter(function (n) { return n.nodeType === 1; }); }
});

Object.defineProperty(Element.prototype, "textContent", {
  get: function () {
    return this.childNodes.map(function (n) { return n.textContent; }).join("");
  },
  set: function (v) {
    this.childNodes = [];
    if (v !== null && v !== undefined && String(v).length) {
      this.appendChild(new TextNode(v));
    }
  }
});

Element.prototype.appendChild = function (node) {
  if (node === null || node === undefined) return node;
  node.parentNode = this;
  this.childNodes.push(node);
  return node;
};
Element.prototype.removeChild = function (node) {
  var i = this.childNodes.indexOf(node);
  if (i !== -1) { this.childNodes.splice(i, 1); node.parentNode = null; }
  return node;
};
Element.prototype.setAttribute = function (name, value) {
  this._attrs[String(name)] = value === null || value === undefined ? "" : String(value);
};
Element.prototype.getAttribute = function (name) {
  return Object.prototype.hasOwnProperty.call(this._attrs, name) ? this._attrs[name] : null;
};
Element.prototype.hasAttribute = function (name) {
  return Object.prototype.hasOwnProperty.call(this._attrs, name);
};
Element.prototype.removeAttribute = function (name) { delete this._attrs[name]; };
Element.prototype.addEventListener = function (type, fn) {
  (this._listeners[type] = this._listeners[type] || []).push(fn);
};
/** Not a real event: no bubbling, no Event object. Enough to prove a handler
 *  was wired to the node the test thinks it was wired to. */
Element.prototype.dispatch = function (type, payload) {
  (this._listeners[type] || []).forEach(function (fn) { fn(payload); });
  return (this._listeners[type] || []).length;
};

/* ---- selectors ----------------------------------------------------------
   Supported: a descendant chain of simple selectors, each of which may combine
   a tag name, any number of .classes, and any number of [attr] / [attr="value"]
   parts -- e.g. `.card h2.title`, `button[data-role="reveal"]`. That is the whole
   vocabulary the renderers' own tests need; anything richer (combinators, :not,
   nth-child) throws rather than quietly matching the wrong thing.
   ------------------------------------------------------------------------ */
var SIMPLE_RE = /^([a-zA-Z][\w-]*)?((?:[.#][\w-]+|\[[^\]]+\])*)$/;
var PART_RE = /([.#][\w-]+|\[[^\]]+\])/g;

function parseSimple(selector) {
  var m = SIMPLE_RE.exec(selector);
  if (!m) throw new Error("_dom_shim: unsupported selector fragment " + JSON.stringify(selector));
  var spec = { tag: m[1] ? m[1].toLowerCase() : null, classes: [], id: null, attrs: [] };
  var parts = m[2] ? m[2].match(PART_RE) || [] : [];
  parts.forEach(function (p) {
    if (p[0] === ".") spec.classes.push(p.slice(1));
    else if (p[0] === "#") spec.id = p.slice(1);
    else {
      var body = p.slice(1, -1);
      var eq = body.indexOf("=");
      if (eq === -1) spec.attrs.push([body, null]);
      else {
        var name = body.slice(0, eq);
        var value = body.slice(eq + 1).replace(/^["']|["']$/g, "");
        spec.attrs.push([name, value]);
      }
    }
  });
  return spec;
}

function matchesSimple(node, spec) {
  if (node.nodeType !== 1) return false;
  if (spec.tag && node.tag !== spec.tag) return false;
  if (spec.id && node.getAttribute("id") !== spec.id) return false;
  for (var i = 0; i < spec.classes.length; i++) {
    if (!node.classList.contains(spec.classes[i])) return false;
  }
  for (var j = 0; j < spec.attrs.length; j++) {
    var name = spec.attrs[j][0], want = spec.attrs[j][1];
    if (!node.hasAttribute(name)) return false;
    if (want !== null && node.getAttribute(name) !== want) return false;
  }
  return true;
}

function descendants(root, out) {
  out = out || [];
  root.childNodes.forEach(function (c) {
    if (c.nodeType !== 1) return;
    out.push(c);
    descendants(c, out);
  });
  return out;
}

function queryAll(root, selector) {
  var chain = String(selector).trim().split(/\s+/).map(parseSimple);
  var current = [root];
  chain.forEach(function (spec) {
    var next = [];
    current.forEach(function (node) {
      descendants(node).forEach(function (d) {
        if (matchesSimple(d, spec) && next.indexOf(d) === -1) next.push(d);
      });
    });
    current = next;
  });
  return current;
}

Element.prototype.querySelectorAll = function (selector) { return queryAll(this, selector); };
Element.prototype.querySelector = function (selector) {
  var found = queryAll(this, selector);
  return found.length ? found[0] : null;
};
Element.prototype.matches = function (selector) { return matchesSimple(this, parseSimple(selector)); };

/* ---- document ---------------------------------------------------------- */

function createDocument() {
  var doc = new Element("html");
  doc.createElement = function (tag) { return new Element(tag); };
  doc.createTextNode = function (text) { return new TextNode(text); };
  doc.createDocumentFragment = function () { return new Element("#fragment"); };
  doc.getElementById = function (id) { return doc.querySelector("[id=\"" + id + "\"]"); };
  doc.body = new Element("body");
  doc.appendChild(doc.body);
  return doc;
}

/** The tree as plain JSON, which is what the Python side asserts on.
 *  `text` is the node's full textContent (so a test can assert on a row's
 *  reading without walking children); `attrs` excludes `class`, which is
 *  reported once as `classes`. */
function serialize(node) {
  if (node === null || node === undefined) return null;
  if (node.nodeType === 3) return { tag: "#text", attrs: {}, classes: [], text: node.nodeValue, children: [] };
  var attrs = {};
  Object.keys(node._attrs).forEach(function (k) {
    if (k !== "class") attrs[k] = node._attrs[k];
  });
  return {
    tag: node.tag,
    attrs: attrs,
    classes: node.classList._read(),
    text: node.textContent,
    listeners: Object.keys(node._listeners),
    children: node.childNodes.map(serialize)
  };
}

/** dashboard.html's own `el()`, to the letter -- the contract every
 *  *_render.js file is written against. Kept here (not in the shim's Element)
 *  because it is the PAGE's helper, injected into the renderers; the shim only
 *  has to be able to host it. */
function makeH(doc) {
  return function h(tag, attrs, children) {
    var node = doc.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        if (k === "class") node.className = attrs[k];
        else if (k === "text") node.textContent = attrs[k];
        else if (k === "html") {
          // The page's own el() supports it; a *_render.js file must not. Every
          // renderer under test builds nodes, never markup strings -- fenced
          // quote text and untrusted-wrapped fact text pass through here.
          throw new Error("_dom_shim: h(..., {html}) is not supported -- renderers must not inject HTML");
        }
        else if (k.indexOf("on") === 0 && typeof attrs[k] === "function") node.addEventListener(k.slice(2), attrs[k]);
        else node.setAttribute(k, attrs[k]);
      });
    }
    (children || []).forEach(function (c) {
      if (c === null || c === undefined) return;
      node.appendChild(typeof c === "string" ? doc.createTextNode(c) : c);
    });
    return node;
  };
}

module.exports = {
  Element: Element,
  TextNode: TextNode,
  createDocument: createDocument,
  serialize: serialize,
  makeH: makeH
};
