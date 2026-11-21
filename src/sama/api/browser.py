"""The browser settings of the web API (docs/architecture.md, Sections 14.1 and 16.4; M5.3).

- The URL policy of the org: an org admin reads and sets the allow list and the block list.
- The saved logins of the browser vault: a member lists the org logins and their own logins and
  adds personal logins. An org admin adds org logins and sees, changes, and removes every login.
  No answer has a user name or a password.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from sama.browser.logins import (
    MAX_NAME_CHARS,
    MAX_PASSWORD_CHARS,
    MAX_SITE_CHARS,
    MAX_USERNAME_CHARS,
    BrowserLogins,
    Login,
    LoginError,
    can_manage,
)
from sama.browser.policy import MAX_HOST_CHARS, MAX_HOSTS, PolicyError, PolicyStore
from sama.tenancy.access import Grants

ADMIN_ONLY = "only an owner or an admin of the org can do this"


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


_Host = Annotated[str, Field(max_length=MAX_HOST_CHARS + 50)]
"""An entry of a list: a host, ``*.host``, or a URL (the host is taken from it)."""


class UrlPolicyIn(_Body):
    allow_hosts: list[_Host] = Field(default_factory=list, max_length=MAX_HOSTS)
    """When not empty, the browser opens only these hosts and their subdomains."""
    block_hosts: list[_Host] = Field(default_factory=list, max_length=MAX_HOSTS)
    """The browser never opens these hosts and their subdomains."""


class LoginIn(_Body):
    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    site: str = Field(min_length=1, max_length=MAX_SITE_CHARS)
    """The sign-in page or the origin of the site, for example ``https://github.com``."""
    username: SecretStr = Field(min_length=1, max_length=MAX_USERNAME_CHARS)
    password: SecretStr = Field(min_length=1, max_length=MAX_PASSWORD_CHARS)
    scope: Literal["personal", "org"] = "personal"


class LoginChange(_Body):
    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_CHARS)
    site: str | None = Field(default=None, min_length=1, max_length=MAX_SITE_CHARS)
    username: SecretStr | None = Field(default=None, min_length=1, max_length=MAX_USERNAME_CHARS)
    password: SecretStr | None = Field(default=None, min_length=1, max_length=MAX_PASSWORD_CHARS)


class BrowserSettings:
    """The handlers of the browser routes. The caller checks the membership first and gives
    the grants."""

    def __init__(
        self,
        policies: PolicyStore,
        logins: BrowserLogins | None,
        *,
        on_policy_change: Callable[[str], None] | None = None,
    ) -> None:
        self._policies = policies
        self._logins = logins
        self._on_policy_change = on_policy_change

    def _require_logins(self) -> BrowserLogins:
        if self._logins is None:
            raise HTTPException(status_code=503, detail="the vault is off in this process")
        return self._logins

    async def policy(self, org_id: uuid.UUID, grants: Grants) -> dict[str, Any]:
        _admin(grants)
        return (await self._policies.view(org_id)).public()

    async def set_policy(
        self, org_id: uuid.UUID, grants: Grants, body: UrlPolicyIn
    ) -> dict[str, Any]:
        _admin(grants)
        try:
            view = await self._policies.put(
                org_id,
                allow_hosts=body.allow_hosts,
                block_hosts=body.block_hosts,
                actor_user_id=grants.user_id,
            )
        except PolicyError as error:
            raise HTTPException(status_code=400, detail=str(error)) from None
        if self._on_policy_change is not None:
            self._on_policy_change(str(org_id))
        return view.public()

    async def logins(self, org_id: uuid.UUID, grants: Grants) -> list[dict[str, Any]]:
        logins = await self._require_logins().visible(
            org_id, user_id=grants.user_id, admin=grants.org_admin
        )
        return [_public(login, grants) for login in logins]

    async def add_login(self, org_id: uuid.UUID, grants: Grants, body: LoginIn) -> dict[str, Any]:
        if body.scope == "org":
            _admin(grants)
        try:
            login = await self._require_logins().create(
                org_id,
                name=body.name,
                site=body.site,
                username=body.username.get_secret_value(),
                password=body.password,
                owner_user_id=grants.user_id if body.scope == "personal" else None,
                actor_user_id=grants.user_id,
            )
        except LoginError as error:
            raise HTTPException(status_code=error.status_code, detail=error.message) from None
        return _public(login, grants)

    async def change_login(
        self, org_id: uuid.UUID, login_id: uuid.UUID, grants: Grants, body: LoginChange
    ) -> dict[str, Any]:
        service = self._require_logins()
        login = await self._managed(service, org_id, login_id, grants)
        try:
            changed = await service.change(
                login,
                name=body.name,
                site=body.site,
                username=None if body.username is None else body.username.get_secret_value(),
                password=body.password,
                actor_user_id=grants.user_id,
            )
        except LoginError as error:
            raise HTTPException(status_code=error.status_code, detail=error.message) from None
        return _public(changed, grants)

    async def remove_login(self, org_id: uuid.UUID, login_id: uuid.UUID, grants: Grants) -> None:
        service = self._require_logins()
        login = await self._managed(service, org_id, login_id, grants)
        try:
            await service.remove(login, actor_user_id=grants.user_id)
        except LoginError as error:
            raise HTTPException(status_code=error.status_code, detail=error.message) from None

    async def _managed(
        self, service: BrowserLogins, org_id: uuid.UUID, login_id: uuid.UUID, grants: Grants
    ) -> Login:
        login = await service.get(org_id, login_id)
        visible = login is not None and (
            grants.org_admin or login.user_id is None or login.user_id == grants.user_id
        )
        if login is None or not visible:
            raise HTTPException(status_code=404, detail="the login does not exist")
        if not can_manage(login, user_id=grants.user_id, admin=grants.org_admin):
            raise HTTPException(status_code=403, detail=ADMIN_ONLY)
        return login


def _admin(grants: Grants) -> None:
    if not grants.org_admin:
        raise HTTPException(status_code=403, detail=ADMIN_ONLY)


def _public(login: Login, grants: Grants) -> dict[str, Any]:
    return {
        **login.public(),
        "mine": login.user_id == grants.user_id,
        "can_manage": can_manage(login, user_id=grants.user_id, admin=grants.org_admin),
    }
