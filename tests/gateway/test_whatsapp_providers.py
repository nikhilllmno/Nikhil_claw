"""Unit tests of the WhatsApp providers (docs/architecture.md, Section 11.9)."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from typing import Any
from urllib.parse import parse_qs, urlencode

import httpx
import pytest
from pydantic import SecretStr

from sama.gateway.twilio_client import TwilioClient, signature
from sama.gateway.whatsapp_providers import (
    SECRET_HEADER,
    Button,
    GupshupProvider,
    MediaRef,
    MetaProvider,
    TwilioProvider,
    WhatsAppCredentials,
    WhatsAppError,
    build_provider,
    e164,
)
from sama.media.generate import StoredMedia

NUMBER = "+15550001111"
SID = "AC" + "0" * 32
AUTH = "a" * 32
APP_SECRET = "b" * 32
APP_ID = str(uuid.UUID(int=7))


class Recorder:
    def __init__(self, answers: dict[str, httpx.Response] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.answers = answers or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for prefix, answer in self.answers.items():
            if str(request.url).startswith(prefix):
                return answer
        return httpx.Response(200, json={"messages": [{"id": "wamid.1"}], "sid": "SM1"})

    def http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


def meta(recorder: Recorder) -> MetaProvider:
    return MetaProvider(
        number=NUMBER,
        phone_number_id="1234567",
        access_token=SecretStr("EAAtoken"),
        app_secret=SecretStr(APP_SECRET),
        http=recorder.http(),
    )


def meta_body(*messages: dict[str, Any], phone_number_id: str = "1234567") -> bytes:
    value = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "15550001111", "phone_number_id": phone_number_id},
        "messages": list(messages),
    }
    return json.dumps(
        {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": value}]}]}
    ).encode()


def test_e164_reads_the_forms_of_a_number() -> None:
    assert e164("whatsapp:+15551234567") == "+15551234567"
    assert e164("15551234567") == "+15551234567"
    assert e164("not a number") is None
    assert e164("+0123") is None


# -- Meta ---------------------------------------------------------------------------------------


def test_meta_checks_the_hmac_of_the_raw_body() -> None:
    provider = meta(Recorder())
    body = meta_body()
    digest = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert provider.verify("u", {"x-hub-signature-256": f"sha256={digest}"}, body)
    assert not provider.verify("u", {"x-hub-signature-256": f"sha256={digest}"}, body + b" ")
    assert not provider.verify("u", {}, body)


def test_meta_parses_text_buttons_and_media_of_its_number_only() -> None:
    provider = meta(Recorder())
    body = meta_body(
        {"from": "15557654321", "id": "w1", "type": "text", "text": {"body": " hi "}},
        {
            "from": "15557654321",
            "id": "w2",
            "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {"id": "yes AB12CD"}},
        },
        {
            "from": "15557654321",
            "id": "w3",
            "type": "image",
            "image": {"id": "m1", "mime_type": "image/jpeg", "caption": "look"},
        },
        {"from": "15557654321", "id": "w4", "type": "audio", "audio": {"id": "m2"}},
    )
    messages = provider.parse(body, "application/json")
    assert [(m.id, m.sender, m.text) for m in messages] == [
        ("w1", "+15557654321", "hi"),
        ("w2", "+15557654321", "yes AB12CD"),
        ("w3", "+15557654321", "look"),
        ("w4", "+15557654321", ""),
    ]
    assert messages[2].media == (MediaRef("m1", "image/jpeg", "image"),)
    other = meta_body({"from": "1555", "id": "x", "type": "text"}, phone_number_id="999")
    assert provider.parse(other, "application/json") == []
    assert provider.parse(b"not json", "application/json") == []


async def test_meta_sends_text_buttons_and_media() -> None:
    recorder = Recorder()
    provider = meta(recorder)
    await provider.send_text("+15557654321", "hi")
    await provider.send_buttons(
        "+15557654321", "Approve?", [Button("Approve", "yes AB12CD"), Button("Deny", "no AB12CD")]
    )
    media = StoredMedia(uuid.uuid4(), "image", "image/png", "", b"png")
    await provider.send_media("+15557654321", media, "https://sama.test/m")
    bodies = [json.loads(r.content) for r in recorder.requests]
    assert all(r.url.path == "/v21.0/1234567/messages" for r in recorder.requests)
    assert recorder.requests[0].headers["authorization"] == "Bearer EAAtoken"
    assert bodies[0] == {
        "messaging_product": "whatsapp",
        "to": "15557654321",
        "type": "text",
        "text": {"body": "hi"},
    }
    buttons = bodies[1]["interactive"]["action"]["buttons"]
    assert [b["reply"]["id"] for b in buttons] == ["yes AB12CD", "no AB12CD"]
    assert bodies[2]["image"] == {"link": "https://sama.test/m"}
    document = StoredMedia(uuid.uuid4(), "file", "text/csv", "", b"a,b", "top5.csv")
    await provider.send_media("+15557654321", document, "https://sama.test/d")
    last = json.loads(recorder.requests[-1].content)
    assert last["type"] == "document"
    assert last["document"] == {"link": "https://sama.test/d", "filename": "top5.csv"}
    await provider.aclose()


async def test_meta_downloads_media_only_from_its_hosts() -> None:
    recorder = Recorder(
        {
            "https://graph.facebook.com/v21.0/m1": httpx.Response(
                200, json={"url": "https://lookaside.fbsbx.com/x"}
            ),
            "https://graph.facebook.com/v21.0/m2": httpx.Response(
                200, json={"url": "https://evil.test/x"}
            ),
            "https://lookaside.fbsbx.com/x": httpx.Response(200, content=b"IMG"),
        }
    )
    provider = meta(recorder)
    assert await provider.download(MediaRef("m1", "image/png"), max_bytes=10) == b"IMG"
    with pytest.raises(WhatsAppError, match="not at the provider"):
        await provider.download(MediaRef("m2", "image/png"), max_bytes=10)
    with pytest.raises(WhatsAppError, match="too large"):
        await provider.download(MediaRef("m1", "image/png"), max_bytes=2)


async def test_meta_reports_a_refused_call_without_the_token() -> None:
    recorder = Recorder(
        {"https://graph.facebook.com": httpx.Response(401, json={"error": {"message": "bad"}})}
    )
    provider = meta(recorder)
    with pytest.raises(WhatsAppError) as error:
        await provider.check()
    assert "401" in str(error.value)
    assert "EAAtoken" not in str(error.value)


# -- Twilio -------------------------------------------------------------------------------------


def twilio(recorder: Recorder) -> TwilioProvider:
    return TwilioProvider(
        number=NUMBER,
        client=TwilioClient(SID, SecretStr(AUTH), http=recorder.http()),
        auth_token=SecretStr(AUTH),
    )


def test_the_twilio_signature_matches_the_documented_example() -> None:
    fields = [
        ("CallSid", "CA1234567890ABCDE"),
        ("Caller", "+12349013030"),
        ("Digits", "1234"),
        ("From", "+12349013030"),
        ("To", "+18005551212"),
    ]
    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    assert signature("12345", url, fields) == "0/KCTR6DLpKmkAf8muzZqo1nDgQ="


def test_twilio_verifies_and_parses_a_form() -> None:
    provider = twilio(Recorder())
    url = "https://sama.test/whatsapp/i/webhook/t"
    form = {
        "From": "whatsapp:+15557654321",
        "To": f"whatsapp:{NUMBER}",
        "MessageSid": "SM1",
        "Body": "hello",
        "NumMedia": "1",
        "MediaUrl0": "https://api.twilio.com/2010-04-01/Accounts/x/Messages/SM1/Media/ME1",
        "MediaContentType0": "image/jpeg",
    }
    body = urlencode(form).encode()
    good = {"x-twilio-signature": signature(AUTH, url, form.items())}
    assert provider.verify(url, good, body)
    assert not provider.verify(url + "x", good, body)
    [message] = provider.parse(body, "application/x-www-form-urlencoded")
    assert (message.id, message.sender, message.text) == ("SM1", "+15557654321", "hello")
    assert message.media[0].mime_type == "image/jpeg"
    # A message to another number or from SMS is dropped.
    other = urlencode({**form, "To": "whatsapp:+15550002222"}).encode()
    assert provider.parse(other, "") == []
    sms = urlencode({**form, "From": "+15557654321"}).encode()
    assert provider.parse(sms, "") == []


async def test_twilio_sends_and_downloads_with_basic_auth_only_at_twilio() -> None:
    media_url = f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Messages/SM1/Media/ME1"
    recorder = Recorder(
        {
            media_url: httpx.Response(
                307, headers={"location": "https://s3.amazonaws.com/media.twiliocdn.com/x"}
            ),
            "https://s3.amazonaws.com/": httpx.Response(200, content=b"IMG"),
        }
    )
    provider = twilio(recorder)
    await provider.send_text("+15557654321", "x" * 2000)
    sent = parse_qs(recorder.requests[0].content.decode())
    assert sent["From"] == [f"whatsapp:{NUMBER}"]
    assert sent["To"] == ["whatsapp:+15557654321"]
    assert len(sent["Body"][0]) == 1600
    assert await provider.download(MediaRef(media_url, "image/png"), max_bytes=10) == b"IMG"
    assert "authorization" in recorder.requests[1].headers
    assert "authorization" not in recorder.requests[2].headers
    with pytest.raises(WhatsAppError, match="not of this account"):
        await provider.download(MediaRef("https://evil.test/x", "image/png"), max_bytes=10)
    await provider.aclose()


# -- Gupshup ------------------------------------------------------------------------------------


def gupshup(recorder: Recorder, header: str | None = "hs") -> GupshupProvider:
    return GupshupProvider(
        number=NUMBER,
        app_id=APP_ID,
        app_name="samaapp",
        api_key=SecretStr("gkey"),
        header_secret=SecretStr(header) if header else None,
        http=recorder.http(),
    )


def test_gupshup_checks_its_header_and_parses_both_formats() -> None:
    provider = gupshup(Recorder())
    assert provider.verify("u", {SECRET_HEADER: "hs"}, b"")
    assert not provider.verify("u", {SECRET_HEADER: "no"}, b"")
    assert not gupshup(Recorder(), header=None).verify("u", {SECRET_HEADER: ""}, b""), "fail closed"
    v2 = json.dumps(
        {
            "app": "samaapp",
            "type": "message",
            "payload": {
                "id": "g1",
                "source": "15557654321",
                "type": "quick_reply",
                "payload": {"text": "Approve", "postbackText": "yes AB12CD"},
                "sender": {"phone": "15557654321"},
            },
        }
    ).encode()
    [message] = provider.parse(v2, "application/json")
    assert (message.id, message.sender, message.text) == ("g1", "+15557654321", "yes AB12CD")
    image = json.dumps(
        {
            "app": "samaapp",
            "type": "message",
            "payload": {
                "id": "g2",
                "type": "image",
                "payload": {"url": "https://filemanager.gupshup.io/x", "contentType": "image/png"},
                "sender": {"phone": "15557654321"},
            },
        }
    ).encode()
    assert provider.parse(image, "")[0].media[0].ref == "https://filemanager.gupshup.io/x"
    v3 = meta_body({"from": "15557654321", "id": "w9", "type": "text", "text": {"body": "hey"}})
    assert provider.parse(v3, "")[0].text == "hey"
    other_app = v2.replace(b'"samaapp"', b'"other"')
    assert provider.parse(other_app, "") == []


async def test_gupshup_sends_quick_replies_and_subscribes() -> None:
    recorder = Recorder(
        {
            f"https://api.gupshup.io/wa/app/{APP_ID}/subscription": httpx.Response(
                200, json={"status": "success", "subscription": {"id": "sub1"}}
            ),
            "https://api.gupshup.io/wa/api/v1/msg": httpx.Response(
                200, json={"status": "submitted", "messageId": "gm1"}
            ),
        }
    )
    provider = gupshup(recorder)
    assert await provider.send_buttons("+15557654321", "Approve?", [Button("Approve", "yes A")])
    form = parse_qs(recorder.requests[0].content.decode())
    assert form["source"] == ["15550001111"]
    assert form["destination"] == ["15557654321"]
    assert recorder.requests[0].headers["apikey"] == "gkey"
    message = json.loads(form["message"][0])
    assert message["type"] == "quick_reply"
    assert message["options"] == [{"type": "text", "title": "Approve", "postbackText": "yes A"}]
    assert await provider.subscribe("https://sama.test/hook", SecretStr("hs")) == "sub1"
    subscription = parse_qs(recorder.requests[1].content.decode())
    assert subscription["url"] == ["https://sama.test/hook"]
    assert json.loads(subscription["meta"][0]) == {"X-Sama-Webhook-Secret": "hs"}


async def test_gupshup_reports_an_error_status() -> None:
    recorder = Recorder(
        {"https://api.gupshup.io": httpx.Response(200, json={"status": "error", "message": "no"})}
    )
    with pytest.raises(WhatsAppError, match="no"):
        await gupshup(recorder).send_text("+15557654321", "hi")


# -- credentials --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("credentials", "problem"),
    [
        (WhatsAppCredentials("twilio", "15550001111", SecretStr(AUTH), account_sid=SID), "+1555"),
        (WhatsAppCredentials("twilio", NUMBER, SecretStr(AUTH), account_sid="AC1"), "SID"),
        (WhatsAppCredentials("twilio", NUMBER, SecretStr("x"), account_sid=SID), "auth token"),
        (
            WhatsAppCredentials("meta", NUMBER, SecretStr("EAA" + "x" * 30), phone_number_id="1"),
            "phone number ID",
        ),
        (
            WhatsAppCredentials(
                "meta", NUMBER, SecretStr("EAA" + "x" * 30), phone_number_id="123456"
            ),
            "app secret",
        ),
        (WhatsAppCredentials("gupshup", NUMBER, SecretStr("k" * 20), app_id="x"), "app ID"),
    ],
)
def test_wrong_credentials_have_a_problem(credentials: WhatsAppCredentials, problem: str) -> None:
    assert problem in (credentials.problem() or "")


def test_good_credentials_build_their_provider() -> None:
    twilio_credentials = WhatsAppCredentials("twilio", NUMBER, SecretStr(AUTH), account_sid=SID)
    meta_credentials = WhatsAppCredentials(
        "meta",
        NUMBER,
        SecretStr("EAA" + "x" * 30),
        app_secret=SecretStr(APP_SECRET),
        phone_number_id="123456",
    )
    gupshup_credentials = WhatsAppCredentials(
        "gupshup", NUMBER, SecretStr("k" * 32), app_id=APP_ID, app_name="samaapp"
    )
    for credentials, kind in (
        (twilio_credentials, TwilioProvider),
        (meta_credentials, MetaProvider),
        (gupshup_credentials, GupshupProvider),
    ):
        assert credentials.problem() is None
        assert isinstance(build_provider(credentials), kind)
