"""Who may decide an approval in a shared session (docs/architecture.md, Section 11.3)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from sama.gate.approvers import Approvers, approvers_of, may_decide_shared
from sama.gateway.sessions import ShownApproval, answerers_text, approval_text
from sama.tenancy.access import Grants

WS = uuid.uuid4()
OTHER_WS = uuid.uuid4()
ME = uuid.uuid4()
ASKER = uuid.uuid4()
FRIEND = uuid.uuid4()


def grants(*rows: tuple[uuid.UUID | None, str], user: uuid.UUID = ME) -> Grants:
    return Grants(user, rows)


MEMBER = grants((None, "member"), (WS, "member"))


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (None, Approvers()),
        ({}, Approvers()),
        ("*", Approvers()),
        ({"approvers": "*"}, Approvers()),
        ({"approvers": {"x": 1}}, Approvers()),
        ({"approvers": []}, Approvers()),
        ({"approvers": ["*"]}, Approvers(any_member=True)),
        ({"approvers": [str(FRIEND)]}, Approvers(users=frozenset({FRIEND}))),
        (
            {"approvers": ["*", str(FRIEND), "bob", 7, None]},
            Approvers(any_member=True, users=frozenset({FRIEND})),
        ),
        ({"approvers": ["bob", 7]}, Approvers()),
    ],
)
def test_the_approvers_are_read_from_the_policy(policy: Any, expected: Approvers) -> None:
    assert approvers_of(policy) == expected


def test_the_requester_and_the_admins_may_decide_by_default() -> None:
    nobody = Approvers()
    assert may_decide_shared(MEMBER, WS, requester=ME, approvers=nobody)
    assert not may_decide_shared(MEMBER, WS, requester=ASKER, approvers=nobody)
    assert not may_decide_shared(MEMBER, WS, requester=None, approvers=nobody)
    assert may_decide_shared(
        grants((None, "member"), (WS, "admin")), WS, requester=ASKER, approvers=nobody
    )
    assert may_decide_shared(
        grants((None, "member"), (WS, "owner")), WS, requester=ASKER, approvers=nobody
    )
    assert may_decide_shared(grants((None, "admin")), WS, requester=ASKER, approvers=nobody)
    assert may_decide_shared(grants((None, "owner")), WS, requester=None, approvers=nobody)


def test_an_admin_of_another_workspace_may_not_decide() -> None:
    other = grants((None, "member"), (OTHER_WS, "admin"), (WS, "member"))
    assert not may_decide_shared(other, WS, requester=ASKER, approvers=Approvers())


def test_a_listed_user_and_the_star_widen_the_set() -> None:
    listed = Approvers(users=frozenset({ME}))
    assert may_decide_shared(MEMBER, WS, requester=ASKER, approvers=listed)
    assert not may_decide_shared(
        grants((None, "member"), (WS, "member"), user=FRIEND),
        WS,
        requester=ASKER,
        approvers=listed,
    )
    assert may_decide_shared(MEMBER, WS, requester=ASKER, approvers=Approvers(any_member=True))


def test_the_card_tells_who_may_decide() -> None:
    assert (
        answerers_text("Mia", Approvers())
        == "Mia, who asked, or a workspace admin can decide this."
    )
    assert answerers_text(None, Approvers()) == "A workspace admin can decide this."
    assert answerers_text("  ", Approvers()) == "A workspace admin can decide this."
    assert (
        answerers_text("Mia", Approvers(any_member=True))
        == "Each member of the workspace can decide this."
    )
    long = answerers_text("N" * 200 + "\nx", Approvers())
    assert "\n" not in long
    assert long.startswith("N" * 60 + ", who asked")


def test_the_text_of_a_card_has_the_line_of_the_approvers_only_when_there_is_one() -> None:
    line = "Mia, who asked, or a workspace admin can decide this."
    shared = ShownApproval(uuid.uuid4(), "Bash", "deletes files", {"command": "rm"}, line)
    private = ShownApproval(uuid.uuid4(), "Bash", "deletes files", {"command": "rm"})
    assert approval_text(shared).splitlines()[:3] == [
        "Approval needed: Bash",
        "deletes files",
        line,
    ]
    assert line not in approval_text(private)
    assert approval_text(private).startswith("Approval needed: Bash\ndeletes files\n\n")
