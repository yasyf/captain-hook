// The in-page engine mirroring check_condition and dispatch's declarative slice.
// tests/test_tutorial_parity.py gates every branch against the real Python engine.

import { mountAll } from "./dom";

export { caseLabel, selectChips } from "./dom";
import {
  ADVISORY_SEPARATOR,
  Condition,
  EventInput,
  HONESTY_MESSAGE,
  SerializedHook,
  Verdict,
} from "./specs";
import { CommandLine, detectHonesty, tokenize } from "./tokenizer";

class SubsetExceeded extends Error {}

interface Fired {
  action: "block" | "warn" | "rewrite";
  message: string | null;
  rewritten: string | null;
}

function compileRegex(pattern: string, flags = ""): RegExp {
  try {
    return new RegExp(pattern, flags);
  } catch {
    throw new SubsetExceeded(pattern);
  }
}

function pyReplacementToJs(replace: string): string {
  return replace
    .replace(/\$/g, "$$$$")
    .replace(/\\g<([^>]+)>/g, "$<$1>")
    .replace(/\\(\d+)/g, "$$$1");
}

function mcpSuffix(name: string): string {
  return name.startsWith("mcp__") ? (name.split("__").pop() ?? name) : name;
}

function toolMatches(tool: string | null | undefined, names: string[]): boolean {
  if (!tool) return false;
  return names.includes(tool) || names.includes(mcpSuffix(tool));
}

function fnmatchToRegex(glob: string): RegExp {
  let out = "";
  for (let i = 0; i < glob.length; i++) {
    const c = glob[i];
    if (c === "*") out += ".*";
    else if (c === "?") out += ".";
    else if (c === "[") {
      let j = i + 1;
      if (glob[j] === "!") j++;
      if (glob[j] === "]") j++;
      while (j < glob.length && glob[j] !== "]") j++;
      if (j >= glob.length) {
        out += "\\[";
      } else {
        let inner = glob.slice(i + 1, j).replace(/\\/g, "\\\\");
        if (inner[0] === "!") inner = "^" + inner.slice(1);
        out += "[" + inner + "]";
        i = j;
      }
    } else out += c.replace(/[.\\+^$(){}|]/g, "\\$&");
  }
  return new RegExp("(?:" + out + ")$", "s");
}

function fnmatch(path: string, glob: string): boolean {
  const re = fnmatchToRegex(glob);
  const base = path.split("/").pop() ?? path;
  return re.test(path) || re.test(base);
}

function isProjectPath(path: string, repoRoot: string | undefined): boolean {
  if (!path.startsWith("/")) return true;
  if (!repoRoot) return true;
  return path === repoRoot || path.startsWith(repoRoot.endsWith("/") ? repoRoot : `${repoRoot}/`);
}

const LEADING_WRAPPERS = new Set(["sudo", "env", "timeout", "nohup", "command", "time", "xargs"]);
const SHELLS = new Set(["sh", "bash", "dash", "zsh", "ksh"]);

function commandExecutable(argv: string[]): string {
  let i = 0;
  while (i < argv.length && LEADING_WRAPPERS.has(argv[i])) i++;
  return argv[i] ?? "";
}

function hasWrapper(cl: CommandLine): boolean {
  return cl.commands.some((c) => {
    const exe = commandExecutable(c.argv);
    return exe === "eval" || (SHELLS.has(exe) && c.argv.includes("-c"));
  });
}

function prefixEquals(argv: string[], prefix: string[]): boolean {
  return prefix.length <= argv.length && prefix.every((tok, i) => argv[i] === tok);
}

function skillMatches(skill: string, names: string[]): boolean {
  return names.includes(skill) || names.includes(skill.split(":").pop() ?? skill);
}

