---
name: simplify-code
description: Make recent code simpler with no change in behavior
version: 1.0.0
tags: [software, refactoring]
requires_toolsets: [core]
---

# Simplify code

Use this skill when the user asks you to clean up, simplify, or refactor code, or after a
large change, before a review.

## Steps

1. Define the scope: the files or the diff to simplify. Use `git diff` against the base
   branch for a recent change.
2. Run the tests first. When there are no tests for the code, write a few tests that pin the
   current behavior.
3. Look for these problems, in order of value:
   - duplicate code that can be one function,
   - dead code: unused functions, variables, imports, and flags,
   - deep nesting that an early return can remove,
   - functions that do more than one thing,
   - names that do not say what a thing is or does,
   - comments that repeat the code, or that are now wrong,
   - complex code for a case that cannot happen.
4. Make one kind of change at a time. Run the tests after each change.
5. Report each change and why it is simpler.

## Rules

- Do not change behavior. That includes error messages, log text, and public interfaces,
  unless the user agrees.
- Follow the style of the project, not your own style.
- Do not add a new abstraction for a single use.
- When a change is a matter of taste, leave the code as it is.
