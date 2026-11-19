"""The sama vault and redaction (docs/architecture.md, Section 16.3).

- ``sama.vault.redact``: the redactor. It has no dependencies, so every process can use it.
- ``sama.vault.crypto``: envelope encryption.
- ``sama.vault.store``: the vault in Postgres and the org keys.
"""
