---
name: session-to-workflow
description: Turn the work of this session into a saved workflow
version: 1.0.1
tags: [agents, workflows, automation]
requires_toolsets: [workflows]
---

# Turn this session into a workflow

Use this skill when the user says "turn this session into a workflow", "make this repeatable",
or "do this every week". A workflow is a saved YAML definition of steps. It runs again with no
person in the run.

## Steps

1. Read the session again. Find the steps that worked. Ignore the attempts that failed, the
   questions, and the corrections.
2. Write each step as one workflow step:
   - One connector tool call with fixed arguments is a `tool` step (no LLM).
   - Work that needs judgment, such as a summary or a draft, is an `agent` step. Write a
     prompt that stands alone: the run has no person and does not see this session.
   - A point where the user checked the result before an outside write is an `approval` step.
   - A choice that depends on a value is a `branch` step with a fixed comparison.
   - Work on independent items at the same time is a `parallel` step.
3. Find the values that change between runs, such as a repository, a channel, or a date.
   Make each value an input with a default. Use `{{inputs.<name>}}` in the steps.
4. Use `{{steps.<id>.output}}` to pass the output of one step to the next step.
5. Ask the user when the workflow runs: by hand only, or on a schedule (`triggers`, with a
   time zone).
6. Call `workflow_manage` with `action: validate` and the YAML. Fix each error.
7. Show the user the steps, the inputs, the schedule, and the outside writes. Ask for a yes.
8. Call `workflow_manage` with `action: create`. Then tell the user the status. A workflow
   that writes to an outside system is `pending_approval`. The owner must approve that
   version in the web app or the API before its first run.
9. Offer a first run with `workflow_run` and `action: start`. Show the result with
   `action: status`.

## Example

```yaml
name: weekly-issue-digest
description: Summarise the open issues and post the digest
inputs:
  repo: { default: Nikhil_claw }
triggers:
  - schedule: every monday at 9am
    tz: Asia/Kolkata
steps:
  - id: fetch
    tool: github_list_repository_issues
    args: { owner: nikhilllmno, repo: "{{inputs.repo}}", state: open }
  - id: summarise
    agent:
      prompt: "Summarise these issues by priority, in 10 lines or fewer: {{steps.fetch.output}}"
  - id: check
    approval: { prompt: "Post this digest to #eng?" }
  - id: post
    tool: slack_send_message
    args: { channel: "#eng", text: "{{steps.summarise.output}}" }
```

## Rules

- Do not put a secret, a token, or a password in the YAML. A tool step uses the connections of
  the owner.
- Use only tools that exist. A tool step runs a connector tool. Use an `agent` step for other
  tools.
- Put an `approval` step before an outside write that a person checked in the session.
- Keep the workflow small. Split a long workflow into sub-workflows with `workflow` steps.
- You cannot approve a workflow or an approval step. Only the owner can.
