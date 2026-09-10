from dataclasses import dataclass
from typing import Literal

type FinishReason = Literal["stop", "length"]


@dataclass(frozen=True)
class Delta:
    text: str


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class Completed:
    reason: FinishReason
    usage: Usage | None
