import logging
import os
from contextlib import AsyncExitStack

import httpx
import pytest

from app.core.settings import Settings
from app.inference.client import InferenceClient
from app.inference.response import PreparedStream
from app.main import create_app
from app.users.service import AuthService
from tests.integration.test_auth import accounts  # noqa: F401

pytestmark = pytest.mark.integration


async def test_authenticated_failure_paths_hide_credentials_and_private_content(
    accounts,  # noqa: F811
    tmp_path,
    caplog,
):
    repository, _, users = accounts
    key = await AuthService(repository).issue_key(users[0])
    canaries = ["PRIVATE_QUESTION", "PRIVATE_PDF", "PRIVATE_UPSTREAM", "PRIVATE_QUERY"]

    def unavailable(request):
        return httpx.Response(500, text="PRIVATE_UPSTREAM")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(unavailable), base_url="http://model"
    ) as model:
        inference = InferenceClient(model, "test-model")

        async def prepare(identity, question, stack: AsyncExitStack):
            events = await stack.enter_async_context(
                inference.open([{"role": "user", "content": question}])
            )
            return PreparedStream([], events)

        app = create_app(
            Settings(database_url=os.environ["LLM_LAB_TEST_DATABASE_URL"], upload_root=tmp_path),
            prepare_question=prepare,
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                headers = {"Authorization": f"Bearer {key}"}
                with caplog.at_level(logging.DEBUG):
                    upstream_error = await client.post(
                        "/questions?token=PRIVATE_QUERY",
                        json={"question": "PRIVATE_QUESTION"},
                        headers=headers,
                    )
                    malformed = await client.post(
                        "/questions",
                        content=b'{"question":"PRIVATE_QUESTION"',
                        headers={**headers, "Content-Type": "application/json"},
                    )
                    upload = await client.post(
                        "/documents",
                        content=b"PRIVATE_PDF",
                        headers={
                            **headers,
                            "Content-Type": "multipart/form-data; boundary=test-boundary",
                        },
                    )
                    invalid_auth = await client.post(
                        "/questions",
                        json={"question": "PRIVATE_QUESTION"},
                        headers={"Authorization": "Bearer PRIVATE_KEY"},
                    )
    assert [
        upstream_error.status_code,
        malformed.status_code,
        upload.status_code,
        invalid_auth.status_code,
    ] == [502, 422, 422, 401]
    exposed = caplog.text + "".join(
        response.text for response in (upstream_error, malformed, upload, invalid_auth)
    )
    assert all(value not in exposed for value in [key, "PRIVATE_KEY", *canaries])
