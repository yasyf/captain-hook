// capt-hook-widget src-sha256: ec5dff6469132682dad6a9d9d1853fb86a181efd2d98ccfc0edf994eccc82284

// dom.ts
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}
function caseLabel(input) {
  if (input.command != null) return input.command;
  if (input.file != null) return `${input.tool ?? "Edit"} ${input.file}`;
  return input.tool ?? "event";
}
function renderVerdict(panel, verdict) {
  panel.textContent = "";
  panel.className = `ch-widget-verdict ch-widget-verdict--${verdict.action}`;
  panel.appendChild(el("span", "ch-widget-badge", verdict.action));
  if (verdict.message) panel.appendChild(el("p", "ch-widget-message", verdict.message));
  if (verdict.rewritten) panel.appendChild(el("code", "ch-widget-rewrite", verdict.rewritten));
}
function presetRow(labels, onPick) {
  const presets = el("div", "ch-widget-presets");
  for (const input of labels) {
    const button = el("button", "ch-widget-preset", caseLabel(input));
    button.type = "button";
    button.addEventListener("click", () => onPick(input));
    presets.appendChild(button);
  }
  return presets;
}
function renderLive(root, data, evaluate2) {
  const event = data.cases[0]?.event ?? "PreToolUse";
  const panel = el("div", "ch-widget-verdict");
  const input = el("input", "ch-widget-input");
  input.type = "text";
  input.spellcheck = false;
  input.setAttribute("aria-label", "Bash command to evaluate");
  const run = (value) => renderVerdict(panel, evaluate2(data.hooks, { event, tool: "Bash", command: value }));
  input.addEventListener("input", () => run(input.value));
  const onPick = (picked) => {
    if (picked.command != null) {
      input.value = picked.command;
      run(picked.command);
    } else {
      renderVerdict(panel, evaluate2(data.hooks, picked));
    }
  };
  if (data.cases.some((c) => c.command != null)) root.append(input);
  root.append(presetRow(data.cases, onPick), panel);
  const first = data.cases[0];
  if (first) onPick(first);
}
function renderCanned(root, data) {
  const recordings = data.recordings;
  const panel = el("div", "ch-widget-verdict");
  const onPick = (input) => {
    const rec = recordings.find((r) => r.input === input);
    if (rec) renderVerdict(panel, rec.verdict);
  };
  root.append(
    el("p", "ch-widget-badge-recorded", "recorded run \u2014 not evaluated in your browser"),
    presetRow(
      recordings.map((r) => r.input),
      onPick
    ),
    panel
  );
  if (recordings[0]) renderVerdict(panel, recordings[0].verdict);
}
function mountAll(evaluate2) {
  for (const root of Array.from(document.querySelectorAll(".ch-widget"))) {
    if (root.dataset.mounted) continue;
    const script = root.querySelector("script.ch-widget-data");
    if (!script?.textContent) continue;
    root.dataset.mounted = "1";
    const data = JSON.parse(script.textContent);
    const stage = el("div", "ch-widget-stage");
    root.appendChild(stage);
    if (data.mode === "canned") renderCanned(stage, data);
    else renderLive(stage, data, evaluate2);
  }
}

// specs.ts
var HONESTY_MESSAGE = "outside the demo subset \u2014 run `capt-hook test` for the real engine";
var ADVISORY_SEPARATOR = "Additional advisories (not the reason for the deny):";

