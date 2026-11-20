---
name: sql-query
description: Write and check SQL queries safely
version: 1.0.0
tags: [data, sql]
---

# SQL query

Use this skill when the user needs a SQL query, or help with one that fails or is slow.

## Steps

1. Find the database type (PostgreSQL, MySQL, SQLite, or other). The syntax is not the same.
2. Get the table and column names. Ask for the schema, or read it from the database. Never invent a column name.
3. Write the query in small parts. Use a common table expression for each step so that you can check each one.
4. Explain what each part does in one line.
5. For a slow query, read the plan (`EXPLAIN` or `EXPLAIN ANALYZE`). Look for full table scans on big tables, missing indexes, and joins that multiply rows.

## Rules

- Start with `SELECT`. Do not run a query that changes data unless the user asked for it. Before an `UPDATE` or a `DELETE`, run the same `WHERE` in a `SELECT` and show the row count.
- Use bound parameters for values in application code. Do not join user input into the text of a query.
- Add a `LIMIT` when you look at a big table.
- Check the result: the row count, null values, and duplicates from joins.
