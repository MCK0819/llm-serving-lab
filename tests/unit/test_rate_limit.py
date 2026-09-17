from uuid import uuid4

import pytest

from app.core.errors import AppError


def test_question_limit_uses_a_sixty_second_sliding_window() -> None:
    from app.core.rate_limit import RateLimiter

    now = [100.0]
    limiter = RateLimiter(clock=lambda: now[0])
    user_id = uuid4()

    for _ in range(20):
        limiter.check(user_id, "question")

    with pytest.raises(AppError) as rejected:
        limiter.check(user_id, "question")
    assert rejected.value.status == 429
    assert rejected.value.code == "rate_limited"
    assert rejected.value.headers == {"Retry-After": "60"}

    now[0] = 160.0
    limiter.check(user_id, "question")


def test_retry_after_rounds_remaining_window_up() -> None:
    from app.core.rate_limit import RateLimiter

    now = [0.25]
    limiter = RateLimiter(clock=lambda: now[0])
    user_id = uuid4()
    for _ in range(3):
        limiter.check(user_id, "upload")

    now[0] = 1.01
    with pytest.raises(AppError) as rejected:
        limiter.check(user_id, "upload")
    assert rejected.value.headers == {"Retry-After": "60"}

    now[0] = 59.26
    with pytest.raises(AppError) as nearly_ready:
        limiter.check(user_id, "upload")
    assert nearly_ready.value.headers == {"Retry-After": "1"}


def test_sliding_window_only_expires_timestamps_at_the_boundary() -> None:
    from app.core.rate_limit import RateLimiter

    now = [0.0]
    limiter = RateLimiter(clock=lambda: now[0])
    user_id = uuid4()
    limiter.check(user_id, "question")
    now[0] = 1.0
    for _ in range(19):
        limiter.check(user_id, "question")

    now[0] = 60.0
    limiter.check(user_id, "question")
    with pytest.raises(AppError) as rejected:
        limiter.check(user_id, "question")
    assert rejected.value.headers == {"Retry-After": "1"}


def test_categories_have_independent_limits() -> None:
    from app.core.rate_limit import RateLimiter

    limiter = RateLimiter(clock=lambda: 10.0)
    user_id = uuid4()
    for _ in range(3):
        limiter.check(user_id, "upload")
    for _ in range(20):
        limiter.check(user_id, "question")
    for _ in range(60):
        limiter.check(user_id, "read")

    for category in ("upload", "question", "read"):
        with pytest.raises(AppError) as rejected:
            limiter.check(user_id, category)
        assert rejected.value.status == 429


def test_users_have_independent_limits() -> None:
    from app.core.rate_limit import RateLimiter

    limiter = RateLimiter(clock=lambda: 10.0)
    first = uuid4()
    second = uuid4()
    for _ in range(3):
        limiter.check(first, "upload")

    limiter.check(second, "upload")

    with pytest.raises(AppError):
        limiter.check(first, "upload")


def test_key_capacity_fails_closed_until_an_inactive_key_expires() -> None:
    from app.core.rate_limit import RateLimiter

    now = [5.0]
    limiter = RateLimiter(clock=lambda: now[0], max_keys=1)
    first = uuid4()
    second = uuid4()
    limiter.check(first, "read")

    with pytest.raises(AppError) as rejected:
        limiter.check(second, "read")
    assert rejected.value.status == 429
    assert rejected.value.headers == {"Retry-After": "60"}

    now[0] = 65.0
    limiter.check(second, "read")


def test_app_error_adds_optional_headers_without_breaking_existing_constructor() -> None:
    error = AppError(
        429,
        "rate_limited",
        "요청이 너무 많습니다.",
        headers={"Retry-After": "7", "X-Internal-Path": "secret"},
    )

    assert error.headers == {"Retry-After": "7", "X-Internal-Path": "secret"}
    assert AppError(503, "busy", "잠시 후 다시 시도해 주세요.").headers == {}
