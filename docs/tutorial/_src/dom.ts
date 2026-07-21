// Renders the .ch-widget nodes embed_widgets.py stamps into the tutorial pages. Live widgets get
// an editable code panel, a case combobox, derived session controls, and a recompiling verdict.

import { createCombobox } from "./autocomplete";
import { deriveControls, renderControls } from "./controls";
import { evaluateRmWorld } from "./rm_world";
import {
  CANNED_NOTE,
  EditorHandle,
  EditorModule,
  EventInput,
  LIVE_NOTE,
  RecordedCase,
  SessionState,
  Verdict,
  WidgetCase,
  WidgetData,
  WORLD_NOTE,
  WorldSpec,
} from "./specs";
import type { CompileResult } from "./compiler";

type Evaluate = (hooks: WidgetData["hooks"], input: EventInput) => Verdict;
type CompilerModule = { compileSource: (source: string) => CompileResult };

const RECOMPILE_DEBOUNCE_MS = 300;

export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function sessionSummary(session: SessionState | undefined): string {
  const files = (session?.touchedFiles ?? []).map((f) => f.split("/").pop() || f);
  const skills = session?.usedSkills ?? [];
  return `${files.length ? `edited ${files.join(", ")}` : "no edits"} · ${skills.length ? skills.join(", ") : "no skills"}`;
}

export function caseLabel(input: WidgetCase): string {
  if (input.label != null) return input.label;
  if (input.command != null) return input.command;
  if (input.file != null) return `${input.tool ?? "Edit"} ${input.file}`;
  return sessionSummary(input.session);
}

export function selectChips<T extends { featured?: boolean }>(cases: T[]): T[] {
  const featured = cases.filter((c) => c.featured);
  return featured.length > 0 ? featured : cases.slice(0, 4);
}

function header(event: string, note: string): HTMLElement {
  const bar = el("header", "ch-widget-header");
  bar.append(el("span", "ch-widget-event", event), el("span", "ch-widget-mode-note", note));
  return bar;
}

function renderVerdict(panel: HTMLElement, verdict: Verdict): void {
  panel.textContent = "";
  panel.className = `ch-widget-verdict ch-widget-verdict--${verdict.action}`;
  panel.appendChild(el("span", "ch-widget-badge", verdict.action));
  if (verdict.message) panel.appendChild(el("p", "ch-widget-message", verdict.message));
  if (verdict.rewritten) panel.appendChild(el("code", "ch-widget-rewrite", verdict.rewritten));
}

function renderCompileError(panel: HTMLElement, message: string): void {
  panel.textContent = "";
  panel.className = "ch-widget-verdict ch-widget-verdict--compile-error";
  panel.append(el("span", "ch-widget-badge", "compile error"), el("p", "ch-widget-message", message));
}

function chipRow(cases: WidgetCase[], onPick: (index: number) => void): HTMLElement {
  const row = el("div", "ch-widget-chips");
  for (const c of selectChips(cases.map((cc, index) => ({ ...cc, index })))) {
    const chip = el("button", "ch-widget-chip", caseLabel(c));
    chip.type = "button";
    chip.addEventListener("click", () => onPick(c.index));
    row.append(chip);
  }
  return row;
}

function importModule<T>(relative: string): Promise<T> {
  return import(new URL(relative, document.baseURI).href) as Promise<T>;
}

class LiveWidget {
  private hooks: WidgetData["hooks"];
  private session: SessionState = {};
  private current: EventInput = {};
  private compileError: string | null = null;
  private editor: EditorHandle | null = null;
  private editorLoad: Promise<EditorModule> | null = null;
  private compiler: Promise<CompilerModule> | null = null;
  private recompileTimer: ReturnType<typeof setTimeout> | undefined;
  private readonly panel = el("div", "ch-widget-verdict");
  private readonly controlsHost = el("div", "ch-widget-controls-host");
  private readonly commandMode: boolean;

