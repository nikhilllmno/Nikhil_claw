---
name: action-items
description: Find tasks, owners, and deadlines in a document
version: 1.0.0
tags: [productivity, documents, tasks]
---

# Action items

Use this skill when the user gives you a contract, a policy, an email thread, a report, or
notes, and wants to know what someone must do and by when.

## Steps

1. Read the full text once before you extract anything. Find the parties and the dates that
   the text uses as a base, for example the signature date or the start date.
2. Find each obligation. Look for words such as "must", "shall", "will", "is responsible for",
   "no later than", "within", "before", and "each month".
3. For each obligation, record:
   - the action, as one short sentence that starts with a verb,
   - the owner (a person, a team, or a party), or "not stated",
   - the deadline, as a date when the text gives one, or as the rule ("30 days after
     delivery") when it does not,
   - the source: the section number, the page, or a short exact quote.
4. Calculate the dates that depend on a base date. Show the calculation. Say when the base
   date is not in the text.
5. Mark the items that have a condition ("if the customer asks", "on renewal").
6. Sort the list by deadline. Put the items with no deadline last.

## Rules

- Do not add an obligation that the text does not state. Put your own suggestions in a
  separate list.
- Keep each quote exact and short. Do not change the words of a quote.
- When the text is not clear, say what is not clear. Do not guess the owner or the date.
- This is not legal advice. Tell the user to check important items with an expert.

## Output

A table with the columns: action, owner, deadline, condition, source. Then a short list of
open questions.
