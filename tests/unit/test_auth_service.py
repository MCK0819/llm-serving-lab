import pytest

from app.core.errors import AppError


@pytest.mark.parametrize(
    "raw", ["", "invalid", "x" * 10000, "00000000-0000-0000-0000-000000000000.비밀", "a.b.c"]
)
async def test_malformed_key_is_rejected_before_database_access(raw: str) -> None:
    from app.users.service import AuthService

    class UnavailableRepository:
        async def find_active_key(self, key_id: object) -> None:
            raise AssertionError("Malformed credentials must not query storage")

    with pytest.raises(AppError) as error:
        await AuthService(UnavailableRepository()).authenticate(raw)
    assert error.value.status == 401
