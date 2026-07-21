// Session-state controls derived from the compiled hooks' condition kinds, so they re-derive
// on edit: TouchedFile -> editable file chips, UsedSkill -> a checkbox per skill, Waiting -> toggle.

import { el } from "./dom";
import type { Condition, SerializedHook, SessionState } from "./specs";

export type ControlSpec =
  | { kind: "touchedFiles" }
  | { kind: "usedSkill"; name: string }
  | { kind: "waiting" };

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

export function deriveControls(hooks: SerializedHook[]): ControlSpec[] {
  let touched = false;
  let waiting = false;
  const skills: string[] = [];
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
    ...(touched ? [{ kind: "touchedFiles" } as const] : []),
    ...skills.map((name) => ({ kind: "usedSkill", name }) as const),
    ...(waiting ? [{ kind: "waiting" } as const] : []),
  ];
}

function basename(path: string): string {
  return path.split("/").pop() || path;
}

function fileChips(session: SessionState, onChange: () => void): HTMLElement {
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
      const remove = el("button", "ch-widget-filechip-remove", "×");
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
  add.placeholder = "add path…";
  add.spellcheck = false;
  add.setAttribute("aria-label", "add a touched file path");
  add.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const path = add.value.trim();
    if (!path) return;
    session.touchedFiles = [...(session.touchedFiles ?? []), path];
    add.value = "";
    render();
    onChange();
    add.focus();
  });
  render();
  row.append(chips);
  return row;
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
