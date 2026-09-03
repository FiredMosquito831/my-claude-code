"""Freeze ``PROVIDER_CATALOG`` insertion order used as canonical provider ranking."""

from my_claude_code.config.provider_catalog import (
    PROVIDER_CATALOG,
    SUPPORTED_PROVIDER_IDS,
)

_EXPECTED_PROVIDER_ORDER: tuple[str, ...] = (
    "anthropic",
    "anthropic_oauth",
    "nvidia_nim",
    "openai",
    "open_router",
    "gemini",
    "vertex",
    "azure_openai",
    "deepseek",
    "mistral",
    "mistral_codestral",
    "opencode",
    "opencode_go",
    "vercel",
    "huggingface",
    "cohere",
    "github_models",
    "wafer",
    "kimi",
    "kimi_coding",
    "chatgpt_oauth",
    "minimax",
    "cerebras",
    "groq",
    "sambanova",
    "fireworks",
    "novita",
    "nous_portal",
    "kilo",
    "commandcode",
    "cline",
    "cloudflare",
    "zai",
    "qwencloud",
    "qwencloud_coding",
    "agnes",
    "zenmux",
    "wandb",
    "bedrock",
    "tokenrouter",
    "nararoute",
    "hypercharm",
    "xai",
    "together",
    "deepinfra",
    "siliconflow",
    "nebius",
    "chutes",
    "featherless",
    "alibaba_coding",
    "alibaba_coding_cn",
    "alibaba",
    "alibaba_cn",
    "ollama_cloud",
    "lmstudio",
    "llamacpp",
    "ollama",
)


def test_provider_catalog_key_order_matches_canonical_plan() -> None:
    """NIM first; OpenCode pair stays adjacent; gateways precede native remotes."""

    assert tuple(PROVIDER_CATALOG.keys()) == _EXPECTED_PROVIDER_ORDER
    assert SUPPORTED_PROVIDER_IDS == _EXPECTED_PROVIDER_ORDER
