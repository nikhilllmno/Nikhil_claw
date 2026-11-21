---
name: release-notes
description: Write release notes or a changelog from git history
version: 1.0.0
tags: [software, writing, git]
requires_toolsets: [core]
---

# Release notes

Use this skill when the user wants release notes, a changelog entry, or a summary of what
changed between two versions.

## Steps

1. Find the range: the last release tag and the new tag or branch. Use `git tag --sort` and
   `git log <from>..<to> --oneline`.
2. Read the commits and the titles of the merged pull requests. Read the diff of a change
   when its title is not clear.
3. Put each change in one group: breaking changes, new features, improvements, fixes,
   security, and internal. Leave the internal changes out of notes for users.
4. Write each item for the reader: what they can do now, or what works better. Do not use
   the internal names of modules.
5. For each breaking change, say what the user must do, with an example.
6. Follow the format of the existing changelog (for example "Keep a Changelog") when there is
   one.

## Rules

- Do not claim a change that is not in the range.
- Credit the authors when the project does that.
- Put breaking changes and security fixes first.
- Keep each item to one or two lines.
