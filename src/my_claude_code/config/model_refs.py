"""Provider-prefixed model reference helpers."""

from dataclasses import dataclass
from typing import Protocol

MODEL_REF_LIST_SEPARATOR = ","


@dataclass(frozen=True, slots=True)
class ConfiguredChatModelRef:
    """A unique configured chat model reference and the env keys that set it."""

    model_ref: str
    provider_id: str
    model_id: str
    sources: tuple[str, ...]


class ChatModelConfig(Protocol):
    model: str
    model_mythos: str | None
    model_fable: str | None
    model_opus: str | None
    model_sonnet: str | None
    model_haiku: str | None
    model_fallbacks: str | None
    model_mythos_fallbacks: str | None
    model_fable_fallbacks: str | None
    model_opus_fallbacks: str | None
    model_sonnet_fallbacks: str | None
    model_haiku_fallbacks: str | None
    model_vision: str | None
    model_vision_fallbacks: str | None


def parse_provider_type(model_ref: str) -> str:
    """Extract provider type from any 'provider/model' string."""

    return model_ref.split("/", 1)[0]


def parse_model_name(model_ref: str) -> str:
    """Extract model name from any 'provider/model' string."""

    return model_ref.split("/", 1)[1]


def parse_model_ref_list(raw: str | None) -> tuple[str, ...]:
    """Split a comma-separated fallback chain into unique ordered model refs.

    Duplicates are dropped rather than rejected: a chain that retries the same
    provider/model twice would burn an attempt on something that just failed.
    """

    if not raw:
        return ()
    refs: list[str] = []
    for candidate in raw.split(MODEL_REF_LIST_SEPARATOR):
        model_ref = candidate.strip()
        if model_ref and model_ref not in refs:
            refs.append(model_ref)
    return tuple(refs)


def format_model_ref_list(model_refs: tuple[str, ...]) -> str:
    """Render a fallback chain back into its canonical env-var form."""

    return MODEL_REF_LIST_SEPARATOR.join(model_refs)


def configured_chat_model_refs(
    settings: ChatModelConfig,
) -> tuple[ConfiguredChatModelRef, ...]:
    """Return unique configured chat provider/model refs with source env keys.

    Fallback chains are included so their providers are discovered and cached
    like any primary route; a chain entry whose provider was never warmed would
    only be found out about at the moment it is needed.
    """

    candidates: list[tuple[str, str | None]] = [
        ("MODEL", settings.model),
        ("MODEL_MYTHOS", settings.model_mythos),
        ("MODEL_FABLE", settings.model_fable),
        ("MODEL_OPUS", settings.model_opus),
        ("MODEL_SONNET", settings.model_sonnet),
        ("MODEL_HAIKU", settings.model_haiku),
        ("MODEL_VISION", settings.model_vision),
    ]
    chains = (
        ("MODEL_FALLBACKS", settings.model_fallbacks),
        ("MODEL_MYTHOS_FALLBACKS", settings.model_mythos_fallbacks),
        ("MODEL_FABLE_FALLBACKS", settings.model_fable_fallbacks),
        ("MODEL_OPUS_FALLBACKS", settings.model_opus_fallbacks),
        ("MODEL_SONNET_FALLBACKS", settings.model_sonnet_fallbacks),
        ("MODEL_HAIKU_FALLBACKS", settings.model_haiku_fallbacks),
        ("MODEL_VISION_FALLBACKS", settings.model_vision_fallbacks),
    )
    for source, raw in chains:
        candidates.extend(
            (source, model_ref) for model_ref in parse_model_ref_list(raw)
        )

    sources_by_ref: dict[str, list[str]] = {}
    for source, model_ref in candidates:
        if model_ref is None:
            continue
        sources = sources_by_ref.setdefault(model_ref, [])
        if source not in sources:
            sources.append(source)

    return tuple(
        ConfiguredChatModelRef(
            model_ref=model_ref,
            provider_id=parse_provider_type(model_ref),
            model_id=parse_model_name(model_ref),
            sources=tuple(sources),
        )
        for model_ref, sources in sources_by_ref.items()
    )
