"""The ``change_propose`` tool: the agent proposes a change of its own setup (docs/architecture.md,
Sections 9.4.2 and 10.2, M6.1).

The targets are:

- ``soul``: the full new soul (``sama.review.soul``, M3.2).
- ``profile``: the name, the runtime, the model, or the sub-agents of the profile.
- ``toolsets``: the full new list of toolsets, of group toolsets, or of both.
- ``policy``: the policy rules of the profile, one rule for each tool or prefix. The action
  ``none`` removes the rule of the profile.

A change of the setup changes the agent for every user of the profile, so it is always a
proposal, also when the user of the run is an admin. The roles are the same as for a soul: a
workspace admin or the org owner approves a change of a workspace profile, and only the org
owner approves a change of an org profile. The proposal keeps the values that the agent saw
(``before``) and the new values (``after``). The applier writes the change only when the
profile still has the ``before`` values (``sama.profile_changes.write_change``). The change
applies from the next session.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.profile_changes import TOOL_MAX_CHARS, check_rules, profile_rules
from sama.profiles import KIND_FIELDS, ProfileError, planned_values, profile_value
from sama.review.proposals import NewProposal, ProposalKind, ProposalScope, ProposalSink
from sama.review.soul import SUMMARY_MAX_CHARS, SoulError, SoulProposer, load_run
from sama.runtime.contract import RuntimeKind
from sama.subagents import LockedProfile, SubagentError
from sama.tenancy.authority import run_user
from sama.tenancy.run_token import RunTokenClaims
from sama.vault.redact import redact

ChangeTarget = Literal["soul", "profile", "toolsets", "policy"]
CHANGE_KINDS: Final = ("profile", "toolsets", "policy")
"""The kinds of proposals that ``sama.profile_changes.write_change`` applies."""
CHANGE_LABELS: Final = {"profile": "settings", "toolsets": "toolsets", "policy": "policy rules"}


class RuleChange(BaseModel):
    """One policy rule of a ``policy`` change, as the tool takes it."""

    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1, max_length=TOOL_MAX_CHARS)
    action: Literal["allow", "ask", "deny", "none"]


@dataclass(frozen=True, slots=True)
class ChangeRequest:
    """The arguments of ``change_propose``. A field that is ``None`` does not change."""

    target: ChangeTarget
    summary: str
    content: str | None = None
    name: str | None = None
    runtime: str | None = None
    model: str | None = None
    subagents: Sequence[Any] | None = None
    toolsets: Sequence[str] | None = None
    group_toolsets: Sequence[str] | None = None
    rules: Sequence[tuple[str, str]] | None = None
    """``(tool, action)`` pairs. The action ``none`` removes the rule."""

    def fields(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for field in ("name", "runtime", "model", "subagents", "toolsets", "group_toolsets"):
            value = getattr(self, field)
            if value is not None:
                out[field] = value if isinstance(value, str) else list(value)
        if self.rules is not None:
            out["rules"] = [list(pair) for pair in self.rules]
        return out


def _locked(run: Any) -> LockedProfile:
    """The profile columns of the run row. The row is not locked: the proposal only checks the
    values, and the applier checks them again under a lock."""
    return LockedProfile(
        name=run.name,
        soul=run.soul,
        runtime=RuntimeKind(run.runtime),
        model=run.model,
        toolsets=[str(t) for t in run.toolsets] if isinstance(run.toolsets, list) else [],
        subagents=run.subagents,
        workspace_id=run.profile_workspace_id,
        review_model=run.review_model,
        group_toolsets=(
            [str(t) for t in run.group_toolsets] if isinstance(run.group_toolsets, list) else []
        ),
    )


class ChangeProposer:
    """The body of ``change_propose``."""

    def __init__(self, engine: AsyncEngine, proposals: ProposalSink) -> None:
        self._engine = engine
        self._proposals = proposals
        self._souls = SoulProposer(engine, proposals)

    async def propose(self, claims: RunTokenClaims, request: ChangeRequest) -> str:
        """Stores the proposal and returns the text for the agent. Raises ``SoulError``."""
        given = request.fields()
        if request.target == "soul":
            if given:
                raise SoulError("the target soul takes only content and summary")
            if request.content is None:
                raise SoulError("give the full new soul in content")
            return await self._souls.propose(claims, request.content, request.summary)
        if request.content:
            raise SoulError(
                f"content is only for the target soul. Give the fields of {request.target}"
            )
        allowed = KIND_FIELDS[request.target]
        extra = sorted(set(given) - set(allowed))
        if extra:
            raise SoulError(
                f"the target {request.target} takes only "
                + ", ".join(allowed)
                + ", not "
                + ", ".join(extra)
            )
        if not given:
            raise SoulError(f"give at least one of {', '.join(allowed)}")
        summary = " ".join(request.summary.split())[:SUMMARY_MAX_CHARS]
        if not summary:
            raise SoulError("give a summary of the change")
        text = json.dumps(given, sort_keys=True, ensure_ascii=False)
        if redact(text) != text:
            raise SoulError("the change contains a secret. Remove it")
        run = await load_run(self._engine, claims)
        try:
            if request.target == "policy":
                before, after = await self._policy(claims, run, request.rules or ())
            else:
                before, after = _profile_change(_locked(run), request.target, given)
        except (ProfileError, SubagentError) as error:
            raise SoulError(str(error)) from None
        if not after:
            return "The profile already has these values. Nothing to propose."
        scope: ProposalScope = "workspace" if run.profile_workspace_id is not None else "org"
        kind: ProposalKind = request.target
        label = CHANGE_LABELS[request.target]
        await self._proposals.propose(
            NewProposal(
                org_id=uuid.UUID(claims.org_id),
                workspace_id=run.workspace_id,
                session_id=uuid.UUID(claims.session_id),
                run_id=uuid.UUID(claims.run_id),
                acting_user_id=run_user(run.triggered_by_user_id, run.audience, run.owner_user_id),
                kind=kind,
                scope=scope,
                payload={
                    "profile_id": str(run.agent_profile_id),
                    "profile_name": run.name,
                    "summary": summary,
                    "before": before,
                    "after": after,
                },
                summary=f"change the {label} of the agent profile {run.name}: {summary}",
                origin="chat",
            )
        )
        who = "A workspace admin or the org owner" if scope == "workspace" else "Only the org owner"
        return (
            f"Proposed a change of the {label} of the profile {run.name}. It is not saved. {who}"
            " must approve it first. An approval card shows the change. After the approval,"
            " the change applies from the next session. Do not propose it again."
        )

    async def _policy(
        self, claims: RunTokenClaims, run: Any, pairs: Sequence[tuple[str, str]]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        wanted = {tool.strip(): None if action == "none" else action for tool, action in pairs}
        if len(wanted) != len(pairs):
            raise ProfileError("a tool is in the rules two times")
        rules = check_rules(wanted)
        async with self._engine.connect() as connection:
            current = await profile_rules(
                connection, uuid.UUID(claims.org_id), run.agent_profile_id, list(rules)
            )
        changed = {tool: action for tool, action in rules.items() if current.get(tool) != action}
        if not changed:
            return {}, {}
        return {"rules": {tool: current.get(tool) for tool in changed}}, {"rules": changed}


def _profile_change(
    profile: LockedProfile, target: str, given: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The ``before`` and ``after`` values of a profile or toolsets change, checked against the
    profile now. A field without a change is left out."""
    values = planned_values(profile, given)
    changed = [field for field in KIND_FIELDS[target] if field in values]
    return (
        {field: profile_value(profile, field) for field in changed},
        {field: values[field] for field in changed},
    )
