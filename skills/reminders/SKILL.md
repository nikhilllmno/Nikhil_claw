---
name: reminders
description: Set, list, and cancel reminders for the user
version: 1.0.0
tags: [productivity, schedule]
requires_toolsets: [cron]
---

# Reminders

Use this skill when the user says "remind me", "tell me at", "every Friday remind me", or
asks which reminders they have.

## Steps

1. Find the text of the reminder, the time, and the repeat rule. If the time zone is not
   known, ask the user once. Save the time zone with `memory` only if the user agrees and you
   have that tool.
2. Call `cronjob_manage` with `action: preview`, the schedule in plain English (for example
   `tomorrow at 7pm` or `every friday at 16:00`), and the time zone. Check that the first fire
   time is what the user wants.
3. Call `cronjob_manage` with `action: create`. Use a short name that starts with
   `reminder:`. Write a prompt that tells the job to send only the reminder text, with no
   other work, for example: "Send this reminder to the user, with no other text: Call the
   bank about the loan."
4. Confirm to the user: the text, the next time, and the repeat rule.
5. To list reminders, call `cronjob_manage` with `action: list` and show only the jobs whose
   name starts with `reminder:`. To cancel, call `action: remove` with the `job_id`.

## Rules

- A one-time reminder uses a schedule with one date, for example `tomorrow at 9am`. Do not
  make a repeating job for a one-time reminder.
- Do not give the reminder job tools. It only sends text.
- When a time is in the past, tell the user and ask for a new time.
