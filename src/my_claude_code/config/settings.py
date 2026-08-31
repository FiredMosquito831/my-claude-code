"""Flat application settings schema loaded by Pydantic Settings."""

from functools import lru_cache
from typing import Any

from loguru import logger
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .constants import (
    ANTHROPIC_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
    CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
    CREDENTIAL_LOCKOUT_TIERS_DEFAULT,
    DESKTOP_ACTIVATION_POLL_SECONDS_DEFAULT,
    DESKTOP_ADMIN_REQUEST_TIMEOUT_DEFAULT,
    DESKTOP_HEALTH_CHECK_INTERVAL_DEFAULT,
    DESKTOP_HEALTH_FAILURE_THRESHOLD_DEFAULT,
    DESKTOP_HEALTH_POLL_SECONDS_DEFAULT,
    DESKTOP_SERVER_START_TIMEOUT_DEFAULT,
    DESKTOP_WINDOW_HEIGHT_DEFAULT,
    DESKTOP_WINDOW_WIDTH_DEFAULT,
    FAILURE_KIND_NAMES,
    FALLBACK_ATTEMPT_SHARE_FLOOR_DEFAULT,
    FALLBACK_BEHAVIOR_DEFAULT,
    FALLBACK_BENCH_ENABLED_DEFAULT,
    FALLBACK_COOLDOWN_STEP_OVER_FLOOR_DEFAULT,
    FALLBACK_EJECT_AFTER_FAILURES_DEFAULT,
    FALLBACK_EJECT_FAILURE_RATE_DEFAULT,
    FALLBACK_EJECT_MIN_SAMPLES_DEFAULT,
    FALLBACK_EJECT_SECONDS_DEFAULT,
    FALLBACK_EJECT_WINDOW_DEFAULT,
    FALLBACK_END_CLEANLY_AFTER_COMMIT_DEFAULT,
    FALLBACK_FIRST_TOKEN_TIMEOUT_DEFAULT,
    FALLBACK_ON_REASONING_ONLY_DEFAULT,
    FALLBACK_REASONING_ANSWER_TIMEOUT_DEFAULT,
    FALLBACK_RETRY_FIRST_DEFAULT,
    FALLBACK_SKIP_KINDS_DEFAULT,
    FALLBACK_STALL_TIMEOUT_DEFAULT,
    FALLBACK_TOTAL_TIMEOUT_DEFAULT,
    HTTP_CONNECT_TIMEOUT_DEFAULT,
    MAX_OUTPUT_TOKENS_CEILING,
    MAX_OUTPUT_TOKENS_CONTEXT_FLOOR,
    MAX_OUTPUT_TOKENS_CONTEXT_MARGIN,
    MAX_OUTPUT_TOKENS_UNKNOWN_DEFAULT,
    MODEL_VISIBILITY_ALLOW_DEFAULT,
    MODEL_VISIBILITY_DENY_DEFAULT,
    PROVIDER_RETRY_ATTEMPTS_DEFAULT,
    PROVIDER_RETRY_BACKOFF_BASE_SECONDS_DEFAULT,
    PROVIDER_RETRY_BACKOFF_JITTER_SECONDS_DEFAULT,
    PROVIDER_RETRY_BACKOFF_MAX_SECONDS_DEFAULT,
    RATE_LIMIT_COOLDOWN_SECONDS_DEFAULT,
    REASONING_ANSWER_FLOOR_MAX,
    REQUEST_LOG_COMPRESSION_LEVEL_DEFAULT,
    REQUEST_LOG_IMAGE_MAX_PIXELS_DEFAULT,
    REQUEST_LOG_LADDER_BODY_MAX_CHARS_DEFAULT,
    REQUEST_LOG_QUEUE_MAX_SIZE_DEFAULT,
    REQUEST_LOG_TEXT_MAX_CHARS_DEFAULT,
    REQUEST_LOG_WIRE_BODY_MAX_CHARS_DEFAULT,
    SERVER_GRACEFUL_SHUTDOWN_SECONDS_DEFAULT,
    STREAM_COMMIT_HOLDBACK_SECONDS_DEFAULT,
    STREAM_EARLY_RETRY_ATTEMPTS_DEFAULT,
    STREAM_MIDSTREAM_RECOVERY_ATTEMPTS_DEFAULT,
    TOOL_RESULT_TRIM_KEEP_HEAD_CHARS_DEFAULT,
    TOOL_RESULT_TRIM_KEEP_TAIL_CHARS_DEFAULT,
    TOOL_RESULT_TRIM_PROTECT_RECENT_DEFAULT,
    TOOL_RESULT_TRIM_THRESHOLD_CHARS_DEFAULT,
    TRIM_MODE_NAMES,
)
from .env_files import (
    ANTHROPIC_AUTH_TOKEN_ENV,
    env_file_override,
    settings_env_files,
)
from .limits import LIMIT_RANGES
from .model_refs import format_model_ref_list, parse_model_ref_list
from .nim import NimSettings
from .paths import anthropic_oauth_managed_store_path, chatgpt_oauth_auth_path
from .provider_registry import get_provider_registry
from .reasoning import ReasoningPreference
from .websearch_catalog import SUPPORTED_WEBSEARCH_PROVIDER_IDS

# Settings fields whose validators read a blank value as "not set" rather than
# as a broken value. The admin layer needs the same list: writing ``KEY=`` is
# how the dashboard masks a repo ``.env`` entry without inventing a value, and
# it is only safe for a field that actually accepts the empty string.
BLANK_MEANS_UNSET_FIELDS: tuple[str, ...] = (
    "telegram_bot_token",
    "allowed_telegram_user_id",
    "discord_bot_token",
    "allowed_discord_channels",
    "model_fable",
    "model_opus",
    "model_sonnet",
    "model_haiku",
    "model_vision",
    "model_fallbacks",
    "model_fable_fallbacks",
    "model_opus_fallbacks",
    "model_sonnet_fallbacks",
    "model_haiku_fallbacks",
    "model_vision_fallbacks",
    "ollama_search_api_key",
    "exa_api_key",
    "tavily_api_key",
    "brave_search_api_key",
    "jina_api_key",
    "serper_api_key",
    "firecrawl_api_key",
    "linkup_api_key",
    "perplexity_search_api_key",
    "parallel_api_key",
    "searchapi_api_key",
    "serpapi_api_key",
    "searxng_base_url",
)


def parse_lockout_tiers(value: str) -> tuple[float, ...]:
    """Turn a comma-separated auth lockout ladder into seconds.

    Each entry is one step of the bench a credential earns for a 401/403, so
    every one has to be a positive number and there has to be at least one --
    an empty or zero ladder would silently mean "a rejected key is never
    benched", which is the opposite of what configuring it says.
    """
    parts = [part.strip() for part in (value or "").split(",")]
    kept = [part for part in parts if part]
    if not kept:
        raise ValueError("needs at least one duration in seconds")
    tiers: list[float] = []
    for part in kept:
        try:
            seconds = float(part)
        except ValueError as exc:
            raise ValueError(f"{part!r} is not a number of seconds") from exc
        if seconds <= 0:
            raise ValueError(f"{part!r} must be greater than 0")
        tiers.append(seconds)
    return tuple(tiers)


def _require_provider_prefixed_model_ref(model_ref: str) -> None:
    """Raise when a model ref is not a `provider/model` for a known provider."""

    supported_ids = get_provider_registry().supported_ids()
    if "/" not in model_ref:
        raise ValueError(
            f"Model must be prefixed with provider type. "
            f"Valid providers: {', '.join(supported_ids)}. "
            f"Format: provider_type/model/name"
        )
    provider = model_ref.split("/", 1)[0]
    if provider not in supported_ids:
        supported = ", ".join(f"'{p}'" for p in supported_ids)
        raise ValueError(f"Invalid provider: '{provider}'. Supported: {supported}")


