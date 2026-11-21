"""Redaction of secrets (docs/architecture.md, Section 16.3).

A ``Redactor`` replaces two kinds of secrets with ``[REDACTED]``:

- **Known values**: the secrets of the process (the settings) and the values in the vault.
  A known value is replaced wherever it occurs. Values shorter than ``MIN_KNOWN_CHARS`` are
  ignored, so that a short value does not hide normal text. A known value stays for the life
  of the process, also after the vault changes, because a cache or a run can still use it.
- **Patterns**: common key and token forms, for example ``sk-...``, ``ghp_...``, ``AKIA...``,
  JSON web tokens, private key blocks, ``Bearer`` tokens, and passwords in database URLs.

The process has one default redactor (``default_redactor``). The logs, the run queue (the
transcript), the tool gate (stored arguments), the tool server (tool output), and the trace
export use it.

Each pattern runs in linear time, also on hostile text: a scan from one start position stops
at a delimiter that the next start position also needs. The tests check this.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Iterable, Mapping
from typing import Any, Final, TypeVar
from urllib.parse import urlsplit

REDACTED: Final = "[REDACTED]"
MIN_KNOWN_CHARS: Final = 8

_T = TypeVar("_T")

# A group named ``s`` marks the secret part. Without it, the whole match is the secret.
PATTERNS: Final = tuple(
    re.compile(pattern)
    for pattern in (
        # The body stops at the next "-----", so a BEGIN with no END scans only to the next
        # BEGIN.
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[^-]*(?:-(?!----)[^-]*)*"
        r"-----END [A-Z ]*PRIVATE KEY-----",
        r"\bsk-[A-Za-z0-9_-]{20,}",
        r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}",
        r"\bgithub_pat_[A-Za-z0-9_]{22,}",
        r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
        r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
        r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+",
        r"\b\d{8,10}:AA[A-Za-z0-9_-]{33}\b",
        r"\bAIza[0-9A-Za-z_-]{35}\b",
        r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}",
        r"\be2b_[0-9a-f]{40}\b",
        r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
        r"(?i)\bbearer\s+(?P<s>[A-Za-z0-9._~+/-]{20,}=*)",
        r"(?i)\b(?:postgres(?:ql)?|mysql|redis|mongodb(?:\+srv)?)://[^:@/\s\"']+:(?P<s>[^@/\s\"']+)@",
        # Capability URLs of the WhatsApp and email channels (Section 11.9): the webhook token,
        # the approval link token, and the media link signature. Access logs show paths.
        r"/whatsapp/[0-9a-fA-F-]{36}/webhook/(?P<s>[A-Za-z0-9_-]+)",
        r"/email/[0-9a-fA-F-]{36}/approvals/(?P<s>[A-Za-z0-9_.=-]+)",
        r"/whatsapp/[0-9a-fA-F-]{36}/media/[0-9a-fA-F-]{36}\?[^\s\"']*?\bs=(?P<s>[A-Za-z0-9_-]+)",
        r"hub\.verify_token=(?P<s>[^&\s\"']+)",
    )
)


def _replace(match: re.Match[str]) -> str:
    if "s" not in match.re.groupindex:
        return REDACTED
    whole, start = match.group(0), match.start()
    return whole[: match.start("s") - start] + REDACTED + whole[match.end("s") - start :]


class Redactor:
    """Replaces secrets in text and in JSON values. It is safe to use from threads."""

    def __init__(self, values: Iterable[str | None] = ()) -> None:
        self._lock = threading.Lock()
        self._values: frozenset[str] = frozenset()
        self._known: re.Pattern[str] | None = None
        self.add(*values)

    def add(self, *values: str | None) -> None:
        """Adds known values. They stay for the life of the process."""
        usable = _usable(values)
        if usable <= self._values:
            return
        with self._lock:
            self._values = self._values | usable
            self._build()

    def forget(self, *values: str) -> None:
        """Removes known values. For tests."""
        with self._lock:
            self._values = self._values - set(values)
            self._build()

    def _build(self) -> None:
        values = sorted(self._values, key=len, reverse=True)
        # One assignment, so a reader sees the old pattern or the new one.
        self._known = re.compile("|".join(map(re.escape, values))) if values else None

    def text(self, value: str) -> str:
        known = self._known
        if known is not None:
            value = known.sub(REDACTED, value)
        for pattern in PATTERNS:
            value = pattern.sub(_replace, value)
        return value

    def json(self, value: _T) -> _T:
        """Redacts each string in a JSON value (dicts, lists, and strings), keys included."""
        return _walk(self, value)  # type: ignore[no-any-return]


def _walk(redactor: Redactor, value: Any) -> Any:
    if isinstance(value, str):
        return redactor.text(value)
    if isinstance(value, dict):
        return {_walk(redactor, k): _walk(redactor, v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_walk(redactor, item) for item in value]
    return value


def _usable(values: Iterable[str | None]) -> set[str]:
    return {value for value in values if value and len(value) >= MIN_KNOWN_CHARS}


_DEFAULT: Final = Redactor()


def default_redactor() -> Redactor:
    """The redactor of the process."""
    return _DEFAULT


def redact(value: str) -> str:
    """``default_redactor().text(value)``."""
    return _DEFAULT.text(value)


def env_secrets(*names: str, env: Mapping[str, str] | None = None) -> list[str]:
    """The values of the environment variables ``names`` that are set. For a database URL,
    the password too. A standalone service gives these to the redactor at start."""
    source = os.environ if env is None else env
    values = []
    for name in names:
        value = source.get(name, "").strip()
        if not value:
            continue
        values.append(value)
        if "://" in value:
            password = urlsplit(value).password
            if password:
                values.append(password)
    return values
