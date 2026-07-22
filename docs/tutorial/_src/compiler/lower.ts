// Lowers the accepted tree to the same SerializedHook[] widget_compiler.serialize_hook emits,
// mirroring each primitive's Python lowering and refusing the features it cannot model.

import type { SyntaxNode, Tree } from "@lezer/common";

import type { Condition, SerializedHook } from "../specs";
import TOOL_ALIASES from "../generated/tool_aliases.json";
import { CompileError } from "./validate";

const ALIASES: Record<string, string[]> = TOOL_ALIASES;

// Event.Flag iteration order (definition order), so `A | B` serializes like the Python enum.
const EVENT_ORDER = [
  "PreToolUse",
  "PostToolUse",
  "PostToolUseFailure",
  "UserPromptSubmit",
  "Stop",
  "SubagentStop",
  "SubagentStart",
  "PreCompact",
  "Notification",
  "SessionStart",
  "SessionEnd",
  "PermissionRequest",
];

// captain_hook.primitives.commands.block_command_pattern re.escape set.
const RE_SPECIAL = new Set("()[]{}?*+-|^$\\.&~# \t\n\r\x0b\x0c");

// widget_compiler.check_regex_dialect: Python-only regex syntax with no JS equivalent.
const INLINE_FLAG = /\(\?[aiLmsux]+[):]/;
const FORBIDDEN_REGEX = ["(?P<", "(?P=", "\\A", "\\Z"];

// captain_hook.ast_grep.TEMPLATE_VAR: a $NAME / $$$NAME metavar marks a structural rewrite.
const TEMPLATE_VAR = /\$\$\$[A-Z_][A-Z0-9_]*|\$[A-Z_][A-Z0-9_]*/;

interface Signature {
  maxPositional: number;
  keywords: Set<string>;
}

// Max positional slots + accepted keywords per captain_hook.primitives signature.
const PRIMITIVE_SIGS: Record<string, Signature> = {
  hook: {
    maxPositional: 2,
    keywords: new Set([
      "events", "message", "only_if", "skip_if", "block",
      "advisory_on_deny", "respect_gitignore", "max_fires", "tests", "async_", "skip_planning_agents",
    ]),
  },
  block_command: {
    maxPositional: 1,
    keywords: new Set(["pattern", "reason", "hint", "only_if", "skip_if", "tests"]),
  },
  warn_command: {
    maxPositional: 1,
    keywords: new Set(["pattern", "message", "only_if", "skip_if", "tests", "events"]),
  },
  rewrite_command: {
    maxPositional: 2,
    keywords: new Set(["pattern", "replace", "only_if", "skip_if", "to", "block", "note", "tests"]),
  },
  gate: {
    maxPositional: 1,
    keywords: new Set([
      "message", "when", "signals", "only_if", "skip_if",
      "events", "max_fires", "tests", "async_", "skip_planning_agents",
    ]),
  },
  nudge: {
    maxPositional: 1,
    keywords: new Set([
      "message", "when", "signals", "only_if", "skip_if", "block",
      "advisory_on_deny", "events", "max_fires", "tests", "async_", "skip_planning_agents",
    ]),
  },
};

// Same, per captain_hook.types condition signature (varargs conditions take unbounded positionals).
const CONDITION_SIGS: Record<string, Signature> = {
  Tool: { maxPositional: Infinity, keywords: new Set() },
  Command: { maxPositional: 1, keywords: new Set(["pattern"]) },
  Runs: { maxPositional: Infinity, keywords: new Set() },
  FilePath: { maxPositional: Infinity, keywords: new Set(["project_only"]) },
  Content: { maxPositional: 2, keywords: new Set(["pattern", "project_only"]) },
  TouchedFile: { maxPositional: Infinity, keywords: new Set(["subagents"]) },
  UsedSkill: { maxPositional: Infinity, keywords: new Set(["subagents", "scope"]) },
  RanCommand: { maxPositional: Infinity, keywords: new Set(["subagents"]) },
  Waiting: { maxPositional: 0, keywords: new Set() },
  Not: { maxPositional: 1, keywords: new Set(["condition"]) },
  Or: { maxPositional: Infinity, keywords: new Set() },
  And: { maxPositional: Infinity, keywords: new Set() },
};

interface Args {
  positional: SyntaxNode[];
  keywords: Map<string, SyntaxNode>;
}

function reEscape(token: string): string {
  let out = "";
  for (const ch of token) out += RE_SPECIAL.has(ch) ? `\\${ch}` : ch;
  return out;
}

