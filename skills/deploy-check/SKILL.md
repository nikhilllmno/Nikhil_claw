---
name: deploy-check
description: Check a service after a deploy and report its health
version: 1.0.0
tags: [devops, operations]
requires_toolsets: [core]
---

# Deploy check

Use this skill after a release or a deploy, or when the user asks "is the service up?", "did
the deploy work?", or "why is the site slow?".

## Steps

1. Get the facts: the service URL, its health endpoint, the version that should be live, and
   where the logs and metrics are.
2. Check the health endpoint with a short request and a timeout. Record the status code and
   the response time. Repeat three times.
3. Check the version: an endpoint, a header, or a page footer that shows the build. Compare it
   with the expected version.
4. Check the main user paths with simple requests: the home page, the login page, and one
   API call that reads data.
5. Read the logs of the last minutes when you have access. Look for new errors, restarts, and
   slow requests. Compare them with the time before the deploy.
6. Report a verdict: healthy, degraded, or down, with the evidence.

## Rules

- Use only read requests. Do not change data to test the service.
- Send few requests. A check is not a load test.
- When the service is down, give the facts first, then the probable causes, then the options
  (for example a rollback). Do not roll back without the agreement of the user.
- Do not print secrets from logs or configuration.
