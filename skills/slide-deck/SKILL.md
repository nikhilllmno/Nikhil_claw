---
name: slide-deck
description: Build a PowerPoint deck (.pptx) from an outline
version: 1.0.0
tags: [productivity, presentation, office]
requires_toolsets: [core]
---

# Slide deck

Use this skill when the user wants a `.pptx` file, or wants to change one. For the story and
the text of a talk, load the skill `presentation-outline` first if you have it.

## Steps

1. Get or write the outline: one line for the message of each slide, then at most five short
   bullets or one chart or one image for each slide.
2. Use Python with `python-pptx`. Install it in the sandbox if it is missing.
3. When the user gives a template, open it and use its slide layouts. Do not set fonts and
   colors by hand. When there is no template, use the default layouts.
4. Use the layout `Title Slide` for the first slide and `Title and Content` for most slides.
   Put the message of the slide in the title, as a full sentence.
5. Make charts with the native chart objects of `python-pptx`, so that the user can edit
   the data. Use an image only for a chart type that it does not have.
6. Put speaker notes on each slide when the user will present.
7. Save to a new file. Open it again and check the slide count, the titles, and that no text
   is outside its box (count the characters: more than about 90 words on one slide is too
   much).
8. If LibreOffice is installed, export a PDF and look at one or two pages as images to check
   the layout.

## Rules

- One message per slide.
- Never overwrite the original file.
- Give the source of each number on the slide or in the notes.
