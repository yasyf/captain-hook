// Shared shapes for the lowered declarative specs the emulator evaluates, plus the
// verdict shape both this bundle and the Python engine normalize to for CI parity.

export type Action = "block" | "warn" | "allow" | "rewrite" | "pass" | "subset-exceeded";

export interface Verdict {
  action: Action;
  message: string | null;
  rewritten: string | null;
}

export type Condition =
  | { kind: "Tool"; names: string[] }
  | { kind: "Command"; pattern: string }
  | { kind: "Runs"; argv: string[] }
  | { kind: "FilePath"; patterns: string[]; project_only: boolean }
  | { kind: "Content"; pattern: string; project_only: boolean }
  | { kind: "TouchedFile"; patterns: string[] }
  | { kind: "UsedSkill"; names: string[] }
  | { kind: "RanCommand"; argv: string[] }
  | { kind: "Waiting" }
  | { kind: "Not"; condition: Condition }
  | { kind: "Or"; conditions: Condition[] }
  | { kind: "And"; conditions: Condition[] };

export interface RewriteSpec {
  pattern: string;
  replace: string;
  note: string | null;
}

export interface SerializedHook {
  events: string[];
  message: string | null;
  block: boolean;
  rewrite?: RewriteSpec;
  only_if: Condition[];
  skip_if: Condition[];
}

export interface SessionState {
  waiting?: boolean;
  usedSkills?: string[];
  touchedFiles?: string[];
  ranCommands?: string[][];
  repoRoot?: string;
}

export interface EventInput {
  event?: string;
  tool?: string | null;
  command?: string | null;
  file?: string | null;
  content?: string | null;
  old?: string | null;
  agent_type?: string | null;
  permission_mode?: string | null;
  cwd?: string | null;
  session?: SessionState;
}

export interface RecordedCase {
  id: string;
  input: EventInput;
  verdict: Verdict;
}

// A live case is an EventInput enriched with the two presentation hints Lane B stamps in:
// `label` overrides the derived caseLabel and `featured` promotes the case into the chip row.
export interface WidgetCase extends EventInput {
  label?: string;
  featured?: boolean;
}

export interface WidgetData {
  mode: "live" | "canned";
  source?: string;
  hooks: SerializedHook[];
  cases: WidgetCase[];
  recordings: RecordedCase[];
}

// The init surface the core calls across the dynamic-import boundary to widgets/editor.js.
// Kept structural so emulator.js never statically pulls CodeMirror into its own bundle.
export interface EditorDiagnostic {
  from: number;
  to: number;
  message: string;
  severity?: "error" | "warning" | "info";
}

export interface EditorOptions {
  parent: HTMLElement;
  doc: string;
  readOnly?: boolean;
  onChange?: (doc: string) => void;
}

export interface EditorHandle {
  getDoc(): string;
  setDoc(doc: string): void;
  setDiagnostics(diagnostics: EditorDiagnostic[]): void;
  destroy(): void;
}

export interface EditorModule {
  createEditor(options: EditorOptions): EditorHandle;
}

// The single subset-exceeded verdict message. When the tokenizer meets a construct it
// cannot faithfully model, the emulator says so plainly rather than guessing a verdict.
export const HONESTY_MESSAGE = "outside the demo subset — run `capt-hook test` for the real engine";

// Mirrors captain_hook.dispatch.ADVISORY_SEPARATOR: warns ride along on a deny under this line.
export const ADVISORY_SEPARATOR = "Additional advisories (not the reason for the deny):";

// The widget-header mode notes, verbatim from docs/scripts/embed_widgets.py, now that the
// trailing <p class="ch-widget-note"> is gone and the note renders inside the header.
export const LIVE_NOTE = "This runs a browser model of the demo subset — run `capt-hook test` for the real engine.";
export const CANNED_NOTE = "Recorded from the real engine, not evaluated in your browser.";