function blockCommandPattern(tokens: string[]): string {
  return tokens
    .map((t) =>
      t === "*"
        ? "\\S+"
        : t.includes("|")
          ? `(?:${t.split("|").map(reEscape).join("|")})`
          : reEscape(t),
    )
    .join("\\s+");
}

function checkRegexDialect(pattern: string): string {
  if (INLINE_FLAG.test(pattern) || FORBIDDEN_REGEX.some((tok) => pattern.includes(tok))) {
    throw new CompileError(`regex "${pattern}" uses Python-only syntax outside the JS-shared subset`);
  }
  return pattern;
}

function rstripDot(text: string): string {
  return text.replace(/\.+$/, "");
}

export function lower(tree: Tree, source: string): SerializedHook[] {
  return new Lowerer(source).run(tree);
}

class Lowerer {
  constructor(private source: string) {}

  private text(node: SyntaxNode): string {
    return this.source.slice(node.from, node.to);
  }

  // Attach a node's span to any position-less CompileError thrown while lowering it, so a
  // refusal deep in a primitive call still points the editor squiggle at that call.
  private atNode<T>(node: SyntaxNode, run: () => T): T {
    try {
      return run();
    } catch (e) {
      if (e instanceof CompileError && !e.pos) throw new CompileError(e.message, { from: node.from, to: node.to });
      throw e;
    }
  }

  run(tree: Tree): SerializedHook[] {
    const hooks: SerializedHook[] = [];
    for (let child = tree.topNode.firstChild; child; child = child.nextSibling) {
      if (child.name !== "ExpressionStatement") continue;
      const call = this.firstMeaningful(child);
      if (!call || call.name !== "CallExpression") continue;
      hooks.push(this.atNode(call, () => this.lowerCall(call)));
    }
    return hooks;
  }

  private firstMeaningful(node: SyntaxNode): SyntaxNode | null {
    for (let c = node.firstChild; c; c = c.nextSibling) {
      if (c.name !== "Comment") return c;
    }
    return null;
  }

  private unwrap(node: SyntaxNode): SyntaxNode {
    let n = node;
    while (n.name === "ParenthesizedExpression") {
      const inner = this.firstMeaningful(n);
      if (!inner || inner.name === "(" || inner.name === ")") break;
      n = inner;
    }
    return n;
  }

  private children(node: SyntaxNode, skip: Set<string>): SyntaxNode[] {
    const out: SyntaxNode[] = [];
    for (let c = node.firstChild; c; c = c.nextSibling) {
      if (!skip.has(c.name)) out.push(c);
    }
    return out;
  }

  private lowerCall(call: SyntaxNode): SerializedHook {
    const callee = call.firstChild;
    if (!callee || callee.name !== "VariableName") {
      throw new CompileError("only bare primitive calls are supported");
    }
    const name = this.text(callee);
    const list = call.getChild("ArgList");
    if (!list) throw new CompileError(`malformed call to ${name}`);
    const args = this.parseArgs(list);
    this.checkSignature(name, args, PRIMITIVE_SIGS);
    switch (name) {
      case "hook":
        return this.lowerHook(args);
      case "block_command":
        return this.lowerBlockCommand(args);
      case "warn_command":
        return this.lowerWarnCommand(args);
      case "rewrite_command":
        return this.lowerRewriteCommand(args);
      case "gate":
        return this.lowerNudge(args, true);
      case "nudge":
        return this.lowerNudge(args, false);
      default:
        throw new CompileError(`unsupported primitive \`${name}()\``);
    }
  }

  private parseArgs(list: SyntaxNode): Args {
    const kids = this.children(list, new Set(["(", ")", ",", "Comment"]));
    const positional: SyntaxNode[] = [];
    const keywords = new Map<string, SyntaxNode>();
    let i = 0;
    while (i < kids.length) {
      const kid = kids[i];
      if (kid.name === "VariableName" && kids[i + 1]?.name === "AssignOp") {
        const value = kids[i + 2];
        if (!value) throw new CompileError(`malformed keyword argument ${this.text(kid)}=`);
        keywords.set(this.text(kid), value);
        i += 3;
      } else {
        positional.push(kid);
        i += 1;
      }
    }
    return { positional, keywords };
  }

  private arg(args: Args, index: number, name: string): SyntaxNode | undefined {
    return args.positional[index] ?? args.keywords.get(name);
  }

  private required(args: Args, index: number, name: string, prim: string): SyntaxNode {
    const node = this.arg(args, index, name);
    if (!node) throw new CompileError(`${prim}() missing required argument: ${name}`);
    return node;
  }

  private requiredKeyword(args: Args, name: string, prim: string): SyntaxNode {
    const node = args.keywords.get(name);
    if (!node) throw new CompileError(`${prim}() missing required keyword argument: ${name}`);
    return node;
  }

