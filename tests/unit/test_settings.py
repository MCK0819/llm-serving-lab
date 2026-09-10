import pytest
from pydantic import ValidationError


@pytest.mark.parametrize(
    "field,value",
    [
        ("inference_url", "file:///private"),
        ("inference_url", "https://user:secret-marker@host"),
        ("embedding_url", "http://host?token=secret-marker"),
        ("running_limit", 0),
        ("waiting_limit", -1),
        ("execution_timeout", -0.1),
        ("execution_timeout", float("inf")),
        ("output_tokens", 4096),
    ],
)
def test_invalid_configuration_is_rejected(field: str, value: object) -> None:
    from app.core.settings import Settings

    with pytest.raises(ValidationError):
        Settings(**{field: value})


def test_environment_configuration_is_read_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.settings import Settings
    from app.main import create_app

    monkeypatch.setenv("LLM_LAB_INFERENCE_URL", "http://inference:8000")
    settings = Settings()
    assert str(settings.inference_url) == "http://inference:8000/"
    assert create_app(settings).title == "LLM Serving Lab"
