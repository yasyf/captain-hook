// A WAI-ARIA combobox over the widget's cases: substring filter, ArrowUp/Down + Enter, Escape
// closes, opens on focus or chevron. Free-typed text still flows to onType for live command eval.

import { el } from "./dom";

export interface ComboboxItem {
  label: string;
  index: number;
}

export interface ComboboxOptions {
  items: ComboboxItem[];
  placeholder: string;
  ariaLabel: string;
  onSelect: (index: number) => void;
  onType?: (text: string) => void;
}

export interface Combobox {
  root: HTMLElement;
  input: HTMLInputElement;
  setValue(text: string): void;
}

let counter = 0;

export function createCombobox(opts: ComboboxOptions): Combobox {
  const listId = `ch-widget-listbox-${counter++}`;
  const root = el("div", "ch-widget-combobox");
  const input = el("input", "ch-widget-input");
  input.type = "text";
  input.spellcheck = false;
  input.placeholder = opts.placeholder;
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-label", opts.ariaLabel);
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-expanded", "false");
  input.setAttribute("aria-controls", listId);

  const toggle = el("button", "ch-widget-combobox-toggle", "▾");
  toggle.type = "button";
  toggle.tabIndex = -1;
  toggle.setAttribute("aria-label", "show scenarios");

  const listbox = el("ul", "ch-widget-listbox");
  listbox.id = listId;
  listbox.setAttribute("role", "listbox");
  listbox.hidden = true;

  let filtered: ComboboxItem[] = [];
  let active = -1;
  let open = false;

  const optionId = (i: number) => `${listId}-opt-${i}`;

  const setOpen = (next: boolean) => {
    open = next;
    listbox.hidden = !next;
    input.setAttribute("aria-expanded", String(next));
    if (!next) {
      active = -1;
      input.removeAttribute("aria-activedescendant");
    }
  };

  const highlight = () => {
    for (const [i, li] of Array.from(listbox.children).entries()) {
      const on = i === active;
      li.classList.toggle("ch-widget-option--active", on);
      li.setAttribute("aria-selected", String(on));
    }
    input.setAttribute("aria-activedescendant", active >= 0 ? optionId(active) : "");
    if (active >= 0) listbox.children[active]?.scrollIntoView({ block: "nearest" });
  };

  const paint = (query: string) => {
    const needle = query.trim().toLowerCase();
    filtered = needle
      ? opts.items.filter((it) => it.label.toLowerCase().includes(needle))
      : opts.items.slice();
    listbox.textContent = "";
    filtered.forEach((it, i) => {
      const li = el("li", "ch-widget-option", it.label);
      li.id = optionId(i);
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", "false");
      li.addEventListener("mousedown", (e) => e.preventDefault());
      li.addEventListener("click", () => choose(i));
      listbox.append(li);
    });
    active = -1;
    setOpen(filtered.length > 0);
    highlight();
  };

  const choose = (i: number) => {
    const item = filtered[i];
    if (!item) return;
    setOpen(false);
    opts.onSelect(item.index);
  };

  const move = (delta: number) => {
    if (!open) {
      paint(input.value);
      return;
    }
    if (filtered.length === 0) return;
    active = (active + delta + filtered.length) % filtered.length;
    highlight();
  };

  input.addEventListener("input", () => {
    paint(input.value);
    opts.onType?.(input.value);
  });
  input.addEventListener("focus", () => paint(input.value));
  input.addEventListener("keydown", (e) => {
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        move(1);
        break;
      case "ArrowUp":
        e.preventDefault();
        move(-1);
        break;
      case "Enter":
        if (open && active >= 0) {
          e.preventDefault();
          choose(active);
        }
        break;
      case "Escape":
        if (open) {
          e.preventDefault();
          setOpen(false);
        }
        break;
    }
  });
  input.addEventListener("blur", () => setOpen(false));
  toggle.addEventListener("mousedown", (e) => e.preventDefault());
  toggle.addEventListener("click", () => {
    open ? setOpen(false) : paint(input.value);
    input.focus();
  });

  root.append(input, toggle, listbox);
  return { root, input, setValue: (text: string) => (input.value = text) };
}