def _no_ceiling_when_zero(value: int | None) -> int | None:
    """Read the output ceiling's 0 sentinel: "let every model's own limit stand".

    Shared by the field validator and by the range clamp, because the clamp
    can produce a 0 the field validator never saw.
    """

    return None if value == 0 else value


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # ==================== OpenRouter Config ====================
    open_router_api_key: str = Field(default="", validation_alias="OPENROUTER_API_KEY")

    # ==================== Mistral La Plateforme ====================
    mistral_api_key: str = Field(default="", validation_alias="MISTRAL_API_KEY")

    # ==================== Mistral Codestral (codestral.mistral.ai) ====================
    codestral_api_key: str = Field(default="", validation_alias="CODESTRAL_API_KEY")

    # ==================== DeepSeek Config ====================
    deepseek_api_key: str = Field(default="", validation_alias="DEEPSEEK_API_KEY")

    # ==================== Kimi Config ====================
    kimi_api_key: str = Field(default="", validation_alias="KIMI_API_KEY")

    # ==================== Kimi For Coding Config ====================
    kimi_coding_api_key: str = Field(default="", validation_alias="KIMI_CODING_API_KEY")

    # ==================== ChatGPT OAuth (experimental) Config ====================
    chatgpt_oauth_access_token: str = Field(
        default="", validation_alias="CHATGPT_OAUTH_ACCESS_TOKEN"
    )
    chatgpt_oauth_account_id: str = Field(
        default="", validation_alias="CHATGPT_OAUTH_ACCOUNT_ID"
    )
    chatgpt_oauth_base_url: str = Field(
        default="", validation_alias="CHATGPT_OAUTH_BASE_URL"
    )

    # ==================== Wafer Config ====================
    wafer_api_key: str = Field(default="", validation_alias="WAFER_API_KEY")

    # ==================== MiniMax Config ====================
    minimax_api_key: str = Field(default="", validation_alias="MINIMAX_API_KEY")

    # ==================== OpenCode Zen / OpenCode Go ====================
    # Same key from opencode.ai/auth; zen uses prefix ``opencode/``, Go uses ``opencode_go/``.
    opencode_api_key: str = Field(default="", validation_alias="OPENCODE_API_KEY")

    # ==================== Vercel AI Gateway ====================
    vercel_ai_gateway_api_key: str = Field(
        default="", validation_alias="AI_GATEWAY_API_KEY"
    )

    # ==================== Hugging Face Inference Providers ====================
    huggingface_api_key: str = Field(default="", validation_alias="HUGGINGFACE_API_KEY")

    # ==================== Cohere Compatibility API ====================
    cohere_api_key: str = Field(default="", validation_alias="COHERE_API_KEY")

    # ==================== GitHub Models ====================
    github_models_token: str = Field(default="", validation_alias="GITHUB_MODELS_TOKEN")

    # ==================== SambaNova Cloud ====================
    sambanova_api_key: str = Field(default="", validation_alias="SAMBANOVA_API_KEY")

    # ==================== Z.ai Config ====================
    zai_api_key: str = Field(default="", validation_alias="ZAI_API_KEY")

    # ==================== Fireworks AI Config ====================
    fireworks_api_key: str = Field(default="", validation_alias="FIREWORKS_API_KEY")

    # ==================== Novita AI Config ====================
    novita_api_key: str = Field(default="", validation_alias="NOVITA_API_KEY")

    # ==================== Anthropic (Claude API) Config ====================
    # A Claude Console API key, billed per token. Distinct from
    # ``ANTHROPIC_AUTH_TOKEN``, which is the token clients present to MCC.
    anthropic_api_key: str = Field(default="", validation_alias="ANTHROPIC_API_KEY")
    # Deliberately NOT ``ANTHROPIC_BASE_URL``: that variable points Claude Code
    # at MCC, so reading it here would make the proxy dial itself and loop.
    anthropic_base_url: str = Field(
        default="", validation_alias="ANTHROPIC_UPSTREAM_BASE_URL"
    )

    # ============ Anthropic Claude subscription (OAuth) ============
    # Optional: the credential is normally discovered from MCC's own store or
    # from ~/.claude/.credentials.json, so this stays empty for most setups.
    anthropic_oauth_access_token: str = Field(
        default="", validation_alias="ANTHROPIC_OAUTH_ACCESS_TOKEN"
    )
    anthropic_oauth_base_url: str = Field(
        default="", validation_alias="ANTHROPIC_OAUTH_UPSTREAM_BASE_URL"
    )
    # Refuse any request that did not come from the Claude Code CLI. Turning
    # this off routes Agent SDK and other harness traffic onto the
    # subscription credential, which is the case Anthropic's policy names
    # explicitly. See docs/ANTHROPIC-SUBSCRIPTION.md.
    anthropic_oauth_require_claude_code: bool = Field(
        default=True, validation_alias="ANTHROPIC_OAUTH_REQUIRE_CLAUDE_CODE"
    )

    # ==================== Nous Portal Config ====================
    nous_api_key: str = Field(default="", validation_alias="NOUS_API_KEY")

    # ==================== Kilo AI Gateway Config ====================
    kilo_api_key: str = Field(default="", validation_alias="KILO_API_KEY")

    # ==================== Command Code Provider API ====================
    commandcode_api_key: str = Field(default="", validation_alias="COMMANDCODE_API_KEY")

    # ==================== Cline Config ====================
    cline_api_key: str = Field(default="", validation_alias="CLINE_API_KEY")

    # ==================== Alibaba Cloud Model Studio Config ====================
    # Four separate providers because both the plan and the region change the
    # credential: a Coding Plan key is ``sk-sp-`` prefixed and rejected by the
    # pay-per-token endpoints, and a key issued in one region is not valid in
    # the other. Base URLs are overridable for workspace-scoped or US regions.
    alibaba_api_key: str = Field(default="", validation_alias="ALIBABA_API_KEY")
    alibaba_base_url: str = Field(default="", validation_alias="ALIBABA_BASE_URL")
    alibaba_cn_api_key: str = Field(default="", validation_alias="ALIBABA_CN_API_KEY")
    alibaba_cn_base_url: str = Field(default="", validation_alias="ALIBABA_CN_BASE_URL")
    alibaba_coding_api_key: str = Field(
        default="", validation_alias="ALIBABA_CODING_API_KEY"
    )
    alibaba_coding_base_url: str = Field(
        default="", validation_alias="ALIBABA_CODING_BASE_URL"
    )
    alibaba_coding_cn_api_key: str = Field(
        default="", validation_alias="ALIBABA_CODING_CN_API_KEY"
    )
    alibaba_coding_cn_base_url: str = Field(
        default="", validation_alias="ALIBABA_CODING_CN_BASE_URL"
    )

    # ==================== Cloudflare Workers AI Config ====================
    cloudflare_api_token: str = Field(
        default="", validation_alias="CLOUDFLARE_API_TOKEN"
    )
    cloudflare_account_id: str = Field(
        default="", validation_alias="CLOUDFLARE_ACCOUNT_ID"
    )

    # ==================== Google Gemini (Google AI Studio) ====================
    gemini_api_key: str = Field(default="", validation_alias="GEMINI_API_KEY")

    # ==================== Google Vertex AI ====================
    # Authentication uses Application Default Credentials (ADC); there is no
    # API key. ``vertex_project_id`` and ``vertex_location`` select the endpoint.
    vertex_project_id: str = Field(default="", validation_alias="VERTEX_PROJECT_ID")
    vertex_location: str = Field(default="global", validation_alias="VERTEX_LOCATION")
    vertex_base_url: str = Field(default="", validation_alias="VERTEX_BASE_URL")

    # ==================== Groq (OpenAI-compatible) ====================
    groq_api_key: str = Field(default="", validation_alias="GROQ_API_KEY")

    # ==================== Cerebras Inference (OpenAI-compatible) ====================
    cerebras_api_key: str = Field(default="", validation_alias="CEREBRAS_API_KEY")

    # ==================== Ollama Cloud ====================
    ollama_api_key: str = Field(default="", validation_alias="OLLAMA_API_KEY")

    # ==================== Azure OpenAI (v1 API) ====================
    # The endpoint is per-resource, so there is no default base URL to ship:
    # AZURE_OPENAI_BASE_URL must name your own resource. ``model`` is the
    # deployment name you chose in Azure, not the underlying model name.
    azure_openai_api_key: str = Field(
        default="", validation_alias="AZURE_OPENAI_API_KEY"
    )
    azure_openai_base_url: str = Field(
        default="", validation_alias="AZURE_OPENAI_BASE_URL"
    )

    # ==================== Amazon Bedrock Mantle ====================
    bedrock_api_key: str = Field(
        default="", validation_alias="AWS_BEARER_TOKEN_BEDROCK"
    )
    bedrock_base_url: str = Field(
        default="https://bedrock-mantle.us-east-1.api.aws/v1",
        validation_alias="BEDROCK_BASE_URL",
    )

    # ==================== TokenRouter Config ====================
    tokenrouter_api_key: str = Field(default="", validation_alias="TOKENROUTER_API_KEY")
    tokenrouter_base_url: str = Field(
        default="https://api.tokenrouter.com/v1",
        validation_alias="TOKENROUTER_BASE_URL",
    )

    # ==================== NaraRoute Config ====================
    nararoute_api_key: str = Field(default="", validation_alias="NARAROUTE_API_KEY")
    nararoute_base_url: str = Field(
        default="https://router.bynara.id/v1",
        validation_alias="NARAROUTE_BASE_URL",
    )

    # ==================== QwenCloud Token Plan (OpenAI-compatible) ====================
    qwencloud_api_key: str = Field(default="", validation_alias="QWENCLOUD_API_KEY")

    # ==================== QwenCloud Coding Plan (OpenAI-compatible) ====================
    qwencloud_coding_api_key: str = Field(
        default="", validation_alias="QWENCLOUD_CODING_API_KEY"
    )

    # ==================== Agnes AI (OpenAI-compatible) ====================
    agnes_api_key: str = Field(default="", validation_alias="AGNES_API_KEY")

    # ==================== ZenMux (OpenAI-compatible) ====================
    zenmux_api_key: str = Field(default="", validation_alias="ZENMUX_API_KEY")

    # ==================== W&B Inference (OpenAI-compatible) ====================
    wandb_api_key: str = Field(default="", validation_alias="WANDB_API_KEY")

    # ==================== xAI (Grok) ====================
    xai_api_key: str = Field(default="", validation_alias="XAI_API_KEY")

    # ==================== Together AI ====================
    together_api_key: str = Field(default="", validation_alias="TOGETHER_API_KEY")

    # ==================== DeepInfra ====================
    deepinfra_api_key: str = Field(default="", validation_alias="DEEPINFRA_API_KEY")

    # ==================== SiliconFlow ====================
    siliconflow_api_key: str = Field(default="", validation_alias="SILICONFLOW_API_KEY")

    # ==================== Nebius Token Factory ====================
    nebius_api_key: str = Field(default="", validation_alias="NEBIUS_API_KEY")

    # ==================== Chutes ====================
    chutes_api_key: str = Field(default="", validation_alias="CHUTES_API_KEY")

    # ==================== Featherless AI ====================
    featherless_api_key: str = Field(default="", validation_alias="FEATHERLESS_API_KEY")

    # ==================== Messaging Platform Selection ====================
    # Valid: "telegram" | "discord" | "none"
    messaging_platform: str = Field(
        default="discord", validation_alias="MESSAGING_PLATFORM"
    )
    messaging_rate_limit: int = Field(
        default=1, validation_alias="MESSAGING_RATE_LIMIT"
    )
    messaging_rate_window: float = Field(
        default=1.0, validation_alias="MESSAGING_RATE_WINDOW"
    )

    # ==================== NVIDIA NIM Config ====================
    nvidia_nim_api_key: str = ""

    # ==================== LM Studio Config ====================
    lm_studio_base_url: str = Field(
        default="http://localhost:1234/v1",
        validation_alias="LM_STUDIO_BASE_URL",
    )

    # ==================== Llama.cpp Config ====================
    llamacpp_base_url: str = Field(
        default="http://localhost:8080/v1",
        validation_alias="LLAMACPP_BASE_URL",
    )

    # ==================== Ollama Config ====================
    ollama_base_url: str = Field(
        default="http://localhost:11434",
        validation_alias="OLLAMA_BASE_URL",
    )

    # ==================== Model ====================
    # All Claude model requests are mapped to this single model (fallback)
    # Format: provider_type/model/name
    model: str = "nvidia_nim/nvidia/nemotron-3-super-120b-a12b"

    # Per-model overrides (optional, falls back to MODEL)
    # Each can use a different provider
    model_fable: str | None = Field(default=None, validation_alias="MODEL_FABLE")
    model_opus: str | None = Field(default=None, validation_alias="MODEL_OPUS")
    model_sonnet: str | None = Field(default=None, validation_alias="MODEL_SONNET")
    model_haiku: str | None = Field(default=None, validation_alias="MODEL_HAIKU")

    # Ordered fallback chains, comma-separated `provider/model` refs. A route
    # uses its own chain when it has its own primary override, otherwise the
    # root MODEL chain; the two are never merged, so what a route will try is
    # exactly the primary plus the chain sitting next to it.
    model_fallbacks: str | None = Field(
        default=None, validation_alias="MODEL_FALLBACKS"
    )
    model_fable_fallbacks: str | None = Field(
        default=None, validation_alias="MODEL_FABLE_FALLBACKS"
    )
    model_opus_fallbacks: str | None = Field(
        default=None, validation_alias="MODEL_OPUS_FALLBACKS"
    )
    model_sonnet_fallbacks: str | None = Field(
        default=None, validation_alias="MODEL_SONNET_FALLBACKS"
    )
    model_haiku_fallbacks: str | None = Field(
        default=None, validation_alias="MODEL_HAIKU_FALLBACKS"
    )

    # Vision adapter: serves requests carrying images when the model a route
    # resolved to is known not to accept them.
    model_vision: str | None = Field(default=None, validation_alias="MODEL_VISION")
    # The adapter is a route like any other, so it gets the same safety net:
    # one unreachable vision model must not lose every image on the machine.
    model_vision_fallbacks: str | None = Field(
        default=None, validation_alias="MODEL_VISION_FALLBACKS"
    )

    # ==================== Model visibility ====================
    # Comma-separated globs matched case-insensitively against the full
    # `provider/model` ref. Empty allow means "list everything"; deny is
    # applied after allow and wins. These hide models from `/v1/models` and
    # from the Admin pickers only -- routing never consults them, so a hidden
    # model named in MODEL or a fallback chain still serves requests.
    model_visibility_allow: str = Field(
        default=MODEL_VISIBILITY_ALLOW_DEFAULT,
        validation_alias="MODEL_VISIBILITY_ALLOW",
    )
    model_visibility_deny: str = Field(
        default=MODEL_VISIBILITY_DENY_DEFAULT,
        validation_alias="MODEL_VISIBILITY_DENY",
    )

    # ==================== Output tokens ====================
    # What one request may ask the routed model to generate. The model's own
    # published limit governs whenever a source has one; these only cover
    # the cases where it does not, or where the operator wants a hard stop.
    #
    # Used only when nothing published an output limit for the routed model.
    # A fallback for a missing client value, never a cap on a present one:
    # capping an explicit request against a number nobody published would be an
    # invented limit.
    max_output_tokens_unknown_default: int = Field(
        default=MAX_OUTPUT_TOKENS_UNKNOWN_DEFAULT,
        validation_alias="MAX_OUTPUT_TOKENS_UNKNOWN_DEFAULT",
    )
    # An absolute head on one answer, shipped set because a thinking turn now
    # asks for the routed model's own maximum rather than for the client's
    # answer-sized ask. It never raises a model above its own published limit,
    # and 0 lifts it entirely.
    max_output_tokens_ceiling: int | None = Field(
        default=MAX_OUTPUT_TOKENS_CEILING,
        validation_alias="MAX_OUTPUT_TOKENS_CEILING",
    )
    # Tokens reserved for the prompt when the model's output limit is large
    # enough to swallow its own context window.
    max_output_tokens_context_margin: int = Field(
        default=MAX_OUTPUT_TOKENS_CONTEXT_MARGIN,
        validation_alias="MAX_OUTPUT_TOKENS_CONTEXT_MARGIN",
    )
    # Smallest budget that bounding by remaining context may produce. Below it
    # the request is sent unmodified so the provider names the real context
    # error, rather than succeeding with a max_tokens too small to answer with.
    max_output_tokens_context_floor: int = Field(
        default=MAX_OUTPUT_TOKENS_CONTEXT_FLOOR,
        validation_alias="MAX_OUTPUT_TOKENS_CONTEXT_FLOOR",
    )
    # Most tokens ever held back from the output allowance for the visible
    # answer when thinking is on. Applied as
    # min(this, effective_output // 2), so it never starves a small model.
    reasoning_answer_floor_max: int = Field(
        default=REASONING_ANSWER_FLOOR_MAX,
        validation_alias="REASONING_ANSWER_FLOOR_MAX",
    )

    # ==================== Fallback timing ====================
    # A chain can only rescue a request while nothing has been sent to the
    # client, so a stalled model has to be declared stalled inside that window.
    # Without a deadline the only thing that ends a silent attempt is the
    # transport read timeout, minutes later and long past any use.
    #
    # Seconds to wait for a model's first output before giving the next model
    # on the chain a turn. Nothing has been streamed yet, so this is invisible
    # to the client. 0 disables it.
    fallback_first_token_timeout: float = Field(
        default=FALLBACK_FIRST_TOKEN_TIMEOUT_DEFAULT,
        validation_alias="FALLBACK_FIRST_TOKEN_TIMEOUT",
    )
    # Whole-request budget covering every attempt, retry and recovery. This is
    # the backstop for a stream that already committed and then stalled: no
    # chain can replace it, but it should still end. 0 disables it.
    fallback_total_timeout: float = Field(
        default=FALLBACK_TOTAL_TIMEOUT_DEFAULT,
        validation_alias="FALLBACK_TOTAL_TIMEOUT",
    )
    fallback_stall_timeout: float = Field(
        default=FALLBACK_STALL_TIMEOUT_DEFAULT,
        validation_alias="FALLBACK_STALL_TIMEOUT",
    )
    # Smallest first-token allowance the equal-share division may produce. The
    # share is what stops one model draining the budget, but on a long chain it
    # shrank below the deadline the operator had configured and silently
    # replaced it: 600s over eight models is 75s, and the log said so. With
    # this floor the number in the box is the number a silent model gets.
    # 0 restores pure equal-share.
    fallback_attempt_share_floor: float = Field(
        default=FALLBACK_ATTEMPT_SHARE_FLOOR_DEFAULT,
        validation_alias="FALLBACK_ATTEMPT_SHARE_FLOOR",
    )

    # Comma-separated FailureKind values. Empty means "fall back on every
    # failure", which is the literal reading of what a chain is for; the
    # default excludes only a malformed request, which no model can serve.
    fallback_skip_kinds: str = Field(
        default=FALLBACK_SKIP_KINDS_DEFAULT,
        validation_alias="FALLBACK_SKIP_KINDS",
    )

    # Whether the route-level bench runs at all. When on, the configured eject
    # mode (consecutive or rate-based), the provider rate-limit skip and the
    # kind-aware bench durations all apply to the models on a route. When off,
    # every model in the chain is tried on every request.
    #
    # It shipped off in 5.58.0-5.60.0, on from 5.61.0, and off again here.
    # What changed the answer is what feeds it: the bench counted every
    # failure, including request-shaped ones, so a prompt larger than any
    # model's context ejected the whole chain and the request was answered by
    # whichever model was left holding the 400. Benching now counts only
    # model-shaped failures (see ``failure_counts_toward_bench``), but a guard
    # that removes capacity is opt-in rather than something an install
    # inherits.
    #
    # A default only applies where the key is unset: an install whose managed
    # .env already carries FALLBACK_BENCH_ENABLED=true keeps benching on until
    # the line is removed (the field's "set here" chip says so).
    fallback_bench_enabled: bool = Field(
        default=FALLBACK_BENCH_ENABLED_DEFAULT,
        validation_alias="FALLBACK_BENCH_ENABLED",
    )
    fallback_eject_after_failures: int = Field(
        default=FALLBACK_EJECT_AFTER_FAILURES_DEFAULT,
        validation_alias="FALLBACK_EJECT_AFTER_FAILURES",
    )
    fallback_eject_seconds: float = Field(
        default=FALLBACK_EJECT_SECONDS_DEFAULT,
        validation_alias="FALLBACK_EJECT_SECONDS",
    )

    # Eject mode. ``rate_based`` (default) benches a model when the failure
    # rate over the last ``fallback_eject_window`` requests crosses
    # ``fallback_eject_failure_rate`` (with at least
    # ``fallback_eject_min_samples`` observed so the rate is meaningful),
    # for ``fallback_eject_seconds``. ``legacy`` preserves the historical
    # consecutive-count behavior keyed on ``FALLBACK_EJECT_AFTER_FAILURES``
    # and ``FALLBACK_EJECT_SECONDS``.
    fallback_behavior: str = Field(
        default=FALLBACK_BEHAVIOR_DEFAULT,
        validation_alias="FALLBACK_BEHAVIOR",
    )
    # Window size (in requests) for the rate-based eject math.
    fallback_eject_window: int = Field(
        default=FALLBACK_EJECT_WINDOW_DEFAULT,
        validation_alias="FALLBACK_EJECT_WINDOW",
    )
    # Failure-rate threshold (0.0-1.0) for rate-based eject.
    fallback_eject_failure_rate: float = Field(
        default=FALLBACK_EJECT_FAILURE_RATE_DEFAULT,
        validation_alias="FALLBACK_EJECT_FAILURE_RATE",
    )
    # Minimum number of requests observed before the rate is evaluated.
    # Prevents a single failure on a low-traffic model from tripping it.
    fallback_eject_min_samples: int = Field(
        default=FALLBACK_EJECT_MIN_SAMPLES_DEFAULT,
        validation_alias="FALLBACK_EJECT_MIN_SAMPLES",
    )
    # Whether the chain retries a failed primary once before falling back.
    # ``skip`` (default) moves on immediately; ``retry_once`` gives the
    # primary one more chance for transient errors (timeouts, 5xx, 429)
    # before falling through. Applies only to position 0 (the primary);
    # already-failed fallbacks are not retried.
    fallback_retry_first: str = Field(
        default=FALLBACK_RETRY_FIRST_DEFAULT,
        validation_alias="FALLBACK_RETRY_FIRST",
    )

    # How many times one model is retried on a 429 or 5xx before the chain
    # moves on. Each retry waits longer than the last, so this is the delay a
    # healthy fallback waits behind.
    provider_retry_attempts: int = Field(
        default=PROVIDER_RETRY_ATTEMPTS_DEFAULT,
        validation_alias="PROVIDER_RETRY_ATTEMPTS",
    )
    # Retries inside one provider before the failure reaches routing at all.
    stream_early_retry_attempts: int = Field(
        default=STREAM_EARLY_RETRY_ATTEMPTS_DEFAULT,
        validation_alias="STREAM_EARLY_RETRY_ATTEMPTS",
    )
    # After output has started and the connection drops, how many times the same
    # model is asked to finish. No chain can help here, so this bounds how long
    # a dying stream may hold a request.
    stream_midstream_recovery_attempts: int = Field(
        default=STREAM_MIDSTREAM_RECOVERY_ATTEMPTS_DEFAULT,
        validation_alias="STREAM_MIDSTREAM_RECOVERY_ATTEMPTS",
    )
    # How long the first output is held before it commits. Raising it widens the
    # window in which a failure can still fall back invisibly, at the cost of
    # exactly that much time-to-first-token. 0 commits immediately, which
    # disables invisible recovery entirely.
    fallback_reasoning_answer_timeout: float = Field(
        default=FALLBACK_REASONING_ANSWER_TIMEOUT_DEFAULT,
        validation_alias="FALLBACK_REASONING_ANSWER_TIMEOUT",
        description=(
            "Seconds a model may think before the route stops waiting for it "
            "to start answering."
        ),
    )
    fallback_on_reasoning_only: bool = Field(
        default=FALLBACK_ON_REASONING_ONLY_DEFAULT,
        validation_alias="FALLBACK_ON_REASONING_ONLY",
        description=(
            "Treat a stream that has emitted only reasoning as uncommitted, so "
            "a model that thinks without ever answering falls back."
        ),
    )
    fallback_end_cleanly_after_commit: bool = Field(
        default=FALLBACK_END_CLEANLY_AFTER_COMMIT_DEFAULT,
        validation_alias="FALLBACK_END_CLEANLY_AFTER_COMMIT",
        description=(
            "End a stream that has already reached the client as a valid, "
            "truncated message when it fails, instead of an API error."
        ),
    )
    stream_commit_holdback_seconds: float = Field(
        default=STREAM_COMMIT_HOLDBACK_SECONDS_DEFAULT,
        validation_alias="STREAM_COMMIT_HOLDBACK_SECONDS",
    )
    # Applied only when a rate-limited provider sends no Retry-After header.
    rate_limit_cooldown_seconds: float = Field(
        default=RATE_LIMIT_COOLDOWN_SECONDS_DEFAULT,
        validation_alias="RATE_LIMIT_COOLDOWN_SECONDS",
    )
    # The escalating bench for a key the provider keeps rejecting with
    # 401/403. Comma-separated seconds, walked by consecutive auth failures
    # and clamped at the last entry. Nothing else moves a key's health.
    credential_lockout_tiers: str = Field(
        default=CREDENTIAL_LOCKOUT_TIERS_DEFAULT,
        validation_alias="CREDENTIAL_LOCKOUT_TIERS",
    )
    # Backoff between a provider's own retries of a 429 or 5xx.
    provider_retry_backoff_base_seconds: float = Field(
        default=PROVIDER_RETRY_BACKOFF_BASE_SECONDS_DEFAULT,
        validation_alias="PROVIDER_RETRY_BACKOFF_BASE_SECONDS",
    )
    provider_retry_backoff_max_seconds: float = Field(
        default=PROVIDER_RETRY_BACKOFF_MAX_SECONDS_DEFAULT,
        validation_alias="PROVIDER_RETRY_BACKOFF_MAX_SECONDS",
    )
    provider_retry_backoff_jitter_seconds: float = Field(
        default=PROVIDER_RETRY_BACKOFF_JITTER_SECONDS_DEFAULT,
        validation_alias="PROVIDER_RETRY_BACKOFF_JITTER_SECONDS",
    )
    # Shortest cooldown worth routing a model around rather than waiting out.
    fallback_cooldown_step_over_floor: float = Field(
        default=FALLBACK_COOLDOWN_STEP_OVER_FLOOR_DEFAULT,
        validation_alias="FALLBACK_COOLDOWN_STEP_OVER_FLOOR",
    )
    # Longest text stored per field; longer text is truncated, which also bounds
    # what content search can ever find.
    request_log_text_max_chars: int = Field(
        default=REQUEST_LOG_TEXT_MAX_CHARS_DEFAULT,
        validation_alias="REQUEST_LOG_TEXT_MAX_CHARS",
    )
    # zstd level for stored bodies. Measured on a real log, level 19 was 4.9%
    # smaller than 9 at a ninth of the speed.
    request_log_compression_level: int = Field(
        default=REQUEST_LOG_COMPRESSION_LEVEL_DEFAULT,
        validation_alias="REQUEST_LOG_COMPRESSION_LEVEL",
    )
    # Pending writes held in memory. When this fills, records are dropped.
    request_log_queue_max_size: int = Field(
        default=REQUEST_LOG_QUEUE_MAX_SIZE_DEFAULT,
        validation_alias="REQUEST_LOG_QUEUE_MAX_SIZE",
    )

    # ==================== Per-Provider Proxy ====================
    nvidia_nim_proxy: str = Field(default="", validation_alias="NVIDIA_NIM_PROXY")
    open_router_proxy: str = Field(default="", validation_alias="OPENROUTER_PROXY")
    mistral_proxy: str = Field(default="", validation_alias="MISTRAL_PROXY")
    codestral_proxy: str = Field(default="", validation_alias="CODESTRAL_PROXY")
    lmstudio_proxy: str = Field(default="", validation_alias="LMSTUDIO_PROXY")
    llamacpp_proxy: str = Field(default="", validation_alias="LLAMACPP_PROXY")
    kimi_proxy: str = Field(default="", validation_alias="KIMI_PROXY")
    kimi_coding_proxy: str = Field(default="", validation_alias="KIMI_CODING_PROXY")
    chatgpt_oauth_proxy: str = Field(default="", validation_alias="CHATGPT_OAUTH_PROXY")
    wafer_proxy: str = Field(default="", validation_alias="WAFER_PROXY")
    minimax_proxy: str = Field(default="", validation_alias="MINIMAX_PROXY")
    opencode_proxy: str = Field(default="", validation_alias="OPENCODE_PROXY")
    opencode_go_proxy: str = Field(default="", validation_alias="OPENCODE_GO_PROXY")
    vercel_ai_gateway_proxy: str = Field(
        default="", validation_alias="VERCEL_AI_GATEWAY_PROXY"
    )
    huggingface_proxy: str = Field(default="", validation_alias="HUGGINGFACE_PROXY")
    cohere_proxy: str = Field(default="", validation_alias="COHERE_PROXY")
    github_models_proxy: str = Field(default="", validation_alias="GITHUB_MODELS_PROXY")
    sambanova_proxy: str = Field(default="", validation_alias="SAMBANOVA_PROXY")
    zai_proxy: str = Field(default="", validation_alias="ZAI_PROXY")
    fireworks_proxy: str = Field(default="", validation_alias="FIREWORKS_PROXY")
    novita_proxy: str = Field(default="", validation_alias="NOVITA_PROXY")
    nous_proxy: str = Field(default="", validation_alias="NOUS_PROXY")
    kilo_proxy: str = Field(default="", validation_alias="KILO_PROXY")
    anthropic_proxy: str = Field(default="", validation_alias="ANTHROPIC_PROXY")
    anthropic_oauth_proxy: str = Field(
        default="", validation_alias="ANTHROPIC_OAUTH_PROXY"
    )
    commandcode_proxy: str = Field(default="", validation_alias="COMMANDCODE_PROXY")
    cline_proxy: str = Field(default="", validation_alias="CLINE_PROXY")
    alibaba_proxy: str = Field(default="", validation_alias="ALIBABA_PROXY")
    alibaba_cn_proxy: str = Field(default="", validation_alias="ALIBABA_CN_PROXY")
    alibaba_coding_proxy: str = Field(
        default="", validation_alias="ALIBABA_CODING_PROXY"
    )
    alibaba_coding_cn_proxy: str = Field(
        default="", validation_alias="ALIBABA_CODING_CN_PROXY"
    )
    cloudflare_proxy: str = Field(default="", validation_alias="CLOUDFLARE_PROXY")
    deepseek_proxy: str = Field(default="", validation_alias="DEEPSEEK_PROXY")
    azure_openai_proxy: str = Field(default="", validation_alias="AZURE_OPENAI_PROXY")
    gemini_proxy: str = Field(default="", validation_alias="GEMINI_PROXY")
    vertex_proxy: str = Field(default="", validation_alias="VERTEX_PROXY")
    openai_proxy: str = Field(default="", validation_alias="OPENAI_PROXY")
    groq_proxy: str = Field(default="", validation_alias="GROQ_PROXY")
    cerebras_proxy: str = Field(default="", validation_alias="CEREBRAS_PROXY")
    ollama_cloud_proxy: str = Field(default="", validation_alias="OLLAMA_CLOUD_PROXY")
    qwencloud_proxy: str = Field(default="", validation_alias="QWENCLOUD_PROXY")
    qwencloud_coding_proxy: str = Field(
        default="", validation_alias="QWENCLOUD_CODING_PROXY"
    )
    agnes_proxy: str = Field(default="", validation_alias="AGNES_PROXY")
    zenmux_proxy: str = Field(default="", validation_alias="ZENMUX_PROXY")
    wandb_proxy: str = Field(default="", validation_alias="WANDB_PROXY")
    bedrock_proxy: str = Field(default="", validation_alias="BEDROCK_PROXY")
    tokenrouter_proxy: str = Field(default="", validation_alias="TOKENROUTER_PROXY")
    nararoute_proxy: str = Field(default="", validation_alias="NARAROUTE_PROXY")
    xai_proxy: str = Field(default="", validation_alias="XAI_PROXY")
    together_proxy: str = Field(default="", validation_alias="TOGETHER_PROXY")
    deepinfra_proxy: str = Field(default="", validation_alias="DEEPINFRA_PROXY")
    siliconflow_proxy: str = Field(default="", validation_alias="SILICONFLOW_PROXY")
    nebius_proxy: str = Field(default="", validation_alias="NEBIUS_PROXY")
    chutes_proxy: str = Field(default="", validation_alias="CHUTES_PROXY")
    featherless_proxy: str = Field(default="", validation_alias="FEATHERLESS_PROXY")

    # ==================== Provider Rate Limiting ====================
    provider_rate_limit: int = Field(default=40, validation_alias="PROVIDER_RATE_LIMIT")
    provider_rate_window: int = Field(
        default=60, validation_alias="PROVIDER_RATE_WINDOW"
    )
    provider_max_concurrency: int = Field(
        default=5, validation_alias="PROVIDER_MAX_CONCURRENCY"
    )
    reasoning_policy: ReasoningPreference = Field(
        default=ReasoningPreference.CLIENT,
        validation_alias="REASONING_POLICY",
    )
    reasoning_fable: ReasoningPreference = Field(
        default=ReasoningPreference.INHERIT,
        validation_alias="REASONING_FABLE",
    )
    reasoning_opus: ReasoningPreference = Field(
        default=ReasoningPreference.INHERIT,
        validation_alias="REASONING_OPUS",
    )
    reasoning_sonnet: ReasoningPreference = Field(
        default=ReasoningPreference.INHERIT,
        validation_alias="REASONING_SONNET",
    )
    reasoning_haiku: ReasoningPreference = Field(
        default=ReasoningPreference.INHERIT,
        validation_alias="REASONING_HAIKU",
    )

    # ==================== HTTP Client Timeouts ====================
    http_read_timeout: float = Field(
        default=120.0, validation_alias="HTTP_READ_TIMEOUT"
    )
    http_write_timeout: float = Field(
        default=10.0, validation_alias="HTTP_WRITE_TIMEOUT"
    )
    http_connect_timeout: float = Field(
        default=HTTP_CONNECT_TIMEOUT_DEFAULT,
        validation_alias="HTTP_CONNECT_TIMEOUT",
    )

    # ==================== Optimizations ====================
    enable_title_generation_skip: bool = True
    enable_suggestion_mode_skip: bool = True
    # Answer a client harness's tiny startup "Say OK" reachability probe
    # locally instead of spending a real upstream call. The reply echoes the
    # routed model, so a routing substitution is still detected; upstream
    # liveness is proven by the run's first real request.
    enable_probe_auto_response: bool = True

    # ==================== Tool-Result Trimming (Read / Grep / Glob) ==========
    # Off by default, and deliberately so: this layer is the only thing in the
    # proxy that changes what the model is allowed to see. A fresh install must
    # produce a byte-identical request, which is what the master switch below
    # guarantees -- it beats every per-rule mode, so there is one way to turn
    # the whole layer off and no partial states.
    enable_tool_result_trimming: bool = Field(
        default=False, validation_alias="ENABLE_TOOL_RESULT_TRIMMING"
    )
    # Each rule is off / observe / on. `observe` is the safety story: it records
    # what the rule would have removed from real traffic while changing nothing
    # on the wire, so a rule can be watched before it is trusted.
    tool_result_trim_read: str = Field(
        default="off", validation_alias="TOOL_RESULT_TRIM_READ"
    )
    tool_result_trim_grep: str = Field(
        default="off", validation_alias="TOOL_RESULT_TRIM_GREP"
    )
    tool_result_trim_glob: str = Field(
        default="off", validation_alias="TOOL_RESULT_TRIM_GLOB"
    )
    # Longest a single tool result may be before a rule considers it at all.
    # See config/constants.py for the measured distribution behind the default.
    tool_result_trim_threshold_chars: int = Field(
        default=TOOL_RESULT_TRIM_THRESHOLD_CHARS_DEFAULT,
        validation_alias="TOOL_RESULT_TRIM_THRESHOLD_CHARS",
    )
    # Kept from the start and the end of a trimmed body. The head carries the
    # path and opening line numbers, the tail carries the last matches; only the
    # middle can be replaced by a marker without costing the ability to navigate
    # what is left.
    tool_result_trim_keep_head_chars: int = Field(
        default=TOOL_RESULT_TRIM_KEEP_HEAD_CHARS_DEFAULT,
        validation_alias="TOOL_RESULT_TRIM_KEEP_HEAD_CHARS",
    )
    tool_result_trim_keep_tail_chars: int = Field(
        default=TOOL_RESULT_TRIM_KEEP_TAIL_CHARS_DEFAULT,
        validation_alias="TOOL_RESULT_TRIM_KEEP_TAIL_CHARS",
    )
    # How many of the newest attributable results are never touched.
    tool_result_trim_protect_recent_results: int = Field(
        default=TOOL_RESULT_TRIM_PROTECT_RECENT_DEFAULT,
        validation_alias="TOOL_RESULT_TRIM_PROTECT_RECENT_RESULTS",
    )

    # ==================== Local web server tools (web_search / web_fetch) ====================
    # On by default to match the shipped env template (env.example) and the
    # dashboard manifest. These tools perform outbound HTTP from the proxy, so
    # operators who do not want that can set ENABLE_WEB_SERVER_TOOLS=false.
    enable_web_server_tools: bool = Field(
        default=True, validation_alias="ENABLE_WEB_SERVER_TOOLS"
    )
    # Comma-separated URL schemes allowed for web_fetch (default: http,https).
    web_fetch_allowed_schemes: str = Field(
        default="http,https", validation_alias="WEB_FETCH_ALLOWED_SCHEMES"
    )
    # When true, skip private/loopback/link-local IP blocking for web_fetch (lab only).
    web_fetch_allow_private_networks: bool = Field(
        default=False, validation_alias="WEB_FETCH_ALLOW_PRIVATE_NETWORKS"
    )

    # ==================== Web Search Providers ====================
    # Backend for the proxy-fulfilled web_search server tool:
    # "auto" (first configured catalog provider, else ddgs), "off" (legacy scrape),
    # "disabled" (reject searches), or a provider id from config.websearch_catalog.
    web_search_provider: str = Field(
        default="auto", validation_alias="WEB_SEARCH_PROVIDER"
    )
    # "auto" keeps auto-selection resilient but treats an explicit provider as strict.
    # Other values explicitly choose no fallback, DDGS only, or DDGS then legacy.
    web_search_fallback_policy: str = Field(
        default="auto", validation_alias="WEB_SEARCH_FALLBACK_POLICY"
    )
    # One optional credential per provider; comma-separate multiple keys for rotation.
    ollama_search_api_key: str | None = Field(
        default=None, validation_alias="OLLAMA_SEARCH_API_KEY"
    )
    exa_api_key: str | None = Field(default=None, validation_alias="EXA_API_KEY")
    tavily_api_key: str | None = Field(default=None, validation_alias="TAVILY_API_KEY")
    brave_search_api_key: str | None = Field(
        default=None, validation_alias="BRAVE_SEARCH_API_KEY"
    )
    jina_api_key: str | None = Field(default=None, validation_alias="JINA_API_KEY")
    serper_api_key: str | None = Field(default=None, validation_alias="SERPER_API_KEY")
    firecrawl_api_key: str | None = Field(
        default=None, validation_alias="FIRECRAWL_API_KEY"
    )
    linkup_api_key: str | None = Field(default=None, validation_alias="LINKUP_API_KEY")
    perplexity_search_api_key: str | None = Field(
        default=None, validation_alias="PERPLEXITY_SEARCH_API_KEY"
    )
    parallel_api_key: str | None = Field(
        default=None, validation_alias="PARALLEL_API_KEY"
    )
    searchapi_api_key: str | None = Field(
        default=None, validation_alias="SEARCHAPI_API_KEY"
    )
    serpapi_api_key: str | None = Field(
        default=None, validation_alias="SERPAPI_API_KEY"
    )
    # Base URL of a self-hosted SearXNG instance (format=json must be enabled).
    searxng_base_url: str | None = Field(
        default=None, validation_alias="SEARXNG_BASE_URL"
    )
    # Web search usage analytics (SQLite under ~/.fcc/logs/).
    websearch_log_enabled: bool = Field(
        default=True, validation_alias="WEBSEARCH_LOG_ENABLED"
    )
    websearch_log_max_rows: int = Field(
        default=50000, validation_alias="WEBSEARCH_LOG_MAX_ROWS"
    )
    # Persist complete normalized provider inputs/outputs for Admin drill-down.
    # Disable for hashes/lengths only when searches may contain sensitive content.
    websearch_log_capture_content: bool = Field(
        default=True, validation_alias="WEBSEARCH_LOG_CAPTURE_CONTENT"
    )
    websearch_log_content_max_chars: int = Field(
        default=2_000_000,
        ge=512,
        validation_alias="WEBSEARCH_LOG_CONTENT_MAX_CHARS",
    )
    # Rich digest for the proxy-fulfilled web_search text block: per-result
    # excerpt character cap and the optional provider answer lead.
    websearch_digest_chars: int = Field(
        default=600, validation_alias="WEBSEARCH_DIGEST_CHARS"
    )
    # Cap for the provider's extracted page text, which is far longer than a
    # snippet and only present when an operator opts into it (EXA_CONTENTS,
    # TAVILY_INCLUDE_RAW_CONTENT, FIRECRAWL_SCRAPE_FORMAT, ...). Separate from
    # the snippet cap so turning content on is not trimmed back to snippet size.
    websearch_digest_content_chars: int = Field(
        default=2000, ge=0, validation_alias="WEBSEARCH_DIGEST_CONTENT_CHARS"
    )
    websearch_digest_answer: bool = Field(
        default=True, validation_alias="WEBSEARCH_DIGEST_ANSWER"
    )

    # ==================== Debug / diagnostic logging (avoid sensitive content) ====================
    # Minimum log level for the JSON file sink (DEBUG, INFO, WARNING, ERROR, CRITICAL).
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    # When false (default), API and SSE helpers log only metadata (counts, lengths, ids).
    log_raw_api_payloads: bool = Field(
        default=False, validation_alias="LOG_RAW_API_PAYLOADS"
    )
    log_raw_sse_events: bool = Field(
        default=False, validation_alias="LOG_RAW_SSE_EVENTS"
    )
    # When false (default), unhandled exceptions log only type + route metadata (no message/traceback).
    log_api_error_tracebacks: bool = Field(
        default=False, validation_alias="LOG_API_ERROR_TRACEBACKS"
    )
    # When false (default), messaging logs omit text/transcription previews (metadata only).
    log_raw_messaging_content: bool = Field(
        default=False, validation_alias="LOG_RAW_MESSAGING_CONTENT"
    )
    # When true, log full Claude CLI stderr, non-JSON lines, and parser error text.
    log_raw_cli_diagnostics: bool = Field(
        default=False, validation_alias="LOG_RAW_CLI_DIAGNOSTICS"
    )
    # When true, log exception text / CLI error strings in messaging (may leak user content).
    log_messaging_error_details: bool = Field(
        default=False, validation_alias="LOG_MESSAGING_ERROR_DETAILS"
    )
    debug_platform_edits: bool = Field(
        default=False, validation_alias="DEBUG_PLATFORM_EDITS"
    )
    debug_subagent_stack: bool = Field(
        default=False, validation_alias="DEBUG_SUBAGENT_STACK"
    )

    # ==================== Request Analytics Log ====================
    # Persistent per-request log (SQLite at ~/.fcc/logs/requests.db) feeding the
    # admin Requests/Analytics tab. Writes are non-blocking (background writer).
    request_log_enabled: bool = Field(
        default=True, validation_alias="REQUEST_LOG_ENABLED"
    )
    # When false, store only body lengths + SHA-256 hashes instead of raw text.
    request_log_capture_bodies: bool = Field(
        default=True, validation_alias="REQUEST_LOG_CAPTURE_BODIES"
    )
    # Retention cap; oldest rows are pruned periodically past this many rows.
    request_log_max_rows: int = Field(
        default=50_000, validation_alias="REQUEST_LOG_MAX_ROWS"
    )
    # Store request/response text zstd-compressed in a side table instead of
    # inline. Bodies are ~99% of the bytes, so this is roughly 9x less disk for
    # the same retention. Rows written before it was enabled are still read.
    request_log_compress_bodies: bool = Field(
        default=True, validation_alias="REQUEST_LOG_COMPRESS_BODIES"
    )
    # Keep a thumbnail of every image a request carried, so the request detail
    # can show what the model was actually looking at. Counts and sizes are
    # recorded either way; this only decides whether pixels are stored.
    request_log_capture_images: bool = Field(
        default=True, validation_alias="REQUEST_LOG_CAPTURE_IMAGES"
    )
    request_log_image_max_pixels: int = Field(
        default=REQUEST_LOG_IMAGE_MAX_PIXELS_DEFAULT,
        validation_alias="REQUEST_LOG_IMAGE_MAX_PIXELS",
    )
    # Bounds the message/tool structure stored beside the knobs; the knobs
    # themselves are always stored whole (core/wire_capture.py).
    request_log_wire_body_max_chars: int = Field(
        default=REQUEST_LOG_WIRE_BODY_MAX_CHARS_DEFAULT,
        validation_alias="REQUEST_LOG_WIRE_BODY_MAX_CHARS",
    )
    # Bounds each upstream error body stored per try in the retry ladder.
    # 60 tries at the default is 48 KB worst case, against the 8,000-char wire
    # body already stored once per attempt.
    request_log_ladder_body_max_chars: int = Field(
        default=REQUEST_LOG_LADDER_BODY_MAX_CHARS_DEFAULT,
        validation_alias="REQUEST_LOG_LADDER_BODY_MAX_CHARS",
    )

    # ==================== NIM Settings ====================
    nim: NimSettings = Field(default_factory=NimSettings)

    # ==================== Voice Note Transcription ====================
    voice_note_enabled: bool = Field(
        default=True, validation_alias="VOICE_NOTE_ENABLED"
    )
    # Device: "cpu" | "cuda" | "nvidia_nim"
    # - "cpu"/"cuda": local Whisper (requires voice_local extra: uv sync --extra voice_local)
    # - "nvidia_nim": NVIDIA NIM Whisper API (requires voice extra: uv sync --extra voice)
    whisper_device: str = Field(default="cpu", validation_alias="WHISPER_DEVICE")
    # Whisper model ID or short name (for local Whisper) or NVIDIA NIM model (for nvidia_nim)
    # Local Whisper: "tiny", "base", "small", "medium", "large-v2", "large-v3", "large-v3-turbo"
    # NVIDIA NIM: "nvidia/parakeet-ctc-1.1b-asr", "openai/whisper-large-v3", etc.
    whisper_model: str = Field(default="base", validation_alias="WHISPER_MODEL")
    # ==================== Bot Wrapper Config ====================
    telegram_bot_token: str | None = None
    allowed_telegram_user_id: str | None = None
    telegram_proxy_url: str = Field(default="", validation_alias="TELEGRAM_PROXY_URL")
    discord_bot_token: str | None = Field(
        default=None, validation_alias="DISCORD_BOT_TOKEN"
    )
    allowed_discord_channels: str | None = Field(
        default=None, validation_alias="ALLOWED_DISCORD_CHANNELS"
    )
    allowed_dir: str = ""
    max_message_log_entries_per_chat: int | None = Field(
        default=None, validation_alias="MAX_MESSAGE_LOG_ENTRIES_PER_CHAT"
    )

    # ==================== Server ====================
    host: str = "0.0.0.0"
    port: int = 8082
    open_admin_browser: bool = Field(default=True, validation_alias="FCC_OPEN_BROWSER")
    # Optional proxy bearer token protecting public API endpoints.
    # Set via env `ANTHROPIC_AUTH_TOKEN`. When empty, no auth is required.
    anthropic_auth_token: str = Field(
        default="", validation_alias="ANTHROPIC_AUTH_TOKEN"
    )
    # Seconds each server generation is given to finish in-flight requests during
    # a RELOAD or REPLACE_PROCESS handoff before the supervisor force-drops them.
    # A bounded, configurable field surfaced through the Limits manifest. The
    # floor is 1s because uvicorn treats 0 as an immediate, no-drain shutdown
    # rather than waiting indefinitely for in-flight work to drain.
    server_graceful_shutdown_seconds: float = Field(
        default=SERVER_GRACEFUL_SHUTDOWN_SECONDS_DEFAULT,
        validation_alias="SERVER_GRACEFUL_SHUTDOWN_SECONDS",
    )

    # ==================== Desktop (mcc-desktop) ====================
    # mcc-desktop is a SEPARATE PROCESS from the server. It calls
    # get_settings() once, at launch, so a change made here in the dashboard
    # applies to the next mcc-desktop start, not to a tray already running.
    #
    # Tight poll used while waiting for a freshly spawned mcc-server child to
    # become healthy.
    desktop_health_check_interval: float = Field(
        default=DESKTOP_HEALTH_CHECK_INTERVAL_DEFAULT,
        validation_alias="DESKTOP_HEALTH_CHECK_INTERVAL",
    )
    # How long to wait for a spawned mcc-server child to become healthy
    # before reporting a start failure.
    desktop_server_start_timeout: float = Field(
        default=DESKTOP_SERVER_START_TIMEOUT_DEFAULT,
        validation_alias="DESKTOP_SERVER_START_TIMEOUT",
    )
    # Timeout for one loopback call to the server's admin API.
    desktop_admin_request_timeout: float = Field(
        default=DESKTOP_ADMIN_REQUEST_TIMEOUT_DEFAULT,
        validation_alias="DESKTOP_ADMIN_REQUEST_TIMEOUT",
    )
    # How often the tray polls for another instance's "show my window" signal.
    desktop_activation_poll_seconds: float = Field(
        default=DESKTOP_ACTIVATION_POLL_SECONDS_DEFAULT,
        validation_alias="DESKTOP_ACTIVATION_POLL_SECONDS",
    )
    # How often the tray's background thread probes the running server once
    # it is up (the ongoing health monitor, distinct from the startup poll).
    desktop_health_poll_seconds: float = Field(
        default=DESKTOP_HEALTH_POLL_SECONDS_DEFAULT,
        validation_alias="DESKTOP_HEALTH_POLL_SECONDS",
    )
    # Consecutive failed health probes before the tray reports an outage.
    # Debounces a self-update restart so it is not read as the server dying.
    desktop_health_failure_threshold: int = Field(
        default=DESKTOP_HEALTH_FAILURE_THRESHOLD_DEFAULT,
        validation_alias="DESKTOP_HEALTH_FAILURE_THRESHOLD",
    )
    # Desktop window size, in CSS pixels, for the app-mode/embedded window.
    desktop_window_width: int = Field(
        default=DESKTOP_WINDOW_WIDTH_DEFAULT, validation_alias="DESKTOP_WINDOW_WIDTH"
    )
    desktop_window_height: int = Field(
        default=DESKTOP_WINDOW_HEIGHT_DEFAULT,
        validation_alias="DESKTOP_WINDOW_HEIGHT",
    )
    # Explicit path to a Chromium-family binary (Chrome/Edge/Brave/Chromium).
    # When set, it takes precedence over the built-in candidate search; when
    # it points at something that does not exist, mcc-desktop logs a warning
    # and falls through to the search rather than failing to open a window.
    desktop_browser_path: str = Field(
        default="", validation_alias="DESKTOP_BROWSER_PATH"
    )

    # Handle empty strings for optional string fields
    @field_validator(*BLANK_MEANS_UNSET_FIELDS, mode="before")
    @classmethod
    def parse_optional_str(cls, v: Any) -> Any:
        if v == "":
            return None
        return v

    @field_validator("max_message_log_entries_per_chat", mode="before")
    @classmethod
    def parse_optional_log_cap(cls, v: Any) -> Any:
        if v == "" or v is None:
            return None
        return v

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in valid:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(valid)}, got {v!r}")
        return upper

    @field_validator("reasoning_policy")
    @classmethod
    def validate_root_reasoning_policy(
        cls, value: ReasoningPreference
    ) -> ReasoningPreference:
        if value is ReasoningPreference.INHERIT:
            raise ValueError("REASONING_POLICY cannot inherit")
        return value

    @field_validator("whisper_device")
    @classmethod
    def validate_whisper_device(cls, v: str) -> str:
        if v not in ("cpu", "cuda", "nvidia_nim"):
            raise ValueError(
                f"whisper_device must be 'cpu', 'cuda', or 'nvidia_nim', got {v!r}"
            )
        return v

    @field_validator("messaging_platform")
    @classmethod
    def validate_messaging_platform(cls, v: str) -> str:
        if v not in ("telegram", "discord", "none"):
            raise ValueError(
                f"messaging_platform must be 'telegram', 'discord', or 'none', got {v!r}"
            )
        return v

    @field_validator("messaging_rate_limit")
    @classmethod
    def validate_messaging_rate_limit(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("messaging_rate_limit must be > 0")
        return v

    @field_validator("messaging_rate_window")
    @classmethod
    def validate_messaging_rate_window(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("messaging_rate_window must be > 0")
        return float(v)

    @field_validator("web_search_provider")
    @classmethod
    def validate_web_search_provider(cls, v: str) -> str:
        value = v.strip().lower()
        allowed = {"auto", "off", "disabled", *SUPPORTED_WEBSEARCH_PROVIDER_IDS}
        if value not in allowed:
            raise ValueError(
                f"web_search_provider must be 'auto', 'off', 'disabled', or one of "
                f"{SUPPORTED_WEBSEARCH_PROVIDER_IDS}, got {v!r}"
            )
        return value

    @field_validator("web_search_fallback_policy")
    @classmethod
    def validate_web_search_fallback_policy(cls, v: str) -> str:
        value = v.strip().lower()
        allowed = ("auto", "none", "ddgs", "legacy")
        if value not in allowed:
            raise ValueError(
                "web_search_fallback_policy must be 'auto', 'none', 'ddgs', "
                f"or 'legacy', got {v!r}"
            )
        return value

    @field_validator("web_fetch_allowed_schemes")
    @classmethod
    def validate_web_fetch_allowed_schemes(cls, v: str) -> str:
        schemes = [part.strip().lower() for part in v.split(",") if part.strip()]
        if not schemes:
            raise ValueError("web_fetch_allowed_schemes must list at least one scheme")
        for scheme in schemes:
            if not scheme.isascii() or not scheme.isalpha():
                raise ValueError(
                    f"Invalid URL scheme in web_fetch_allowed_schemes: {scheme!r}"
                )
        return ",".join(schemes)

    @field_validator("fallback_skip_kinds")
    @classmethod
    def validate_fallback_skip_kinds(cls, v: str) -> str:
        """Reject an unknown kind at load rather than ignoring it at runtime.

        A typo here fails open -- the kind never matches, so the route falls
        back on everything and the setting silently does nothing. Refusing the
        value is the difference between a config error and a setting that looks
        applied for months.
        """
        kinds = [part.strip().lower() for part in (v or "").split(",")]
        kept = [kind for kind in kinds if kind]
        known = FAILURE_KIND_NAMES
        unknown = [kind for kind in kept if kind not in known]
        if unknown:
            raise ValueError(
                f"Unknown fallback_skip_kinds: {', '.join(unknown)}. "
                f"Known kinds: {', '.join(sorted(known))}"
            )
        return ",".join(dict.fromkeys(kept))

    @field_validator("credential_lockout_tiers")
    @classmethod
    def validate_credential_lockout_tiers(cls, v: str) -> str:
        """Reject a ladder that cannot be walked, at load rather than at 401.

        Stored as the operator typed it so the admin form round-trips the same
        string it showed; :func:`parse_lockout_tiers` turns it into seconds
        where it is used. A ladder is not a range, so it has no LIMIT_RANGES
        entry to clamp it -- this is the check that stands in for one.
        """
        try:
            parse_lockout_tiers(v)
        except ValueError as exc:
            raise ValueError(f"CREDENTIAL_LOCKOUT_TIERS: {exc}") from exc
        return v

    @field_validator(
        "tool_result_trim_read",
        "tool_result_trim_grep",
        "tool_result_trim_glob",
    )
    @classmethod
    def validate_trim_mode(cls, v: str, info: Any) -> str:
        """Reject an unknown mode rather than guess at it.

        Guessing here would mean either trimming when the operator asked for
        something else, or silently doing nothing when they asked to trim. A
        typo has to be loud in both directions. A blank value is not a typo --
        the admin UI writes ``KEY=`` for a cleared field -- so it falls back to
        the field default the same way a blank numeric limit does.
        """
        mode = str(v).strip().lower()
        if not mode:
            return str(cls.model_fields[info.field_name].default)
        if mode not in TRIM_MODE_NAMES:
            raise ValueError(
                f"Unknown trim mode: {v!r}. Known modes: "
                f"{', '.join(sorted(TRIM_MODE_NAMES))}"
            )
        return mode

    @field_validator(
        "model",
        "model_fable",
        "model_opus",
        "model_sonnet",
        "model_haiku",
        "model_vision",
    )
    @classmethod
    def validate_model_format(cls, v: str | None) -> str | None:
        if v is None:
            return None
        _require_provider_prefixed_model_ref(v)
        return v

    @field_validator(
        "model_fallbacks",
        "model_fable_fallbacks",
        "model_opus_fallbacks",
        "model_sonnet_fallbacks",
        "model_haiku_fallbacks",
        "model_vision_fallbacks",
    )
    @classmethod
    def validate_model_fallback_chain(cls, v: str | None) -> str | None:
        """Validate every chain entry and store the chain in canonical form."""
        if v is None:
            return None
        model_refs = parse_model_ref_list(v)
        if not model_refs:
            return None
        for model_ref in model_refs:
            _require_provider_prefixed_model_ref(model_ref)
        return format_model_ref_list(model_refs)

    @field_validator(*LIMIT_RANGES, mode="before")
    @classmethod
    def blank_limit_falls_back_to_its_default(cls, value: object, info: Any) -> object:
        """Treat an empty value as "not set" rather than as a broken number.

        The admin UI writes ``KEY=`` for a cleared field and a hand-edited file
        can hold the same thing. Refusing to parse it stopped the server from
        starting over a setting the user was trying to stop specifying.
        """
        if isinstance(value, str) and not value.strip():
            return cls.model_fields[info.field_name].default
        return value

    @field_validator("max_output_tokens_ceiling", mode="after")
    @classmethod
    def a_zero_ceiling_means_no_ceiling(cls, value: int | None) -> int | None:
        """0 is how an operator says "let every model's own limit stand".

        The field ships set (131,072), so "leave it blank" now resolves to the
        default rather than to None -- see
        :meth:`blank_limit_falls_back_to_its_default` directly above. A
        sentinel is the only remaining way out, and 0 is the one this file
        already uses for "this bound is off" (config/limits.py).
        """
        return _no_ceiling_when_zero(value)

    @model_validator(mode="after")
    def keep_limits_inside_their_usable_range(self) -> Settings:
        """Clamp a limit rather than refuse to start.

        A value outside its range is always a mistake, but the two ways to
        answer a mistake are not equal: the admin UI rejects it up front with
        the range quoted, while a file edited by hand is only discovered at
        boot -- and a proxy that will not start is worse than one running with
        a sane number and a warning saying so.
        """
        for attr, limit in LIMIT_RANGES.items():
            value = getattr(self, attr)
            if value is None:
                # An optional limit that is simply not set, e.g. the output
                # ceiling. Nothing to keep inside a range.
                continue
            clamped = limit.clamp(value)
            if clamped == value:
                continue
            coerced = type(value)(clamped)
            logger.warning(
                "{} is outside its usable range ({} to {}); using {}",
                attr.upper(),
                limit.minimum,
                limit.maximum,
                coerced,
            )
            setattr(self, attr, coerced)
        # The clamp above can land on 0 from below (a negative typed by hand),
        # and 0 is the sentinel, not a value: a ceiling of 0 would cap every
        # answer at nothing. Re-applied here because the field validator ran
        # before the clamp, not after it.
        self.max_output_tokens_ceiling = _no_ceiling_when_zero(
            self.max_output_tokens_ceiling
        )
        return self

    @model_validator(mode="after")
    def reference_managed_chatgpt_oauth_credentials(self) -> Settings:
        """Mark FCC-owned OAuth credentials without loading secrets into Settings."""
        if self.chatgpt_oauth_access_token.strip():
            return self
        if chatgpt_oauth_auth_path().is_file():
            self.chatgpt_oauth_access_token = CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE
        return self

    @model_validator(mode="after")
    def reference_managed_anthropic_oauth_credentials(self) -> Settings:
        """Mark an FCC-owned Claude subscription credential without loading secrets."""
        if self.anthropic_oauth_access_token.strip():
            return self
        if anthropic_oauth_managed_store_path().is_file():
            self.anthropic_oauth_access_token = (
                ANTHROPIC_OAUTH_MANAGED_CREDENTIAL_REFERENCE
            )
        return self

    @model_validator(mode="after")
    def check_nvidia_nim_api_key(self) -> Settings:
        if (
            self.voice_note_enabled
            and self.whisper_device == "nvidia_nim"
            and not self.nvidia_nim_api_key.strip()
        ):
            raise ValueError(
                "NVIDIA_NIM_API_KEY is required when WHISPER_DEVICE is 'nvidia_nim'. "
                "Set it in your .env file."
            )
        return self

    @model_validator(mode="after")
    def prefer_dotenv_anthropic_auth_token(self) -> Settings:
        """Let explicit .env auth config override stale shell/client tokens."""
        dotenv_value = env_file_override(self.model_config, ANTHROPIC_AUTH_TOKEN_ENV)
        if dotenv_value is not None:
            self.anthropic_auth_token = dotenv_value
        return self

    model_config = SettingsConfigDict(
        env_file=settings_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
