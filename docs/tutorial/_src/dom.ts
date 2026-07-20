// Renders the .ch-widget nodes embed_widgets.py stamps into the tutorial pages. Live
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
  panel.className = `ch-widget-verdict ch-widget-verdict--${verdict.action}`;
  panel.appendChild(el("span", "ch-widget-badge", verdict.action));
  if (verdict.message) panel.appendChild(el("p", "ch-widget-message", verdict.message));
  if (verdict.rewritten) panel.appendChild(el("code", "ch-widget-rewrite", verdict.rewritten));
}

function presetRow(labels: EventInput[], onPick: (input: EventInput) => void): HTMLElement {
  const presets = el("div", "ch-widget-presets");
  for (const input of labels) {
    const button = el("button", "ch-widget-preset", caseLabel(input));
    button.type = "button";
    button.addEventListener("click", () => onPick(input));
    presets.appendChild(button);
  }
  return presets;
}

function renderLive(root: HTMLElement, data: WidgetData, evaluate: Evaluate): void {
  const event = data.cases[0]?.event ?? "PreToolUse";
  const panel = el("div", "ch-widget-verdict");
  const input = el("input", "ch-widget-input");
  input.type = "text";
  input.spellcheck = false;
  input.setAttribute("aria-label", "Bash command to evaluate");

  const run = (value: string) => renderVerdict(panel, evaluate(data.hooks, { event, tool: "Bash", command: value }));
  input.addEventListener("input", () => run(input.value));

  const onPick = (picked: EventInput) => {
    if (picked.command != null) {
      input.value = picked.command;
      run(picked.command);
    } else {
      renderVerdict(panel, evaluate(data.hooks, picked));
    }
  };

  if (data.cases.some((c) => c.command != null)) root.append(input);
  root.append(presetRow(data.cases, onPick), panel);
  const first = data.cases[0];
  if (first) onPick(first);
}

function renderCanned(root: HTMLElement, data: WidgetData): void {
  const recordings = data.recordings as RecordedCase[];
  const panel = el("div", "ch-widget-verdict");
  const onPick = (input: EventInput) => {
    const rec = recordings.find((r) => r.input === input);
    if (rec) renderVerdict(panel, rec.verdict);
  };
  root.append(
    el("p", "ch-widget-badge-recorded", "recorded run — not evaluated in your browser"),
    presetRow(
      recordings.map((r) => r.input),
      onPick,
    ),
    panel,
  );
  if (recordings[0]) renderVerdict(panel, recordings[0].verdict);
}

export function mountAll(evaluate: Evaluate): void {
  for (const root of Array.from(document.querySelectorAll<HTMLElement>(".ch-widget"))) {
    if (root.dataset.mounted) continue;
    const script = root.querySelector<HTMLScriptElement>("script.ch-widget-data");
    if (!script?.textContent) continue;
    root.dataset.mounted = "1";
    const data = JSON.parse(script.textContent) as WidgetData;
    const stage = el("div", "ch-widget-stage");
    root.appendChild(stage);
    if (data.mode === "canned") renderCanned(stage, data);
    else renderLive(stage, data, evaluate);
  }
}
