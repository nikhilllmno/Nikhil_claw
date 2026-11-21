"""Sama as a server: the OpenAI-compatible endpoint and the MCP server for other agents
(docs/architecture.md, Section 10.7, M7.6)."""

from sama.server.core import ServerCore, ServerError, ServerSettings
from sama.server.mcp import SamaMcpServer
from sama.server.openai import OpenAICompat

__all__ = ["OpenAICompat", "SamaMcpServer", "ServerCore", "ServerError", "ServerSettings"]
