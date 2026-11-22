---
name: channel-setup
description: Connect WhatsApp, email, Telegram, or Slack step by step
version: 1.1.0
tags: [setup, channels, whatsapp, email]
requires_toolsets: [connections]
---

# Channel setup

Use this skill when the user wants to talk to sama in WhatsApp, by email, in Telegram, or in Slack, or asks how to set up one of these channels for sama.

A channel is not the account of the user. "Connect my Slack" (read and send Slack messages as the user) is the `slack` connector, and "connect my email" or "my inbox" is the `gmail` or `outlook` connector: call `connection_request` with that connector, not with a channel. Use a channel only when the user wants to talk to sama in that app.

## Steps

1. Find the channel. For WhatsApp, ask which provider the org uses: Meta WhatsApp Cloud API, Twilio, or Gupshup. If the user does not know, ask where the business number is managed.
2. Call `connection_request` with the channel: `whatsapp_meta`, `whatsapp_twilio`, `whatsapp_gupshup`, `email_channel`, `telegram`, or `slack_app`. The answer has the steps and the link to the Settings page, or, for a user who is not an org owner or admin, the admins to ask.
3. Give the steps to the user one at a time. Wait until the user finishes a step before you give the next one. Answer questions about the provider console when the user is stuck.
4. The answer says whether the user can set up the channel. An org owner or admin does it now. A member asks one of the admins that the answer names.
5. After the connect, the Settings page shows the steps that are left at the provider, such as a webhook URL, a verify token, or an MX record. Help the user with them.
6. Tell the user to make a pairing code on the Settings page and send it to the number or the address. Then the user can talk to sama there.

## Rules

- Never ask for a token, an API key, an auth token, an app secret, or a password in the chat. The admin pastes them only on the Settings page, and sama keeps them in the vault.
- If the user pastes a secret in the chat, tell the user to rotate it at the provider and to paste the new value only on the Settings page.
- A WhatsApp business number can send a message first only with a template that Meta approved. Replies within 24 hours of a user message need no template.
- Email needs DNS changes (an MX record and a domain authentication). Tell the user who can change the DNS of the domain.
