"""Promote the model a user named into Cline's single-provider settings block.

Cline's ``providers.json`` carries one settings object per provider id, and
that object holds the numbers for **the one model it names** -- there is no
per-model array in the schema (see ``application/catalogues/cline.py``). So the
serialiser writes every routable model's resolved limits into an inert
``_mcc_models`` block and leaves the first as the session default, and this
module moves whichever one the user actually asked for into ``settings`` on the
way to disk.

It lives in ``config`` rather than beside the serialiser for the same reason
every other cross-layer harness helper does: ``cli`` may not import
``application``, and both the launcher and the runtime fan-out publisher need
it.
"""

from collections.abc import Mapping, Sequence
from typing import Any

#: Cline's own flags for choosing a model, in the spellings its own ``--help``
#: publishes. A user who passes one of these is naming the model this session
#: runs on, and MCC's job is to make sure that model's numbers are the ones in
#: the document Cline reads.
MODEL_FLAGS: tuple[str, ...] = ("-m", "--model")

#: The block the Cline serialiser writes and :func:`strip_mcc_keys` removes,
#: mirrored here so the launcher does not have to import ``application``.
MODELS_KEY = "_mcc_models"

#: Mirrored from ``application/catalogues/base.py`` for the same reason:
#: ``config`` may not import ``application``, and both halves have to agree on
#: the key that is stripped.
DEFAULTED_KEY = "_mcc_defaulted"


def selected_model(argv: Sequence[str]) -> str | None:
    """Return the model id a user named with ``-m``/``--model``, if any.

    Both the separated (``-m id``) and joined (``--model=id``) forms are read,
    and the *last* occurrence wins, because that is what Cline's own argument
    parser does with a repeated option.
    """

    found: str | None = None
    index = 0
    while index < len(argv):
        argument = argv[index]
        for flag in MODEL_FLAGS:
            if argument == flag and index + 1 < len(argv):
                found = argv[index + 1]
                index += 1
                break
            if argument.startswith(f"{flag}="):
                found = argument[len(flag) + 1 :]
                break
        index += 1
    return found or None


def with_selected_model(
    document: Mapping[str, Any], provider_id: str, model_id: str | None
) -> dict[str, Any]:
    """Return the document with ``model_id``'s resolved numbers in ``settings``.

    A ``model_id`` of ``None``, or one MCC does not route, leaves the document
    exactly as the serialiser built it: the first routable model stays the
    session default. Silently substituting a *different* model's context
    window for one MCC has never heard of would be worse than leaving Cline to
    its own fallback, which is to carry no limits at all.
    """

    models = document.get(MODELS_KEY)
    if model_id is None or not isinstance(models, Mapping):
        return dict(document)
    record = models.get(model_id)
    if not isinstance(record, Mapping):
        return dict(document)

    providers = document.get("providers")
    if not isinstance(providers, Mapping):
        return dict(document)
    entry = providers.get(provider_id)
    if not isinstance(entry, Mapping):
        return dict(document)
    settings = entry.get("settings")
    if not isinstance(settings, Mapping):
        return dict(document)

    # Every limit key any model record can carry is cleared first, so a model
    # that publishes no context window does not inherit the previous default's.
    keys = {
        key for value in models.values() if isinstance(value, Mapping) for key in value
    }
    updated = {key: value for key, value in settings.items() if key not in keys}
    updated["model"] = model_id
    updated.update(record)

    return {
        **document,
        "providers": {
            **providers,
            provider_id: {**entry, "settings": updated},
        },
    }


def with_api_key(
    document: Mapping[str, Any], provider_id: str, api_key: str
) -> dict[str, Any]:
    """Return the document with the real proxy token in ``settings.apiKey``.

    Cline stores a plain string and its environment fallback does not work for
    a headless run (measured: 3.0.61 blocked indefinitely with ``apiKey``
    absent and ``OPENAI_API_KEY`` set), so the value has to be literal. The
    file it lands in is MCC's own under ``~/.fcc/cline``, narrowed to ``0600``
    by the launcher; nothing is written into ``~/.cline``.
    """

    providers = document.get("providers")
    if not isinstance(providers, Mapping):
        return dict(document)
    entry = providers.get(provider_id)
    if not isinstance(entry, Mapping):
        return dict(document)
    settings = entry.get("settings")
    if not isinstance(settings, Mapping):
        return dict(document)
    return {
        **document,
        "providers": {
            **providers,
            provider_id: {**entry, "settings": {**settings, "apiKey": api_key}},
        },
    }


def strip_mcc_keys(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return the document as Cline itself would write it, and no more.

    Cline validates ``providers.json`` as a whole and discards it on any
    surprise. Measured on 3.0.61: one unrecognised *root* key -- ``_mcc_models``,
    or even ``_mcc_defaulted`` alone -- made it drop the provider settings it
    had just read and rewrite the file with its own bundled default model,
    losing the base URL and the key with it. The next run then reached
    ``api.openai.com``.

    So MCC's bookkeeping never reaches disk. It exists in the document the
    server publishes because :func:`with_selected_model` needs it, and this is
    the last step before the write. The consequence is stated where it shows:
    the Coding agents card reads the file, so Cline's card reports one provider
    block rather than a model count, and the defaulted record is reported on
    the launcher's stderr and by ``GET /admin/api/catalogue-models`` instead.
    """

    return {
        key: value
        for key, value in document.items()
        if key not in (MODELS_KEY, DEFAULTED_KEY)
    }