  constructor(
    private readonly stage: HTMLElement,
    private readonly data: WidgetData,
    private readonly event: string,
    private readonly evaluate: Evaluate,
    private readonly editorJs: string,
    private readonly compilerJs: string,
  ) {
    this.hooks = data.hooks;
    this.commandMode = data.cases.some((c) => c.command != null);
  }

  mount(): void {
    this.stage.append(header(this.event, LIVE_NOTE));
    if (this.data.source != null) this.stage.append(this.codePanel(this.data.source));

    const combobox = createCombobox({
      items: this.data.cases.map((c, index) => ({ label: caseLabel(c), index })),
      placeholder: this.commandMode ? "type a command…" : "pick a scenario…",
      ariaLabel: this.commandMode ? "command to evaluate" : "scenario to evaluate",
      onSelect: (index) => this.applyCase(index, combobox.setValue),
      onType: this.commandMode ? (text) => this.applyCommand(text) : undefined,
    });

    this.stage.append(
      combobox.root,
      chipRow(this.data.cases, (index) => this.applyCase(index, combobox.setValue)),
      this.controlsHost,
      this.panel,
    );
    this.renderControlsPanel();
    if (this.data.cases.length > 0) this.applyCase(0, combobox.setValue);
  }

  private codePanel(source: string): HTMLElement {
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
        this.editorLoad ??= importModule<EditorModule>(this.editorJs);
        obs.disconnect();
      }
    }).observe(wrap);
    return wrap;
  }

  private async upgradeEditor(
    body: HTMLElement,
    pre: HTMLElement,
    source: string,
    reset: HTMLButtonElement,
  ): Promise<void> {
    if (this.editor) return;
    body.style.minHeight = `${pre.offsetHeight}px`;
    this.editorLoad ??= importModule<EditorModule>(this.editorJs);
    const mod = await this.editorLoad;
    if (this.editor) return;
    pre.remove();
    this.editor = mod.createEditor({
      parent: body,
      doc: source,
      onChange: (doc) => {
        reset.disabled = doc === source;
        this.scheduleRecompile(doc);
      },
    });
  }

  private scheduleRecompile(source: string): void {
    clearTimeout(this.recompileTimer);
    this.recompileTimer = setTimeout(() => void this.recompile(source), RECOMPILE_DEBOUNCE_MS);
  }

  private async recompile(source: string): Promise<void> {
    this.compiler ??= importModule<CompilerModule>(this.compilerJs);
    const result = (await this.compiler).compileSource(source);
    if ("error" in result) {
      this.compileError = result.error;
      this.editor?.setDiagnostics(
        result.from != null && result.to != null ? [{ from: result.from, to: result.to, message: result.error }] : [],
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

  private applyCase(index: number, setValue: (text: string) => void): void {
    const c = this.data.cases[index];
    if (!c) return;
    const { label: _label, featured: _featured, session, ...input } = c;
    this.current = input;
    this.session = structuredClone(session ?? {});
    setValue(this.commandMode ? (c.command ?? "") : caseLabel(c));
    this.renderControlsPanel();
    this.evaluateNow();
  }

  private applyCommand(text: string): void {
    this.current = { tool: "Bash", command: text };
    this.evaluateNow();
  }

  private renderControlsPanel(): void {
    this.controlsHost.textContent = "";
    const panel = renderControls(deriveControls(this.hooks), this.session, () => this.evaluateNow());
    if (panel) this.controlsHost.append(panel);
  }

  private evaluateNow(): void {
    if (this.compileError != null) {
      renderCompileError(this.panel, this.compileError);
      return;
    }
    renderVerdict(this.panel, this.evaluate(this.hooks, { ...this.current, event: this.event, session: this.session }));
  }
}

function renderCanned(stage: HTMLElement, data: WidgetData, event: string): void {
  const recordings = data.recordings as RecordedCase[];
  const cases: WidgetCase[] = recordings.map((r) => r.input);
  const panel = el("div", "ch-widget-verdict");
  const show = (index: number) => {
    combobox.setValue(caseLabel(cases[index]));
    renderVerdict(panel, recordings[index].verdict);
  };
  const combobox = createCombobox({
    items: cases.map((c, index) => ({ label: caseLabel(c), index })),
    placeholder: "pick a recorded run…",
    ariaLabel: "recorded run to show",
    onSelect: show,
  });
  stage.append(header(event, CANNED_NOTE), combobox.root, chipRow(cases, show), panel);
  if (recordings.length > 0) show(0);
}

function readOnlyCode(source: string, name: string): HTMLElement {
  const wrap = el("div", "ch-widget-code ch-widget-code--readonly");
  const bar = el("div", "ch-widget-code-bar");
  bar.append(el("span", "ch-widget-code-name", name));
  const body = el("div", "ch-widget-code-body");
  body.append(el("pre", "ch-widget-code-pre", source));
  wrap.append(bar, body);
  return wrap;
}

// The rm_walk world widget: a read-only view of the real pack hook, a command input over the
// declared cases, and a trash-present toggle that flips world.trash and re-evaluates in place.
class WorldWidget {
  private readonly world: WorldSpec;
  private readonly declaredTrash: string;
  private current = "";
  private readonly panel = el("div", "ch-widget-verdict");

  constructor(
    private readonly stage: HTMLElement,
    private readonly data: WidgetData,
    private readonly event: string,
    world: WorldSpec,
  ) {
    this.world = structuredClone(world);
    this.declaredTrash = world.trash ?? "/usr/bin/trash";
  }

  mount(): void {
    this.stage.append(header(this.event, WORLD_NOTE));
    if (this.data.source != null) this.stage.append(readOnlyCode(this.data.source, "deletions.py"));
    const combobox = createCombobox({
      items: this.data.cases.map((c, index) => ({ label: caseLabel(c), index })),
      placeholder: "type an rm command…",
      ariaLabel: "command to evaluate",
      onSelect: (index) => this.applyCase(index, combobox.setValue),
      onType: (text) => this.applyCommand(text),
    });
    this.stage.append(
      combobox.root,
      chipRow(this.data.cases, (index) => this.applyCase(index, combobox.setValue)),
      this.trashToggle(),
      this.panel,
    );
    if (this.data.cases.length > 0) this.applyCase(0, combobox.setValue);
  }

  private applyCase(index: number, setValue: (text: string) => void): void {
    const c = this.data.cases[index];
    if (!c) return;
    this.current = c.command ?? "";
    setValue(this.current);
    this.evaluateNow();
  }

  private applyCommand(text: string): void {
    this.current = text;
    this.evaluateNow();
  }

  private trashToggle(): HTMLElement {
    const panel = el("div", "ch-widget-controls");
    const label = el("label", "ch-widget-control ch-widget-control--check");
    const box = el("input");
    box.type = "checkbox";
    box.checked = this.world.trash !== null;
    box.addEventListener("change", () => {
      this.world.trash = box.checked ? this.declaredTrash : null;
      this.evaluateNow();
    });
    label.append(box, el("span", undefined, "trash available "), el("code", undefined, this.declaredTrash));
    panel.append(label);
    return panel;
  }

  private evaluateNow(): void {
    renderVerdict(this.panel, evaluateRmWorld(this.world, this.current));
  }
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
    if (data.mode === "canned") {
      renderCanned(stage, data, data.recordings[0]?.input.event ?? "PreToolUse");
    } else if (data.mode === "world" && data.world) {
      new WorldWidget(stage, data, data.cases[0]?.event ?? "PreToolUse", data.world).mount();
    } else {
      new LiveWidget(
        stage,
        data,
        data.cases[0]?.event ?? "PreToolUse",
        evaluate,
        root.dataset.editorJs ?? "editor.js",
        root.dataset.compilerJs ?? "compiler.js",
      ).mount();
    }
  }
}
