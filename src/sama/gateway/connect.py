"""``/connect <name>`` (Telegram) and ``/sama connect <name>`` (Slack): a link to the
Connections page of the web app (docs/architecture.md, Section 10.3). The chat never takes a
secret."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from sama.gateway.sessions import Target
from sama.tenancy.access import Grants

if TYPE_CHECKING:
    from sama.connectors.service import ConnectionService

NO_CONNECTIONS: Final = "Connections are not set up in this sama."


def connect_answer(
    connections: ConnectionService | None, target: Target, grants: Grants, name: str
) -> str:
    """The chat answer to the command. A shared chat gets the link of a workspace
    connection, and a direct chat the link of a personal connection."""
    if connections is None:
        return NO_CONNECTIONS
    return connections.connect_reply(
        target.workspace_id,
        name,
        shared=target.audience != "private",
        admin=grants.admins_workspace(target.workspace_id),
    )
