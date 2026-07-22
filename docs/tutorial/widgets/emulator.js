// capt-hook-widget src-sha256: 96cc2c0e9f388309567cead6ac8d64bf89652d5e12f868e4eec552655e61fa99

// autocomplete.ts
var counter = 0;
function createCombobox(opts) {
  const listId = `ch-widget-listbox-${counter++}`;
  const root = el("div", "ch-widget-combobox");
  const input = el("input", "ch-widget-input");
  input.type = "text";
  input.spellcheck = false;
  input.placeholder = opts.placeholder;
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-label", opts.ariaLabel);
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-expanded", "false");
  input.setAttribute("aria-controls", listId);
  const toggle = el("button", "ch-widget-combobox-toggle", "\u25BE");
  toggle.type = "button";
  toggle.tabIndex = -1;
  toggle.setAttribute("aria-label", "show scenarios");
  const listbox = el("ul", "ch-widget-listbox");
  listbox.id = listId;
  listbox.setAttribute("role", "listbox");
  listbox.hidden = true;
  let filtered = [];
  let active = -1;
  let open = false;
  const optionId = (i) => `${listId}-opt-${i}`;
  const setOpen = (next) => {
    open = next;
    listbox.hidden = !next;
    input.setAttribute("aria-expanded", String(next));
    if (!next) {
      active = -1;
      input.removeAttribute("aria-activedescendant");
    }
  };
  const highlight = () => {
    for (const [i, li] of Array.from(listbox.children).entries()) {
      const on = i === active;
      li.classList.toggle("ch-widget-option--active", on);
      li.setAttribute("aria-selected", String(on));
    }
    input.setAttribute("aria-activedescendant", active >= 0 ? optionId(active) : "");
    if (active >= 0) listbox.children[active]?.scrollIntoView({ block: "nearest" });
  };
  const paint = (query) => {
    const needle = query.trim().toLowerCase();
    filtered = needle ? opts.items.filter((it) => it.label.toLowerCase().includes(needle)) : opts.items.slice();
    listbox.textContent = "";
    filtered.forEach((it, i) => {
      const li = el("li", "ch-widget-option", it.label);
      li.id = optionId(i);
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", "false");
      li.addEventListener("mousedown", (e) => e.preventDefault());
      li.addEventListener("click", () => choose(i));
      listbox.append(li);
    });
    active = -1;
    setOpen(filtered.length > 0);
    highlight();
  };
  const choose = (i) => {
    const item = filtered[i];
    if (!item) return;
    setOpen(false);
    opts.onSelect(item.index);
  };
  const move = (delta) => {
    if (!open) {
      paint(input.value);
      return;
    }
    if (filtered.length === 0) return;
    active = (active + delta + filtered.length) % filtered.length;
    highlight();
  };
  input.addEventListener("input", () => {
    paint(input.value);
    opts.onType?.(input.value);
  });
  input.addEventListener("focus", () => paint(input.value));
  input.addEventListener("keydown", (e) => {
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        move(1);
        break;
      case "ArrowUp":
        e.preventDefault();
        move(-1);
        break;
      case "Enter":
        if (open && active >= 0) {
          e.preventDefault();
          choose(active);
        }
        break;
      case "Escape":
        if (open) {
          e.preventDefault();
          setOpen(false);
        }
        break;
    }
  });
  input.addEventListener("blur", () => setOpen(false));
  toggle.addEventListener("mousedown", (e) => e.preventDefault());
  toggle.addEventListener("click", () => {
    open ? setOpen(false) : paint(input.value);
    input.focus();
  });
  root.append(input, toggle, listbox);
  return { root, input, setValue: (text) => input.value = text };
}

