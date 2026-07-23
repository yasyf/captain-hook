// The on-device LLM adapter, isolated into widgets/llm.js so emulator.js never bundles pocket-llm
// or its engines (Chrome's Prompt API, WebLLM, wllama). The core dynamic-imports this by URL on the
// first interaction with an llm widget and calls initLlm; `detect` previews without downloading, and
// `start` is the only path that creates a session (and thus may download) — the core gates it behind
// an explicit click on the download offer.

import { createSession, detect } from "pocket-llm";

import type { LlmAdapter, LlmInitOptions } from "./specs";

export function initLlm(options: LlmInitOptions): LlmAdapter {
  return {
    detect: () => detect({}),
    start: () =>
      createSession({
        system: options.system,
        responseSchema: options.schema as never,
        assets: options.assets,
        ...(options.onProgress ? { onProgress: options.onProgress } : {}),
      }),
  };
}
