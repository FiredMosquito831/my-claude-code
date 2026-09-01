"""Per-CLI serialisers for the neutral catalogue record.

One pure function per harness catalogue format, each
``Iterable[CatalogueModel] -> (document, DefaultedFields)``. Looking a
serialiser up by ``format_id`` is what lets the registry
(``config/harnesses.py``) name a format without ``config`` depending on this
package, and what lets the runtime fan-out publisher iterate every harness
without knowing any of them by name.
"""

from collections.abc import Callable, Iterable
from typing import Any

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues.base import DefaultedFields
from my_claude_code.application.catalogues.codex import build_codex_catalogue
from my_claude_code.application.catalogues.pi import build_pi_catalogue

type CatalogueSerialiser = Callable[
    [Iterable[CatalogueModel]], tuple[dict[str, Any], DefaultedFields]
]

SERIALISERS: dict[str, CatalogueSerialiser] = {
    "codex": build_codex_catalogue,
    "pi": build_pi_catalogue,
}


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
