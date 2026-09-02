"""Parse the ``{model}:{method}`` tail of a Gemini REST path.

Google puts the method in the path with a colon -- ``models/gemini-3-pro
:generateContent`` -- and the model segment itself may contain slashes,
because MCC's routable ids are ``<gateway>/<provider>/<model>``. That is legal
and survives the wire: ``@google/genai`` joins the path as a plain string and
hands it to ``new URL()``, which percent-encodes neither ``/`` nor ``:`` in a
path. Verified against the bundled SDK's ``constructUrl``/``tModel`` in Gemini
CLI 0.49.0.

So the route has to match greedily and split here rather than relying on a
single path parameter, and the split is from the **right**: a colon inside a
model id would otherwise eat the method.
"""

from dataclasses import dataclass

#: The methods this proxy serves under ``/v1beta/models/``.
GENERATE_CONTENT = "generateContent"
STREAM_GENERATE_CONTENT = "streamGenerateContent"
COUNT_TOKENS = "countTokens"

SUPPORTED_METHODS: frozenset[str] = frozenset(
    {GENERATE_CONTENT, STREAM_GENERATE_CONTENT, COUNT_TOKENS}
)

#: Every method Google publishes for a model, as ``GET /v1beta/models`` reports
#: it. Only the three above are actually served; the list is what a client
#: reads to decide whether to try, so it names exactly those three.
SUPPORTED_GENERATION_METHODS: tuple[str, ...] = (
    GENERATE_CONTENT,
    STREAM_GENERATE_CONTENT,
    COUNT_TOKENS,
)

_MODELS_PREFIX = "models/"


@dataclass(frozen=True, slots=True)
class GeminiModelPath:
    """One parsed ``{model}:{method}`` tail."""

    model: str
    method: str


def parse_model_method_path(tail: str) -> GeminiModelPath | None:
    """Split ``"<model>:<method>"`` into its two halves.

    Returns ``None`` when the tail carries no method at all, which is a
    ``GET /v1beta/models/<model>`` describe request rather than a generation
    call. A leading ``models/`` is stripped: Google's own SDKs prefix it, and
    MCC's own model ids never begin with it.
    """

    cleaned = tail.strip().strip("/")
    if not cleaned:
        return None
    model, separator, method = cleaned.rpartition(":")
    if not separator:
        return None
    return GeminiModelPath(model=strip_models_prefix(model), method=method)


def strip_models_prefix(value: str) -> str:
    """Return a model id with Google's ``models/`` collection prefix removed."""

    cleaned = value.strip().strip("/")
    while cleaned.startswith(_MODELS_PREFIX):
        cleaned = cleaned[len(_MODELS_PREFIX) :]
    return cleaned


def model_resource_name(model_id: str) -> str:
    """Return the ``models/<id>`` resource name Google's listings publish."""

    return f"{_MODELS_PREFIX}{strip_models_prefix(model_id)}"
