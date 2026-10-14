// Verify that the Mermaid the renderer emits actually parses.
//
// The renderer builds diagram text by string concatenation, and that text is the
// artefact a human reads to decide whether the agent behaved. Invalid syntax
// renders as an error box, which is worse than no diagram: it looks like the run
// failed when only the drawing did.
//
// This is a Node script rather than a pytest test because it needs mermaid. The
// Python suite stays dependency-light; this runs on demand.
//
//   cd scripts && npm install && npm run verify:mermaid
//
// Or point it at any directory of .mmd files:
//
//   node verify-mermaid.mjs ../artifacts/agent-demo
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));

// --- minimal DOM ----------------------------------------------------------
//
// Mermaid is a browser library. Its module initialiser registers a window load
// listener, and its sanitiser expects DOMPurify to have been configured. Under
// Node neither holds, so both are stubbed before mermaid is imported. This is
// scaffolding for parsing grammar, not for rendering: no SVG is produced.
const listeners = { load: [] };

globalThis.window = {
  addEventListener: (name, fn) => {
    (listeners[name] ||= []).push(fn);
  },
  removeEventListener: () => {},
  location: { href: "http://localhost/", protocol: "http:" },
  document: {
    createElement: () => ({
      style: {},
      setAttribute() {},
      appendChild() {},
      getBBox: () => ({ x: 0, y: 0, width: 0, height: 0 }),
      getBoundingClientRect: () => ({ x: 0, y: 0, width: 0, height: 0 }),
    }),
    createElementNS: () => ({ style: {}, setAttribute() {}, appendChild() {} }),
    body: { appendChild() {}, removeChild() {}, style: {} },
    querySelector: () => null,
    querySelectorAll: () => [],
    getElementById: () => null,
  },
};
globalThis.document = globalThis.window.document;
// Node 21+ exposes a read-only `navigator`, so only define it when absent.
if (!globalThis.navigator) {
  Object.defineProperty(globalThis, "navigator", {
    value: { userAgent: "node" },
    configurable: true,
  });
}

let mermaid;
try {
  ({ default: mermaid } = await import("mermaid"));
} catch (err) {
  console.error("mermaid is not installed. Run:  cd scripts && npm install");
  console.error(String(err?.message ?? err).split("\n")[0]);
  process.exit(2);
}

// DOMPurify resolves to a browser-less shim here, so give it the surface
// mermaid calls. Done after the import so the object exists to patch.
try {
  const { default: dompurify } = await import("dompurify");
  if (typeof dompurify.addHook !== "function") {
    const noop = () => {};
    Object.assign(dompurify, {
      addHook: noop,
      removeHook: noop,
      removeHooks: noop,
      sanitize: (value) => value,
      setConfig: noop,
      clearConfig: noop,
      isValidAttribute: () => true,
    });
  }
} catch {
  // dompurify absent: mermaid may still parse the diagram types we emit.
}

mermaid.initialize({ startOnLoad: false });

// --- run ------------------------------------------------------------------

// Default to the demo artefacts; accept an explicit directory.
const dir = process.argv[2] || path.resolve(here, "..", "artifacts", "agent-demo");

if (!fs.existsSync(dir)) {
  console.error(`directory not found: ${dir}`);
  console.error("Generate it first:  python scripts/demo_agent_run.py");
  process.exit(2);
}

const files = fs.readdirSync(dir).filter((f) => f.endsWith(".mmd")).sort();
if (files.length === 0) {
  console.error(`no .mmd files in ${dir}`);
  process.exit(2);
}

let failed = 0;
for (const file of files) {
  const source = fs.readFileSync(path.join(dir, file), "utf8");
  try {
    const result = await mermaid.parse(source);
    console.log(`PASS  ${file.padEnd(28)} ${result?.diagramType ?? ""}`);
  } catch (err) {
    failed += 1;
    console.log(`FAIL  ${file}`);
    console.log(`      ${String(err?.message ?? err).split("\n").slice(0, 3).join(" | ")}`);
  }
}

console.log(`\n${files.length - failed}/${files.length} parsed`);
process.exit(failed ? 1 : 0);
