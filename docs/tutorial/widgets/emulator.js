// capt-hook-widget src-sha256: b0c3bd3dddf985e85054aee426d52cadc4491d720c41fe1f9561128f354726f1

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
  caseLabel,
  evaluate,
  selectChips
};
