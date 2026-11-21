// The sama window: the questions of the extension when no side panel is open (lib/prompter.ts).
// It closes itself when no question is left.

import { PORT_NAME, type PanelToWorker, type WorkerToPanel } from "../lib/messages";
import { renderPrompts } from "./prompts";

const container = document.getElementById("prompts") as HTMLElement;
const empty = document.getElementById("empty") as HTMLElement;
const port = chrome.runtime.connect({ name: PORT_NAME });
let nextId = 1;
let seen = false;

port.onMessage.addListener((message: WorkerToPanel) => {
  if (message.kind !== "prompts") return;
  renderPrompts(container, message.prompts, (promptId, choice) => {
    const request: PanelToWorker = { id: nextId++, call: "answerPrompt", promptId, choice };
    port.postMessage(request);
  });
  empty.hidden = message.prompts.length > 0;
  if (message.prompts.length > 0) seen = true;
  else if (seen) setTimeout(() => window.close(), 300);
});
