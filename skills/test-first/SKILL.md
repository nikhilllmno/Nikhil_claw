---
name: test-first
description: Write a failing test first, then the code to pass it
version: 1.0.0
tags: [software, testing]
requires_toolsets: [core]
---

# Test first

Use this skill when you add a feature or fix a bug and the project has tests, or when the
user asks for test-driven development.

## Steps

1. Find how the project runs its tests. Run them once before you change anything, and note
   the tests that already fail.
2. Write one small test for the next behavior. For a bug, the test reproduces the bug.
3. Run it. Check that it fails, and that it fails for the right reason (not an import error
   or a typo).
4. Write the smallest code that makes the test pass.
5. Run the test, then the related tests. All must pass.
6. Improve the code and the test (names, duplicates, structure) with all tests passing. Run
   the tests after each change.
7. Repeat from step 2 for the next behavior.
8. At the end, run the full test suite and the linter.

## Rules

- Test behavior through the public interface, not private details.
- One test checks one behavior. The test name says the behavior.
- Do not change a test to make it pass unless the test is wrong. Say why it was wrong.
- Do not use a mock for code that you own and that is fast. Use mocks at the edges: the
  network, the clock, and paid services.
- When a test is slow or flaky, fix the cause. Do not add a retry.
