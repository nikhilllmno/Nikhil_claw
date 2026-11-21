// The form logic of the custom tools page (docs/architecture.md, Section 10.8). The page keeps
// a draft as text fields; these functions turn a draft into an API body and back, and check it
// before it leaves the page. The server checks everything again.

import type {
  CustomTool,
  CustomToolCreate,
  CustomToolCredential,
  CustomToolDefinition,
  CustomToolKind,
  CustomToolPolicy,
  CustomToolScope,
  CustomToolVersion,
  HttpMethod,
} from "./api";

export const MAX_DESCRIPTION_CHARS = 1000;
export const MAX_CODE_CHARS = 50_000;
export const NAME_PATTERN = /^[a-z][a-z0-9_]{1,39}$/;

export const SCOPE_LABELS: Record<CustomToolScope, string> = {
  user: "Only me",
  workspace: "This workspace",
  org: "The whole org",
};

export const POLICY_LABELS: Record<CustomToolPolicy, string> = {
  allow: "Run without asking",
  ask: "Ask me before each call",
  deny: "Turned off",
};

export type AuthType = "none" | "bearer" | "api_key" | "basic";

/** The text fields of the editor. */
export interface Draft {
  name: string;
  scope: CustomToolScope;
  kind: CustomToolKind;
  description: string;
  policy: CustomToolPolicy;
  schema: string;
  method: HttpMethod;
  url: string;
  headers: string;
  body: string;
  authType: AuthType;
  authWhere: "header" | "query";
  authName: string;
  responsePath: string;
  responseFields: string;
  responseMaxChars: number;
  language: "python" | "node";
  code: string;
  timeout: string;
  summary: string;
  secret: string;
  username: string;
  password: string;
}

const PYTHON_TEMPLATE = `import json
import sys

args = json.load(sys.stdin)  # the arguments of the call
print(json.dumps({"greeting": "Hello, " + args.get("name", "world")}))
`;

const NODE_TEMPLATE = `const args = JSON.parse(require("fs").readFileSync(0, "utf8"));
console.log(JSON.stringify({ greeting: "Hello, " + (args.name ?? "world") }));
`;

const SCHEMA_TEMPLATE = JSON.stringify(
  {
    type: "object",
    properties: { name: { type: "string", description: "Who to greet" } },
    required: ["name"],
  },
  null,
  2,
);

export function emptyDraft(kind: CustomToolKind = "http"): Draft {
  return {
    name: "",
    scope: "user",
    kind,
    description: "",
    policy: "ask",
    schema: SCHEMA_TEMPLATE,
    method: "GET",
    url: "https://api.example.com/greet?name={{name}}",
    headers: "",
    body: "",
    authType: "none",
    authWhere: "header",
    authName: "X-Api-Key",
    responsePath: "",
    responseFields: "",
    responseMaxChars: 20_000,
    language: "python",
    code: PYTHON_TEMPLATE,
    timeout: "30",
    summary: "",
    secret: "",
    username: "",
    password: "",
  };
}

export function codeTemplate(language: "python" | "node"): string {
  return language === "node" ? NODE_TEMPLATE : PYTHON_TEMPLATE;
}

/** A draft with the definition of a version, for an edit. The credential fields stay empty. */
export function draftOf(tool: CustomTool, version: CustomToolVersion): Draft {
  const draft = {
    ...emptyDraft(version.kind),
    name: tool.name,
    scope: tool.scope,
  };
  draft.description = version.description;
  draft.policy = version.policy;
  draft.schema = JSON.stringify(version.input_schema, null, 2);
  if (version.http) {
    const http = version.http;
    draft.method = http.method;
    draft.url = http.url;
    draft.headers = Object.entries(http.headers ?? {})
      .map(([name, value]) => `${name}: ${value}`)
      .join("\n");
    draft.body = http.body === null || http.body === undefined ? "" : JSON.stringify(http.body, null, 2);
    draft.authType = http.auth.type;
    draft.authWhere = http.auth.query ? "query" : "header";
    draft.authName = http.auth.query ?? http.auth.header ?? "X-Api-Key";
    draft.responsePath = http.response.path ?? "";
    draft.responseFields = (http.response.fields ?? []).join(", ");
    draft.responseMaxChars = http.response.max_chars ?? 20_000;
  }
  if (version.script) {
    draft.language = version.script.language;
    draft.code = version.script.code;
    draft.timeout = String(version.script.timeout_s);
  }
  return draft;
}

/** `Name: value` lines to an object. */
export function parseHeaders(text: string): Record<string, string> | string {
  const headers: Record<string, string> = {};
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const at = line.indexOf(":");
    if (at <= 0) return `Write each header as "Name: value". This line has no name: ${line.slice(0, 40)}`;
    headers[line.slice(0, at).trim()] = line.slice(at + 1).trim();
  }
  return headers;
}

