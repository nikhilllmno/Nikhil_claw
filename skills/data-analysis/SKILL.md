---
name: data-analysis
description: Analyze a dataset and report findings with evidence
version: 1.0.0
tags: [data, analysis]
requires_toolsets: [core]
---

# Data analysis

Use this skill when the user asks a question that data can answer.

## Steps

1. Write the question in one sentence, and the measure that answers it.
2. Load the data and inspect it: shape, types, missing values, ranges, and duplicates. Print them.
3. Clean only what you must. Write down each change.
4. Explore. Use group-by tables, simple charts, and summary numbers before any model.
5. Test the finding. Check it on a subset, with another measure, or with a simple test. Look for a reason that the result could be wrong: a small sample, an outlier, or a change in how the data was collected.
6. Report.

## Tools

Use Python with `pandas` and `matplotlib` in the sandbox. Keep the code in a script in the work folder. Save charts as PNG files.

## Report format

- The answer in one or two sentences.
- The evidence: key numbers and one or two charts.
- The method and the data used, with row counts.
- The limits: what the data cannot show.

Do not claim a cause when the data shows only a link.
