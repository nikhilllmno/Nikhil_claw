---
name: word-document
description: Create and edit Word documents (.docx)
version: 1.0.0
tags: [productivity, documents, office]
requires_toolsets: [core]
---

# Word document

Use this skill when a task has a `.docx` file as input or output: a letter, a report, a
contract draft, a form, or a template.

## Steps

1. For a new document, agree on the structure first: the title, the headings, and the
   sections. Write the text before you format it.
2. Use Python with `python-docx`. Install it in the sandbox if it is missing.
3. Use the built-in styles (`Title`, `Heading 1`, `Heading 2`, `List Bullet`, `Table Grid`)
   so that the document has a navigation pane and a table of contents works.
4. To edit a document, read it first: list the paragraphs with their styles, the tables, and
   the headers and footers. Change the text in the runs and keep the formatting of each run.
5. To fill a template, find the placeholders (for example `{{name}}`). A placeholder can be
   split over several runs. Join the runs of a paragraph before you replace it, and keep the
   style of the first run.
6. Save to a new file name. Open the file again and check the headings and the tables.
7. For a PDF copy, convert with LibreOffice in headless mode
   (`soffice --headless --convert-to pdf`) if it is installed.

## Rules

- Never overwrite the original file.
- Tracked changes and comments in the input are not visible to `python-docx` text reads.
  Tell the user when the file has them.
- Keep the fonts and the page size of a template.
- Put the final file in the work folder and give the user its path.
