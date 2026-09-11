import json
import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest

pytestmark = pytest.mark.integration


def run_admin(*args: str):
    import subprocess
    import sys

    return subprocess.run(
        [sys.executable, "-m", "app.users.cli", *args],
        capture_output=True,
        env={**os.environ, "LLM_LAB_DATABASE_URL": os.environ["LLM_LAB_TEST_DATABASE_URL"]},
        text=True,
        timeout=15,
        check=False,
    )


@pytest.fixture
async def accounts() -> AsyncIterator[tuple[object, list[UUID], list[UUID]]]:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.users.repository import UserRepository

    url = os.environ.get("LLM_LAB_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Use the isolated Docker Compose PostgreSQL test environment")
    engine = create_async_engine(url, hide_parameters=True)
    orgs, users = [uuid4(), uuid4()], [uuid4(), uuid4(), uuid4()]
    try:
        async with engine.begin() as conn:
            for org in orgs:
                await conn.execute(
                    text("INSERT INTO organizations (id, name) VALUES (:id, 'Test org')"),
                    {"id": org},
                )
            for user, org in zip(users, [orgs[0], orgs[0], orgs[1]], strict=True):
                await conn.execute(
                    text(
                        "INSERT INTO users (id, organization_id, name) "
                        "VALUES (:id, :org, 'Test user')"
                    ),
                    {"id": user, "org": org},
                )
        yield UserRepository(async_sessionmaker(engine, expire_on_commit=False)), orgs, users
    finally:
        async with engine.begin() as conn:
            for org in orgs:
                await conn.execute(text("DELETE FROM organizations WHERE id = :id"), {"id": org})
        await engine.dispose()


async def test_key_authentication_resolves_organization_and_revocation(accounts: tuple) -> None:
    from sqlalchemy import text

    from app.core.errors import AppError
    from app.users.service import AuthService

    repository, orgs, users = accounts
    auth = AuthService(repository)
    keys = [await auth.issue_key(user) for user in users]
    for key, user, org in zip(keys, users, [orgs[0], orgs[0], orgs[1]], strict=True):
        identity = await auth.authenticate(key)
        assert identity.user_id == user
        assert identity.organization_id == org
    async with repository.sessions() as session:
        stored = (await session.execute(text("SELECT secret_hash FROM api_keys"))).scalars().all()
    assert all(key.split(".")[1] not in stored for key in keys)
    assert all(len(value) == 64 for value in stored)
    await auth.revoke_key(UUID(keys[0].split(".")[0]))
    for raw in (
        keys[0],
        "invalid",
        keys[1][:-1] + ("a" if keys[1][-1] != "a" else "b"),
        f"{uuid4()}.{keys[1].split('.')[1]}",
    ):
        with pytest.raises(AppError) as error:
            await auth.authenticate(raw)
        assert error.value.status == 401
        assert raw not in str(error.value)
    assert (await auth.authenticate(keys[1])).user_id == users[1]


async def test_authenticated_question_uses_server_identity_and_releases_db_before_stream(
    accounts: tuple,
) -> None:
    import httpx
    from sqlalchemy import text

    from app.core.settings import Settings
    from app.inference.response import PreparedStream
    from app.inference.types import Completed, Delta
    from app.main import create_app
    from app.users.service import AuthService

    repository, orgs, users = accounts
    auth = AuthService(repository)
    key = await auth.issue_key(users[2])

    async def prepare(identity, question, stack):
        async with repository.sessions() as session:
            idle = await session.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE application_name = 'llm-serving-lab-api' "
                    "AND state = 'idle in transaction'"
                )
            )
        assert idle == 0
        assert identity.user_id == users[2]
        assert identity.organization_id == orgs[1]
        assert question == "휴가 규정"

        async def events():
            yield Delta("시험 답변")
            yield Completed("stop", None)

        return PreparedStream([{"source_id": "S1", "page": 1}], events())

    settings = Settings(database_url=os.environ["LLM_LAB_TEST_DATABASE_URL"])
    app = create_app(settings, prepare_question=prepare)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {"Authorization": f"Bearer {key}"}
            response = await client.post(
                "/questions", json={"question": "  휴가 규정  "}, headers=headers
            )
            assert response.status_code == 200
            assert response.text.count("event: sources") == 1
            assert "시험 답변" in response.text
            assert response.text.count("event: done") == 1
            assert "event: error" not in response.text
            for line in response.text.splitlines():
                if line.startswith("data: "):
                    assert json.loads(line[6:])["request_id"] == response.headers["x-request-id"]
            for override in ({"organization_id": str(orgs[0])}, {"model": "untrusted"}):
                rejected = await client.post(
                    "/questions", json={"question": "휴가 규정", **override}, headers=headers
                )
                assert rejected.status_code == 422
            await auth.revoke_key(UUID(key.split(".")[0]))
            rejected = await client.post(
                "/questions", json={"question": "휴가 규정"}, headers=headers
            )
            assert rejected.status_code == 401
            assert rejected.headers["www-authenticate"] == "Bearer"
            assert key not in rejected.text


