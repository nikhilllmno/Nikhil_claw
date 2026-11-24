"""``file_send``: a file of the session sandbox goes to the user (docs/architecture.md, Section
11.10; roadmap M8.6), with a fake sandbox and the real database."""

from __future__ import annotations

import posixpath
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.media.files import (
    MAX_FILES_PER_RUN,
    FileSender,
    download_name,
    file_type,
    media_headers,
    resolve,
)
from sama.media.generate import GeneratedMediaStore, MediaStatus, StoredMedia
from sama.media.meter import MediaError
from sama.orchestrator.queue import RunQueue
from sama.sandbox.base import ExecResult, SandboxFileTooLargeError
from sama.tenancy.run_token import RunTokenClaims
from tests.media.support import PNG
from tests.orchestrator.conftest import Tenant

WORKDIR = "/home/user/work"
PDF = b"%PDF-1.7\n" + b"x" * 40
ZIP = b"PK\x03\x04" + b"\x00" * 40


@dataclass
class FakeSandbox:
    """A sandbox with files at absolute paths and symbolic links (``links``)."""

    files: dict[str, bytes] = field(default_factory=dict)
    links: dict[str, str] = field(default_factory=dict)
    folders: set[str] = field(default_factory=set)
    too_large: bool = False
    workdir: str = WORKDIR
    reads: list[str] = field(default_factory=list)

    async def exec(self, argv: Sequence[str], *, timeout_s: float = 60, **_: object) -> ExecResult:
        path = posixpath.normpath(self.links.get(argv[-1], argv[-1]))
        if path in self.folders:
            return ExecResult(3, "", "")
        if path not in self.files:
            return ExecResult(2, "", "realpath: No such file or directory")
        return ExecResult(0, path, "")

    async def read_file(self, path: str, *, max_bytes: int | None = None) -> bytes:
        self.reads.append(path)
        if self.too_large:
            raise SandboxFileTooLargeError("too large")
        return self.files[path]


@dataclass
class Rig:
    sender: FileSender
    sandbox: FakeSandbox
    store: GeneratedMediaStore
    tenant: Tenant
    run_id: uuid.UUID
    sandboxes: list[FakeSandbox | None]

    def claims(self) -> RunTokenClaims:
        ids = self.tenant.ids
        return RunTokenClaims(
            org_id=str(self.tenant.org),
            workspace_id=str(ids["ws"]),
            session_id=str(ids["s3"]),
            run_id=str(self.run_id),
            user_id=str(ids["mia"]),
            token_id="test",
            expires_at=2_000_000_000,
        )


@pytest.fixture
async def rig(engine: AsyncEngine, tenant: Tenant) -> Rig:
    sandbox = FakeSandbox()
    sandboxes: list[FakeSandbox | None] = [sandbox]

    async def session_sandbox(org_id: str, session_id: str) -> FakeSandbox | None:
        assert (org_id, session_id) == (str(tenant.org), str(tenant.ids["s3"]))
        return sandboxes[0]

    store = GeneratedMediaStore(engine)
    run_id = await RunQueue(engine).enqueue(
        org_id=tenant.org, session_id=tenant.ids["s3"], text="make a csv"
    )
    sender = FileSender(engine, session_sandbox, store=store)  # type: ignore[arg-type]
    return Rig(sender, sandbox, store, tenant, run_id, sandboxes)


async def test_a_csv_of_the_work_folder_goes_to_the_user(rig: Rig) -> None:
    rig.sandbox.files[f"{WORKDIR}/top5.csv"] = b"rank,country\n1,India\n"
    result = await rig.sender.send(rig.claims(), "top5.csv", caption="5 countries")
    assert result.status is MediaStatus.OK
    assert "Sent top5.csv (text/csv, 21 bytes) to the user" in result.text
    assert "Do not give a sandbox path" in result.text
    pending = await rig.store.pending(rig.tenant.org, rig.run_id)
    assert [(m.kind, m.mime_type, m.name, m.caption, m.data) for m in pending] == [
        ("file", "text/csv", "top5.csv", "5 countries", b"rank,country\n1,India\n")
    ]
    listed = await rig.store.list(rig.tenant.org, rig.tenant.ids["s3"], 10)
    assert [(m.name, m.size_bytes, m.run_id) for m in listed] == [("top5.csv", 21, rig.run_id)]


async def test_an_absolute_path_in_tmp_and_a_download_name(rig: Rig) -> None:
    rig.sandbox.files["/tmp/out/chart.png"] = PNG
    result = await rig.sender.send(rig.claims(), "/tmp/out/chart.png", name="Rainfall Mumbai")
    assert "Sent Rainfall_Mumbai.png (image/png" in result.text
    (media,) = await rig.store.pending(rig.tenant.org, rig.run_id)
    assert (media.name, media.mime_type) == ("Rainfall_Mumbai.png", "image/png")


