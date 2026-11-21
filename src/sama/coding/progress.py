"""Progress of a coding agent tool call to the channel (docs/architecture.md, Section 15.4).

``coding_agent_run`` runs on the tool server for minutes. The channel of the run shows only
the run events, and the worker of the run writes them. ``Worker.emit`` adds an event to the
stream of a run that streams in this process (the same seam as ``browser_task``, M5.6). The
control plane runs the tool server and the worker in one process (Section 19.1). When the run
is not in this process, the progress is dropped and only the result of the tool comes back.
"""

from __future__ import annotations

from typing import Protocol

from sama.runtime.contract import RunEvent


class ProgressSink(Protocol):
    """Normally ``sama.orchestrator.worker.Worker``."""

    async def emit(self, org_id: str, run_id: str, event: RunEvent) -> bool: ...
