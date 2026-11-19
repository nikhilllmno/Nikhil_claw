"""The uvicorn server of the control plane (docs/architecture.md, Section 19.1).

uvicorn closes its listener before it sends the lifespan stop. The runs in the sandboxes still
need the model proxy and the tool server of this process, so a stop drains the worker first:

1. The first SIGTERM or SIGINT sets ``ControlPlane.stop``. The worker claims no more runs.
2. HTTP still serves while the current runs end, for at most ``DRAIN_S``.
3. Then uvicorn stops as usual: it closes the listener, gives open requests
   ``timeout_graceful_shutdown``, and sends the lifespan stop.

A second signal during the drain stops uvicorn at once.
"""

from __future__ import annotations

import asyncio
import logging
from types import FrameType

import uvicorn

from sama.controlplane.app import ControlPlane

log = logging.getLogger("sama.controlplane")


class DrainingServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, plane: ControlPlane) -> None:
        super().__init__(config)
        self._plane = plane
        self._stop_requested = False
        self._signal: int | None = None
        self._drain: asyncio.Task[None] | None = None

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        # A signal handler: it only sets flags. ``on_tick`` starts the drain in the loop.
        if self._stop_requested:
            super().handle_exit(sig, frame)
            return
        self._signal = sig
        self._stop_requested = True

    def request_stop(self) -> None:
        """Starts a drain without a signal, for tests."""
        self._stop_requested = True

    async def on_tick(self, counter: int) -> bool:
        if self._stop_requested and self._drain is None:
            log.info("the control plane drains its runs before it stops")
            self._drain = asyncio.create_task(self._plane.drain())
            self._drain.add_done_callback(self._drained)
        return await super().on_tick(counter)

    def _drained(self, _: asyncio.Task[None]) -> None:
        if self._signal is None:
            self.should_exit = True
        else:
            # uvicorn raises the signal again after its stop, as it does without a drain.
            super().handle_exit(self._signal, None)
