import pytest

from app.core.errors import AppError


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
def test_korean_payload_survives_every_byte_boundary(ending: str) -> None:
    from app.inference.sse_parser import SSEParser

    parser = SSEParser()
    events = []
    for byte in (f'data: {{"text":"안녕"}}{ending}{ending}').encode():
        events.extend(parser.feed(bytes([byte])))
    parser.finish()
    assert events == ['{"text":"안녕"}']


def test_multiline_comments_and_unknown_fields() -> None:
    from app.inference.sse_parser import SSEParser

    parser = SSEParser()
    assert parser.feed(b"\xef\xbb\xbf: ping\nid: 1\ndata: first\ndata:  second\n\ndata:\n\n") == [
        "first\n second",
        "",
    ]
    parser.finish()


@pytest.mark.parametrize("data", [b"data: \xff\n\n", b"data: unfinished", b"data: ok\n"])
def test_invalid_or_truncated_event_is_rejected(data: bytes) -> None:
    from app.inference.sse_parser import SSEParser

    parser = SSEParser()
    with pytest.raises(AppError) as error:
        parser.feed(data)
        parser.finish()
    assert error.value.status == 502


@pytest.mark.parametrize(
    "event_limit,buffer_limit,data",
    [
        (12, 100, b":123456\n:123456\n\n"),
        (100, 8, b"data: 123"),
    ],
)
def test_limits_reject_unterminated_input(event_limit: int, buffer_limit: int, data: bytes) -> None:
    from app.inference.sse_parser import SSEParser

    parser = SSEParser(event_limit=event_limit, buffer_limit=buffer_limit)
    with pytest.raises(AppError):
        parser.feed(data)


def test_limits_reset_after_each_complete_event() -> None:
    from app.inference.sse_parser import SSEParser

    parser = SSEParser(event_limit=16, buffer_limit=16)
    assert parser.feed(b"data: a\n\n" * 100) == ["a"] * 100
    parser.finish()
