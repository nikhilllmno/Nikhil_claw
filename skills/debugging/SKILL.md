---
name: debugging
description: Find the cause of a bug before you change code
version: 1.0.0
tags: [code, debugging]
requires_toolsets: [core]
---

# Debugging

Use this skill when something fails, gives a wrong result, or is slow.

## Steps

1. Reproduce the problem. Write the exact command or input and the exact error. If you cannot reproduce it, say so and collect more facts. Do not guess.
2. Make the failure small. Remove steps and input until the smallest case still fails. A failing test is the best result.
3. Make a list of possible causes. Rank them by how likely they are and how easy they are to check.
4. Check one cause at a time. Use logs, prints, a debugger, or `git bisect` to find the change that broke it.
5. Find the root cause, not only the place where the error shows.
6. Fix the cause with the smallest change. Add a test that fails before the fix and passes after it.
7. Run the full tests. Check that no other behavior changed.

## Rules

- Change one thing at a time.
- Do not hide an error with a broad catch or a retry.
- Tell the user the cause in plain words, and what you changed.
- If you tried 3 ideas without progress, stop. Summarize the facts and ask the user.
