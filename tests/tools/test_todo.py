"""The ``todo`` tool logic (docs/architecture.md, Section 16.7)."""

from __future__ import annotations

import asyncio

import pytest

from sama.tools.todo import (
    MAX_ITEMS,
    MemoryTodoStore,
    TodoChange,
    TodoError,
    TodoItem,
    TodoStatus,
    apply,
    render,
)
from tests.support import make_claims


def item(id: str, content: str, status: TodoStatus = TodoStatus.PENDING) -> TodoItem:
    return TodoItem(id=id, content=content, status=status)


def test_replace() -> None:
    current = [item("old", "Old")]
    new = apply(current, [TodoChange(id="1", content="One")], merge=False)
    assert new == [item("1", "One")]


def test_replace_needs_content() -> None:
    with pytest.raises(TodoError, match="needs content"):
        apply([], [TodoChange(id="1", status=TodoStatus.COMPLETED)], merge=False)


def test_merge_updates_by_id_and_appends() -> None:
    current = [item("1", "One"), item("2", "Two")]
    new = apply(
        current,
        [
            TodoChange(id="2", status=TodoStatus.IN_PROGRESS),
            TodoChange(id="1", content="One again"),
            TodoChange(id="3", content="Three"),
        ],
        merge=True,
    )
    assert new == [
        item("1", "One again"),
        item("2", "Two", TodoStatus.IN_PROGRESS),
        item("3", "Three"),
    ]


def test_merge_of_a_new_item_needs_content() -> None:
    with pytest.raises(TodoError, match="is new"):
        apply([], [TodoChange(id="9", status=TodoStatus.PENDING)], merge=True)


def test_duplicate_ids_are_refused() -> None:
    with pytest.raises(TodoError, match="one time only"):
        apply([], [TodoChange(id="1", content="a"), TodoChange(id="1", content="b")], merge=False)


def test_the_item_limit() -> None:
    current = [item(str(n), "x") for n in range(MAX_ITEMS)]
    with pytest.raises(TodoError, match=f"at most {MAX_ITEMS}"):
        apply(current, [TodoChange(id="new", content="x")], merge=True)


def test_render() -> None:
    assert render([]) == "The task list is empty."
    text = render(
        [
            item("1", "Plan", TodoStatus.COMPLETED),
            item("2", "Build", TodoStatus.IN_PROGRESS),
            item("3", "Test"),
            item("4", "Skip", TodoStatus.CANCELLED),
        ]
    )
    assert text.splitlines() == [
        "Task list (4 items: 1 pending, 1 in progress, 1 completed, 1 cancelled):",
        "[x] 1: Plan",
        "[>] 2: Build",
        "[ ] 3: Test",
        "[-] 4: Skip",
    ]


async def test_memory_store_is_scoped_by_session() -> None:
    store = MemoryTodoStore()
    await store.change(make_claims(), lambda items: [*items, item("1", "a")])
    assert await store.read(make_claims()) == [item("1", "a")]
    assert await store.read(make_claims(session="sess-2")) == []


async def test_memory_store_changes_are_atomic() -> None:
    store = MemoryTodoStore()

    async def add(n: int) -> None:
        await store.change(make_claims(), lambda items: [*items, item(str(n), "x")])

    await asyncio.gather(*(add(n) for n in range(20)))
    assert len(await store.read(make_claims())) == 20


async def test_a_failed_change_keeps_the_list() -> None:
    store = MemoryTodoStore()
    await store.change(make_claims(), lambda items: [item("1", "a")])

    def bad(items: list[TodoItem]) -> list[TodoItem]:
        raise TodoError("no")

    with pytest.raises(TodoError):
        await store.change(make_claims(), bad)
    assert await store.read(make_claims()) == [item("1", "a")]
