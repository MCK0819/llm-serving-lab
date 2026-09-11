import pytest
from pydantic import ValidationError

from app.core.settings import Settings


def test_database_url_is_optional_and_secret_in_settings_repr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LLM_LAB_DATABASE_URL", raising=False)
    assert Settings().database_url is None
    settings = Settings(database_url="postgresql+psycopg://user:secret-marker@localhost/test")
    assert "secret-marker" not in repr(settings)


@pytest.mark.parametrize(
    "url", ["sqlite:///local.db", "postgresql://localhost/test", "postgresql+psycopg://localhost"]
)
def test_only_named_postgresql_database_with_supported_async_driver_is_accepted(url: str) -> None:
    with pytest.raises(ValidationError):
        Settings(database_url=url)
