"""Admin configuration manifest."""

from collections.abc import Iterable
from dataclasses import replace

from my_claude_code.config.limits import describe_range, range_for
from my_claude_code.config.reasoning import (
    ROOT_REASONING_PREFERENCES,
    ROUTE_REASONING_PREFERENCES,
    ReasoningPreference,
)
from my_claude_code.config.settings import Settings

# Spec types live in the neutral .spec module so catalog-derived generators
# (provider_manifest, websearch_manifest) can use them without import cycles;
# they remain importable from here for existing consumers.
from .provider_manifest import provider_field_specs
from .spec import ConfigFieldSpec, ConfigOptionSpec, ConfigSectionSpec, FieldType
from .websearch_manifest import websearch_field_specs

__all__ = [
    "ConfigFieldSpec",
    "ConfigOptionSpec",
    "ConfigSectionSpec",
    "FieldType",
]


def _reasoning_options(
    preferences: tuple[ReasoningPreference, ...],
) -> tuple[ConfigOptionSpec, ...]:
    labels = {
        ReasoningPreference.INHERIT: "Inherit",
        ReasoningPreference.OFF: "Off",
        ReasoningPreference.CLIENT: "From client",
        ReasoningPreference.ADAPTIVE: "Adaptive",
        ReasoningPreference.LOW: "Low",
        ReasoningPreference.MEDIUM: "Medium",
        ReasoningPreference.HIGH: "High",
        ReasoningPreference.XHIGH: "X-High",
        ReasoningPreference.MAX: "Max",
    }
    return tuple(
        ConfigOptionSpec(preference.value, labels[preference])
        for preference in preferences
    )


# One shared option list, so the three rules cannot drift apart.
_TRIM_MODE_OPTIONS: tuple[ConfigOptionSpec, ...] = (
    ConfigOptionSpec("off", "Off"),
    ConfigOptionSpec("observe", "Observe"),
    ConfigOptionSpec("on", "On"),
)

_TRIM_MODE_HELP = (
    "Off leaves every {tool} result untouched. Observe measures what would "
    "have been removed and records it, changing nothing on the wire -- run a "
    "rule here first and read the numbers before trusting it. On performs the "
    "elision and marks it inline. Requires the master switch above."
)


SECTIONS: tuple[ConfigSectionSpec, ...] = (
    ConfigSectionSpec(
        "providers",
        "Providers",
        "Provider keys, local endpoints, and proxy settings.",
    ),
    ConfigSectionSpec(
        "models",
        "Model Routing",
        "Where each Claude tier sends its requests, and what covers it when "
        "that model cannot.",
    ),
    ConfigSectionSpec(
        "reasoning",
        "Reasoning",
        "Client reasoning policy and route-specific overrides.",
    ),
    ConfigSectionSpec(
        "runtime",
        "Runtime",
        "Server API token, rate limits, timeouts, and process settings.",
    ),
    ConfigSectionSpec(
        "messaging",
        "Messaging",
        "Discord, Telegram, CLI workspace, and session settings.",
    ),
    ConfigSectionSpec(
        "voice",
        "Voice",
        "Voice note transcription settings.",
    ),
    ConfigSectionSpec(
        "web_tools",
        "Web Tools",
        "Local Anthropic web_search and web_fetch behavior.",
    ),
    ConfigSectionSpec(
        "websearch",
        "Web Search",
        "Web search provider selection, API keys, and key rotation.",
    ),
    ConfigSectionSpec(
        "optimizer",
        "Tool-result trimming",
        "Shortens large Read, Grep and Glob results before they reach the "
        "model. This is the only feature in MCC that changes what the model is "
        "allowed to see, and it is off by default. The local rules above cost "
        "the model nothing; these controls do.",
    ),
    ConfigSectionSpec(
        "limits",
        "Limits",
        "What MCC waits for, keeps, and records. Every value here is a "
        "trade-off between how long a failing model may hold a request, how "
        "much history survives on disk, and how much log noise you want.",
    ),
    ConfigSectionSpec(
        "desktop",
        "Desktop",
        "Tray/window timing and sizing for mcc-desktop. mcc-desktop is a "
        "separate process from the server and reads these once, at launch -- "
        "a change here applies to the next mcc-desktop start, not to a tray "
        "already running.",
    ),
    ConfigSectionSpec(
        "diagnostics",
        "Diagnostics",
        "Logging and debugging flags.",
        advanced=True,
    ),
)


