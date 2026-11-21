---
name: security-review
description: Review code or a change for security problems
version: 1.0.0
tags: [software, security, review]
requires_toolsets: [core]
---

# Security review

Use this skill when the user asks for a security review of code, a pull request, or a
configuration, or before a release that touches authentication, payments, or user data.

## Steps

1. Find what the code exposes: the inputs from users and other systems, the outputs, the
   data that it stores, and who can call it.
2. Check the inputs: validation, size limits, and the use of input in SQL, shell commands,
   file paths, HTML, URLs, and templates.
3. Check access: authentication on each route, authorization on each object (can user A read
   the data of user B?), and the default when a check fails.
4. Check secrets: no keys in the code, the history, logs, error messages, or URLs.
5. Check the outbound calls: a URL that a user controls can reach internal addresses.
6. Check the dependencies. Run the audit tool of the ecosystem (for example `pip-audit`,
   `npm audit`, or `govulncheck`) when it is installed.
7. Run a secret scanner (for example `gitleaks`) on the change when it is installed.
8. Write the findings.

## Rules

- Report a finding only with the file, the line, and the way an attacker can use it.
- Give a severity (critical, high, medium, low) with a reason.
- Suggest a fix for each finding. Prefer a fix in the shared code over a fix in one caller.
- Do not test against systems that you do not own or that the user did not name.
- Do not print a secret that you find. Give its location and tell the user to rotate it.
