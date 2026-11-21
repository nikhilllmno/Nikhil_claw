"""The setup guide of the chat channels for the agent (docs/architecture.md, Section 11.9).

``connection_request`` with a channel name (``whatsapp``, ``whatsapp_meta``,
``whatsapp_twilio``, ``whatsapp_gupshup``, ``email``, ``telegram``, or ``slack``) gives the agent
the steps for a person and a link to the Settings page of the web app. The secrets go only into
the web app, which stores them in the vault. The agent never asks for them in the chat.
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
    "email": (
        "email through SendGrid",
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
    "slack": (
        "Slack",
        [
            "On the page below, click Start Slack setup and open the link to make the Slack app"
            " from the manifest. Install the app.",
            "Paste the bot token and the signing secret of the app on the page.",
        ],
    ),
}

CHANNEL_NAMES: Final = frozenset({"whatsapp", *_STEPS})


def channel_guide(connector: str, web_base: str | None) -> str | None:
    """The guide for a channel name, or ``None`` when ``connector`` is not a channel."""
    name = connector.strip().lower().replace("-", "_")
    if name not in CHANNEL_NAMES:
        return None
    if name == "whatsapp":
        return _WHATSAPP_CHOICE
    title, steps = _STEPS[name]
    kind, _, provider = name.partition("_")
    query = {"connect": kind, **({"provider": provider} if provider else {})}
    link = f"{web_base.rstrip('/')}/settings?{urlencode(query)}" if web_base else None
    lines = [
        f"Guide the user step by step to connect {title}. An org owner or admin does the connect.",
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