async def test_default_question_never_opens_ungrounded_inference(accounts: tuple) -> None:
    import httpx

    from app.core.settings import Settings
    from app.main import create_app
    from app.users.service import AuthService

    repository, _, users = accounts
    key = await AuthService(repository).issue_key(users[0])
    app = create_app(
        Settings(
            database_url=os.environ["LLM_LAB_TEST_DATABASE_URL"], inference_url="http://127.0.0.1:1"
        )
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/questions",
                json={"question": "휴가 규정"},
                headers={"Authorization": f"Bearer {key}"},
            )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "rag_unavailable"


async def test_question_limits_received_bytes_and_rejects_invalid_json(accounts: tuple) -> None:
    import httpx

    from app.core.settings import Settings
    from app.main import create_app
    from app.users.service import AuthService

    repository, _, users = accounts
    key = await AuthService(repository).issue_key(users[0])
    app = create_app(Settings(database_url=os.environ["LLM_LAB_TEST_DATABASE_URL"]))
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:

            async def chunks():
                yield b'{"question":"'
                yield b"x" * 16384
                yield b'"}'

            response = await client.post(
                "/questions", content=chunks(), headers={**headers, "Content-Length": "1"}
            )
            assert response.status_code == 413
            for payload in (b"not json", b'{"question":" "}', b'{"question":42}'):
                response = await client.post("/questions", content=payload, headers=headers)
                assert response.status_code == 422
            response = await client.post(
                "/questions", content=b"{}", headers={**headers, "Content-Type": "text/plain"}
            )
            assert response.status_code == 415


async def test_admin_cli_provisions_users_issues_once_and_revokes(
    accounts: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import text

    from app.core.errors import AppError
    from app.users.service import AuthService

    repository, _, _ = accounts
    # An unrelated app setting must never redirect test CLI writes.
    monkeypatch.setenv("LLM_LAB_DATABASE_URL", "postgresql+psycopg://localhost:1/unrelated")
    result = run_admin("create-organization", "--name", "CLI test")
    assert result.returncode == 0, result.stderr
    org_id = UUID(result.stdout.strip())
    try:
        result = run_admin("create-user", "--organization", str(org_id), "--name", "CLI user")
        assert result.returncode == 0, result.stderr
        user_id = UUID(result.stdout.strip())
        result = run_admin("issue-key", "--user", str(user_id))
        assert result.returncode == 0, result.stderr
        raw_key = result.stdout.strip()
        assert len(result.stdout.splitlines()) == 1
        assert raw_key not in result.stderr
        identity = await AuthService(repository).authenticate(raw_key)
        assert identity.user_id == user_id
        assert identity.organization_id == org_id
        result = run_admin("revoke-key", "--key-id", raw_key.split(".")[0])
        assert result.returncode == 0, result.stderr
        assert raw_key not in result.stdout + result.stderr
        with pytest.raises(AppError) as error:
            await AuthService(repository).authenticate(raw_key)
        assert error.value.status == 401
        result = run_admin("issue-key", "--user", str(uuid4()))
        assert result.returncode != 0
        assert "INSERT" not in result.stderr
        assert "Traceback" not in result.stderr
    finally:
        async with repository.sessions.begin() as session:
            await session.execute(text("DELETE FROM organizations WHERE id = :id"), {"id": org_id})
