// The overlay of the agent in a tab (docs/architecture.md, Section 14.2.2): a bar "sama is acting
// in this tab" with a Stop button, the agent cursor, and a box around the element of the action.
// When the user clicks or presses a key in the page while the agent does not act, the overlay
// tells the extension that the user took over.
//
// `installOverlay` runs in an isolated world of the tab (CDP Page.createIsolatedWorld), so the
// scripts of the page cannot call it. The DOM is shared, so the overlay is in a closed shadow
// root, and the buttons act only on a real click of the user (`isTrusted`). The overlay talks to
// the extension through a CDP binding of the isolated world (Runtime.addBinding).

export const OVERLAY_WORLD = "sama-overlay";
export const OVERLAY_BINDING = "__samaSignal";

export type OverlayMode = "acting" | "paused" | "waiting";
export type OverlaySignal = "stop" | "resume" | "takeover";

/** The functions of the overlay in the isolated world (globalThis.__sama). */
export interface OverlayApi {
  mode(mode: OverlayMode): void;
  move(x: number, y: number): void;
  box(rect: { x: number; y: number; width: number; height: number } | null): void;
  busy(value: boolean): void;
  hidden(value: boolean): void;
  remove(): void;
  /** True when the real overlay is in the page and visible. */
  ok(): boolean;
  /** Puts the real overlay back into the page. */
  repair(): void;
}

/**
 * Runs in the isolated world. It must not use anything from outside the function. The colors are
 * the values of src/panel/tokens.css (the page has no access to the tokens of the extension).
 */