  private checkSignature(name: string, args: Args, sigs: Record<string, Signature>): void {
    const sig = sigs[name];
    if (!sig) return;
    if (args.positional.length > sig.maxPositional) {
      throw new CompileError(
        `${name}() takes at most ${sig.maxPositional} positional argument${sig.maxPositional === 1 ? "" : "s"} but ${args.positional.length} were given`,
      );
    }
    for (const key of args.keywords.keys()) {
      if (!sig.keywords.has(key)) throw new CompileError(`${name}() got an unexpected keyword argument '${key}'`);
    }
  }

  // A keyword given a non-None value, matching how the primitives treat `x=None` as absent.
  private hasFeature(args: Args, name: string): boolean {
    const node = args.keywords.get(name);
    return node !== undefined && this.unwrap(node).name !== "None";
  }

  private evalString(node: SyntaxNode): string {
    const n = this.unwrap(node);
    if (n.name === "ContinuedString") {
      return this.children(n, new Set(["Comment"]))
        .map((seg) => this.evalStringLiteral(seg))
        .join("");
    }
    return this.evalStringLiteral(n);
  }

  private evalStringLiteral(n: SyntaxNode): string {
    if (n.name === "FormatString") throw new CompileError("f-string is outside the demo subset");
    if (n.name !== "String") throw new CompileError(`expected a string literal, got ${n.name}`);
    return this.parsePyString(this.text(n));
  }

  private evalOptString(node: SyntaxNode): string | null {
    return this.unwrap(node).name === "None" ? null : this.evalString(node);
  }

  private evalBool(node: SyntaxNode | undefined, fallback: boolean): boolean {
    if (!node) return fallback;
    const n = this.unwrap(node);
    if (n.name !== "Boolean") throw new CompileError("expected True or False");
    return this.text(n) === "True";
  }

  private listElements(node: SyntaxNode): SyntaxNode[] {
    const n = this.unwrap(node);
    if (n.name !== "ArrayExpression" && n.name !== "TupleExpression") {
      throw new CompileError(`expected a list, got ${n.name}`);
    }
    return this.children(n, new Set(["[", "]", "(", ")", ",", "Comment"]));
  }

  private stringArgs(args: Args): string[] {
    return args.positional.map((p) => this.evalString(p));
  }

  private splitNames(names: string[]): string[] {
    return names.flatMap((s) => s.split("|"));
  }

  private toolCondition(names: string[]): Condition {
    const expanded = new Set<string>();
    for (const n of names) for (const alias of ALIASES[n] ?? [n]) expanded.add(alias);
    return { kind: "Tool", names: [...expanded].sort() };
  }

  private conditions(node: SyntaxNode | undefined): Condition[] {
    return node ? this.listElements(node).map((e) => this.serializeCondition(e)) : [];
  }

  private serializeCondition(node: SyntaxNode): Condition {
    const n = this.unwrap(node);
    if (n.name === "MemberExpression") {
      if (this.text(n) === "Tool.EditTools") {
        return this.toolCondition(["Edit", "MultiEdit", "NotebookEdit", "Write"]);
      }
      throw new CompileError(`unsupported condition: ${this.text(n)}`);
    }
    if (n.name !== "CallExpression") {
      throw new CompileError(`unsupported condition: ${n.name}`);
    }
    const callee = n.firstChild;
    if (!callee || callee.name !== "VariableName") throw new CompileError("unsupported condition call");
    const name = this.text(callee);
    const list = n.getChild("ArgList");
    if (!list) throw new CompileError(`malformed condition ${name}`);
    const args = this.parseArgs(list);
    this.checkSignature(name, args, CONDITION_SIGS);
    switch (name) {
      case "Tool":
        return this.toolCondition(this.splitNames(this.stringArgs(args)));
      case "Command":
        return { kind: "Command", pattern: checkRegexDialect(this.evalString(this.required(args, 0, "pattern", "Command"))) };
      case "Runs":
        return { kind: "Runs", argv: this.stringArgs(args) };
      case "FilePath":
        return { kind: "FilePath", patterns: this.stringArgs(args), project_only: this.evalBool(args.keywords.get("project_only"), true) };
      case "Content": {
        const pattern = checkRegexDialect(this.evalString(this.required(args, 0, "pattern", "Content")));
        return { kind: "Content", pattern, project_only: this.evalBool(this.arg(args, 1, "project_only"), true) };
      }
      case "TouchedFile":
        return { kind: "TouchedFile", patterns: this.stringArgs(args) };
      case "UsedSkill":
        return { kind: "UsedSkill", names: this.splitNames(this.stringArgs(args)) };
      case "RanCommand":
        return { kind: "RanCommand", argv: this.stringArgs(args) };
      case "Waiting":
        return { kind: "Waiting" };
      case "Not":
        return { kind: "Not", condition: this.serializeCondition(this.required(args, 0, "condition", "Not")) };
      case "Or":
        return { kind: "Or", conditions: args.positional.map((p) => this.serializeCondition(p)) };
      case "And":
        return { kind: "And", conditions: args.positional.map((p) => this.serializeCondition(p)) };
      default:
        throw new CompileError(`unsupported condition: ${name}`);
    }
  }