// tokenizer.ts
var ASSIGNMENT = /^[A-Za-z_][A-Za-z0-9_]*=/;
function detectHonesty(raw) {
  let singleQuoted = false;
  let doubleQuoted = false;
  for (let i = 0; i < raw.length; i++) {
    const c = raw[i];
    const n = raw[i + 1];
    const p = raw[i - 1];
    if (singleQuoted) {
      if (c === "'") singleQuoted = false;
      continue;
    }
    if (doubleQuoted) {
      if (c === "\\") {
        i++;
        continue;
      }
      if (c === "`") return true;
      if (c === "$" && (n === "(" || n === "{")) return true;
      if (c === '"') doubleQuoted = false;
      continue;
    }
    if (c === "'") {
      singleQuoted = true;
      continue;
    }
    if (c === '"') {
      doubleQuoted = true;
      continue;
    }
    if (c === "\\") return true;
    if (c === "`") return true;
    if (c === "$" && (n === "(" || n === "{")) return true;
    if (c === "<" && (n === "(" || n === "<")) return true;
    if (c === ">" && n === "(") return true;
    if (c === "(") return true;
    if (c === "{" && (n === " " || n === "	")) return true;
    if (c === "#" && (i === 0 || p === " " || p === "	")) return true;
  }
  return false;
}
function splitSegments(raw) {
  const segments = [];
  let cur = "";
  let quote = null;
  for (let i = 0; i < raw.length; i++) {
    const c = raw[i];
    const n = raw[i + 1];
    if (quote) {
      cur += c;
      if (c === quote) quote = null;
      continue;
    }
    if (c === "'" || c === '"') {
      quote = c;
      cur += c;
      continue;
    }
    if (c === ";" || c === "\n") {
      segments.push(cur);
      cur = "";
      continue;
    }
    if (c === "|" && n === "|") {
      segments.push(cur);
      cur = "";
      i++;
      continue;
    }
    if (c === "|") {
      segments.push(cur);
      cur = "";
      continue;
    }
    if (c === "&" && n === "&") {
      segments.push(cur);
      cur = "";
      i++;
      continue;
    }
    if (c === "&") {
      if (cur.trimEnd().endsWith(">") || n === ">") {
        cur += c;
        continue;
      }
      segments.push(cur);
      cur = "";
      continue;
    }
    cur += c;
  }
  segments.push(cur);
  return segments.map((s) => s.trim()).filter((s) => s.length > 0);
}
function splitWords(segment) {
  const words = [];
  let cur = "";
  let started = false;
  for (let i = 0; i < segment.length; i++) {
    const c = segment[i];
    if (c === "'") {
      started = true;
      i++;
      while (i < segment.length && segment[i] !== "'") cur += segment[i++];
      continue;
    }
    if (c === '"') {
      started = true;
      i++;
      while (i < segment.length && segment[i] !== '"') {
        if (segment[i] === "\\" && i + 1 < segment.length) {
          cur += segment[i + 1];
          i += 2;
        } else {
          cur += segment[i++];
        }
      }
      continue;
    }
    if (c === " " || c === "	") {
      if (started) {
        words.push(cur);
        cur = "";
        started = false;
      }
      continue;
    }
    started = true;
    cur += c;
  }
  if (started) words.push(cur);
  return words;
}
function redirectKind(word) {
  if (/^&?\d*(>>|>|<)$/.test(word)) return "operator";
  if (/^&?\d*(>>|>|<)./.test(word)) return "attached";
  return null;
}
function parseCommand(segment) {
  const words = splitWords(segment);
  const kept = [];
  for (let i = 0; i < words.length; i++) {
    const kind = redirectKind(words[i]);
    if (kind === "operator") {
      i++;
      continue;
    }
    if (kind === "attached") continue;
    kept.push(words[i]);
  }
  let start = 0;
  while (start < kept.length && ASSIGNMENT.test(kept[start])) start++;
  const argv = kept.slice(start);
  if (argv.length === 0) return null;
  return { argv, text: argv.join(" ") };
}
function tokenize(raw) {
  const commands = [];
  for (const segment of splitSegments(raw)) {
    const command = parseCommand(segment);
    if (command) commands.push(command);
  }
  return { raw, commands };
}

