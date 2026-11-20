---
name: summarize
description: Summarize a document, page, or thread at set length
version: 1.0.0
tags: [summary, reading]
requires_toolsets: [web]
---

# Summarize

Use this skill when the user wants a shorter version of a text, a web page, a document, or a conversation.

## Steps

1. Get the full text. For a web page use `web_extract`. For a file read all of it. If it is long, work in parts and then join the part summaries.
2. Find the length that the user wants. If they gave none, use 5 bullet points or one short paragraph.
3. Find the main point, the key facts, and any decision or request.
4. Write the summary. Put the most important point first.
5. Compare the summary with the source. Remove any claim that is not in the source.

## Rules

- Do not add your own opinion. If the user asks for an opinion, put it in a separate section.
- Keep names, numbers, and dates exact.
- Tell the user what you left out when it may matter, for example a long appendix.
- For a thread, name who asked for what and what is still open.
