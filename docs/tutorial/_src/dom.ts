// Renders the .ch-emu widgets embed_widgets.py stamps into the tutorial pages. Live
// widgets drive evaluate() as the reader types; canned widgets replay recorded verdicts.

import { EventInput, RecordedCase, Verdict, WidgetData } from "./specs";

type Evaluate = (hooks: WidgetData["hooks"], input: EventInput) => Verdict;

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function caseLabel(input: EventInput): string {
  if (input.command != null) return input.command;
  if (input.file != null) return `${input.tool ?? "Edit"} ${input.file}`;
  return input.tool ?? "event";
}

function renderVerdict(panel: HTMLElement, verdict: Verdict): void {
  panel.textContent = "";
  panel.className = `ch-emu-verdict ch-emu-verdict--${verdict.action}`;
  panel.appendChild(el("span", "ch-emu-badge", verdict.action));
  if (verdict.message) panel.appendChild(el("p", "ch-emu-message", verdict.message));
  if (verdict.command) panel.appendChild(el("code", "ch-emu-rewrite", verdict.command));
}

function renderLive(root: HTMLElement, data: WidgetData, evaluate: Evaluate): void {
  const event = data.cases[0]?.event ?? "PreToolUse";
  const panel = el("div", "ch-emu-verdict");
  const input = el("input", "ch-emu-input");
  input.type = "text";
  input.spellcheck = false;
  input.setAttribute("aria-label", "Bash command to evaluate");

  const run = (value: string) => renderVerdict(panel, evaluate(data.hooks, { event, tool: "Bash", command: value }));
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
        renderVerdict(panel, evaluate(data.hooks, testCase));
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
    renderVerdict(panel, evaluate(data.hooks, first));
  }
}

function renderCanned(root: HTMLElement, data: WidgetData): void {
  const table = el("table", "ch-emu-canned");
  const head = el("tr");
  head.append(el("th", undefined, "input"), el("th", undefined, "verdict"));
  table.appendChild(head);
  for (const rec of data.recordings as RecordedCase[]) {
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

export function mountAll(evaluate: Evaluate): void {
  for (const root of Array.from(document.querySelectorAll<HTMLElement>(".ch-emu"))) {
    if (root.dataset.mounted) continue;
    const script = root.querySelector<HTMLScriptElement>("script.ch-emu-data");
    if (!script?.textContent) continue;
    root.dataset.mounted = "1";
    const data = JSON.parse(script.textContent) as WidgetData;
    const stage = el("div", "ch-emu-stage");
    root.appendChild(stage);
    if (data.mode === "canned") renderCanned(stage, data);
    else renderLive(stage, data, evaluate);
  }
}
