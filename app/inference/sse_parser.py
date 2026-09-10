from app.core.errors import AppError


class SSEParser:
    """Incremental UTF-8 SSE framing with bounded line and event storage.

    Limits count wire bytes, excluding the optional LF following CR.
    A terminated comment-only event is ignored. Truncated events are rejected.
    """

    def __init__(self, event_limit: int = 65536, buffer_limit: int = 131072) -> None:
        if event_limit <= 0 or buffer_limit <= 0:
            raise ValueError("SSE limits must be positive")
        self.event_limit = event_limit
        self.buffer_limit = buffer_limit
        self._line = bytearray()
        self._data: list[str] = []
        self._event_bytes = 0
        self._skip_lf = False
        self._first_line = True

    def feed(self, data: bytes) -> list[str]:
        events: list[str] = []
        for byte in data:
            if self._skip_lf:
                self._skip_lf = False
                if byte == 10:
                    continue
            self._event_bytes += 1
            if self._event_bytes > min(self.event_limit, self.buffer_limit):
                raise self._invalid()
            if byte in (10, 13):
                self._skip_lf = byte == 13
                self._complete_line(events)
            else:
                self._line.append(byte)
        return events

    def _complete_line(self, events: list[str]) -> None:
        try:
            line = self._line.decode("utf-8")
        except UnicodeDecodeError:
            raise self._invalid() from None
        self._line.clear()
        if self._first_line:
            line = line.removeprefix("\ufeff")
            self._first_line = False
        if not line:
            if self._data:
                events.append("\n".join(self._data))
            self._data.clear()
            self._event_bytes = 0
            return
        field, separator, value = line.partition(":")
        if field == "data":
            self._data.append(value.removeprefix(" ") if separator else "")

    def finish(self) -> None:
        if self._event_bytes:
            raise self._invalid()

    @staticmethod
    def _invalid() -> AppError:
        return AppError(502, "invalid_stream", "모델 응답 스트림을 처리하지 못했습니다.")
