// The help pages of the web app (docs/architecture.md, Section 17.6, M6.9). The text is for
// customers, in ASD-STE100 Simplified Technical English. Each topic has short sections and
// links to the pages that it explains.

export interface HelpSection {
  heading: string;
  paragraphs?: string[];
  steps?: string[];
  items?: string[];
}

export interface HelpTopic {
  id: string;
  title: string;
  summary: string;
  /** Who uses the topic: everyone, or an org owner or admin. */
  audience: "everyone" | "admin";
  sections: HelpSection[];
  links: { href: string; label: string }[];
}

export const HELP_TOPICS: HelpTopic[] = [
  {
    id: "getting-started",
    title: "Getting started",
    summary: "Create an org, connect a chat app, pick an agent, and send your first message.",
    audience: "everyone",
    sections: [
      {
        heading: "What sama is",
        paragraphs: [
          "Sama gives your team AI agents that work for you. An agent reads your messages, uses tools, and asks you before it does something risky.",
          "You talk to an agent in the web app, in Telegram, or in Slack. Each conversation is a session.",
        ],
      },
      {
        heading: "Set up your org",
        steps: [
          "Create an account on the sign-in page with your email address. Confirm the email.",
          "Create an org. You become its owner. Sama adds a workspace called Main and an agent called sama.",
          "Follow the setup guide: choose a plan, connect a chat app, pick or create an agent, and send a first message.",
          "Invite your team on the Members page.",
        ],
      },
      {
        heading: "Words that sama uses",
        items: [
          "Org: your company or team. It has members, workspaces, agents, and a plan.",
          "Workspace: a group of sessions, notes, and tools. A member can see only the workspaces that an admin gives.",
          "Agent: an AI assistant with a soul (its instructions), a model, and tools.",
          "Session: one conversation with an agent. A private session is only for you. A shared session is for the workspace.",
        ],
      },
    ],
    links: [
      { href: "/onboarding", label: "Open the setup guide" },
      { href: "/", label: "Start a session" },
    ],
  },
  {
    id: "sessions",
    title: "Sessions and chat",
    summary: "Start a session, answer approvals and questions, stop a run, and export a transcript.",
    audience: "everyone",
    sections: [
      {
        heading: "Start a session",
        steps: [
          "Click New session in the sidebar.",
          "Choose the workspace and the agent.",
          "Choose who can see it: only you, or everyone in the workspace.",
          "Click Start session, then type your message.",
        ],
      },
      {
        heading: "Approvals and questions",
        paragraphs: [
          "Before the agent does something risky, for example a change to a file or a message to another person, it shows an approval card. Click Approve, Always allow, or Deny.",
          "When the agent needs more information, it shows a question card. Type or choose your answer.",
        ],
      },
      {
        heading: "Control a run",
        items: [
          "Stop: ends the run that is working now.",
          "A message while the agent works: sama adds it as a note to the run.",
          "/compact: makes the history of a long session shorter, so the agent stays fast.",
          "/undo and /rollback: put the files of the session back to an earlier checkpoint.",
        ],
      },
      {
        heading: "Export and runs",
        paragraphs: [
          "The chat header has Markdown and JSON export buttons. Runs shows each run with its tokens, its cost, and a trace of its steps.",
        ],
      },
    ],
    links: [
      { href: "/", label: "New session" },
      { href: "/search", label: "Search your sessions" },
    ],
  },
  {
    id: "agents",
    title: "Agents",
    summary: "Change the soul, the model, and the tools of an agent, and revert a change.",
    audience: "everyone",
    sections: [
      {
        heading: "Agent settings",
        paragraphs: [
          "Open an agent from the sidebar. Every member can read the settings. Only an org owner or admin can change them.",
          "A change applies to new sessions. A session that is open keeps the settings that it started with. A tool that you remove stops at once.",
        ],
      },
      {
        heading: "Create an agent",
        steps: [
          "Open the setup guide, or the Agents part of the Settings page.",
          "Give a name and a soul. The soul tells the agent who it is and how it works.",
          "Choose the workspace that can use it, or the whole org.",
        ],
      },
      {
        heading: "History and revert",
        paragraphs: [
          "Each change to an agent is kept in its history: who changed it, when, and what changed. An admin can revert a change. The Admin area shows the changes of all agents in one list.",
          "The agent can also propose a change to itself. A person approves the proposal on an approval card.",
        ],
      },
    ],
    links: [
      { href: "/settings", label: "Agents on the Settings page" },
      { href: "/admin/changes", label: "Change history" },
    ],
  },
  {
    id: "chat-apps",
    title: "Telegram and Slack",
    summary: "Connect the bot of your org and link your chat account to an agent.",
    audience: "everyone",
    sections: [
      {
        heading: "Connect Telegram (an admin)",
        steps: [
          "In Telegram, open @BotFather and make a new bot. Copy its token.",
          "In sama, open Settings, then Chat apps. Paste the token and click Connect.",
          "Never paste a token in a chat with an agent.",
        ],
      },
      {
        heading: "Connect Slack (an admin)",
        steps: [
          "In sama, open Settings, then Chat apps, and click Set up Slack.",
          "Click Create the Slack app. Slack opens with the settings of the app filled in.",
          "Install the app in your Slack workspace.",
          "Copy the Bot User OAuth Token and the Signing Secret from Slack, paste them in sama, and click Connect.",
        ],
      },
      {
        heading: "Link your account",
        paragraphs: [
          "On the Settings page, under Linked chat accounts, click Get a link code. Send the code to the bot. Then your messages to the bot go to the agent that you chose.",
        ],
      },
    ],
    links: [{ href: "/settings", label: "Open Settings" }],
  },
  {
    id: "tools",
    title: "Connections, memory, and skills",
    summary: "Give the agents tools, keep notes, and teach them how your team works.",
    audience: "everyone",
    sections: [
      {
        heading: "Connections",
        paragraphs: [
          "A connection gives the agents a tool: an app such as GitHub or Gmail, an MCP server, an HTTP API, or a database. Sama keeps the secrets of a connection in its vault. The agent never sees them.",
        ],
      },
      {
        heading: "Browser logins",
        paragraphs: [
          "On the Settings page, the Browser card keeps saved logins for the sites that the browser of the agents opens. The agent never sees the user name or the password. An admin adds logins for the whole org and chooses the sites that the browser can open.",
        ],
      },
      {
        heading: "Memory",
        paragraphs: [
          "The agents keep short notes: notes about the workspace, notes about you, and notes of each agent. You can read, change, and revert the notes on the Memory page.",
        ],
      },
      {
        heading: "Skills",
        paragraphs: [
          "A skill is a guide that an agent follows for one kind of task. Agents can write skills. Each week the curator marks the skills that nobody uses as stale, and later archives them. It never deletes a skill. You can pin a skill to keep it.",
        ],
      },
    ],
    links: [
      { href: "/connections", label: "Connections" },
      { href: "/memory", label: "Memory" },
      { href: "/skills", label: "Skills" },
    ],
  },
  {
    id: "jobs",
    title: "Jobs and the task board",
    summary: "Run an agent on a schedule, and give cards of work to agents and people.",
    audience: "everyone",
    sections: [
      {
        heading: "Jobs",
        paragraphs: [
          "A job runs an agent on a schedule, for example each Monday at 9:00. No person is there to approve a tool during a job, so a risky tool is denied unless a person approved it for that job before.",
        ],
      },
      {
        heading: "Task board",
        paragraphs: [
          "The task board has cards in columns. Assign a card to an agent and it starts to work. The card shows its status and its comments.",
        ],
      },
    ],
    links: [
      { href: "/jobs", label: "Jobs" },
      { href: "/tasks", label: "Task board" },
    ],
  },
  {
    id: "members",
    title: "Members and roles",
    summary: "Invite people, change roles, and choose the workspaces of each member.",
    audience: "admin",
    sections: [
      {
        heading: "Roles",
        items: [
          "Owner: does everything, also export and delete the org. An org has at least one owner.",
          "Admin: manages members, agents, chat apps, keys, and the data settings.",
          "Member: uses the workspaces that an admin gives.",
        ],
      },
      {
        heading: "Invite a person",
        steps: [
          "Open Members and type the email address of the person.",
          "Choose the role and, for a member, the workspace.",
          "Copy the invite link and send it to the person. Sama does not send email.",
          "The link works for 7 days and only for that email address.",
        ],
      },
    ],
    links: [{ href: "/members", label: "Members" }],
  },
  {
    id: "admin",
    title: "Admin: usage, audit log, and keys",
    summary: "See the cost of your org, read the audit log, and use your own model keys.",
    audience: "admin",
    sections: [
      {
        heading: "Usage",
        paragraphs: [
          "The Usage page shows the model cost and the tokens of your org for 7, 30, or 90 days: for each day, each person, each agent, and each model.",
        ],
      },
      {
        heading: "Audit log",
        paragraphs: [
          "The audit log records the important changes in your org: members, invites, agents, keys, chat apps, data controls, and skills. Nobody can change or delete an entry.",
        ],
      },
      {
        heading: "Org keys and region",
        paragraphs: [
          "Your org can use its own Anthropic and OpenAI keys. Sama checks a key with the provider before it keeps it. The key goes in the vault and never shows again.",
          "Each org has a region. Your data stays in that region. An operator moves an org to another region.",
        ],
      },
      {
        heading: "API keys",
        paragraphs: [
          "Other agents and apps can use sama with an API key: the OpenAI-compatible chat endpoint at /v1 and the MCP server at /mcp. A key acts as the admin who made it, in one workspace.",
          "The runs of a key use the policy, the approvals, and the budgets of the agent. With Deny, a tool call that needs an approval is denied. With Wait, it waits for a person in the web app.",
          "Sama shows a new key one time. Revoke a key when you do not need it. A key also stops when its admin leaves the org or is no longer an admin.",
        ],
      },
      {
        heading: "Curator reports",
        paragraphs: [
          "Each week the curator checks the skills and the memory of each workspace. Its reports show the stale and archived skills and the memory that is nearly full.",
        ],
      },
    ],
    links: [
      { href: "/admin", label: "Open the Admin area" },
      { href: "/admin/usage", label: "Usage" },
      { href: "/admin/audit", label: "Audit log" },
      { href: "/admin/api-keys", label: "API keys" },
    ],
  },
  {
    id: "billing",
    title: "Plans and billing",
    summary: "Choose a plan, see the cost against the quotas, and change or cancel the plan.",
    audience: "admin",
    sections: [
      {
        heading: "Plans",
        paragraphs: [
          "A new org starts on the free plan. Each plan has quotas: the model cost in each month, the model cost of each person in each day, and the number of members.",
          "When the org uses all of a quota, sama stops new runs and shows a message with a link to the Billing page.",
        ],
      },
      {
        heading: "Buy or change a plan (an owner or an admin)",
        steps: [
          "Open Billing in the sidebar.",
          "Click the button of the plan that you want. Stripe opens and asks for the card.",
          "After the payment, sama shows the new plan. This can take a few seconds.",
          "To change the card, see the invoices, or cancel, click Payment method, invoices, and cancel.",
        ],
      },
      {
        heading: "Your own model keys",
        paragraphs: [
          "When the org uses its own Anthropic or OpenAI key, the cost of those calls goes to your key. The Usage page shows that cost as On org keys.",
        ],
      },
    ],
    links: [
      { href: "/billing", label: "Billing" },
      { href: "/admin/usage", label: "Usage" },
    ],
  },
  {
    id: "data",
    title: "Your data and privacy",
    summary: "Export your data, delete it, and set how long sama keeps sessions.",
    audience: "everyone",
    sections: [
      {
        heading: "Export",
        paragraphs: [
          "You can download a ZIP of your own data in an org from the Settings page. An org owner can download all the data of the org from the Admin area.",
          "An export never has a secret: no keys, tokens, or passwords.",
        ],
      },
      {
        heading: "Delete",
        paragraphs: [
          "You can delete your data in an org. Sama deletes your private sessions, your notes, your connections, and your jobs. Shared content stays, without your name.",
          "An org owner can delete the whole org. This cannot be undone.",
        ],
      },
      {
        heading: "Retention",
        paragraphs: [
          "An admin can set a retention: sama then deletes a session after that many days without activity. Memory, skills, and the audit log stay.",
        ],
      },
    ],
    links: [
      { href: "/settings", label: "Your data on the Settings page" },
      { href: "/admin/data", label: "Data controls of the org" },
    ],
  },
  {
    id: "troubleshooting",
    title: "Troubleshooting",
    summary: "What to do when a page shows an error.",
    audience: "everyone",
    sections: [
      {
        heading: "Common messages",
        items: [
          "Your sign-in ended: sign in again. Sama signs you out when the browser tab closes.",
          "You do not have permission to do this: ask an owner or an admin of your org.",
          "Too many requests: wait a minute, then try again. Your plan limits the messages in each minute.",
          "The org used a quota of its plan: an owner or an admin buys a larger plan on the Billing page, or waits for the next month.",
          "The org is in another region: open the link to the deployment of that region.",
          "No key for a provider: an admin sets an org key on the Org keys page, or chooses a model of another provider.",
        ],
      },
      {
        heading: "The agent does not answer",
        steps: [
          "Look at the run in the chat. A failed run shows the reason.",
          "Open Runs in the chat header to see each step of the run.",
          "If the run waits for an approval, approve or deny the card.",
          "Try again in a new session.",
        ],
      },
    ],
    links: [{ href: "/help/getting-started", label: "Getting started" }],
  },
];

export function helpTopic(id: string): HelpTopic | null {
  return HELP_TOPICS.find((t) => t.id === id) ?? null;
}

/** The topics that match the words of a search, in order. An empty search gives all. */
export function searchHelp(text: string): HelpTopic[] {
  const words = text.toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return HELP_TOPICS;
  return HELP_TOPICS.filter((topic) => {
    const all = [
      topic.title,
      topic.summary,
      ...topic.sections.flatMap((s) => [s.heading, ...(s.paragraphs ?? []), ...(s.steps ?? []), ...(s.items ?? [])]),
    ]
      .join(" ")
      .toLowerCase();
    return words.every((w) => all.includes(w));
  });
}
