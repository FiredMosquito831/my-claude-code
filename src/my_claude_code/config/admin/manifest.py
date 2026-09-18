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
        "budgets",
        "Output & thinking budgets",
        "How many tokens one answer may be, and how they are split between "
        "thinking and the answer. A per-model limit published by the provider "
        "always wins over anything here. One bound is not on this card: "
        "NVIDIA NIM carries its own max_tokens in the nested NIM settings, and "
        "where it is set it lowers whatever this card resolved, for NIM "
        "routes only.",
    ),
    ConfigSectionSpec(
        "deadlines",
        "Deadlines",
        "How long one model may hold a request before the chain moves on. "
        "Every deadline here ships at 0, meaning no limit: out of the box "
        "MCC never ends a silent or stalled upstream itself, and the "
        "fallback chain moves only on an error the provider returns. The "
        "silent-attempt floor is the exception, and is not a deadline: it "
        "ends nothing, it only bounds how small a slice of the total budget "
        "one model may be cut to, and with the budget at 0 it does nothing "
        "either. Set the ones you want and the readout below this grid "
        "computes what each model on your own routes actually gets.",
    ),
    ConfigSectionSpec(
        "benching",
        "Chain benching",
        "Whether a model that keeps failing is skipped for a while, and on "
        "what evidence. With benching off, every control in this card is "
        "inert and a failing model is retried at every request.",
    ),
    ConfigSectionSpec(
        "provider_retries",
        "Provider retries & throughput",
        "How hard one model is retried, and how fast requests are allowed to "
        "leave, before the fallback chain is used at all.",
    ),
    ConfigSectionSpec(
        "credential_health",
        "Credential health",
        "What one API key's failures cost it, and how long it sits out. A rate "
        "limit is scoped to the model it happened on unless several models are "
        "limited on the same key. "
        "Rotation policy is per pool and lives on each provider's card.",
    ),
    ConfigSectionSpec(
        "cost",
        "Cost estimation",
        "What each request cost, and who said so. The ladder is the one every "
        "other model fact walks: the host's own reported figure first, then "
        "this provider's models.dev bucket, then LiteLLM's price map if you "
        "turn it on, then a cross-provider vote. A request nothing prices is "
        "stored as not priced and shown as a dash, never as $0.00, which "
        "would be a claim it was free. Reported and estimated amounts are "
        "shown side by side on every card and are never added together.",
    ),
    ConfigSectionSpec(
        "request_log",
        "Request log storage",
        "What the request log keeps, and therefore what the tables above and "
        "content search can ever show you. Every field here is read at "
        "startup.",
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
        "Logging and debugging flags. The log level decides how much the "
        "server writes at all; the flags below add specific payloads to it.",
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
        "MODEL_PAUSED",
        "Default Route Paused Models",
        "models",
        "text",
        settings_attr="model_paused",
        default="",
        description=(
            "Comma-separated provider/model refs on this route that are "
            "switched off. A paused model is never tried and costs no "
            "attempt, but it keeps its place in the chain and still "
            "appears in the request log as skipped. Pausing stops a model "
            "being tried. Hiding (Models page) only removes it from "
            "listings and never changes routing. Written by the Pause "
            "button on Model Config rather than typed here."
        ),
        affects_providers=False,
    ),
    ConfigFieldSpec(
        "MODEL_MYTHOS",
        "Mythos Override",
        "models",
        "optional_model",
        settings_attr="model_mythos",
    ),
    ConfigFieldSpec(
        "MODEL_MYTHOS_FALLBACKS",
        "Mythos Fallback Chain",
        "models",
        "model_chain",
        settings_attr="model_mythos_fallbacks",
    ),
    ConfigFieldSpec(
        "MODEL_MYTHOS_PAUSED",
        "Mythos Paused Models",
        "models",
        "text",
        settings_attr="model_mythos_paused",
        default="",
        description=(
            "Comma-separated provider/model refs on this route that are "
            "switched off. A paused model is never tried and costs no "
            "attempt, but it keeps its place in the chain and still "
            "appears in the request log as skipped. Pausing stops a model "
            "being tried. Hiding (Models page) only removes it from "
            "listings and never changes routing. Written by the Pause "
            "button on Model Config rather than typed here."
        ),
        affects_providers=False,
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
        "MODEL_FABLE_PAUSED",
        "Fable Paused Models",
        "models",
        "text",
        settings_attr="model_fable_paused",
        default="",
        description=(
            "Comma-separated provider/model refs on this route that are "
            "switched off. A paused model is never tried and costs no "
            "attempt, but it keeps its place in the chain and still "
            "appears in the request log as skipped. Pausing stops a model "
            "being tried. Hiding (Models page) only removes it from "
            "listings and never changes routing. Written by the Pause "
            "button on Model Config rather than typed here."
        ),
        affects_providers=False,
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
        "MODEL_OPUS_PAUSED",
        "Opus Paused Models",
        "models",
        "text",
        settings_attr="model_opus_paused",
        default="",
        description=(
            "Comma-separated provider/model refs on this route that are "
            "switched off. A paused model is never tried and costs no "
            "attempt, but it keeps its place in the chain and still "
            "appears in the request log as skipped. Pausing stops a model "
            "being tried. Hiding (Models page) only removes it from "
            "listings and never changes routing. Written by the Pause "
            "button on Model Config rather than typed here."
        ),
        affects_providers=False,
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
        "MODEL_SONNET_PAUSED",
        "Sonnet Paused Models",
        "models",
        "text",
        settings_attr="model_sonnet_paused",
        default="",
        description=(
            "Comma-separated provider/model refs on this route that are "
            "switched off. A paused model is never tried and costs no "
            "attempt, but it keeps its place in the chain and still "
            "appears in the request log as skipped. Pausing stops a model "
            "being tried. Hiding (Models page) only removes it from "
            "listings and never changes routing. Written by the Pause "
            "button on Model Config rather than typed here."
        ),
        affects_providers=False,
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
        "MODEL_HAIKU_PAUSED",
        "Haiku Paused Models",
        "models",
        "text",
        settings_attr="model_haiku_paused",
        default="",
        description=(
            "Comma-separated provider/model refs on this route that are "
            "switched off. A paused model is never tried and costs no "
            "attempt, but it keeps its place in the chain and still "
            "appears in the request log as skipped. Pausing stops a model "
            "being tried. Hiding (Models page) only removes it from "
            "listings and never changes routing. Written by the Pause "
            "button on Model Config rather than typed here."
        ),
        affects_providers=False,
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
        "VISION_ADAPTER_MODE",
        "Vision Adapter Mode",
        "models",
        "select",
        settings_attr="vision_adapter_mode",
        default="route",
        options=(
            ConfigOptionSpec("route", "Send the request to the vision model"),
            ConfigOptionSpec("describe", "Describe the image, keep the model"),
        ),
        description=(
            "What the adapter does when a request carries an image and the "
            "model its tier picked is published as unable to read one. Route "
            "hands the whole request to the vision model, so the vision model "
            "answers -- that is what every release before 6.51.0 did, and it "
            "is still the default. Describe asks the vision model what each "
            "picture shows, puts its answer in the picture's place, and lets "
            "the model the tier actually picked answer the question. Pick "
            "describe when your coding model is fast, cheap and blind and the "
            "screenshot is context rather than the question; pick route when "
            "the picture is the question. Descriptions are cached against the "
            "image itself, so a screenshot re-sent every turn is described "
            "once."
        ),
    ),
    ConfigFieldSpec(
        "TOOL_RESULT_IMAGE_DELIVERY",
        "Tool-Returned Images",
        "models",
        "select",
        settings_attr="tool_result_image_delivery",
        default="auto",
        options=(
            ConfigOptionSpec("auto", "Auto"),
            ConfigOptionSpec("attach", "Always attach"),
            ConfigOptionSpec("strip", "Never attach"),
        ),
        description=(
            "What happens to an image a tool handed back -- a screenshot, a "
            "Read of a PNG -- when the model that answers is not an Anthropic "
            "one. No OpenAI-format chat message can carry an image inside a "
            "tool result, so it is moved into a short user message right after "
            "it, marked as tool output. Auto does that unless the model is "
            "published as not accepting images, in which case a plain sentence "
            "takes its place; an unpublished capability counts as yes, the "
            "same rule the vision adapter above uses. Always attach ignores "
            "that capability. Never attach replaces every tool-returned image "
            "with the sentence. There is no option to send the picture as "
            "base64 text: that was the pre-6.49.0 bug, and it billed roughly "
            "one token per byte for something the model never saw."
        ),
    ),
    ConfigFieldSpec(
        "IMAGE_MAX_LONG_EDGE",
        "Shrink Images Larger Than (px)",
        "models",
        "number",
        settings_attr="image_max_long_edge",
        default="1568",
        description=(
            "The longest edge an image may have when it leaves the proxy. "
            "Anything larger is resized once, before it is sent, and the "
            "request detail shows the before and after. 1568 is the number "
            "Anthropic itself resizes to and the number Claude Code ships, and "
            "the resize also respects the destination's own token budget -- "
            "which is why a 1920x1080 screenshot lands on 1456x819 rather than "
            "1568x882. On most hosts this changes nothing you can see, because "
            "they already resize server-side and bill the resized picture: "
            "Anthropic, OpenAI's tile-billed models and Gemini all come out on "
            "exactly the same token count either way. On OpenAI's newer "
            "patch-billed models it is a real 41% saving and a real fidelity "
            "cut -- the model genuinely sees less -- which is why this is one "
            "box and not a hidden constant. Set 0 to send whatever the client "
            "sent, byte for byte. The picture stored in the request log is "
            "unaffected: that has always been a thumbnail."
        ),
    ),
    ConfigFieldSpec(
        "IMAGE_JPEG_QUALITY",
        "Re-encode Resized Images as JPEG (quality)",
        "models",
        "number",
        settings_attr="image_jpeg_quality",
        default="0",
        description=(
            "Off by default, and 0 means off: a PNG stays a PNG. Re-encoding "
            "is where the bandwidth actually is, but the images in question "
            "are screenshots of text and code, which is the worst case for "
            "JPEG ringing. 85 is the usual opt-in. Any image carrying an alpha "
            "channel is skipped whatever this says, because flattening "
            "transparency is a change nobody asked for when they asked for a "
            "smaller file."
        ),
    ),
    ConfigFieldSpec(
        "IMAGE_DETAIL",
        "Image Detail (OpenAI hosts)",
        "models",
        "select",
        settings_attr="image_detail",
        default="auto",
        options=(
            ConfigOptionSpec("auto", "Auto (send no detail field)"),
            ConfigOptionSpec("low", "Low (thumbnail, base tokens only)"),
            ConfigOptionSpec("high", "High"),
        ),
        description=(
            "OpenAI's per-image fidelity knob, and meaningless to every other "
            "host. Auto sends no detail field at all, which is what OpenAI "
            "applies anyway and what every release before 6.53.0 did. Low is a "
            "large, silent fidelity cut: it bills the base tokens only and the "
            "model is shown a thumbnail, so it will confidently misread a "
            "screenshot rather than say it cannot see one. This never changes "
            "the token estimate -- the estimate is a function of the picture's "
            "real dimensions and the host's published formula, not of this "
            "field, which is the mistake that makes LiteLLM charge every "
            "Anthropic image a flat 85 tokens."
        ),
    ),
    ConfigFieldSpec(
        "MODEL_VISION_PAUSED",
        "Vision Paused Models",
        "models",
        "text",
        settings_attr="model_vision_paused",
        default="",
        description=(
            "Comma-separated provider/model refs on this route that are "
            "switched off. A paused model is never tried and costs no "
            "attempt, but it keeps its place in the chain and still "
            "appears in the request log as skipped. Pausing stops a model "
            "being tried. Hiding (Models page) only removes it from "
            "listings and never changes routing. Written by the Pause "
            "button on Model Config rather than typed here."
        ),
        affects_providers=False,
    ),
    ConfigFieldSpec(
        "MODEL_VISIBILITY_ALLOW",
        "Only list these models",
        "models",
        "text",
        settings_attr="model_visibility_allow",
        default="",
        description=(
            "Comma-separated glob patterns matched against the full "
            "provider/model reference, case-insensitively -- for example "
            "nvidia_nim/*, *:free, *inkling*, or one exact ref. Leave empty "
            "to list every model a provider publishes. This changes only "
            "what is listed: which models appear in /v1/models and in this "
            "page's pickers. It is not an access control and it does not "
            "disable anything. A model left off this list still routes and "
            "still serves requests when a route or a fallback chain above "
            "names it."
        ),
        # Hide-only, by contract (``core/model_visibility.py``: "Nothing here
        # may affect routing"). Both consumers -- the /v1/models response and
        # the catalogue documents -- read this off Settings when they build,
        # never off a provider client, so re-querying every provider's /models
        # after a Hide click cannot change what the click does. It only made
        # the click cost a network sweep.
        affects_providers=False,
    ),
    ConfigFieldSpec(
        "MODEL_VISIBILITY_DENY",
        "Hide these models from the listings",
        "models",
        "text",
        settings_attr="model_visibility_deny",
        default="",
        description=(
            "Comma-separated glob patterns, same form as above. Applied after "
            "the allow list and wins over it, so a broad allow can be trimmed "
            "without listing every survivor. This hides models from "
            "/v1/models and from this page's pickers -- nothing more. It does "
            "not block, disable or unroute a model: one named in MODEL, "
            "MODEL_OPUS or a MODEL_*_FALLBACKS chain is still tried and still "
            "answers, it is simply not listed. To stop using a model, take it "
            "out of the route that names it."
        ),
        # Same reasoning as MODEL_VISIBILITY_ALLOW above.
        affects_providers=False,
    ),
    ConfigFieldSpec(
        "HARNESS_TIER_ALIASES",
        "List MCC tiers in coding agents",
        "models",
        "boolean",
        settings_attr="harness_tier_aliases",
        default="true",
        description=(
            "Add mcc/cyber, mcc/best, mcc/good, mcc/medium, mcc/cheap and "
            "mcc/vision to the top of every coding agent's model picker. "
            "They are names for MCC's own routes, the way Claude Code asks "
            "for claude-sonnet-5 "
            "and gets whatever MODEL_SONNET points at, so a session started on "
            "one follows the route when you move it instead of pinning a "
            "provider's model id inside the agent's config. Each agent can be "
            "given its own chain per tier on the Coding agents page; until it "
            "is, every tier follows the global route above. Turn this off to "
            "keep the pickers to concrete refs only -- an agent that already "
            "names a tier keeps working either way."
        ),
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
            "Since 6.46.0 invalid_request means only what the provider itself "
            "called malformed -- its message says the request is malformed -- "
            "and that body fails identically everywhere, so retrying it costs a "
            "round trip per model to reach the same error. Every other 400 is "
            "model_rejected: a model that does not exist on that endpoint, a "
            "parameter that model pins, a field longer than that host allows. "
            "Those are about one model, so they are not in the default and the "
            "chain gets its turn; listing model_rejected here means one host's "
            "refusal ends the whole route. Leave empty to fall back on every "
            "failure. Known kinds: invalid_request, model_rejected, "
            "context_length, authentication, permission, quota, rate_limit, "
            "overloaded, timeout, upstream, unavailable. Listing quota here "
            "means an account out of credits ends the route instead of trying "
            "the next key and the next model, which is almost never what you "
            "want."
        ),
    ),
    # ---- Keeping the catalogue current ---------------------------------
    ConfigFieldSpec(
        "MODEL_DISCOVERY_REFRESH_SECONDS",
        "Refresh model catalogues every (seconds)",
        "models",
        "number",
        settings_attr="model_discovery_refresh_seconds",
        default="3600",
        restart_required=True,
        description=(
            "How often every usable provider's model list is re-read in the "
            "background, so a model a gateway added today appears without a "
            "restart. 0 turns it off; anything between 1 and 300 is raised to "
            "300, because a sweep is one upstream request per provider. A "
            "provider whose sweep fails with 401 or 403 is backed off "
            "exponentially rather than asked again every tick. Unrelated to "
            "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY, which is a Claude "
            "Code variable MCC writes for the agent and never reads itself."
        ),
    ),
    ConfigFieldSpec(
        "MODEL_PROBE_NEW_MODELS",
        "Probe models the refresh discovers",
        "models",
        "boolean",
        settings_attr="model_probe_new_models",
        default="false",
        restart_required=True,
        advanced=True,
        description=(
            "Off by default and deliberately so. A sweep that found 40 new "
            "models overnight would otherwise send 40-120 unattended upstream "
            "requests against a credential nobody is watching, and some hosts "
            "bill or answer 403 before they validate a body. Leave this off "
            "and press Probe capabilities on the provider card instead, which "
            "states the request count before it runs."
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
        "REASONING_MYTHOS",
        "Mythos Reasoning",
        "reasoning",
        "select",
        settings_attr="reasoning_mythos",
        default="inherit",
        options=_reasoning_options(ROUTE_REASONING_PREFERENCES),
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
        default="",
        secret=True,
        restart_required=True,
        description="Bearer token protecting Claude/API access. It is not admin-page login.",
    ),
    ConfigFieldSpec(
        "HOST",
        "Server Host",
        "runtime",
        settings_attr="host",
        default="127.0.0.1",
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
        "MCC_OPEN_BROWSER",
        "Open Admin on Startup",
        "runtime",
        "boolean",
        settings_attr="open_admin_browser",
        default="true",
        description="Open the Admin UI after the next mcc-server launch becomes healthy.",
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
        "ENABLE_PROBE_AUTO_RESPONSE",
        "Probe Auto Response",
        "optimizer",
        "boolean",
        settings_attr="enable_probe_auto_response",
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
        default="true",
    ),
    ConfigFieldSpec(
        "LOG_LEVEL",
        "Log level",
        "diagnostics",
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
    ConfigFieldSpec(
        "SERVER_LOG_RETAIN_FILES",
        "Rotated server logs to keep",
        "diagnostics",
        "number",
        settings_attr="server_log_retain_files",
        default="10",
        restart_required=True,
        description=(
            "How many rotated server.*.log files to keep. The current log file "
            "is never counted and never deleted; only the oldest rotated files "
            "beyond this cap are removed, both when a new rotation happens and "
            "at startup. Set to 0 to keep every rotated file. An earlier install "
            "left hundreds of 50 MB rotated files, so the startup sweep is what "
            "actually reclaims the space."
        ),
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
    # ---- Budgets: how long one answer may be -----------------------------
    ConfigFieldSpec(
        "MAX_OUTPUT_TOKENS_UNKNOWN_DEFAULT",
        "Output tokens when unknown",
        "budgets",
        "number",
        settings_attr="max_output_tokens_unknown_default",
        default="32768",
        description=(
            "Output-token budget used only when no source publishes a limit "
            "for the routed model. Whenever one does -- the provider's own "
            "/models payload or the models.dev catalogue -- that number is "
            "used instead, so a capable model is never held to this one. It "
            "also never reduces a max_tokens the client asked for. Set 0 to "
            "send no max_tokens at all in that case and let the provider size "
            "its own answer."
        ),
    ),
    ConfigFieldSpec(
        "MAX_OUTPUT_TOKENS_FLOOR",
        "Minimum output tokens",
        "budgets",
        "number",
        settings_attr="max_output_tokens_floor",
        default="8192",
        description=(
            "Smallest allowance any request is sent with. A client that "
            "hardcodes a tiny max_tokens gets a truncated answer out of a "
            "model that could have finished it, and nothing else in this card "
            "can raise a number -- the other five only lower. This one raises, "
            "and it is bounded by the routed model's own published limit, so "
            "it can never ask a 16,384-token model for more than 16,384. It "
            "stands down on an explicit max_tokens of 0, and the ceiling below "
            'still wins over it. Not the same setting as "Smallest bounded '
            'budget", which decides when the prompt reserve gives up. Set 0 '
            "to raise nothing, which is the behaviour before 6.47.0."
        ),
    ),
    ConfigFieldSpec(
        "MAX_OUTPUT_TOKENS_CEILING",
        "Output token ceiling",
        "budgets",
        "number",
        settings_attr="max_output_tokens_ceiling",
        default="131072",
        description=(
            "Absolute head on output tokens for every request, reasoning or "
            "not. Ships at 131,072 because a thinking request asks for the "
            "routed model's full published limit, and an unbounded ask can "
            "reserve a whole TPM bucket on hosts that pre-reserve max_tokens. "
            "It never raises a model above its own limit. Set 0 to lift it "
            "entirely."
        ),
    ),
    ConfigFieldSpec(
        "MAX_OUTPUT_TOKENS_CONTEXT_MARGIN",
        "Prompt reserve",
        "budgets",
        "number",
        settings_attr="max_output_tokens_context_margin",
        default="1024",
        description=(
            "Tokens held back from the context window when a model's output "
            "limit is large enough to swallow its own context. Absorbs the "
            "difference between FCC's token count and the upstream "
            "tokenizer's. 0 reserves nothing."
        ),
    ),
    ConfigFieldSpec(
        "MAX_OUTPUT_TOKENS_CONTEXT_FLOOR",
        "Smallest bounded budget",
        "budgets",
        "number",
        settings_attr="max_output_tokens_context_floor",
        default="4096",
        description=(
            "Smallest output budget the prompt reserve above is allowed to "
            "produce. When a model's remaining context leaves less than this, "
            "the request is sent unchanged so the provider reports the real "
            "context error, instead of succeeding with a budget too small to "
            "answer with. 0 sends any positive headroom, however small."
        ),
    ),
    ConfigFieldSpec(
        "REASONING_ANSWER_FLOOR_MAX",
        "Answer reserve when thinking",
        "budgets",
        "number",
        settings_attr="reasoning_answer_floor_max",
        default="16384",
        description=(
            "Most tokens ever held back from a request's output allowance for "
            "the visible answer while extended thinking is on. Thinking and "
            "the answer share one max_tokens, so without a reserve a large "
            "thinking budget can consume the whole allowance. Applied as the "
            "smaller of this and half the model's output allowance, so a "
            "16,384-token model still gets a working budget."
        ),
    ),
    ConfigFieldSpec(
        "ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS",
        "Last-resort output default",
        "budgets",
        "number",
        settings_attr="anthropic_default_max_output_tokens",
        default="81920",
        description=(
            "The max_tokens written onto a request that reached a provider "
            "with none of its own and no published limit to take one from. It "
            "knows nothing about the model, so it is only ever a fallback and "
            "never a cap: a routed request has already been bound to the "
            "model's real limit long before this applies. Set 0 to send no "
            "max_tokens at all on those paths."
        ),
    ),
    ConfigFieldSpec(
        "REASONING_EFFORT_BUDGET_RATIOS",
        "Thinking share per effort (comma-separated)",
        "budgets",
        "text",
        settings_attr="reasoning_effort_budget_ratios",
        default="0.10,0.20,0.50,0.80,0.95,0.95",
        advanced=True,
        description=(
            "Share of one request's output allowance each reasoning effort may "
            "spend on thinking, in order: minimal, low, medium, high, xhigh, "
            "max. Six values, each above 0 and below 1, and never decreasing -- "
            "the inverse lookup that turns a token budget back into an effort "
            "picks the strongest one that fits, so an out-of-order ladder can "
            "return an effort whose budget does not. Two surprises worth "
            "knowing: a small ratio does not disable thinking, because "
            "Anthropic's 1,024-token minimum is applied afterwards and wins on "
            "any allowance above 1,025; and the 0.95 at the top is not what "
            "keeps the budget under max_tokens -- a separate clamp does that -- "
            "so raising it only eats into the answer reserve above."
        ),
    ),
    # ---- Deadlines: when to stop waiting ---------------------------------
    ConfigFieldSpec(
        "FALLBACK_FIRST_TOKEN_TIMEOUT",
        "First-token deadline",
        "deadlines",
        "number",
        settings_attr="fallback_first_token_timeout",
        default="0",
        description=(
            "Seconds a model may stay silent before the next model on the "
            "chain takes over. Nothing has reached the client yet, so the "
            "handover is invisible. Ships 0, which waits indefinitely: with "
            "no value here a silent model holds the request until the "
            "transport read timeout ends it, and the chain is never reached. "
            "This is the one setting that turns a hang into a failover."
        ),
    ),
    ConfigFieldSpec(
        "FALLBACK_ATTEMPT_SHARE_FLOOR",
        "Silent-attempt floor",
        "deadlines",
        "number",
        settings_attr="fallback_attempt_share_floor",
        default="3600",
        description=(
            "Smallest slice of the total budget one attempt may be cut to. "
            "The budget is divided between the models still to try -- this is "
            "a chain-side allowance, not a per-retry one -- and on a long "
            "chain that share can fall below the first-token deadline and "
            "quietly replace it: 600 over eight models is 75 seconds, "
            "whatever the deadline said. This floor keeps the first-token "
            "deadline above the number that actually fires. The trade: "
            "several silent models in a row can spend the floor each until "
            "the total budget runs out, leaving the models after them less. "
            "Ships 0, which divides the budget equally with no floor, and is "
            "moot while the total budget is 0 -- there is no budget to divide."
        ),
    ),
    ConfigFieldSpec(
        "FALLBACK_TOTAL_TIMEOUT",
        "Total request budget",
        "deadlines",
        "number",
        settings_attr="fallback_total_timeout",
        default="0",
        description=(
            "Seconds one request may run across every attempt, retry and "
            "recovery. Each attempt gets an equal share of what is left until "
            "it produces output, so a silent model cannot spend the whole "
            "budget -- but never less than the silent-attempt floor above. "
            "Once output has started no fallback can replace it, but it can "
            "still stop. Ships 0, which disables the budget entirely: a "
            "request may then run for as long as the upstream keeps the "
            "connection open."
        ),
    ),
    ConfigFieldSpec(
        "FALLBACK_STALL_TIMEOUT",
        "Stall deadline",
        "deadlines",
        "number",
        settings_attr="fallback_stall_timeout",
        default="0",
        description=(
            "Seconds a model that has already produced output may then say "
            "nothing before the request is given up on. Measured from the last "
            "chunk that moved the answer forward, so a long answer is never "
            "cut and a keepalive never counts as progress. Ships 0, which "
            "disables it and leaves only the total budget -- and with that at "
            "0 too, nothing here ends a stalled stream at all."
        ),
    ),
    ConfigFieldSpec(
        "FALLBACK_REASONING_ANSWER_TIMEOUT",
        "Thinking time before the chain moves on",
        "deadlines",
        "number",
        settings_attr="fallback_reasoning_answer_timeout",
        default="0",
        restart_required=True,
        description=(
            "Seconds a model may think before the route stops waiting for it "
            "to start an answer and tries the next model. Only applies while "
            "the setting below is on, because only then is the attempt still "
            "abandonable. Measured on real traffic: every request that ran out "
            "of budget while thinking used the full 600s, and 98% of slow "
            "reasoning requests that did answer had started by 300s, so a "
            "value between the two separates them almost exactly. Ships 0, "
            "which lets a thinking model run to the total request budget -- "
            "and to no limit at all while that is 0."
        ),
    ),
    ConfigFieldSpec(
        "FALLBACK_ON_REASONING_ONLY",
        "Fall back when a model only thinks",
        "deadlines",
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
            "answer does. Turn this off to watch a model think in real time. "
            "Known gap: this covers a deadline reached while a stream is still "
            "open, so a stream that thinks, then ends on time with an empty "
            "visible answer, is never rescued -- raise the output ceiling or "
            "lower the reasoning tier if you see it."
        ),
    ),
    ConfigFieldSpec(
        "FALLBACK_END_CLEANLY_AFTER_COMMIT",
        "End a dead stream as a message, not an error",
        "deadlines",
        "boolean",
        settings_attr="fallback_end_cleanly_after_commit",
        default="true",
        restart_required=True,
        description=(
            "Once a model has started answering, the chain can no longer step "
            "in -- the reader has already seen its words. Until this setting "
            "existed, any failure after that point (the stall limit above, the "
            "total budget, a mid-stream 5xx, a dropped connection) reached the "
            "client as an API error printed underneath a half-written answer, "
            "and the turn died. With this on the message is instead *ended*: "
            "the open block is closed and the client is told the answer was "
            "cut short, so the session continues with a short but complete "
            "reply. The answer really is incomplete, and the request detail "
            "says how far it got and what actually went wrong. One case still "
            "errors and cannot be helped: a stream that stopped halfway "
            "through a tool call's arguments, which cannot be completed "
            "honestly. Turn this off to go back to the error."
        ),
    ),
    ConfigFieldSpec(
        "FALLBACK_RESUME_AFTER_COMMIT",
        "Continue a dead answer on the next model",
        "deadlines",
        "boolean",
        settings_attr="fallback_resume_after_commit",
        default="true",
        restart_required=True,
        description=(
            "When a model dies part-way through an answer, the setting above "
            "ends the message early and the reader keeps whatever was written. "
            "With this on, the route goes one step further: the next model is "
            "given the text already on screen and asked to carry on from it, "
            "and its output is spliced into the same message -- so the turn "
            "survives instead of stopping short. It is the same chain, the "
            "same bench and the same budget as an ordinary fallback; nothing "
            "new is retried. Continuation is not reliable on every model: many "
            "answer nothing, and one that starts the answer over is rejected "
            "rather than shown twice. Every one of those outcomes ends as the "
            "short message you would have got anyway, never as an error. The "
            "one state never continued is a half-written tool call. Turn this "
            "off to stop at the short message."
        ),
    ),
    ConfigFieldSpec(
        "STREAM_COMMIT_HOLDBACK_SECONDS",
        "Commit holdback",
        "deadlines",
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
        "STREAM_COMMIT_HOLDBACK_CHARS",
        "Commit holdback characters",
        "deadlines",
        "number",
        settings_attr="stream_commit_holdback_chars",
        default="0",
        restart_required=True,
        description=(
            "Visible characters that must also arrive before output is "
            "released, on top of the seconds above -- both conditions have to "
            "be met, or the stream has to end. It buys the trade the seconds "
            "box cannot: a model that writes one word and dies inside the "
            "window has shown the reader nothing, so the route can start over "
            "on the next model invisibly and the reader sees only the answer "
            "that worked. The cost is paid on every request, in time-to-first-"
            "visible-word, whether or not anything goes wrong. 0 asks only the "
            "clock. The buffer's own 65,536-byte ceiling still releases output "
            "whatever is set here."
        ),
    ),
    ConfigFieldSpec(
        "HTTP_READ_TIMEOUT",
        "HTTP Read Timeout",
        "deadlines",
        "number",
        settings_attr="http_read_timeout",
        default="300",
        description=(
            "Transport ceiling on waiting for bytes from a provider. It sits under "
            "every deadline above: set below the first-token deadline and a slow "
            "model produces a transport error instead of a clean handover to the "
            "next model."
        ),
    ),
    ConfigFieldSpec(
        "HTTP_WRITE_TIMEOUT",
        "HTTP Write Timeout",
        "deadlines",
        "number",
        settings_attr="http_write_timeout",
        default="60",
        description=(
            "Transport ceiling on sending one request body to a provider. Large "
            "image or document payloads are what run into it."
        ),
    ),
    ConfigFieldSpec(
        "HTTP_CONNECT_TIMEOUT",
        "HTTP Connect Timeout",
        "deadlines",
        "number",
        settings_attr="http_connect_timeout",
        default="60",
        description=(
            "Transport ceiling on opening the connection. A provider that is down "
            "usually refuses or times out here, before any deadline above applies."
        ),
    ),
    ConfigFieldSpec(
        "SERVER_GRACEFUL_SHUTDOWN_SECONDS",
        "Graceful shutdown budget",
        "deadlines",
        "number",
        settings_attr="server_graceful_shutdown_seconds",
        default="20",
        restart_required=True,
        description=(
            "Seconds a stop may take, end to end -- a Ctrl+C, the reload after "
            "you apply a setting, or the restart that finishes an update. At the "
            "bound the server closes whatever is still open: an in-flight stream "
            "simply ends and your coding agent retries onto the restarted "
            "server. From the instant a stop begins, new requests are refused "
            "with 503 so they cannot extend it, and the process exits a few "
            "seconds past the bound whatever is still running. Raise it if long "
            "requests matter more than a fast restart; 1s is the floor and 600s "
            "the ceiling. If you set 300 here on an earlier version, consider "
            "20: before 6.41.0 this bounded only one wait inside the stop and "
            "the rest was unbounded, so a large value cost nothing -- now it is "
            "the time you wait for a restart. It is also the time you wait for "
            "every UPDATE, twice over: the installer does not start until the "
            "old server has finished draining, and the reconnect budget the "
            "desktop window counts down is this number plus the install "
            "allowance. Measured on a real machine, 300 here added five silent "
            "minutes to a fourteen-minute update and turned the window's "
            '"reconnecting for up to 17 minutes" into 22.'
        ),
    ),
    ConfigFieldSpec(
        "SERVER_PORT_TAKEOVER",
        "Port takeover on start",
        "deadlines",
        "select",
        settings_attr="server_port_takeover",
        default="always",
        restart_required=True,
        options=(
            ConfigOptionSpec("always", "Stop whatever holds the port"),
            ConfigOptionSpec("mcc-only", "Stop only My Claude Code's own processes"),
            ConfigOptionSpec("never", "Stop nothing; refuse to start"),
        ),
        description=(
            "What happens when this server starts and something is already "
            "listening on its port. Always -- the default -- stops the holder "
            "and takes the port, which is what makes a restart or an update "
            "come back on its own: the commonest holder by far is My Claude "
            "Code's own previous process, one that overran its drain or that "
            "the desktop app started twice. A holder that is not My Claude "
            "Code is named in one WARNING line in the server log before it is "
            "stopped. Mcc-only stops only processes this install can identify "
            "as its own and leaves anything else alone -- pick it if this "
            "port might legitimately belong to another program. Never is the "
            "behaviour of 6.58.4 and earlier: the server names the holder and "
            "refuses to start, and you sort it out by hand. The holder is "
            "identified by its process, never by what it answers on the port, "
            "because a server that is still starting answers nothing at all."
        ),
    ),
    ConfigFieldSpec(
        "SERVER_STALE_SERVER_ACTION",
        "Other My Claude Code servers",
        "deadlines",
        "select",
        settings_attr="server_stale_server_action",
        default="report",
        restart_required=True,
        options=(
            ConfigOptionSpec("report", "Name them in the log and leave them alone"),
            ConfigOptionSpec("stop", "Also stop the ones proven to be finished"),
        ),
        description=(
            "What this server does about OTHER My Claude Code servers it finds "
            "running when it starts. Report -- the default -- writes one line "
            "per server into the server log naming its process id, its session, "
            "the port it recorded, when it started, when it last checked in and "
            "which files it is holding open, and stops nothing whatsoever. That "
            "is the safe answer and almost always the right one: a server that "
            "is not listening on a port right now may be starting, may be "
            "draining, or may be streaming an answer to a request it accepted "
            "before its socket closed. Stop also stops the ones this install "
            "can PROVE are finished -- a server whose heartbeat has been silent "
            "past the budget below AND whose port is now served by a different "
            "My Claude Code, or a launcher whose server process is gone "
            "entirely. It never stops a server merely because no listening "
            "socket was found for it. Turn this on if stale servers keep "
            "holding files open and making your updates retry."
        ),
    ),
    ConfigFieldSpec(
        "SERVER_STALE_SESSION_SECONDS",
        "Silence before a server counts as stale",
        "deadlines",
        "number",
        settings_attr="server_stale_session_seconds",
        default="900",
        restart_required=True,
        description=(
            "How long another My Claude Code server's heartbeat must have been "
            "silent before this install is willing to describe it as stale. A "
            "running server writes into its session row every 30 seconds, so "
            "the default of 900 is thirty missed beats. Silence alone never "
            "stops anything -- it only makes the word available; something else "
            "must also be serving that server's port before the setting above "
            "will act. Raise it if you keep servers parked for long periods."
        ),
    ),
    ConfigFieldSpec(
        "CATALOGUE_FETCH_TIMEOUT_SECONDS",
        "Coding agent catalogue build budget",
        "deadlines",
        "number",
        settings_attr="catalogue_fetch_timeout_seconds",
        default="20",
        description=(
            "Seconds an mcc-<agent> launcher waits for this server to build a "
            "coding agent's model list the first time, when no document for "
            "that agent exists under ~/.mcc yet. Every later launch reads the "
            "file the server keeps up to date and spends none of this. Read by "
            "the launcher process, so a change applies to the next mcc-<agent> "
            "you run, not to the server. 1s is the floor."
        ),
    ),
    # ---- Benching: when to stop trying a model ---------------------------
    ConfigFieldSpec(
        "FALLBACK_BENCH_ENABLED",
        "Bench failures",
        "benching",
        "select",
        settings_attr="fallback_bench_enabled",
        default="false",
        options=("false", "true"),
        description=(
            "Off (default): every model in the chain is tried every time. On: "
            "a model that keeps failing is skipped for FALLBACK_EJECT_SECONDS "
            "and a provider Retry-After is honoured, and every Eject setting "
            "below becomes live. Only model-shaped failures count towards the "
            "bench -- upstream 5xx, overloaded, and 401/403 from the provider. "
            "Timeouts, 429s, exhausted credits, context-length and "
            "malformed-request failures are "
            "the request's problem rather than the model's and never bench "
            "one. The default changed to off in 6.14.0; an install that "
            "already has this written in its .env keeps whatever it says."
        ),
    ),
    ConfigFieldSpec(
        "FALLBACK_BEHAVIOR",
        "Eject mode",
        "benching",
        "select",
        settings_attr="fallback_behavior",
        default="rate_based",
        options=("rate_based", "legacy"),
        description=(
            "How a failing model is benched. rate_based (default) skips a model when its failure rate over the last FALLBACK_EJECT_WINDOW requests crosses FALLBACK_EJECT_FAILURE_RATE, for FALLBACK_EJECT_SECONDS. legacy preserves the historical consecutive-count behavior (FALLBACK_EJECT_AFTER_FAILURES + FALLBACK_EJECT_SECONDS)."
        ),
    ),
    ConfigFieldSpec(
        "FALLBACK_EJECT_WINDOW",
        "Rate window (requests)",
        "benching",
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
        "benching",
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
        "benching",
        "number",
        settings_attr="fallback_eject_min_samples",
        default="8",
        description=(
            "Minimum requests observed before the failure rate is evaluated. Prevents a single failure on a low-traffic model from tripping it. Ignored in legacy mode."
        ),
        minimum=1,
    ),
    ConfigFieldSpec(
        "FALLBACK_EJECT_AFTER_FAILURES",
        "Bench a model after",
        "benching",
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
        "benching",
        "number",
        settings_attr="fallback_eject_seconds",
        default="10",
        description="Seconds a benched model stays out of routing.",
    ),
    ConfigFieldSpec(
        "FALLBACK_RETRY_FIRST",
        "Retry primary once",
        "benching",
        "select",
        settings_attr="fallback_retry_first",
        default="skip",
        options=("skip", "retry_once"),
        description=(
            "What happens when the primary model fails. skip (default) moves straight to the next fallback. retry_once gives the primary one more chance for transient errors (timeout, 5xx, 429) before falling through. Auth and invalid-request errors are never retried regardless."
        ),
    ),
    ConfigFieldSpec(
        "FALLBACK_COOLDOWN_STEP_OVER_FLOOR",
        "Step over a cooled-down model when its wait is at least",
        "benching",
        "number",
        settings_attr="fallback_cooldown_step_over_floor",
        default="5",
        restart_required=True,
        advanced=True,
        description=(
            "Seconds of remaining rate-limit cooldown that make it worth "
            "trying the next model instead of waiting. Shorter waits are "
            "waited out, because stepping over costs the chain a slot."
        ),
    ),
    # ---- Provider retries: how hard to try before the chain --------------
    ConfigFieldSpec(
        "PROVIDER_RETRY_ATTEMPTS",
        "Retries before the chain",
        "provider_retries",
        "number",
        settings_attr="provider_retry_attempts",
        default="2",
        restart_required=True,
        description=(
            "How many times one model is retried on the same key after an "
            "upstream 5xx or a dropped connection, before the next model is "
            "tried. A 429 uses none of these: it routes around the model "
            'instead, unless "Route around a rate-limited model" is off. '
            "Each retry waits longer than the last, so the two shipped "
            "attempts spend about 2s before a healthy fallback is used."
        ),
    ),
    ConfigFieldSpec(
        "STREAM_EARLY_RETRY_ATTEMPTS",
        "Retries inside one model",
        "provider_retries",
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
        "provider_retries",
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
        "PROVIDER_RETRY_BACKOFF_BASE_SECONDS",
        "Retry backoff: first wait",
        "provider_retries",
        "number",
        settings_attr="provider_retry_backoff_base_seconds",
        default="2",
        restart_required=True,
        advanced=True,
        description=(
            "How long a provider waits before its first retry of a 429 or "
            "5xx. Each further retry doubles it."
        ),
    ),
    ConfigFieldSpec(
        "PROVIDER_RETRY_BACKOFF_MAX_SECONDS",
        "Retry backoff: longest wait",
        "provider_retries",
        "number",
        settings_attr="provider_retry_backoff_max_seconds",
        default="5",
        restart_required=True,
        advanced=True,
        description=(
            "The longest single wait between one model's own retries -- the "
            "ceiling the doubling backoff stops growing past. That ladder now "
            "runs only for an upstream 5xx or a dropped connection, and the "
            "fallback chain is not consulted until it is spent, so every "
            "second here is added to how long a request waits before another "
            "model is tried. A 429 walks no ladder at all."
        ),
    ),
    ConfigFieldSpec(
        "PROVIDER_RETRY_BACKOFF_JITTER_SECONDS",
        "Retry backoff: jitter",
        "provider_retries",
        "number",
        settings_attr="provider_retry_backoff_jitter_seconds",
        default="0.5",
        restart_required=True,
        advanced=True,
        description=(
            "Random spread added to each retry wait so several clients "
            "hitting the same limit do not retry in lockstep."
        ),
    ),
    ConfigFieldSpec(
        "PROVIDER_RATE_LIMIT",
        "Provider Rate Limit",
        "provider_retries",
        "number",
        settings_attr="provider_rate_limit",
        default="300",
        description=(
            "Requests one provider may start inside the window below. This is a "
            "client-side pace, not the provider's own limit. The shipped 300 "
            "per 2s is 150 a second per provider -- far above any interactive "
            "volume, so it throttles nothing you can type while still capping a "
            "runaway loop. 0 paces nothing at all. A provider that is really "
            "over quota answers 429 with a Retry-After, which is obeyed either "
            "way. Lower it to hold a metered key back on purpose -- it is "
            "counted per provider, and every routing attempt spends one."
        ),
    ),
    ConfigFieldSpec(
        "PROVIDER_RATE_WINDOW",
        "Provider Rate Window",
        "provider_retries",
        "number",
        settings_attr="provider_rate_window",
        default="2",
        description="Length of the window the request count above is measured over.",
    ),
    ConfigFieldSpec(
        "PROVIDER_MAX_CONCURRENCY",
        "Provider Max Concurrency",
        "provider_retries",
        "number",
        settings_attr="provider_max_concurrency",
        default="300",
        description=(
            "Streams one provider may have open at once. A further request waits "
            "for a slot rather than being refused."
        ),
    ),
    # ---- Credential health: what one key's failures cost it --------------
    ConfigFieldSpec(
        "RATE_LIMIT_COOLDOWN_SECONDS",
        "Rate-limit cooldown",
        "credential_health",
        "number",
        settings_attr="rate_limit_cooldown_seconds",
        default="60",
        restart_required=True,
        advanced=True,
        description=(
            "How long a rate-limited provider is paused when it sends no "
            "Retry-After header of its own to obey. 0 does not pause at all -- "
            "the key is offered again immediately, and the provider goes on "
            "answering 429 until it stops."
        ),
    ),
    ConfigFieldSpec(
        "RATE_LIMIT_COOLDOWN_MAX_SECONDS",
        "Longest cooldown a provider may ask for",
        "credential_health",
        "number",
        settings_attr="rate_limit_cooldown_max_seconds",
        default="3600",
        restart_required=True,
        advanced=True,
        minimum=0,
        description=(
            "The ceiling on a wait a provider asks for in a Retry-After or "
            "x-ratelimit-reset header. An hour was hard-coded until 7.22.0 and "
            "is still the default, so leaving this alone changes nothing. "
            "Lower it to stop one buggy or hostile header taking a key out of "
            "the pool for an hour; 0 removes the ceiling and obeys whatever "
            "the provider asked for. It does not apply to a wait a provider "
            "published in the body of its answer -- that is a statement about "
            "the account's allowance for the day, is bounded at one day, and "
            "is ignored only by the cooldown mode below."
        ),
    ),
    ConfigFieldSpec(
        "RATE_LIMIT_COOLDOWN_MODE",
        "What a 429 costs the key",
        "credential_health",
        "select",
        settings_attr="rate_limit_cooldown_mode",
        default="provider",
        restart_required=True,
        advanced=True,
        options=(
            ConfigOptionSpec("provider", "Wait as long as the provider asked"),
            ConfigOptionSpec("fixed", "Always the cooldown above"),
            ConfigOptionSpec("off", "Never pause -- keep trying"),
        ),
        description=(
            "Three settings, one question: what a 429 costs the key that met "
            "it. Wait as long as the provider asked is what every release "
            "before 7.22.0 did -- obey the Retry-After under the ceiling "
            "above, and use the cooldown above when the provider sent none. "
            "Always the cooldown above ignores what the provider published and "
            "uses your number, for a host whose headers you do not trust. "
            "Never pause benches nothing at all: no key bench, no (key, model) "
            "bench, no provider-wide pause. MCC still rotates to the next key "
            "and still moves down the fallback chain -- rotating and benching "
            "are separate questions -- so the consequence is the plain one: "
            "the provider goes on answering 429 and MCC goes on spending "
            "attempts on it. Setting the cooldown above to 0 is the milder "
            "version: nothing is benched when the provider sent no header, but "
            "a wait it did publish is still honoured."
        ),
    ),
    ConfigFieldSpec(
        "CREDENTIAL_LOCKOUT_TIERS",
        "Auth lockout ladder (seconds, comma-separated)",
        "credential_health",
        "text",
        settings_attr="credential_lockout_tiers",
        default="300,3600,86400",
        restart_required=True,
        description=(
            "How long a key is benched after the provider rejects it with "
            "401/403, escalating one step per consecutive rejection and "
            "staying at the last entry. This is the only ladder left: a 429 "
            "waits exactly as long as the provider asked, and nothing else "
            "changes a key's health."
        ),
    ),
    ConfigFieldSpec(
        "CREDENTIAL_MODEL_BENCH_ESCALATION",
        "Models before a whole key is benched",
        "credential_health",
        "number",
        settings_attr="credential_model_bench_escalation",
        default="2",
        restart_required=True,
        minimum=0,
        description=(
            "A 429 benches only the model it happened on, on the key it "
            "happened on. Once this many different models are rate-limited "
            "on the same key at once, the key itself is benched instead. "
            "1 benches the whole key on every 429; 0 never does."
        ),
    ),
    ConfigFieldSpec(
        "PROXY_MAX_SWITCHES_PER_REQUEST",
        "Proxy switches per request",
        "credential_health",
        "number",
        settings_attr="proxy_max_switches_per_request",
        default="2",
        restart_required=True,
        advanced=True,
        minimum=1,
        maximum=5,
        description=(
            "The most times one request may move to the next address in a "
            "provider's proxy chain before the failure is handed to the model "
            "fallback chain as it is today. 2 tries at most three addresses. "
            "Every switch spends wall-clock inside a single attempt and the "
            "deadlines do not move to make room. Each chain carries its own "
            "number on the Proxying page; the smaller of the two applies. "
            "Nothing at all happens on a provider with no chain."
        ),
    ),
    ConfigFieldSpec(
        "PROXY_CHAIN_MAX_ENTRIES",
        "Most entries in one proxy chain",
        "credential_health",
        "number",
        settings_attr="proxy_chain_max_entries",
        default="0",
        restart_required=True,
        advanced=True,
        minimum=0,
        description=(
            "How many addresses one provider's chain may hold. 0 -- what "
            "ships -- means no limit: paste as many as you like. Set a number "
            "if you want MCC to refuse a longer chain, and it refuses it with "
            "a message rather than silently dropping the extra entries. "
            "Length costs memory for the list, not connections: a chain's "
            "legs are built on first use and closed again when idle."
        ),
    ),
    ConfigFieldSpec(
        "PROXY_MAX_OPEN_LEGS",
        "Open connections per proxy chain",
        "credential_health",
        "number",
        settings_attr="proxy_max_open_legs",
        default="32",
        restart_required=True,
        advanced=True,
        minimum=0,
        maximum=4096,
        description=(
            "How many addresses in one chain may hold an open HTTP client at "
            "the same time. A leg is built the first time a request goes out "
            "through it and closed again when it has been idle longest, so a "
            "three-hundred-entry chain does not mean three hundred connection "
            "pools. 0 keeps every leg that was ever used open."
        ),
    ),
    ConfigFieldSpec(
        "PROXY_CONNECT_TIMEOUT_SECONDS",
        "Proxy connect timeout (seconds)",
        "credential_health",
        "number",
        settings_attr="proxy_connect_timeout_seconds",
        default="10",
        restart_required=True,
        advanced=True,
        minimum=1,
        maximum=120,
        description=(
            "How long a request waits for the handshake with a proxy address "
            "before giving up on it and trying the next one. Only the connect "
            "step, and only when the request is going out through a proxy: "
            "the read and write timeouts are the provider's own, and a "
            "provider with no chain is not affected at all. A dead address "
            "costs this many seconds, once -- MCC no longer dials the same "
            "address a second time after a connect failure, because another "
            "address is what fixes one."
        ),
    ),
    ConfigFieldSpec(
        "PROXY_MAX_LIVE_FAILURES",
        "Live proxy failures before going direct",
        "credential_health",
        "number",
        settings_attr="proxy_max_live_failures",
        default="5",
        restart_required=True,
        advanced=True,
        minimum=0,
        maximum=1000,
        description=(
            "How many addresses may fail while actually carrying one request "
            "before MCC stops walking the chain and sends that request out on "
            "this machine's own address instead. Addresses already known to "
            "be unhealthy are skipped for free and do not count here. 0 "
            "removes the bound, which on a long chain of dead addresses is "
            "one connect timeout each inside a single attempt. Whether the "
            "direct fallback happens at all is a per-chain switch on the "
            "Proxying page."
        ),
    ),
    ConfigFieldSpec(
        "PROXY_HEALTH_REPROBE_ENABLED",
        "Re-test unhealthy proxies so they can come back",
        "credential_health",
        "boolean",
        settings_attr="proxy_health_reprobe_enabled",
        default="true",
        restart_required=True,
        advanced=True,
        description=(
            "An address that failed is held out of the rotation until a check "
            "passes -- a bench running out means 'due for a re-check', never "
            "'healthy again'. This loop is what runs that re-check, on the "
            "same 1 minute / 5 minute / 1 hour ladder the bench uses. It "
            "contacts only the provider hosts you already route to, only for "
            "addresses in an ENABLED chain, and only ones that have already "
            "failed on your own traffic. Turn it off and an address that "
            "fails stays out until you press Check now."
        ),
    ),
    ConfigFieldSpec(
        "PROXY_CHECK_ENABLED",
        "Check proxies in the background",
        "credential_health",
        "boolean",
        settings_attr="proxy_check_enabled",
        default="false",
        restart_required=True,
        advanced=True,
        description=(
            "Re-measure every address in a proxy chain on a timer: does it "
            "answer, does its tunnel keep certificate validation intact, and "
            "how long does it take. A proxy that breaks certificate "
            "validation is refused and stops being used; a dead one is "
            "benched and the chain routes around it without you doing "
            "anything. Off by default, and while it is off MCC makes no "
            "outbound request you did not ask for -- the Test button on the "
            "Proxying page always works and is one request per press."
        ),
    ),
    ConfigFieldSpec(
        "PROXY_CHECK_INTERVAL_MINUTES",
        "Minutes between proxy checks",
        "credential_health",
        "number",
        settings_attr="proxy_check_interval_minutes",
        default="30",
        restart_required=True,
        advanced=True,
        minimum=0,
        maximum=1440,
        description=(
            "How often the background checker sweeps the addresses in your "
            "chains. One HEAD request per address per sweep, to that "
            "provider's own host. 0 switches the loop off even when the "
            "check above is enabled."
        ),
    ),
    ConfigFieldSpec(
        "PROXY_CHECK_EXIT_IP_URL",
        "Exit-IP check URL",
        "credential_health",
        "text",
        settings_attr="proxy_check_exit_ip_url",
        default="",
        restart_required=False,
        advanced=True,
        affects_providers=False,
        description=(
            "Optional, and empty by default. A URL of your choosing that "
            "answers with the address it saw, fetched through each proxy so "
            "you can see that the source address really changed. MCC ships no "
            "default here: this is the one part of the check that contacts "
            "somebody you did not already choose to talk to, so it happens "
            "only if you name them. It never decides whether a proxy passes."
        ),
    ),
    ConfigFieldSpec(
        "PROXY_FEED_REFRESH_ENABLED",
        "Refresh proxy feeds in the background",
        "credential_health",
        "boolean",
        settings_attr="proxy_feed_refresh_enabled",
        default="false",
        restart_required=True,
        advanced=True,
        affects_providers=False,
        description=(
            "Re-read the public proxy lists you switched on, on a timer, so "
            "the candidate list on the Proxying page stays current. This is "
            "only half the switch: which lists may be read at all is chosen "
            "on that page and nothing is selected on a fresh install, so "
            "turning this on by itself still contacts nobody. A candidate is "
            "never used for anything until you move it into a chain by hand."
        ),
    ),
    ConfigFieldSpec(
        "PROXY_FEED_REFRESH_MINUTES",
        "Minutes between proxy feed refreshes",
        "credential_health",
        "number",
        settings_attr="proxy_feed_refresh_minutes",
        default="60",
        restart_required=True,
        advanced=True,
        affects_providers=False,
        minimum=0,
        maximum=10080,
        description=(
            "How often the enabled feeds are re-read. One request per feed "
            "per pass. Anything under 30 minutes is treated as 30: these are "
            "other people's servers and their lists do not change faster than "
            "that. 0 switches the loop off even when the setting above is on."
        ),
    ),
    ConfigFieldSpec(
        "PROXY_CANDIDATES_MAX",
        "Most addresses one fetch may offer",
        "credential_health",
        "number",
        settings_attr="proxy_candidates_max",
        default="0",
        restart_required=False,
        advanced=True,
        affects_providers=False,
        minimum=0,
        maximum=100000,
        description=(
            "How many of the addresses a fetch found it tests and keeps on "
            "offer, best-ranked first. 0 is no limit and is what ships: a "
            "chain has held any number of entries since 7.19.0, so trimming "
            "the offer to a round number would be MCC inventing a ceiling you "
            "did not ask for. Set one if you would rather a fetch finished "
            "sooner -- the addresses more feeds agree on are tested first."
        ),
    ),
    ConfigFieldSpec(
        "PROXY_FETCH_TEST_CONCURRENCY",
        "Addresses tested at once by a fetch",
        "credential_health",
        "number",
        settings_attr="proxy_fetch_test_concurrency",
        default="100",
        restart_required=False,
        advanced=True,
        affects_providers=False,
        minimum=4,
        maximum=500,
        description=(
            "A fetch tests every address it found before offering it, and a "
            "public list holds hundreds. Each test waits on somebody else's "
            "network rather than on this machine, so they overlap. Measured: "
            "seven lists offered 1,592 addresses and 32 at a time had tested "
            "848 of them after three minutes, because a dead address costs the "
            "whole connect timeout. A hundred is the default and 500 the "
            "ceiling. Raising this opens more sockets at once; the number here "
            "is exactly how many. The single Test and Add buttons are "
            "unaffected -- those are one address with you watching."
        ),
    ),
    ConfigFieldSpec(
        "PROXY_FETCH_CONCURRENCY_MODE",
        "How that number is read",
        "credential_health",
        "select",
        settings_attr="proxy_fetch_concurrency_mode",
        default="fixed",
        restart_required=False,
        advanced=True,
        affects_providers=False,
        options=(
            ConfigOptionSpec("fixed", "A count of addresses"),
            ConfigOptionSpec("percent", "A percentage of what the feeds offered"),
        ),
        description=(
            "A count of addresses is what MCC has always meant by the setting "
            "above. A percentage reads the same number against however many "
            "addresses your feeds actually offered this pass -- so 6 with "
            "1,592 on offer tests 96 at a time, and the same setting paces a "
            "list of two hundred and a list of five thousand. It is resolved "
            "after the lists are read, never below 4 and never above 500, and "
            "the Proxying page says what it worked out to. In percent mode a "
            "number outside 1-100 is a typo: it is reported on the page and "
            "read as a fixed count for that pass rather than refusing to run."
        ),
    ),
    ConfigFieldSpec(
        "PROXY_FETCH_CHECK_DEPTH",
        "How far a fetch tests each address",
        "credential_health",
        "select",
        settings_attr="proxy_fetch_check_depth",
        default="tls",
        restart_required=False,
        advanced=True,
        affects_providers=False,
        options=(
            ConfigOptionSpec("tls", "Tunnel and verify the certificate"),
            ConfigOptionSpec("request", "Tunnel and send an HTTPS request"),
        ),
        description=(
            "Tunnel and verify opens the tunnel, completes a full TLS "
            "handshake to the provider's own host through it with ordinary "
            "strict trust, and closes -- no HTTP request is sent. It catches "
            "an intercepting proxy exactly as the other one does, because that "
            "verdict arrives during the handshake. It ships as the default "
            "because a sweep of 1,592 addresses otherwise arrives at your "
            "provider as about a thousand requests from a thousand different "
            "source addresses, which is a lot to ask of somebody else's "
            "service for an answer the handshake already gave. Tunnel and send "
            'a request is what 7.22.1 did. Either way, Test, Add and "Add all '
            'working" always send the request: an address entering a chain is '
            "proven end to end."
        ),
    ),
    ConfigFieldSpec(
        "PROXY_FETCH_CONNECT_TIMEOUT_SECONDS",
        "Seconds a fetch waits for an address to answer",
        "credential_health",
        "number",
        settings_attr="proxy_fetch_connect_timeout_seconds",
        default="5",
        restart_required=False,
        advanced=True,
        affects_providers=False,
        minimum=1,
        maximum=60,
        description=(
            "The first step of a fetch's test is a plain TCP connection, and "
            "the commonest thing in a public list is an address that has "
            "stopped listening. This is how long that step waits before "
            "calling it dead. It applies only to the fetch sweep: an address "
            "that does answer gets the full check timeout for the HTTPS "
            "handshake that follows, and the Test button is unaffected."
        ),
    ),
    ConfigFieldSpec(
        "PROXY_CHECK_TIMEOUT_SECONDS",
        "Seconds one leg of an address check may take",
        "credential_health",
        "number",
        settings_attr="proxy_check_timeout_seconds",
        default="10",
        restart_required=False,
        advanced=True,
        affects_providers=False,
        minimum=1,
        maximum=120,
        description=(
            "Every check of an address opens a tunnel and completes a TLS "
            "handshake to your provider's own host through it; this is how "
            "long any one of those steps may take before the address is "
            "called dead. Ten seconds is what MCC has always used and nothing "
            "changes unless you change it. Raise it for a chain that has to "
            "reach the other side of the world -- a working but distant "
            "address can otherwise be written off. Lower it and a sweep "
            "finishes sooner, at the cost of failing addresses that were only "
            "slow. This is the check, not the request path: a real request "
            'through a proxy uses "Seconds to open a proxied connection".'
        ),
    ),
    ConfigFieldSpec(
        "PROXY_CHECK_MAX_CONCURRENCY",
        "Addresses checked at once outside a fetch",
        "credential_health",
        "number",
        settings_attr="proxy_check_max_concurrency",
        default="4",
        restart_required=False,
        advanced=True,
        affects_providers=False,
        minimum=1,
        maximum=128,
        description=(
            "How many addresses the background health re-prober tests at the "
            "same time. That loop is the only thing this paces: since 7.19.0 "
            "an address that failed stays out of the rotation until a check "
            "passes, and this is how many of those catch-up checks overlap. "
            "Four is what MCC has always used. Each one sends the full "
            "end-to-end request to your provider, so raising this sends that "
            "many at once from this machine -- which is why the ceiling is "
            "128 and not the 500 the fetch sweep allows. Raise it if a chain "
            "of many benched addresses takes too long to come back. The "
            'fetch sweep and "Add all working" are paced by "Addresses '
            'tested at once by a fetch" instead.'
        ),
    ),
    ConfigFieldSpec(
        "PROXY_FEED_TIMEOUT_SECONDS",
        "Seconds one feed has to answer",
        "credential_health",
        "number",
        settings_attr="proxy_feed_timeout_seconds",
        default="15",
        restart_required=False,
        advanced=True,
        affects_providers=False,
        minimum=1,
        maximum=300,
        description=(
            "Feeds are read one after another, so this is what bounds the "
            "Fetch button: a feed that has not answered in this long is "
            "skipped for that pass and the next one is read. Fifteen seconds "
            "is what MCC has always used. Raise it if you have a large list "
            "on a slow mirror and the Proxying page keeps reporting it as "
            "unreachable; a list that cannot answer at all is not one to "
            "build a chain on."
        ),
    ),
    ConfigFieldSpec(
        "PROXY_FEED_MAX",
        "Most feed URLs the store will hold",
        "credential_health",
        "number",
        settings_attr="proxy_feed_max",
        default="20",
        restart_required=False,
        advanced=True,
        affects_providers=False,
        minimum=1,
        maximum=1000,
        description=(
            "Saving the feed list on the Proxying page refuses more than this "
            "many URLs. Twenty is what MCC has always allowed. It bounds a "
            "list you type, not anything a chain may hold: every feed is one "
            "request to somebody else's server on every refresh pass, so a "
            "hundred feeds is a hundred requests each time the loop runs. "
            "Raise it if you have the lists."
        ),
    ),
    ConfigFieldSpec(
        "PROXY_CANDIDATE_BULK_MAX",
        "Most addresses one bulk Add may carry",
        "credential_health",
        "number",
        settings_attr="proxy_candidate_bulk_max",
        default="100",
        restart_required=False,
        advanced=True,
        affects_providers=False,
        minimum=1,
        maximum=100000,
        description=(
            "How many addresses one press of Add all working, or of a bulk "
            "Discard, may send. A hundred is what MCC has always allowed. It "
            "bounds one request and the sweep it starts, not how many entries "
            "a chain may hold -- that has had no limit since 7.19.0. Raise it "
            "to hand a whole fetch to a provider in one press; every address "
            "is still proven end to end before it enters a chain."
        ),
    ),
    ConfigFieldSpec(
        "PROXY_FETCH_PERSIST_INTERVAL_SECONDS",
        "Seconds between a running fetch's saves",
        "credential_health",
        "number",
        settings_attr="proxy_fetch_persist_interval_seconds",
        default="5",
        restart_required=False,
        advanced=True,
        affects_providers=False,
        minimum=0.5,
        maximum=300,
        description=(
            "A fetch writes what has passed so far while it is still running, "
            "so a server that stops mid-sweep keeps the addresses it had "
            "already proven. This is the longest a proven address may sit "
            "unwritten. Five seconds is what MCC has always used. Lower it to "
            "lose less to a crash; raise it to touch the store less often "
            "during a long sweep. Addresses are also written whenever enough "
            "of them have accumulated, and always once at the end."
        ),
    ),
    ConfigFieldSpec(
        "RATE_LIMIT_ROUTES_AROUND_MODEL",
        "Route around a rate-limited model",
        "credential_health",
        "boolean",
        settings_attr="rate_limit_routes_around_model",
        default="true",
        restart_required=True,
        description=(
            "On a 429, try another model on the same provider instead of "
            "retrying the same one and then spending the rest of the key "
            "pool on it. One measured request spent 51 of its 57 seconds "
            "asleep between retries of a model that was refusing in 0.2s. "
            "Off restores retry-then-rotate."
        ),
    ),
    # ---- Cost estimation -------------------------------------------------
    ConfigFieldSpec(
        "COST_ESTIMATION_ENABLED",
        "Estimate request cost",
        "cost",
        "boolean",
        settings_attr="cost_estimation_enabled",
        default="true",
        affects_providers=False,
        description=(
            "Works out what each request cost and stores it beside the "
            "request. Off, the cost column and the Analytics cost cards stay "
            "empty for new requests; rows already priced keep their figures."
        ),
    ),
    ConfigFieldSpec(
        "COST_ESTIMATION_MODE",
        "Which sources may price a request",
        "cost",
        "select",
        settings_attr="cost_estimation_mode",
        default="auto",
        affects_providers=False,
        options=(
            ConfigOptionSpec("auto", "The whole ladder (recommended)"),
            ConfigOptionSpec("reported_only", "Only what the host reports"),
            ConfigOptionSpec("computed_only", "Always compute, ignore the host"),
        ),
        description=(
            "Auto takes the host's own figure where it reports one and "
            "computes from a published price where it does not. Reported only "
            "leaves everything else unpriced. Computed only ignores the host's "
            "figure entirely, which is how you audit a provider's billing "
            "against a published price."
        ),
    ),
    ConfigFieldSpec(
        "COST_SOURCE_LITELLM_ENABLED",
        "Use LiteLLM's price map too",
        "cost",
        "boolean",
        settings_attr="cost_source_litellm_enabled",
        default="false",
        affects_providers=False,
        description=(
            "Adds LiteLLM's model_prices_and_context_window.json below "
            "models.dev: the widest published price table there is, and the "
            "only one that names a separate reasoning rate. Costs one cached "
            "2.3 MB file and one conditional fetch a day. Fetched live and "
            "integrity-checked on every refresh, never shipped in the package."
        ),
    ),
    # ---- Request log: what to keep ---------------------------------------
    ConfigFieldSpec(
        "REQUEST_LOG_ENABLED",
        "Record requests",
        "request_log",
        "boolean",
        settings_attr="request_log_enabled",
        default="true",
        restart_required=True,
        description="Turn the request log and the Analytics tab on or off.",
    ),
    ConfigFieldSpec(
        "REQUEST_LOG_MAX_ROWS",
        "Requests to keep",
        "request_log",
        "number",
        settings_attr="request_log_max_rows",
        default="700000",
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
        "request_log",
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
        "REQUEST_LOG_WIRE_BODY_MAX_CHARS",
        "Outbound body detail to store",
        "request_log",
        "number",
        settings_attr="request_log_wire_body_max_chars",
        default="8000",
        restart_required=True,
        description=(
            "How much of each outbound request body the log stores. Sampling "
            "and reasoning parameters are always stored whole; this bounds the "
            "message and tool structure stored beside them."
        ),
    ),
    ConfigFieldSpec(
        "REQUEST_LOG_LADDER_BODY_MAX_CHARS",
        "Upstream error detail per try",
        "request_log",
        "number",
        settings_attr="request_log_ladder_body_max_chars",
        default="800",
        restart_required=True,
        description=(
            "How much of each upstream error body the retry ladder keeps, for "
            "every try behind an attempt. The ladder records the status, the "
            "credential and the wait for each try regardless; this only bounds "
            "the body stored beside them."
        ),
    ),
    ConfigFieldSpec(
        "REQUEST_LOG_COMPRESS_BODIES",
        "Compress stored text",
        "request_log",
        "boolean",
        settings_attr="request_log_compress_bodies",
        default="true",
        restart_required=True,
        description=(
            "Compresses bodies against a dictionary trained on your own "
            "traffic and stores a repeated prompt once. Applies to new rows; "
            "run mcc-compact-log to convert existing history."
        ),
    ),
    ConfigFieldSpec(
        "REQUEST_LOG_CAPTURE_IMAGES",
        "Store image thumbnails",
        "request_log",
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
        "request_log",
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
        "request_log",
        "number",
        settings_attr="request_log_text_max_chars",
        default="10000000",
        restart_required=True,
        description=(
            "Text longer than this is truncated before it is stored, which "
            "also bounds what content search can ever find."
        ),
    ),
    ConfigFieldSpec(
        "REQUEST_LOG_COMPRESSION_LEVEL",
        "Compression level",
        "request_log",
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
        "request_log",
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
        default="20",
        description=(
            "How long mcc-desktop waits for a spawned mcc-server to become "
            "healthy before reporting a start failure. Applies the next time "
            "mcc-desktop starts, not to a tray already running."
        ),
    ),
    ConfigFieldSpec(
        "DESKTOP_SERVER_START_RETRIES",
        "Server start retries",
        "desktop",
        "number",
        settings_attr="desktop_server_start_retries",
        default="2",
        description=(
            "How many further attempts the desktop window makes after the "
            "first start timeout expires. The default gives a start three "
            "attempts of the timeout above -- 45 seconds -- before anything "
            "that looks like a failure is shown, and the window keeps "
            "checking even then. Applies the next time mcc-desktop starts, "
            "not to a window already open."
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
        "DESKTOP_RECONNECT_RESTATUS_SECONDS",
        "Reconnect re-check",
        "desktop",
        "number",
        settings_attr="desktop_reconnect_restatus_seconds",
        default="30",
        advanced=True,
        description=(
            "While the desktop window is waiting for the server to come back "
            "after a restart, how often it re-reads the whole status instead "
            "of only re-checking the health URL. This is what lets it notice "
            "that the port has gone free and start the server itself, once, "
            "rather than waiting out the whole reconnect budget. Applies the "
            "next time mcc-desktop starts, not to a window already open."
        ),
    ),
    ConfigFieldSpec(
        "DESKTOP_TICK_SECONDS",
        "Lifecycle tick",
        "desktop",
        "number",
        settings_attr="desktop_tick_seconds",
        default="10",
        advanced=True,
        description=(
            "How often the desktop app checks the server -- and, when the "
            "server is not running, how often it starts one. It never gives "
            "up and never parks on a button: the page says when it last "
            "checked and when it will try again. Applies the next time the "
            "desktop app starts, not to a window already open."
        ),
    ),
    ConfigFieldSpec(
        "DESKTOP_START_BACKOFF_SECONDS",
        "Start backoff",
        "desktop",
        "number",
        settings_attr="desktop_start_backoff_seconds",
        default="10",
        advanced=True,
        description=(
            "The shortest gap between two server starts by the desktop app. "
            "Equal to the tick by default. Raise it if a server that fails on "
            "start should be retried less often; the health check keeps its "
            "own cadence either way."
        ),
    ),
    ConfigFieldSpec(
        "DESKTOP_HEALTH_PROBE_TIMEOUT",
        "Health probe timeout",
        "desktop",
        "number",
        settings_attr="desktop_health_probe_timeout",
        default="1.5",
        advanced=True,
        description=(
            "How long one check of the server's health URL may take before it "
            "counts as no answer. One number for every process that makes the "
            "check, including the desktop app."
        ),
    ),
    ConfigFieldSpec(
        "DESKTOP_FOREIGN_GRACE_SECONDS",
        "Port conflict grace",
        "desktop",
        "number",
        settings_attr="desktop_foreign_grace_seconds",
        default="45",
        advanced=True,
        description=(
            "How long an unrecognised program may hold the port before the "
            "desktop app reports a conflict instead of assuming the holder is "
            "My Claude Code still starting. Shorter means faster conflict "
            "reporting; longer means fewer false alarms during a slow start."
        ),
    ),
    ConfigFieldSpec(
        "DESKTOP_STATUS_WALL_SECONDS",
        "Status read timeout",
        "desktop",
        "number",
        settings_attr="desktop_status_wall_seconds",
        default="15",
        advanced=True,
        description=(
            "How long the desktop app waits for one status read before it "
            "paints something anyway. Raise it on a slow machine where "
            "antivirus scanning makes a cold start take longer than this."
        ),
    ),
    ConfigFieldSpec(
        "DESKTOP_SHELL_AUTO_UPDATE",
        "Update the desktop app automatically",
        "desktop",
        "boolean",
        settings_attr="desktop_shell_auto_update",
        default="true",
        description=(
            "Whether the server brings an out-of-date desktop app up to the "
            "release it pins, once, just after it starts. On means updating "
            "the server updates the app too, with nothing to run by hand; the "
            "new app is used the next time you start it."
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
    """Attach the usable range to a numeric field.

    The bounds the form enforces are the same object the server clamps to;
    two hand-maintained copies would eventually disagree. The human form of
    the range is published separately (``range_hint``) rather than glued to
    the end of the description: measured on the Limits page, a field's help
    ran to 80 words before the bound appeared.
    """
    limit = range_for(field.settings_attr)
    if limit is None:
        return field
    return replace(
        field,
        minimum=limit.minimum,
        maximum=limit.maximum,
        range_hint=describe_range(limit),
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


def update_affects_providers(updates: Iterable[str]) -> bool:
    """Whether an admin update can change a provider client or its catalogue.

    Read off the manifest, never a key list written here: each field declares
    ``affects_providers`` beside its own label and help text, so a key added
    later is classified where it is defined. A key the manifest does not own
    answers ``True`` -- the expensive answer is the safe one.

    The fields that answer ``False`` are the ones read off ``Settings`` where
    they are used rather than baked into a provider client. A pause is read at
    plan time and never reaches ``create_provider``; the two model-visibility
    lists are read by the ``/v1/models`` response and the catalogue documents
    when those are built. For either, rebuilding every provider and re-querying
    every ``/models`` cannot change what the write does; it only made the click
    cost a network sweep.
    """

    keys = tuple(updates)
    if not keys:
        return False
    return any(
        FIELD_BY_KEY[key].affects_providers if key in FIELD_BY_KEY else True
        for key in keys
    )


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