// emulator.ts
var SubsetExceeded = class extends Error {
};
function compileRegex(pattern, flags = "") {
  try {
    return new RegExp(pattern, flags);
  } catch {
    throw new SubsetExceeded(pattern);
  }
}
function pyReplacementToJs(replace) {
  return replace.replace(/\$/g, "$$$$").replace(/\\g<([^>]+)>/g, "$<$1>").replace(/\\(\d+)/g, "$$$1");
}
function mcpSuffix(name) {
  return name.startsWith("mcp__") ? name.split("__").pop() ?? name : name;
}
function toolMatches(tool, names) {
  if (!tool) return false;
  return names.includes(tool) || names.includes(mcpSuffix(tool));
}
function fnmatchToRegex(glob) {
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
function fnmatch(path, glob) {
  const re = fnmatchToRegex(glob);
  const base = path.split("/").pop() ?? path;
  return re.test(path) || re.test(base);
}
function isProjectPath(path, repoRoot) {
  if (!path.startsWith("/")) return true;
  if (!repoRoot) return true;
  return path === repoRoot || path.startsWith(repoRoot.endsWith("/") ? repoRoot : `${repoRoot}/`);
}
var LEADING_WRAPPERS = /* @__PURE__ */ new Set(["sudo", "env", "timeout", "nohup", "command", "time", "xargs"]);
var SHELLS = /* @__PURE__ */ new Set(["sh", "bash", "dash", "zsh", "ksh"]);
function commandExecutable(argv) {
  let i = 0;
  while (i < argv.length && LEADING_WRAPPERS.has(argv[i])) i++;
  return argv[i] ?? "";
}
function hasWrapper(cl) {
  return cl.commands.some((c) => {
    const exe = commandExecutable(c.argv);
    return exe === "eval" || SHELLS.has(exe) && c.argv.includes("-c");
  });
}
function prefixEquals(argv, prefix) {
  return prefix.length <= argv.length && prefix.every((tok, i) => argv[i] === tok);
}
function skillMatches(skill, names) {
  return names.includes(skill) || names.includes(skill.split(":").pop() ?? skill);
}
function checkCondition(cond, ev, cl) {
  switch (cond.kind) {
    case "Tool":
      return toolMatches(ev.tool, cond.names);
    case "Command":
      return cl !== null && [cl.raw, ...cl.commands.map((c) => c.text)].some((s) => compileRegex(cond.pattern).test(s));
    case "Runs":
      return cl !== null && cond.argv.length > 0 && cl.commands.some((c) => prefixEquals(c.argv, cond.argv));
    case "FilePath":
      return ev.file != null && (!cond.project_only || isProjectPath(ev.file, ev.session?.repoRoot)) && cond.patterns.some((p) => fnmatch(ev.file, p));
    case "Content":
      return ev.content != null && (!cond.project_only || ev.file != null && isProjectPath(ev.file, ev.session?.repoRoot)) && compileRegex(cond.pattern, "m").test(ev.content);
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
function fire(hook, command) {
  if (hook.rewrite) {
    if (command === null) return null;
    const re = compileRegex(hook.rewrite.pattern, "g");
    return { action: "rewrite", message: hook.rewrite.note, rewritten: command.replace(re, pyReplacementToJs(hook.rewrite.replace)) };
  }
  if (hook.message == null) return null;
  return { action: hook.block ? "block" : "warn", message: hook.message, rewritten: null };
}
function combine(fired) {
  const blocks = fired.filter((f) => f.action === "block").map((f) => f.message).filter((m) => m != null);
  const warns = fired.filter((f) => f.action === "warn").map((f) => f.message).filter((m) => m != null);
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
    const notes = [...rewrite.message ? [rewrite.message] : [], ...warns];
    return { action: "rewrite", message: notes.join("\n\n") || null, rewritten: rewrite.rewritten };
  }
  if (warns.length > 0) return { action: "warn", message: warns.join("\n\n"), rewritten: null };
  return { action: "pass", message: null, rewritten: null };
}
function evaluate(hooks, input) {
  const event = input.event ?? "PreToolUse";
  const command = input.command ?? null;
  if (command !== null && detectHonesty(command)) {
    return { action: "subset-exceeded", message: HONESTY_MESSAGE, rewritten: null };
  }
  const ev = { ...input, event, tool: input.tool ?? null };
  try {
    const cl = command !== null ? tokenize(command) : null;
    if (cl !== null && hasWrapper(cl)) {
      return { action: "subset-exceeded", message: HONESTY_MESSAGE, rewritten: null };
    }
    const fired = [];
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
export {
  evaluate
};
