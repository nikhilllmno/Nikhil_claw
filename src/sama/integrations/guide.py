"""The setup guide of the chat channels for the agent (docs/architecture.md, Sections 10.3 and
11.9).

A chat channel is the place where people talk to sama (a Slack app, a Telegram bot, a WhatsApp
number, an email address). It is not a connection to the account of a user. Since M8.1 the
channel names are ``slack_app``, ``telegram``, ``whatsapp``, ``whatsapp_meta``,
``whatsapp_twilio``, ``whatsapp_gupshup``, and ``email_channel``: ``slack`` and ``email`` mean
the Slack and the inbox connectors of the user (Composio), because that is what a user means with
"connect my Slack" or "connect my email".

``connection_request`` with a channel name gives the agent the steps and a link to the Settings
page of the web app. An org owner or admin gets the steps. A member gets the names of the admins
to ask. The secrets go only into the web app, which stores them in the vault. The agent never
asks for them in the chat.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import urlencode

NEVER_IN_CHAT: Final = (
    "Do not ask for tokens, keys, or secrets in the chat: the admin pastes them only in the web"
    " app."
)

_WHATSAPP_CHOICE: Final = (
    "sama connects WhatsApp through one of three providers. Ask the user which one the org uses,"
    " then call connection_request again with whatsapp_meta, whatsapp_twilio, or"
    " whatsapp_gupshup:\n"
    "- Meta WhatsApp Cloud API: the number is in a Meta app of the business.\n"
    "- Twilio: the number (or the WhatsApp sandbox) is in a Twilio account.\n"
    "- Gupshup: the number is in a Gupshup WhatsApp app."
)

_STEPS: Final[dict[str, tuple[str, list[str]]]] = {
    "whatsapp_meta": (
        "WhatsApp through the Meta WhatsApp Cloud API",
        [
            "In developers.facebook.com, open the app of the business. Under WhatsApp > API"
            " Setup, copy the phone number ID and the number.",
            "Make a permanent access token: Business settings > System users > add a user with"
            " the whatsapp_business_messaging permission > Generate token.",
            "Copy the app secret from App settings > Basic.",
            "Paste the number, the phone number ID, the token, and the app secret on the page"
            " below, with the provider Meta.",
            "sama then shows a callback URL and a verify token. Enter both under WhatsApp >"
            " Configuration > Webhook, click Verify, and subscribe the field messages.",
        ],
    ),
    "whatsapp_twilio": (
        "WhatsApp through Twilio",
        [
            "In console.twilio.com, copy the Account SID and the Auth Token.",
            "Copy the WhatsApp number of the account (Messaging > Senders > WhatsApp senders),"
            " or the number of the WhatsApp sandbox for a test.",
            "Paste the number, the Account SID, and the Auth Token on the page below, with the"
            " provider Twilio.",
            "sama then shows a webhook URL. Set it as 'When a message comes in' (POST) of the"
            " WhatsApp sender or of the sandbox.",
        ],
    ),
    "whatsapp_gupshup": (
        "WhatsApp through Gupshup",
        [
            "In the Gupshup dashboard, open the WhatsApp app. Copy its app ID, its app name, and"
            " its number.",
            "Copy the API key of the account (the profile menu > API key).",
            "Paste the number, the app ID, the app name, and the API key on the page below, with"
            " the provider Gupshup.",
            "sama adds the webhook subscription of the app itself. No other step is necessary.",
        ],
    ),
    "email_channel": (
        "an email address for sama through SendGrid (people email sama; this is not the inbox"
        " of the user: for that, connect gmail or outlook)",
        [
            "Pick an inbound address on a subdomain that receives no other mail, for example"
            " sama@mail.example.com.",
            "Add an MX record for the subdomain that points to mx.sendgrid.net (priority 10).",
            "In SendGrid, authenticate the domain (Settings > Sender Authentication).",
            "Make a SendGrid API key with the Mail Send permission.",
            "Make a security policy with signature verification for the Inbound Parse webhook"
            " (the API POST /v3/user/webhooks/security/policies) and copy its public key.",
            "Paste the address, the API key, and the public key on the page below. sama then"
            " shows the Inbound Parse URL: add it under Settings > Inbound Parse with the policy.",
        ],
    ),
    "telegram": (
        "Telegram",
        [
            "In Telegram, talk to @BotFather and send /newbot. Copy the token of the bot.",
            "Paste the token on the page below. sama registers its webhook itself.",
        ],
    ),
    "slack_app": (
        "the sama Slack app (people talk to sama in Slack; to read or send Slack messages as the"
        " user, connect the slack connector instead)",
        [
            "On the page below, click Start Slack setup and open the link to make the Slack app"
            " from the manifest. Install the app.",
            "Paste the bot token and the signing secret of the app on the page.",
        ],
    ),
}

CHANNEL_NAMES: Final = frozenset({"whatsapp", *_STEPS})
_ALIASES: Final = {
    "slack_bot": "slack_app",
    "slack_channel": "slack_app",
    "sama_slack_app": "slack_app",
    "email_address": "email_channel",
    "mail_channel": "email_channel",
    "telegram_bot": "telegram",
}
_PAGE_KIND: Final = {"slack_app": "slack", "email_channel": "email"}


def channel_name(connector: str) -> str | None:
    """The channel name of ``connector``, or ``None`` when it is not a chat channel."""
    name = "_".join(connector.strip().lower().replace("-", "_").split())
    name = _ALIASES.get(name, name)
    return name if name in CHANNEL_NAMES else None


def channel_guide(
    connector: str,
    web_base: str | None,
    *,
    admin: bool = True,
    admins: tuple[str, ...] = (),
) -> str | None:
    """The guide for a channel name, or ``None`` when ``connector`` is not a channel. ``admin``
    is true for an org owner or admin, who can set the channel up. A member gets ``admins``,
    the names of the people who can."""
    name = channel_name(connector)
    if name is None:
        return None
    if name == "whatsapp":
        return _WHATSAPP_CHOICE
    title, steps = _STEPS[name]
    kind, _, provider = name.partition("_")
    kind = _PAGE_KIND.get(name, kind)
    provider = "" if name in _PAGE_KIND else provider
    query = {"connect": kind, **({"provider": provider} if provider else {})}
    link = f"{web_base.rstrip('/')}/settings?{urlencode(query)}" if web_base else None
    if not admin:
        who = ", ".join(admins) if admins else "an org owner or admin"
        return (
            f"The user cannot set up {title}: only an org owner or admin can. Tell the user to"
            f" ask {who}. sama recorded the request in the audit log."
            + (f" The admin opens: {link}" if link else "")
        )
    lines = [
        f"The user is an org owner or admin, so the user can set up {title} now. Guide the user"
        " step by step:",
        *(f"{number}. {step}" for number, step in enumerate(steps, start=1)),
    ]
    if link is not None:
        lines.append(f"The page: {link}")
    else:
        lines.append("The page: Settings > Integrations in the sama web app.")
    lines += [
        "After the connect, each user makes a pairing code on the same page and sends it to the"
        " number or the address, to link their account.",
        NEVER_IN_CHAT,
    ]
    return "\n".join(lines)
