"""The store of ``plan_propose`` (``sama.tools.plan``, Section 15.5)."""

from __future__ import annotations

import uuid

from sama.coding_mode.service import CodingError, CodingService
from sama.db.models import MAX_PLAN_CHARS
from sama.tenancy.run_token import RunTokenClaims
from sama.tools.plan import PlanError


class PlanTool:
    def __init__(self, coding: CodingService) -> None:
        self._coding = coding

    async def propose(self, claims: RunTokenClaims, plan: str, title: str | None) -> str:
        body = plan.strip()
        if title and title.strip():
            body = f"# {title.strip()}\n\n{body}"
        if len(body) > MAX_PLAN_CHARS:
            raise PlanError(f"the plan has more than {MAX_PLAN_CHARS} characters; make it shorter")
        try:
            stored = await self._coding.propose(
                org_id=uuid.UUID(claims.org_id),
                session_id=uuid.UUID(claims.session_id),
                run_id=uuid.UUID(claims.run_id),
                body=body,
            )
        except CodingError as error:
            raise PlanError(str(error)) from None
        return (
            f"The plan is saved as version {stored.version}, and the user sees it. Plan mode is"
            " on. End your answer now: give a short summary of the plan, and tell the user to"
            " approve it (/plan approve), edit it (/plan edit NEW PLAN), or reject it with"
            " feedback (/plan reject FEEDBACK), or to use the Code page of the web app. Do not"
            " change files before the approval."
        )
