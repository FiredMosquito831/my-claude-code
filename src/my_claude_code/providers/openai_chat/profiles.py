"""Declarative profiles for ordinary OpenAI-compatible providers."""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

from my_claude_code.application.errors import InvalidRequestError
from my_claude_code.config.constants import ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS
from my_claude_code.core.anthropic import ReasoningReplayMode
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import (
    ReasoningDialectOrigin,
    ReasoningEffort,
    ReasoningPolicy,
)
from my_claude_code.providers.model_listing import RequiredPathValues

from .base_url import openai_v1_base_url
from .extra_body import validate_extra_body_does_not_override_canonical_fields
from .reasoning import (
    LLAMACPP_REASONING,
    SPLIT_REASONING_OUTPUT,
    ChatTemplateReasoning,
    EffortOrThinkingBudgetReasoning,
    NamedEffortReasoning,
    ReasoningEncoder,
    ReasoningObject,
    ThinkingObjectReasoning,
)
from .reasoning_details import apply_reasoning_details_replay
from .request_policy import OpenAIChatPostprocessor, OpenAIChatRequestPolicy

_ALL_EFFORTS = tuple((effort, effort.value) for effort in ReasoningEffort)
_LOW_MEDIUM_HIGH = (
    (ReasoningEffort.MINIMAL, "low"),
    (ReasoningEffort.LOW, "low"),
    (ReasoningEffort.MEDIUM, "medium"),
    (ReasoningEffort.HIGH, "high"),
    (ReasoningEffort.XHIGH, "high"),
    (ReasoningEffort.MAX, "high"),
)
_MINIMAL_TO_XHIGH = (
    (ReasoningEffort.MINIMAL, "minimal"),
    (ReasoningEffort.LOW, "low"),
    (ReasoningEffort.MEDIUM, "medium"),
    (ReasoningEffort.HIGH, "high"),
    (ReasoningEffort.XHIGH, "xhigh"),
    (ReasoningEffort.MAX, "xhigh"),
)
_LOW_TO_MAX = (
    (ReasoningEffort.MINIMAL, "low"),
    (ReasoningEffort.LOW, "low"),
    (ReasoningEffort.MEDIUM, "medium"),
    (ReasoningEffort.HIGH, "high"),
    (ReasoningEffort.XHIGH, "max"),
    (ReasoningEffort.MAX, "max"),
)

# The OpenAI Chat Completions standard control, and this file's default.
# ``reasoning_effort`` is defined by the API itself, so an OpenAI-compatible
# host either reads it or ignores it; the third case -- rejecting it -- is what
# ``reasoning_reject.py`` learns from, per model, in one request.
#
# The installed SDK (openai 2.54.0, ``openai/types/shared/reasoning_effort.py``)
# types the field as ``none|minimal|low|medium|high|xhigh|max``, but ``xhigh``,
# ``max`` and ``none`` are recent additions. Whether third-party gateways have
# them was probed on 2026-08-29 against every provider in this file with a
# credential on the machine; the table is in PR #221's body. It settled
# nothing: of the twenty hosts, sixteen had no credential configured at all,
# and the four that did answered before validating the body --
#
#     opencode      deepseek-v4-flash-free  400 on "bogus_value" AND on "medium"
#                                           (per-model upstream refusal, the
#                                           400 names no enum)
#     opencode_go   glm-5.3-flash           401 CreditsError, before validation
#     kimi_coding   kimi-k2-turbo-preview   500 to every request, bare included
#     novita        glm-5.3-flash           403 NOT_ENOUGH_BALANCE, before it
#
# -- so no host named an enum, and the conservative ladder stands: the four
# rungs an OpenAI-compatible host has been able to take since the field
# existed. ``xhigh`` and ``max`` clamp down to ``high``, which is what an
# effort scale is for, and ``none`` is never sent. A host that genuinely takes
# the wider enum declares it (``opencode`` does).
_OPENAI_STANDARD_EFFORTS = (
    (ReasoningEffort.MINIMAL, "minimal"),
    (ReasoningEffort.LOW, "low"),
    (ReasoningEffort.MEDIUM, "medium"),
    (ReasoningEffort.HIGH, "high"),
    (ReasoningEffort.XHIGH, "high"),
    (ReasoningEffort.MAX, "high"),
)

