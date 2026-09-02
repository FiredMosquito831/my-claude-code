"""Catalog-derived Admin provider fields."""

from typing import Any

from my_claude_code.config.provider_catalog import (
    AZURE_OPENAI_BASE_URL_EXAMPLE,
    PROVIDER_CATALOG,
)
from my_claude_code.config.settings import Settings

_PROVIDER_FIELD_OVERRIDES: dict[str, dict[str, Any]] = {
    "NVIDIA_NIM_API_KEY": {
        "label": "NVIDIA NIM API Key",
        "description": (
            "Used by NVIDIA NIM chat and optional NIM voice transcription. "
            "Add as many keys as you like below; requests spread across them."
        ),
    },
    "MISTRAL_API_KEY": {
        "label": "Mistral API Key",
        "description": (
            "Mistral La Plateforme (api.mistral.ai); Experiment plan is free tier with rate limits."
        ),
    },
    "CODESTRAL_API_KEY": {
        "label": "Codestral API Key",
        "description": (
            "Mistral Codestral endpoint (codestral.mistral.ai); distinct from Mistral "
            "La Plateforme ``MISTRAL_API_KEY``. See Mistral docs for coding/FIM domains."
        ),
    },
    "OPENCODE_API_KEY": {
        "label": "OpenCode API Key",
        "description": (
            "OpenCode Zen curated gateway (opencode.ai/zen/v1) and OpenCode Go subscription "
            "gateway (opencode.ai/zen/go/v1); single key from opencode.ai/auth."
        ),
    },
    "AI_GATEWAY_API_KEY": {
        "label": "Vercel AI Gateway API Key",
        "description": (
            "Vercel AI Gateway API key for the OpenAI-compatible endpoint at "
            "ai-gateway.vercel.sh/v1."
        ),
    },
    "HUGGINGFACE_API_KEY": {
        "label": "Hugging Face API Key",
        "description": (
            "Hugging Face token with Inference Providers permission; also used "
            "for local Whisper model downloads when voice notes need gated models."
        ),
    },
    "COHERE_API_KEY": {
        "label": "Cohere API Key",
        "description": "Cohere API key for the OpenAI-compatible Compatibility API.",
    },
    "GITHUB_MODELS_TOKEN": {
        "label": "GitHub Models Token",
        "description": (
            "GitHub token with Models access for the OpenAI-compatible inference API "
            "at models.github.ai."
        ),
    },
    "ZAI_API_KEY": {
        "label": "Z.ai API Key",
        "description": "Z.ai Coding Plan API key.",
    },
    "ALIBABA_CODING_API_KEY": {
        "label": "Alibaba Coding Plan Key (International)",
        "description": (
            "Qwen Coding Plan subscription, Singapore region. The key starts with "
            "`sk-sp-` and comes from the Coding Plan page of "
            "[Model Studio](https://bailian.console.alibabacloud.com/) — a "
            "pay-per-token key will not work here. Pairing the plan with the wrong "
            "endpoint bills pay-as-you-go instead of drawing on the subscription, "
            "so keep this key on this provider. Alibaba restricts the plan to "
            "interactive coding tools."
        ),
    },
    "ALIBABA_CODING_CN_API_KEY": {
        "label": "Alibaba Coding Plan Key (China)",
        "description": (
            "Qwen Coding Plan subscription, Beijing region. Same `sk-sp-` key format "
            "as the international plan but issued separately at "
            "[Model Studio](https://bailian.console.aliyun.com/); the two regions do "
            "not share credentials."
        ),
    },
    "ALIBABA_API_KEY": {
        "label": "Alibaba Token Plan Key (International)",
        "description": (
            "Pay-per-token Model Studio (Bailian) key for the Singapore region, from "
            "[bailian.console.alibabacloud.com](https://bailian.console.alibabacloud.com/). "
            "**Billed per token.** If you hold a Coding Plan subscription, use the "
            "Coding Plan provider instead — a subscription is not consumed through "
            "this endpoint, and requests here bill pay-as-you-go without warning. "
            "Set the Base URL below for another region (e.g. "
            "`dashscope-us.aliyuncs.com`) or a workspace-scoped endpoint."
        ),
    },
    "ALIBABA_CN_API_KEY": {
        "label": "Alibaba Token Plan Key (China)",
        "description": (
            "Pay-per-token Model Studio (Bailian) key for the Beijing region, from "
            "[bailian.console.aliyun.com](https://bailian.console.aliyun.com/). "
            "**Billed per token**, and the two regions are separate accounts — an "
            "international key does not work here. If you hold a Coding Plan "
            "subscription, use the Coding Plan provider instead; requests here bill "
            "pay-as-you-go rather than drawing on the plan."
        ),
    },
    "FIREWORKS_API_KEY": {
        "label": "Fireworks API Key",
        "description": "Fireworks AI inference API key.",
    },
    "NOVITA_API_KEY": {
        "label": "Novita AI API Key",
        "description": (
            "Novita AI OpenAI-compatible API key (create at "
            "[novita.ai/settings](https://novita.ai/settings))."
        ),
    },
    "NOUS_API_KEY": {
        "label": "Nous Portal API Key",
        "description": (
            "Nous Research inference API key (`sk-nous-...`), OpenAI-compatible gateway "
            "with 350+ models (create at [portal.nousresearch.com](https://portal.nousresearch.com/))."
        ),
    },
    "KILO_API_KEY": {
        "label": "Kilo AI Gateway API Key",
        "description": (
            "Kilo AI Gateway API key for the OpenAI-compatible endpoint at "
            "api.kilo.ai/api/gateway (create at [app.kilo.ai](https://app.kilo.ai/))."
        ),
    },
    "ANTHROPIC_API_KEY": {
        "label": "Anthropic API Key",
        "description": (
            "Claude Console API key (`sk-ant-...`) for Anthropic's own Messages API, "
            "billed per token. This is the authentication method Anthropic documents "
            "for software that calls Claude on your behalf "
            "(create at [platform.claude.com](https://platform.claude.com/settings/keys)). "
            "Not the same as `ANTHROPIC_AUTH_TOKEN`, which is the token Claude Code "
            "presents to MCC."
        ),
    },
    "ANTHROPIC_OAUTH_ACCESS_TOKEN": {
        "label": "Claude subscription OAuth token (unsupported)",
        "description": (
            "**Anthropic does not permit this.** Their terms state Claude Free/Pro/Max "
            "OAuth credentials are for Claude Code and Claude.ai only and may not be "
            "routed through a third-party product, and that they may enforce without "
            "notice — the risk is to your Claude account. Read "
            "docs/ANTHROPIC-SUBSCRIPTION.md first. Normally left empty: MCC discovers "
            "the credential from `mcc-anthropic-oauth-login` or from Claude Code's own "
            "`~/.claude/.credentials.json`. A pasted token cannot be refreshed. The "
            "supported alternative is the Anthropic API Key field above."
        ),
    },
    "COMMANDCODE_API_KEY": {
        "label": "Command Code API Key",
        "description": (
            "Command Code Provider-plan API key. MCC routes Claude models through "
            "native Anthropic Messages and other models through OpenAI Chat Completions. "
            "Create a key in [Command Code Studio](https://commandcode.ai/studio)."
        ),
    },
    "CLINE_API_KEY": {
        "label": "Cline API Key",
        "description": (
            "Cline API key for the OpenAI-compatible gateway at api.cline.bot "
            "(create at [app.cline.bot](https://app.cline.bot/) under Settings > API Keys)."
        ),
    },
    "KIMI_CODING_API_KEY": {
        "label": "Kimi Coding API Key",
        "description": (
            "Kimi For Coding subscription API key. "
            "Used for the OpenAI-compatible Chat Completions API at api.kimi.com/coding."
        ),
    },
    "CHATGPT_OAUTH_ACCESS_TOKEN": {
        "label": "ChatGPT OAuth Credential",
        "description": (
            "Experimental/unsanctioned: managed by the login/import buttons. "
            "MCC stores renewable credentials privately under ~/.fcc/auth. "
            "A raw access token may still be entered as an advanced override, "
            "but it cannot be refreshed. "
            "Use at your own risk; this flow is not an official OpenAI API product."
        ),
    },
    "CHATGPT_OAUTH_BASE_URL": {
        "label": "ChatGPT OAuth Base URL",
        "description": (
            "Experimental/unsanctioned: ChatGPT/Codex backend root. Defaults to "
            "https://chatgpt.com/backend-api. Only change this if you need to "
            "route through a proxy or mirror."
        ),
    },
    "CHATGPT_OAUTH_PROXY": {
        "label": "ChatGPT OAuth Proxy",
        "description": (
            "Experimental/unsanctioned: optional HTTP proxy for ChatGPT/Codex requests."
        ),
    },
    "OPENAI_PROXY": {
        "label": "OpenAI / ChatGPT Proxy",
        "description": (
            "Optional proxy used for OpenAI sign-in and ChatGPT Codex requests. "
            "Changing it restarts FCC."
        ),
        "restart_required": True,
    },
    "MINIMAX_API_KEY": {
        "label": "MiniMax API Key",
        "description": (
            "MiniMax API key for the OpenAI-compatible Chat Completions API at "
            "my_claude_code.api.minimax.io/v1."
        ),
    },
    "CLOUDFLARE_API_TOKEN": {
        "label": "Cloudflare API Token",
        "description": (
            "Cloudflare API token for account-scoped AI REST requests. "
            "Use with CLOUDFLARE_ACCOUNT_ID."
        ),
    },
    "GEMINI_API_KEY": {
        "label": "Gemini API Key",
        "description": (
            "Google AI Studio Gemini API key (Google AI Studio / Gemini API "
            "[OpenAI-compatible](https://ai.google.dev/gemini-api/docs/openai)); "
            "free tier has per-model rate limits and data may be used for improvement "
            "outside the UK/CH/EEA/EU."
        ),
    },
    "GROQ_API_KEY": {
        "label": "Groq API Key",
        "description": (
            "GroqCloud OpenAI-compatible API key ([console.groq.com/keys]("
            "https://console.groq.com/keys)); see Groq "
            "[OpenAI compatibility docs](https://console.groq.com/docs/openai)."
        ),
    },
    "SAMBANOVA_API_KEY": {
        "label": "SambaNova API Key",
        "description": (
            "SambaNova Cloud OpenAI-compatible API key (create at "
            "[cloud.sambanova.ai/apis](https://cloud.sambanova.ai/apis))."
        ),
    },
    "CEREBRAS_API_KEY": {
        "label": "Cerebras API Key",
        "description": (
            "Cerebras Inference API key (create in [Cloud Console](https://cloud.cerebras.ai)); "
            "see [Quickstart](https://inference-docs.cerebras.ai/quickstart) and "
            "[OpenAI compatibility](https://inference-docs.cerebras.ai/resources/openai)."
        ),
    },
    "OLLAMA_API_KEY": {
        "description": (
            "Ollama API key for direct OpenAI-compatible Cloud access at ollama.com/v1."
        ),
    },
    "AZURE_OPENAI_API_KEY": {
        "label": "Azure OpenAI Key",
        "description": (
            "Key for your Azure OpenAI resource, from the resource's Keys and "
            "Endpoint page in the [Azure portal](https://portal.azure.com/). "
            "Set the Base URL below as well — it names your own resource, so "
            "there is no default that could work. The model you request is "
            "your **deployment name**, not the underlying model name."
        ),
    },
    "AZURE_OPENAI_BASE_URL": {
        "label": "Azure OpenAI Base URL",
        "description": (
            "Required. Your resource endpoint with the v1 path appended, e.g. "
            f"`{AZURE_OPENAI_BASE_URL_EXAMPLE}`. Use this `/openai/v1/` form "
            "rather than the older `/openai/deployments/...?api-version=` one: "
            "it speaks plain OpenAI Chat Completions, which is the dialect MCC "
            "sends."
        ),
    },
    "AWS_BEARER_TOKEN_BEDROCK": {
        "label": "Amazon Bedrock API Key",
        "description": (
            "Amazon Bedrock bearer API key for the region-specific Mantle "
            "OpenAI-compatible endpoint."
        ),
    },
    "BEDROCK_BASE_URL": {
        "description": (
            "Amazon Bedrock Mantle OpenAI base URL for the same region as the "
            "API key and selected models."
        ),
    },
    "TOKENROUTER_API_KEY": {
        "label": "TokenRouter API Key",
        "description": (
            "TokenRouter OpenAI-compatible gateway API key for api.tokenrouter.com/v1."
        ),
    },
    "TOKENROUTER_BASE_URL": {
        "description": (
            "TokenRouter OpenAI-compatible Chat Completions base URL. "
            "Defaults to https://api.tokenrouter.com/v1."
        ),
    },
    "NARAROUTE_API_KEY": {
        "label": "NaraRoute API Key",
        "description": (
            "NaraRoute OpenAI-compatible gateway API key for router.bynara.id/v1. "
            "Keys begin with sk-nry-; create one at router.bynara.id/keys."
        ),
    },
    "NARAROUTE_BASE_URL": {
        "description": (
            "NaraRoute OpenAI-compatible Chat Completions base URL. "
            "Defaults to https://router.bynara.id/v1."
        ),
    },
    "QWENCLOUD_API_KEY": {
        "label": "QwenCloud Token Plan API Key",
        "description": (
            "Dedicated QwenCloud Token Plan key (sk-sp-...). Token Plan, Coding "
            "Plan, and pay-as-you-go keys use separate endpoints and cannot be "
            "mixed."
        ),
    },
    "QWENCLOUD_CODING_API_KEY": {
        "label": "QwenCloud Coding Plan API Key",
        "description": (
            "Dedicated QwenCloud Coding Plan key (sk-sp-...) for personal, "
            "interactive coding-agent use. Token Plan, Coding Plan, and "
            "pay-as-you-go keys use separate endpoints and cannot be mixed."
        ),
    },
    "AGNES_API_KEY": {
        "label": "Agnes AI API Key",
        "description": (
            "Agnes AI OpenAI-compatible API key for apihub.agnes-ai.com/v1."
        ),
    },
    "XAI_API_KEY": {
        "label": "xAI API Key",
        "description": (
            "xAI OpenAI-compatible API key for Grok chat and image-understanding "
            "models."
        ),
    },
    "TOGETHER_API_KEY": {
        "label": "Together AI API Key",
        "description": (
            "Together AI OpenAI-compatible API key for serverless and dedicated "
            "chat models."
        ),
    },
    "DEEPINFRA_API_KEY": {
        "label": "DeepInfra API Key",
        "description": (
            "DeepInfra API key for OpenAI-compatible chat and reasoning models."
        ),
    },
    "SILICONFLOW_API_KEY": {
        "label": "SiliconFlow API Key",
        "description": (
            "SiliconFlow API key for OpenAI-compatible chat, reasoning, and "
            "vision models."
        ),
    },
    "NEBIUS_API_KEY": {
        "label": "Nebius Token Factory API Key",
        "description": (
            "Nebius Token Factory API key for OpenAI-compatible chat, reasoning, "
            "and tool-capable models."
        ),
    },
    "CHUTES_API_KEY": {
        "label": "Chutes API Key",
        "description": (
            "Chutes API key for OpenAI-compatible chat, reasoning, and "
            "tool-capable models."
        ),
    },
    "FEATHERLESS_API_KEY": {
        "label": "Featherless AI API Key",
        "description": (
            "Featherless AI API key for plan-available OpenAI-compatible chat, "
            "reasoning, and tool-capable models."
        ),
    },
    "WANDB_API_KEY": {
        "label": "W&B Inference API Key",
        "description": (
            "W&B API key for Serverless Inference at api.inference.wandb.ai/v1. "
            "Create one in [W&B User Settings](https://wandb.ai/settings)."
        ),
    },
    "ZENMUX_API_KEY": {
        "label": "ZenMux API Key",
        "description": (
            "ZenMux OpenAI-compatible gateway API key for zenmux.ai/api/v1. "
            "Create one at zenmux.ai/platform/pay-as-you-go."
        ),
    },
}


