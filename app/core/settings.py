from typing import Self
from urllib.parse import urlsplit

from pydantic import (
    HttpUrl,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LLM_LAB_", frozen=True, extra="forbid", allow_inf_nan=False
    )

    inference_url: HttpUrl = HttpUrl("http://127.0.0.1:8001")
    database_url: SecretStr | None = None
    embedding_url: HttpUrl = HttpUrl("http://127.0.0.1:8080")
    running_limit: PositiveInt = 2
    waiting_limit: NonNegativeInt = 4
    waiting_timeout: PositiveFloat = 10
    connect_timeout: PositiveFloat = 5
    read_timeout: PositiveFloat = 30
    execution_timeout: PositiveFloat = 120
    send_timeout: PositiveFloat = 10
    context_tokens: PositiveInt = 4096
    output_tokens: PositiveInt = 512

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            parsed = urlsplit(value.get_secret_value())
            if (
                parsed.scheme != "postgresql+psycopg"
                or not parsed.hostname
                or not parsed.path.strip("/")
            ):
                raise ValueError("Use a named PostgreSQL database with the psycopg driver")
        return value

    @field_validator("inference_url", "embedding_url")
    @classmethod
    def reject_url_secrets(cls, value: HttpUrl) -> HttpUrl:
        if value.username or value.password or value.query or value.fragment:
            raise ValueError("Service URLs must not contain credentials, queries or fragments")
        return value

    @model_validator(mode="after")
    def require_input_budget(self) -> Self:
        if self.output_tokens >= self.context_tokens:
            raise ValueError("Output reservation must leave room for input")
        return self
