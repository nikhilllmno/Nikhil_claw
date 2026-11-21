"""The saved logins of the browser vault (docs/architecture.md, Section 14.1; roadmap M5.3).

A saved login has a name (for example ``github``), a site (the origin where sama fills it), and
a user name and a password. The table ``browser_logins`` keeps the name and the site. The vault
keeps the user name and the password in one entry (``browser_login:<id>``), so the control plane
is the only place that can read them.

- An org login (no ``user_id``) is for every member of the org. Only an org admin adds, changes,
  or removes it.
- A personal login (with ``user_id``) is for one user. The user adds it. The user and the org
  admins can change or remove it.

The agent sees only the name and the site. The tool ``browser_vault`` fills the user name and the
password into the page inside the control plane (Section 14.1).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol
from urllib.parse import urlsplit

from pydantic import SecretStr
from sqlalchemy import delete, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db.models import AuditEntry, BrowserLogin
from sama.tools.outbound import ascii_host
from sama.vault.crypto import VaultError
from sama.vault.store import Vault, browser_login_secret

log: Final = logging.getLogger("sama.browser")

MAX_NAME_CHARS: Final = 80
MAX_SITE_CHARS: Final = 300
MAX_USERNAME_CHARS: Final = 500
MAX_PASSWORD_CHARS: Final = 1_000
MAX_LOGINS: Final = 200
"""The most logins that the tool lists for one user."""
_DEFAULT_PORTS: Final = {"http": 80, "https": 443}
_HOST: Final = re.compile(r"[a-z0-9_.-]+")


class LoginError(Exception):
    """A login that cannot be added, changed, read, or removed. The message is fixed text of
    sama: it never has a user name or a password. ``status_code`` is the HTTP answer."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class Login:
    """A saved login without its secret."""

    id: uuid.UUID
    org_id: uuid.UUID
    user_id: uuid.UUID | None
    name: str
    site: str
    created_at: datetime
    updated_at: datetime

    @property
    def scope(self) -> str:
        return "personal" if self.user_id is not None else "org"

    def public(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "name": self.name,
            "site": self.site,
            "scope": self.scope,
            "user_id": None if self.user_id is None else str(self.user_id),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class Credentials:
    """The user name and the password of a login. ``repr`` shows neither."""

    username: SecretStr
    password: SecretStr


def normal_name(value: str) -> str:
    name = " ".join(value.split())
    if not 1 <= len(name) <= MAX_NAME_CHARS:
        raise LoginError(f"the name must have 1 to {MAX_NAME_CHARS} characters")
    return name


def normal_site(value: str) -> str:
    """The origin of a site: ``https://host`` or ``https://host:port``. A value without a scheme
    gets ``https://``. Only ``http`` and ``https`` are allowed. The path is dropped."""
    text = value.strip()
    if "://" not in text:
        text = f"https://{text}"
    try:
        parts = urlsplit(text)
        scheme = parts.scheme.lower()
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        raise LoginError("the site is not a valid URL") from None
    if scheme not in _DEFAULT_PORTS:
        raise LoginError("the site must be an http or https URL")
    if parts.username or parts.password:
        raise LoginError("the site must not have a user name or a password")
    if not host:
        raise LoginError("the site has no host")
    try:
        host = str(ipaddress.ip_address(host))
        host = f"[{host}]" if ":" in host else host
    except ValueError:
        try:
            host = ascii_host(host)
        except ValueError:
            raise LoginError("the site is not a valid URL") from None
        if not _HOST.fullmatch(host):
            raise LoginError("the site is not a valid URL") from None
    origin = f"{scheme}://{host}"
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        origin += f":{port}"
    if len(origin) > MAX_SITE_CHARS:
        raise LoginError(f"the site must have at most {MAX_SITE_CHARS} characters")
    return origin


def site_allows(site: str, url: str) -> bool:
    """True if sama may fill a login of ``site`` into a page at ``url``: the same scheme, the
    same port, and the same host or a subdomain of it."""
    try:
        want, got = urlsplit(site), urlsplit(url)
        want_scheme, got_scheme = want.scheme.lower(), got.scheme.lower()
        if want_scheme != got_scheme or want_scheme not in _DEFAULT_PORTS:
            return False
        want_port = want.port or _DEFAULT_PORTS[want_scheme]
        got_port = got.port or _DEFAULT_PORTS[got_scheme]
        want_host, got_host = want.hostname or "", got.hostname or ""
        if not want_host or not got_host or got.username or got.password:
            return False
        try:
            got_host = str(ipaddress.ip_address(got_host))
            want_host = str(ipaddress.ip_address(want_host))
        except ValueError:
            got_host, want_host = ascii_host(got_host), ascii_host(want_host)
    except ValueError:
        return False
    if want_port != got_port:
        return False
    return got_host == want_host or got_host.endswith("." + want_host)


def _secret_value(username: str, password: str) -> str:
    return json.dumps({"username": username, "password": password}, ensure_ascii=False)


def _check_username(value: str) -> str:
    if not 1 <= len(value) <= MAX_USERNAME_CHARS or value != value.strip():
        raise LoginError(
            f"the user name must have 1 to {MAX_USERNAME_CHARS} characters and no spaces at"
            " its ends"
        )
    return value


def _check_password(value: str) -> str:
    if not 1 <= len(value) <= MAX_PASSWORD_CHARS:
        raise LoginError(f"the password must have 1 to {MAX_PASSWORD_CHARS} characters")
    return value


_COLUMNS: Final = (
    BrowserLogin.id,
    BrowserLogin.org_id,
    BrowserLogin.user_id,
    BrowserLogin.name,
    BrowserLogin.site,
    BrowserLogin.created_at,
    BrowserLogin.updated_at,
)


def _row(row: Any) -> Login:
    return Login(**row._asdict())


class LoginSource(Protocol):
    """What the browser tools need from the vault."""

    async def usable(self, org_id: str, user_id: str) -> list[Login]:
        """The logins that the user can use: the org logins and the personal logins."""
        ...

    async def credentials(self, login: Login) -> Credentials:
        """The secret of the login. Raises ``LoginError`` when the vault cannot give it."""
        ...


class BrowserLogins:
    """The saved logins in Postgres and the vault. ``vault`` must be the vault of the process,
    so the redactor knows each value."""

    def __init__(self, engine: AsyncEngine, vault: Vault) -> None:
        self._engine = engine
        self._vault = vault

    # -- reads -------------------------------------------------------------------------------

    async def visible(self, org_id: uuid.UUID, *, user_id: uuid.UUID, admin: bool) -> list[Login]:
        """The logins that a member sees: the org logins and their own logins. An admin sees
        the personal logins of the other members too (the name and the site only)."""
        query = select(*_COLUMNS).where(BrowserLogin.org_id == org_id)
        if not admin:
            query = query.where(
                or_(BrowserLogin.user_id.is_(None), BrowserLogin.user_id == user_id)
            )
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    query.order_by(BrowserLogin.user_id.is_not(None), func.lower(BrowserLogin.name))
                )
            ).all()
        return [_row(row) for row in rows]

    async def usable(self, org_id: str, user_id: str) -> list[Login]:
        org, user = uuid.UUID(org_id), uuid.UUID(user_id)
        logins = await self.visible(org, user_id=user, admin=False)
        return logins[:MAX_LOGINS]

    async def get(self, org_id: uuid.UUID, login_id: uuid.UUID) -> Login | None:
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(*_COLUMNS).where(
                        BrowserLogin.org_id == org_id, BrowserLogin.id == login_id
                    )
                )
            ).first()
        return None if row is None else _row(row)

    async def credentials(self, login: Login) -> Credentials:
        try:
            value = await self._vault.get(login.org_id, browser_login_secret(login.id))
        except VaultError:
            log.error("a browser login does not decrypt org=%s login=%s", login.org_id, login.id)
            raise LoginError(
                "the vault cannot open this login; ask an admin to set it again"
            ) from None
        if value is None:
            raise LoginError("the vault has no secret for this login; ask an admin to set it")
        try:
            data = json.loads(value.get_secret_value())
            username, password = str(data["username"]), str(data["password"])
        except (ValueError, KeyError, TypeError):
            raise LoginError("the vault entry of this login is not valid; set it again") from None
        return Credentials(username=SecretStr(username), password=SecretStr(password))

    # -- writes ------------------------------------------------------------------------------

    async def create(
        self,
        org_id: uuid.UUID,
        *,
        name: str,
        site: str,
        username: str,
        password: SecretStr,
        owner_user_id: uuid.UUID | None,
        actor_user_id: uuid.UUID,
    ) -> Login:
        """Adds a login and its vault entry in one transaction. ``owner_user_id`` makes a
        personal login."""
        name, site = normal_name(name), normal_site(site)
        secret = _secret_value(
            _check_username(username), _check_password(password.get_secret_value())
        )
        try:
            async with self._engine.begin() as connection:
                row = (
                    await connection.execute(
                        insert(BrowserLogin)
                        .values(
                            org_id=org_id,
                            user_id=owner_user_id,
                            name=name,
                            site=site,
                            created_by_user_id=actor_user_id,
                        )
                        .returning(*_COLUMNS)
                    )
                ).one()
                login = _row(row)
                await self._vault.put(
                    org_id,
                    browser_login_secret(login.id),
                    secret,
                    actor_user_id=actor_user_id,
                    connection=connection,
                )
                await _audit(connection, login, "browser.login_created", actor_user_id)
        except IntegrityError as error:
            raise _conflict(error) from None
        log.info("a browser login is added org=%s login=%s", org_id, login.id)
        return login

    async def change(
        self,
        login: Login,
        *,
        name: str | None = None,
        site: str | None = None,
        username: str | None = None,
        password: SecretStr | None = None,
        actor_user_id: uuid.UUID,
    ) -> Login:
        """Changes the name, the site, or the secret. A new user name or password keeps the
        other part of the secret."""
        values: dict[str, object] = {}
        if name is not None:
            values["name"] = normal_name(name)
        if site is not None:
            values["site"] = normal_site(site)
        secret: str | None = None
        if username is not None or password is not None:
            if username is None or password is None:
                old = await self.credentials(login)
                username = old.username.get_secret_value() if username is None else username
                password = old.password if password is None else password
            secret = _secret_value(
                _check_username(username), _check_password(password.get_secret_value())
            )
        try:
            async with self._engine.begin() as connection:
                row = (
                    await connection.execute(
                        update(BrowserLogin)
                        .where(BrowserLogin.org_id == login.org_id, BrowserLogin.id == login.id)
                        .values(**values, updated_at=func.now())
                        .returning(*_COLUMNS)
                    )
                ).first()
                if row is None:
                    raise LoginError("the login does not exist", status_code=404)
                if secret is not None:
                    await self._vault.put(
                        login.org_id,
                        browser_login_secret(login.id),
                        secret,
                        actor_user_id=actor_user_id,
                        connection=connection,
                    )
                changed = _row(row)
                await _audit(connection, changed, "browser.login_changed", actor_user_id)
        except IntegrityError as error:
            raise _conflict(error) from None
        return changed

    async def remove(self, login: Login, *, actor_user_id: uuid.UUID) -> None:
        """Removes the login and its vault entry in one transaction."""
        async with self._engine.begin() as connection:
            gone = (
                await connection.execute(
                    delete(BrowserLogin)
                    .where(BrowserLogin.org_id == login.org_id, BrowserLogin.id == login.id)
                    .returning(BrowserLogin.id)
                )
            ).first()
            if gone is None:
                raise LoginError("the login does not exist", status_code=404)
            await self._vault.delete(
                login.org_id,
                browser_login_secret(login.id),
                actor_user_id=actor_user_id,
                connection=connection,
            )
            await _audit(connection, login, "browser.login_removed", actor_user_id)
        log.info("a browser login is removed org=%s login=%s", login.org_id, login.id)


def _conflict(error: IntegrityError) -> Exception:
    """A duplicate name is a ``409``. Another integrity error is a fault of sama: it goes up
    as it is."""
    if "uq_browser_logins_" in str(error.orig):
        return LoginError("a login with this name exists", status_code=409)
    return error


def can_manage(login: Login, *, user_id: uuid.UUID, admin: bool) -> bool:
    """An org admin manages every login of the org. A member manages their own logins."""
    return admin or (login.user_id is not None and login.user_id == user_id)


async def _audit(
    connection: AsyncConnection, login: Login, action: str, actor_user_id: uuid.UUID
) -> None:
    await connection.execute(
        insert(AuditEntry).values(
            org_id=login.org_id,
            actor_kind="user",
            actor_user_id=actor_user_id,
            action=action,
            target_type="browser_login",
            target_id=str(login.id),
            details={"name": login.name, "site": login.site, "scope": login.scope},
        )
    )
