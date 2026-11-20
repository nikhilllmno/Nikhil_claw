"""Connectors and connections (docs/architecture.md, Section 10.3).

- ``manifest``: the catalog of connector manifests in ``connectors/catalog/``.
- ``composio``: the client of the Composio API (the hosted link flow and tool calls).
- ``http_api``: the driver of a connector that calls one HTTPS API with a form credential.
- ``service``: the ``connections`` rows, their states, and the connector tools of a run.

The package imports no module here: ``service`` uses the tool modules, and the tool server
uses ``errors``.
"""
