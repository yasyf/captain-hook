// Shared shapes for the lowered declarative specs the emulator evaluates, plus the
// verdict shape both this bundle and the Python engine normalize to for CI parity.

export type Action = "block" | "warn" | "allow" | "rewrite" | "pass" | "subset-exceeded";

export interface Verdict {
  action: Action;
  message: string | null;
  command: string | null;
}

export type Condition =
  | { kind: "Tool"; names: string[] }
  | { kind: "Command"; pattern: string }
  | { kind: "Runs"; argv: string[] }
  | { kind: "FilePath"; patterns: string[]; project_only: boolean }
  | { kind: "Content"; pattern: string; project_only: boolean }
  | { kind: "UsedSkill"; names: string[] }
  | { kind: "Not"; condition: Condition }
  | { kind: "Or"; conditions: Condition[] }
  | { kind: "And"; conditions: Condition[] };

export interface SerializedHook {
  events: string[];
  message: string | null;
  block: boolean;
  only_if: Condition[];
  skip_if: Condition[];
}

export interface SessionState {
  usedSkills?: string[];
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

export interface WidgetData {
  mode: "live" | "canned";
  hooks: SerializedHook[];
  cases: EventInput[];
  recordings: RecordedCase[];
}

// The single subset-exceeded verdict message. When the tokenizer meets a construct it
// cannot faithfully model, the emulator says so plainly rather than guessing a verdict.
export const HONESTY_MESSAGE = "outside the demo subset — run `capt-hook test` for the real engine";

// Mirrors captain_hook.dispatch.ADVISORY_SEPARATOR: warns ride along on a deny under this line.
export const ADVISORY_SEPARATOR = "Additional advisories (not the reason for the deny):";
