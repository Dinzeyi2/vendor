// Runs INSIDE the sandbox (started by runner.node, after lockdown), next to untrusted package code.
// Reads one JSON payload on stdin, prints one JSON line on stdout.
//   discover: load the package, record the calls its README examples make (with `//=> value`
//             notes as expected results), choose the main function, infer inputs, test it.
//   call:     run one imported function on JSON inputs (the production path).
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const payload = JSON.parse(fs.readFileSync(0, "utf8"));
const realLog = console.log.bind(console);
console.log = console.info = console.warn = console.debug = (...a) => process.stderr.write(a.map(String).join(" ") + "\n");
const emit = (obj) => { process.stdout.write("\n" + JSON.stringify(obj) + "\n"); process.exit(0); };
const fail = (code, reason, extra = {}) => emit({ ok: false, code, reason: String(reason).slice(0, 900),
  error: String(reason).slice(0, 900), error_type: extra.error_type || "operation_failed", ...extra });

async function loadPackage() {
  const dir = payload.package_dir;
  const candidates = [payload.main, "index.js", "index.mjs", "index.cjs", "dist/index.js", "lib/index.js"]
    .filter(Boolean).map((m) => path.resolve(dir, m));
  for (const file of candidates) {
    for (const f of [file, file + ".js", path.join(file, "index.js")]) {
      if (fs.existsSync(f) && fs.statSync(f).isFile()) {
        const ns = await import(pathToFileURL(f).href);
        return { ns, file: f };
      }
    }
  }
  throw new Error("no entry file found (tried " + candidates.map((c) => path.relative(dir, c)).join(", ") + ")");
}