def provider_field_specs() -> tuple[dict[str, Any], ...]:
    """Return provider fields generated from the provider catalog."""

    return (
        *_credential_field_specs(),
        *_rotation_field_specs(),
        *_chatgpt_oauth_login_field_specs(),
        *_chatgpt_oauth_account_field_specs(),
        *_anthropic_oauth_login_field_specs(),
        *_cloudflare_account_field_specs(),
        *_vertex_field_specs(),
        *_base_url_field_specs(),
        *_proxy_field_specs(),
    )


def _vertex_field_specs() -> tuple[dict[str, Any], ...]:
    return (
        {
            "key": "VERTEX_PROJECT_ID",
            "label": "Google Cloud Project ID",
            "section_id": "providers",
            "provider": "vertex",
            "settings_attr": "vertex_project_id",
            "description": (
                "Google Cloud project used for Vertex AI. Authentication uses "
                "Application Default Credentials (ADC)."
            ),
        },
        {
            "key": "VERTEX_LOCATION",
            "label": "Vertex AI Location",
            "section_id": "providers",
            "provider": "vertex",
            "settings_attr": "vertex_location",
            "description": (
                "Use global for the global Vertex AI endpoint or a region such as "
                "us-central1."
            ),
        },
    )


def credential_env_owner(credential_env: str) -> str | None:
    """Return the provider that owns the editable field for one credential.

    Two providers may legitimately share one key -- OpenCode Zen and OpenCode
    Go are one account behind two gateways. Only the first in catalog order
    gets the field, because a second input bound to the same env var would put
    two controls on the page writing one value. The other providers still need
    to say where their key is managed, which is what this answers.
    """
    for descriptor in PROVIDER_CATALOG.values():
        if descriptor.credential_env == credential_env:
            return descriptor.provider_id
    return None


