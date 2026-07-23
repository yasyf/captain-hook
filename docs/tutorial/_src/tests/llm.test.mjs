// `node --test` adapter-wiring suite for widgets/llm.js, run against the llm.ts source with
// pocket-llm's module surface stubbed — no real on-device engine, no network. The pytest wrapper
// passes --experimental-strip-types (native TS import) and --experimental-test-module-mocks.

import assert from "node:assert/strict";
import { beforeEach, mock, test } from "node:test";

const calls = { detect: [], create: [] };
const session = {
  prompt: async () => ({ block: true, reasoning: "drops a table with no backup" }),
  destroy: async () => {},
};

mock.module("pocket-llm", {
  namedExports: {
    detect: async (options) => {
      calls.detect.push(options);
      return { lane: "wllama", availability: "needs-download", model: "SmolLM2-135M", downloadBytes: 105_454_144 };
    },
    createSession: async (options) => {
      calls.create.push(options);
      return session;
    },
  },
});

const { initLlm } = await import("../llm.ts");

const OPTS = {
  system: "You review database migrations for irreversible data loss.",
  schema: { type: "object", properties: { block: { type: "boolean" }, reasoning: { type: "string" } } },
  assets: { wllama: { default: "https://docs.example/widgets/wllama/wllama.wasm" } },
  onProgress: () => {},
};

beforeEach(() => {
  calls.detect = [];
  calls.create = [];
});

test("detect() previews the lane without ever creating a session", async () => {
  const detection = await initLlm(OPTS).detect();
  assert.equal(detection.lane, "wllama");
  assert.equal(detection.availability, "needs-download");
  assert.equal(detection.downloadBytes, 105_454_144);
  assert.equal(calls.create.length, 0, "createSession must not run during detect — that is the download gate");
});

test("start() passes system, schema, and assets through to createSession", async () => {
  const adapter = initLlm(OPTS);
  await adapter.detect();
  assert.equal(calls.create.length, 0, "detect() alone never creates a session");
  const live = await adapter.start();
  assert.equal(calls.create.length, 1, "start() creates exactly one session");
  const passed = calls.create[0];
  assert.equal(passed.system, OPTS.system);
  assert.equal(passed.responseSchema, OPTS.schema);
  assert.deepEqual(passed.assets, OPTS.assets);
  assert.equal(typeof passed.onProgress, "function");
  assert.equal(typeof live.prompt, "function");
  assert.equal(typeof live.destroy, "function");
});

test("initLlm without onProgress omits the key from createSession options", async () => {
  const { onProgress: _drop, ...noProgress } = OPTS;
  await initLlm(noProgress).start();
  assert.equal("onProgress" in calls.create[0], false, "no onProgress key when the caller supplies none");
});
