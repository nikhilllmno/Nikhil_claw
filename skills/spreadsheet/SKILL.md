---
name: spreadsheet
description: Read, clean, and analyze CSV and Excel data
version: 1.0.0
tags: [data, spreadsheet]
requires_toolsets: [core]
---

# Spreadsheet

Use this skill for work on CSV, TSV, and Excel files.

## Steps

1. Look at the file before you change it. Print the sheet names, the column names, the types, the row count, and 5 sample rows.
2. Use Python with `pandas` for tables. For `.xlsx` files use `openpyxl`. If a package is missing, install it in the sandbox.
3. Check the data: empty cells, duplicate rows, mixed types in one column, dates in different formats, and numbers stored as text.
4. Do the work in a script, not by hand. Keep the script in the work folder so that the user can run it again.
5. Write the result to a new file. Never overwrite the original file.
6. Open the result file once more and check the row count and a few values.

## Rules

- Keep formulas as formulas in an `.xlsx` file when the user needs a live sheet. Write values when they need a fixed result.
- Say how many rows you dropped, changed, or could not parse.
- Do not guess a date format. Check for values with a day above 12 to find the order.
- Put a short summary of the method in the answer.
