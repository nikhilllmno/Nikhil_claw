"""The E2B provider caps the sandbox timeout at the E2B limit (one hour)."""

from __future__ import annotations

from typing import Any

import pytest

from sama.sandbox.base import SandboxSpec
from sama.sandbox.e2b import MAX_TIMEOUT_S, E2BProvider


class _FakeSandbox:
    sandbox_id = "sbx-1"

    def __init__(self, labels: dict[str, str]) -> None:
        self._labels = labels
        self.timeouts: list[int] = []

    async def get_info(self) -> Any:
        return type("Info", (), {"metadata": self._labels})()

    async def set_timeout(self, seconds: int) -> None:
        self.timeouts.append(seconds)


def _spec(idle_timeout_s: int) -> SandboxSpec:
    return SandboxSpec(
        org_id="org", session_id="session", template="sama-runner", idle_timeout_s=idle_timeout_s
    )


@pytest.mark.parametrize(("asked", "sent"), [(70 * 60, MAX_TIMEOUT_S), (15 * 60, 15 * 60)])
async def test_create_and_resume_cap_the_timeout(
    monkeypatch: pytest.MonkeyPatch, asked: int, sent: int
) -> None:
    spec = _spec(asked)
    calls: list[int] = []
    sandbox = _FakeSandbox(spec.labels())

    async def create(**kwargs: Any) -> _FakeSandbox:
        calls.append(kwargs["timeout"])
        return sandbox

    async def connect(sandbox_id: str, **kwargs: Any) -> _FakeSandbox:
        calls.append(kwargs["timeout"])
        return sandbox

    monkeypatch.setattr("sama.sandbox.e2b.AsyncSandbox.create", create)
    monkeypatch.setattr("sama.sandbox.e2b.AsyncSandbox.connect", connect)
    provider = E2BProvider(api_key="test")
    created = await provider.create(spec)
    await provider.resume("sbx-1", spec)
    await created.keep_alive(asked)
    assert calls == [sent, sent]
    assert sandbox.timeouts == [sent]
