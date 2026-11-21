// The questions of the extension (lib/prompter.ts) as cards with buttons. The side panel and the
// sama window (prompt.html) both use it. The site and the label come from the web: use
// textContent only.

import type { Prompt, PromptChoice } from "../lib/messages";

function node(tag: string, className: string, text?: string): HTMLElement {
  const element = document.createElement(tag);
  element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function button(label: string, primary: boolean, onClick: () => void): HTMLButtonElement {
  const element = node("button", primary ? "primary" : "", label) as HTMLButtonElement;
  element.type = "button";
  element.addEventListener("click", onClick);
  return element;
}

/** One card for each open question. `answer` sends the choice to the service worker. */
export function renderPrompts(
  container: HTMLElement,
  prompts: Prompt[],
  answer: (id: string, choice: PromptChoice) => void,
): void {
  container.replaceChildren(
    ...prompts.map((prompt) => {
      const card = node("div", "approval prompt");
      card.setAttribute("role", "alertdialog");
      const row = node("div", "row");
      if (prompt.kind === "site") {
        card.append(node("strong", "", `Let sama act on ${prompt.site}?`));
        card.append(
          node(
            "p",
            "prompt-text",
            "sama asks to read and use this site in your Chrome. You see its cursor, and you can stop it at any time.",
          ),
        );
        row.append(
          button("Allow on this site", true, () => answer(prompt.id, "allow")),
          button("Allow once", false, () => answer(prompt.id, "once")),
          button("Not now", false, () => answer(prompt.id, "deny")),
          button("Never", false, () => answer(prompt.id, "never")),
        );
      } else {
        card.append(node("strong", "", `Allow this action on ${prompt.site}?`));
        const what = prompt.label ? `sama asks to ${prompt.reason}: "${prompt.label}".` : `sama asks to ${prompt.reason}.`;
        card.append(node("p", "prompt-text", what));
        row.append(
          button("Allow", true, () => answer(prompt.id, "allow")),
          button("Deny", false, () => answer(prompt.id, "deny")),
        );
      }
      card.append(row);
      return card;
    }),
  );
}
