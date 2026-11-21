"""The browser vault and the URL policy of each org in ``BrowserService`` (Section 14.1, M5.3)."""

from __future__ import annotations

import html
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import quote, quote_plus

import pytest
from pydantic import SecretStr

from sama.browser.logins import Credentials, Login, LoginError, normal_site
from sama.browser.policy import OrgUrlPolicy, PolicyCache
from sama.browser.service import NO_LOGINS, REDACTED, BrowserService
from sama.browser.target import BrowserError
from sama.tools.outbound import BlockedUrlError
from tests.browser.fakes import AllowAll, FakeSession, FakeTarget, FillSession
from tests.support import make_claims

USER = "user-1"
PASSWORD = "s3cret-Passw0rd!"
USERNAME = "robot@example.com"
NOW = datetime(2026, 11, 21, tzinfo=UTC)


def login(name: str, site: str, *, personal: bool) -> Login:
    return Login(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        user_id=uuid.UUID(int=1) if personal else None,
        name=name,
        site=normal_site(site),
        created_at=NOW,
        updated_at=NOW,
    )


class Logins:
    def __init__(self, *logins: Login) -> None:
        self.logins = list(logins)
        self.asked: list[tuple[str, str]] = []
        self.secrets: dict[uuid.UUID, Credentials] = {}
        self.broken: set[uuid.UUID] = set()

    async def usable(self, org_id: str, user_id: str) -> list[Login]:
        self.asked.append((org_id, user_id))
        return self.logins

    async def credentials(self, login: Login) -> Credentials:
        if login.id in self.broken:
            raise LoginError("the vault cannot open this login; ask an admin to set it again")
        return self.secrets.get(
            login.id, Credentials(username=SecretStr(USERNAME), password=SecretStr(PASSWORD))
        )


class Policies:
    def __init__(self, policy: OrgUrlPolicy) -> None:
        self.value = policy

    async def policy(self, org_id: str) -> OrgUrlPolicy:
        return self.value


@pytest.fixture
def logins() -> Logins:
    return Logins(
        login("Example", "https://example.com", personal=False),
        login("example", "https://accounts.example.com", personal=True),
        login("Other", "https://other.test", personal=False),
    )


@pytest.fixture
def target() -> FakeTarget:
    return FakeTarget(session_class=FillSession)


@pytest.fixture
async def service(target: FakeTarget, logins: Logins) -> AsyncIterator[BrowserService]:
    service = BrowserService(target, AllowAll(), logins=logins)
    yield service
    await service.aclose()


async def test_list_shows_names_and_sites_but_no_secret(
    service: BrowserService, logins: Logins
) -> None:
    text = await service.vault_list(make_claims())
    assert '"name": "Example"' in text
    assert '"site": "https://other.test"' in text
    assert '"scope": "personal"' in text
    assert PASSWORD not in text
    assert USERNAME not in text
    assert logins.asked == [("org-1", USER)]


async def test_fill_uses_the_personal_login_first_and_hides_the_values(
    service: BrowserService, target: FakeTarget
) -> None:
    claims = make_claims()
    await service.open(claims, "https://accounts.example.com/login")
    (session,) = target.sessions
    assert isinstance(session, FillSession)
    # The page shows the user name and the password after the fill (a text field).
    session.tree = f'- textbox "User" [ref=e1]: {USERNAME}\n- textbox "Pin" [ref=e2]: {PASSWORD}'
    text = await service.vault_fill(claims, " EXAMPLE ", submit=True)
    (request,) = session.fills
    assert request.username.get_secret_value() == USERNAME
    assert request.password.get_secret_value() == PASSWORD
    assert request.submit is True
    assert request.site_allows("https://accounts.example.com/login")
    assert not request.site_allows("https://example.com/login")  # the personal login won
    assert PASSWORD not in text
    assert USERNAME not in text
    assert text.count(REDACTED) == 2
    assert 'Note: sama filled the user name and the password of the saved login "example"' in text
    assert "pressed Enter" in text
    # Later results of the run hide the values too.
    later = await service.snapshot(claims)
    assert PASSWORD not in later
    assert USERNAME not in later
    # The run end forgets the values.
    service.end_run("org-1", "run-1")
    assert service._secrets == {}


async def test_fill_refuses_a_page_of_another_site(
    service: BrowserService, target: FakeTarget
) -> None:
    claims = make_claims()
    await service.open(claims, "https://evil.test/login")
    with pytest.raises(BrowserError, match="not on the site of this login"):
        await service.vault_fill(claims, "Other")
    (session,) = target.sessions
    assert isinstance(session, FillSession)
    assert session.fills == []


async def test_an_error_after_a_fill_hides_the_values(
    service: BrowserService, target: FakeTarget
) -> None:
    claims = make_claims()
    await service.open(claims, "https://other.test/")
    await service.vault_fill(claims, "Other", snapshot=False)
    (session,) = target.sessions
    session.error = BrowserError("the snapshot failed", detail=f"text near {PASSWORD} here")
    with pytest.raises(BrowserError) as raised:
        await service.snapshot(claims)
    assert PASSWORD not in str(raised.value)
    assert REDACTED in str(raised.value)