// controls.ts
function walk(cond, visit) {
  visit(cond);
  switch (cond.kind) {
    case "Not":
      walk(cond.condition, visit);
      return;
    case "Or":
    case "And":
      cond.conditions.forEach((sub) => walk(sub, visit));
      return;
    default:
      return;
  }
}
function deriveControls(hooks) {
  let touched = false;
  let waiting = false;
  const skills = [];
  for (const hook of hooks) {
    for (const cond of [...hook.only_if, ...hook.skip_if]) {
      walk(cond, (c) => {
        if (c.kind === "TouchedFile") touched = true;
        else if (c.kind === "Waiting") waiting = true;
        else if (c.kind === "UsedSkill") c.names.forEach((n) => skills.includes(n) || skills.push(n));
      });
    }
  }
  return [
    ...touched ? [{ kind: "touchedFiles" }] : [],
    ...skills.map((name) => ({ kind: "usedSkill", name })),
    ...waiting ? [{ kind: "waiting" }] : []
  ];
}
function basename(path) {
  return path.split("/").pop() || path;
}
function fileChips(session, onChange) {
  const row = el("div", "ch-widget-control ch-widget-control--files");
  row.append(el("span", "ch-widget-control-label", "touched files"));
  const chips = el("div", "ch-widget-filechips");
  const add = el("input", "ch-widget-filechip-add");
  const render = () => {
    chips.textContent = "";
    for (const path of session.touchedFiles ?? []) {
      const chip = el("span", "ch-widget-filechip");
      chip.title = path;
      chip.append(el("span", "ch-widget-filechip-name", basename(path)));
      const remove = el("button", "ch-widget-filechip-remove", "\xD7");
      remove.type = "button";
      remove.setAttribute("aria-label", `remove ${path}`);
      remove.addEventListener("click", () => {
        session.touchedFiles = (session.touchedFiles ?? []).filter((p) => p !== path);
        render();
        onChange();
      });
      chip.append(remove);
      chips.append(chip);
    }
    chips.append(add);
  };
  add.type = "text";
  add.placeholder = "add path\u2026";
  add.spellcheck = false;
  add.setAttribute("aria-label", "add a touched file path");
  add.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const path = add.value.trim();
    if (!path) return;
    session.touchedFiles = [...session.touchedFiles ?? [], path];
    add.value = "";
    render();
    onChange();
    add.focus();
  });
  render();
  row.append(chips);
  return row;
}
function skillCheckbox(name, session, onChange) {
  const label = el("label", "ch-widget-control ch-widget-control--check");
  const box = el("input");
  box.type = "checkbox";
  box.checked = (session.usedSkills ?? []).includes(name);
  box.addEventListener("change", () => {
    const skills = new Set(session.usedSkills ?? []);
    box.checked ? skills.add(name) : skills.delete(name);
    session.usedSkills = [...skills];
    onChange();
  });
  label.append(box, el("span", void 0, "used the "), el("code", void 0, name), el("span", void 0, " skill"));
  return label;
}
function waitingToggle(session, onChange) {
  const label = el("label", "ch-widget-control ch-widget-control--check");
  const box = el("input");
  box.type = "checkbox";
  box.checked = session.waiting ?? false;
  box.addEventListener("change", () => {
    session.waiting = box.checked;
    onChange();
  });
  label.append(box, el("span", void 0, "waiting on the user"));
  return label;
}
function renderControls(controls, session, onChange) {
  if (controls.length === 0) return null;
  const panel = el("div", "ch-widget-controls");
  for (const control of controls) {
    switch (control.kind) {
      case "touchedFiles":
        panel.append(fileChips(session, onChange));
        break;
      case "usedSkill":
        panel.append(skillCheckbox(control.name, session, onChange));
        break;
      case "waiting":
        panel.append(waitingToggle(session, onChange));
        break;
    }
  }
  return panel;
}

// specs.ts
var HONESTY_MESSAGE = "outside the demo subset \u2014 run `capt-hook test` for the real engine";
var ADVISORY_SEPARATOR = "Additional advisories (not the reason for the deny):";
var LIVE_NOTE = "This runs a browser model of the demo subset \u2014 run `capt-hook test` for the real engine.";
var CANNED_NOTE = "Recorded from the real engine, not evaluated in your browser.";
var WORLD_NOTE = "This walks a declared virtual filesystem with a faithful port of the real hook \u2014 run `capt-hook test` for the real engine.";
var WORLD_HONESTY_MESSAGE = "outside the filesystem this demo declares \u2014 run `capt-hook test` for the real engine.";

