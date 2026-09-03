"""Put the real harness id into a generated catalogue that cannot know it.

MCC's own launchers tell the proxy which coding agent they started, by way of
the ``x-mcc-harness`` request header the CLI is configured to send. For a
harness configured from argv or the environment the launcher writes the value
directly -- it holds the ``HarnessSpec``. For a harness configured from a
*document*, it cannot: a catalogue serialiser is typed
``Iterable[CatalogueModel] -> document`` and is a pure function of the model
records, so ``build_opencode_catalogue`` genuinely does not know whether it is
writing OpenCode's file, OpenCode 2's or Kilo's -- one serialiser, three
harness ids, and the id is exactly what the header has to carry.

So the serialiser writes ``MCC_HARNESS_ID_SENTINEL`` and the caller resolves
it here, which is the same shape ``config/harness_base_url`` already uses for
the base URLs the serialisers cannot know either. Two callers, and between
them every route a document takes to disk:
``cli/harnesses/catalogue_client.harness_catalogue`` for a launch, and
``runtime/harness_catalogues`` for the background refresh.

The header name itself lives in ``core/client_fingerprint`` beside the
classifier that reads it back, so the emitter and the reader can never
disagree about its spelling. It is deliberately not restated in ``config``:
``config`` names the sentinel, ``core`` names the header, and the serialisers
-- which are in ``application`` and may import both -- put the two together.
"""

from collections.abc import Mapping
from typing import Any

from my_claude_code.config.harness_base_url import with_resolved_sentinel
from my_claude_code.config.harnesses import MCC_HARNESS_ID_SENTINEL


def with_harness_id(document: Mapping[str, Any], harness_id: str) -> dict[str, Any]:
    """Return the document with the harness-id sentinel resolved to ``harness_id``.

    Idempotent and safe on a document that carries no sentinel at all, which is
    most of them: only the five formats whose CLI accepts a custom request
    header declare one, and the other seven pass through untouched.
    """

    return with_resolved_sentinel(document, MCC_HARNESS_ID_SENTINEL, harness_id)