  private collectEvents(node: SyntaxNode, out: string[]): void {
    const n = this.unwrap(node);
    if (n.name === "BinaryExpression") {
      const op = n.getChild("BitOp");
      if (!op || this.text(op) !== "|") throw new CompileError("only `Event.X | Event.Y` event unions are supported");
      for (const c of this.children(n, new Set(["BitOp", "(", ")", "Comment"]))) this.collectEvents(c, out);
      return;
    }
    if (n.name === "MemberExpression") {
      const base = n.firstChild;
      const prop = n.getChild("PropertyName");
      if (!base || this.text(base) !== "Event" || !prop) {
        throw new CompileError(`unsupported events expression: ${this.text(n)}`);
      }
      const name = this.text(prop);
      if (!EVENT_ORDER.includes(name)) throw new CompileError(`unknown event Event.${name}`);
      out.push(name);
      return;
    }
    throw new CompileError(`unsupported events expression: ${this.text(n)}`);
  }

  private evalEvents(node: SyntaxNode): string[] {
    const names: string[] = [];
    this.collectEvents(node, names);
    return [...new Set(names)].sort((a, b) => EVENT_ORDER.indexOf(a) - EVENT_ORDER.indexOf(b));
  }

  private lowerHook(args: Args): SerializedHook {
    return {
      events: this.evalEvents(this.required(args, 0, "events", "hook")),
      message: this.evalString(this.required(args, 1, "message", "hook")),
      block: this.evalBool(args.keywords.get("block"), false),
      advisory_on_deny: this.evalBool(args.keywords.get("advisory_on_deny"), false),
      only_if: this.conditions(args.keywords.get("only_if")),
      skip_if: this.conditions(args.keywords.get("skip_if")),
    };
  }

  private commandPattern(node: SyntaxNode): string {
    const n = this.unwrap(node);
    if (n.name === "ArrayExpression") {
      return blockCommandPattern(this.listElements(n).map((e) => this.evalString(e)));
    }
    if (n.name === "TupleExpression") {
      throw new CompileError("command pattern must be a string or list, not a tuple");
    }
    return this.evalString(n);
  }

  private lowerBlockCommand(args: Args): SerializedHook {
    const pattern = this.commandPattern(this.required(args, 0, "pattern", "block_command"));
    const reason = this.evalString(this.requiredKeyword(args, "reason", "block_command"));
    const hintNode = args.keywords.get("hint");
    const hint = hintNode ? this.evalOptString(hintNode) : null;
    const message = `BLOCKED: ${rstripDot(reason)}.${hint ? ` ${rstripDot(hint)}.` : ""}`;
    return {
      events: ["PreToolUse"],
      message,
      block: true,
      advisory_on_deny: false,
      only_if: [this.toolCondition(["Bash"]), { kind: "Command", pattern: checkRegexDialect(pattern) }, ...this.conditions(args.keywords.get("only_if"))],
      skip_if: this.conditions(args.keywords.get("skip_if")),
    };
  }

  private lowerWarnCommand(args: Args): SerializedHook {
    const pattern = this.commandPattern(this.required(args, 0, "pattern", "warn_command"));
    const message = this.evalString(this.requiredKeyword(args, "message", "warn_command"));
    const eventsNode = args.keywords.get("events");
    return {
      events: eventsNode ? this.evalEvents(eventsNode) : ["PostToolUse"],
      message,
      block: false,
      advisory_on_deny: false,
      only_if: [this.toolCondition(["Bash"]), { kind: "Command", pattern: checkRegexDialect(pattern) }, ...this.conditions(args.keywords.get("only_if"))],
      skip_if: this.conditions(args.keywords.get("skip_if")),
    };
  }