def _credential_field_specs() -> tuple[dict[str, Any], ...]:
    specs: list[dict[str, Any]] = []
    seen_env_keys: set[str] = set()
    for descriptor in PROVIDER_CATALOG.values():
        if descriptor.credential_env is None:
            continue
        # See credential_env_owner: one field per credential, not per provider.
        if descriptor.credential_env in seen_env_keys:
            continue
        seen_env_keys.add(descriptor.credential_env)
        spec = {
            "key": descriptor.credential_env,
            "label": f"{descriptor.display_name} API Key",
            "section_id": "providers",
            "provider": descriptor.provider_id,
            "field_type": "secret",
            "settings_attr": descriptor.credential_attr,
            "secret": True,
        }
        spec.update(_PROVIDER_FIELD_OVERRIDES.get(descriptor.credential_env, {}))
        specs.append(spec)
    return tuple(specs)


def _rotation_field_specs() -> tuple[dict[str, Any], ...]:
    specs: list[dict[str, Any]] = []
    seen_env_keys: set[str] = set()
    for descriptor in PROVIDER_CATALOG.values():
        if descriptor.credential_env is None:
            continue
        if descriptor.credential_env in seen_env_keys:
            continue
        seen_env_keys.add(descriptor.credential_env)
        specs.append(
            {
                "key": f"{descriptor.credential_env}_ROTATION",
                "label": f"{descriptor.display_name} Key Rotation",
                "section_id": "providers",
                "provider": descriptor.provider_id,
                "field_type": "select",
                "default": "single",
                "options": ("single", "round_robin", "least_used", "failover"),
                "advanced": True,
                "restart_required": True,
                "description": (
                    "Rotation policy across the keys you have added for "
                    f"{descriptor.credential_env}. single = use the first key only; "
                    "round_robin = spread requests across healthy keys; "
                    "least_used = least-used healthy key first; "
                    "failover = stick to one key until it fails, then move on. "
                    "A key is benched only for what the provider says about the "
                    "key itself: an escalating lockout on 401/403, a 429 "
                    "bench lasting exactly as long as the provider asked, and "
                    "a RATE_LIMIT_COOLDOWN_SECONDS bench when the provider "
                    "says in words that the account is out of credits. "
                    "Timeouts, 5xx and every other 4xx leave every key "
                    "untouched and move the fallback chain to the next model. "
                    "Requires restart."
                ),
            }
        )
    return tuple(specs)