async def test_fill_errors_are_clear(service: BrowserService, logins: Logins) -> None:
    claims = make_claims()
    with pytest.raises(BrowserError, match="no saved login with this name"):
        await service.vault_fill(claims, "missing")
    logins.broken.add(logins.logins[2].id)
    with pytest.raises(BrowserError, match="vault cannot open"):
        await service.vault_fill(claims, "Other")
    shared = claims.model_copy(update={"user_id": None})
    with pytest.raises(BrowserError, match="private session"):
        await service.vault_list(shared)


async def test_a_short_user_name_is_not_replaced_but_a_short_password_is(
    service: BrowserService, logins: Logins, target: FakeTarget
) -> None:
    other = logins.logins[2]
    logins.secrets[other.id] = Credentials(username=SecretStr("al"), password=SecretStr("pw9"))
    claims = make_claims()
    await service.open(claims, "https://other.test/")
    (session,) = target.sessions
    session.tree = "- text: al typed pw9"
    text = await service.vault_fill(claims, "Other")
    assert "al typed [REDACTED]" in text


async def test_without_logins_the_list_says_how_to_add_them(target: FakeTarget) -> None:
    service = BrowserService(target, AllowAll(), logins=Logins())
    assert await service.vault_list(make_claims()) == NO_LOGINS
    off = BrowserService(target, AllowAll())
    with pytest.raises(BrowserError, match="vault is off"):
        await off.vault_list(make_claims())


async def test_a_target_that_cannot_fill_gives_a_clear_error(logins: Logins) -> None:
    service = BrowserService(FakeTarget(session_class=FakeSession), AllowAll(), logins=logins)
    claims = make_claims()
    await service.open(claims, "https://other.test/")
    with pytest.raises(BrowserError, match="cannot fill saved logins"):
        await service.vault_fill(claims, "Other")
    await service.aclose()


async def test_the_url_policy_of_the_org_blocks_open_and_new_tabs(target: FakeTarget) -> None:
    policies = Policies(OrgUrlPolicy(block_hosts=("blocked.test",)))
    cache = PolicyCache(policies)
    service = BrowserService(target, AllowAll(), policies=cache)
    claims = make_claims()
    with pytest.raises(BrowserError, match="block list of the org"):
        await service.open(claims, "https://www.blocked.test/")
    assert target.starts == []  # the check runs before a session starts
    await service.open(claims, "https://example.com/")
    with pytest.raises(BrowserError, match="block list of the org"):
        await service.tabs(claims, "new", url="blocked.test")
    # The session gets the guard of the org, and a change applies after forget_policy.
    (guard,) = target.guards
    policies.value = OrgUrlPolicy(allow_hosts=("example.com",))
    await guard.check("https://other.test/")  # the cache still has the old policy
    service.forget_policy("org-1")
    with pytest.raises(BlockedUrlError, match="allow list"):
        await guard.check("https://other.test/")
    assert service.guard_for("org-1") is guard
    assert service.guard_for("org-2") is not guard
    await service.aclose()


async def test_no_screenshot_after_a_fill(service: BrowserService, target: FakeTarget) -> None:
    claims = make_claims()
    await service.open(claims, "https://other.test/")
    caption, _ = await service.screenshot(claims)
    assert "screenshot" in caption
    await service.vault_fill(claims, "Other", snapshot=False)
    with pytest.raises(BrowserError, match="screenshots are off"):
        await service.screenshot(claims)
    # Another run can still take screenshots.
    other = make_claims("run-2")
    await service.open(other, "https://other.test/")
    await service.screenshot(other)


async def test_escaped_and_encoded_forms_of_the_values_are_hidden(
    service: BrowserService, logins: Logins, target: FakeTarget
) -> None:
    odd = 'pa"ss\\wo&rd <x> é'
    logins.secrets[logins.logins[2].id] = Credentials(
        username=SecretStr("someone@example.com"), password=SecretStr(odd)
    )
    claims = make_claims()
    await service.open(claims, "https://other.test/")
    (session,) = target.sessions
    await service.vault_fill(claims, "Other", snapshot=False)
    session.tree = "\n".join(
        [
            json.dumps({"field": odd}),
            json.dumps({"field": odd}, ensure_ascii=False),
            "https://other.test/?p=" + quote_plus(odd),
            "https://other.test/?p=" + quote(odd, safe=""),
            html.escape(odd),
            "user someone%40example.com",
        ]
    )
    text = await service.snapshot(claims)
    for form in (
        odd,
        json.dumps(odd)[1:-1],
        quote_plus(odd),
        html.escape(odd),
        "someone%40example.com",
    ):
        assert form not in text
