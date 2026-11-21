"""The browser of sama (docs/architecture.md, Section 14.1).

- ``target``: the interface of a browser target (``cloud`` now, ``my_chrome`` in M5.5).
- ``guard``: the outbound URL policy for navigation.
- ``cdp``: a session over CDP with Playwright.
- ``browserbase``: the ``cloud`` target.
- ``contexts``: the persistent context of each user.
- ``service``: the live session of each run and the rendering for the agent.

This module imports nothing, so a process without Playwright can import ``sama.browser.service``.
"""
