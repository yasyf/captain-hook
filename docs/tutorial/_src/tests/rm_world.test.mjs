// `node --test` suite pinning the rm_walk world engine: every construct an adversarial review
// found diverging from the real guard_rm now returns the honesty card or the correct verdict.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { evaluateRmWorld } from "../../widgets/emulator.js";

const world = JSON.parse(readFileSync(fileURLToPath(new URL("../matrix.json", import.meta.url)), "utf8")).widgets
  .rm_walk.world;

const assertHonesty = (command) => {
  const verdict = evaluateRmWorld(world, command);
  assert.equal(verdict.action, "subset-exceeded", `${command} → ${JSON.stringify(verdict)}`);
  assert.match(verdict.message ?? "", /capt-hook test/);
  assert.equal(verdict.rewritten, null);
};

// Finding 1 — a redirect operand cannot be faithfully placed in a rewrite.
for (const command of [
  "rm foo.txt > out.log",
  "rm foo.txt 2>/dev/null",
  "rm foo.txt >>out.log",
  "rm foo.txt &>/tmp/out",
  "rm foo.txt < in.txt",
  "rm /tmp/x.py > out.log",
]) {
  test(`redirect → honesty: ${command}`, () => assertHonesty(command));
}

// Finding 2 — a wrapper whose argument pushes rm out of head position is un-modelable arity.
for (const command of [
  "sudo -u root rm -rf /",
  "timeout 5 rm -rf /",
  "env -i rm -rf /",
  "xargs -n1 rm -rf /",
  "sudo -- rm -rf /",
  "nice -n 5 rm -rf /",
]) {
  test(`wrapper arity → honesty: ${command}`, () => assertHonesty(command));
}

// Finding 3 — subshells and brace groups are not descended into.
for (const command of ["( rm -rf / )", "(rm -rf /)", "{ rm -rf /; }", "cat foo && ( rm notes.md )"]) {
  test(`grouping → honesty: ${command}`, () => assertHonesty(command));
}

// Finding 5 — a `time` reserved word timing an rm is span-un-modelable.
test("time rm → honesty", () => assertHonesty("time rm foo.txt"));

// Finding 6 — a bare `/run/user/*` is not scratch without the uid segment.
test("run/user → honesty", () => assertHonesty("rm /run/user/x.py"));

// Finding 7 — the recovery-note filename for a >=2-recoverable glob is scandir-order-dependent.
test("multi-recoverable glob → honesty", () => assertHonesty("rm src/*"));

const FS_ROOT_BLOCK =
  "BLOCKED: '/' is the filesystem root — deleting it destroys the entire system. " +
  "If this is really intended, ask the user to run it themselves.";

// Finding 2 — the completed wrapper set (doas/exec/nice) now reaches rm and blocks, byte-equal.
for (const command of ["exec rm -rf /", "nice rm -rf /", "doas rm -rf /"]) {
  test(`completed wrapper → block: ${command}`, () => {
    assert.deepEqual(evaluateRmWorld(world, command), { action: "block", message: FS_ROOT_BLOCK, rewritten: null });
  });
}

const recoverableNote = (token) =>
  `Rewrote rm to trash: '${token}' resolves outside any git/jj repository, so rm would be ` +
  "unrecoverable. The targets were moved to the macOS Trash instead — restorable via Finder (Put Back). " +
  "If permanent deletion is truly intended, ask the user to run the rm themselves.";

// Finding 4 — a `-`-leading operand after `--` is a real target, not a flag (faithful, byte-equal).
test("terminator keeps dash-leading operand", () => {
  assert.deepEqual(evaluateRmWorld(world, "rm -- -foo.txt"), {
    action: "rewrite",
    message: recoverableNote("-foo.txt"),
    rewritten: "/usr/bin/trash ./-foo.txt",
  });
});

test("terminator mid-args drops -- and keeps operands", () => {
  assert.deepEqual(evaluateRmWorld(world, "rm foo.txt -- notes.md"), {
    action: "rewrite",
    message: recoverableNote("foo.txt"),
    rewritten: "/usr/bin/trash foo.txt notes.md",
  });
});

test("bare -- with no operands passes", () => {
  assert.deepEqual(evaluateRmWorld(world, "rm --"), { action: "pass", message: null, rewritten: null });
});
