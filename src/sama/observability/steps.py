"""Step times of one run for the log (docs/architecture.md, Section 18.1)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Steps:
    """The end time of each step, in seconds from the start. ``str`` gives ``name_s=1.234``
    pairs in the order of the marks, for one log line per run."""

    started: float = field(default_factory=time.monotonic)
    marks: dict[str, float] = field(default_factory=dict)

    def mark(self, name: str, *, once: bool = False) -> None:
        """Records the time of ``name`` now. With ``once``, a second mark is ignored."""
        if not (once and name in self.marks):
            self.marks[name] = time.monotonic() - self.started

    def __str__(self) -> str:
        return " ".join(f"{name}_s={value:.3f}" for name, value in self.marks.items())
