"""Sandbox providers (docs/architecture.md, Section 16.2; decision D5).

``base`` has the interface. ``e2b``, ``docker``, and ``local`` are the providers. Import a
provider module directly: ``sama.sandbox.e2b`` needs the ``sandbox-e2b`` dependency group.
"""

from sama.sandbox.base import (
    WORKDIR,
    ExecResult,
    Sandbox,
    SandboxError,
    SandboxNotFoundError,
    SandboxOutdatedError,
    SandboxProcess,
    SandboxProvider,
    SandboxSpec,
)

__all__ = [
    "WORKDIR",
    "ExecResult",
    "Sandbox",
    "SandboxError",
    "SandboxNotFoundError",
    "SandboxOutdatedError",
    "SandboxProcess",
    "SandboxProvider",
    "SandboxSpec",
]
