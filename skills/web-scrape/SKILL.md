---
name: web-scrape
description: Collect structured data from web pages into a file
version: 1.0.0
tags: [web, data]
requires_toolsets: [core]
---

# Web scrape

Use this skill when the user wants data from one or more web pages in a table: prices,
listings, contacts of businesses, events, or product specs.

## Steps

1. Agree on the fields, the pages, and the number of rows that the user needs.
2. Look for a better source first: an official API, a download (CSV, JSON, or Excel), an RSS
   feed, or a sitemap. Use it when it exists.
3. Read one page and find where the data is: in the HTML, in a JSON block of the page, or in
   a request that the page makes for its data.
4. Write a Python script in the work folder with `requests` and `beautifulsoup4` (or `lxml`).
   Add a `User-Agent` that names the tool, a timeout, and a delay of at least one second
   between requests.
5. Run it on two or three pages. Check the values against the pages. Then run it on all
   pages.
6. Clean the data: trim text, parse numbers and dates, and remove duplicate rows.
7. Write the result to CSV (and Excel if the user wants). Report the row count, the empty
   values, and the pages that failed.

## Rules

- Check `robots.txt` and the terms of the site. Do not scrape a site that forbids it.
- Do not collect personal data that the user has no clear reason to have.
- Do not get past a login, a paywall, or a bot check.
- Keep the script, so that the user can run it again.
