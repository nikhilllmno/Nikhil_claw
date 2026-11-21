"""Whether a person approved the tool call that runs now (Section 16.1).

The tool server sets ``human_approved`` before it runs the body of a tool: it is true when the
gate allowed the call because of a recorded approval of a person (a ticket). The ``my_chrome``
browser target sends it to the extension, so the extension does not ask the user a second time
for a sensitive action that the user approved in the approval card (Section 14.2.2).
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Final

human_approved: Final[ContextVar[bool]] = ContextVar("sama_human_approved", default=False)
