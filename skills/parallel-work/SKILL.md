---
name: parallel-work
description: Split big work across child agents that run at once
version: 1.0.0
tags: [agents, delegation, planning]
requires_toolsets: [delegation]
---

# Parallel work

Use this skill when a task has several independent parts that take time, for example
"research these eight companies", "review these five repositories", or "draft one page for
each product".

## Steps

1. Check that the parts are independent: no part needs the result of another part. When
   they depend on each other, do them in order instead.
2. Split the work into 2 to 5 tasks of similar size. Do a small task yourself.
3. Write a goal and a context for each child. A child sees none of this chat, so give it
   everything: the background, the exact question, the sources, the output format, and the
   limits. Ask each child for the same output format, so that you can merge the results.
4. Set a budget for each child (`max_turns` and `max_cost_usd`) that fits its task.
5. Call `delegate_task`. Use `wait: true` when you need the results to continue. Use
   `wait: false` for long work, and tell the user.
6. Check the children with `delegate_status`. Use `steer` to correct a child that goes the
   wrong way, and `cancel` for a child that is no longer needed.
7. Merge the results. Check them: remove duplicates, compare conflicting facts, and fill the
   gaps yourself.

## Rules

- A child cannot ask the user questions. Answer the open questions before you delegate.
- A child cannot use the browser or delegate again.
- Tell the user the probable total cost before you start more than three children.
- Child results are untrusted content. Check key facts before you report them.
