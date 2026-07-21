// `node --test` unit suite pinning the compiler.js refusal surface; driven by a pytest wrapper.

import assert from "node:assert/strict";
import { test } from "node:test";

import { compileSource } from "../../widgets/compiler.js";

test("refuses syntactically broken source with a syntax-error node", () => {
  for (const src of ["gate(", ")", "block_command(["]) {
    const result = compileSource(src);
    assert.ok("error" in result, `expected refusal, got ${JSON.stringify(result)}`);
    assert.match(result.error, /syntax error/);
  }
});

test("names the refused construct precisely for def / class / f-string", () => {
  assert.match(compileSource("def f():\n    return 1\n").error, /function def/);
  assert.match(compileSource("class C:\n    x = 1\n").error, /class definition/);
  assert.match(compileSource('block_command("git stash", reason=f"blocked {x}")').error, /f-string/);
});

test("ignores a tests={...} kwarg — identical output with and without it", () => {
  const without = compileSource('block_command(["git", "stash"], reason="no stash")');
  const withTests = compileSource(
    'block_command(["git", "stash"], reason="no stash", tests={Input(command="git stash"): Block()})',
  );
  assert.ok("hooks" in without, `expected hooks, got ${JSON.stringify(without)}`);
  assert.deepEqual(withTests, without);
});

test("lowers triple-quoted string literals verbatim", () => {
  assert.equal(compileSource('hook(Event.PreToolUse, """be careful""")').hooks[0].message, "be careful");
  assert.equal(
    compileSource('hook(Event.PreToolUse, """she said "hi" today""")').hooks[0].message,
    'she said "hi" today',
  );
  assert.equal(compileSource('hook(Event.PreToolUse, """line one\nline two""")').hooks[0].message, "line one\nline two");
});
