// Structural gate over the @lezer/python tree: any error node refuses, and only the shape
// widget_compiler.compile_fragment accepts (top-level from-imports and primitive calls) survives.

import type { Tree, SyntaxNode } from "@lezer/common";

// Optional 0-based source span so callers can surface a CodeMirror lint squiggle at the offense.
export interface Pos {
  from: number;
  to: number;
}

export class CompileError extends Error {
  constructor(
    message: string,
    readonly pos?: Pos,
  ) {
    super(message);
  }
}

// @lezer/python node name -> the construct name surfaced in the refusal message.
const REFUSED_CONSTRUCT: Record<string, string> = {
  FunctionDefinition: "function def",
  ClassDefinition: "class definition",
  DecoratedStatement: "decorator",
  Decorator: "decorator",
  LambdaExpression: "lambda",
  FormatString: "f-string",
  ArrayComprehensionExpression: "comprehension",
  SetComprehensionExpression: "comprehension",
  DictionaryComprehensionExpression: "comprehension",
  ComprehensionExpression: "comprehension",
  AssignStatement: "assignment",
  UpdateStatement: "augmented assignment",
  AssignmentExpression: "walrus assignment",
  ConditionalExpression: "conditional expression",
  AwaitExpression: "await",
  YieldExpression: "yield",
  IfStatement: "if statement",
  ForStatement: "for loop",
  WhileStatement: "while loop",
  WithStatement: "with statement",
  TryStatement: "try/except",
  MatchStatement: "match statement",
  GlobalStatement: "global statement",
  NonlocalStatement: "nonlocal statement",
  DeleteStatement: "del statement",
  AssertStatement: "assert statement",
  RaiseStatement: "raise statement",
  ReturnStatement: "return statement",
  PassStatement: "pass statement",
};

function snippet(source: string, from: number, to: number): string {
  const text = source.slice(from, to).replace(/\s+/g, " ").trim();
  return text.length > 40 ? `${text.slice(0, 40)}…` : text;
}

function firstMeaningfulChild(node: SyntaxNode): SyntaxNode | null {
  for (let c = node.firstChild; c; c = c.nextSibling) {
    if (c.name !== "Comment") return c;
  }
  return null;
}

export function validate(tree: Tree, source: string): void {
  const cursor = tree.cursor();
  do {
    if (cursor.type.isError) {
      throw new CompileError(`syntax error near "${snippet(source, cursor.from, cursor.to)}"`, {
        from: cursor.from,
        to: cursor.to,
      });
    }
    const construct = REFUSED_CONSTRUCT[cursor.name];
    if (construct) {
      throw new CompileError(`unsupported construct: ${construct}`, { from: cursor.from, to: cursor.to });
    }
  } while (cursor.next());

  for (let child = tree.topNode.firstChild; child; child = child.nextSibling) {
    if (child.name === "Comment" || child.name === "ImportStatement") continue;
    if (child.name === "ExpressionStatement") {
      const inner = firstMeaningfulChild(child);
      if (!inner || inner.name !== "CallExpression") {
        throw new CompileError(
          `top-level statements must be hook primitive calls, got "${snippet(source, child.from, child.to)}"`,
          { from: child.from, to: child.to },
        );
      }
      continue;
    }
    const construct = REFUSED_CONSTRUCT[child.name] ?? child.name;
    throw new CompileError(`unsupported top-level statement: ${construct}`, { from: child.from, to: child.to });
  }
}
