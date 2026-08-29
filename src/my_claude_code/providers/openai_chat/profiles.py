"""Declarative profiles for ordinary OpenAI-compatible providers."""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

from my_claude_code.application.errors import InvalidRequestError
from my_claude_code.config.constants import ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS
from my_claude_code.core.anthropic import ReasoningReplayMode
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import ReasoningEffort, ReasoningPolicy
from my_claude_code.providers.model_listing import RequiredPathValues

from .base_url import openai_v1_base_url
from .extra_body import validate_extra_body_does_not_override_canonical_fields
from .reasoning import (
    LLAMACPP_REASONING,
    NO_REASONING,
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


# 2026-08-29 NO_REASONING audit. Every profile below that still carries
# ``NO_REASONING`` was checked against, in order: the operator's request log
# (228,104 rows), the models.dev capability index, the provider's own
# ``/models`` payload, and -- where a credential exists -- a live dialect probe
# that sends a deliberately invalid value so the gateway names its own accepted
# enum. ``NO_REASONING`` is a *verdict*, not a default: each one is annotated
# with what was checked and why nothing is sent. Do not re-run this audit
# without new evidence, and do not wire a profile from models.dev alone --
# that index describes what a *model* can do, not what a given gateway parses.
OPENAI_CHAT_PROFILES: dict[str, OpenAIChatProfile] = {
    # 2026-08-29 audit: correctly NO_REASONING. Codestral is Mistral's
    # completion/FIM family; models.dev has no ``mistral_codestral`` bucket at
    # all, and the ``mistral`` bucket reports reasoning on 7 of 34 rows, none of
    # them Codestral. No credential to probe with. There is nothing to request.
    "mistral_codestral": OpenAIChatProfile(
        _policy(
            "CODESTRAL",
            ReasoningReplayMode.THINK_TAGS,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
        ),
        NO_REASONING,
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
    # 2026-08-29 audit: SUSPECTED but unproven, deliberately unchanged. Same
    # host as ``opencode`` (``/zen/go/v1``) and models.dev reports reasoning on
    # all 33 rows of its bucket, so the sibling's ``reasoning_effort`` enum very
    # probably applies here too. It could not be confirmed: the Go roster has no
    # free tier, so every probe -- including the invalid-value probe that makes
    # the sibling name its enum -- is rejected with HTTP 401 CreditsError before
    # any body validation runs. The request log holds 9 rows on one model, all
    # without thinking, which is far too small to separate "never asked" from
    # "did not reason". Wiring this from the sibling's evidence would be a
    # guess, and a wrong encoder 400s a provider that works today. Revisit with
    # credit on the account.
    "opencode_go": OpenAIChatProfile(
        _policy(
            "OPENCODE_GO",
            ReasoningReplayMode.THINK_TAGS,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
        ),
        NO_REASONING,
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
    # 2026-08-29 audit: correctly NO_REASONING. HF Inference Providers routes to
    # many independent third-party backends and models.dev shows no single
    # vocabulary behind it: of 71 rows, 38 publish an empty
    # ``reasoning_options``, 16 publish none at all, and the remainder split
    # between low|medium|high and low|high|max. One encoder cannot be right for
    # that set, and the project forbids branching on model names to pick between
    # them. No credential to probe with; ``extra_body`` passes through for a user
    # who knows their backend.
    "huggingface": OpenAIChatProfile(
        _policy(
            "HUGGINGFACE",
            ReasoningReplayMode.DISABLED,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
        ),
        NO_REASONING,
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
        # performed and recorded by capability gating against
        # ``providers.reasoning_vocabulary`` rather than disappearing here.
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
    # 2026-08-29 audit: SUSPECTED but unprobeable, deliberately unchanged.
    # models.dev reports reasoning on all 4 ``kimi-for-coding`` rows and the
    # sibling ``kimi`` profile above uses a ``thinking`` object, so the same
    # dialect is plausible. It could not be checked: with the operator's
    # KIMI_CODING credential, ``GET /coding/v1/models`` and every
    # ``POST /chat/completions`` variant -- the bare body included -- return
    # HTTP 500 "The server had an error while processing your request", so the
    # endpoint answers nothing at all, valid or invalid. Copying the sibling's
    # ``thinking`` object on that basis would be a guess against a Coding-Plan
    # endpoint already known to differ from the main API elsewhere.
    "kimi_coding": OpenAIChatProfile(
        _policy(
            "KIMI_CODING",
            ReasoningReplayMode.REASONING_CONTENT,
            reject_extra_body_message=(
                "Kimi For Coding API does not support caller extra_body on requests."
            ),
            default_max_tokens=ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
        ),
        NO_REASONING,
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
    # 2026-08-29 audit: unprobeable, deliberately unchanged. Novita hosts 155
    # models from many vendors; models.dev reports reasoning on 57 of 107 rows
    # and their controls disagree (50 publish no options, 23 a bare toggle, 2 a
    # low|medium|high effort), so there is no provider-wide vocabulary to
    # encode. The operator's credential has no balance and Novita bills before
    # it validates -- every dialect, invalid ones included, returns the same
    # HTTP 403 NOT_ENOUGH_BALANCE -- so the invalid-value trick yields nothing
    # here.
    "novita": OpenAIChatProfile(
        _policy(
            "NOVITA",
            ReasoningReplayMode.THINK_TAGS,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
        ),
        NO_REASONING,
    ),
    # 2026-08-29 audit: SUSPECTED but unprobeable, deliberately unchanged. This
    # is the strongest unwired candidate in the table: models.dev publishes an
    # effort vocabulary for all 13 ``cline-pass`` rows, 11 of them the same
    # none|low|medium|high|xhigh set, and the profile already reads
    # OpenRouter-style ``reasoning`` deltas and structured
    # ``reasoning_details``, which is the shape a ``reasoning`` object usually
    # accompanies. What is missing is any evidence of which knob the Cline Pass
    # gateway itself parses: models.dev states model capability, not gateway
    # dialect, and the two differed on every gateway probed in this audit --
    # OpenCode parses only ``reasoning_effort`` and silently discards
    # ``reasoning``; Command Code does the same. There is no CLINE credential on
    # this machine, so nothing can be sent. Probe before wiring.
    "cline": OpenAIChatProfile(
        _policy(
            "CLINE",
            ReasoningReplayMode.DISABLED,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
        ),
        NO_REASONING,
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
    # 2026-08-29 audit: unassessable, deliberately unchanged. models.dev has no
    # ``qwencloud`` bucket and there is no credential to probe with. The
    # DashScope-family control is ``enable_thinking``, which the ``alibaba`` note
    # below explains is rejected outright by some models on these endpoints --
    # the same trade-off applies here, and so does the same resolution.
    "qwencloud": OpenAIChatProfile(
        _policy(
            "QWENCLOUD",
            ReasoningReplayMode.REASONING_CONTENT,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
            default_max_tokens=ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
        ),
        NO_REASONING,
    ),
    # 2026-08-29 audit: unassessable, deliberately unchanged. As ``qwencloud``
    # above -- no models.dev bucket, no credential, and a DashScope
    # ``enable_thinking`` control that 400s on part of the roster.
    "qwencloud_coding": OpenAIChatProfile(
        _policy(
            "QWENCLOUD_CODING",
            ReasoningReplayMode.REASONING_CONTENT,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
        ),
        NO_REASONING,
    ),
    # 2026-08-29 audit: correctly NO_REASONING. models.dev reports reasoning on
    # only 6 of 12 xAI rows, and even among those the vocabulary is not shared:
    # 2 publish low|medium|high|xhigh, 1 publishes low|medium|high and 3 publish
    # nothing. Sending ``reasoning_effort`` unconditionally would 400 every
    # request on the non-reasoning half of the roster, and the project forbids
    # branching on model names to avoid that. No credential to probe with.
    "xai": OpenAIChatProfile(
        _policy(
            "XAI",
            ReasoningReplayMode.REASONING_CONTENT,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
            default_max_tokens=ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
        ),
        NO_REASONING,
        model_listing=OpenAIModelListing(
            path="/language-models",
            collection_field="models",
            aliases_field="aliases",
        ),
    ),
    # 2026-08-29 audit: correctly NO_REASONING. Together is a multi-vendor host:
    # models.dev reports reasoning on 26 of 37 rows and, among those, 11 publish
    # no options, 10 a bare toggle and 3 a high|max effort -- no provider-wide
    # vocabulary to encode. The return path is already correct: this profile
    # reads ``reasoning`` deltas and replays on the same field. No credential to
    # probe with.
    "together": OpenAIChatProfile(
        _policy(
            "TOGETHER",
            ReasoningReplayMode.REASONING,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
            default_max_tokens=ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
        ),
        NO_REASONING,
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
    # 2026-08-29 audit: unprobeable, deliberately unchanged. SiliconFlow's
    # control is a numeric ``thinking_budget``, not an effort enum: of the 24
    # reasoning rows models.dev publishes, 19 state a budget_tokens range
    # (min 128, max 32768) and not one states an effort vocabulary. No existing
    # encoder emits a bare top-level numeric budget under that name --
    # ``EffortOrThinkingBudgetReasoning`` nests it inside a ``thinking`` object
    # and ``LlamaCppReasoning`` uses ``thinking_budget_tokens`` -- and inventing
    # one is outside the scope of an audit. No credential to probe with.
    "siliconflow": OpenAIChatProfile(
        _policy(
            "SILICONFLOW",
            ReasoningReplayMode.REASONING_CONTENT,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
            default_max_tokens=ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
        ),
        NO_REASONING,
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
    # 2026-08-29 audit: correctly NO_REASONING. models.dev reports reasoning on
    # 12 of 14 rows but publishes an effort vocabulary for none of them -- 10 are
    # a bare toggle and the rest state nothing -- so there is no scale to map
    # onto, and a toggle is what the upstream already does by default. Chutes
    # serves open-weight models whose thinking is chat-template driven; the
    # profile passes ``extra_body`` through for a user who knows their model's
    # kwarg. No credential to probe with.
    "chutes": OpenAIChatProfile(
        _policy(
            "CHUTES",
            ReasoningReplayMode.DISABLED,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
            default_max_tokens=ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
        ),
        NO_REASONING,
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
    # 2026-08-29 audit: correctly NO_REASONING. Bedrock fronts 120 models from
    # nine vendors behind one OpenAI-compatible shim; models.dev reports
    # reasoning on 90 of them under mutually incompatible controls (Anthropic
    # budget_tokens, Nova toggles, OpenAI-style efforts). There is no
    # provider-wide knob, and a request addresses an inference profile whose
    # vendor is not derivable without branching on the model id. No credential
    # to probe with.
    "bedrock": OpenAIChatProfile(
        _policy(
            "BEDROCK",
            ReasoningReplayMode.THINK_TAGS,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
        ),
        NO_REASONING,
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
    # 2026-08-29 audit: unassessable, deliberately unchanged. models.dev has no
    # ``tokenrouter`` bucket, TokenRouter publishes no reasoning documentation,
    # and there is no credential on this machine. Nothing can be established
    # either way, so the profile is left exactly as it was.
    "tokenrouter": OpenAIChatProfile(
        _policy(
            "TOKENROUTER",
            ReasoningReplayMode.DISABLED,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
        ),
        NO_REASONING,
    ),
    # Alibaba Model Studio speaks OpenAI Chat Completions and streams thinking
    # back as ``reasoning_content``, so reasoning is READ. It is deliberately not
    # REQUESTED: DashScope's control is ``enable_thinking``, which is rejected
    # outright by some models on these endpoints -- and the Coding Plan roster
    # proxies third-party models (GLM, Kimi, MiniMax) whose handling of it we
    # cannot check without a subscription. An unsent control costs thinking on
    # some models; a wrongly-sent one 400s every request. ``extra_body`` passes
    # through, so a user who knows their model supports it can send it.
    #
    # 2026-08-29 audit: re-confirmed, and now quantified. models.dev reports
    # reasoning on 30 of 55 ``alibaba`` rows, 48 of 87 ``alibaba-cn`` and 9 of 12
    # on each Coding Plan bucket, and the control it publishes for them is a
    # toggle plus a numeric budget -- never an effort enum -- so roughly half of
    # each roster would 400 on an unconditional control and the other half has
    # no scale to map an effort onto. No credential to probe with.
    "alibaba": OpenAIChatProfile(
        _policy(
            "ALIBABA",
            ReasoningReplayMode.REASONING_CONTENT,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
        ),
        NO_REASONING,
    ),
    "alibaba_cn": OpenAIChatProfile(
        _policy(
            "ALIBABA_CN",
            ReasoningReplayMode.REASONING_CONTENT,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
        ),
        NO_REASONING,
    ),
    "alibaba_coding": OpenAIChatProfile(
        _policy(
            "ALIBABA_CODING",
            ReasoningReplayMode.REASONING_CONTENT,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
        ),
        NO_REASONING,
    ),
    "alibaba_coding_cn": OpenAIChatProfile(
        _policy(
            "ALIBABA_CODING_CN",
            ReasoningReplayMode.REASONING_CONTENT,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
        ),
        NO_REASONING,
    ),
    # Azure OpenAI addresses a *deployment*, and the deployment name is chosen
    # by the customer -- so nothing in the request tells FCC whether it is
    # talking to a reasoning model. ``reasoning_effort`` is therefore not sent:
    # omitting it costs thinking on a reasoning deployment, whereas sending it
    # to a non-reasoning one 400s every request. ``extra_body`` passes through
    # for users who know which of theirs is which. Same trade-off, and the same
    # resolution, as the Alibaba profiles above.
    #
    # ``max_completion_tokens`` rather than ``max_tokens``: the o-series and
    # gpt-5 deployments reject the older field outright, and Azure accepts the
    # newer one across the Chat Completions models that support either.
    # 2026-08-29 audit: re-confirmed correct, with a number on the trade-off
    # above -- models.dev reports reasoning on 52 of 84 ``azure`` rows, so
    # roughly a third of a customer's possible deployments would 400 outright on
    # an unconditional ``reasoning_effort``.
    "azure_openai": OpenAIChatProfile(
        _policy(
            "AZURE_OPENAI",
            ReasoningReplayMode.THINK_TAGS,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
            max_tokens_field="max_completion_tokens",
            default_max_tokens=ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
        ),
        NO_REASONING,
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
#
# 2026-08-29 audit: correctly NO_REASONING, and the one profile in this file
# where that is not even a close call. This profile backs every custom and
# dynamically configured provider, so the endpoint behind it is unknown by
# construction: it may be vLLM, llama.cpp, a corporate gateway or a vendor API,
# and the audit above found that even *named* gateways disagree about which
# reasoning knob they parse. Sending any of them here would 400 an unknown
# share of user-configured endpoints in exchange for reasoning on the rest.
# ``include_extra_body`` is the escape hatch: a user who knows their endpoint
# can send its control verbatim.
GENERIC_OPENAI_PROFILE = OpenAIChatProfile(
    _policy(
        "CUSTOM",
        ReasoningReplayMode.THINK_TAGS,
        include_extra_body=True,
        extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
    ),
    NO_REASONING,
)
