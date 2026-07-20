// The in-page engine mirroring check_condition and dispatch's declarative slice.
// tests/test_tutorial_parity.py gates every branch against the real Python engine.

import { mountAll } from "./dom";
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
  action: "block" | "warn";
  message: string;
}

function compileRegex(pattern: string, flags = ""): RegExp {
  try {
    return new RegExp(pattern, flags);
  } catch {
    throw new SubsetExceeded(pattern);
  }
}

function mcpSuffix(name: string): string {
  return name.startsWith("mcp__") ? name.split("__").pop() ?? name : name;
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

function skillMatches(skill: string, names: string[]): boolean {
  return names.includes(skill) || names.includes(skill.split(":").pop() ?? skill);
}

function prefixEquals(argv: string[], prefix: string[]): boolean {
  return prefix.length <= argv.length && prefix.every((tok, i) => argv[i] === tok);
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
      return ev.file != null && cond.patterns.some((p) => fnmatch(ev.file as string, p));
    case "Content":
      return ev.content != null && compileRegex(cond.pattern, "m").test(ev.content);
    case "UsedSkill":
      return (ev.session?.usedSkills ?? []).some((s) => skillMatches(s, cond.names));
    case "Not":
      return !checkCondition(cond.condition, ev, cl);
    case "Or":
      return cond.conditions.some((sub) => checkCondition(sub, ev, cl));
    case "And":
      return cond.conditions.every((sub) => checkCondition(sub, ev, cl));
  }
}

function combine(fired: Fired[]): Verdict {
  const blocks = fired.filter((f) => f.action === "block").map((f) => f.message);
  const warns = fired.filter((f) => f.action === "warn").map((f) => f.message);
  if (blocks.length > 0) {
    const parts = [...blocks];
    if (warns.length > 0) {
      if (parts.length > 0) parts.push(ADVISORY_SEPARATOR);
      parts.push(...warns);
    }
    return { action: "block", message: parts.join("\n\n") || null, command: null };
  }
  if (warns.length > 0) return { action: "warn", message: warns.join("\n\n"), command: null };
  return { action: "pass", message: null, command: null };
}

export function evaluate(hooks: SerializedHook[], input: EventInput): Verdict {
  const event = input.event ?? "PreToolUse";
  const command = input.command ?? null;
  if (command !== null && detectHonesty(command)) {
    return { action: "subset-exceeded", message: HONESTY_MESSAGE, command: null };
  }
  const ev: EventInput = { ...input, event, tool: input.tool ?? null };
  try {
    const cl = command !== null ? tokenize(command) : null;
    const fired: Fired[] = [];
    for (const hook of hooks) {
      if (!hook.events.includes(event)) continue;
      if (!hook.only_if.every((c) => checkCondition(c, ev, cl))) continue;
      if (hook.skip_if.some((c) => checkCondition(c, ev, cl))) continue;
      if (hook.message == null) continue;
      fired.push({ action: hook.block ? "block" : "warn", message: hook.message });
    }
    return combine(fired);
  } catch (e) {
    if (e instanceof SubsetExceeded) return { action: "subset-exceeded", message: HONESTY_MESSAGE, command: null };
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
