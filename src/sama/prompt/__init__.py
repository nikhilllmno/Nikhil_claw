"""Prompt assembly: the frozen ``RunConfig`` of a session (docs/architecture.md, Section 6.1)."""

from sama.instructions import DEFAULT_SOUL, SYSTEM_PROMPT
from sama.prompt.assemble import (
    MAX_CONTEXT_FILES,
    PromptAssembler,
    environment,
    prepare,
    truncate,
    unique_path,
)
from sama.prompt.scan import HIDDEN_COMMENT, PATTERNS, scan

__all__ = [
    "DEFAULT_SOUL",
    "HIDDEN_COMMENT",
    "MAX_CONTEXT_FILES",
    "PATTERNS",
    "SYSTEM_PROMPT",
    "PromptAssembler",
    "environment",
    "prepare",
    "scan",
    "truncate",
    "unique_path",
]
