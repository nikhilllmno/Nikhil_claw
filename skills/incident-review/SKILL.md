---
name: incident-review
description: Write a blameless review after an outage or incident
version: 1.0.0
tags: [devops, writing, incidents]
---

# Incident review

Use this skill after an outage, a data problem, a security event, or a failed release, when
the user wants a review document (a post-mortem).

## Steps

1. Collect the facts: alerts, logs, chat messages, and the actions that people took, with
   times. Use one time zone for all times and name it.
2. Write the timeline: detection, the first response, each key action, the fix, and the
   recovery.
3. State the impact with numbers: how long, how many users or requests, what data, and what
   cost.
4. Find the causes. Ask "why" until you reach a cause that a change in the system can fix.
   An incident usually has more than one cause.
5. Note what worked well and what made the response harder.
6. Write the actions. Each action has an owner, a due date, and a type: prevent, detect
   sooner, or recover faster.

## Rules

- Be blameless. Describe actions and systems, not the faults of people.
- Keep facts and guesses separate. Mark each guess.
- Do not put secrets, personal data, or customer names in the document unless the user
  asks.
- Prefer a few actions that will happen over a long list.

## Output

Summary, impact, timeline, causes, what went well, what went badly, actions (a table with
owner and date).
