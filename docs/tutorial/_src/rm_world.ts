// TypeScript port of deletions.py (guard_rm) over a declared virtual filesystem (WorldSpec).
// BLOCKED/rewrite strings are verbatim so a materialized-tmpdir parity gate byte-compares.

import { Verdict, WORLD_HONESTY_MESSAGE, WorldSpec } from "./specs";

const GLOB_LIMIT = 10;

const LEADING_WRAPPERS = new Set(["sudo", "env", "timeout", "nohup", "command", "time", "xargs"]);
const SHELLS = new Set(["sh", "bash", "dash", "zsh", "ksh", "ash", "fish", "csh", "tcsh"]);
const ASSIGNMENT = /^[A-Za-z_][A-Za-z0-9_]*=/;
const SAFE_WORD = /^[^\s'"\\$`;&|<>(){}#]+$/;
// util/scratch.py: temp roots plus scratch-named ancestor directories.
const TEMP_ROOTS = ["/tmp", "/private/tmp", "/var/folders", "/dev/shm", "/run/user"];
const SCRATCH_DIR_NAMES = new Set(["tmp", "temp", "scratch", "scratchpad", "scratchpads"]);

// --- verbatim message strings (captain_hook/builtin_packs/general/hooks/deletions.py) ----------

const commandSubBlock = (raw: string): string =>
  `BLOCKED: a command substitution supplies rm targets in '${raw}', so they cannot be verified ` +
  `against any git/jj repository or scratch exemption. Expand the substitution to explicit ` +
  `paths first, or ask the user to run it themselves.`;

const globOverLimitBlock = (token: string): string =>
  `BLOCKED: the glob '${token}' matches more than ${GLOB_LIMIT} files — an easy way to delete far ` +
  `more than intended. List the matches first (ls ${token}), narrow the pattern, or name a ` +
  `directory explicitly with rm -r <dir>.`;

const repoRootBlock = (token: string): string =>
  `BLOCKED: '${token}' is a git/jj repository root — deleting it destroys the repo and its entire ` +
  `history. If this is really intended, ask the user to run it themselves.`;

const fsRootBlock = (token: string): string =>
  `BLOCKED: '${token}' is the filesystem root — deleting it destroys the entire ` +
  `system. If this is really intended, ask the user to run it themselves.`;

const containsRepoBlock = (token: string): string =>
  `BLOCKED: '${token}' contains git/jj repositories — deleting it would destroy ` +
  `them and their entire history. Delete a narrower path instead, or ask the user ` +
  `to run it themselves.`;

const unrecoverableBlock = (token: string): string =>
  `BLOCKED: rm target '${token}' resolves outside any git/jj repository, so nothing can restore it ` +
  `after deletion. Move it to the trash instead, or stop and ask the user to confirm this deletion. ` +
  `(Temp and scratch paths are exempt.)`;

const recoverableNote = (token: string): string =>
  `Rewrote rm to trash: '${token}' resolves outside any git/jj repository, so rm would be ` +
  `unrecoverable. The targets were moved to the macOS Trash instead — restorable via Finder (Put Back). ` +
  `If permanent deletion is truly intended, ask the user to run the rm themselves.`;

// --- posix path helpers (the world is posix, no symlinks) --------------------------------------

function norm(p: string): string {
  const abs = p.startsWith("/");
  const out: string[] = [];
  for (const part of p.split("/")) {
    if (part === "" || part === ".") continue;
    if (part === "..") {
      if (out.length > 0 && out[out.length - 1] !== "..") out.pop();
      else if (!abs) out.push("..");
    } else out.push(part);
  }
  const joined = (abs ? "/" : "") + out.join("/");
  return joined === "" ? (abs ? "/" : ".") : joined;
}

function join(cwd: string, p: string): string {
  return norm(p.startsWith("/") ? p : `${cwd}/${p}`);
}

function dirname(p: string): string {
  const i = p.lastIndexOf("/");
  return i <= 0 ? "/" : p.slice(0, i);
}

function hasMagic(s: string): boolean {
  return /[*?[]/.test(s);
}

function isScratch(resolved: string): boolean {
  if (TEMP_ROOTS.some((root) => resolved === root || resolved.startsWith(`${root}/`))) return true;
  return resolved
    .split("/")
    .filter((s) => s.length > 0)
    .slice(0, -1)
    .some((seg) => SCRATCH_DIR_NAMES.has(seg));
}

// --- the declared virtual filesystem -----------------------------------------------------------

class Vfs {
  private readonly files = new Set<string>();
  private readonly dirs = new Set<string>();
  private readonly repos = new Set<string>();
  readonly cwd: string;

  constructor(world: WorldSpec) {
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

  private addAncestors(p: string): void {
    let d = dirname(p);
    while (!this.dirs.has(d)) {
      this.dirs.add(d);
      if (d === "/") break;
      d = dirname(d);
    }
  }

  private addDir(p: string): void {
    this.dirs.add(p);
    this.addAncestors(p);
  }

  private addFile(p: string): void {
    this.files.add(p);
    this.addAncestors(p);
  }

  isDir(p: string): boolean {
    return this.dirs.has(p);
  }

  isRepoRoot(p: string): boolean {
    return this.repos.has(p);
  }

  inRepo(p: string): boolean {
    return [...this.repos].some((r) => p === r || p.startsWith(`${r}/`));
  }

  containsRepo(p: string): boolean {
    return [...this.repos].some((r) => r.startsWith(`${p}/`));
  }

  childNames(dir: string): string[] {
    const prefix = dir === "/" ? "/" : `${dir}/`;
    const names = new Set<string>();
    for (const p of [...this.files, ...this.dirs]) {
      if (p !== dir && p.startsWith(prefix)) {
        const name = p.slice(prefix.length).split("/")[0];
        if (name) names.add(name);
      }
    }
    return [...names];
  }
}

function inNamespace(cwd: string, p: string): boolean {
  return p === cwd || p.startsWith(`${cwd}/`);
}

function segToRegex(seg: string): RegExp {
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

// glob.iglob(token, root_dir=cwd, recursive=False, include_hidden=False), cwd-relative, capped.
function globExpand(vfs: Vfs, token: string, cwd: string): string[] {
  let frontier: { abs: string; rel: string }[] = [{ abs: cwd, rel: "" }];
  for (const seg of token.split("/")) {
    const next: { abs: string; rel: string }[] = [];
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

// --- target classification ---------------------------------------------------------------------

interface RmTarget {
  raw: string;
  value: string;
  isGlob: boolean;
  emittable: boolean;
}

type Classification =
  | { kind: "substitution" }
  | { kind: "honesty" }
  | { kind: "flag" }
  | { kind: "target"; target: RmTarget };

function dequote(raw: string): string {
  let out = "";
  let quote: "'" | '"' | null = null;
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

// util/shell.py emit_token, SAFE_WORD branch only — the escape branch is unreachable because
// backslash targets already resolve to a world honesty card.
function emitToken(token: string, plainWords: boolean): string | null {
  if (!SAFE_WORD.test(token)) return null;
  if ((hasMagic(token) || token.startsWith("~")) && !plainWords) return null;
  return token.startsWith("-") ? `./${token}` : token;
}

function classifyTarget(raw: string): Classification {
  if (raw.includes("$(") || raw.includes("`")) return { kind: "substitution" };
  if (raw.includes("${") || raw.includes("\\")) return { kind: "honesty" };
  const value = dequote(raw);
  const plain = !raw.includes("'") && !raw.includes('"');
  if (value.startsWith("-")) return { kind: "flag" };
  if (value.startsWith("~") || value.includes("**") || value.includes("{")) return { kind: "honesty" };
  const isGlob = hasMagic(value);
  if (isGlob && !plain) return { kind: "honesty" };
  return { kind: "target", target: { raw, value, isGlob, emittable: emitToken(value, plain) !== null } };
}

// --- resolution & the check_resolved / check_target ladder -------------------------------------

type CheckResult =
  | { kind: "allow" }
  | { kind: "honesty" }
  | { kind: "block"; message: string }
  | { kind: "recoverable"; token: string };

function resolveTarget(value: string, cwd: string): string {
  return join(cwd, value);
}

function checkResolved(vfs: Vfs, resolved: string, token: string, rewritable: boolean): CheckResult {
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

function checkTarget(vfs: Vfs, target: RmTarget, rewritable: boolean): CheckResult {
  if (!target.isGlob) return checkResolved(vfs, resolveTarget(target.value, vfs.cwd), target.value, rewritable);
  const matches = globExpand(vfs, target.value, vfs.cwd);
  if (matches.length > GLOB_LIMIT) return { kind: "block", message: globOverLimitBlock(target.value) };
  let recovery: CheckResult | null = null;
  for (const match of matches) {
    const result = checkResolved(vfs, resolveTarget(match, vfs.cwd), match, rewritable);
    if (result.kind === "block" || result.kind === "honesty") return result;
    if (result.kind === "recoverable" && recovery === null) recovery = result;
  }
  return recovery ?? { kind: "allow" };
}

// --- command-line walk (rm-call extraction, wrapper honesty, the guard loop) --------------------

interface Segment {
  text: string;
  start: number;
  end: number;
}

function splitSegments(raw: string): Segment[] {
  const out: Segment[] = [];
  let startIdx = 0;
  let quote: "'" | '"' | null = null;
  const push = (from: number, to: number): void => {
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
    } else if ((c === "|" && n === "|") || (c === "&" && n === "&")) {
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

function splitWords(segment: string): string[] {
  const words: string[] = [];
  let cur = "";
  let started = false;
  let quote: "'" | '"' | null = null;
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
    } else if (c === " " || c === "\t") {
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

// cmd.py basename/normalize_executable: strip matched surrounding quotes, unescape, basename, fold.
function headBasename(raw: string): string {
  const stripped =
    raw.length >= 2 && raw[0] === raw[raw.length - 1] && (raw[0] === "'" || raw[0] === '"')
      ? raw.slice(1, -1)
      : raw;
  const unescaped = stripped.replace(/\\(.)/g, "$1");
  return (unescaped.split("/").pop() ?? unescaped).toLowerCase();
}

interface RmCall {
  segment: Segment;
  args: string[];
}

// The unwrapped head basename and its argument words, leading env assignments and wrapper commands
// stripped, or null when the segment has no command word.
function headAndArgs(words: string[]): { head: string; args: string[] } | null {
  let i = 0;
  while (i < words.length && (ASSIGNMENT.test(words[i]) || LEADING_WRAPPERS.has(headBasename(words[i])))) i++;
  return i < words.length ? { head: headBasename(words[i]), args: words.slice(i + 1) } : null;
}

function honesty(): Verdict {
  return { action: "subset-exceeded", message: WORLD_HONESTY_MESSAGE, rewritten: null };
}

function block(message: string): Verdict {
  return { action: "block", message, rewritten: null };
}

function applyReplacements(command: string, edits: Segment[]): string {
  let out = command;
  for (const edit of [...edits].sort((a, b) => b.start - a.start)) {
    out = out.slice(0, edit.start) + edit.text + out.slice(edit.end);
  }
  return out;
}

export function evaluateRmWorld(world: WorldSpec, command: string): Verdict {
  const vfs = new Vfs(world);
  const rmCalls: RmCall[] = [];
  for (const segment of splitSegments(command)) {
    const parsed = headAndArgs(splitWords(segment.text));
    if (parsed === null) continue;
    if (parsed.head === "eval" || (SHELLS.has(parsed.head) && parsed.args.includes("-c"))) return honesty();
    if (parsed.head === "rm") rmCalls.push({ segment, args: parsed.args });
  }
  if (rmCalls.length === 0) return { action: "pass", message: null, rewritten: null };

  const rewritable = world.trash !== null;
  const edits: Segment[] = [];
  const notes: string[] = [];
  let result: Verdict | null = null;

  for (const call of rmCalls) {
    const targets: RmTarget[] = [];
    let substitution = false;
    let honest = false;
    for (const raw of call.args) {
      const cls = classifyTarget(raw);
      if (cls.kind === "substitution") substitution = true;
      else if (cls.kind === "honesty") honest = true;
      else if (cls.kind === "target") targets.push(cls.target);
    }
    // check_call: an incomplete operand list (a lifted command substitution) blocks first.
    if (substitution) return block(commandSubBlock(call.segment.text));
    if (honest) return honesty();

    let recovery: string | null = null;
    for (const target of targets) {
      const check = checkTarget(vfs, target, rewritable);
      if (check.kind === "honesty") return honesty();
      if (check.kind === "block") return block(check.message);
      if (check.kind === "recoverable" && recovery === null) recovery = check.token;
    }
    if (recovery === null) continue;

    if (rewritable && targets.every((t) => t.emittable)) {
      const args = targets.map((t) => (t.raw.startsWith("-") ? `./${t.raw}` : t.raw));
      edits.push({ ...call.segment, text: [world.trash, ...args].join(" ") });
      notes.push(recoverableNote(recovery));
      result = {
        action: "rewrite",
        message: [...new Set(notes)].join("\n") || null,
        rewritten: applyReplacements(command, edits),
      };
      continue;
    }
    return block(unrecoverableBlock(recovery));
  }
  return result ?? { action: "pass", message: null, rewritten: null };
}
