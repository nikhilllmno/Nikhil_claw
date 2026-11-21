// Key presses for CDP Input.dispatchKeyEvent: "Enter", "Tab", "Control+A", "Shift+ArrowDown".

export interface KeyEvent {
  key: string;
  code: string;
  keyCode: number;
  /** The text that the key types, if any. */
  text: string;
  /** Alt 1, Control 2, Meta 4, Shift 8. */
  modifiers: number;
}

const MODIFIERS: Record<string, number> = { Alt: 1, Control: 2, Ctrl: 2, Meta: 4, Command: 4, Cmd: 4, Shift: 8 };

const NAMED: Record<string, [string, number, string]> = {
  Enter: ["Enter", 13, "\r"],
  Tab: ["Tab", 9, ""],
  Escape: ["Escape", 27, ""],
  Backspace: ["Backspace", 8, ""],
  Delete: ["Delete", 46, ""],
  ArrowUp: ["ArrowUp", 38, ""],
  ArrowDown: ["ArrowDown", 40, ""],
  ArrowLeft: ["ArrowLeft", 37, ""],
  ArrowRight: ["ArrowRight", 39, ""],
  Home: ["Home", 36, ""],
  End: ["End", 35, ""],
  PageUp: ["PageUp", 33, ""],
  PageDown: ["PageDown", 34, ""],
  Space: ["Space", 32, " "],
  " ": ["Space", 32, " "],
};

/** Parses a key such as "Control+A". Returns `null` for a key that is not known. */
export function parseKey(spec: string): KeyEvent | null {
  const parts = spec.split("+").map((part) => part.trim());
  // "Control++" is Control and the plus key.
  if (spec.endsWith("++")) parts.splice(parts.length - 2, 2, "+");
  const name = parts.pop() ?? "";
  let modifiers = 0;
  for (const part of parts) {
    const bit = MODIFIERS[part];
    if (bit === undefined) return null;
    modifiers |= bit;
  }
  const named = NAMED[name];
  if (named) {
    const [code, keyCode, text] = named;
    const key = name === "Space" ? " " : name;
    return { key, code, keyCode, text: modifiers & ~8 ? "" : text, modifiers };
  }
  if (/^F([1-9]|1[0-2])$/.test(name)) {
    return { key: name, code: name, keyCode: 111 + Number(name.slice(1)), text: "", modifiers };
  }
  if ([...name].length !== 1) return null;
  const upper = name.toUpperCase();
  const letter = /^[A-Z]$/.test(upper);
  const digit = /^[0-9]$/.test(name);
  const shifted = (modifiers & 8) !== 0;
  const key = letter ? (shifted ? upper : name.toLowerCase()) : name;
  return {
    key,
    code: letter ? `Key${upper}` : digit ? `Digit${name}` : "",
    keyCode: letter || digit ? upper.charCodeAt(0) : 0,
    // Control, Alt, and Meta shortcuts type no text.
    text: modifiers & 7 ? "" : key,
    modifiers,
  };
}
