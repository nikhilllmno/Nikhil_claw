"""The rules of session search that need no database (Section 7.1)."""

from __future__ import annotations

import uuid

from sama.search.core import (
    RRF_K,
    SNIPPETS_PER_SESSION,
    Candidate,
    clip,
    fuse,
    pick,
    vector_literal,
)


def candidate(session: uuid.UUID | None = None) -> Candidate:
    return Candidate(uuid.uuid4(), session or uuid.uuid4())


def test_fusion_keeps_the_order_of_one_list() -> None:
    first, second, third = candidate(), candidate(), candidate()
    fused = fuse([[first, second, third]])
    assert [c for c, _ in fused] == [first, second, third]
    assert fused[0][1] == 1 / (RRF_K + 1)


def test_a_message_in_both_lists_comes_first() -> None:
    both, only_text, only_meaning = candidate(), candidate(), candidate()
    fused = fuse([[only_text, both], [only_meaning, both]])
    assert fused[0][0] == both
    assert fused[0][1] == 2 / (RRF_K + 2)
    # The two messages with one list and rank 1 have the same score. The id breaks the tie.
    assert {c for c, _ in fused[1:]} == {only_text, only_meaning}


def test_fusion_of_no_lists_is_empty() -> None:
    assert fuse([]) == []
    assert fuse([[], []]) == []


def test_a_session_ranks_as_its_best_message_and_keeps_two_snippets() -> None:
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ranked = [candidate(a), candidate(b), candidate(a), candidate(a), candidate(c)]
    chosen = pick(fuse([ranked]), limit=2)
    assert [session for session, _ in chosen] == [a, b]
    assert [len(ids) for _, ids in chosen] == [SNIPPETS_PER_SESSION, 1]
    assert chosen[0][1] == [ranked[0].message_id, ranked[2].message_id]


def test_vector_literal_is_the_text_form_of_pgvector() -> None:
    assert vector_literal([1, 0.5, -2.25]) == "[1.0,0.5,-2.25]"


def test_clip_marks_a_cut() -> None:
    assert clip("short", 10) == "short"
    assert clip("a" * 20, 10) == "a" * 10 + " [cut]"
