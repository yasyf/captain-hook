// CI parity runner over the committed browser bundles. `mode:"compile"` drives widgets/compiler.js;
// otherwise the {hooks, cases} shape drives widgets/emulator.js like the browser does.

import { fileURLToPath } from "node:url";
import { evaluate, evaluateRmWorld } from "../widgets/emulator.js";

const bundle = fileURLToPath(new URL("../widgets/emulator.js", import.meta.url));

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return Buffer.concat(chunks).toString("utf8");
}

const request = JSON.parse(await readStdin());

if (request.mode === "compile") {
  const { compileSource } = await import("../widgets/compiler.js");
  process.stdout.write(JSON.stringify(compileSource(request.source)));
} else if (request.mode === "world") {
  process.stdout.write(JSON.stringify({ verdict: evaluateRmWorld(request.world, request.input.command) }));
} else {
  const { hooks, cases } = request;
  const verdicts = cases.map(({ id, input }) => ({ id, verdict: evaluate(hooks, input) }));
  process.stdout.write(JSON.stringify({ bundle, verdicts }));
}
