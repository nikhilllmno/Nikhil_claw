"""The delivery of job results, with receipts (docs/architecture.md, Section 12.1).

Each row of ``deliveries`` is one message. The sender works in three steps:

1. It claims a ``pending`` row that is due: the row becomes ``sending``, ``attempts`` goes up by
   one, and the lease runs for ``LEASE_S``. This commits before the provider call.
2. It calls the provider. The answer decides the result:
   - ``sent``: the provider took the message. ``provider_message_id`` is the receipt.
   - a definite failure (no connection, a 4xx answer, ``ok: false``): the row goes back to
     ``pending`` with a backoff, or becomes ``failed`` after ``MAX_ATTEMPTS``.
   - an unknown result (a timeout after the request went out, a 5xx answer): the row becomes
     ``unknown``. sama never sends it again, because the message can be there already.
3. A ``sending`` row whose lease expired (the process stopped during the call) becomes
   ``unknown`` for the same reason.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final, Literal

import httpx
from pydantic import SecretStr
from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Delivery, Job, JobFire, SessionBinding
from sama.vault.store import SecretName

log = logging.getLogger("sama.scheduler.delivery")

LEASE_S: Final = 120
MAX_ATTEMPTS: Final = 3
BACKOFF_S: Final = 30
BATCH: Final = 20
TELEGRAM_URL: Final = "https://api.telegram.org"
SLACK_URL: Final = "https://slack.com/api"
SENDGRID_URL: Final = "https://api.sendgrid.com/v3/mail/send"

Outcome = Literal["sent", "failed", "unknown"]


@dataclass(frozen=True, slots=True)
class SendResult:
    outcome: Outcome
    receipt: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class EmailConfig:
    api_key: SecretStr
    sender: str


SecretGetter = Callable[[uuid.UUID, SecretName], Awaitable[SecretStr | None]]


def _failed(error: str) -> SendResult:
    return SendResult("failed", error=error)


def _unknown(error: str) -> SendResult:
    return SendResult("unknown", error=error)


class Sender:
    """Sends one message to one provider. It never raises: each error becomes a result."""

    def __init__(
        self,
        secret: SecretGetter,
        *,
        email: EmailConfig | None = None,
        http: httpx.AsyncClient | None = None,
        telegram_url: str = TELEGRAM_URL,
        slack_url: str = SLACK_URL,
        sendgrid_url: str = SENDGRID_URL,
    ) -> None:
        self._secret = secret
        self._email = email
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))
        self._telegram_url = telegram_url.rstrip("/")
        self._slack_url = slack_url.rstrip("/")
        self._sendgrid_url = sendgrid_url

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _token(self, org_id: uuid.UUID, name: SecretName) -> SecretStr | None:
        """The token, or None when the vault cannot give it. Nothing went out yet, so the
        delivery fails and can try again."""
        try:
            return await self._secret(org_id, name)
        except Exception:
            log.exception("the vault cannot give a delivery token org=%s", org_id)
            return None

    async def _post(
        self, url: str, *, json: dict[str, Any], headers: dict[str, str] | None = None
    ) -> httpx.Response | SendResult:
        """The answer, or the result of a transport error. An error before the request went
        out is a definite failure. An error after it went out is unknown."""
        try:
            return await self._http.post(url, json=json, headers=headers)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as error:
            return _failed(f"no connection: {type(error).__name__}")
        except httpx.HTTPError as error:
            return _unknown(f"no answer: {type(error).__name__}")

    async def telegram(
        self, org_id: uuid.UUID, chat_id: str, text: str, thread_id: int | None = None
    ) -> SendResult:
        token = await self._token(org_id, SecretName.TELEGRAM_BOT_TOKEN)
        if token is None:
            return _failed("Telegram is not connected")
        body: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if thread_id is not None:
            body["message_thread_id"] = thread_id
        url = f"{self._telegram_url}/bot{token.get_secret_value()}/sendMessage"
        answer = await self._post(url, json=body)
        if isinstance(answer, SendResult):
            return answer
        if answer.status_code >= 500:
            return _unknown(f"Telegram answered {answer.status_code}")
        try:
            data = answer.json()
        except ValueError:
            return _unknown("Telegram sent an answer that is not JSON")
        if isinstance(data, dict) and data.get("ok"):
            message_id = (data.get("result") or {}).get("message_id")
            return SendResult("sent", receipt=f"telegram:{chat_id}:{message_id}")
        description = data.get("description") if isinstance(data, dict) else None
        return _failed(f"Telegram refused the message: {description or answer.status_code}")

    async def slack(
        self, org_id: uuid.UUID, channel: str, text: str, thread_ts: str | None = None
    ) -> SendResult:
        token = await self._token(org_id, SecretName.SLACK_BOT_TOKEN)
        if token is None:
            return _failed("Slack is not connected")
        body: dict[str, Any] = {"channel": channel, "text": text}
        if thread_ts:
            body["thread_ts"] = thread_ts
        answer = await self._post(
            f"{self._slack_url}/chat.postMessage",
            json=body,
            headers={"Authorization": f"Bearer {token.get_secret_value()}"},
        )
        if isinstance(answer, SendResult):
            return answer
        if answer.status_code >= 500:
            return _unknown(f"Slack answered {answer.status_code}")
        try:
            data = answer.json()
        except ValueError:
            return _unknown("Slack sent an answer that is not JSON")
        if isinstance(data, dict) and data.get("ok"):
            return SendResult("sent", receipt=f"slack:{data.get('channel')}:{data.get('ts')}")
        error = data.get("error") if isinstance(data, dict) else None
        return _failed(f"Slack refused the message: {error or answer.status_code}")

    async def email(self, address: str, subject: str, text: str) -> SendResult:
        if self._email is None:
            return _failed("email is not configured")
        answer = await self._post(
            self._sendgrid_url,
            json={
                "personalizations": [{"to": [{"email": address}]}],
                "from": {"email": self._email.sender},
                "subject": subject,
                "content": [{"type": "text/plain", "value": text}],
            },
            headers={"Authorization": f"Bearer {self._email.api_key.get_secret_value()}"},
        )
        if isinstance(answer, SendResult):
            return answer
        if answer.status_code in (200, 202):
            return SendResult("sent", receipt=answer.headers.get("x-message-id") or "sendgrid")
        if answer.status_code >= 500:
            return _unknown(f"SendGrid answered {answer.status_code}")
        return _failed(f"SendGrid refused the message: {answer.status_code}")


@dataclass(frozen=True, slots=True)
class _Claimed:
    id: uuid.UUID
    org_id: uuid.UUID
    job_id: uuid.UUID
    fire_id: uuid.UUID
    kind: str
    target: str
    text: str
    attempts: int


class Deliverer:
    """Claims due deliveries and sends them, with the receipt rules of this module."""

    def __init__(self, engine: AsyncEngine, sender: Sender) -> None:
        self._engine = engine
        self._sender = sender

    async def aclose(self) -> None:
        await self._sender.aclose()

    async def expire(self) -> int:
        """Marks each ``sending`` row with an expired lease ``unknown``. Returns the count."""
        async with self._engine.begin() as connection:
            rows = await connection.execute(
                update(Delivery)
                .where(Delivery.status == "sending", Delivery.lease_expires_at < func.now())
                .values(
                    status="unknown",
                    lease_expires_at=None,
                    error="the sender stopped during the call; the result is not known",
                )
                .returning(Delivery.id)
            )
            return len(rows.all())

    async def claim(self, limit: int = BATCH) -> list[_Claimed]:
        due = (
            select(Delivery.id)
            .where(Delivery.status == "pending", Delivery.next_attempt_at <= func.now())
            .order_by(Delivery.next_attempt_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        async with self._engine.begin() as connection:
            rows = (
                await connection.execute(
                    update(Delivery)
                    .where(Delivery.id.in_(due))
                    .values(
                        status="sending",
                        attempts=Delivery.attempts + 1,
                        lease_expires_at=func.now() + timedelta(seconds=LEASE_S),
                    )
                    .returning(
                        Delivery.id,
                        Delivery.org_id,
                        Delivery.job_id,
                        Delivery.fire_id,
                        Delivery.kind,
                        Delivery.target,
                        Delivery.text,
                        Delivery.attempts,
                    )
                )
            ).all()
        return [_Claimed(**row._asdict()) for row in rows]

    async def run_once(self) -> int:
        """Expires old leases, then sends the due rows. Returns how many rows it sent."""
        await self.expire()
        sent = 0
        # One row at a time: a row is claimed just before its send, so a slow send cannot use
        # up the lease of a row that waits behind it.
        while sent < BATCH:
            claimed = await self.claim(limit=1)
            if not claimed:
                break
            item = claimed[0]
            try:
                result = await self.send(item)
            except Exception as error:
                log.exception("delivery %s failed", item.id)
                result = _unknown(f"the sender failed: {type(error).__name__}")
            await self._record(item, result)
            sent += 1
        return sent

    async def send(self, item: _Claimed) -> SendResult:
        kind, _, rest = item.target.partition(":")
        if item.target == "origin":
            return await self._origin(item)
        if kind == "telegram":
            return await self._sender.telegram(item.org_id, rest, item.text)
        if kind == "slack":
            return await self._sender.slack(item.org_id, rest, item.text)
        if kind == "email":
            first = item.text.split("\n", 1)[0][:120]
            return await self._sender.email(rest, f"sama: {first}", item.text)
        if item.target == "none":
            return SendResult("sent", receipt="none")
        return _failed(f"the target {item.target!r} is not known")

    async def _origin(self, item: _Claimed) -> SendResult:
        """The chat where the job was made. A job from the web app, or with no chat, keeps the
        result in the session of the fire, and that is the receipt."""
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(Job.origin_session_id, JobFire.session_id, Job.owner_user_id)
                    .join(JobFire, and_(JobFire.org_id == Job.org_id, JobFire.job_id == Job.id))
                    .where(Job.org_id == item.org_id, JobFire.id == item.fire_id)
                )
            ).one()
            binding = None
            if row.origin_session_id is not None:
                binding = (
                    await connection.execute(
                        select(SessionBinding.channel, SessionBinding.external_key)
                        .where(
                            SessionBinding.org_id == item.org_id,
                            SessionBinding.session_id == row.origin_session_id,
                            SessionBinding.channel.in_(("telegram", "slack")),
                        )
                        .order_by(SessionBinding.created_at)
                        .limit(1)
                    )
                ).one_or_none()
        if binding is not None:
            parts = binding.external_key.split(":")
            if binding.channel == "telegram" and len(parts) == 3 and parts[1] == "chat":
                return await self._sender.telegram(item.org_id, parts[2], item.text)
            if binding.channel == "slack" and len(parts) >= 3 and parts[1] in ("dm", "thread"):
                thread = parts[3] if parts[1] == "thread" and len(parts) > 3 else None
                return await self._sender.slack(item.org_id, parts[2], item.text, thread or None)
        return SendResult("sent", receipt=f"session:{row.session_id}")

    async def _record(self, item: _Claimed, result: SendResult) -> None:
        values: dict[str, Any] = {"lease_expires_at": None, "error": result.error}
        if result.outcome == "sent":
            values.update(status="sent", provider_message_id=result.receipt, sent_at=func.now())
        elif result.outcome == "unknown":
            values.update(status="unknown")
        elif item.attempts >= MAX_ATTEMPTS:
            values.update(status="failed")
        else:
            delay = timedelta(seconds=BACKOFF_S * 2**item.attempts)
            values.update(status="pending", next_attempt_at=func.now() + delay)
        # A send that ends after its lease expired still keeps its receipt: the row became
        # ``unknown``, and the provider says that the message went out.
        current = ("sending", "unknown") if result.outcome == "sent" else ("sending",)
        async with self._engine.begin() as connection:
            await connection.execute(
                update(Delivery)
                .where(Delivery.id == item.id, Delivery.status.in_(current))
                .values(**values)
            )
        if result.outcome != "sent":
            log.warning(
                "delivery %s of job %s: %s (%s)", item.id, item.job_id, result.outcome, result.error
            )
