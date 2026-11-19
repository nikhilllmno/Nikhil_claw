"""Tests of the code copy of the RLS visibility rules (docs/architecture.md, Section 17.1)."""

from __future__ import annotations

import uuid

from sama.tenancy.access import Grants

USER, WS, OTHER = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def test_a_workspace_member_sees_the_workspace_and_shared_sessions() -> None:
    grants = Grants(USER, ((None, "member"), (WS, "member")))
    assert grants.org_member
    assert not grants.org_admin
    assert grants.sees_workspace(WS)
    assert not grants.sees_workspace(OTHER)
    assert grants.sees_session(WS, "shared", None)
    assert grants.sees_session(WS, "private", USER)
    assert not grants.sees_session(WS, "private", uuid.uuid4())
    assert not grants.sees_session(OTHER, "shared", None)
    assert not grants.sees_session(WS, "public", None)


def test_a_workspace_row_without_the_org_membership_sees_nothing() -> None:
    grants = Grants(USER, ((WS, "member"),))
    assert not grants.org_member
    assert not grants.sees_workspace(WS)


def test_an_org_admin_sees_every_workspace_but_not_private_sessions_of_others() -> None:
    grants = Grants(USER, ((None, "admin"),))
    assert grants.org_admin
    assert grants.sees_workspace(OTHER)
    assert grants.sees_session(OTHER, "shared", None)
    assert not grants.sees_session(OTHER, "private", uuid.uuid4())


def test_no_rows_sees_nothing() -> None:
    grants = Grants(USER, ())
    assert not grants.org_member
    assert not grants.org_admin
    assert not grants.sees_workspace(WS)