@pytest.mark.parametrize(
    "path", ["../.claude/settings.json", "/etc/passwd", "/home/user/.bashrc", "/tmp", ""]
)
async def test_a_path_outside_the_work_folder_and_tmp_is_refused(rig: Rig, path: str) -> None:
    with pytest.raises(MediaError):
        await rig.sender.send(rig.claims(), path or " ")
    assert rig.sandbox.reads == []


async def test_a_link_that_points_outside_is_refused(rig: Rig) -> None:
    rig.sandbox.files["/home/user/.claude/.credentials.json"] = b"{}"
    rig.sandbox.links[f"{WORKDIR}/creds.json"] = "/home/user/.claude/.credentials.json"
    with pytest.raises(MediaError, match="work folder"):
        await rig.sender.send(rig.claims(), "creds.json")
    assert rig.sandbox.reads == []


async def test_missing_empty_folder_and_large_files_get_a_clear_error(rig: Rig) -> None:
    with pytest.raises(MediaError, match="There is no file"):
        await rig.sender.send(rig.claims(), "nothing.csv")
    rig.sandbox.folders.add(f"{WORKDIR}/out")
    with pytest.raises(MediaError, match="is a folder"):
        await rig.sender.send(rig.claims(), "out")
    rig.sandbox.files[f"{WORKDIR}/empty.txt"] = b""
    with pytest.raises(MediaError, match="empty"):
        await rig.sender.send(rig.claims(), "empty.txt")
    rig.sandbox.files[f"{WORKDIR}/big.zip"] = ZIP
    rig.sandbox.too_large = True
    with pytest.raises(MediaError, match="larger than 10.0 MB"):
        await rig.sender.send(rig.claims(), "big.zip")
    assert await rig.store.pending(rig.tenant.org, rig.run_id) == []


async def test_a_file_that_does_not_start_like_its_type_is_refused(rig: Rig) -> None:
    rig.sandbox.files[f"{WORKDIR}/report.pdf"] = b"# not a pdf"
    with pytest.raises(MediaError, match="not a valid application/pdf file"):
        await rig.sender.send(rig.claims(), "report.pdf")
    rig.sandbox.files[f"{WORKDIR}/report2.pdf"] = PDF
    assert (await rig.sender.send(rig.claims(), "report2.pdf")).status is MediaStatus.OK


async def test_no_sandbox_and_the_limit_per_run(rig: Rig) -> None:
    rig.sandbox.files[f"{WORKDIR}/a.txt"] = b"a"
    for _ in range(MAX_FILES_PER_RUN):
        await rig.sender.send(rig.claims(), "a.txt")
    with pytest.raises(MediaError, match="already sent 10 files"):
        await rig.sender.send(rig.claims(), "a.txt")
    rig.sandboxes[0] = None
    with pytest.raises(MediaError, match="no sandbox"):
        await rig.sender.send(rig.claims(), "a.txt")


@pytest.mark.parametrize(
    ("name", "mime"),
    [
        ("a.csv", "text/csv"),
        ("A.PDF", "application/pdf"),
        ("deck.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        ("notes.md", "text/markdown"),
        ("data.parquet", "application/octet-stream"),
        ("README", "application/octet-stream"),
    ],
)
def test_the_type_comes_from_the_extension(name: str, mime: str) -> None:
    assert file_type(name) == mime


def test_download_names_are_safe_and_keep_the_extension() -> None:
    assert download_name(None, "/home/user/work/top 5 (2026).csv") == "top_5_2026.csv"
    assert download_name("Report", "/tmp/r.pdf") == "Report.pdf"
    assert download_name("../../evil.sh", "/tmp/r.pdf") == "evil.sh"
    long = download_name("x" * 200, "/tmp/r.xlsx")
    assert long.endswith(".xlsx")
    assert len(long) == 80


def test_resolve_normalises_inside_the_allowed_folders() -> None:
    assert resolve("out/../a.csv", WORKDIR) == f"{WORKDIR}/a.csv"
    assert resolve("/tmp/x/a.csv", WORKDIR) == "/tmp/x/a.csv"
    with pytest.raises(MediaError):
        resolve("/tmp/../etc/passwd", WORKDIR)


def test_a_file_is_a_download_that_no_browser_runs() -> None:
    page = StoredMedia(uuid.uuid4(), "file", "text/html", "", b"<script>", "page.html")
    headers = media_headers(page)
    assert headers["Content-Disposition"] == 'attachment; filename="page.html"'
    assert headers["Content-Security-Policy"] == "sandbox"
    assert headers["X-Content-Type-Options"] == "nosniff"
    image = StoredMedia(uuid.uuid4(), "image", "image/png", "", PNG)
    assert media_headers(image)["Content-Disposition"] == 'inline; filename="image.png"'
    assert "Content-Security-Policy" not in media_headers(image)
