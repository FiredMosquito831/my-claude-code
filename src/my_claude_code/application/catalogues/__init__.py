"""Per-CLI serialisers for the neutral catalogue record.

One pure function per harness catalogue format, each
``Iterable[CatalogueModel] -> (document, DefaultedFields)``. Looking a
serialiser up by ``format_id`` is what lets the registry
(``config/harnesses.py``) name a format without ``config`` depending on this
package, and what lets the runtime fan-out publisher iterate every harness
without knowing any of them by name.
"""

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues.base import DefaultedFields
from my_claude_code.application.catalogues.codex import build_codex_catalogue
from my_claude_code.application.catalogues.commandcode import (
    PROVIDER_ID as COMMANDCODE_PROVIDER_ID,
)
from my_claude_code.application.catalogues.commandcode import (
    build_commandcode_catalogue,
)
from my_claude_code.application.catalogues.opencode import (
    PROVIDER_ID as OPENCODE_PROVIDER_ID,
)
from my_claude_code.application.catalogues.opencode import build_opencode_catalogue
from my_claude_code.application.catalogues.pi import build_pi_catalogue

type CatalogueSerialiser = Callable[
    [Iterable[CatalogueModel]], tuple[dict[str, Any], DefaultedFields]
]

SERIALISERS: dict[str, CatalogueSerialiser] = {
    "codex": build_codex_catalogue,
    "commandcode": build_commandcode_catalogue,
    "opencode": build_opencode_catalogue,
    "pi": build_pi_catalogue,
}

#: Where each format keeps its per-model entries. Two shapes exist and both
#: are the CLI's own: Codex and Pi take a list under ``models``, OpenCode a
#: mapping nested under the provider key it was told to write. Stating the
#: path once is what lets a launcher check "did I get any models?" and the
#: contract tests inspect every format without knowing any of them by name.
MODEL_ENTRY_PATHS: dict[str, tuple[str, ...]] = {
    "codex": ("models",),
    "commandcode": ("provider", COMMANDCODE_PROVIDER_ID, "models"),
    "opencode": ("provider", OPENCODE_PROVIDER_ID, "models"),
    "pi": ("models",),
}


def model_entries(format_id: str, document: Mapping[str, Any]) -> list[Any]:
    """Return one catalogue document's per-model entries, whatever its shape."""

    node: Any = document
    for key in MODEL_ENTRY_PATHS[format_id]:
        if not isinstance(node, Mapping):
            return []
        node = node.get(key)
    if isinstance(node, Mapping):
        return list(node.values())
    if isinstance(node, list):
        return list(node)
    return []


def serialiser_for(format_id: str) -> CatalogueSerialiser:
    """Return the serialiser registered for one catalogue format."""

    try:
        return SERIALISERS[format_id]
    except KeyError:
        raise KeyError(f"no catalogue serialiser for format: {format_id}") from None


def serialise(
    format_id: str, models: Iterable[CatalogueModel]
) -> tuple[dict[str, Any], DefaultedFields]:
    """Serialise the neutral records into one CLI's catalogue document."""

    return serialiser_for(format_id)(models)
