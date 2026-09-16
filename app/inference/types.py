from dataclasses import dataclass
from typing import Literal

type FinishReason = Literal["stop", "length"]
type CompletionReason = FinishReason | Literal["no_context"]


@dataclass(frozen=True)
class Delta:
    text: str


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class Completed:
    reason: CompletionReason
    usage: Usage | None