function checkCondition(cond: Condition, ev: EventInput, cl: CommandLine | null): boolean {
  switch (cond.kind) {
    case "Tool":
      return toolMatches(ev.tool, cond.names);
    case "Command":
      return cl !== null && [cl.raw, ...cl.commands.map((c) => c.text)].some((s) => compileRegex(cond.pattern).test(s));
    case "Runs":
      return cl !== null && cond.argv.length > 0 && cl.commands.some((c) => prefixEquals(c.argv, cond.argv));
    case "FilePath":
      return (
        ev.file != null &&
        (!cond.project_only || isProjectPath(ev.file, ev.session?.repoRoot)) &&
        cond.patterns.some((p) => fnmatch(ev.file as string, p))
      );
    case "Content":
      return (
        ev.content != null &&
        (!cond.project_only || (ev.file != null && isProjectPath(ev.file, ev.session?.repoRoot))) &&
        compileRegex(cond.pattern, "m").test(ev.content)
      );
    case "TouchedFile":
      return (ev.session?.touchedFiles ?? []).some((f) => cond.patterns.some((p) => fnmatch(f, p)));
    case "UsedSkill":
      return (ev.session?.usedSkills ?? []).some((s) => skillMatches(s, cond.names));
    case "RanCommand":
      return (ev.session?.ranCommands ?? []).some((argv) => prefixEquals(argv, cond.argv));
    case "Waiting":
      return ev.session?.waiting ?? false;
    case "Not":
      return !checkCondition(cond.condition, ev, cl);
    case "Or":
      return cond.conditions.some((sub) => checkCondition(sub, ev, cl));
    case "And":
      return cond.conditions.every((sub) => checkCondition(sub, ev, cl));
  }
}

function fire(hook: SerializedHook, command: string | null): Fired | null {
  if (hook.rewrite) {
    if (command === null) return null;
    const re = compileRegex(hook.rewrite.pattern, "g");
    return { action: "rewrite", message: hook.rewrite.note, rewritten: command.replace(re, pyReplacementToJs(hook.rewrite.replace)) };
  }
  if (hook.message == null) return null;
  return { action: hook.block ? "block" : "warn", message: hook.message, rewritten: null };
}

function combine(fired: Fired[]): Verdict {
  const blocks = fired.filter((f) => f.action === "block").map((f) => f.message).filter((m): m is string => m != null);
  const warns = fired.filter((f) => f.action === "warn").map((f) => f.message).filter((m): m is string => m != null);
  if (fired.some((f) => f.action === "block")) {
    const parts = [...blocks];
    if (warns.length > 0) {
      if (parts.length > 0) parts.push(ADVISORY_SEPARATOR);
      parts.push(...warns);
    }
    return { action: "block", message: parts.join("\n\n") || null, rewritten: null };
  }
  const rewrite = fired.find((f) => f.action === "rewrite");
  if (rewrite) {
    const notes = [...(rewrite.message ? [rewrite.message] : []), ...warns];
    return { action: "rewrite", message: notes.join("\n\n") || null, rewritten: rewrite.rewritten };
  }
  if (warns.length > 0) return { action: "warn", message: warns.join("\n\n"), rewritten: null };
  return { action: "pass", message: null, rewritten: null };
}

export function evaluate(hooks: SerializedHook[], input: EventInput): Verdict {
  const event = input.event ?? "PreToolUse";
  const command = input.command ?? null;
  if (command !== null && detectHonesty(command)) {
    return { action: "subset-exceeded", message: HONESTY_MESSAGE, rewritten: null };
  }
  const ev: EventInput = { ...input, event, tool: input.tool ?? null };
  try {
    const cl = command !== null ? tokenize(command) : null;
    if (cl !== null && hasWrapper(cl)) {
      return { action: "subset-exceeded", message: HONESTY_MESSAGE, rewritten: null };
    }
    const fired: Fired[] = [];
    for (const hook of hooks) {
      if (!hook.events.includes(event)) continue;
      if (!hook.only_if.every((c) => checkCondition(c, ev, cl))) continue;
      if (hook.skip_if.some((c) => checkCondition(c, ev, cl))) continue;
      const result = fire(hook, command);
      if (result) fired.push(result);
    }
    return combine(fired);
  } catch (e) {
    if (e instanceof SubsetExceeded) return { action: "subset-exceeded", message: HONESTY_MESSAGE, rewritten: null };
    throw e;
  }
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => mountAll(evaluate));
  } else {
    mountAll(evaluate);
  }
}
