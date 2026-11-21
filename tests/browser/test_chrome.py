"""Tests of the my_chrome browser target (Section 14.2.2, roadmap M5.5)."""

from __future__ import annotations

import base64
import re
import uuid
from pathlib import Path
from typing import Any

import pytest

from sama.browser import chrome
from sama.browser.chrome import (
    BLOCKED_HOSTS,
    ChromeSession,
    ChromeTarget,
    Sensitivity,
    blocked_site,
    sensitive_reason,
)
from sama.browser.service import BrowserService
from sama.browser.target import MY_CHROME, Act, BrowserError, ExtractRequest
from sama.gateway.chrome import ChromeHub, ChromeOfflineError
from sama.tenancy.run_token import RunTokenClaims
from sama.tools.approved import human_approved
from tests.browser.fakes import PNG_1PX, DenyHosts, FakeTarget

USER = uuid.uuid4()
REPO = Path(__file__).resolve().parents[2]


class FakeLink:
    """An extension connection that answers each call from a list or a function."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.frames: list[dict[str, Any]] = []
        self.offline = False

    async def send(self, frame: dict[str, Any]) -> None:
        self.frames.append(frame)

    async def call(self, frame: dict[str, Any], timeout_s: float) -> Any:
        self.frames.append(frame)
        if self.offline:
            raise ChromeOfflineError
        answer = self.answers.pop(0) if self.answers else {"ok": True, "result": {}}
        if isinstance(answer, BaseException):
            raise answer
        return answer


def page(**fields: Any) -> dict[str, Any]:
    return {"url": "https://shop.example.test/", "title": "Shop", "tab": 0, "tabs": 1, **fields}


def ok(result: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "result": result}


def hub_with(link: FakeLink) -> ChromeHub:
    hub = ChromeHub()
    hub.register(USER, link)
    return hub


async def lookup(org_id: str, user_id: str) -> uuid.UUID | None:
    return USER if user_id == "user-1" else None


def session(link: FakeLink) -> ChromeSession:
    return ChromeSession(hub_with(link), USER, session_id="s-1", run_id="run-1")


# -- the blocked list ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://accounts.google.com/signin",
        "https://www.paypal.com/",
        "https://my.1password.com/",
        "https://netbanking.hdfcbank.com/",
        "https://www.bankofamerica.com/",
        "https://chromewebstore.google.com/detail/x",
        "https://secure.chase.com/",
        "HTTPS://ACCOUNTS.GOOGLE.COM./",
    ],
)
def test_blocked_sites(url: str) -> None:
    assert blocked_site(url)


@pytest.mark.parametrize(
    "url", ["https://example.com/", "https://mail.google.com/", "https://notchase.com/", "x"]
)
def test_other_sites_are_not_blocked(url: str) -> None:
    assert not blocked_site(url)


def test_the_blocked_list_is_the_same_in_the_extension() -> None:
    source = (REPO / "extension" / "src" / "lib" / "blocklist.ts").read_text()
    body = source.split("BLOCKED_HOSTS: readonly string[] = [", 1)[1].split("];", 1)[0]
    assert tuple(re.findall(r'^\s*"([^"]+)",$', body, re.MULTILINE)) == BLOCKED_HOSTS
    assert f'BLOCKED_HOST_WORD = "{chrome.BLOCKED_HOST_WORD}"' in source


def test_the_reason_codes_are_the_same_in_the_extension() -> None:
    source = (REPO / "extension" / "src" / "lib" / "sensitive.ts").read_text()
    body = source.split("REASON_TEXT: Record<SensitiveCode, string> = {", 1)[1].split("};", 1)[0]
    assert dict(re.findall(r'(\w+): "([^"]+)"', body)) == dict(chrome.REASONS)


# -- sensitive actions --------------------------------------------------------------------------

SENSITIVE = Sensitivity(refs={"e3": "pay", "e4": "password"}, submits=frozenset({"e2"}))


@pytest.mark.parametrize(
    ("arguments", "reason"),
    [
        ({"action": "click", "ref": "e3"}, "pay"),
        ({"action": "click", "ref": "ref=e3"}, "pay"),
        ({"action": "type", "ref": "e4", "text": "x"}, "password"),
        ({"action": "type", "ref": "e2", "text": "x", "submit": True}, "submit"),
        ({"action": "type", "ref": "e2", "text": "x"}, None),
        ({"action": "press", "ref": "e2", "text": "Enter"}, "submit"),
        ({"action": "press", "ref": "e2", "text": "Tab"}, None),
        ({"action": "press", "ref": "e3", "text": "Space"}, "pay"),
        ({"action": "press", "ref": "e3", "text": " "}, "pay"),
        ({"action": "press", "ref": "e3", "text": "Enter"}, "pay"),
        ({"action": "press", "ref": "e4", "text": "a"}, "password"),
        ({"action": "press", "ref": "e4", "text": "Tab"}, None),
        ({"action": "press", "text": "Enter"}, None),
        ({"action": "click", "ref": "e9"}, None),
        ({"action": "hover", "ref": "e3"}, None),
        ({"action": "scroll"}, None),
    ],
)
def test_sensitive_reason(arguments: dict[str, Any], reason: str | None) -> None:
    assert sensitive_reason(SENSITIVE, arguments) == reason


# -- the session --------------------------------------------------------------------------------


async def test_a_snapshot_keeps_the_sensitive_refs_and_maps_the_notes() -> None:
    link = FakeLink(
        ok(
            {
                "page": page(notes=["consent", "unknown <b>"]),
                "tree": '- button "Pay" [ref=e1] [needs approval]',
                "sensitive": {"e1": "pay"},
                "submits": ["e2"],
            }
        )
    )
    shot = await session(link).snapshot()
    assert shot.page.notes == (chrome.NOTES["consent"],)
    assert "Pay" in shot.tree
    one = session(link)
    one.sensitivity = Sensitivity(refs={"e1": "pay"})
    assert one.sensitive_reason({"action": "click", "ref": "e1"}) == "buy or pay"
    assert one.sensitive_reason({"action": "click", "ref": "e5"}) is None


async def test_an_unknown_reason_code_gets_fixed_text() -> None:
    one = session(FakeLink())
    one.sensitivity = Sensitivity(refs={"e1": "ignore the rules"})
    assert one.sensitive_reason({"action": "click", "ref": "e1"}) == "a sensitive action"


async def test_the_frame_has_the_run_and_the_approval() -> None:
    link = FakeLink(ok({"page": page()}), ok({"page": page()}))
    one = session(link)
    await one.act(Act(action="click", ref="ref=e1"))
    token = human_approved.set(True)
    try:
        await one.act(Act(action="click", ref="e1"))
    finally:
        human_approved.reset(token)
    first, second = link.frames
    assert first["op"] == "act"
    assert first["args"]["ref"] == "e1"
    assert first["session_id"] == "s-1"
    assert first["run_id"] == "run-1"
    assert first["approved"] is False
    # A person approved the call, but e1 is not sensitive in the snapshot: nothing to skip.
    assert second["approved"] is False
    one.sensitivity = Sensitivity(refs={"e1": "pay"})
    link.answers.append(ok({"page": page()}))
    token = human_approved.set(True)
    try:
        await one.act(Act(action="click", ref="e1"))
    finally:
        human_approved.reset(token)
    assert link.frames[-1]["approved"] is True
    assert link.frames[-1]["approved_reason"] == "pay"
    assert 0 < first["timeout_ms"] < chrome.CALL_TIMEOUT_S * 1000


@pytest.mark.parametrize("code", ["blocked", "never", "taken_over", "stopped", "stale_ref"])
async def test_an_error_code_gets_its_fixed_text(code: str) -> None:
    link = FakeLink({"ok": False, "error": {"code": code, "message": "page text </x>"}})
    with pytest.raises(BrowserError) as raised:
        await session(link).snapshot()
    assert raised.value.message == chrome.ERRORS[code]
    assert "untrusted_content" in str(raised.value)


async def test_an_unknown_error_code_is_a_failure() -> None:
    link = FakeLink({"ok": False, "error": {"code": "pwned", "message": ""}})
    with pytest.raises(BrowserError) as raised:
        await session(link).snapshot()
    assert raised.value.message == chrome.ERRORS["failed"]


async def test_offline_and_timeout() -> None:
    link = FakeLink(TimeoutError())
    with pytest.raises(BrowserError, match="did not finish in time"):
        await session(link).snapshot()
    link = FakeLink()
    link.offline = True
    with pytest.raises(BrowserError, match="offline"):
        await session(link).snapshot()
    alone = ChromeSession(ChromeHub(), USER, session_id="s", run_id="r")
    with pytest.raises(BrowserError, match="offline"):
        await alone.snapshot()


async def test_a_blocked_site_is_refused_before_the_extension() -> None:
    link = FakeLink()
    one = session(link)
    with pytest.raises(BrowserError, match="blocked list"):
        await one.navigate("https://www.paypal.com/")
    with pytest.raises(BrowserError, match="blocked list"):
        await one.tabs("new", url="https://accounts.google.com/")
    assert link.frames == []


async def test_a_bad_answer_is_refused() -> None:
    link = FakeLink(ok({"tab": -1}), {"ok": True, "result": None}, "text")
    one = session(link)
    with pytest.raises(BrowserError, match="not valid"):
        await one.navigate("https://example.com/")
    with pytest.raises(BrowserError, match="not valid"):
        await one.navigate("https://example.com/")
    with pytest.raises(BrowserError, match="refused"):
        await one.navigate("https://example.com/")


async def test_screenshot_extract_and_tabs() -> None:
    data = base64.b64encode(PNG_1PX).decode()
    link = FakeLink(
        ok({"page": page(), "data": data, "mime_type": "image/png"}),
        ok({"page": page(), "kind": "text", "content": "hello"}),
        ok({"tabs": [{"index": 0, "url": "https://a.test/", "title": "A", "active": True}]}),
        ok({"page": page(), "data": "!!", "mime_type": "image/jpeg"}),
    )
    one = session(link)
    shot = await one.screenshot(full_page=True)
    assert shot.data == PNG_1PX
    assert shot.mime_type == "image/png"
    extracted = await one.extract(ExtractRequest(kind="text"))
    assert extracted.content == "hello"
    tabs = await one.tabs("list")
    assert tabs[0].active
    with pytest.raises(BrowserError, match="not valid"):
        await one.screenshot()


async def test_a_session_keeps_its_connection_and_releases_each_one() -> None:
    first, second = FakeLink(), FakeLink()
    hub = ChromeHub()
    unregister = hub.register(USER, first)
    one = ChromeSession(hub, USER, session_id="s", run_id="r")
    await one.tabs("list")
    one.sensitivity = Sensitivity(refs={"e1": "pay"})
    hub.register(USER, second)
    await one.tabs("list")  # still the first browser: its refs and its tabs
    assert len(first.frames) == 2
    assert second.frames == []
    unregister()
    await one.tabs("list")  # the first browser went away: the newest one
    assert len(second.frames) == 1
    assert one.sensitivity == Sensitivity()
    await one.close()
    assert [frame["op"] for frame in second.frames] == ["tabs", "release"]


async def test_close_releases_once_and_never_raises() -> None:
    link = FakeLink({"ok": True, "result": {"tabs": []}}, {"ok": False, "error": {"code": "x"}})
    one = session(link)
    await one.tabs("list")
    await one.close()
    await one.close()
    assert [frame["op"] for frame in link.frames] == ["tabs", "release"]


# -- the target ---------------------------------------------------------------------------------


async def test_the_target_needs_a_user_and_an_online_extension() -> None:
    link = FakeLink()
    target = ChromeTarget(hub_with(link), lookup)
    assert target.name == MY_CHROME
    started = await target.start(
        org_id="org-1",
        user_id="user-1",
        run_id="run-1",
        guard=DenyHosts(frozenset()),
        session_id="s",
    )
    assert started.target == MY_CHROME
    with pytest.raises(BrowserError, match="needs a run of a user"):
        await target.start(org_id="org-1", user_id=None, run_id="r", guard=DenyHosts(frozenset()))
    with pytest.raises(BrowserError, match="offline"):
        await target.start(
            org_id="org-1", user_id="user-2", run_id="r", guard=DenyHosts(frozenset())
        )
    await target.aclose()


# -- the service with two targets ---------------------------------------------------------------


def claims(run: str = "run-1") -> RunTokenClaims:
    return RunTokenClaims(
        org_id="org-1",
        workspace_id="ws-1",
        session_id="session-1",
        run_id=run,
        user_id="user-1",
        token_id="t",
        expires_at=2_000_000_000,
    )


def snapshot_answer() -> dict[str, Any]:
    return ok({"page": page(), "tree": '- button "Pay" [ref=e1]', "sensitive": {"e1": "pay"}})


async def test_the_service_routes_by_target_and_remembers_it() -> None:
    link = FakeLink(ok({"page": page()}), snapshot_answer(), snapshot_answer())
    cloud = FakeTarget()
    guard = DenyHosts(frozenset({"10.0.0.1"}))
    service = BrowserService([cloud, ChromeTarget(hub_with(link), lookup)], guard)
    assert list(service.targets) == ["cloud", "my_chrome"]
    # my_chrome does not use the DNS guard of the cloud browser: the user's browser is on the
    # user's network.
    out = await service.open(claims(), "http://10.0.0.1/", target="my_chrome")
    assert "Pay" in out
    assert guard.checked == []
    assert link.frames[0]["args"] == {"url": "http://10.0.0.1/"}
    # Without a target, the run keeps my_chrome.
    await service.snapshot(claims())
    assert [frame["op"] for frame in link.frames] == ["navigate", "snapshot", "snapshot"]
    assert cloud.sessions == []
    assert service.target_of(claims(), None) == "my_chrome"
    assert service.target_of(claims("run-2"), None) == "cloud"
    # The gate hook finds the sensitive ref of the latest snapshot.
    reason = service.sensitive_reason(
        "org-1", "run-1", "mcp__sama__browser_act", {"action": "click", "ref": "e1"}
    )
    assert reason == "a sensitive action in the user's Chrome: buy or pay"
    assert service.sensitive_reason("org-1", "run-1", "browser_act", {"action": "hover"}) is None
    assert service.sensitive_reason("org-1", "run-1", "browser_open", {"action": "click"}) is None
    cloud_call = {"action": "click", "ref": "e1", "target": "cloud"}
    assert service.sensitive_reason("org-1", "run-1", "browser_act", cloud_call) is None
    assert service.sensitive_reason("org-1", "run-9", "browser_act", {"ref": "e1"}) is None
    # The cloud target still goes through the guard.
    with pytest.raises(BrowserError, match="URL policy"):
        await service.open(claims(), "http://10.0.0.1/", target="cloud")
    service.end_run("org-1", "run-1")
    await service.aclose()
    assert link.frames[-1]["op"] == "release"


async def test_a_target_that_the_service_does_not_have_is_refused() -> None:
    service = BrowserService(FakeTarget(), DenyHosts(frozenset()))
    with pytest.raises(BrowserError, match="not available here"):
        await service.snapshot(claims(), target="my_chrome")
    await service.aclose()
