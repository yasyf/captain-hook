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
  advisory_on_deny: boolean;
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

// A declared virtual filesystem the world engine walks: an absolute cwd, relative file paths
// (a trailing "/" marks an empty directory), relative git/jj repo roots, and the trash binary
// path (null models a machine with no `trash`). Materialized to a real tmpdir for CI parity.
export interface WorldSpec {
  cwd: string;
  files: string[];
  repos: string[];
  trash: string | null;
}

export interface WidgetData {
  mode: "live" | "canned" | "world" | "llm";
  source?: string;
  rubric?: string;
  world?: WorldSpec;
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

// Mirrors captain_hook.dispatch.ADVISORY_SEPARATOR: opted-in warns ride along on a deny under this line.
export const ADVISORY_SEPARATOR = "Additional advisories (not the reason for the deny):";

// The widget-header mode notes, verbatim from docs/scripts/embed_widgets.py, now that the
// trailing <p class="ch-widget-note"> is gone and the note renders inside the header.
export const LIVE_NOTE = "This runs a browser model of the demo subset — run `capt-hook test` for the real engine.";
export const CANNED_NOTE = "Recorded from the real engine, not evaluated in your browser.";

// World-mode strings (placeholder wording — the orchestrator owns the final prose pass).
// WORLD_NOTE heads the widget; WORLD_HONESTY_MESSAGE is the card shown when a typed command
// reaches outside the filesystem this demo declares (a construct the world cannot answer).
export const WORLD_NOTE = "This walks a declared virtual filesystem with a faithful port of the real hook — run `capt-hook test` for the real engine.";
export const WORLD_HONESTY_MESSAGE = "outside the filesystem this demo declares — run `capt-hook test` for the real engine.";

// The init surface the core calls across the dynamic-import boundary to widgets/llm.js. Kept
// structural (no pocket-llm types) so emulator.js never statically pulls pocket-llm or its
// on-device engines into its own bundle — llm.js is imported by URL on first interaction.
export interface LlmDetection {
  lane: string;
  availability: "ready" | "needs-download";
  model: string;
  downloadBytes: number | null;
}

export interface LlmProgress {
  loaded: number;
  total: number | null;
}

export interface LlmSession {
  prompt(text: string): Promise<unknown>;
  destroy(): Promise<void>;
}

export interface LlmAdapter {
  detect(): Promise<LlmDetection>;
  start(): Promise<LlmSession>;
}

export interface LlmInitOptions {
  system: string;
  schema: unknown;
  assets: { wllama: { default: string } };
  onProgress?: (progress: LlmProgress) => void;
}

export interface LlmModule {
  initLlm(options: LlmInitOptions): LlmAdapter;
}

// The structured-output constraint the live gate answers under, mirroring the read fields of
// captain_hook.primitives.llm.GateVerdict.model_json_schema() (block: deny, reasoning: why).
export const GATE_SCHEMA = {
  type: "object",
  properties: { block: { type: "boolean" }, reasoning: { type: "string" } },
  required: ["block", "reasoning"],
};

// LLM-widget copy (placeholder wording — the orchestrator owns the final prose pass). The verdict
// badge is verbatim from the phase spec: a live model verdict is nondeterministic and uncheckable.
export const LLM_NOTE = "A recorded model verdict — run it live on-device, nothing leaves your browser.";
export const LLM_RECORDED_BADGE = "recording";
export const LLM_RUN_LIVE_LABEL = "Run it live on-device";
export const LLM_DETECTING = "Checking what this browser can run…";
export const LLM_BUILTIN_READY = "This browser has a built-in model ready — no download needed.";
export const LLM_RUN_LABEL = "Run the gate";
export const LLM_DOWNLOAD_OFFER = "Running on-device needs a one-time download of {model} ({size}).";
export const LLM_DOWNLOAD_LABEL = "Download & run";
export const LLM_LOADING = "Starting the model…";
export const LLM_DOWNLOADING = "Downloading the model… {percent}";
export const LLM_GENERATING = "Asking the model…";
export const LLM_VERDICT_BADGE = "model verdict — nondeterministic, not part of the parity suite";
export const LLM_UNAVAILABLE = "No on-device model lane is available in this browser — the recorded verdict above is what a real run produced.";
export const LLM_SIZE_UNKNOWN = "unknown size";
export const LLM_USER_PROMPT = "Evaluate this pending action:\n\nTool: {tool}\nCommand: {command}";
