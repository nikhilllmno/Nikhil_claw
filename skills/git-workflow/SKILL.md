---
name: git-workflow
description: Branch, commit, and open a pull request on GitHub
version: 1.0.0
tags: [software, git, github]
requires_toolsets: [core, github]
---

# Git workflow

Use this skill when you change code in a GitHub repository and the user wants the change as
a branch or a pull request.

## Steps

1. Clone the repository with `repo_clone`. Read the `README`, the contributing guide, and
   any agent instruction file (for example `AGENTS.md` or `CLAUDE.md`) before you change
   code.
2. Make a branch whose name starts with `sama/`, for example `sama/fix-login-timeout`. Only
   `sama/*` branches can be pushed.
3. Make the change in small steps. Run the tests and the linter of the project after each
   step.
4. Commit with clear messages. Use the style of the project. When the project has no style,
   use Conventional Commits (`fix:`, `feat:`, `docs:`, `chore:`).
5. Before you push, read `git diff` of the full branch once more. Remove debug code, stray
   files, and secrets.
6. Push the branch with `git push`. If the push needs an approval for a protected path, call
   `repo_push_approval` with the values from the error, then push the same commit again.
7. Open the pull request with `pr_create`. Write a description with: what changed, why, how
   you tested it, and the risks.
8. Give the link to the user. A person reviews and merges. You cannot merge.

## Rules

- Do not rewrite the history of a shared branch.
- Do not commit generated files, build output, or `.env` files.
- One pull request does one thing. Split unrelated changes.
- When a rebase has conflicts, resolve each conflict on purpose. Run the tests again after.
