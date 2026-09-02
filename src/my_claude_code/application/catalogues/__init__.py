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
from my_claude_code.application.catalogues.aider import (
    build_aider_catalogue,
    build_aider_model_settings,
)
from my_claude_code.application.catalogues.base import DEFAULTED_KEY, DefaultedFields
from my_claude_code.application.catalogues.cline import build_cline_catalogue
from my_claude_code.application.catalogues.codex import build_codex_catalogue
from my_claude_code.application.catalogues.commandcode import (
    PROVIDER_ID as COMMANDCODE_PROVIDER_ID,
)
from my_claude_code.application.catalogues.commandcode import (
    build_commandcode_catalogue,
)
from my_claude_code.application.catalogues.crush import (
    PROVIDER_ID as CRUSH_PROVIDER_ID,
)
from my_claude_code.application.catalogues.crush import build_crush_catalogue
from my_claude_code.application.catalogues.droid import (
    CUSTOM_MODELS_KEY as DROID_MODELS_KEY,
)
from my_claude_code.application.catalogues.droid import build_droid_catalogue
from my_claude_code.application.catalogues.kimi import build_kimi_catalogue
from my_claude_code.application.catalogues.opencode import (
    PROVIDER_ID as OPENCODE_PROVIDER_ID,
)
from my_claude_code.application.catalogues.opencode import build_opencode_catalogue
from my_claude_code.application.catalogues.pi import build_pi_catalogue
from my_claude_code.application.catalogues.qwen import AUTH_TYPE as QWEN_AUTH_TYPE
from my_claude_code.application.catalogues.qwen import build_qwen_catalogue

type CatalogueSerialiser = Callable[
    [Iterable[CatalogueModel]], tuple[dict[str, Any], DefaultedFields]
]

SERIALISERS: dict[str, CatalogueSerialiser] = {
    "aider": build_aider_catalogue,
    "cline": build_cline_catalogue,
    "codex": build_codex_catalogue,
    "commandcode": build_commandcode_catalogue,
    "crush": build_crush_catalogue,
    "droid": build_droid_catalogue,
    "kimi": build_kimi_catalogue,
    "opencode": build_opencode_catalogue,
    "pi": build_pi_catalogue,
    "qwen": build_qwen_catalogue,
}

#: Where each format keeps its per-model entries. Two shapes exist and both
#: are the CLI's own: Codex and Pi take a list under ``models``, OpenCode a
#: mapping nested under the provider key it was told to write. Kimi Code is a
#: third: a mapping at the document root, keyed by the whole model id, because
#: that is what ``Config.models`` is. Crush and Qwen Code are lists again, one
#: nested under a provider key and one under an auth-type key. Stating the
#: path once is what lets a launcher check "did I get any models?" and the
#: contract tests inspect every format without knowing any of them by name.
MODEL_ENTRY_PATHS: dict[str, tuple[str, ...]] = {
    "aider": (),
    "cline": ("providers",),
    "codex": ("models",),
    "commandcode": ("provider", COMMANDCODE_PROVIDER_ID, "models"),
    "crush": ("providers", CRUSH_PROVIDER_ID, "models"),
    "droid": (DROID_MODELS_KEY,),
    "kimi": ("models",),
    "opencode": ("provider", OPENCODE_PROVIDER_ID, "models"),
    "pi": ("models",),
    "qwen": ("modelProviders", QWEN_AUTH_TYPE),
}

#: The second document a harness reads, for the one format that has two.
#: ``Iterable[CatalogueModel] -> list`` rather than the pair every primary
#: serialiser returns: the defaulted record belongs to the primary document,
#: which is the one the launcher and the dashboard read it back out of.
type CatalogueSidecarSerialiser = Callable[[Iterable[CatalogueModel]], list[Any]]

SIDECAR_SERIALISERS: dict[str, CatalogueSidecarSerialiser] = {
    "aider": build_aider_model_settings,
}


def serialise_sidecar(
    format_id: str, models: Iterable[CatalogueModel]
) -> list[Any] | None:
    """Serialise the second document, for a format that declares one."""

    serialiser = SIDECAR_SERIALISERS.get(format_id)
    return None if serialiser is None else serialiser(models)


def model_entries(format_id: str, document: Mapping[str, Any]) -> list[Any]:
    """Return one catalogue document's per-model entries, whatever its shape."""

    node: Any = document
    for key in MODEL_ENTRY_PATHS[format_id]:
        if not isinstance(node, Mapping):
            return []
        node = node.get(key)
    if isinstance(node, Mapping):
        # Aider's metadata file *is* the model map, so the record of what was
        # defaulted sits beside the models rather than under them. It is not a
        # model and must not be counted as one.
        return [value for key, value in node.items() if key != DEFAULTED_KEY]
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
