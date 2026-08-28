"""NVIDIA NIM request settings.

Every sampling knob here defaults to ``None``, meaning "not set". A field that
is ``None`` is never written into the outbound request body, so NIM applies its
own default for that model. This matters because NIM pins some parameters
per-model and rejects any other value: ``moonshotai/kimi-k3`` requires
``top_p`` to be exactly ``0.95`` and answers ``400 Validation: top_p is
immutable for this model`` to anything else.

Before 5.61.0 these were non-optional floats defaulting to ``1.0``/``0.0``,
which had two consequences. Every NIM request carried a ``top_p`` and a
``temperature`` the client never asked for -- Claude Code speaks the Anthropic
protocol and sends neither -- and "unset" was indistinguishable from a
deliberate ``presence_penalty=0.0``, because the request builder used
"value equals the default" as its proxy for "user did not set it".
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

# Sampling fields that are absent from the request body unless explicitly set.
_OPTIONAL_FLOAT_FIELDS = (
    "temperature",
    "top_p",
    "min_p",
    "presence_penalty",
    "frequency_penalty",
    "repetition_penalty",
)


class NimSettings(BaseModel):
    """NVIDIA NIM request settings; unset fields defer to the provider."""

    temperature: Annotated[float, Field(ge=0.0, le=2.0)] | None = None
    top_p: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    top_k: int | None = None
    # Unset by default, so the client's max_tokens reaches NIM unchanged and
    # the model's own output limit applies. This used to default to
    # ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS (81920) and was applied as
    # min(client_value, 81920), which silently truncated any model whose real
    # limit is higher -- an invented ceiling of exactly the kind the project
    # forbids. Set it to impose a deliberate cap.
    max_tokens: Annotated[int, Field(ge=1)] | None = None
    presence_penalty: Annotated[float, Field(ge=-2.0, le=2.0)] | None = None
    frequency_penalty: Annotated[float, Field(ge=-2.0, le=2.0)] | None = None
    min_p: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    repetition_penalty: Annotated[float, Field(ge=0.0)] | None = None
    seed: int | None = None
    stop: str | None = None
    parallel_tool_calls: bool = True
    ignore_eos: bool | None = None
    min_tokens: Annotated[int, Field(ge=0)] | None = None
    chat_template: str | None = None
    request_id: str | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("top_k", mode="before")
    @classmethod
    def validate_top_k(cls, v, info: ValidationInfo):
        # -1 was the historical "unset" sentinel and is still accepted as one,
        # so an existing caller passing -1 keeps meaning "let NIM decide".
        if v is None or v == "" or v == -1:
            return None
        int_v = int(v)
        if int_v < 0:
            raise ValueError(f"{info.field_name} must be -1 or >= 0")
        return int_v

    @field_validator(*_OPTIONAL_FLOAT_FIELDS, mode="before")
    @classmethod
    def validate_float_fields(cls, v, info: ValidationInfo):
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError) as err:
            raise ValueError(
                f"{info.field_name} must be a float. Got {type(v).__name__}."
            ) from err

    @field_validator("max_tokens", "min_tokens", mode="before")
    @classmethod
    def validate_int_fields(cls, v, info: ValidationInfo):
        if v is None or v == "":
            return None
        try:
            return int(v)
        except (TypeError, ValueError) as err:
            raise ValueError(
                f"{info.field_name} must be an int. Got {type(v).__name__}."
            ) from err

    @field_validator("seed", mode="before")
    @classmethod
    def parse_optional_int(cls, v, info: ValidationInfo):
        if v == "" or v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError) as err:
            raise ValueError(
                f"{info.field_name} must be an int or empty/None."
            ) from err

    @field_validator("stop", "chat_template", "request_id", mode="before")
    @classmethod
    def parse_optional_str(cls, v, info: ValidationInfo):
        if v == "":
            return None
        if v is not None and not isinstance(v, str):
            return str(v)
        return v
