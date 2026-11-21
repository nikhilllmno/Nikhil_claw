---
name: chart
description: Make a clear chart image from data
version: 1.0.0
tags: [data, charts, creative]
requires_toolsets: [core]
---

# Chart

Use this skill when the user wants a chart from data: a CSV, a spreadsheet, numbers in the
chat, or the result of an analysis.

## Steps

1. Find the message of the chart in one sentence, for example "sales grew in every region
   except the north".
2. Choose the chart type for the message:
   - change over time: a line chart,
   - compare categories: a bar chart, sorted by value,
   - parts of a whole with few parts: a stacked bar (avoid pie charts with more than four
     parts),
   - the relation of two numbers: a scatter plot,
   - a distribution: a histogram or a box plot.
3. Load and check the data with `pandas`. Check the units, the missing values, and the
   dates.
4. Draw it with `matplotlib`. Install it in the sandbox if it is missing.
5. Make it readable:
   - a title that states the message,
   - axis labels with units,
   - a y-axis that starts at zero for bars,
   - direct labels instead of a legend when there are few series,
   - the source of the data in a note under the chart.
6. Save a PNG at 150 to 200 dpi (and an SVG when the user wants to edit it). Give the path to
   the user.

## Rules

- Do not change the data to make the chart look better.
- Use colors that people with color blindness can tell apart.
- Keep the code in a script in the work folder, so that the user can run it again.