# No ``enabled_value``: an enabled value is a *default rung*, and a level-less
# "on" must not be answered with a level nobody asked for. No
# ``disabled_value``: ``reasoning_effort: "none"`` is part of the same recent
# addition as ``xhigh``, so OFF is spelled by sending nothing, which is what
# gating already does when a dialect has no OFF spelling.
OPENAI_STANDARD_REASONING = NamedEffortReasoning(
    _OPENAI_STANDARD_EFFORTS, origin=ReasoningDialectOrigin.DEFAULT
)


@dataclass(frozen=True, slots=True)
class OpenAIModelPagination:
    """Bounded numbered-pagination metadata for a model-list endpoint."""

    page_param: str = "page"
    first_page: int = 1
    current_page_path: tuple[str, ...] = ("pagination", "current_page")
    total_pages_path: tuple[str, ...] = ("pagination", "total_pages")
    max_pages: int = 100


@dataclass(frozen=True, slots=True)
class OpenAIModelListing:
    """Declarative model-list endpoint and response shape."""

    path: str | None = None
    query_params: tuple[tuple[str, str], ...] = ()
    collection_field: str | None = "data"
    id_field: str = "id"
    aliases_field: str | None = None
    required_path_values: RequiredPathValues = ()
    required_null_field: str | None = None
    required_sequence_items: tuple[tuple[str, str], ...] = ()
    exclude_missing_sequence_fields: bool = False
    tags_field: str | None = None
    thinking_tag: str = "reasoning"
    non_thinking_tag: str | None = None
    thinking_boolean_path: tuple[str, ...] | None = None
    pagination: OpenAIModelPagination | None = None


@dataclass(frozen=True, slots=True)
class OpenAIChatProfile:
    """Immutable transport and reasoning behavior for one provider."""

    request_policy: OpenAIChatRequestPolicy
    reasoning: ReasoningEncoder
    postprocessors: tuple[OpenAIChatPostprocessor, ...] = ()
    model_ids_are_routable: bool = True
    model_listing: OpenAIModelListing = OpenAIModelListing()
    normalize_base_url: bool = False
    reasoning_delta_field: Literal["reasoning_content", "reasoning"] = (
        "reasoning_content"
    )
    reasoning_delta_fallback_field: Literal["reasoning_content", "reasoning"] | None = (
        None
    )
    structured_reasoning_details: bool = False

    @property
    def provider_name(self) -> str:
        return self.request_policy.provider_name

    def base_url(self, configured: str) -> str:
        return openai_v1_base_url(configured) if self.normalize_base_url else configured

    def reasoning_delta(self, delta: Any) -> str | None:
        value = getattr(delta, self.reasoning_delta_field, None)
        if isinstance(value, str) and value:
            return value
        fallback = self.reasoning_delta_fallback_field
        if fallback is None:
            return value if isinstance(value, str) else None
        fallback_value = getattr(delta, fallback, None)
        if isinstance(fallback_value, str):
            return fallback_value
        return value if isinstance(value, str) else None

    def apply_reasoning(
        self,
        body: dict[str, Any],
        _request: MessagesRequest,
        policy: ReasoningPolicy,
    ) -> None:
        self.reasoning.encode(body, policy)

    @property
    def request_postprocessors(self) -> tuple[OpenAIChatPostprocessor, ...]:
        return (*self.postprocessors, self.apply_reasoning)


def _apply_cohere_request_quirks(
    body: dict[str, Any], request: MessagesRequest, _policy: ReasoningPolicy
) -> None:
    _merge_allowed_cohere_extra_body(body, request.extra_body)


_COHERE_EXTRA_BODY_KEYS = frozenset(
    {
        "frequency_penalty",
        "presence_penalty",
        "response_format",
        "seed",
    }
)


def _merge_allowed_cohere_extra_body(body: dict[str, Any], extra_body: Any) -> None:
    if extra_body in (None, {}):
        return
    if not isinstance(extra_body, Mapping):
        raise InvalidRequestError("Cohere extra_body must be an object when provided.")

    unsupported = sorted(
        str(key) for key in extra_body if key not in _COHERE_EXTRA_BODY_KEYS
    )
    if unsupported:
        raise InvalidRequestError(
            "Cohere extra_body supports only these keys: "
            f"{sorted(_COHERE_EXTRA_BODY_KEYS)}. Unsupported: {unsupported}"
        )
    body.update({str(key): deepcopy(value) for key, value in extra_body.items()})


