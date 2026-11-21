---
name: scheduled-job
description: Make a recurring agent job that runs on a schedule
version: 1.0.0
tags: [productivity, schedule, automation]
requires_toolsets: [cron]
---

# Scheduled job

Use this skill when the user wants work to happen again and again with no person present,
for example "check X every hour", "every Monday, make a report of Y", or "on the first of the
month, collect Z".

## Steps

1. Do the task once in this chat, with the user. Make sure that the result is what the user
   wants before you schedule it.
2. Write the job prompt. The job does not see this chat. Put in the prompt:
   - the goal and the exact output format,
   - the sources, the URLs, or the connectors to use,
   - what to answer when there is nothing new (a short line such as "No change"). Each
     successful run sends its answer to the deliver target. A job cannot stay silent, unless
     `deliver` is `none`,
   - what to do on an error.
3. List the tools that the job must use with no approval. Give each tool in
   `preapproved_tools` with limits on its arguments where you can. Use the smallest list.
4. Call `cronjob_manage` with `action: preview` and show the next three fire times.
5. Call `cronjob_manage` with `action: create`. Give `deliver` (`origin` for this chat) and,
   if the user wants, `failure_deliver`. Set `attach_to_session: true` when a run must see
   the results of earlier runs, for example to report only what is new. Give `skills` when
   the job must follow a skill.
6. Tell the user that a job with pre-approved tools, a script, or another deliver target
   waits for the approval of its owner (`/cron approve` or the jobs page).
7. Offer a test with `action: run`.

## Rules

- Prefer a long interval. Ask the user before you make a job that runs more than once an
  hour.
- Do not put a secret in the prompt. Use a connector or a saved login.
- Each run costs money. Say the probable number of runs each month.
- Use the `workflows` toolset instead when the work has fixed steps, approvals, or must not
  repeat an outside write.