// rm_world.ts
var GLOB_LIMIT = 10;
var LEADING_WRAPPERS = /* @__PURE__ */ new Set(["command", "doas", "env", "exec", "nice", "nohup", "sudo", "timeout", "xargs"]);
var SHELLS = /* @__PURE__ */ new Set(["sh", "bash", "dash", "zsh", "ksh", "ash", "fish", "csh", "tcsh"]);
var ASSIGNMENT = /^[A-Za-z_][A-Za-z0-9_]*=/;
var SAFE_WORD = /^[^\s'"\\$`;&|<>(){}#]+$/;
var TEMP_ROOTS = ["/tmp", "/private/tmp", "/var/folders", "/dev/shm"];
var SCRATCH_DIR_NAMES = /* @__PURE__ */ new Set(["tmp", "temp", "scratch", "scratchpad", "scratchpads"]);
var commandSubBlock = (raw) => `BLOCKED: a command substitution supplies rm targets in '${raw}', so they cannot be verified against any git/jj repository or scratch exemption. Expand the substitution to explicit paths first, or ask the user to run it themselves.`;
var globOverLimitBlock = (token) => `BLOCKED: the glob '${token}' matches more than ${GLOB_LIMIT} files \u2014 an easy way to delete far more than intended. List the matches first (ls ${token}), narrow the pattern, or name a directory explicitly with rm -r <dir>.`;
var repoRootBlock = (token) => `BLOCKED: '${token}' is a git/jj repository root \u2014 deleting it destroys the repo and its entire history. If this is really intended, ask the user to run it themselves.`;
var fsRootBlock = (token) => `BLOCKED: '${token}' is the filesystem root \u2014 deleting it destroys the entire system. If this is really intended, ask the user to run it themselves.`;
var containsRepoBlock = (token) => `BLOCKED: '${token}' contains git/jj repositories \u2014 deleting it would destroy them and their entire history. Delete a narrower path instead, or ask the user to run it themselves.`;
var unrecoverableBlock = (token) => `BLOCKED: rm target '${token}' resolves outside any git/jj repository, so nothing can restore it after deletion. Move it to the trash instead, or stop and ask the user to confirm this deletion. (Temp and scratch paths are exempt.)`;
var recoverableNote = (token) => `Rewrote rm to trash: '${token}' resolves outside any git/jj repository, so rm would be unrecoverable. The targets were moved to the macOS Trash instead \u2014 restorable via Finder (Put Back). If permanent deletion is truly intended, ask the user to run the rm themselves.`;
function norm(p) {
  const abs = p.startsWith("/");
  const out = [];
  for (const part of p.split("/")) {
    if (part === "" || part === ".") continue;
    if (part === "..") {
      if (out.length > 0 && out[out.length - 1] !== "..") out.pop();
      else if (!abs) out.push("..");
    } else out.push(part);
  }
  const joined = (abs ? "/" : "") + out.join("/");
  return joined === "" ? abs ? "/" : "." : joined;
}
function join(cwd, p) {
  return norm(p.startsWith("/") ? p : `${cwd}/${p}`);
}
function dirname(p) {
  const i = p.lastIndexOf("/");
  return i <= 0 ? "/" : p.slice(0, i);
}
function hasMagic(s) {
  return /[*?[]/.test(s);
}
function isScratch(resolved) {
  if (TEMP_ROOTS.some((root) => resolved === root || resolved.startsWith(`${root}/`))) return true;
  return resolved.split("/").filter((s) => s.length > 0).slice(0, -1).some((seg) => SCRATCH_DIR_NAMES.has(seg));
}
var Vfs = class {
  files = /* @__PURE__ */ new Set();
  dirs = /* @__PURE__ */ new Set();
  repos = /* @__PURE__ */ new Set();
  cwd;
  constructor(world) {
    this.cwd = norm(world.cwd);
    this.dirs.add(this.cwd);
    this.addAncestors(this.cwd);
    for (const f of world.files) {
      if (f.endsWith("/")) this.addDir(join(this.cwd, f));
      else this.addFile(join(this.cwd, f));
    }
    for (const r of world.repos) {
      const p = join(this.cwd, r);
      this.repos.add(p);
      this.addDir(p);
    }
  }
  addAncestors(p) {
    let d = dirname(p);
    while (!this.dirs.has(d)) {
      this.dirs.add(d);
      if (d === "/") break;
      d = dirname(d);
    }
  }
  addDir(p) {
    this.dirs.add(p);
    this.addAncestors(p);
  }
  addFile(p) {
    this.files.add(p);
    this.addAncestors(p);
  }
  isDir(p) {
    return this.dirs.has(p);
  }
  isRepoRoot(p) {
    return this.repos.has(p);
  }
  inRepo(p) {
    return [...this.repos].some((r) => p === r || p.startsWith(`${r}/`));
  }
  containsRepo(p) {
    return [...this.repos].some((r) => r.startsWith(`${p}/`));
  }
  childNames(dir) {
    const prefix = dir === "/" ? "/" : `${dir}/`;
    const names = /* @__PURE__ */ new Set();
    for (const p of [...this.files, ...this.dirs]) {
      if (p !== dir && p.startsWith(prefix)) {
        const name = p.slice(prefix.length).split("/")[0];
        if (name) names.add(name);
      }
    }
    return [...names];
  }
};
function inNamespace(cwd, p) {
  return p === cwd || p.startsWith(`${cwd}/`);
}
function segToRegex(seg) {
  let out = "";
  for (let i = 0; i < seg.length; i++) {
    const c = seg[i];
    if (c === "*") out += "[^/]*";
    else if (c === "?") out += "[^/]";
    else if (c === "[") {
      let j = i + 1;
      if (seg[j] === "!") j++;
      if (seg[j] === "]") j++;
      while (j < seg.length && seg[j] !== "]") j++;
      if (j >= seg.length) out += "\\[";
      else {
        let inner = seg.slice(i + 1, j);
        if (inner[0] === "!") inner = `^${inner.slice(1)}`;
        out += `[${inner}]`;
        i = j;
      }
    } else out += c.replace(/[.\\+^$(){}|]/g, "\\$&");
  }
  return new RegExp(`^${out}$`);
}
function globExpand(vfs, token, cwd) {
  let frontier = [{ abs: cwd, rel: "" }];
  for (const seg of token.split("/")) {
    const next = [];
    for (const node of frontier) {
      if (!hasMagic(seg)) {
        const child = join(node.abs, seg);
        if (vfs.isDir(child) || vfs.childNames(node.abs).includes(seg)) {
          next.push({ abs: child, rel: node.rel ? `${node.rel}/${seg}` : seg });
        }
      } else {
        const re = segToRegex(seg);
        for (const name of vfs.childNames(node.abs)) {
          if (!name.startsWith(".") && re.test(name)) {
            next.push({ abs: join(node.abs, name), rel: node.rel ? `${node.rel}/${name}` : name });
          }
        }
      }
    }
    frontier = next;
  }
  return frontier.map((n) => n.rel).slice(0, GLOB_LIMIT + 1);
}
function dequote(raw) {
  let out = "";
  let quote = null;
  for (let i = 0; i < raw.length; i++) {
    const c = raw[i];
    if (quote === "'") {
      if (c === "'") quote = null;
      else out += c;
      continue;
    }
    if (quote === '"') {
      if (c === '"') quote = null;
      else if (c === "\\" && i + 1 < raw.length && '"\\$`'.includes(raw[i + 1])) out += raw[++i];
      else out += c;
      continue;
    }
    if (c === "'" || c === '"') quote = c;
    else if (c === "\\" && i + 1 < raw.length) out += raw[++i];
    else out += c;
  }
  return out;
}
function emitToken(token, plainWords) {
  if (!SAFE_WORD.test(token)) return null;
  if ((hasMagic(token) || token.startsWith("~")) && !plainWords) return null;
  return token.startsWith("-") ? `./${token}` : token;
}
function classifyTarget(raw, afterTerminator) {
  if (raw.includes("$(") || raw.includes("`")) return { kind: "substitution" };
  if (raw.includes("${") || raw.includes("\\")) return { kind: "honesty" };
  const value = dequote(raw);
  const plain = !raw.includes("'") && !raw.includes('"');
  if (!afterTerminator && value.startsWith("-")) return { kind: "flag" };
  if (value.startsWith("~") || value.includes("**") || value.includes("{")) return { kind: "honesty" };
  const isGlob = hasMagic(value);
  if (isGlob && !plain) return { kind: "honesty" };
  return { kind: "target", target: { raw, value, isGlob, emittable: emitToken(value, plain) !== null } };
}
function resolveTarget(value, cwd) {
  return join(cwd, value);
}
function checkResolved(vfs, resolved, token, rewritable) {
  if (isScratch(resolved)) return { kind: "allow" };
  if (resolved !== "/" && !inNamespace(vfs.cwd, resolved)) return { kind: "honesty" };
  if (vfs.isRepoRoot(resolved)) return { kind: "block", message: repoRootBlock(token) };
  if (vfs.inRepo(resolved)) return { kind: "allow" };
  if (rewritable) {
    if (resolved === "/") return { kind: "block", message: fsRootBlock(token) };
    if (vfs.isDir(resolved) && vfs.containsRepo(resolved)) return { kind: "block", message: containsRepoBlock(token) };
  }
  return { kind: "recoverable", token };
}
function checkTarget(vfs, target, rewritable) {
  if (!target.isGlob) return checkResolved(vfs, resolveTarget(target.value, vfs.cwd), target.value, rewritable);
  const matches = globExpand(vfs, target.value, vfs.cwd);
  if (matches.length > GLOB_LIMIT) return { kind: "block", message: globOverLimitBlock(target.value) };
  let recovery = null;
  for (const match of matches) {
    const result = checkResolved(vfs, resolveTarget(match, vfs.cwd), match, rewritable);
    if (result.kind === "block" || result.kind === "honesty") return result;
    if (result.kind === "recoverable") {
      if (recovery !== null) return { kind: "honesty" };
      recovery = result;
    }
  }
  return recovery ?? { kind: "allow" };
}
function splitSegments(raw) {
  const out = [];
  let startIdx = 0;
  let quote = null;
  const push = (from, to) => {
    let s = from;
    let e = to;
    while (s < e && /\s/.test(raw[s])) s++;
    while (e > s && /\s/.test(raw[e - 1])) e--;
    if (e > s) out.push({ text: raw.slice(s, e), start: s, end: e });
  };
  for (let i = 0; i < raw.length; i++) {
    const c = raw[i];
    const n = raw[i + 1];
    if (quote) {
      if (c === quote) quote = null;
      continue;
    }
    if (c === "'" || c === '"') quote = c;
    else if (c === ";" || c === "\n") {
      push(startIdx, i);
      startIdx = i + 1;
    } else if (c === "|" && n === "|" || c === "&" && n === "&") {
      push(startIdx, i);
      startIdx = ++i + 1;
    } else if (c === "|") {
      push(startIdx, i);
      startIdx = i + 1;
    } else if (c === "&" && !raw.slice(startIdx, i).trimEnd().endsWith(">") && n !== ">") {
      push(startIdx, i);
      startIdx = i + 1;
    }
  }
  push(startIdx, raw.length);
  return out;
}
function splitWords(segment) {
  const words = [];
  let cur = "";
  let started = false;
  let quote = null;
  for (let i = 0; i < segment.length; i++) {
    const c = segment[i];
    if (quote) {
      cur += c;
      if (c === quote) quote = null;
      continue;
    }
    if (c === "'" || c === '"') {
      quote = c;
      cur += c;
      started = true;
    } else if (c === " " || c === "	") {
      if (started) {
        words.push(cur);
        cur = "";
        started = false;
      }
    } else if (c === "\\" && i + 1 < segment.length) {
      cur += c + segment[++i];
      started = true;
    } else {
      cur += c;
      started = true;
    }
  }
  if (started) words.push(cur);
  return words;
}
function headBasename(raw) {
  const stripped = raw.length >= 2 && raw[0] === raw[raw.length - 1] && (raw[0] === "'" || raw[0] === '"') ? raw.slice(1, -1) : raw;
  const unescaped = stripped.replace(/\\(.)/g, "$1");
  return (unescaped.split("/").pop() ?? unescaped).toLowerCase();
}
function classifyRm(words) {
  let i = 0;
  let wrapped = false;
  while (i < words.length && (ASSIGNMENT.test(words[i]) || LEADING_WRAPPERS.has(headBasename(words[i])))) {
    if (!ASSIGNMENT.test(words[i])) wrapped = true;
    i++;
  }
  if (i >= words.length) return { kind: "none" };
  const head = headBasename(words[i]);
  const rest = words.slice(i + 1);
  if (head === "time") return classifyRm(rest).kind === "none" ? { kind: "none" } : { kind: "honesty" };
  if (head === "eval" || SHELLS.has(head) && rest.includes("-c")) return { kind: "honesty" };
  if (head === "rm") return { kind: "rm", args: rest };
  if (wrapped && rest.some((w) => headBasename(w) === "rm")) return { kind: "honesty" };
  return { kind: "none" };
}
function skipSubst(s, open) {
  const opener = s[open];
  const closer = opener === "(" ? ")" : "}";
  let depth = 0;
  let quote = null;
  for (let i = open; i < s.length; i++) {
    const c = s[i];
    if (quote) {
      if (c === quote) quote = null;
    } else if (c === "'" || c === '"') quote = c;
    else if (c === "\\") i++;
    else if (c === opener) depth++;
    else if (c === closer && --depth === 0) return i;
  }
  return s.length;
}
function hasGrouping(command) {
  let quote = null;
  for (let i = 0; i < command.length; i++) {
    const c = command[i];
    if (quote) {
      if (c === quote) quote = null;
    } else if (c === "'" || c === '"') quote = c;
    else if (c === "\\") i++;
    else if (c === "`") i = (i = command.indexOf("`", i + 1)) < 0 ? command.length : i;
    else if (c === "$" && (command[i + 1] === "(" || command[i + 1] === "{")) i = skipSubst(command, i + 1);
    else if (c === "(" || c === ")" || c === "{" || c === "}") return true;
  }
  return false;
}
function hasRedirectOperator(word) {
  let quote = null;
  for (let i = 0; i < word.length; i++) {
    const c = word[i];
    if (quote) {
      if (c === quote) quote = null;
    } else if (c === "'" || c === '"') quote = c;
    else if (c === "\\") i++;
    else if (c === "<" || c === ">") return true;
  }
  return false;
}
function honesty() {
  return { action: "subset-exceeded", message: WORLD_HONESTY_MESSAGE, rewritten: null };
}
function block(message) {
  return { action: "block", message, rewritten: null };
}
function applyReplacements(command, edits) {
  let out = command;
  for (const edit of [...edits].sort((a, b) => b.start - a.start)) {
    out = out.slice(0, edit.start) + edit.text + out.slice(edit.end);
  }
  return out;
}
function evaluateRmWorld(world, command) {
  const vfs = new Vfs(world);
  if (hasGrouping(command)) return honesty();
  const rmCalls = [];
  for (const segment of splitSegments(command)) {
    const parsed = classifyRm(splitWords(segment.text));
    if (parsed.kind === "honesty") return honesty();
    if (parsed.kind === "rm") rmCalls.push({ segment, args: parsed.args });
  }
  if (rmCalls.length === 0) return { action: "pass", message: null, rewritten: null };
  const rewritable = world.trash !== null;
  const edits = [];
  const notes = [];
  let result = null;
  for (const call of rmCalls) {
    if (call.args.some(hasRedirectOperator)) return honesty();
    const targets = [];
    let substitution = false;
    let honest = false;
    let terminated = false;
    for (const raw of call.args) {
      if (!terminated && dequote(raw) === "--") {
        terminated = true;
        continue;
      }
      const cls = classifyTarget(raw, terminated);
      if (cls.kind === "substitution") substitution = true;
      else if (cls.kind === "honesty") honest = true;
      else if (cls.kind === "target") targets.push(cls.target);
    }
    if (substitution) return block(commandSubBlock(call.segment.text));
    if (honest) return honesty();
    let recovery = null;
    for (const target of targets) {
      const check = checkTarget(vfs, target, rewritable);
      if (check.kind === "honesty") return honesty();
      if (check.kind === "block") return block(check.message);
      if (check.kind === "recoverable" && recovery === null) recovery = check.token;
    }
    if (recovery === null) continue;
    if (rewritable && targets.every((t) => t.emittable)) {
      const args = targets.map((t) => t.raw.startsWith("-") ? `./${t.raw}` : t.raw);
      edits.push({ ...call.segment, text: [world.trash, ...args].join(" ") });
      notes.push(recoverableNote(recovery));
      result = {
        action: "rewrite",
        message: [...new Set(notes)].join("\n") || null,
        rewritten: applyReplacements(command, edits)
      };
      continue;
    }
    return block(unrecoverableBlock(recovery));
  }
  return result ?? { action: "pass", message: null, rewritten: null };
}

// dom.ts
var RECOMPILE_DEBOUNCE_MS = 300;
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}
function sessionSummary(session) {
  const files = (session?.touchedFiles ?? []).map((f) => f.split("/").pop() || f);
  const skills = session?.usedSkills ?? [];
  return `${files.length ? `edited ${files.join(", ")}` : "no edits"} \xB7 ${skills.length ? skills.join(", ") : "no skills"}`;
}
function caseLabel(input) {
  if (input.label != null) return input.label;
  if (input.command != null) return input.command;
  if (input.file != null) return `${input.tool ?? "Edit"} ${input.file}`;
  return sessionSummary(input.session);
}
function selectChips(cases) {
  const featured = cases.filter((c) => c.featured);
  return featured.length > 0 ? featured : cases.slice(0, 4);
}
function header(event, note) {
  const bar = el("header", "ch-widget-header");
  bar.append(el("span", "ch-widget-event", event), el("span", "ch-widget-mode-note", note));
  return bar;
}
function renderVerdict(panel, verdict) {
  panel.textContent = "";
  panel.className = `ch-widget-verdict ch-widget-verdict--${verdict.action}`;
  panel.appendChild(el("span", "ch-widget-badge", verdict.action));
  if (verdict.message) panel.appendChild(el("p", "ch-widget-message", verdict.message));
  if (verdict.rewritten) panel.appendChild(el("code", "ch-widget-rewrite", verdict.rewritten));
}
function renderCompileError(panel, message) {
  panel.textContent = "";
  panel.className = "ch-widget-verdict ch-widget-verdict--compile-error";
  panel.append(el("span", "ch-widget-badge", "compile error"), el("p", "ch-widget-message", message));
}
function chipRow(cases, onPick) {
  const row = el("div", "ch-widget-chips");
  for (const c of selectChips(cases.map((cc, index) => ({ ...cc, index })))) {
    const chip = el("button", "ch-widget-chip", caseLabel(c));
    chip.type = "button";
    chip.addEventListener("click", () => onPick(c.index));
    row.append(chip);
  }
  return row;
}
function importModule(relative) {
  return import(new URL(relative, document.baseURI).href);
}
var LiveWidget = class {
  constructor(stage, data, event, evaluate2, editorJs, compilerJs) {
    this.stage = stage;
    this.data = data;
    this.event = event;
    this.evaluate = evaluate2;
    this.editorJs = editorJs;
    this.compilerJs = compilerJs;
    this.hooks = data.hooks;
    this.commandMode = data.cases.some((c) => c.command != null);
  }
  hooks;
  session = {};
  current = {};
  compileError = null;
  editor = null;
  editorLoad = null;
  compiler = null;
  recompileTimer;
  panel = el("div", "ch-widget-verdict");
  controlsHost = el("div", "ch-widget-controls-host");
  commandMode;
  mount() {
    this.stage.append(header(this.event, LIVE_NOTE));
    if (this.data.source != null) this.stage.append(this.codePanel(this.data.source));
    const combobox = createCombobox({
      items: this.data.cases.map((c, index) => ({ label: caseLabel(c), index })),
      placeholder: this.commandMode ? "type a command\u2026" : "pick a scenario\u2026",
      ariaLabel: this.commandMode ? "command to evaluate" : "scenario to evaluate",
      onSelect: (index) => this.applyCase(index, combobox.setValue),
      onType: this.commandMode ? (text) => this.applyCommand(text) : void 0
    });
    this.stage.append(
      combobox.root,
      chipRow(this.data.cases, (index) => this.applyCase(index, combobox.setValue)),
      this.controlsHost,
      this.panel
    );
    this.renderControlsPanel();
    if (this.data.cases.length > 0) this.applyCase(0, combobox.setValue);
  }
  codePanel(source) {
    const wrap = el("div", "ch-widget-code");
    const reset = el("button", "ch-widget-reset", "Reset");
    reset.type = "button";
    reset.disabled = true;
    reset.addEventListener("click", () => {
      this.editor?.setDoc(source);
      void this.recompile(source);
      reset.disabled = true;
    });
    const bar = el("div", "ch-widget-code-bar");
    bar.append(el("span", "ch-widget-code-name", "hooks.py"), reset);
    const body = el("div", "ch-widget-code-body");
    const pre = el("pre", "ch-widget-code-pre", source);
    body.append(pre);
    wrap.append(bar, body);
    const upgrade = () => void this.upgradeEditor(body, pre, source, reset);
    body.addEventListener("pointerenter", upgrade, { once: true });
    body.addEventListener("focusin", upgrade, { once: true });
    new IntersectionObserver((entries, obs) => {
      if (entries.some((e) => e.isIntersecting)) {
        this.editorLoad ??= importModule(this.editorJs);
        obs.disconnect();
      }
    }).observe(wrap);
    return wrap;
  }
  async upgradeEditor(body, pre, source, reset) {
    if (this.editor) return;
    body.style.minHeight = `${pre.offsetHeight}px`;
    this.editorLoad ??= importModule(this.editorJs);
    const mod = await this.editorLoad;
    if (this.editor) return;
    pre.remove();
    this.editor = mod.createEditor({
      parent: body,
      doc: source,
      onChange: (doc) => {
        reset.disabled = doc === source;
        this.scheduleRecompile(doc);
      }
    });
  }
  scheduleRecompile(source) {
    clearTimeout(this.recompileTimer);
    this.recompileTimer = setTimeout(() => void this.recompile(source), RECOMPILE_DEBOUNCE_MS);
  }
  async recompile(source) {
    this.compiler ??= importModule(this.compilerJs);
    const result = (await this.compiler).compileSource(source);
    if ("error" in result) {
      this.compileError = result.error;
      this.editor?.setDiagnostics(
        result.from != null && result.to != null ? [{ from: result.from, to: result.to, message: result.error }] : []
      );
      renderCompileError(this.panel, result.error);
      return;
    }
    this.compileError = null;
    this.hooks = result.hooks;
    this.editor?.setDiagnostics([]);
    this.renderControlsPanel();
    this.evaluateNow();
  }
  applyCase(index, setValue) {
    const c = this.data.cases[index];
    if (!c) return;
    const { label: _label, featured: _featured, session, ...input } = c;
    this.current = input;
    this.session = structuredClone(session ?? {});
    setValue(this.commandMode ? c.command ?? "" : caseLabel(c));
    this.renderControlsPanel();
    this.evaluateNow();
  }
  applyCommand(text) {
    this.current = { tool: "Bash", command: text };
    this.evaluateNow();
  }
  renderControlsPanel() {
    this.controlsHost.textContent = "";
    const panel = renderControls(deriveControls(this.hooks), this.session, () => this.evaluateNow());
    if (panel) this.controlsHost.append(panel);
  }
  evaluateNow() {
    if (this.compileError != null) {
      renderCompileError(this.panel, this.compileError);
      return;
    }
    renderVerdict(this.panel, this.evaluate(this.hooks, { ...this.current, event: this.event, session: this.session }));
  }
};
function renderCanned(stage, data, event) {
  const recordings = data.recordings;
  const cases = recordings.map((r) => r.input);
  const panel = el("div", "ch-widget-verdict");
  const show = (index) => {
    combobox.setValue(caseLabel(cases[index]));
    renderVerdict(panel, recordings[index].verdict);
  };
  const combobox = createCombobox({
    items: cases.map((c, index) => ({ label: caseLabel(c), index })),
    placeholder: "pick a recorded run\u2026",
    ariaLabel: "recorded run to show",
    onSelect: show
  });
  stage.append(header(event, CANNED_NOTE), combobox.root, chipRow(cases, show), panel);
  if (recordings.length > 0) show(0);
}
function readOnlyCode(source, name) {
  const wrap = el("div", "ch-widget-code ch-widget-code--readonly");
  const bar = el("div", "ch-widget-code-bar");
  bar.append(el("span", "ch-widget-code-name", name));
  const body = el("div", "ch-widget-code-body");
  body.append(el("pre", "ch-widget-code-pre", source));
  wrap.append(bar, body);
  return wrap;
}
var WorldWidget = class {
  constructor(stage, data, event, world) {
    this.stage = stage;
    this.data = data;
    this.event = event;
    this.world = structuredClone(world);
    this.declaredTrash = world.trash ?? "/usr/bin/trash";
  }
  world;
  declaredTrash;
  current = "";
  panel = el("div", "ch-widget-verdict");
  mount() {
    this.stage.append(header(this.event, WORLD_NOTE));
    if (this.data.source != null) this.stage.append(readOnlyCode(this.data.source, "deletions.py"));
    const combobox = createCombobox({
      items: this.data.cases.map((c, index) => ({ label: caseLabel(c), index })),
      placeholder: "type an rm command\u2026",
      ariaLabel: "command to evaluate",
      onSelect: (index) => this.applyCase(index, combobox.setValue),
      onType: (text) => this.applyCommand(text)
    });
    this.stage.append(
      combobox.root,
      chipRow(this.data.cases, (index) => this.applyCase(index, combobox.setValue)),
      this.trashToggle(),
      this.panel
    );
    if (this.data.cases.length > 0) this.applyCase(0, combobox.setValue);
  }
  applyCase(index, setValue) {
    const c = this.data.cases[index];
    if (!c) return;
    this.current = c.command ?? "";
    setValue(this.current);
    this.evaluateNow();
  }
  applyCommand(text) {
    this.current = text;
    this.evaluateNow();
  }
  trashToggle() {
    const panel = el("div", "ch-widget-controls");
    const label = el("label", "ch-widget-control ch-widget-control--check");
    const box = el("input");
    box.type = "checkbox";
    box.checked = this.world.trash !== null;
    box.addEventListener("change", () => {
      this.world.trash = box.checked ? this.declaredTrash : null;
      this.evaluateNow();
    });
    label.append(box, el("span", void 0, "trash available "), el("code", void 0, this.declaredTrash));
    panel.append(label);
    return panel;
  }
  evaluateNow() {
    renderVerdict(this.panel, evaluateRmWorld(this.world, this.current));
  }
};
function mountAll(evaluate2) {
  for (const root of Array.from(document.querySelectorAll(".ch-widget"))) {
    if (root.dataset.mounted) continue;
    const script = root.querySelector("script.ch-widget-data");
    if (!script?.textContent) continue;
    root.dataset.mounted = "1";
    const data = JSON.parse(script.textContent);
    const stage = el("div", "ch-widget-stage");
    root.appendChild(stage);
    if (data.mode === "canned") {
      renderCanned(stage, data, data.recordings[0]?.input.event ?? "PreToolUse");
    } else if (data.mode === "world" && data.world) {
      new WorldWidget(stage, data, data.cases[0]?.event ?? "PreToolUse", data.world).mount();
    } else {
      new LiveWidget(
        stage,
        data,
        data.cases[0]?.event ?? "PreToolUse",
        evaluate2,
        root.dataset.editorJs ?? "editor.js",
        root.dataset.compilerJs ?? "compiler.js"
      ).mount();
    }
  }
}

// tokenizer.ts
var ASSIGNMENT2 = /^[A-Za-z_][A-Za-z0-9_]*=/;
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
function splitSegments2(raw) {
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
function splitWords2(segment) {
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
  const words = splitWords2(segment);
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
  while (start < kept.length && ASSIGNMENT2.test(kept[start])) start++;
  const argv = kept.slice(start);
  if (argv.length === 0) return null;
  return { argv, text: argv.join(" ") };
}
function tokenize(raw) {
  const commands = [];
  for (const segment of splitSegments2(raw)) {
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
var LEADING_WRAPPERS2 = /* @__PURE__ */ new Set(["sudo", "env", "timeout", "nohup", "command", "time", "xargs"]);
var SHELLS2 = /* @__PURE__ */ new Set(["sh", "bash", "dash", "zsh", "ksh"]);
function commandExecutable(argv) {
  let i = 0;
  while (i < argv.length && LEADING_WRAPPERS2.has(argv[i])) i++;
  return argv[i] ?? "";
}
function hasWrapper(cl) {
  return cl.commands.some((c) => {
    const exe = commandExecutable(c.argv);
    return exe === "eval" || SHELLS2.has(exe) && c.argv.includes("-c");
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
    return {
      action: "rewrite",
      message: hook.rewrite.note,
      rewritten: command.replace(re, pyReplacementToJs(hook.rewrite.replace)),
      advisoryOnDeny: false
    };
  }
  if (hook.message == null) return null;
  return {
    action: hook.block ? "block" : "warn",
    message: hook.message,
    rewritten: null,
    advisoryOnDeny: hook.advisory_on_deny
  };
}
function combine(fired) {
  const blocks = fired.filter((f) => f.action === "block").map((f) => f.message).filter((m) => m != null);
  const warnResults = fired.filter((f) => f.action === "warn");
  const warns = warnResults.map((f) => f.message).filter((m) => m != null);
  if (fired.some((f) => f.action === "block")) {
    const denyAdvisories = warnResults.filter((f) => f.advisoryOnDeny).map((f) => f.message).filter((m) => m != null);
    const parts = [...blocks];
    if (denyAdvisories.length > 0) {
      if (parts.length > 0) parts.push(ADVISORY_SEPARATOR);
      parts.push(...denyAdvisories);
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
  caseLabel,
  evaluate,
  evaluateRmWorld,
  selectChips
};
