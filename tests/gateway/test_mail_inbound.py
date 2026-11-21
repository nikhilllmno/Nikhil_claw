"""Unit tests of inbound email and the signed links (docs/architecture.md, Section 11.9)."""

from __future__ import annotations

import json
import uuid

import pytest

from sama.gateway.links import (
    approval_token,
    media_signature,
    media_valid,
    read_approval_token,
)
from sama.gateway.mail_inbound import (
    BadKey,
    MailIn,
    addressed_to,
    authenticated,
    parse,
    public_key,
    reply_text,
    signed,
    thread_of,
)
from sama.gateway.sendgrid_client import Attachment, Mail
from tests.gateway.mail_support import (
    CONTENT_TYPE,
    form,
    key_pair,
    parse_fields,
    raw_email,
    sign,
)
from tests.media.support import PNG

NOW = 1_800_000_000


def test_the_signature_of_the_parse_webhook_is_checked() -> None:
    private, public = key_pair()
    key = public_key(public)
    body = form(parse_fields())
    headers = sign(private, NOW, body)
    signature = headers["X-Twilio-Email-Event-Webhook-Signature"]
    assert signed(key, signature, str(NOW), body, now=NOW + 5)
    assert not signed(key, signature, str(NOW), body + b"x", now=NOW)
    assert not signed(key, signature, str(NOW + 1), body, now=NOW)
    assert not signed(key, signature, str(NOW), body, now=NOW + 3600), "too old"
    assert not signed(key, None, str(NOW), body, now=NOW)
    assert not signed(key, "not base64!", str(NOW), body, now=NOW)
    _, other = key_pair()
    assert not signed(public_key(other), signature, str(NOW), body, now=NOW)


def test_a_wrong_public_key_is_refused() -> None:
    with pytest.raises(BadKey):
        public_key("not a key")
    with pytest.raises(BadKey):
        public_key("aGVsbG8=")


def test_the_default_form_is_parsed() -> None:
    body = form(
        parse_fields(in_reply_to="<sama.0123456789abcdef.aa@mail.sama.test>"),
        {"attachment1": ("cat.png", "image/png", PNG)},
    )
    mail = parse(body, CONTENT_TYPE)
    assert mail is not None
    assert (mail.sender, mail.subject, mail.text, mail.message_id) == (
        "mia@example.com",
        "Hello",
        "hi",
        "<m1@example.com>",
    )
    assert mail.recipients == ("sama@mail.sama.test",)
    assert mail.references == ("<sama.0123456789abcdef.aa@mail.sama.test>",)
    assert [(f.name, f.mime_type, f.data) for f in mail.files] == [("cat.png", "image/png", PNG)]
    assert authenticated(mail)


def test_the_raw_form_is_parsed() -> None:
    raw = raw_email(
        sender="Mia <mia@example.com>",
        to="sama+t0123456789abcdef@mail.sama.test",
        subject="Cat",
        text="what is this",
        image=PNG,
    )
    fields = {
        "email": raw.decode(),
        "envelope": json.dumps({"to": ["sama+t0123456789abcdef@mail.sama.test"]}),
        "dkim": "{@example.com : pass}",
        "SPF": "fail",
    }
    mail = parse(form(fields), CONTENT_TYPE)
    assert mail is not None
    assert (mail.sender, mail.subject, mail.text.strip()) == (
        "mia@example.com",
        "Cat",
        "what is this",
    )
    assert mail.files[0].name == "cat.png"
    assert mail.files[0].data == PNG
    assert addressed_to(mail, "sama@mail.sama.test") == "0123456789abcdef"


def _mail(**changes: object) -> MailIn:
    base = parse(form(parse_fields()), CONTENT_TYPE)
    assert base is not None
    return MailIn(**{**{f: getattr(base, f) for f in base.__dataclass_fields__}, **changes})