def _policy(
    provider_name: str,
    replay: ReasoningReplayMode,
    **kwargs: Any,
) -> OpenAIChatRequestPolicy:
    return OpenAIChatRequestPolicy(
        provider_name=provider_name,
        reasoning_replay=replay,
        **kwargs,
    )


# Every profile here speaks OpenAI Chat Completions, so every one declares a
# reasoning dialect. ``OPENAI_STANDARD_REASONING`` is the default: the field
# the API itself defines, over the four rungs every implementation has had.
# A profile names its own encoder only where the host was *probed* speaking
# something else -- a ``thinking`` object, a chat-template flag, a wider enum.
# What a *model* supports is not this file's business: the per-model capability
# gate decides whether the declared field is sent at all, which is why the
# 5.70.0 audit's "one encoder cannot be right for a mixed roster" verdicts are
# gone -- that objection was always about models, and models now have their own
# answer. A host that refuses the field says so with a 400, and
# ``reasoning_reject.py`` remembers it per model.
OPENAI_CHAT_PROFILES: dict[str, OpenAIChatProfile] = {
    "mistral_codestral": OpenAIChatProfile(
        _policy(
            "CODESTRAL",
            ReasoningReplayMode.THINK_TAGS,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
        ),
        OPENAI_STANDARD_REASONING,
    ),
    # 2026-08-29: WIRED. OpenCode Zen reasons by default -- 12,068 of 47,152
    # logged requests carried thinking while this profile sent nothing -- which
    # is why it looked healthy. It was not: the gateway *does* parse
    # ``reasoning_effort``, so a client asking for a particular effort, or for
    # reasoning OFF, was silently ignored. A deliberately invalid value is
    # rejected with the enum spelled out, identically across five models on two
    # different upstream stacks: "reasoning_effort: Invalid option: expected one
    # of max|xhigh|high|medium|low|minimal|none". That is exactly FCC's own
    # effort ladder plus a "none" rung, so the mapping is 1:1 and OFF has a real
    # wire value. A top-level ``reasoning`` object, a ``thinking`` object and
    # ``chat_template_kwargs`` are all accepted and silently discarded -- a
    # deliberately invalid ``reasoning: {"effort": "bogus_value"}`` still
    # returns 200 -- so none of those is the dialect.
    "opencode": OpenAIChatProfile(
        _policy(
            "OPENCODE",
            # Reasoning arrives as ``reasoning_content`` deltas, never as
            # ``<think>`` tags in ``content``, so assistant history is replayed
            # through the field it was received on. Probed: an assistant turn
            # carrying ``reasoning_content`` is accepted (HTTP 200).
            ReasoningReplayMode.REASONING_CONTENT,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
        ),
        # No ``enabled_value``, and that is a *finding*, not an omission --
        # OpenCode is the exact inverse of Command Code. A live A/B on
        # 2026-08-29 (identical prompt, ``max_tokens: 3000``, ``hy3-free``):
        #
        #     bare (no reasoning_effort)  HTTP 200  reasoning_tokens=3000
        #     reasoning_effort: "max"     HTTP 200  reasoning_tokens=903
        #     reasoning_effort: "minimal" HTTP 200  reasoning_tokens=836
        #     reasoning_effort: "none"    HTTP 200  reasoning_tokens=0
        #
        # so naming any rung here really does reduce reasoning, and inventing
        # an on-value for a policy that named no level would cost thinking.
        # Worse, the enum is validated by the *gateway* but forwarded to the
        # *model*: ``mimo-v2.5-free`` answers HTTP 400 "Upstream request
        # failed: [400] Invalid request parameters" to every rung, valid ones
        # included, while a bare request returns 200 and reasons. So the field
        # is safe only where per-model capability says the model has effort
        # control -- which is exactly the gate that already stands in front of
        # it, and the reason this profile must not gain a value it would send
        # unconditionally.
        NamedEffortReasoning(_ALL_EFFORTS, disabled_value="none"),
    ),
    "opencode_go": OpenAIChatProfile(
        _policy(
            "OPENCODE_GO",
            ReasoningReplayMode.THINK_TAGS,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
        ),
        OPENAI_STANDARD_REASONING,
    ),
    "vercel": OpenAIChatProfile(
        _policy(
            "VERCEL",
            ReasoningReplayMode.THINK_TAGS,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
        ),
        ReasoningObject(_ALL_EFFORTS),
    ),
    "huggingface": OpenAIChatProfile(
        _policy(
            "HUGGINGFACE",
            ReasoningReplayMode.DISABLED,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
        ),
        OPENAI_STANDARD_REASONING,
    ),
    "cohere": OpenAIChatProfile(
        _policy(
            "COHERE",
            ReasoningReplayMode.REASONING_CONTENT,
            strip_message_names=True,
            unsupported_body_keys=frozenset(
                {
                    "audio",
                    "logit_bias",
                    "metadata",
                    "modalities",
                    "n",
                    "parallel_tool_calls",
                    "prediction",
                    "service_tier",
                    "store",
                    "top_logprobs",
                }
            ),
        ),
        # Cohere documents ``thinking.type`` and ``thinking.token_budget`` as
        # its reasoning controls and publishes no effort vocabulary at all
        # (https://docs.cohere.com/docs/reasoning), so there is one on-value to
        # send and no scale to map onto. The clamp from a lower effort is
        # performed and recorded by capability gating against this encoder's
        # declared dialect (one rung, "high") rather than disappearing here.
        NamedEffortReasoning(
            tuple((effort, "high") for effort in ReasoningEffort),
            disabled_value="none",
            enabled_value="high",
        ),
        postprocessors=(_apply_cohere_request_quirks,),
    ),
    "wafer": OpenAIChatProfile(
        _policy(
            "WAFER",
            ReasoningReplayMode.REASONING_CONTENT,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
            default_max_tokens=ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
        ),
        NamedEffortReasoning(
            _LOW_TO_MAX,
            disabled_value="none",
            enabled_value="high",
        ),
    ),
    "kimi": OpenAIChatProfile(
        _policy(
            "KIMI",
            ReasoningReplayMode.REASONING_CONTENT,
            reject_extra_body_message=(
                "Kimi Chat Completions API does not support caller extra_body on requests."
            ),
            default_max_tokens=ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
        ),
        ThinkingObjectReasoning(
            enabled={"type": "enabled"},
            disabled={"type": "disabled"},
        ),
    ),
    "kimi_coding": OpenAIChatProfile(
        _policy(
            "KIMI_CODING",
            ReasoningReplayMode.REASONING_CONTENT,
            reject_extra_body_message=(
                "Kimi For Coding API does not support caller extra_body on requests."
            ),
            default_max_tokens=ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
        ),
        OPENAI_STANDARD_REASONING,
    ),
    "minimax": OpenAIChatProfile(
        _policy(
            "MINIMAX",
            ReasoningReplayMode.REASONING_CONTENT,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
            default_max_tokens=ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
            max_tokens_field="max_completion_tokens",
        ),
        SPLIT_REASONING_OUTPUT,
    ),
    "cerebras": OpenAIChatProfile(
        _policy(
            "CEREBRAS",
            ReasoningReplayMode.THINK_TAGS,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
            max_tokens_field="max_completion_tokens",
        ),
        NamedEffortReasoning(
            _LOW_MEDIUM_HIGH,
            disabled_value="none",
            enabled_value="medium",
        ),
        reasoning_delta_field="reasoning",
    ),
    "groq": OpenAIChatProfile(
        _policy(
            "GROQ",
            ReasoningReplayMode.REASONING_CONTENT,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
            max_tokens_field="max_completion_tokens",
            strip_message_names=True,
            unsupported_body_keys=frozenset({"logprobs", "logit_bias", "top_logprobs"}),
            normalize_n_to_one=True,
        ),
        NamedEffortReasoning(
            _LOW_MEDIUM_HIGH,
            disabled_value="none",
            enabled_value="medium",
        ),
    ),
    "sambanova": OpenAIChatProfile(
        _policy(
            "SAMBANOVA",
            ReasoningReplayMode.REASONING_CONTENT,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
        ),
        NamedEffortReasoning(
            _LOW_MEDIUM_HIGH,
            enabled_value="medium",
        ),
    ),
    "fireworks": OpenAIChatProfile(
        _policy(
            "FIREWORKS",
            ReasoningReplayMode.REASONING_CONTENT,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
            default_max_tokens=ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
        ),
        EffortOrThinkingBudgetReasoning(
            (
                (ReasoningEffort.MINIMAL, "low"),
                (ReasoningEffort.LOW, "low"),
                (ReasoningEffort.MEDIUM, "medium"),
                (ReasoningEffort.HIGH, "high"),
                (ReasoningEffort.XHIGH, "high"),
                (ReasoningEffort.MAX, "high"),
            ),
            enabled_value="high",
        ),
    ),
    "novita": OpenAIChatProfile(
        _policy(
            "NOVITA",
            ReasoningReplayMode.THINK_TAGS,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
        ),
        OPENAI_STANDARD_REASONING,
    ),
    "cline": OpenAIChatProfile(
        _policy(
            "CLINE",
            ReasoningReplayMode.DISABLED,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
        ),
        OPENAI_STANDARD_REASONING,
        postprocessors=(apply_reasoning_details_replay,),
        model_listing=OpenAIModelListing(
            path="/ai/cline/recommended-models",
            collection_field="clinePass",
        ),
        reasoning_delta_field="reasoning",
        structured_reasoning_details=True,
    ),
    "zai": OpenAIChatProfile(
        _policy(
            "ZAI",
            ReasoningReplayMode.REASONING_CONTENT,
            reject_extra_body_message=(
                "Z.ai Chat Completions API does not support caller extra_body on requests."
            ),
            default_max_tokens=ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
        ),
        ThinkingObjectReasoning(
            enabled={"type": "enabled", "clear_thinking": False},
            disabled={"type": "disabled"},
        ),
    ),
    # TODO(probe): DashScope again -- ``enable_thinking`` rather than an
    # effort enum. See the ``alibaba`` note below.
    "qwencloud": OpenAIChatProfile(
        _policy(
            "QWENCLOUD",
            ReasoningReplayMode.REASONING_CONTENT,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
            default_max_tokens=ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
        ),
        OPENAI_STANDARD_REASONING,
    ),
    "qwencloud_coding": OpenAIChatProfile(
        _policy(
            "QWENCLOUD_CODING",
            ReasoningReplayMode.REASONING_CONTENT,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
        ),
        OPENAI_STANDARD_REASONING,
    ),
    "xai": OpenAIChatProfile(
        _policy(
            "XAI",
            ReasoningReplayMode.REASONING_CONTENT,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
            default_max_tokens=ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
        ),
        OPENAI_STANDARD_REASONING,
        model_listing=OpenAIModelListing(
            path="/language-models",
            collection_field="models",
            aliases_field="aliases",
        ),
    ),
    "together": OpenAIChatProfile(
        _policy(
            "TOGETHER",
            ReasoningReplayMode.REASONING,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
            default_max_tokens=ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
        ),
        OPENAI_STANDARD_REASONING,
        model_listing=OpenAIModelListing(
            path="/models",
            collection_field=None,
            required_path_values=((("type",), ("chat",)),),
        ),
        reasoning_delta_field="reasoning",
    ),
    "deepinfra": OpenAIChatProfile(
        _policy(
            "DEEPINFRA",
            ReasoningReplayMode.REASONING_CONTENT,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
            default_max_tokens=ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
        ),
        NamedEffortReasoning(
            _ALL_EFFORTS,
            disabled_value="none",
            enabled_value="high",
            use_extra_body=True,
        ),
        model_listing=OpenAIModelListing(
            path="https://api.deepinfra.com/models/list",
            collection_field=None,
            id_field="model_name",
            required_path_values=((("reported_type",), ("text-generation",)),),
            required_null_field="deprecated",
            tags_field="tags",
            non_thinking_tag="non-reasoning",
        ),
    ),
    # TODO(probe): SiliconFlow's native control is a numeric
    # ``thinking_budget`` (models.dev states a 128..32768 range on 19 of its
    # 24 reasoning rows, and no effort vocabulary at all). No encoder emits a
    # bare top-level numeric budget under that name yet; the standard field
    # below is a strictly better starting point than nothing.
    "siliconflow": OpenAIChatProfile(
        _policy(
            "SILICONFLOW",
            ReasoningReplayMode.REASONING_CONTENT,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
            default_max_tokens=ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
        ),
        OPENAI_STANDARD_REASONING,
        model_listing=OpenAIModelListing(
            path="/models",
            query_params=(("sub_type", "chat"),),
        ),
    ),
    "nebius": OpenAIChatProfile(
        _policy(
            "NEBIUS",
            ReasoningReplayMode.REASONING_CONTENT,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
            default_max_tokens=ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
        ),
        NamedEffortReasoning(
            _MINIMAL_TO_XHIGH,
            disabled_value="none",
            enabled_value="xhigh",
        ),
        model_listing=OpenAIModelListing(
            path="/models",
            query_params=(("verbose", "true"),),
            required_path_values=((("architecture", "modality"), ("text->text",)),),
        ),
    ),
    "chutes": OpenAIChatProfile(
        _policy(
            "CHUTES",
            ReasoningReplayMode.DISABLED,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
            default_max_tokens=ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
        ),
        OPENAI_STANDARD_REASONING,
        model_listing=OpenAIModelListing(
            path="/models",
            required_sequence_items=(
                ("input_modalities", "text"),
                ("output_modalities", "text"),
                ("supported_features", "tools"),
            ),
            exclude_missing_sequence_fields=True,
            tags_field="supported_features",
        ),
    ),
    "featherless": OpenAIChatProfile(
        _policy(
            "FEATHERLESS",
            ReasoningReplayMode.REASONING_CONTENT,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
            default_max_tokens=ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
        ),
        ChatTemplateReasoning(field="enable_thinking"),
        model_listing=OpenAIModelListing(
            path="/models",
            query_params=(
                ("capabilities", "chat,tool-use"),
                ("available_on_current_plan", "true"),
                ("status", "active"),
                ("per_page", "1000"),
            ),
            required_path_values=(
                (("features", "tool_use"), (True,)),
                (("is_gated",), (False,)),
                (("available_on_current_plan",), (True,)),
            ),
            pagination=OpenAIModelPagination(),
        ),
    ),
    "agnes": OpenAIChatProfile(
        _policy(
            "AGNES",
            ReasoningReplayMode.THINK_TAGS,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
            default_max_tokens=ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
        ),
        ChatTemplateReasoning(field="enable_thinking"),
    ),
    "wandb": OpenAIChatProfile(
        _policy(
            "WANDB",
            ReasoningReplayMode.DISABLED,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
            default_max_tokens=ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
            max_tokens_field="max_completion_tokens",
        ),
        ChatTemplateReasoning(field="enable_thinking"),
        reasoning_delta_field="reasoning",
    ),
    "zenmux": OpenAIChatProfile(
        _policy(
            "ZENMUX",
            ReasoningReplayMode.REASONING,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
            default_max_tokens=ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
            max_tokens_field="max_completion_tokens",
        ),
        ReasoningObject(_MINIMAL_TO_XHIGH),
        postprocessors=(apply_reasoning_details_replay,),
        model_listing=OpenAIModelListing(
            required_sequence_items=(
                ("input_modalities", "text"),
                ("output_modalities", "text"),
            ),
            thinking_boolean_path=("capabilities", "reasoning"),
        ),
        reasoning_delta_field="reasoning",
        reasoning_delta_fallback_field="reasoning_content",
        structured_reasoning_details=True,
    ),
    "bedrock": OpenAIChatProfile(
        _policy(
            "BEDROCK",
            ReasoningReplayMode.THINK_TAGS,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
        ),
        OPENAI_STANDARD_REASONING,
        normalize_base_url=True,
    ),
    "nararoute": OpenAIChatProfile(
        _policy(
            "NARAROUTE",
            ReasoningReplayMode.DISABLED,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
            default_max_tokens=ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
        ),
        NamedEffortReasoning(_LOW_MEDIUM_HIGH, enabled_value="medium"),
    ),
    "tokenrouter": OpenAIChatProfile(
        _policy(
            "TOKENROUTER",
            ReasoningReplayMode.DISABLED,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
        ),
        OPENAI_STANDARD_REASONING,
    ),
    # Alibaba Model Studio speaks OpenAI Chat Completions and streams thinking
    # back as ``reasoning_content``, so reasoning is READ on the return path
    # regardless of what the request asks for.
    # TODO(probe): DashScope's own control is ``enable_thinking``, a toggle plus
    # a numeric budget rather than an effort enum, and the Coding Plan roster
    # proxies third-party models (GLM, Kimi, MiniMax) whose handling of it is
    # unchecked. A declared encoder for it is the right end state; the standard
    # field below is the interim, and ``extra_body`` still passes a user's own
    # control through verbatim.
    "alibaba": OpenAIChatProfile(
        _policy(
            "ALIBABA",
            ReasoningReplayMode.REASONING_CONTENT,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
        ),
        OPENAI_STANDARD_REASONING,
    ),
    "alibaba_cn": OpenAIChatProfile(
        _policy(
            "ALIBABA_CN",
            ReasoningReplayMode.REASONING_CONTENT,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
        ),
        OPENAI_STANDARD_REASONING,
    ),
    "alibaba_coding": OpenAIChatProfile(
        _policy(
            "ALIBABA_CODING",
            ReasoningReplayMode.REASONING_CONTENT,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
        ),
        OPENAI_STANDARD_REASONING,
    ),
    "alibaba_coding_cn": OpenAIChatProfile(
        _policy(
            "ALIBABA_CODING_CN",
            ReasoningReplayMode.REASONING_CONTENT,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
        ),
        OPENAI_STANDARD_REASONING,
    ),
    # Azure OpenAI addresses a *deployment*, and the deployment name is chosen
    # by the customer, so nothing in the request tells FCC whether it is talking
    # to a reasoning model. That is a per-model question with a per-model
    # answer now: the capability gate decides, and a deployment that refuses the
    # standard field answers with a 400 that is learned once.
    #
    # ``max_completion_tokens`` rather than ``max_tokens``: the o-series and
    # gpt-5 deployments reject the older field outright, and Azure accepts the
    # newer one across the Chat Completions models that support either.
    "azure_openai": OpenAIChatProfile(
        _policy(
            "AZURE_OPENAI",
            ReasoningReplayMode.THINK_TAGS,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
            max_tokens_field="max_completion_tokens",
            default_max_tokens=ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
        ),
        OPENAI_STANDARD_REASONING,
    ),
    "ollama_cloud": OpenAIChatProfile(
        _policy(
            "OLLAMA_CLOUD",
            ReasoningReplayMode.REASONING,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
            default_max_tokens=ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
        ),
        NamedEffortReasoning(
            _LOW_TO_MAX,
            disabled_value="none",
            enabled_value="high",
        ),
        reasoning_delta_field="reasoning",
    ),
    "llamacpp": OpenAIChatProfile(
        _policy(
            "LLAMACPP",
            ReasoningReplayMode.THINK_TAGS,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
            default_max_tokens=ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
        ),
        LLAMACPP_REASONING,
        normalize_base_url=True,
    ),
    "ollama": OpenAIChatProfile(
        _policy(
            "OLLAMA",
            ReasoningReplayMode.REASONING,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
            default_max_tokens=ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
        ),
        NamedEffortReasoning(
            _LOW_TO_MAX,
            disabled_value="none",
            enabled_value="high",
        ),
        normalize_base_url=True,
        reasoning_delta_field="reasoning",
    ),
}

# Fallback profile for dynamic custom providers: plain OpenAI-compatible Chat
# Completions with no provider-specific quirks.
# This profile backs every custom and dynamically configured provider, so the
# endpoint behind it is unknown by construction: it may be vLLM, llama.cpp, a
# corporate gateway or a vendor API. It gets the standard field like every
# other OpenAI-compatible host, because the alternative -- a hand-written
# verdict about an endpoint nobody can see -- is a guess either way, and this
# one is self-correcting: a gateway that refuses the field says so with a 400
# and is never asked again for that model. ``include_extra_body`` remains the
# escape hatch for a user who knows their endpoint's own control.
GENERIC_OPENAI_PROFILE = OpenAIChatProfile(
    _policy(
        "CUSTOM",
        ReasoningReplayMode.THINK_TAGS,
        include_extra_body=True,
        extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
    ),
    OPENAI_STANDARD_REASONING,
)
