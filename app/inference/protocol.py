from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt

from app.inference.types import FinishReason


class WireModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")


class WireDelta(WireModel):
    content: str | None = None
    role: Literal["assistant"] | None = None
    # These fields are deliberately restricted: no tools or hidden reasoning output.
    tool_calls: None = None
    function_call: None = None
    reasoning_content: None = None


class WireChoice(WireModel):
    index: Literal[0]
    delta: WireDelta
    finish_reason: FinishReason | None = None


class WireUsage(WireModel):
    prompt_tokens: NonNegativeInt
    completion_tokens: NonNegativeInt


class WireChunk(WireModel):
    choices: list[WireChoice] = Field(max_length=1)
    usage: WireUsage | None = None