def test_a_sender_must_pass_dkim_or_aligned_spf() -> None:
    assert authenticated(_mail(dkim="{@example.com : pass}", spf="fail"))
    assert authenticated(_mail(dkim="{@mail.example.com : pass}", spf="none")) is False
    assert authenticated(_mail(sender="mia@eu.example.com", dkim="{@example.com : pass}"))
    assert not authenticated(_mail(dkim="{@evil.test : pass}", spf="fail"))
    assert authenticated(_mail(dkim="", spf="pass", envelope_from="bounce@example.com"))
    assert not authenticated(_mail(dkim="", spf="pass", envelope_from="x@evil.test"))
    assert not authenticated(_mail(dkim="{@example.com : fail}", spf="softfail"))


def test_the_address_and_the_thread_of_a_mail() -> None:
    address = "sama@mail.sama.test"
    assert addressed_to(_mail(recipients=(address,)), address) == ""
    assert addressed_to(_mail(recipients=("other@mail.sama.test",)), address) is None
    assert addressed_to(_mail(recipients=("SAMA+tbad@mail.sama.test",)), address) == ""
    reference = "<sama.00000000000000aa.1@mail.sama.test>"
    assert thread_of(_mail(references=(reference,)), "") == "00000000000000aa"
    assert thread_of(_mail(references=("<x@y>",)), "") is None
    assert thread_of(_mail(), "ffffffffffffffff") == "ffffffffffffffff"


def test_the_quoted_text_of_a_reply_is_cut() -> None:
    text = "Yes, do it.\nThanks\n\nOn Mon, 1 Jan 2026 at 10:00, sama <s@x>\nwrote:\n> old"
    assert reply_text(text) == "Yes, do it.\nThanks"
    assert reply_text("New text\n> quoted\nmore") == "New text"
    assert reply_text("Body\n-----Original Message-----\nold") == "Body"
    assert reply_text("Body\n--\nMia") == "Body"
    assert reply_text("Only text") == "Only text"


def test_approval_links_are_signed_and_expire() -> None:
    approval, user = uuid.uuid4(), uuid.uuid4()
    token = approval_token("secret", approval, "y", user, now=NOW)
    link = read_approval_token("secret", token, now=NOW + 10)
    assert link is not None
    assert (link.approval_id, link.choice, link.user_id) == (
        approval,
        "y",
        user,
    )
    assert read_approval_token("other", token, now=NOW) is None
    assert read_approval_token("secret", token, now=NOW + 25 * 3600) is None
    head, _, mac = token.partition(".")
    assert read_approval_token("secret", f"{head}x.{mac}", now=NOW) is None
    assert read_approval_token("secret", "garbage", now=NOW) is None


def test_media_links_are_signed_and_expire() -> None:
    integration, media = uuid.uuid4(), uuid.uuid4()
    signature = media_signature("s", integration, media, NOW + 60)
    assert media_valid("s", integration, media, NOW + 60, signature, now=NOW)
    assert not media_valid("s", integration, media, NOW + 60, signature, now=NOW + 61)
    assert not media_valid("s", integration, uuid.uuid4(), NOW + 60, signature, now=NOW)


def test_a_mail_body_for_sendgrid() -> None:
    body = Mail(
        sender="sama@mail.sama.test",
        sender_name="sama",
        to="mia@example.com",
        subject="Re: Hello",
        text="hi",
        reply_to="sama+t1@mail.sama.test",
        headers={"In-Reply-To": "<m1@example.com>"},
        attachments=(Attachment("a.png", "image/png", b"png"),),
    ).body()
    assert body["personalizations"] == [{"to": [{"email": "mia@example.com"}]}]
    assert body["reply_to"] == {"email": "sama+t1@mail.sama.test"}
    assert body["attachments"][0]["content"] == "cG5n"


def test_capability_urls_are_redacted_in_logs() -> None:
    from sama.vault.redact import redact

    integration = uuid.uuid4()
    lines = (
        f"POST /whatsapp/{integration}/webhook/tok_SECRET-1 HTTP/1.1",
        f"GET /email/{integration}/approvals/abc.SECRET_2 HTTP/1.1",
        f"GET /whatsapp/{integration}/media/{uuid.uuid4()}?e=1&s=SECRET_3 HTTP/1.1",
        "GET /x?hub.mode=subscribe&hub.verify_token=SECRET_4",
    )
    for line in lines:
        assert "SECRET" not in redact(line)