const isClass = (fn) => /^class[\s{]/.test(Function.prototype.toString.call(fn));
function exportsOf(ns) {
  const out = {};
  const def = ns.default;
  if (typeof def === "function" && !isClass(def)) out.default = def;
  for (const [k, v] of Object.entries(ns)) if (k !== "default" && typeof v === "function" && !isClass(v)) out[k] = v;
  if (def && typeof def === "object") for (const [k, v] of Object.entries(def))
    if (typeof v === "function" && !isClass(v) && !(k in out)) out[k] = v;
  return out;
}

function paramNames(fn) {
  const src = Function.prototype.toString.call(fn);
  const m = src.match(/^[^(]*\(([^)]*)\)/) || src.match(/^\s*(?:async\s+)?([A-Za-z_$][\w$]*)\s*=>/);
  if (!m) return [];
  return m[1].split(",").map((p) => p.trim().replace(/=.*$/s, "").replace(/^\.\.\./, "").trim())
    .map((p, i) => (/^[A-Za-z_$][\w$]*$/.test(p) ? p : (p.startsWith("{") ? "options" : `arg${i}`)))
    .filter((p, i, a) => p && a.indexOf(p) === i);
}

function toJSON(v, depth = 0) {
  if (depth > 20) throw new Error("the result is nested too deeply");
  if (v === null || typeof v === "string" || typeof v === "boolean") return v;
  if (typeof v === "number") { if (!Number.isFinite(v)) throw new Error(`the result contains ${v}`); return v; }
  if (typeof v === "bigint") return v.toString();
  if (v === undefined) throw new Error("the function returned nothing");
  if (v instanceof Date) return v.toISOString();
  if (v instanceof Map) return Object.fromEntries([...v].map(([k, x]) => [String(k), toJSON(x, depth + 1)]));
  if (v instanceof Set) return [...v].map((x) => toJSON(x, depth + 1));
  if (ArrayBuffer.isView(v)) return { base64: Buffer.from(v.buffer, v.byteOffset, v.byteLength).toString("base64") };
  if (Array.isArray(v)) return v.map((x) => toJSON(x, depth + 1));
  if (typeof v === "object") {
    if (typeof v.toJSON === "function") return toJSON(v.toJSON(), depth + 1);
    const o = {};
    for (const [k, x] of Object.entries(v)) if (typeof x !== "function" && x !== undefined) o[k] = toJSON(x, depth + 1);
    return o;
  }
  throw new Error(`the result is a ${typeof v}, which can't be converted to JSON`);
}

function schemaFor(v) {
  if (typeof v === "string") return { type: "string" };
  if (typeof v === "boolean") return { type: "boolean" };
  if (typeof v === "number") return Number.isInteger(v) ? { type: "integer" } : { type: "number" };
  if (Array.isArray(v)) {
    if (v.length && v.every((x) => typeof x === "number")) return { type: "array", items: { type: "number" } };
    if (v.length && v.every((x) => typeof x === "string")) return { type: "array", items: { type: "string" } };
    return { type: "array" };
  }
  if (v && typeof v === "object" && Object.getPrototypeOf(v) === Object.prototype) return { type: "object" };
  return null;
}

function same(a, b) {
  if (typeof a === "number" && typeof b === "number") return Math.abs(a - b) <= 1e-9 + 1e-6 * Math.abs(b);
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((x, i) => same(x, b[i]));
  if (a && b && typeof a === "object" && typeof b === "object") {
    const ka = Object.keys(a), kb = Object.keys(b);
    return ka.length === kb.length && ka.every((k) => same(a[k], b[k]));
  }
  return a === b;
}

// README examples ---------------------------------------------------------------------------------
function blocks(readme) {
  const out = [];
  const re = /```(js|javascript|mjs|cjs|jsx|ts|typescript)?[^\n]*\n([\s\S]*?)```/g;
  let m;
  while ((m = re.exec(readme))) if (m[1] || /\w\(/.test(m[2])) out.push({ ts: /^t/.test(m[1] || ""), code: m[2] });
  return out.slice(0, 30);
}

function rewrite(code, names) {
  // imports/requires of the package itself -> the instrumented exports; other imports kept as dynamic imports
  const pkgRe = names.map((n) => n.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&")).join("|");
  const isPkg = new RegExp(`^(?:${pkgRe})(?:/.*)?$`);
  code = code.replace(/^\s*import\s+(type\s+)?([\s\S]*?)\s+from\s+['"]([^'"]+)['"];?/gm, (all, type, what, src) => {
    if (type) return "";
    const target = isPkg.test(src) ? "globalThis.__cognex.pkg" : `(await import(${JSON.stringify(src)}))`;
    const parts = [];
    let rest = what.trim();
    const ns = rest.match(/^\*\s+as\s+([\w$]+)$/);
    if (ns) return `const ${ns[1]} = ${target};`;
    const def = rest.match(/^([\w$]+)\s*(?:,|$)/);
    if (def) { parts.push(`const ${def[1]} = ${target}.default ?? ${target};`); rest = rest.slice(def[0].length).trim(); }
    const named = rest.match(/^\{([\s\S]*)\}$/);
    if (named) parts.push(`const {${named[1].replace(/\bas\b/g, ":").replace(/\btype\s+[\w$]+\s*,?/g, "")}} = ${target};`);
    return parts.join(" ");
  });
  code = code.replace(/require\(\s*['"]([^'"]+)['"]\s*\)/g, (all, src) =>
    isPkg.test(src) ? "(globalThis.__cognex.pkg.default ?? globalThis.__cognex.pkg)" : all);
  // `//=> value` notes: set the expectation for the statement just before them
  const lines = code.split("\n");
  const out = [];
  for (let i = 0; i < lines.length; i++) {
    const note = lines[i + 1] && lines[i + 1].match(/^\s*\/\/\s*=>\s*(.+)$/);
    const inline = lines[i].match(/^(.*?);?\s*\/\/\s*=>\s*(.+)$/);
    if (inline && !/^\s*\/\//.test(lines[i])) {
      out.push(`globalThis.__cognex.expect = ${JSON.stringify(inline[2].trim())}; ${inline[1]}; globalThis.__cognex.expect = null;`);
    } else if (note && !/^\s*\/\//.test(lines[i])) {
      out.push(`globalThis.__cognex.expect = ${JSON.stringify(note[1].trim())}; ${lines[i]}; globalThis.__cognex.expect = null;`);
      i++;
    } else out.push(lines[i]);
  }
  return out.join("\n");
}

function parseExpect(text) {
  if (!text) return { has: false };
  try { return { has: true, value: toJSON(Function(`"use strict"; return (${text});`)()) }; } catch { return { has: false }; }
}

async function discover() {
  let loaded;
  try { loaded = await loadPackage(); } catch (e) { fail("import_failed", "The package couldn't be loaded: " + (e && e.message)); }
  const fns = exportsOf(loaded.ns);
  if (!Object.keys(fns).length) fail("no_callable_function", "The package exports no functions to call.");
  const calls = {};
  const wrap = (name, fn) => new Proxy(fn, { apply(t, self, args) {
    const entry = { args, expect: globalThis.__cognex.expect };
    const r = Reflect.apply(t, self, args);
    const keep = (v) => { entry.result = v; (calls[name] ||= []).length < 6 && calls[name].push(entry); return v; };
    return r && typeof r.then === "function" ? r.then(keep) : keep(r);
  } });
  const pkg = {};
  for (const [k, v] of Object.entries(loaded.ns)) pkg[k] = fns[k] ? wrap(k, v) : v;
  if (fns.default) {
    const d = wrap("default", fns.default);
    for (const [k, v] of Object.entries(fns)) if (k !== "default" && loaded.ns.default && loaded.ns.default[k] === v) d[k] = pkg[k] = wrap(k, v);
    pkg.default = d;
  }
  globalThis.__cognex = { pkg, expect: null };
  const names = [payload.package_name, payload.repo_name].filter(Boolean);
  let n = 0;
  for (const b of blocks(payload.readme || "")) {
    const file = path.join(payload.scratch, `example${n++}.${b.ts ? "mts" : "mjs"}`);
    fs.writeFileSync(file, rewrite(b.code, names));
    try { await import(pathToFileURL(file).href); } catch { /* examples that need files, network or other packages */ }
  }
  const norm = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9]/g, "");
  const keys = new Set(names.map((s) => norm(String(s).split("/").pop())));
  const scored = Object.entries(fns).map(([name, fn]) => {
    const ok = (calls[name] || []).filter((c) => { try { toJSON(c.result); return c.args.every((a) => schemaFor(a)); } catch { return false; } });
    let score = (keys.has(norm(name)) || name === "default" ? 5 : 0) + (ok.length ? 3 : 0) +
      (new RegExp(`\\b${name}\\(`).test(payload.readme || "") ? 1 : 0);
    if (/^(read|write|load|save|open|fetch|print|log|debug|main|cli|serve|setup|install|configure|test|render)/i.test(name)) score -= 4;
    return { name, fn, ok, score };
  }).sort((a, b) => b.score - a.score);
  let chosen;
  if (payload.entry_point) {
    chosen = scored.find((c) => c.name === payload.entry_point);
    if (!chosen) fail("entry_point_not_found", `'${payload.entry_point}' isn't an exported function. Exported: ` + scored.map((c) => c.name).join(", "));
  } else {
    const usable = scored.filter((c) => c.ok.length);
    if (!usable.length) fail("needs_configuration", "No README example calls an exported function with JSON-compatible " +
      "arguments, so Cognex can't infer the inputs. Exported functions: " + scored.slice(0, 12).map((c) => c.name).join(", "),
      { candidates: scored.slice(0, 10).map((c) => c.name) });
    const top = usable[0], rivals = usable.slice(1).filter((c) => c.score >= top.score - 1);
    if (rivals.length || top.score < 2) fail("multiple_entry_points", "Several exported functions look like the main " +
      "one: " + [top, ...rivals].slice(0, 6).map((c) => c.name + "()").join(", ") + ". Set entry_point.",
      { candidates: [top, ...rivals].slice(0, 10).map((c) => c.name) });
    chosen = top;
  }
  if (!chosen.ok.length) fail("needs_configuration", `No README example calls ${chosen.name}() with JSON-compatible arguments.`);
  const call = chosen.ok[0];
  const params = paramNames(chosen.fn);
  while (params.length < call.args.length) params.push(`arg${params.length}`);
  const used = params.slice(0, call.args.length);
  const properties = {}, sample = {};
  used.forEach((p, i) => { properties[p] = schemaFor(call.args[i]); sample[p] = toJSON(call.args[i]); });
  const exp = parseExpect(call.expect);
  let output;
  try { output = toJSON(await chosen.fn(...used.map((p) => sample[p]))); } catch (e) { fail("test_failed", `Test call ${chosen.name}(${JSON.stringify(Object.values(sample)).slice(1, -1).slice(0, 200)}) failed: ${e && e.message}`); }
  if (exp.has && !same(output, exp.value)) fail("test_failed", `Test call returned ${JSON.stringify(output).slice(0, 200)} but the README says ${JSON.stringify(exp.value).slice(0, 200)}.`);
  emit({ ok: true, function: chosen.name, params: used, input_schema: { type: "object", properties, required: used },
    sample_inputs: sample, sample_output: { result: output }, expected_checked: exp.has,
    entry: path.relative(payload.package_dir, loaded.file), candidates: scored.slice(0, 10).map((c) => ({ name: c.name, score: c.score })) });
}

function fill(t, inputs) {
  if (typeof t === "string") { const m = t.match(/^\{([A-Za-z_]\w*)\}$/); return m ? inputs[m[1]] : t.replace(/\{([A-Za-z_]\w*)\}/g, (a, k) => String(inputs[k] ?? "")); }
  if (Array.isArray(t)) return t.map((x) => fill(x, inputs));
  if (t && typeof t === "object") return Object.fromEntries(Object.entries(t).map(([k, v]) => [k, fill(v, inputs)]));
  return t;
}

async function call() {
  const spec = payload.spec, inputs = payload.inputs;
  let loaded;
  try { loaded = await loadPackage(); } catch (e) { fail("import_failed", "the imported code couldn't be loaded: " + (e && e.message)); }
  let fn = spec.function === "default" ? (loaded.ns.default ?? loaded.ns) : (loaded.ns[spec.function] ?? (loaded.ns.default || {})[spec.function]);
  for (const part of (spec.path || [])) fn = fn && fn[part];
  if (typeof fn !== "function") fail("import_failed", `'${spec.function}' is not an exported function`);
  const args = spec.positional ? fill(spec.positional, inputs) : (spec.params || []).map((p) => inputs[p]);
  while (args.length && args[args.length - 1] === undefined) args.pop();
  let result;
  try { result = await fn(...args); } catch (e) { fail("operation_failed", (e && e.name ? e.name + ": " : "") + (e && e.message)); }
  try { emit({ ok: true, output: { result: toJSON(result) } }); } catch (e) { fail("unsupported_output", e.message); }
}

(payload.mode === "discover" ? discover() : call()).catch((e) => fail("internal_error", e && e.stack || e));
