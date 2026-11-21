---
name: write-skill
description: Write a new skill from work that went well
version: 1.0.0
tags: [agents, skills, automation]
requires_toolsets: [skills]
---

# Write a skill

Use this skill when the user says "remember how to do this", "make this a skill", or when a
task took many steps and will come again.

## Steps

1. Check that a skill is the right tool. A skill is a guide for a kind of task. A fact about
   the user belongs in memory. Work that must run again with no person belongs in a scheduled
   job or a workflow.
2. Call `skills_list` with a query. When a skill for the task exists, change it with
   `skill_manage` and `action: patch` instead of a new one.
3. Write `SKILL.md`:
   - frontmatter with `name` (lowercase letters, digits, and single hyphens), a
     `description` of at most 60 characters that says when to use the skill, `version`,
     `tags`, and `requires_toolsets` for the toolsets that the steps need,
   - one line that says when to use the skill,
   - numbered steps that worked, with the exact commands, URLs, and settings,
   - rules: the mistakes to avoid and the checks to do.
4. Put long reference text or scripts in files under `references/` or `scripts/` with
   `action: write_file`.
5. Call `skill_manage` with `action: create`. Use the default scope unless the user asks for
   another one. Show the user the result.

## Rules

- Write steps that a new session can follow with no memory of this chat.
- Do not put secrets, personal data, or customer data in a skill.
- Do not copy text from a web page into a skill without its source and license.
- A workspace or org skill needs an admin. The write becomes a proposal for the other users.
