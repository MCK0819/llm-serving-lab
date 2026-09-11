import pytest
from pydantic import ValidationError


def test_question_is_trimmed_and_cannot_override_server_context() -> None:
    from app.rag.router import QuestionInput

    assert QuestionInput(question="  휴가 규정은?\n").question == "휴가 규정은?"
    for field in ("organization_id", "user_id", "model", "max_tokens"):
        with pytest.raises(ValidationError):
            QuestionInput.model_validate({"question": "휴가 규정", field: "override"})


@pytest.mark.parametrize("value", ["", " \n\t", None, 42, ["질문"]])
def test_question_requires_nonblank_text(value: object) -> None:
    from app.rag.router import QuestionInput

    with pytest.raises(ValidationError):
        QuestionInput.model_validate({"question": value})


@pytest.mark.parametrize("authorization", [None, "Basic abc", "Bearer", "Bearer malformed"])
async def test_question_requires_bearer_key_even_without_database(
    authorization: str | None,
) -> None:
    import httpx

    from app.main import create_app

    app = create_app()
    headers = {"Authorization": authorization} if authorization else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/questions", json={"question": "휴가 규정"}, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["request_id"] == response.headers["x-request-id"]


def test_openapi_describes_question_body_and_stream_response() -> None:
    from app.main import create_app

    operation = create_app().openapi()["paths"]["/questions"]["post"]
    schema = operation["requestBody"]["content"]["application/json"]["schema"]
    assert set(schema["properties"]) == {"question"}
    assert schema["additionalProperties"] is False
    assert "text/event-stream" in operation["responses"]["200"]["content"]
    assert operation["security"] == [{"HTTPBearer": []}]
