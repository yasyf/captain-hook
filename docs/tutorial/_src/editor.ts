// CodeMirror 6 code panel, isolated into widgets/editor.js so emulator.js never bundles
// CodeMirror. The core dynamic-imports this by URL and calls createEditor on first interaction.

import { EditorView, minimalSetup } from "codemirror";
import { python } from "@codemirror/lang-python";
import { setDiagnostics } from "@codemirror/lint";
import { EditorState } from "@codemirror/state";

import type { EditorDiagnostic, EditorHandle, EditorOptions } from "./specs";

// Colors resolve against the .ch-widget CSS custom properties, so the editor tracks light/dark.
const THEME = EditorView.theme({
  "&": {
    fontSize: "0.85rem",
    backgroundColor: "var(--ch-code-bg, #fff)",
    color: "var(--ch-text)",
    borderRadius: "6px",
  },
  ".cm-content": {
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
    padding: "0.6rem 0",
  },
  ".cm-line": { padding: "0 0.7rem" },
  "&.cm-focused": { outline: "2px solid var(--ch-accent)", outlineOffset: "1px" },
  ".cm-gutters": { display: "none" },
  ".cm-cursor": { borderLeftColor: "var(--ch-text)" },
  ".cm-selectionBackground, &.cm-focused .cm-selectionBackground": {
    backgroundColor: "var(--ch-accent)",
    opacity: "0.25",
  },
});

function toCmDiagnostic(view: EditorView, d: EditorDiagnostic) {
  const max = view.state.doc.length;
  return {
    from: Math.min(d.from, max),
    to: Math.min(Math.max(d.to, d.from), max),
    severity: d.severity ?? "error",
    message: d.message,
  };
}

export function createEditor(options: EditorOptions): EditorHandle {
  const listener = EditorView.updateListener.of((update) => {
    if (update.docChanged) options.onChange?.(update.state.doc.toString());
  });
  const view = new EditorView({
    parent: options.parent,
    doc: options.doc,
    extensions: [
      minimalSetup,
      python(),
      THEME,
      EditorView.lineWrapping,
      EditorView.editable.of(!options.readOnly),
      EditorState.readOnly.of(Boolean(options.readOnly)),
      listener,
    ],
  });
  return {
    getDoc: () => view.state.doc.toString(),
    setDoc: (doc) => view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: doc } }),
    setDiagnostics: (diagnostics) =>
      view.dispatch(setDiagnostics(view.state, diagnostics.map((d) => toCmDiagnostic(view, d)))),
    destroy: () => view.destroy(),
  };
}
