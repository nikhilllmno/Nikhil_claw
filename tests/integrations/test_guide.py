"""Tests of the channel setup guide for the agent and the builtin skill (Section 11.9)."""

from __future__ import annotations

from pathlib import Path

from sama.integrations.guide import NEVER_IN_CHAT, channel_guide
from sama.skills.builtin import load

ROOT = Path(__file__).resolve().parents[2]


def test_whatsapp_asks_for_the_provider_first() -> None:
    text = channel_guide("WhatsApp", "https://app.sama.test")
    assert text is not None
    for name in ("whatsapp_meta", "whatsapp_twilio", "whatsapp_gupshup"):
        assert name in text


def test_each_provider_has_steps_and_a_link_and_never_asks_for_secrets() -> None:
    for name, provider in (
        ("whatsapp_meta", "meta"),
        ("whatsapp-twilio", "twilio"),
        ("whatsapp_gupshup", "gupshup"),
    ):
        text = channel_guide(name, "https://app.sama.test/")
        assert text is not None
        assert f"https://app.sama.test/settings?connect=whatsapp&provider={provider}" in text
        assert "1. " in text
        assert NEVER_IN_CHAT in text
    email = channel_guide("email", None)
    assert email is not None
    assert "mx.sendgrid.net" in email
    assert "Settings > Integrations" in email
    assert channel_guide("github", "https://app.sama.test") is None


def test_the_channel_setup_skill_loads() -> None:
    skills = load(ROOT / "skills")
    skill = skills["channel-setup"]
    assert skill.requires_toolsets == ("connections",)
    assert "connection_request" in skill.skill_md
    assert "Never ask for a token" in skill.skill_md
