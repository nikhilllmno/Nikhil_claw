"""The ``clarify`` tool: the agent asks the user 1 to 5 questions (docs/architecture.md,
Section 16.7).

``ClarifyBroker`` keeps the open questions in memory. The tool call waits until the API or a
channel answers them, or until the time limit. The broker is in the process of the tool
server. The run queue (M1.10) moves the open questions to Postgres.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from sama.tenancy.run_token import RunTokenClaims

MAX_QUESTIONS: Final = 5
MAX_CHOICES: Final = 6
MAX_OPEN_PER_SESSION: Final = 5
MAX_ANSWER_CHARS: Final = 2_000
DEFAULT_WAIT_S: Final = 600.0

Choice = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class ClarifyError(ValueError):
    """The request or the answer is not valid. The message is for the caller."""


class ClarifyQuestion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    question: str = Field(min_length=1, max_length=500)
    choices: tuple[Choice, ...] = Field(
        default=(),
        max_length=MAX_CHOICES,
        description="2 to 6 short choices, or none for a free answer.",
    )


class NativeQuestion(ClarifyQuestion):
    """A question of the native ``AskUserQuestion`` tool of the Claude CLI (M8.3). It has the
    fields of a ``clarify`` question plus the short ``header``, one description for each
    choice, and ``multi_select``. The web app shows these fields as Claude Code does."""

    header: str = Field(default="", max_length=40)
    descriptions: tuple[str, ...] = ()
    multi_select: bool = False


MAX_NATIVE_QUESTIONS: Final = 4
MAX_NATIVE_OPTIONS: Final = 4
MAX_DESCRIPTION_CHARS: Final = 500


def native_questions(arguments: Mapping[str, Any]) -> list[NativeQuestion]:
    """The questions of an ``AskUserQuestion`` input: ``questions`` with ``question``,
    ``header``, ``options`` (``label`` and ``description``), and ``multiSelect``. Raises
    ``ClarifyError`` for an input that does not fit."""
    raw = arguments.get("questions")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_NATIVE_QUESTIONS:
        raise ClarifyError(f"ask 1 to {MAX_NATIVE_QUESTIONS} questions")
    questions: list[NativeQuestion] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ClarifyError("a question is not an object")
        options = item.get("options")
        if not isinstance(options, list) or not 2 <= len(options) <= MAX_NATIVE_OPTIONS:
            raise ClarifyError(f"a question needs 2 to {MAX_NATIVE_OPTIONS} options")
        labels: list[str] = []
        descriptions: list[str] = []
        for option in options:
            if not isinstance(option, Mapping) or not isinstance(option.get("label"), str):
                raise ClarifyError("an option needs a label")
            labels.append(str(option["label"]).strip())
            description = option.get("description")
            descriptions.append(
                description.strip()[:MAX_DESCRIPTION_CHARS] if isinstance(description, str) else ""
            )
        header = item.get("header")
        try:
            questions.append(
                NativeQuestion(
                    question=str(item.get("question") or "").strip(),
                    choices=tuple(labels),
                    header=header.strip()[:40] if isinstance(header, str) else "",
                    descriptions=tuple(descriptions),
                    multi_select=item.get("multiSelect") is True,
                )
            )
        except ValidationError as error:
            raise ClarifyError("a question is not valid") from error
    texts = [q.question for q in questions]
    if len(set(texts)) != len(texts):
        raise ClarifyError("two questions have the same text")
    return questions


def native_answers(questions: Sequence[ClarifyQuestion], answers: Sequence[str]) -> dict[str, str]:
    """The ``answers`` of the ``AskUserQuestion`` result: the answer of each question by the
    text of the question. A multi-select answer is the labels joined with ", "."""
    return {q.question: a for q, a in zip(questions, answers, strict=True)}


@dataclass(frozen=True)
class ClarifyRequest:
    """Open questions of one tool call. Channels render them, the API answers them."""

    id: str
    org_id: str
    workspace_id: str
    session_id: str
    run_id: str
    user_id: str | None
    questions: tuple[ClarifyQuestion, ...]
    created_at: float


@dataclass
class _Open:
    request: ClarifyRequest
    answers: asyncio.Future[tuple[str, ...] | None] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )


def check_questions(questions: Sequence[ClarifyQuestion]) -> None:
    if not 1 <= len(questions) <= MAX_QUESTIONS:
        raise ClarifyError(f"ask 1 to {MAX_QUESTIONS} questions")
    for question in questions:
        if len(question.choices) == 1:
            raise ClarifyError("a question needs 2 or more choices, or none for a free answer")


class ClarifyBroker:
    """In-memory open questions, for one process."""

    def __init__(self, *, wait_s: float = DEFAULT_WAIT_S) -> None:
        self.wait_s = wait_s
        self._open: dict[str, _Open] = {}

    async def ask(
        self, claims: RunTokenClaims, questions: Sequence[ClarifyQuestion]
    ) -> tuple[str, ...] | None:
        """Waits for the answers. Returns ``None`` if nobody answers in time or the run is
        cancelled."""
        check_questions(questions)
        # A run waits on one clarify call at a time. A new call means that the client gave up
        # on the old one (for example after a disconnect), so the old one ends.
        self.cancel_run(claims.org_id, claims.run_id)
        if len(self.pending(claims.org_id, claims.session_id)) >= MAX_OPEN_PER_SESSION:
            raise ClarifyError("the session has too many open questions. Wait for the answers.")
        request = ClarifyRequest(
            id=str(uuid.uuid4()),
            org_id=claims.org_id,
            workspace_id=claims.workspace_id,
            session_id=claims.session_id,
            run_id=claims.run_id,
            user_id=claims.user_id,
            questions=tuple(questions),
            created_at=time.time(),
        )
        entry = _Open(request)
        self._open[request.id] = entry
        try:
            async with asyncio.timeout(self.wait_s):
                return await entry.answers
        except TimeoutError:
            return None
        finally:
            self._open.pop(request.id, None)

    def pending(self, org_id: str, session_id: str) -> list[ClarifyRequest]:
        """The requests of the session that still wait for an answer."""
        return [
            o.request
            for o in self._open.values()
            if o.request.org_id == org_id
            and o.request.session_id == session_id
            and not o.answers.done()
        ]

    def answer(self, org_id: str, request_id: str, answers: Sequence[str]) -> None:
        """Answers an open request of the org. Raises ``ClarifyError`` if it is not open or
        the answers do not fit."""
        entry = self._open.get(request_id)
        if entry is None or entry.request.org_id != org_id or entry.answers.done():
            raise ClarifyError("the question is not open")
        if len(answers) != len(entry.request.questions):
            raise ClarifyError(f"give {len(entry.request.questions)} answers")
        clean = tuple(a.strip() for a in answers)
        if any(not a or len(a) > MAX_ANSWER_CHARS for a in clean):
            raise ClarifyError(f"each answer needs 1 to {MAX_ANSWER_CHARS} characters")
        entry.answers.set_result(clean)

    def cancel_run(self, org_id: str, run_id: str) -> None:
        """Ends the waits of a run, for example after a cancel."""
        for entry in list(self._open.values()):
            if (
                entry.request.org_id == org_id
                and entry.request.run_id == run_id
                and not entry.answers.done()
            ):
                entry.answers.set_result(None)


def render_answers(questions: Sequence[ClarifyQuestion], answers: Sequence[str] | None) -> str:
    if answers is None:
        return (
            "The user did not answer. Continue with your best judgment, and tell the user "
            "which assumptions you made."
        )
    lines = ["The user answered:"]
    for number, (question, answer) in enumerate(zip(questions, answers, strict=True), start=1):
        lines += [f"{number}. {question.question}", f"   Answer: {answer}"]
    return "\n".join(lines)
