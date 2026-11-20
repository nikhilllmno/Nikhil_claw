"""The Slack gateway shares the images and voice notes of a run with the file upload flow
(docs/architecture.md, Section 11.8; roadmap M4.9)."""

from __future__ import annotations

import json

from sqlalchemy.ext.asyncio import AsyncEngine

from sama.gateway.slack import SlackGateway
from sama.media.generate import GeneratedMediaStore
from sama.runtime.contract import RunCompleted
from sama.runtime.fake import FakeScript
from tests.gateway.media_support import PNG, add_media, latest_run
from tests.gateway.test_slack import DM, dm, gateway, pair, slack  # noqa: F401
from tests.integrations.support import FakeSlack
from tests.orchestrator.conftest import (  # noqa: F401
    Tenant,
    engine,
    queue_database_url,
    tenant,
)
from tests.orchestrator.test_worker import Harness


async def test_slack_uploads_the_files_without_the_token(
    gateway: SlackGateway,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    slack: FakeSlack,  # noqa: F811
) -> None:
    await pair(gateway, engine, tenant)
    harness = Harness(engine)
    harness.runtime.add_script(FakeScript(events=(RunCompleted(output="here"),)))
    await gateway.handle_event(dm("draw a fox"))
    run = await latest_run(engine, tenant.org)
    await add_media(engine, run, "image", PNG)
    assert await harness.worker.run_once()
    await gateway.drain()
    [ticket] = slack.method_calls("files.getUploadURLExternal")
    assert ticket == {"filename": "image.png", "length": str(len(PNG))}
    assert slack.uploads == [(slack.upload_url, PNG, None)]
    [complete] = slack.method_calls("files.completeUploadExternal")
    assert complete["channel_id"] == DM
    assert json.loads(complete["files"]) == [{"id": "F0FILE1", "title": "Image"}]
    assert await GeneratedMediaStore(engine).pending(tenant.org, run.id) == []


async def test_slack_refuses_an_upload_url_of_another_host(
    gateway: SlackGateway,  # noqa: F811
    engine: AsyncEngine,  # noqa: F811
    tenant: Tenant,  # noqa: F811
    slack: FakeSlack,  # noqa: F811
) -> None:
    slack.upload_url = "https://evil.test/upload"
    await pair(gateway, engine, tenant)
    harness = Harness(engine)
    harness.runtime.add_script(FakeScript(events=(RunCompleted(output="here"),)))
    await gateway.handle_event(dm("draw a fox"))
    run = await latest_run(engine, tenant.org)
    await add_media(engine, run, "image", PNG)
    assert await harness.worker.run_once()
    await gateway.drain()
    assert slack.uploads == []
    assert slack.method_calls("files.completeUploadExternal") == []
    assert len(await GeneratedMediaStore(engine).pending(tenant.org, run.id)) == 1
