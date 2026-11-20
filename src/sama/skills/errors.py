"""The error of the skill tools."""

from __future__ import annotations


class SkillError(Exception):
    """A skill request that cannot work. The text is for the model: it says what is wrong and
    what to do. The tool server gives it back as a tool error."""
