// `node --test` suite pinning two divergences an adversarial review found: condition evaluation
// short-circuits like matches_conditions, and a chip is tokenized quote-aware, not on whitespace.

import assert from "node:assert/strict";
import { test } from "node:test";

import { evaluate, parseRanCommand } from "../../widgets/emulator.js";

// Atomic grouping: Python's `re` accepts it, JavaScript's RegExp rejects it, so evaluating it at all
// is observable as a subset-exceeded verdict.
const UNSUPPORTED_REGEX = "(?>a)";

const hook = (overrides) => ({
  events: ["PreToolUse"],
  message: "nope",
  block: true,
  advisory_on_deny: false,
  only_if: [],
  skip_if: [],
  ...overrides,
});

test("only_if stops at the first failure, so a later unsupported regex is never compiled", () => {
  const hooks = [
    hook({ only_if: [{ kind: "Tool", names: ["Write"] }, { kind: "Command", pattern: UNSUPPORTED_REGEX }] }),
  ];
  const verdict = evaluate(hooks, { event: "PreToolUse", tool: "Bash", command: "echo a" });
  assert.equal(verdict.action, "pass");
  assert.deepEqual(verdict.reasons, [
    {
      outcome: "did not apply",
      onlyIfMatched: [],
      onlyIfUnmatched: "no Tool match",
      skipIfMatched: null,
      skipIfDeclared: false,
    },
  ]);
});

test("the same unsupported regex is still refused when only_if actually reaches it", () => {
  const hooks = [
    hook({ only_if: [{ kind: "Tool", names: ["Write"] }, { kind: "Command", pattern: UNSUPPORTED_REGEX }] }),
  ];
  const verdict = evaluate(hooks, { event: "PreToolUse", tool: "Write", command: "echo a" });
  assert.equal(verdict.action, "subset-exceeded");
  assert.match(verdict.message, /capt-hook test/);
});

test("skip_if stops at the first match, so a later unsupported regex is never compiled", () => {
  const hooks = [
    hook({
      skip_if: [{ kind: "UsedSkill", names: ["agent-browser"] }, { kind: "Command", pattern: UNSUPPORTED_REGEX }],
    }),
  ];
  const verdict = evaluate(hooks, {
    event: "PreToolUse",
    tool: "Bash",
    command: "echo a",
    session: { usedSkills: ["agent-browser"] },
  });
  assert.equal(verdict.action, "pass");
  assert.deepEqual(verdict.reasons, [
    {
      outcome: "stood down",
      onlyIfMatched: [],
      onlyIfUnmatched: null,
      skipIfMatched: "UsedSkill agent-browser",
      skipIfDeclared: true,
    },
  ]);
});

test("a hook that applies records every only_if it matched and its own outcome", () => {
  const hooks = [
    hook({
      events: ["Stop"],
      only_if: [{ kind: "TouchedFile", patterns: ["**/src/**"] }],
      skip_if: [{ kind: "UsedSkill", names: ["agent-browser"] }],
    }),
  ];
  const verdict = evaluate(hooks, {
    event: "Stop",
    session: { touchedFiles: ["/repo/src/components/Button.tsx"], repoRoot: "/repo" },
  });
  assert.equal(verdict.action, "block");
  assert.deepEqual(verdict.reasons, [
    {
      outcome: "blocked",
      onlyIfMatched: ["TouchedFile on src/components/Button.tsx"],
      onlyIfUnmatched: null,
      skipIfMatched: null,
      skipIfDeclared: true,
    },
  ]);
});

test("a non-blocking hook that applies reports warned, not blocked", () => {
  const hooks = [hook({ block: false, only_if: [{ kind: "Tool", names: ["Bash"] }] })];
  const verdict = evaluate(hooks, { event: "PreToolUse", tool: "Bash", command: "echo a" });
  assert.equal(verdict.action, "warn");
  assert.equal(verdict.reasons[0].outcome, "warned");
});

for (const [raw, argv] of [
  ['git commit -m "hello world"', ["git", "commit", "-m", "hello world"]],
  ["git commit -m 'hello world'", ["git", "commit", "-m", "hello world"]],
  ["uv run pytest", ["uv", "run", "pytest"]],
  ["CI=1 uv run pytest", ["uv", "run", "pytest"]],
]) {
  test(`ran-command chip tokenizes ${raw}`, () => assert.deepEqual(parseRanCommand(raw), argv));
}

for (const raw of ["echo $(id)", "echo `id`", "uv run pytest && git push", "a; b", "a | b", "echo \\n", ""]) {
  test(`ran-command chip refuses ${JSON.stringify(raw)}`, () => assert.equal(parseRanCommand(raw), null));
}

test("a quoted ran-command chip satisfies the RanCommand skip_if it was typed for", () => {
  const hooks = [
    hook({
      events: ["Stop"],
      only_if: [{ kind: "TouchedFile", patterns: ["**/src/**"] }],
      skip_if: [{ kind: "RanCommand", argv: ["git", "commit", "-m", "hello world"] }],
    }),
  ];
  const verdict = evaluate(hooks, {
    event: "Stop",
    session: {
      touchedFiles: ["/repo/src/components/Button.tsx"],
      ranCommands: [parseRanCommand('git commit -m "hello world"')],
      repoRoot: "/repo",
    },
  });
  assert.equal(verdict.action, "pass");
  assert.equal(verdict.reasons[0].outcome, "stood down");
  assert.equal(verdict.reasons[0].skipIfMatched, "RanCommand git commit -m hello world");
});
