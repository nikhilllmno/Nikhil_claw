---
name: spike
description: Run a quick throwaway experiment to test an idea
version: 1.0.0
tags: [software, prototyping]
requires_toolsets: [core]
---

# Spike

Use this skill when there is a question that a small experiment can answer before you build:
"does this library parse our files?", "is this API fast enough?", or "can we do X at all?".

## Steps

1. Write the question as one sentence, and the result that answers it, for example "parse
   the 20 sample PDFs in less than 5 seconds each, with tables intact".
2. Set a time box. Tell the user how long the spike takes.
3. Make a new folder for the spike, outside the main code, for example `spikes/<date>-<name>`.
4. Write the smallest code that answers the question. Skip error handling, structure, and
   tests that do not affect the answer.
5. Run it on real or realistic input. Measure what the question asks for.
6. When there are two or three candidate approaches, try each one with the same input.
7. Write the result in a `README.md` in the spike folder: the question, what you tried, the
   numbers, the answer, and the risks that remain.

## Rules

- Spike code is not production code. Do not merge it as it is.
- Stop at the end of the time box and report what you know.
- A negative answer is a good result. Report it clearly.
