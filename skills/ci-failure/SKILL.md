---
name: ci-failure
description: Find and fix the cause of a failed CI build
version: 1.0.0
tags: [devops, software, ci]
requires_toolsets: [core]
---

# CI failure

Use this skill when a continuous integration run fails (GitHub Actions, GitLab CI, or
another system) and the user wants to know why or wants a fix.

## Steps

1. Get the log of the failed job. Find the first real error, not the last line. Later errors
   are often results of the first one.
2. Put the failure in one group:
   - the code: a test, the type check, the linter, or the build fails,
   - the environment: a missing tool, a version change of a dependency or a runner image, a
     missing secret or permission,
   - flaky: a timeout, a network error, or a test that depends on time or order.
3. Check whether the same job passes on the base branch. When it also fails there, the cause
   is not this change.
4. Reproduce it in the sandbox with the same command and the same versions as the CI file.
5. Fix the cause. For a dependency change, pin the version or update the code, and say which
   one you chose and why.
6. Run the failing step locally again, then the full test suite.
7. Report: the cause in one sentence, the evidence (a short part of the log), the fix, and
   how you checked it.

## Rules

- Do not skip, disable, or delete a failing test to make the build pass, unless the user
  agrees.
- Do not add a retry to hide a flaky test. Find the cause, or report it as flaky with the
  evidence.
- A change to a CI workflow file can need an approval to push. Tell the user.
- Do not print secrets from the CI environment.