export function installOverlay(binding: string): boolean {
  const scope = globalThis as unknown as { __sama?: OverlayApi } & Record<string, unknown>;
  if (scope.__sama !== undefined) {
    // The world keeps a reference to its own host. An element with the same id from the page
    // is not the overlay.
    if (scope.__sama.ok()) return false;
    scope.__sama.repair();
    return false;
  }
  const signal = (value: OverlaySignal) => {
    const send = scope[binding];
    if (typeof send === "function") (send as (payload: string) => void)(value);
  };
  const host = document.createElement("div");
  host.id = "sama-agent-overlay";
  const HOST_STYLE =
    "all: initial !important; display: block !important; position: fixed !important; inset: 0 !important; " +
    "z-index: 2147483647 !important; pointer-events: none !important; visibility: visible !important; opacity: 1 !important;";
  host.style.cssText = HOST_STYLE;
  const root = host.attachShadow({ mode: "closed" });
  const style = document.createElement("style");
  style.textContent = `
    * { box-sizing: border-box; font-family: ui-sans-serif, system-ui, sans-serif; }
    .bar { position: fixed; top: 0.5rem; left: 50%; transform: translateX(-50%); display: flex;
      align-items: center; gap: 0.75rem; padding: 0.5rem 0.75rem; border-radius: 999px;
      background: #2c6a55; color: #ffffff; font-size: 0.8125rem; line-height: 1.4;
      box-shadow: 0 4px 16px rgb(40 36 20 / 0.1), 0 1px 3px rgb(40 36 20 / 0.08);
      pointer-events: auto; max-width: calc(100vw - 1rem); }
    .bar[data-mode="paused"] { background: #7a5200; }
    .bar[data-mode="waiting"] { background: #1c1e1a; }
    .dot { width: 0.5rem; height: 0.5rem; border-radius: 999px; background: #dfece5; flex: none; }
    button { all: unset; cursor: pointer; padding: 0.25rem 0.75rem; border-radius: 999px;
      background: #ffffff; color: #1c1e1a; font-size: 0.8125rem; font-weight: 600; }
    button:focus-visible { outline: 3px solid #dfece5; outline-offset: 1px; }
    button.ghost { background: transparent; color: #ffffff; border: 1px solid #ffffff; }
    .cursor { position: fixed; left: 0; top: 0; width: 22px; height: 22px; margin: -2px 0 0 -2px;
      transition: transform 250ms ease-out; filter: drop-shadow(0 1px 2px rgb(40 36 20 / 0.3)); }
    .box { position: fixed; border: 3px solid #2c6a55; border-radius: 0.375rem;
      background: rgb(44 106 85 / 0.08); transition: all 150ms ease-out; }
    [hidden] { display: none !important; }
    @media (prefers-reduced-motion: reduce) { .cursor, .box { transition: none; } }
  `;
  const bar = document.createElement("div");
  bar.className = "bar";
  bar.setAttribute("role", "status");
  const dot = document.createElement("span");
  dot.className = "dot";
  const text = document.createElement("span");
  const resume = document.createElement("button");
  resume.textContent = "Give back to sama";
  const stop = document.createElement("button");
  stop.className = "ghost";
  stop.textContent = "Stop";
  bar.append(dot, text, resume, stop);
  const box = document.createElement("div");
  box.className = "box";
  box.hidden = true;
  const cursor = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  cursor.setAttribute("class", "cursor");
  cursor.setAttribute("viewBox", "0 0 22 22");
  const arrow = document.createElementNS("http://www.w3.org/2000/svg", "path");
  arrow.setAttribute("d", "M3 2 L3 18 L7.5 14 L10.5 20.5 L13.5 19 L10.5 12.8 L16.5 12.8 Z");
  arrow.setAttribute("fill", "#2c6a55");
  arrow.setAttribute("stroke", "#ffffff");
  arrow.setAttribute("stroke-width", "1.5");
  cursor.append(arrow);
  cursor.style.transform = `translate(${Math.round(innerWidth / 2)}px, ${Math.round(innerHeight / 2)}px)`;
  root.append(style, box, cursor, bar);
  (document.documentElement ?? document.body).append(host);

  let busy = false;
  let hiddenByAgent = false;
  const setMode = (mode: OverlayMode) => {
    bar.dataset.mode = mode;
    text.textContent =
      mode === "paused"
        ? "You have control. sama is paused."
        : mode === "waiting"
          ? "sama waits for your answer in the sama panel."
          : "sama is acting in this tab";
    resume.hidden = mode !== "paused";
    cursor.style.display = mode === "acting" ? "" : "none";
    if (mode !== "acting") box.hidden = true;
  };
  setMode("acting");

  const onButton = (event: Event, value: OverlaySignal) => {
    event.preventDefault();
    event.stopPropagation();
    if (event.isTrusted) signal(value);
  };
  resume.addEventListener("click", (event) => onButton(event, "resume"));
  stop.addEventListener("click", (event) => onButton(event, "stop"));
  const onInput = (event: Event) => {
    if (!event.isTrusted || busy || bar.dataset.mode !== "acting") return;
    if (event.composedPath().includes(host)) return;
    signal("takeover");
  };
  addEventListener("pointerdown", onInput, true);
  addEventListener("keydown", onInput, true);

  const api: OverlayApi = {
    mode: setMode,
    move: (x, y) => {
      cursor.style.transform = `translate(${Math.round(x)}px, ${Math.round(y)}px)`;
    },
    box: (rect) => {
      if (rect === null) {
        box.hidden = true;
        return;
      }
      box.hidden = false;
      box.style.left = `${rect.x - 4}px`;
      box.style.top = `${rect.y - 4}px`;
      box.style.width = `${rect.width + 8}px`;
      box.style.height = `${rect.height + 8}px`;
    },
    busy: (value) => {
      busy = value;
    },
    hidden: (value) => {
      hiddenByAgent = value;
      host.style.cssText = value ? `${HOST_STYLE} display: none !important;` : HOST_STYLE;
    },
    ok: () =>
      host.isConnected &&
      host.parentNode === (document.documentElement ?? document.body) &&
      (hiddenByAgent || getComputedStyle(host).display !== "none"),
    repair: () => {
      host.style.cssText = hiddenByAgent ? `${HOST_STYLE} display: none !important;` : HOST_STYLE;
      if (!host.isConnected || host.parentNode !== (document.documentElement ?? document.body)) {
        (document.documentElement ?? document.body).append(host);
      }
    },
    remove: () => {
      removeEventListener("pointerdown", onInput, true);
      removeEventListener("keydown", onInput, true);
      host.remove();
      delete scope.__sama;
    },
  };
  scope.__sama = api;
  return true;
}
