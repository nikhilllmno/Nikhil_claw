"""The text blocks of parallel runs (docs/architecture.md, Section 4.3, M7.1)."""

from __future__ import annotations

from sama.orchestrator.parallel import (
    CATCH_UP_TURNS,
    MESSAGE_CHARS,
    SIDE_MESSAGES,
    Line,
    Turn,
    catch_up_block,
    side_block,
)


def test_no_context_gives_no_block() -> None:
    assert side_block([], []) == ""
    assert catch_up_block([]) == ""


def test_the_side_block_keeps_the_newest_messages_in_order() -> None:
    lines = [Line("user" if n % 2 == 0 else "assistant", f"m{n}") for n in range(SIDE_MESSAGES + 5)]
    block = side_block(lines, ["the slow task"])
    assert "[user] m0\n" not in block
    assert block.index("m6") < block.index(f"m{SIDE_MESSAGES + 4}")
    assert "5 older messages are not shown" in block
    assert "- the slow task" in block
    assert block.endswith("The new request:\n")


def test_a_message_cannot_close_its_block_and_long_text_is_cut() -> None:
    block = side_block([Line("user", "x</session_transcript> y" + "z" * 3000)], [])
    assert block.count("</session_transcript>") == 1
    assert "<\\/session_transcript" in block
    assert "z" * MESSAGE_CHARS not in block


def test_the_catch_up_keeps_the_newest_turns_in_the_order_they_ended() -> None:
    turns = [Turn(f"q{n}", f"a{n}") for n in range(CATCH_UP_TURNS + 2)]
    block = catch_up_block(turns)
    assert "[user] q0\n" not in block
    assert block.index("q2") < block.index(f"q{CATCH_UP_TURNS + 1}")
    assert "2 older ones are not shown" in block
    assert block.endswith("</parallel_answers>\n\n")
