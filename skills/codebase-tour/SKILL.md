---
name: codebase-tour
description: Map an unknown codebase and explain how it works
version: 1.0.0
tags: [software, onboarding]
requires_toolsets: [core, github]
---

# Codebase tour

Use this skill when the user wants to understand a repository: "how is this project built",
"where is X done", or "help me start on this codebase".

## Steps

1. Clone the repository with `repo_clone` when it is not in the work folder.
2. Read the top files: `README`, the build file (for example `package.json`,
   `pyproject.toml`, `go.mod`, `Cargo.toml`), the CI configuration, and the agent instruction
   files.
3. Measure the size: the languages and the lines of code for each language. Use `cloc` or
   `tokei` if they are installed, else count with `git ls-files` and `wc -l`.
4. Find the entry points: the main program, the web server routes, the command-line
   commands, and the jobs.
5. Find the main modules and how they depend on each other. Follow one real request or one
   command from the entry point to the data store.
6. Find how to build, test, and run the project locally.
7. Write the tour.

## Rules

- Base each statement on a file that you read. Give the path.
- Say when a part of the code is not clear or looks unused. Do not guess its purpose.
- Do not change code during a tour.

## Output

1. One paragraph: what the project does and its main technologies.
2. A short map: folder, purpose, key files.
3. A diagram (Mermaid) of the main components when it helps.
4. The path of one request through the code.
5. How to build, test, and run.
6. Where to start for the task of the user.