  private lowerRewriteCommand(args: Args): SerializedHook {
    if (this.hasFeature(args, "to")) throw new CompileError("rewrite_command(to=…) is outside the demo subset");
    const pattern = this.evalString(this.required(args, 0, "pattern", "rewrite_command"));
    const replace = this.evalString(this.required(args, 1, "replace", "rewrite_command"));
    if (TEMPLATE_VAR.test(pattern)) {
      throw new CompileError("structural rewrite pattern (ast-grep $VAR / $$$VAR) is outside the demo subset");
    }
    const noteNode = args.keywords.get("note");
    const note = noteNode ? this.evalOptString(noteNode) : null;
    const checked = checkRegexDialect(pattern);
    return {
      events: ["PreToolUse"],
      message: null,
      block: false,
      advisory_on_deny: false,
      rewrite: { pattern: checked, replace, note },
      only_if: [this.toolCondition(["Bash"]), { kind: "Command", pattern: checked }, ...this.conditions(args.keywords.get("only_if"))],
      skip_if: this.conditions(args.keywords.get("skip_if")),
    };
  }

  private lowerNudge(args: Args, forceBlock: boolean): SerializedHook {
    const label = forceBlock ? "gate" : "nudge";
    if (this.hasFeature(args, "when")) throw new CompileError(`${label}(when=…) predicate is outside the demo subset`);
    if (this.hasFeature(args, "signals")) throw new CompileError(`${label}(signals=…) is outside the demo subset`);
    const message = this.evalString(this.required(args, 0, "message", label));
    const block = forceBlock || this.evalBool(args.keywords.get("block"), false);
    const eventsNode = args.keywords.get("events");
    const events =
      eventsNode && this.unwrap(eventsNode).name !== "None"
        ? this.evalEvents(eventsNode)
        : block
          ? ["Stop", "SubagentStop"]
          : ["PreToolUse"];
    const skipIf = this.conditions(args.keywords.get("skip_if"));
    const guardsWaiting = block && (events.includes("Stop") || events.includes("SubagentStop"));
    return {
      events,
      message,
      block,
      advisory_on_deny: this.evalBool(args.keywords.get("advisory_on_deny"), false),
      only_if: this.conditions(args.keywords.get("only_if")),
      skip_if: guardsWaiting ? [{ kind: "Waiting" }, ...skipIf] : skipIf,
    };
  }

  private parsePyString(raw: string): string {
    let i = 0;
    let isRaw = false;
    while (i < raw.length && /[rRbBuUfF]/.test(raw[i])) {
      if (raw[i] === "r" || raw[i] === "R") isRaw = true;
      if (raw[i] === "f" || raw[i] === "F") throw new CompileError("f-string is outside the demo subset");
      i++;
    }
    const rest = raw.slice(i);
    const quote = rest.startsWith('"""') || rest.startsWith("'''") ? rest.slice(0, 3) : rest[0];
    const inner = rest.slice(quote.length, rest.length - quote.length);
    return isRaw ? inner : this.unescape(inner);
  }

  private unescape(s: string): string {
    let out = "";
    for (let i = 0; i < s.length; i++) {
      if (s[i] !== "\\") {
        out += s[i];
        continue;
      }
      const c = s[i + 1];
      if (c >= "0" && c <= "7") {
        let oct = "";
        while (oct.length < 3 && i + 1 + oct.length < s.length) {
          const d = s[i + 1 + oct.length];
          if (d < "0" || d > "7") break;
          oct += d;
        }
        out += String.fromCharCode(parseInt(oct, 8));
        i += oct.length;
        continue;
      }
      switch (c) {
        case "n": out += "\n"; i++; break;
        case "t": out += "\t"; i++; break;
        case "r": out += "\r"; i++; break;
        case "\\": out += "\\"; i++; break;
        case "'": out += "'"; i++; break;
        case '"': out += '"'; i++; break;
        case "a": out += "\x07"; i++; break;
        case "b": out += "\b"; i++; break;
        case "f": out += "\f"; i++; break;
        case "v": out += "\v"; i++; break;
        case "\n": i++; break;
        case "N": throw new CompileError("\\N{...} named escape is outside the demo subset");
        case "x": out += String.fromCharCode(this.hexEscape(s, i + 2, 2, "x")); i += 3; break;
        case "u": out += String.fromCharCode(this.hexEscape(s, i + 2, 4, "u")); i += 5; break;
        case "U": out += String.fromCodePoint(this.hexEscape(s, i + 2, 8, "U")); i += 9; break;
        default: out += "\\"; break;
      }
    }
    return out;
  }

  private hexEscape(s: string, start: number, count: number, kind: string): number {
    const hex = s.slice(start, start + count);
    if (!new RegExp(`^[0-9a-fA-F]{${count}}$`).test(hex)) {
      throw new CompileError(`truncated \\${kind} escape "${hex}"`);
    }
    return parseInt(hex, 16);
  }
}
