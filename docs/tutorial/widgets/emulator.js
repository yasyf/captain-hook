// capt-hook-widget src-sha256: 33f5ae2ff068733df33bed4592afc8d927127dea0c8a1191c9d0a1971ad953a0

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
  panel.className = `ch-emu-verdict ch-emu-verdict--${verdict.action}`;
  panel.appendChild(el("span", "ch-emu-badge", verdict.action));
  if (verdict.message) panel.appendChild(el("p", "ch-emu-message", verdict.message));
  if (verdict.command) panel.appendChild(el("code", "ch-emu-rewrite", verdict.command));
}
function renderLive(root, data, evaluate2) {
  const event = data.cases[0]?.event ?? "PreToolUse";
  const panel = el("div", "ch-emu-verdict");
  const input = el("input", "ch-emu-input");
  input.type = "text";
  input.spellcheck = false;
  input.setAttribute("aria-label", "Bash command to evaluate");
  const run = (value) => renderVerdict(panel, evaluate2(data.hooks, { event, tool: "Bash", command: value }));
  input.addEventListener("input", () => run(input.value));
  const presets = el("div", "ch-emu-presets");
  for (const testCase of data.cases) {
    const label = caseLabel(testCase);
    const button = el("button", "ch-emu-preset", label);
    button.type = "button";
    button.addEventListener("click", () => {
      if (testCase.command != null) {
        input.value = testCase.command;
        run(testCase.command);
      } else {
        renderVerdict(panel, evaluate2(data.hooks, testCase));
      }
    });
    presets.appendChild(button);
  }
  root.append(input, presets, panel);
  const first = data.cases[0];
  if (first?.command != null) {
    input.value = first.command;
    run(first.command);
  } else if (first) {
    renderVerdict(panel, evaluate2(data.hooks, first));
  }
}
function renderCanned(root, data) {
  const table = el("table", "ch-emu-canned");
  const head = el("tr");
  head.append(el("th", void 0, "input"), el("th", void 0, "verdict"));
  table.appendChild(head);
  for (const rec of data.recordings) {
    const row = el("tr");
    row.appendChild(el("td", "ch-emu-canned-input", caseLabel(rec.input)));
    const cell = el("td");
    cell.appendChild(el("span", `ch-emu-badge ch-emu-badge--${rec.verdict.action}`, rec.verdict.action));
    if (rec.verdict.message) cell.appendChild(el("p", "ch-emu-message", rec.verdict.message));
    row.appendChild(cell);
    table.appendChild(row);
  }
  root.appendChild(table);
}
function mountAll(evaluate2) {
  for (const root of Array.from(document.querySelectorAll(".ch-emu"))) {
    if (root.dataset.mounted) continue;
    const script = root.querySelector("script.ch-emu-data");
    if (!script?.textContent) continue;
    root.dataset.mounted = "1";
    const data = JSON.parse(script.textContent);
    const stage = el("div", "ch-emu-stage");
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
  for (let i = 0; i < raw.length; i++) {
    const c = raw[i];
    const n = raw[i + 1];
    if (singleQuoted) {
      if (c === "'") singleQuoted = false;
      continue;
    }
    if (c === "'") {
      singleQuoted = true;
      continue;
    }
    if (c === "`") return true;
    if (c === "$" && (n === "(" || n === "{")) return true;
    if (c === "<" && (n === "(" || n === "<")) return true;
    if (c === ">" && n === "(") return true;
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
function skillMatches(skill, names) {
  return names.includes(skill) || names.includes(skill.split(":").pop() ?? skill);
}
function prefixEquals(argv, prefix) {
  return prefix.length <= argv.length && prefix.every((tok, i) => argv[i] === tok);
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
      return ev.file != null && cond.patterns.some((p) => fnmatch(ev.file, p));
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
function combine(fired) {
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
function evaluate(hooks, input) {
  const event = input.event ?? "PreToolUse";
  const command = input.command ?? null;
  if (command !== null && detectHonesty(command)) {
    return { action: "subset-exceeded", message: HONESTY_MESSAGE, command: null };
  }
  const ev = { ...input, event, tool: input.tool ?? null };
  try {
    const cl = command !== null ? tokenize(command) : null;
    const fired = [];
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
export {
  evaluate
};
