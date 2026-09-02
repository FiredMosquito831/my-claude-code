"""Serialise the neutral catalogue into Kilo CLI's config shape.

Kilo is a fork of OpenCode and says so -- "The Kilo CLI is a fork of OpenCode
and supports the same configuration options" -- so the document is
:func:`~my_claude_code.application.catalogues.opencode.build_opencode_catalogue`
verbatim, with exactly one difference, and that difference is why this module
exists rather than a shared ``format_id``.

**Kilo rejects unknown ROOT keys; OpenCode tolerates them.** Read out of the
7.5.9 bundle, Kilo runs an excess-key check against the root document before
decoding it::

    function unknownKeys(schema, value) { ... }
    if (unknown.length) throw new ConfigInvalid({
        issues: [{code: "unrecognized_keys", message: `Unrecognized key: ...`}] })

and it is applied at the root only (``path: []``), with the decode itself
passing no ``onExcessProperty`` -- so a nested extra key is ignored and a root
one is fatal. Measured against the real binary:

* ``_mcc_defaulted`` at the root, in ``$XDG_CONFIG_HOME/kilo/kilo.json``:
  ``Error: Configuration is invalid ... Unrecognized key: _mcc_defaulted``,
  exit 1, zero models.
* the same document through ``KILO_CONFIG``: **exit 0 and no error at all**,
  because that load path wraps the parse in ``catchDefect`` and swallows a
  failure into an empty object. The provider simply vanishes. That is the
  worse of the two outcomes and it is what a user actually saw: ``kilo models``
  listed 300 built-ins, none of them MCC's, and said nothing.

So the record is dropped from the document rather than hidden somewhere Kilo
happens not to look. It has two other homes that Kilo cannot break -- the
launcher's stderr summary and the Coding agents card, both of which read it
from ``GET /admin/api/catalogue-models`` rather than from the file -- and
smuggling MCC's bookkeeping into a CLI's own ``experimental`` namespace to
survive a schema check would be worse than not writing it.

Everything else, including the all-or-nothing ``limit`` rule, is OpenCode's
and is documented there. Kilo enforces the same ``limit``/``cost`` required
pairs (CONFIRMED against 7.5.9:
``Missing key provider.mcc.models.probe-1.limit.output``).
"""

from collections.abc import Iterable
from typing import Any

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues.base import DEFAULTED_KEY, DefaultedFields
from my_claude_code.application.catalogues.opencode import (
    CLI_REQUIRED_KEYS as CLI_REQUIRED_KEYS_OPENCODE,
)
from my_claude_code.application.catalogues.opencode import build_opencode_catalogue

#: Identical to OpenCode's -- Kilo enforces the same per-model rules, and
#: ``Missing key provider.mcc.models.probe-1.limit.output`` was reproduced
#: against 7.5.9. The one difference is at the root, and it is the reason this
#: module exists; see the docstring. ``CLI_DOCUMENTED_DEFAULTS`` is likewise
#: OpenCode's, and is not restated here: two copies of one CLI's numbers is
#: how they drift.
CLI_REQUIRED_KEYS: frozenset[str] = CLI_REQUIRED_KEYS_OPENCODE


def build_kilo_catalogue(
    models: Iterable[CatalogueModel],
) -> tuple[dict[str, Any], DefaultedFields]:
    """Return OpenCode's document without the root key Kilo refuses."""

    document, defaulted = build_opencode_catalogue(models)
    document.pop(DEFAULTED_KEY, None)
    return document, defaulted