def _base_url_field_specs() -> tuple[dict[str, Any], ...]:
    """Base URL fields for providers whose endpoint is the user's to choose.

    Local runtimes and region-specific hosts ship a usable default; Azure
    OpenAI does not, because the host carries the customer's resource name.
    """
    specs: list[dict[str, Any]] = []
    for descriptor in PROVIDER_CATALOG.values():
        if descriptor.base_url_attr is None:
            continue
        key = _settings_env_key(descriptor.base_url_attr)
        spec = {
            "key": key,
            "label": f"{descriptor.display_name} Base URL",
            "section_id": "providers",
            "provider": descriptor.provider_id,
            "settings_attr": descriptor.base_url_attr,
            "default": descriptor.default_base_url or "",
        }
        spec.update(_PROVIDER_FIELD_OVERRIDES.get(key, {}))
        specs.append(spec)
    return tuple(specs)


def _chatgpt_oauth_account_field_specs() -> tuple[dict[str, Any], ...]:
    return (
        {
            "key": "CHATGPT_OAUTH_ACCOUNT_ID",
            "label": "ChatGPT OAuth Account ID",
            "section_id": "providers",
            "provider": "chatgpt_oauth",
            "settings_attr": "chatgpt_oauth_account_id",
            "description": (
                "Experimental/unsanctioned: ChatGPT account ID used for the "
                "ChatGPT-Account-ID header. Leave empty when using MCC-managed "
                "OAuth credentials so it is resolved from the current token."
            ),
        },
    )


