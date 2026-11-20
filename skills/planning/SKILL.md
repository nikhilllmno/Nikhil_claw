---
name: planning
description: Break a goal into a plan with steps and risks
version: 1.0.0
tags: [planning, projects]
requires_toolsets: [todo]
---

# Planning

Use this skill when the user has a goal and needs a plan, or when a task is too big to start at once.

## Steps

1. Write the goal and the finish line: how will you know it is done?
2. Write the limits: time, budget, people, and tools. Ask the user for the ones that you do not know. Use `clarify` and ask once.
3. Split the goal into steps of one day or less. Each step must produce something that you can check.
4. Order the steps by dependency. Mark the steps that can run in parallel.
5. Find the risks. For each risk, write how likely it is, how bad it is, and what you do about it.
6. Put the first steps in `todo`.

## Format

- Goal and finish line.
- Steps as a numbered list with an owner and an estimate.
- Risks as a short table.
- The first action to take now.

## Rules

- Do not plan more detail than the next two weeks need.
- Mark each estimate as known, or as a guess.
- Ask the user to confirm the plan before long or costly work.
