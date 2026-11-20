---
name: api-integration
description: Plan and test a call to an HTTP API
version: 1.0.0
tags: [api, code]
requires_toolsets: [core]
---

# API integration

Use this skill when the user needs code or a script that calls an HTTP API.

## Steps

1. Read the documentation of the API. Use `web_search` and `web_extract` if you do not have it. Find the base URL, the way to authenticate, the rate limits, and the shape of the errors.
2. Write the smallest call that works. Test it with one real request when the user allows it, or with the sample in the documentation.
3. Add the parts that make the code safe:
   - a timeout on every request,
   - a retry with a growing delay for errors that can pass, such as 429 and 5xx,
   - a check of the status code before you read the body,
   - paging when the API returns pages.
4. Test the error paths: a bad key, a missing item, and a timeout.

## Rules

- Never write a secret into the code or into a file. Read it from an environment variable, and tell the user which variable to set.
- Do not print a secret or an authorization header in logs.
- Respect the rate limit of the API. Do not run a loop with no delay.
- Tell the user which calls change data in the other system.
