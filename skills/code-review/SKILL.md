---
name: code-review
description: Review a code change for bugs, risk, and clarity
version: 1.0.0
tags: [code, review]
---

# Code review

Use this skill when the user asks you to review a diff, a pull request, a file, or a branch.

## Steps

1. Get the change. Use `git diff` against the base branch, or read the files that the user names. Read the tests that belong to the change.
2. State in one sentence what the change does. If you cannot, ask the author.
3. Read the whole change once for the shape. Then read it again line by line.
4. Run the tests and the linter if the repository has them. Report the exact result.

## What to look for

Order the findings by risk.

1. Correctness: wrong logic, off-by-one errors, unhandled empty or null input, race conditions, and wrong error handling.
2. Security: input that reaches a shell, a query, or a path without checks; secrets in code or logs; missing access checks.
3. Data: migrations that lock tables or lose data, and changes that break old data.
4. Tests: new behavior without a test, and tests that cannot fail.
5. Clarity: unclear names, dead code, and a function that does too much.

## Report format

- A short summary and a verdict: approve, approve with comments, or request changes.
- Findings as a list. Give each one the file and line, the problem, and a suggested fix.
- Mark each finding as blocking or optional.
- Say what you did not check.

Be specific and kind. Do not report style points that a formatter can fix.
