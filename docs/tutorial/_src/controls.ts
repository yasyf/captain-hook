// Session-state controls derived from the compiled hooks' condition kinds, so they re-derive on
// edit. A session-state kind with no control of its own gets an inline note instead.

import { el } from "./dom";
import {
  absolutePath,
  COMMAND_CHIP_PLACEHOLDER,
  Condition,
  FILE_CHIP_PLACEHOLDER,
  HONESTY_MESSAGE,
  relativePath,
  SerializedHook,
  SESSION_HEADING,
  SessionState,
  unmodelledNote,
} from "./specs";
import { parseRanCommand } from "./tokenizer";

const COMPOSITE_KINDS = new Set(["Not", "Or", "And"]);

export type ControlSpec =
  | { kind: "touchedFiles"; addable: boolean }
  | { kind: "ranCommands" }
  | { kind: "usedSkill"; name: string }
  | { kind: "waiting" }
  | { kind: "unmodelled"; name: string };

export interface DeriveOptions {
  commandMode: boolean;
  lite: boolean;
}

function walk(cond: Condition, visit: (c: Condition) => void): void {
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

export function deriveControls(hooks: SerializedHook[], options: DeriveOptions): ControlSpec[] {
  let touched = false;
  let ran = false;
  let waiting = false;
  const skills: string[] = [];
  const unmodelled: string[] = [];
  for (const hook of hooks) {
    for (const cond of [...hook.only_if, ...hook.skip_if]) {
      walk(cond, (c) => {
        if (c.kind === "TouchedFile") touched = true;
        else if (c.kind === "RanCommand") ran = true;
        else if (c.kind === "Waiting") waiting = waiting || !c.implicit;
        else if (c.kind === "UsedSkill") c.names.forEach((n) => skills.includes(n) || skills.push(n));
        else if (!COMPOSITE_KINDS.has(c.kind) && !unmodelled.includes(c.kind)) unmodelled.push(c.kind);
      });
    }
  }
  if (options.lite) {
    return [
      ...(touched ? [{ kind: "touchedFiles", addable: false } as const] : []),
      ...skills.map((name) => ({ kind: "usedSkill", name }) as const),
    ];
  }
  return [
    ...(touched ? [{ kind: "touchedFiles", addable: true } as const] : []),
    ...(ran ? [{ kind: "ranCommands" } as const] : []),
    ...skills.map((name) => ({ kind: "usedSkill", name }) as const),
    ...(waiting ? [{ kind: "waiting" } as const] : []),
    ...(options.commandMode ? [] : unmodelled.map((name) => ({ kind: "unmodelled", name }) as const)),
  ];
}

interface ChipControlOptions<T> {
  label: string;
  placeholder: string;
  addable: boolean;
  read: () => T[];
  write: (values: T[]) => void;
  display: (value: T) => string;
  title: (value: T) => string;
  // Null when the typed text reaches outside the modelled subset; the row says so instead of adding.
  parse: (text: string) => T | null;
}

function chipControl<T>(options: ChipControlOptions<T>, onChange: () => void): HTMLElement {
  const row = el("div", "ch-widget-control ch-widget-control--files");
  row.append(el("span", "ch-widget-control-label", options.label));
  const chips = el("div", "ch-widget-filechips");
  const add = el("input", "ch-widget-filechip-add");
  const honesty = el("span", "ch-widget-chip-honesty", HONESTY_MESSAGE);
  honesty.hidden = true;
  const render = () => {
    chips.textContent = "";
    options.read().forEach((value, index) => {
      const chip = el("span", "ch-widget-filechip");
      chip.title = options.title(value);
      chip.append(el("span", "ch-widget-filechip-name", options.display(value)));
      const remove = el("button", "ch-widget-filechip-remove", "×");
      remove.type = "button";
      remove.setAttribute("aria-label", `remove ${options.display(value)}`);
      remove.addEventListener("click", () => {
        options.write(options.read().filter((_, i) => i !== index));
        render();
        onChange();
      });
      chip.append(remove);
      chips.append(chip);
    });
    if (options.addable) chips.append(add);
    chips.append(honesty);
  };
  add.type = "text";
  add.placeholder = options.placeholder;
  add.spellcheck = false;
  add.setAttribute("aria-label", options.placeholder);
  add.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const text = add.value.trim();
    if (!text) return;
    const value = options.parse(text);
    honesty.hidden = value !== null;
    if (value === null) return;
    options.write([...options.read(), value]);
    add.value = "";
    render();
    onChange();
    add.focus();
  });
  render();
  row.append(chips);
  return row;
}

function fileChips(session: SessionState, addable: boolean, onChange: () => void): HTMLElement {
  return chipControl<string>(
    {
      label: "touched files",
      placeholder: FILE_CHIP_PLACEHOLDER,
      addable,
      read: () => session.touchedFiles ?? [],
      write: (values) => (session.touchedFiles = values),
      display: (path) => relativePath(path, session.repoRoot),
      title: (path) => path,
      parse: (text) => absolutePath(text, session.repoRoot),
    },
    onChange,
  );
}

function commandChips(session: SessionState, onChange: () => void): HTMLElement {
  return chipControl<string[]>(
    {
      label: "ran commands",
      placeholder: COMMAND_CHIP_PLACEHOLDER,
      addable: true,
      read: () => session.ranCommands ?? [],
      write: (values) => (session.ranCommands = values),
      display: (argv) => argv.join(" "),
      title: (argv) => argv.join(" "),
      parse: parseRanCommand,
    },
    onChange,
  );
}

function skillCheckbox(name: string, session: SessionState, onChange: () => void): HTMLElement {
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
  label.append(box, el("span", undefined, "used the "), el("code", undefined, name), el("span", undefined, " skill"));
  return label;
}

function waitingToggle(session: SessionState, onChange: () => void): HTMLElement {
  const label = el("label", "ch-widget-control ch-widget-control--check");
  const box = el("input");
  box.type = "checkbox";
  box.checked = session.waiting ?? false;
  box.addEventListener("change", () => {
    session.waiting = box.checked;
    onChange();
  });
  label.append(box, el("span", undefined, "waiting on the user"));
  return label;
}

export function renderControls(
  controls: ControlSpec[],
  session: SessionState,
  onChange: () => void,
): HTMLElement | null {
  if (controls.length === 0) return null;
  const panel = el("div", "ch-widget-controls");
  panel.append(el("p", "ch-widget-controls-heading", SESSION_HEADING));
  for (const control of controls) {
    switch (control.kind) {
      case "touchedFiles":
        panel.append(fileChips(session, control.addable, onChange));
        break;
      case "ranCommands":
        panel.append(commandChips(session, onChange));
        break;
      case "usedSkill":
        panel.append(skillCheckbox(control.name, session, onChange));
        break;
      case "waiting":
        panel.append(waitingToggle(session, onChange));
        break;
      case "unmodelled":
        panel.append(el("p", "ch-widget-unmodelled", unmodelledNote(control.name)));
        break;
    }
  }
  return panel;
}