_NON_PROVIDER_FIELDS: tuple[ConfigFieldSpec, ...] = (
    ConfigFieldSpec(
        "MODEL",
        "Default Model",
        "models",
        "model",
        settings_attr="model",
        default="nvidia_nim/nvidia/nemotron-3-super-120b-a12b",
    ),
    ConfigFieldSpec(
        "MODEL_FALLBACKS",
        "Default Fallback Chain",
        "models",
        "model_chain",
        settings_attr="model_fallbacks",
    ),
    ConfigFieldSpec(
        "MODEL_FABLE",
        "Fable Override",
        "models",
        "optional_model",
        settings_attr="model_fable",
    ),
    ConfigFieldSpec(
        "MODEL_FABLE_FALLBACKS",
        "Fable Fallback Chain",
        "models",
        "model_chain",
        settings_attr="model_fable_fallbacks",
    ),
    ConfigFieldSpec(
        "MODEL_OPUS",
        "Opus Override",
        "models",
        "optional_model",
        settings_attr="model_opus",
    ),
    ConfigFieldSpec(
        "MODEL_OPUS_FALLBACKS",
        "Opus Fallback Chain",
        "models",
        "model_chain",
        settings_attr="model_opus_fallbacks",
    ),
    ConfigFieldSpec(
        "MODEL_SONNET",
        "Sonnet Override",
        "models",
        "optional_model",
        settings_attr="model_sonnet",
    ),
    ConfigFieldSpec(
        "MODEL_SONNET_FALLBACKS",
        "Sonnet Fallback Chain",
        "models",
        "model_chain",
        settings_attr="model_sonnet_fallbacks",
    ),
    ConfigFieldSpec(
        "MODEL_HAIKU",
        "Haiku Override",
        "models",
        "optional_model",
        settings_attr="model_haiku",
    ),
    ConfigFieldSpec(
        "MODEL_HAIKU_FALLBACKS",
        "Haiku Fallback Chain",
        "models",
        "model_chain",
        settings_attr="model_haiku_fallbacks",
    ),
    ConfigFieldSpec(
        "MODEL_VISION",
        "Vision Adapter",
        "models",
        "optional_model",
        settings_attr="model_vision",
    ),
    ConfigFieldSpec(
        "MODEL_VISION_FALLBACKS",
        "Vision Fallback Chain",
        "models",
        "model_chain",
        settings_attr="model_vision_fallbacks",
    ),
    ConfigFieldSpec(
        "FALLBACK_SKIP_KINDS",
        "Do not fall back on",
        "models",
        "text",
        settings_attr="fallback_skip_kinds",
        default="invalid_request",
        description=(
            "Failure kinds that end a route instead of trying the next model. "
            "A malformed request fails identically everywhere, so retrying it "
            "costs a round trip per model to reach the same error. Leave empty "
            "to fall back on every failure. Known kinds: invalid_request, "
            "context_length, authentication, permission, rate_limit, "
            "overloaded, timeout, upstream, unavailable."
        ),
    ),
    ConfigFieldSpec(
        "FALLBACK_REASONING_ANSWER_TIMEOUT",
        "Thinking time before the chain moves on",
        "models",
        "number",
        settings_attr="fallback_reasoning_answer_timeout",
        default="300",
        restart_required=True,
        description=(
            "Seconds a model may think before the route stops waiting for it "
            "to start an answer and tries the next model. Only applies while "
            "the setting below is on, because only then is the attempt still "
            "abandonable. Measured on real traffic: every request that ran out "
            "of budget while thinking used the full 600s, and 98% of slow "
            "reasoning requests that did answer had started by 300s. Set 0 to "
            "let a thinking model run to the total request budget."
        ),
    ),
    ConfigFieldSpec(
        "FALLBACK_ON_REASONING_ONLY",
        "Fall back when a model only thinks",
        "models",
        "boolean",
        settings_attr="fallback_on_reasoning_only",
        default="true",
        restart_required=True,
        description=(
            "A model that streams its reasoning and never writes an answer "
            "normally commits the route on the first thought, so the fallback "
            "chain can no longer be used and the request runs until the total "
            "budget ends it. With this on, reasoning is held back like an "
            "envelope frame: the attempt stays uncommitted, its share of the "
            "budget expires, and the next model answers instead. The cost is "
            "that reasoning no longer streams live -- it appears when the "
            "answer does. Turn this off to watch a model think in real time."
        ),
    ),
    ConfigFieldSpec(
        "REASONING_POLICY",
        "Reasoning Policy",
        "reasoning",
        "select",
        settings_attr="reasoning_policy",
        default="client",
        options=_reasoning_options(ROOT_REASONING_PREFERENCES),
        description=(
            "From client preserves CLI effort. Providers translate only the controls "
            "their API supports."
        ),
    ),
    ConfigFieldSpec(
        "REASONING_FABLE",
        "Fable Reasoning",
        "reasoning",
        "select",
        settings_attr="reasoning_fable",
        default="inherit",
        options=_reasoning_options(ROUTE_REASONING_PREFERENCES),
    ),
    ConfigFieldSpec(
        "REASONING_OPUS",
        "Opus Reasoning",
        "reasoning",
        "select",
        settings_attr="reasoning_opus",
        default="inherit",
        options=_reasoning_options(ROUTE_REASONING_PREFERENCES),
    ),
    ConfigFieldSpec(
        "REASONING_SONNET",
        "Sonnet Reasoning",
        "reasoning",
        "select",
        settings_attr="reasoning_sonnet",
        default="inherit",
        options=_reasoning_options(ROUTE_REASONING_PREFERENCES),
    ),
    ConfigFieldSpec(
        "REASONING_HAIKU",
        "Haiku Reasoning",
        "reasoning",
        "select",
        settings_attr="reasoning_haiku",
        default="inherit",
        options=_reasoning_options(ROUTE_REASONING_PREFERENCES),
    ),
    ConfigFieldSpec(
        "ANTHROPIC_AUTH_TOKEN",
        "API/CLI Auth Token",
        "runtime",
        "secret",
        settings_attr="anthropic_auth_token",
        default="freecc",
        secret=True,
        restart_required=True,
        description="Bearer token protecting Claude/API access. It is not admin-page login.",
    ),
    ConfigFieldSpec(
        "PROVIDER_RATE_LIMIT",
        "Provider Rate Limit",
        "runtime",
        "number",
        settings_attr="provider_rate_limit",
        default="1",
    ),
    ConfigFieldSpec(
        "PROVIDER_RATE_WINDOW",
        "Provider Rate Window",
        "runtime",
        "number",
        settings_attr="provider_rate_window",
        default="3",
    ),
    ConfigFieldSpec(
        "PROVIDER_MAX_CONCURRENCY",
        "Provider Max Concurrency",
        "runtime",
        "number",
        settings_attr="provider_max_concurrency",
        default="5",
    ),
    ConfigFieldSpec(
        "HTTP_READ_TIMEOUT",
        "HTTP Read Timeout",
        "runtime",
        "number",
        settings_attr="http_read_timeout",
        default="300",
    ),
    ConfigFieldSpec(
        "HTTP_WRITE_TIMEOUT",
        "HTTP Write Timeout",
        "runtime",
        "number",
        settings_attr="http_write_timeout",
        default="60",
    ),
    ConfigFieldSpec(
        "HTTP_CONNECT_TIMEOUT",
        "HTTP Connect Timeout",
        "runtime",
        "number",
        settings_attr="http_connect_timeout",
        default="60",
    ),
    ConfigFieldSpec(
        "HOST",
        "Server Host",
        "runtime",
        settings_attr="host",
        default="0.0.0.0",
        restart_required=True,
    ),
    ConfigFieldSpec(
        "PORT",
        "Server Port",
        "runtime",
        "number",
        settings_attr="port",
        default="8082",
        restart_required=True,
    ),
    ConfigFieldSpec(
        "FCC_OPEN_BROWSER",
        "Open Admin on Startup",
        "runtime",
        "boolean",
        settings_attr="open_admin_browser",
        default="true",
        description="Open the Admin UI after the next fcc-server launch becomes healthy.",
    ),
    ConfigFieldSpec(
        "MESSAGING_PLATFORM",
        "Messaging Platform",
        "messaging",
        "select",
        settings_attr="messaging_platform",
        default="discord",
        options=("telegram", "discord", "none"),
        session_sensitive=True,
    ),
    ConfigFieldSpec(
        "MESSAGING_RATE_LIMIT",
        "Messaging Rate Limit",
        "messaging",
        "number",
        settings_attr="messaging_rate_limit",
        default="1",
        session_sensitive=True,
    ),
    ConfigFieldSpec(
        "MESSAGING_RATE_WINDOW",
        "Messaging Rate Window",
        "messaging",
        "number",
        settings_attr="messaging_rate_window",
        default="1",
        session_sensitive=True,
    ),
    ConfigFieldSpec(
        "TELEGRAM_BOT_TOKEN",
        "Telegram Bot Token",
        "messaging",
        "secret",
        settings_attr="telegram_bot_token",
        secret=True,
        session_sensitive=True,
    ),
    ConfigFieldSpec(
        "ALLOWED_TELEGRAM_USER_ID",
        "Allowed Telegram User ID",
        "messaging",
        settings_attr="allowed_telegram_user_id",
        session_sensitive=True,
    ),
    ConfigFieldSpec(
        "TELEGRAM_PROXY_URL",
        "Telegram Proxy URL",
        "messaging",
        "secret",
        settings_attr="telegram_proxy_url",
        secret=True,
        session_sensitive=True,
        description="Optional Telegram-only proxy, e.g. socks5://127.0.0.1:1080.",
    ),
    ConfigFieldSpec(
        "DISCORD_BOT_TOKEN",
        "Discord Bot Token",
        "messaging",
        "secret",
        settings_attr="discord_bot_token",
        secret=True,
        session_sensitive=True,
    ),
    ConfigFieldSpec(
        "ALLOWED_DISCORD_CHANNELS",
        "Allowed Discord Channels",
        "messaging",
        settings_attr="allowed_discord_channels",
        session_sensitive=True,
    ),
    ConfigFieldSpec(
        "ALLOWED_DIR",
        "Allowed Directory",
        "messaging",
        settings_attr="allowed_dir",
        session_sensitive=True,
    ),
    ConfigFieldSpec(
        "MAX_MESSAGE_LOG_ENTRIES_PER_CHAT",
        "Max Tracked Messages Per Chat",
        "messaging",
        "number",
        settings_attr="max_message_log_entries_per_chat",
        advanced=True,
        session_sensitive=True,
    ),
    ConfigFieldSpec(
        "VOICE_NOTE_ENABLED",
        "Voice Notes",
        "voice",
        "boolean",
        settings_attr="voice_note_enabled",
        default="false",
        session_sensitive=True,
    ),
    ConfigFieldSpec(
        "WHISPER_DEVICE",
        "Whisper Device",
        "voice",
        "select",
        settings_attr="whisper_device",
        default="nvidia_nim",
        options=("cpu", "cuda", "nvidia_nim"),
        session_sensitive=True,
    ),
    ConfigFieldSpec(
        "WHISPER_MODEL",
        "Whisper Model",
        "voice",
        settings_attr="whisper_model",
        default="openai/whisper-large-v3",
        session_sensitive=True,
    ),
    ConfigFieldSpec(
        "ENABLE_TITLE_GENERATION_SKIP",
        "Title Generation Skip",
        "optimizer",
        "boolean",
        settings_attr="enable_title_generation_skip",
        default="true",
        advanced=True,
    ),
    ConfigFieldSpec(
        "ENABLE_SUGGESTION_MODE_SKIP",
        "Suggestion Mode Skip",
        "optimizer",
        "boolean",
        settings_attr="enable_suggestion_mode_skip",
        default="true",
        advanced=True,
    ),
    ConfigFieldSpec(
        "ENABLE_WEB_SERVER_TOOLS",
        "Web Server Tools",
        "web_tools",
        "boolean",
        settings_attr="enable_web_server_tools",
        default="true",
    ),
    ConfigFieldSpec(
        "WEB_FETCH_ALLOWED_SCHEMES",
        "Allowed Web Fetch Schemes",
        "web_tools",
        settings_attr="web_fetch_allowed_schemes",
        default="http,https",
    ),
    ConfigFieldSpec(
        "WEB_FETCH_ALLOW_PRIVATE_NETWORKS",
        "Allow Private Networks",
        "web_tools",
        "boolean",
        settings_attr="web_fetch_allow_private_networks",
        default="false",
    ),
    ConfigFieldSpec(
        "DEBUG_PLATFORM_EDITS",
        "Debug Platform Edits",
        "diagnostics",
        "boolean",
        settings_attr="debug_platform_edits",
        default="false",
        advanced=True,
        restart_required=True,
    ),
    ConfigFieldSpec(
        "DEBUG_SUBAGENT_STACK",
        "Debug Subagent Stack",
        "diagnostics",
        "boolean",
        settings_attr="debug_subagent_stack",
        default="false",
        advanced=True,
        restart_required=True,
    ),
    ConfigFieldSpec(
        "LOG_RAW_API_PAYLOADS",
        "Log Raw API Payloads",
        "diagnostics",
        "boolean",
        settings_attr="log_raw_api_payloads",
        default="false",
        advanced=True,
        restart_required=True,
    ),
    ConfigFieldSpec(
        "LOG_RAW_SSE_EVENTS",
        "Log Raw SSE Events",
        "diagnostics",
        "boolean",
        settings_attr="log_raw_sse_events",
        default="false",
        advanced=True,
    ),
    ConfigFieldSpec(
        "LOG_API_ERROR_TRACEBACKS",
        "Log API Error Tracebacks",
        "diagnostics",
        "boolean",
        settings_attr="log_api_error_tracebacks",
        default="false",
        advanced=True,
        restart_required=True,
    ),
    ConfigFieldSpec(
        "LOG_RAW_MESSAGING_CONTENT",
        "Log Raw Messaging Content",
        "diagnostics",
        "boolean",
        settings_attr="log_raw_messaging_content",
        default="false",
        advanced=True,
        restart_required=True,
    ),
    ConfigFieldSpec(
        "LOG_RAW_CLI_DIAGNOSTICS",
        "Log Raw CLI Diagnostics",
        "diagnostics",
        "boolean",
        settings_attr="log_raw_cli_diagnostics",
        default="false",
        advanced=True,
        restart_required=True,
    ),
    ConfigFieldSpec(
        "LOG_MESSAGING_ERROR_DETAILS",
        "Log Messaging Error Details",
        "diagnostics",
        "boolean",
        settings_attr="log_messaging_error_details",
        default="false",
        advanced=True,
        restart_required=True,
    ),
    # ---- Limits: when to stop waiting ------------------------------------
    ConfigFieldSpec(
        "FALLBACK_FIRST_TOKEN_TIMEOUT",
        "First-token deadline",
        "limits",
        "number",
        settings_attr="fallback_first_token_timeout",
        default="120",
        description=(
            "Seconds a model may stay silent before the next model on the "
            "chain takes over. Nothing has reached the client yet, so the "
            "handover is invisible. 0 waits indefinitely."
        ),
    ),
    ConfigFieldSpec(
        "FALLBACK_TOTAL_TIMEOUT",
        "Total request budget",
        "limits",
        "number",
        settings_attr="fallback_total_timeout",
        default="600",
        description=(
            "Seconds one request may run across every attempt, retry and "
            "recovery. Each attempt gets an equal share of what is left until "
            "it produces output, so a silent model cannot spend the whole "
            "budget. Once output has started no fallback can replace it, but "
            "it can still stop. 0 disables the budget."
        ),
    ),
    ConfigFieldSpec(
        "FALLBACK_STALL_TIMEOUT",
        "Stall deadline",
        "limits",
        "number",
        settings_attr="fallback_stall_timeout",
        default="120",
        description=(
            "Seconds a model that has already produced output may then say "
            "nothing before the request is given up on. Measured from the last "
            "chunk that moved the answer forward, so a long answer is never "
            "cut and a keepalive never counts as progress. 0 disables it and "
            "leaves only the total budget."
        ),
    ),
    ConfigFieldSpec(
        "SERVER_GRACEFUL_SHUTDOWN_SECONDS",
        "Graceful shutdown budget",
        "limits",
        "number",
        settings_attr="server_graceful_shutdown_seconds",
        default="300",
        restart_required=True,
        description=(
            "Seconds a closing process gives in-flight requests to finish "
            "before the supervisor force-drops them during a reload or process "
            "replace. Sits just over the measured p99.9 whole-request budget so "
            "a healthy long request usually drains; longer ones (up to the 600s total "
            "budget) may still be cut. 1s is the floor; "
            "0 would be an immediate, no-drain shutdown rather than waiting."
        ),
    ),
    ConfigFieldSpec(
        "FALLBACK_EJECT_AFTER_FAILURES",
        "Bench a model after",
        "limits",
        "number",
        settings_attr="fallback_eject_after_failures",
        default="3",
        description=(
            "Consecutive failures before routing skips a model, so a request "
            "stops re-paying a dead model's timeout on its way to a healthy "
            "one. A chain is never emptied: if every model is benched they "
            "are tried in order anyway. 0 disables benching."
        ),
    ),
    ConfigFieldSpec(
        "FALLBACK_EJECT_SECONDS",
        "Keep it benched for",
        "limits",
        "number",
        settings_attr="fallback_eject_seconds",
        default="30",
        description="Seconds a benched model stays out of routing.",
    ),
    ConfigFieldSpec(
        "FALLBACK_BEHAVIOR",
        "Eject mode",
        "limits",
        "select",
        settings_attr="fallback_behavior",
        default="rate_based",
        options=("rate_based", "legacy"),
        description=(
            "How a failing model is benched. rate_based (default) skips a model when its failure rate over the last FALLBACK_EJECT_WINDOW requests crosses FALLBACK_EJECT_FAILURE_RATE, for FALLBACK_EJECT_SECONDS. legacy preserves the historical consecutive-count behavior (FALLBACK_EJECT_AFTER_FAILURES + FALLBACK_EJECT_SECONDS)."
        ),
    ),
    ConfigFieldSpec(
        "FALLBACK_RETRY_FIRST",
        "Retry primary once",
        "limits",
        "select",
        settings_attr="fallback_retry_first",
        default="skip",
        options=("skip", "retry_once"),
        description=(
            "What happens when the primary model fails. skip (default) moves straight to the next fallback. retry_once gives the primary one more chance for transient errors (timeout, 5xx, 429) before falling through. Auth and invalid-request errors are never retried regardless."
        ),
    ),
    ConfigFieldSpec(
        "FALLBACK_EJECT_WINDOW",
        "Rate window (requests)",
        "limits",
        "number",
        settings_attr="fallback_eject_window",
        default="10",
        description=(
            "Window size in requests for the rate-based eject math. A model is benched when at least FALLBACK_EJECT_FAILURE_RATE of its last N requests failed. Ignored in legacy mode."
        ),
        minimum=1,
    ),
    ConfigFieldSpec(
        "FALLBACK_EJECT_FAILURE_RATE",
        "Failure rate threshold",
        "limits",
        "number",
        settings_attr="fallback_eject_failure_rate",
        default="0.5",
        description=(
            "Fraction of failures in the window (0.0-1.0) that benches a model. Ignored in legacy mode."
        ),
        minimum=0.0,
        maximum=1.0,
    ),
    ConfigFieldSpec(
        "FALLBACK_EJECT_MIN_SAMPLES",
        "Min samples before evaluation",
        "limits",
        "number",
        settings_attr="fallback_eject_min_samples",
        default="8",
        description=(
            "Minimum requests observed before the failure rate is evaluated. Prevents a single failure on a low-traffic model from tripping it. Ignored in legacy mode."
        ),
        minimum=1,
    ),
    ConfigFieldSpec(
        "PROVIDER_RETRY_ATTEMPTS",
        "Retries before the chain",
        "limits",
        "number",
        settings_attr="provider_retry_attempts",
        default="5",
        restart_required=True,
        description=(
            "How many times one model is retried on a 429 or 5xx before the "
            "next model is tried. Each retry waits longer than the last, so "
            "5 attempts spend about 30s before a healthy fallback is used."
        ),
    ),
    ConfigFieldSpec(
        "STREAM_EARLY_RETRY_ATTEMPTS",
        "Retries inside one model",
        "limits",
        "number",
        settings_attr="stream_early_retry_attempts",
        default="5",
        restart_required=True,
        advanced=True,
        description=(
            "Attempts a provider makes on its own, before the failure reaches "
            "routing at all."
        ),
    ),
    ConfigFieldSpec(
        "STREAM_MIDSTREAM_RECOVERY_ATTEMPTS",
        "Mid-stream recovery attempts",
        "limits",
        "number",
        settings_attr="stream_midstream_recovery_attempts",
        default="5",
        restart_required=True,
        description=(
            "After output has started and the connection drops, how many "
            "times the same model is asked to finish. No chain can help here, "
            "so this bounds how long a dying stream may hold a request."
        ),
    ),
    ConfigFieldSpec(
        "STREAM_COMMIT_HOLDBACK_SECONDS",
        "Commit holdback",
        "limits",
        "number",
        settings_attr="stream_commit_holdback_seconds",
        default="0.75",
        restart_required=True,
        description=(
            "Seconds the first output is held before it goes to the client. "
            "While it is held a failure can still fall back silently, so this "
            "is the width of the invisible-recovery window. 0 commits at once "
            "and disables invisible recovery."
        ),
    ),
    ConfigFieldSpec(
        "RATE_LIMIT_COOLDOWN_SECONDS",
        "Rate-limit cooldown",
        "limits",
        "number",
        settings_attr="rate_limit_cooldown_seconds",
        default="60",
        restart_required=True,
        advanced=True,
        description=(
            "How long a rate-limited provider is paused when it sends no "
            "Retry-After header of its own to obey."
        ),
    ),
    ConfigFieldSpec(
        "CREDENTIAL_CIRCUIT_THRESHOLD",
        "Bench a key after",
        "limits",
        "number",
        settings_attr="credential_circuit_threshold",
        default="3",
        restart_required=True,
        advanced=True,
        description="Consecutive failures before one API key is benched by rotation.",
    ),
    # ---- Limits: what to keep --------------------------------------------
    ConfigFieldSpec(
        "REQUEST_LOG_ENABLED",
        "Record requests",
        "limits",
        "boolean",
        settings_attr="request_log_enabled",
        default="true",
        restart_required=True,
        description="Turn the request log and the Analytics tab on or off.",
    ),
    ConfigFieldSpec(
        "REQUEST_LOG_MAX_ROWS",
        "Requests to keep",
        "limits",
        "number",
        settings_attr="request_log_max_rows",
        default="50000",
        restart_required=True,
        description=(
            "The newest N requests are kept and older ones are deleted as new "
            "ones arrive. All-time counters keep counting either way; only "
            "the rows themselves are pruned."
        ),
    ),
    ConfigFieldSpec(
        "REQUEST_LOG_CAPTURE_BODIES",
        "Store prompts and replies",
        "limits",
        "boolean",
        settings_attr="request_log_capture_bodies",
        default="true",
        restart_required=True,
        description=(
            "Keeps the full text of each request so content search can find "
            "it. Bodies are about 99% of the stored bytes."
        ),
    ),
    ConfigFieldSpec(
        "REQUEST_LOG_COMPRESS_BODIES",
        "Compress stored text",
        "limits",
        "boolean",
        settings_attr="request_log_compress_bodies",
        default="true",
        restart_required=True,
        description=(
            "Compresses bodies against a dictionary trained on your own "
            "traffic and stores a repeated prompt once. Applies to new rows; "
            "run fcc-compact-log to convert existing history."
        ),
    ),
    ConfigFieldSpec(
        "REQUEST_LOG_CAPTURE_IMAGES",
        "Store image thumbnails",
        "limits",
        "boolean",
        settings_attr="request_log_capture_images",
        default="true",
        restart_required=True,
        description=(
            "Keeps a downscaled copy of every image or document a request "
            "carried, so the request detail can show what the model was "
            "looking at. The count is recorded either way."
        ),
    ),
    ConfigFieldSpec(
        "REQUEST_LOG_IMAGE_MAX_PIXELS",
        "Thumbnail size",
        "limits",
        "number",
        settings_attr="request_log_image_max_pixels",
        default="512",
        restart_required=True,
        description=(
            "Longest edge of a stored thumbnail. The same image re-sent on "
            "later turns of a conversation is stored once."
        ),
    ),
    ConfigFieldSpec(
        "REQUEST_LOG_TEXT_MAX_CHARS",
        "Longest text stored",
        "limits",
        "number",
        settings_attr="request_log_text_max_chars",
        default="50000",
        restart_required=True,
        description=(
            "Text longer than this is truncated before it is stored, which "
            "also bounds what content search can ever find."
        ),
    ),
    ConfigFieldSpec(
        "REQUEST_LOG_COMPRESSION_LEVEL",
        "Compression level",
        "limits",
        "number",
        settings_attr="request_log_compression_level",
        default="9",
        restart_required=True,
        advanced=True,
        description=(
            "zstd level for stored bodies. Measured on a real log, level 19 "
            "was 4.9% smaller than 9 at a ninth of the speed."
        ),
    ),
    ConfigFieldSpec(
        "REQUEST_LOG_QUEUE_MAX_SIZE",
        "Pending writes held",
        "limits",
        "number",
        settings_attr="request_log_queue_max_size",
        default="10000",
        restart_required=True,
        advanced=True,
        description=(
            "Records waiting to be written. When this fills under a burst, "
            "further records are dropped rather than slowing the request."
        ),
    ),
    # ---- Limits: what to record ------------------------------------------
    ConfigFieldSpec(
        "LOG_LEVEL",
        "Log level",
        "limits",
        "select",
        settings_attr="log_level",
        default="INFO",
        options=("DEBUG", "INFO", "WARNING", "ERROR"),
        restart_required=True,
        description=(
            "How much the server writes to its log file. DEBUG includes every "
            "routing decision, which is what to use when a fallback behaves "
            "unexpectedly."
        ),
    ),
    # ---- Desktop: mcc-desktop is a separate process, read once at launch --
    ConfigFieldSpec(
        "DESKTOP_HEALTH_CHECK_INTERVAL",
        "Startup health poll",
        "desktop",
        "number",
        settings_attr="desktop_health_check_interval",
        default="0.25",
        description=(
            "How often mcc-desktop checks whether a freshly spawned "
            "mcc-server has become healthy. Applies the next time mcc-desktop "
            "starts, not to a tray already running."
        ),
    ),
    ConfigFieldSpec(
        "DESKTOP_SERVER_START_TIMEOUT",
        "Server start timeout",
        "desktop",
        "number",
        settings_attr="desktop_server_start_timeout",
        default="15",
        description=(
            "How long mcc-desktop waits for a spawned mcc-server to become "
            "healthy before reporting a start failure. Applies the next time "
            "mcc-desktop starts, not to a tray already running."
        ),
    ),
    ConfigFieldSpec(
        "DESKTOP_ADMIN_REQUEST_TIMEOUT",
        "Admin API timeout",
        "desktop",
        "number",
        settings_attr="desktop_admin_request_timeout",
        default="5",
        description=(
            "Timeout for one loopback call mcc-desktop makes to the server's "
            "admin API. Applies the next time mcc-desktop starts, not to a "
            "tray already running."
        ),
    ),
    ConfigFieldSpec(
        "DESKTOP_ACTIVATION_POLL_SECONDS",
        "Activation poll",
        "desktop",
        "number",
        settings_attr="desktop_activation_poll_seconds",
        default="1",
        advanced=True,
        description=(
            "How often mcc-desktop checks for another launch's \"show my "
            'window" signal. Applies the next time mcc-desktop starts, not '
            "to a tray already running."
        ),
    ),
    ConfigFieldSpec(
        "DESKTOP_HEALTH_POLL_SECONDS",
        "Ongoing health poll",
        "desktop",
        "number",
        settings_attr="desktop_health_poll_seconds",
        default="5",
        description=(
            "How often the running tray probes mcc-server once it is up. "
            "Applies the next time mcc-desktop starts, not to a tray already "
            "running."
        ),
    ),
    ConfigFieldSpec(
        "DESKTOP_HEALTH_FAILURE_THRESHOLD",
        "Outage threshold",
        "desktop",
        "number",
        settings_attr="desktop_health_failure_threshold",
        default="3",
        description=(
            "Consecutive failed health probes before mcc-desktop reports an "
            "outage. This is what keeps a brief self-update restart from "
            "being read as the server dying. Applies the next time "
            "mcc-desktop starts, not to a tray already running."
        ),
    ),
    ConfigFieldSpec(
        "DESKTOP_WINDOW_WIDTH",
        "Window width",
        "desktop",
        "number",
        settings_attr="desktop_window_width",
        default="1400",
        description=(
            "Width, in CSS pixels, of the app-mode/embedded dashboard window. "
            "Applies the next time mcc-desktop starts, not to a window "
            "already open."
        ),
    ),
    ConfigFieldSpec(
        "DESKTOP_WINDOW_HEIGHT",
        "Window height",
        "desktop",
        "number",
        settings_attr="desktop_window_height",
        default="900",
        description=(
            "Height, in CSS pixels, of the app-mode/embedded dashboard "
            "window. Applies the next time mcc-desktop starts, not to a "
            "window already open."
        ),
    ),
    ConfigFieldSpec(
        "ENABLE_TOOL_RESULT_TRIMMING",
        "Trim large tool results",
        "optimizer",
        "boolean",
        settings_attr="enable_tool_result_trimming",
        default="false",
        description=(
            "Master switch for eliding the middle of oversized Read, Grep and "
            "Glob results before they reach the model. Off means the request "
            "goes upstream exactly as Claude Code sent it. This is the only "
            "setting in MCC that changes what the model is allowed to see, so "
            "it beats every per-rule mode below and is off by default. Every "
            "elision carries an inline marker telling the model that content "
            "was removed, by MCC, and how to fetch it -- but a marker is not "
            "the same as the content, and an answer can still be worse for "
            "the gap. Watch a rule in Observe first."
        ),
    ),
    ConfigFieldSpec(
        "TOOL_RESULT_TRIM_READ",
        "Read results",
        "optimizer",
        "select",
        settings_attr="tool_result_trim_read",
        default="off",
        options=_TRIM_MODE_OPTIONS,
        description=_TRIM_MODE_HELP.format(tool="Read"),
    ),
    ConfigFieldSpec(
        "TOOL_RESULT_TRIM_GREP",
        "Grep results",
        "optimizer",
        "select",
        settings_attr="tool_result_trim_grep",
        default="off",
        options=_TRIM_MODE_OPTIONS,
        description=_TRIM_MODE_HELP.format(tool="Grep"),
    ),
    ConfigFieldSpec(
        "TOOL_RESULT_TRIM_GLOB",
        "Glob results",
        "optimizer",
        "select",
        settings_attr="tool_result_trim_glob",
        default="off",
        options=_TRIM_MODE_OPTIONS,
        description=_TRIM_MODE_HELP.format(tool="Glob"),
    ),
    ConfigFieldSpec(
        "TOOL_RESULT_TRIM_THRESHOLD_CHARS",
        "Trim above",
        "optimizer",
        "number",
        settings_attr="tool_result_trim_threshold_chars",
        default="20000",
        description=(
            "A tool result shorter than this is never touched. The default is "
            "the measured 90th percentile of a whole-file Read in a real "
            "repository, so nine reads in ten pass through untouched while the "
            "tenth -- which holds most of the bytes -- is the one considered."
        ),
    ),
    ConfigFieldSpec(
        "TOOL_RESULT_TRIM_KEEP_HEAD_CHARS",
        "Keep from the start",
        "optimizer",
        "number",
        settings_attr="tool_result_trim_keep_head_chars",
        default="4000",
        advanced=True,
        description=(
            "Characters kept before the elision, rounded out to a line "
            "boundary so a path is never cut in half. The head is where the "
            "opening line numbers and the file's shape live."
        ),
    ),
    ConfigFieldSpec(
        "TOOL_RESULT_TRIM_KEEP_TAIL_CHARS",
        "Keep from the end",
        "optimizer",
        "number",
        settings_attr="tool_result_trim_keep_tail_chars",
        default="4000",
        advanced=True,
        description=(
            "Characters kept after the elision, rounded out to a line "
            "boundary. Only the middle is ever removed: the two ends carry the "
            "structure the model needs to act on what is left."
        ),
    ),
    ConfigFieldSpec(
        "TOOL_RESULT_TRIM_PROTECT_RECENT_RESULTS",
        "Never trim the newest",
        "optimizer",
        "number",
        settings_attr="tool_result_trim_protect_recent_results",
        default="2",
        advanced=True,
        description=(
            "How many of the most recent Read/Grep/Glob results are exempt. "
            "The result the model just received is the one it is reasoning "
            "about, and it is also the cheapest to keep whole -- an older "
            "result is re-sent on every later turn, the newest is sent once."
        ),
    ),
    ConfigFieldSpec(
        "DESKTOP_BROWSER_PATH",
        "Browser path",
        "desktop",
        "text",
        settings_attr="desktop_browser_path",
        default="",
        description=(
            "Explicit path to a Chromium-family browser binary (Chrome, "
            "Edge, Brave, or Chromium). When set, it is used instead of the "
            "built-in search, which is otherwise the only way an unusually "
            "installed browser can be found. When set but the file does not "
            "exist, mcc-desktop logs a warning and falls back to the search "
            "instead of failing to open a window. Applies the next time "
            "mcc-desktop starts, not to a window already open."
        ),
    ),
)


