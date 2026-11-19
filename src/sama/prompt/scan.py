"""Checks text for prompt injection before sama puts it in a prompt (Section 6.1).

The checks are simple patterns. They do not find every attack. They stop the common
attacks in context files: instructions that try to replace the system prompt, hidden
text, and commands that send secrets out. Sama does not load a file that has a finding.
"""

from __future__ import annotations

import re
from typing import Final

_FLAGS: Final = re.IGNORECASE | re.DOTALL

PATTERNS: Final[dict[str, re.Pattern[str]]] = {
    "override_instructions": re.compile(
        r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b"
        r"(previous|prior|above|earlier|all|system|your)\b[^.\n]{0,20}\b"
        r"(instructions?|prompts?|rules|guidelines|directions)\b",
        _FLAGS,
    ),
    "new_identity": re.compile(
        r"\byou are now\b|\bnew (system )?instructions\s*:|\bsystem prompt override\b"
        r"|\bact as if you (have|had) no (rules|restrictions)\b",
        _FLAGS,
    ),
    "hide_from_user": re.compile(
        r"\b(do not|don't|never)\s+(tell|inform|reveal to|show|mention to)\s+the\s+user\b",
        _FLAGS,
    ),
    "invisible_text": re.compile(
        "[\u200b-\u200f\u2060-\u2064\ufeff\u202a-\u202e\u2066-\u2069\U000e0000-\U000e007f]"
    ),
    "exfiltration": re.compile(
        r"\b(curl|wget)\b[^\n]{0,200}\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD)\w*"
        r"|\bcat\s+[^\n]{0,40}(\.env\b|~/\.ssh|\.aws/credentials)",
        _FLAGS,
    ),
    "role_marker": re.compile(
        r"<\|im_start\|>\s*system|<\|im_end\|>|</?system>|\[/?SYSTEM\]|<<SYS>>", _FLAGS
    ),
}
"""The finding id and its pattern. Each pattern has bounded repeats, so a search is linear."""

_COMMENT_WORDS: Final = re.compile(
    r"\b(instructions?|ignore|assistant|system prompt|you must|do not tell)\b", re.IGNORECASE
)

HIDDEN_COMMENT: Final = "hidden_comment"
"""The finding id for an HTML comment with instruction words. A comment is hidden when the
file shows as rendered Markdown."""


def scan(text: str) -> tuple[str, ...]:
    """The ids of the findings in the text, in a fixed order."""
    found = [name for name, pattern in PATTERNS.items() if pattern.search(text)]
    if _hidden_comment(text):
        found.append(HIDDEN_COMMENT)
    return tuple(sorted(found))


def _hidden_comment(text: str) -> bool:
    """True if an HTML comment has instruction words. An open comment goes to the end.

    The loop looks at each comment once, so the time is linear in the text length.
    """
    start = text.find("<!--")
    while start != -1:
        end = text.find("-->", start + 4)
        body = text[start + 4 : end if end != -1 else len(text)]
        if _COMMENT_WORDS.search(body):
            return True
        if end == -1:
            return False
        start = text.find("<!--", end + 3)
    return False
