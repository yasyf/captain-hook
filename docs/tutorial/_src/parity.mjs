// CI parity runner: {hooks, cases:[{id, input}]} on stdin -> [{id, verdict}] on stdout,
// each case driven through the committed emulator bundle the browser also loads.

import { fileURLToPath } from "node:url";
import { evaluate } from "../widgets/emulator.js";

const bundle = fileURLToPath(new URL("../widgets/emulator.js", import.meta.url));

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return Buffer.concat(chunks).toString("utf8");
}

const { hooks, cases } = JSON.parse(await readStdin());
const verdicts = cases.map(({ id, input }) => ({ id, verdict: evaluate(hooks, input) }));
process.stdout.write(JSON.stringify({ bundle, verdicts }));