function parseJson(text: string, what: string): { value: unknown } | string {
  try {
    return { value: JSON.parse(text) as unknown };
  } catch {
    return `${what} is not valid JSON.`;
  }
}

/** The definition of a draft, or the first problem of the draft. */
export function definitionOf(draft: Draft): CustomToolDefinition | string {
  if (!draft.description.trim()) return "Write a description. The agent reads it to pick the tool.";
  if (draft.description.length > MAX_DESCRIPTION_CHARS)
    return `The description has more than ${MAX_DESCRIPTION_CHARS} characters.`;
  const schema = parseJson(draft.schema, "The input schema");
  if (typeof schema === "string") return schema;
  const value = schema.value as Record<string, unknown> | null;
  if (value === null || typeof value !== "object" || Array.isArray(value) || value.type !== "object")
    return 'The input schema must be a JSON object with "type": "object".';
  const base = {
    kind: draft.kind,
    description: draft.description.trim(),
    input_schema: value,
    policy: draft.policy,
  };
  if (draft.kind === "script") {
    if (!draft.code.trim()) return "Write the script.";
    if (draft.code.length > MAX_CODE_CHARS) return `The script has more than ${MAX_CODE_CHARS} characters.`;
    const timeout = Number(draft.timeout);
    if (!Number.isInteger(timeout) || timeout < 1 || timeout > 120) return "The time limit must be 1 to 120 seconds.";
    return {
      ...base,
      script: {
        language: draft.language,
        code: draft.code,
        timeout_s: timeout,
      },
    };
  }
  if (!/^https:\/\/[^/?#{}\s]+/.test(draft.url.trim()))
    return "The URL must start with https:// and a host. A placeholder can be in the path and the query only.";
  const headers = parseHeaders(draft.headers);
  if (typeof headers === "string") return headers;
  let body: unknown = null;
  if (draft.body.trim()) {
    if (draft.method === "GET" || draft.method === "HEAD") return `A ${draft.method} request has no body.`;
    const parsed = parseJson(draft.body, "The body");
    if (typeof parsed === "string") return parsed;
    body = parsed.value;
  }
  const auth =
    draft.authType === "api_key"
      ? draft.authWhere === "query"
        ? { type: "api_key" as const, query: draft.authName.trim() }
        : { type: "api_key" as const, header: draft.authName.trim() }
      : { type: draft.authType };
  if (draft.authType === "api_key" && !draft.authName.trim())
    return "Give the name of the API key header or parameter.";
  const fields = draft.responseFields
    .split(",")
    .map((f) => f.trim())
    .filter(Boolean);
  return {
    ...base,
    http: {
      method: draft.method,
      url: draft.url.trim(),
      headers,
      body,
      auth,
      response: {
        path: draft.responsePath.trim() || null,
        fields,
        max_chars: draft.responseMaxChars,
      },
    },
  };
}

/** The credential of a draft, or null when the draft has none. */
export function credentialOf(draft: Draft): CustomToolCredential | null {
  if (draft.kind !== "http" || draft.authType === "none") return null;
  if (draft.authType === "basic") {
    return draft.username && draft.password ? { username: draft.username, password: draft.password } : null;
  }
  return draft.secret ? { value: draft.secret } : null;
}

/** The body of a new tool, or the first problem of the draft. */
export function createBody(draft: Draft): CustomToolCreate | string {
  if (!NAME_PATTERN.test(draft.name))
    return "The name must have 2 to 40 characters: lower-case letters, digits, and _, and start with a letter.";
  const definition = definitionOf(draft);
  if (typeof definition === "string") return definition;
  const credential = credentialOf(draft);
  return {
    ...definition,
    name: draft.name,
    scope: draft.scope,
    summary: draft.summary.trim(),
    credential,
  };
}

/** The arguments of a test, from the text of the test box. */
export function testArguments(text: string): Record<string, unknown> | string {
  if (!text.trim()) return {};
  const parsed = parseJson(text, "The arguments");
  if (typeof parsed === "string") return parsed;
  const value = parsed.value;
  if (value === null || typeof value !== "object" || Array.isArray(value))
    return "The arguments must be a JSON object.";
  return value as Record<string, unknown>;
}

/** A short state line of a tool for the list. */
export function toolState(tool: CustomTool): string {
  if (tool.current_version === null || tool.current === null) return "No version runs";
  return `Version ${tool.current_version} · ${POLICY_LABELS[tool.current.policy]}`;
}

/** The scopes that the create form offers: the scopes that the API says the user can write. */
export function scopeChoices(writable: CustomToolScope[] | null): CustomToolScope[] {
  const order: CustomToolScope[] = ["user", "workspace", "org"];
  return writable === null ? ["user"] : order.filter((scope) => writable.includes(scope));
}
