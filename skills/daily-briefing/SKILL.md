---
name: daily-briefing
description: Set up a scheduled daily briefing on chosen topics
version: 1.0.0
tags: [productivity, news, schedule]
requires_toolsets: [cron, web]
---

# Daily briefing

Use this skill when the user wants a regular briefing, for example "every morning, tell me
the news about X", "a daily summary of Y", or "a Monday digest".

## Steps

1. Agree on the content with the user:
   - the topics, the companies, or the sources,
   - the time and the time zone (an IANA name such as `Europe/Berlin`),
   - the length, for example five items of two lines each,
   - where the briefing goes: this chat, or another channel.
2. Write the job prompt. The job runs with no person and does not see this chat, so the
   prompt must stand alone. Put in it the topics, the length, the format, and these rules:
   use `web_search` and `web_extract`, give a link for each item, skip items older than the
   last briefing, and say "nothing new" when there is nothing new.
3. Call `cronjob_manage` with `action: preview`, the schedule, and the time zone. Show the
   next fire times to the user.
4. When the user agrees, call `cronjob_manage` with `action: create`, a short name, the
   schedule, the time zone, the prompt, and the deliver target.
5. Offer a test: `cronjob_manage` with `action: run` fires the job once now.

## Rules

- A job that sends to another place than this chat waits for the approval of its owner. Tell
  the user that.
- Do not put a secret or a password in the prompt.
- Keep the job prompt below 2,000 characters. A long prompt costs money on every run.
- Tell the user how to pause or remove the job.