def _with_range(field: ConfigFieldSpec) -> ConfigFieldSpec:
    """Attach the usable range to a numeric field, and say so in its help.

    Written here rather than in each spec so the bounds the form enforces are
    the same object the server clamps to; two hand-maintained copies would
    eventually disagree, and the form would accept a value the server changes.
    """
    limit = range_for(field.settings_attr)
    if limit is None:
        return field
    text = f"Accepts {describe_range(limit)}."
    description = f"{field.description} {text}".strip()
    return replace(
        field,
        minimum=limit.minimum,
        maximum=limit.maximum,
        description=description,
    )


FIELDS: tuple[ConfigFieldSpec, ...] = tuple(
    _with_range(field)
    for field in (
        *(ConfigFieldSpec(**spec) for spec in provider_field_specs()),
        *_NON_PROVIDER_FIELDS,
        *(ConfigFieldSpec(**spec) for spec in websearch_field_specs()),
    )
)
FIELD_BY_KEY = {field.key: field for field in FIELDS}


def field_input_key(field: ConfigFieldSpec) -> str | None:
    """Return the Settings input key used for a manifest field."""

    if field.settings_attr is None:
        return None
    model_field = Settings.model_fields[field.settings_attr]
    alias = model_field.validation_alias
    if alias is None:
        return field.settings_attr
    return str(alias)


def env_keys() -> frozenset[str]:
    """Return env keys owned by the admin manifest."""

    return frozenset(field.key for field in FIELDS)


def fields_with_attrs() -> Iterable[ConfigFieldSpec]:
    """Yield fields that validate through Settings."""

    return (field for field in FIELDS if field.settings_attr is not None)
