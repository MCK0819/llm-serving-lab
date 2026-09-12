import os
from contextlib import AsyncExitStack

import httpx
import pytest

from app.core.settings import Settings
from app.main import create_app
from app.users.service import AuthService
from tests.integration.test_auth import accounts  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.fixture
def pdf_file():
    # A small authored, selectable-text PDF; parsing is a later worker responsibility.
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    stream = b"BT /F1 12 Tf 10 100 Td (Company leave policy) Tj ET"
    objects.append(
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
    )
    data = b"%PDF-1.4\n"
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(data)
    data += b"xref\n0 6\n0000000000 65535 f \n"
    data += b"".join(f"{offset:010} 00000 n \n".encode() for offset in offsets[1:])
    data += f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return ("policy.pdf", data, "application/pdf")


@pytest.fixture
async def document_clients(accounts, tmp_path):  # noqa: F811
    repository, _, users = accounts
    auth = AuthService(repository)
    keys = [await auth.issue_key(user) for user in users]
    app = create_app(
        Settings(database_url=os.environ["LLM_LAB_TEST_DATABASE_URL"], upload_root=tmp_path)
    )
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(app.router.lifespan_context(app))
        clients = [
            await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://test",
                    headers={"Authorization": f"Bearer {key}"},
                )
            )
            for key in keys
        ]
        yield clients


@pytest.fixture
def api_a(document_clients):
    return document_clients[0]


@pytest.fixture
def api_b(document_clients):
    return document_clients[2]


async def test_document_id_does_not_grant_cross_tenant_access(api_a, api_b, pdf_file):
    accepted = await api_a.post("/documents", files={"file": pdf_file})
    assert accepted.status_code == 202
    document_id = accepted.json()["document_id"]
    response = await api_b.get(f"/documents/{document_id}")
    assert response.status_code == 404


async def test_same_org_can_read_but_only_uploader_can_delete(document_clients, pdf_file):
    uploader, colleague, outsider = document_clients
    response = await uploader.post("/documents", files={"file": pdf_file})
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["request_id"] == response.headers["x-request-id"]
    url = response.headers["location"]
    detail = await colleague.get(url)
    assert detail.status_code == 200
    assert detail.json()["failure"] is None
    assert "storage_path" not in detail.text
    listing = await colleague.get("/documents")
    assert [item["document_id"] for item in listing.json()["items"]] == [body["document_id"]]
    assert (await colleague.delete(url)).status_code == 403
    assert (await outsider.delete(url)).status_code == 404
    for _ in range(2):
        deleted = await uploader.delete(url)
        assert deleted.status_code == 204
        assert deleted.content == b""
    assert (await uploader.get(url)).status_code == 404
    assert (await colleague.get("/documents")).json()["items"] == []


@pytest.mark.parametrize(
    "files,status",
    [
        ({"file": ("x.pdf", b"not PDF", "application/pdf")}, 415),
        ({"file": ("x.pdf", b"%PDF-1.4", "text/plain")}, 415),
        ({"other": ("x.pdf", b"%PDF-1.4", "application/pdf")}, 422),
        ([("file", ("x.pdf", b"%PDF-1.4", "application/pdf"))] * 2, 422),
        ({"file": ("x" * 256, b"%PDF-1.4", "application/pdf")}, 422),
    ],
)
async def test_invalid_uploads_leave_no_documents(api_a, files, status):
    response = await api_a.post("/documents", files=files)
    assert response.status_code == status
    assert (await api_a.get("/documents")).json()["items"] == []


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "cursor=garbage", "cursor="])
async def test_invalid_pagination_returns_safe_422(api_a, query):
    response = await api_a.get("/documents?" + query)
    assert response.status_code == 422
    assert "Traceback" not in response.text


async def test_documents_require_authentication(api_a, pdf_file):
    response = await api_a.post(
        "/documents", files={"file": pdf_file}, headers={"Authorization": ""}
    )
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_malformed_multipart_is_safe_input_error(api_a):
    response = await api_a.post(
        "/documents",
        content=b"not multipart\r\n",
        headers={
            "Content-Type": "multipart/form-data; boundary=boundary",
        },
    )
    assert response.status_code == 422


async def test_real_broker_outage_still_persists_document_job(accounts, tmp_path, pdf_file):  # noqa: F811
    import socket

    from sqlalchemy import text

    repository, _, users = accounts
    key = await AuthService(repository).issue_key(users[0])
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        app = create_app(
            Settings(
                database_url=os.environ["LLM_LAB_TEST_DATABASE_URL"],
                upload_root=tmp_path,
                broker_url=f"redis://127.0.0.1:{port}/0",
            )
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/documents",
                    files={"file": pdf_file},
                    headers={"Authorization": f"Bearer {key}"},
                )
    assert response.status_code == 202
    async with repository.sessions() as session:
        count = await session.scalar(
            text("SELECT count(*) FROM jobs WHERE document_id = :id"),
            {"id": response.json()["document_id"]},
        )
    assert count == 1
    assert len(list(tmp_path.rglob("*.pdf"))) == 1


async def test_upload_enforces_actual_twenty_mib_file_limit(api_a):
    response = await api_a.post(
        "/documents",
        files={
            "file": (
                "large.pdf",
                b"%PDF-" + b"x" * (20 * 1024**2 - 4),
                "application/pdf",
            )
        },
    )
    assert response.status_code == 413
    assert (await api_a.get("/documents")).json()["items"] == []


@pytest.mark.parametrize("headers", [{}, {"Content-Length": "1"}])
async def test_upload_total_limit_ignores_declared_length(api_a, headers):
    async def body():
        yield (
            b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="large.pdf"\r\n'
            b"Content-Type: application/pdf\r\n\r\n%PDF-"
        )
        for _ in range(22):
            yield b"x" * 1024**2

    response = await api_a.post(
        "/documents",
        content=body(),
        headers={
            **headers,
            "Content-Type": "multipart/form-data; boundary=boundary",
        },
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert (await api_a.get("/documents")).json()["items"] == []
