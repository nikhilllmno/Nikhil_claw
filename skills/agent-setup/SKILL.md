---
name: agent-setup
description: Propose changes to the persona, tools, or policy
version: 1.0.0
tags: [agents, settings]
requires_toolsets: [self]
---

# Agent setup

Use this skill when the user wants to change how this agent works for everyone who uses it:
its persona and standing instructions (the soul), its model or runtime, its toolsets, or its
tool policy.

## Steps

1. Find out what the user wants to change and why. Make sure that it is a change for all
   users of this agent profile, not a preference of one person. For a personal preference,
   offer a memory note instead, if you have the `memory` toolset.
2. Choose the target of `change_propose`:
   - `soul`: the persona and the standing instructions. Give the full new text, not a diff.
     Keep what is still correct, and change only what the user asked.
   - `profile`: the name, the runtime, the model, or the sub-agents.
   - `toolsets`: the full new list of toolsets, or of group toolsets for shared sessions.
   - `policy`: rules of the form tool and action (`allow`, `ask`, `deny`, or `none` to remove
     a rule).
3. Show the user the change before you propose it. Explain the effect, for example "the
   agent will not be able to run shell commands".
4. Call `change_propose` with the target, the fields, and a short summary.
5. Tell the user that a workspace admin or the org owner must approve it on the approval
   card, and that it applies from the next session.

## Rules

- Propose a change only when the user asks for it.
- Do not remove a safety rule, an `ask` policy, or an approval step to make your own work
  easier.
- Prefer `ask` over `allow` for tools that write outside the sandbox.
- One proposal changes one thing.