def _chatgpt_oauth_login_field_specs() -> tuple[dict[str, Any], ...]:
    return (
        {
            "key": "CHATGPT_OAUTH_LOGIN",
            "label": "ChatGPT OAuth Login",
            "section_id": "providers",
            "provider": "chatgpt_oauth",
            "field_type": "oauth_login",
            "description": (
                "Experimental/unsanctioned: device-code login works across WSL and "
                "remote environments. Browser login is available only when the browser "
                "and MCC share the same localhost. Renewable credentials are saved in "
                "MCC's private auth store; Codex CLI credentials are not modified. "
                "Use at your own risk; this is not an official OpenAI API product."
            ),
        },
        {
            "key": "CHATGPT_OAUTH_IMPORT_CODEX",
            "label": "Import Codex CLI Tokens",
            "section_id": "providers",
            "provider": "chatgpt_oauth",
            "field_type": "oauth_login",
            "description": (
                "Experimental/unsanctioned: if you have already run 'codex login', "
                "copy its renewable credentials into MCC's private auth store. "
                "The Codex CLI file remains unchanged."
            ),
        },
    )


def _anthropic_oauth_login_field_specs() -> tuple[dict[str, Any], ...]:
    return (
        {
            "key": "ANTHROPIC_OAUTH_MANAGE",
            "label": "Claude Subscription Credential",
            "section_id": "providers",
            "provider": "anthropic_oauth",
            "field_type": "oauth_login",
            "description": (
                "Anthropic does not permit this: read "
                "docs/ANTHROPIC-SUBSCRIPTION.md before using either option. "
                "Import the credential Claude Code already has, or sign in "
                "directly; either way MCC stores its own renewable copy and "
                "never writes back to Claude Code's credential file."
            ),
        },
        {
            "key": "ANTHROPIC_OAUTH_REQUIRE_CLAUDE_CODE",
            "label": "Only Serve Claude Code And The Agent SDK",
            "section_id": "providers",
            "provider": "anthropic_oauth",
            "field_type": "boolean",
            "settings_attr": "anthropic_oauth_require_claude_code",
            "default": "true",
            "restart_required": True,
            "description": (
                "On (the default), any request that did not come from "
                "Anthropic's own clients -- the Claude Code CLI or the Claude "
                "Agent SDK -- is refused. Turning it off routes every other "
                "harness onto the subscription credential, which is the case "
                "Anthropic's policy names explicitly -- read "
                "docs/ANTHROPIC-SUBSCRIPTION.md first."
            ),
        },
    )


def _cloudflare_account_field_specs() -> tuple[dict[str, Any], ...]:
    return (
        {
            "key": "CLOUDFLARE_ACCOUNT_ID",
            "label": "Cloudflare Account ID",
            "section_id": "providers",
            "provider": "cloudflare",
            "settings_attr": "cloudflare_account_id",
            "description": (
                "Cloudflare account ID used to build the /accounts/{id}/ai/v1 endpoint."
            ),
        },
    )


def _proxy_field_specs() -> tuple[dict[str, Any], ...]:
    specs: list[dict[str, Any]] = []
    for descriptor in PROVIDER_CATALOG.values():
        if descriptor.proxy_attr is None:
            continue
        specs.append(
            {
                "key": _settings_env_key(descriptor.proxy_attr),
                "label": f"{descriptor.display_name} Proxy",
                "section_id": "providers",
                "provider": descriptor.provider_id,
                "field_type": "secret",
                "settings_attr": descriptor.proxy_attr,
                "secret": True,
                "advanced": True,
            }
        )
    return tuple(specs)


def _settings_env_key(settings_attr: str) -> str:
    model_field = Settings.model_fields[settings_attr]
    alias = model_field.validation_alias
    return str(alias) if alias is not None else settings_attr
