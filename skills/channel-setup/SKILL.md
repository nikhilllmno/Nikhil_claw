---
name: channel-setup
description: Connect WhatsApp, email, Telegram, or Slack step by step
version: 1.0.0
tags: [setup, channels, whatsapp, email]
requires_toolsets: [connections]
---

# Channel setup

Use this skill when the user wants to talk to sama in WhatsApp, by email, in Telegram, or in Slack, or asks how to connect one of these channels.

## Steps

1. Find the channel. For WhatsApp, ask which provider the org uses: Meta WhatsApp Cloud API, Twilio, or Gupshup. If the user does not know, ask where the business number is managed.
2. Call `connection_request` with the channel: `whatsapp_meta`, `whatsapp_twilio`, `whatsapp_gupshup`, `email`, `telegram`, or `slack`. The answer has the steps and the link to the Settings page.
3. Give the steps to the user one at a time. Wait until the user finishes a step before you give the next one. Answer questions about the provider console when the user is stuck.
4. Tell the user that an org owner or admin connects the channel on the Settings page. A user who is not an admin asks an admin.
5. After the connect, the Settings page shows the steps that are left at the provider, such as a webhook URL, a verify token, or an MX record. Help the user with them.
6. Tell the user to make a pairing code on the Settings page and send it to the number or the address. Then the user can talk to sama there.

## Rules

- Never ask for a token, an API key, an auth token, an app secret, or a password in the chat. The admin pastes them only on the Settings page, and sama keeps them in the vault.
- If the user pastes a secret in the chat, tell the user to rotate it at the provider and to paste the new value only on the Settings page.
- A WhatsApp business number can send a message first only with a template that Meta approved. Replies within 24 hours of a user message need no template.
- Email needs DNS changes (an MX record and a domain authentication). Tell the user who can change the DNS of the domain.
