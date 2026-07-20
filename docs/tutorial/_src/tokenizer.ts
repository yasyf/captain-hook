// Quote-aware bash tokenizer for the tutorial subset: the raw line plus, per command,
// an env-stripped, dequoted, redirect-dropped argv. detectHonesty refuses real expansion.

export interface ParsedCommand {
  argv: string[];
  text: string;
}

export interface CommandLine {
  raw: string;
  commands: ParsedCommand[];
}

const ASSIGNMENT = /^[A-Za-z_][A-Za-z0-9_]*=/;

export function detectHonesty(raw: string): boolean {
  let singleQuoted = false;
  for (let i = 0; i < raw.length; i++) {
    const c = raw[i];
    const n = raw[i + 1];
    if (singleQuoted) {
      if (c === "'") singleQuoted = false;
      continue;
    }
    if (c === "'") {
      singleQuoted = true;
      continue;
    }
    if (c === "`") return true;
    if (c === "$" && (n === "(" || n === "{")) return true;
    if (c === "<" && (n === "(" || n === "<")) return true;
    if (c === ">" && n === "(") return true;
  }
  return false;
}

function splitSegments(raw: string): string[] {
  const segments: string[] = [];
  let cur = "";
  let quote: "'" | '"' | null = null;
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
      // A lone & backgrounds a job; &> and >& are redirects and stay in the word.
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

function splitWords(segment: string): string[] {
  const words: string[] = [];
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
    if (c === " " || c === "\t") {
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

function redirectKind(word: string): "operator" | "attached" | null {
  if (/^&?\d*(>>|>|<)$/.test(word)) return "operator";
  if (/^&?\d*(>>|>|<)./.test(word)) return "attached";
  return null;
}

function parseCommand(segment: string): ParsedCommand | null {
  const words = splitWords(segment);
  const kept: string[] = [];
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

export function tokenize(raw: string): CommandLine {
  const commands: ParsedCommand[] = [];
  for (const segment of splitSegments(raw)) {
    const command = parseCommand(segment);
    if (command) commands.push(command);
  }
  return { raw, commands };
}
