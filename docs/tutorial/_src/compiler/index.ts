// In-browser DSL compiler entry: parse a docs fragment with @lezer/python and lower it to the
// same SerializedHook[] the Python widget compiler emits, or report why the fragment is refused.

import { parser } from "@lezer/python";

import type { SerializedHook } from "../specs";
import { lower } from "./lower";
import { CompileError, validate } from "./validate";

export type CompileResult = { hooks: SerializedHook[] } | { error: string };

export function compileSource(source: string): CompileResult {
  try {
    const tree = parser.parse(source);
    validate(tree, source);
    return { hooks: lower(tree, source) };
  } catch (e) {
    if (e instanceof CompileError) return { error: e.message };
    throw e;
  }
}
