// The connect card of `connection_request` and `connection_add_mcp` (architecture §10.3.4, M8.1).
// The tool answer ends with `<sama-connect>{json}</sama-connect>`. The chat shows it as a
// Connect button. Only an `https` link without a user name or a password is kept.

export interface ConnectCardData {
  connector: string;
  title: string;
  url: string | null;
  connectionId: string;
  state: string;
  scope: string;
}

const CARD = /<sama-connect>(\{.*?\})<\/sama-connect>/g;
export const CONNECT_TOOLS = new Set(["connection_request", "connection_add_mcp"]);

/** The bare tool name: `mcp__sama__connection_request` gives `connection_request`. */
export function bareToolName(name: string): string {
  return name.replace(/^mcp__sama__/, "");
}

export function safeLink(value: unknown): string | null {
  if (typeof value !== "string") return null;
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username !== "" || url.password !== "") return null;
    return url.toString();
  } catch {
    return null;
  }
}

/**
 * The JSON of a card. The OpenAI runtime gives the tool output as JSON text blocks
 * (`{"type": "text", "text": "..."}`), so the quotes of the card can be escaped.
 */
function cardJson(raw: string): unknown {
  try {
    return JSON.parse(raw);
  } catch {
    // An escaped card: decode it as the body of a JSON string first.
  }
  try {
    return JSON.parse(JSON.parse(`"${raw}"`) as string);
  } catch {
    return null;
  }
}

/** The cards in a tool answer, in order. */
export function parseConnectCards(text: string): ConnectCardData[] {
  const out: ConnectCardData[] = [];
  for (const match of (text ?? "").matchAll(CARD)) {
    const data = cardJson(match[1] ?? "");
    if (typeof data !== "object" || data === null) continue;
    const raw = data as Record<string, unknown>;
    if (typeof raw.title !== "string" || typeof raw.connector !== "string") continue;
    out.push({
      connector: raw.connector,
      title: raw.title,
      url: safeLink(raw.url),
      connectionId: typeof raw.connection_id === "string" ? raw.connection_id : "",
      state: typeof raw.state === "string" ? raw.state : "",
      scope: typeof raw.scope === "string" ? raw.scope : "personal",
    });
  }
  return out;
}

/** The cards of the finished connect calls of a run. */
export function cardsOfCalls(calls: { name: string; status: string; preview: string }[]): ConnectCardData[] {
  return calls
    .filter((call) => call.status === "done" && CONNECT_TOOLS.has(bareToolName(call.name)))
    .flatMap((call) => parseConnectCards(call.preview));
}

/** The text of a card for a state of the connection. */
export function cardStatus(state: string, title: string): string {
  switch (state) {
    case "connected":
      return `${title} is connected. sama continues your request.`;
    case "form":
      return `${title} needs a key. Fill the form on the Connections page. Never paste it in the chat.`;
    case "choice":
      return `Connect ${title} if this is the one you mean.`;
    case "failed":
    case "expired":
      return `${title} is not connected. Ask sama to connect it again.`;
    default:
      return `Sign in to connect ${title}. sama continues your request when you finish.`;
  }
}
