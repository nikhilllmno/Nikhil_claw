---
name: memory-review
description: Review, fix, and tidy what the agent remembers
version: 1.0.0
tags: [agents, memory]
requires_toolsets: [memory]
---

# Memory review

Use this skill when the user asks what you remember, wants you to forget something, or says
that a remembered fact is wrong, or when the memory is near its size limit.

## Steps

1. Show the memory of this session from the prompt. Group it by target: `user` (about this
   person, private sessions only), `agent` (about the work and the tools), and `workspace`
   (facts for the whole workspace).
2. Ask the user which entries are wrong, old, or not wanted.
3. Fix each one with the `memory` tool:
   - `patch` with `old_text` (a unique part of the entry) and the new `content`,
   - `remove` with `old_text` for an entry to forget.
   Use `operations` to apply several changes together.
4. Make the memory short: merge entries that say the same thing, and remove task progress,
   temporary facts, and text that came from a web page or a file.
5. Tell the user that the changes apply from the next session.

## Rules

- Never save a secret, a password, or a key in memory.
- Save only stable facts and the way the user likes to work.
- A `workspace` change needs a workspace admin. Tell other users that an admin must make it.
- When the user asks you to forget something, remove it. Do not keep a copy elsewhere.
