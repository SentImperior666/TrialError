/* ============================================================================
   Run one renderer function from static/*_render.js under Node, print the tree.

   The Python side (tests/test_dashboard_console_render.py) shells out to this
   and asserts on the JSON it prints, so the whole DOM-level test suite needs
   Node and nothing else -- no npm install, no headless browser, no package.json.

   USAGE
     node tests/_console_render_harness.js --fn <name> [options]

       --module <file>     which static/*_render.js to load and which global it
                           exports; default "console_render.js" -> TEConsole.
       --fn <name>         function to call. Looked up on the object create()
                           returns first, then on the module global itself, so
                           both `renderInto` (bound) and `rowButton` (h-first,
                           unbound) are reachable by their own names.
       --args <json>       arguments as a JSON array, on the command line.
       --args-file <path>  the same, read from a file (use this for fixtures;
                           Windows command lines mangle nested quotes).
       --fixture <path>    sugar for a single argument read from a JSON file.
       --select <css>      run the shim's querySelectorAll over the result and
                           print those nodes instead of the whole tree.

   Two markers inside the arguments stand in for things JSON cannot carry:
   {"__targets": [names]} becomes a map of fresh container elements (reported
   back under "targets"), and {"__fn": "name"} becomes a callback.

   OUTPUT (one JSON object on stdout)
     {"kind": "node",  "tree": {...}}                a DOM node came back
     {"kind": "nodes", "trees": [...]}               --select was given
     {"kind": "value", "value": <json>}              anything else (including
                                                     the array renderInto returns)
     {"kind": "error", "error": "...", "stack": "..."}   and exit code 1

   The tree shape is tests/_dom_shim.js's serialize():
     {tag, attrs, classes, text, listeners, children}
   ============================================================================ */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const shim = require("./_dom_shim.js");

const STATIC_DIR = path.join(__dirname, "..", "trialerror", "dashboard", "static");

/** Which global each render file attaches itself to. Adding evidence_render.js
 *  / feed_render.js later is one line each. */
const MODULE_GLOBALS = {
  "console_render.js": "TEConsole",
  "evidence_render.js": "TEEvidence",
  "feed_render.js": "TEFeed"
};

function parseArgv(argv) {
  const out = { module: "console_render.js", fn: null, args: null, select: null };
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    const next = () => {
      i += 1;
      if (i >= argv.length) throw new Error(`${flag} needs a value`);
      return argv[i];
    };
    if (flag === "--module") out.module = next();
    else if (flag === "--fn") out.fn = next();
    else if (flag === "--select") out.select = next();
    else if (flag === "--args") out.args = JSON.parse(next());
    else if (flag === "--args-file") out.args = JSON.parse(fs.readFileSync(next(), "utf8"));
    else if (flag === "--fixture") out.args = [JSON.parse(fs.readFileSync(next(), "utf8"))];
    else throw new Error(`unknown flag ${flag}`);
  }
  if (!out.fn) throw new Error("--fn is required");
  if (!Object.prototype.hasOwnProperty.call(MODULE_GLOBALS, out.module)) {
    throw new Error(`unknown render module ${out.module} (add it to MODULE_GLOBALS)`);
  }
  if (out.args === null) out.args = [];
  if (!Array.isArray(out.args)) throw new Error("--args / --args-file must be a JSON array");
  return out;
}

/** Load a render file into its own vm context with the shim standing in for the
 *  browser. The file attaches itself to the context's global, exactly as it
 *  attaches to `window` in a real page -- which is the point: the harness
 *  exercises the SHIPPED file, byte for byte, not a copy. */
function loadRenderModule(fileName, doc) {
  const src = fs.readFileSync(path.join(STATIC_DIR, fileName), "utf8");
  const sandbox = { console, document: doc, JSON, Math, Date, Object, Array, String, Number };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox, { filename: fileName });
  const globalName = MODULE_GLOBALS[fileName];
  const api = sandbox[globalName];
  if (!api) throw new Error(`${fileName} did not define ${globalName} on its global`);
  return api;
}

function main() {
  const opts = parseArgv(process.argv.slice(2));
  const doc = shim.createDocument();
  const h = shim.makeH(doc);

  const api = loadRenderModule(opts.module, doc);
  const renderer = typeof api.create === "function" ? api.create({ h: h, doc: doc }) : null;

  let fn = renderer && typeof renderer[opts.fn] === "function" ? renderer[opts.fn] : null;
  let thisArg = renderer;
  let args = opts.args;
  if (!fn) {
    if (typeof api[opts.fn] !== "function") {
      throw new Error(`${MODULE_GLOBALS[opts.module]} has no function ${JSON.stringify(opts.fn)}`);
    }
    // module-level primitives take the element helper as their first argument
    fn = api[opts.fn];
    thisArg = api;
    args = [h].concat(opts.args);
  }

  // JSON cannot carry an element or a callback, so two markers stand in for
  // them (both recursive, one level of object nesting is enough for every
  // renderer signature so far):
  //   {"__targets": ["session", ...]}  -> {name: <fresh container element>}
  //   {"__fn": "<tag>"}                -> a recording function; the tree the
  //                                       harness prints lists the event names
  //                                       a node has listeners for, which is
  //                                       how a test asserts "this row is
  //                                       clickable" without firing anything.
  let targetNodes = null;
  const materialize = (value) => {
    if (!value || typeof value !== "object") return value;
    if (Array.isArray(value)) return value.map(materialize);
    if (value.__targets) {
      targetNodes = {};
      value.__targets.forEach((name) => { targetNodes[name] = doc.createElement("div"); });
      return targetNodes;
    }
    if (Object.prototype.hasOwnProperty.call(value, "__fn")) {
      const fn = function () { fn.calls += 1; };
      fn.calls = 0;
      return fn;
    }
    const out = {};
    Object.keys(value).forEach((k) => { out[k] = materialize(value[k]); });
    return out;
  };
  args = args.map(materialize);

  const result = fn.apply(thisArg, args);

  if (opts.select) {
    const root = result && result.nodeType === 1 ? result : doc;
    process.stdout.write(JSON.stringify({
      kind: "nodes",
      trees: root.querySelectorAll(opts.select).map(shim.serialize)
    }));
    return;
  }
  if (result && result.nodeType) {
    process.stdout.write(JSON.stringify({ kind: "node", tree: shim.serialize(result) }));
    return;
  }
  const payload = { kind: "value", value: result === undefined ? null : result };
  if (targetNodes) {
    payload.targets = {};
    Object.keys(targetNodes).forEach((name) => { payload.targets[name] = shim.serialize(targetNodes[name]); });
  }
  process.stdout.write(JSON.stringify(payload));
}

try {
  main();
} catch (err) {
  process.stdout.write(JSON.stringify({ kind: "error", error: String(err && err.message || err), stack: String(err && err.stack || "") }));
  process.exitCode = 1;
}
