"""Which routable refs a CLI picker lists, and which one it opens on.

Two rules that have to hold identically in three places -- the serialisers
under ``application/catalogues``, the launchers under ``cli/launchers`` that
configure a CLI through the environment instead of a file, and the ``/v1beta``
model listing -- and that therefore live in neither. ``cli`` may import
``core`` and ``application`` may import ``core``; this is the only shared
owner the three have.
"""

from collections.abc import Callable, Sequence

#: Routing tags that name a *pricing tier* rather than a second interactive
#: model. A ``:batch`` ref is the same model billed asynchronously: it answers
#: on a queue, not in a session, so a coding agent's picker gains nothing from
#: it and every user of every CLI pays for it in list length. They stay in
#: ``/v1/models`` -- that route answers "what ids may I send?", and the answer
#: still includes them -- and are excluded from the pickers.
EXCLUDED_REF_SUFFIXES: tuple[str, ...] = (":batch",)

#: Routing tags that name a free tier. Free routes are real routes and stay in
#: every catalogue; they are only skipped when MCC has to *choose one model for
#: the user* and has no configured route to choose. A free tier is the entry
#: most likely to have been withdrawn upstream, and a session that opens on a
#: withdrawn model looks like MCC itself is broken.
FREE_REF_SUFFIXES: tuple[str, ...] = (":free",)


def is_excluded_ref(provider_model_ref: str) -> bool:
    """Whether a ref names a pricing tier no CLI picker should list."""

    return provider_model_ref.endswith(EXCLUDED_REF_SUFFIXES)


def is_free_ref(provider_model_ref: str) -> bool:
    """Whether a ref names a free tier."""

    return provider_model_ref.endswith(FREE_REF_SUFFIXES)


#: How the model a session opens on was chosen, in the operator's words. The
#: launcher prints it and the dashboard card renders it, because "MCC picked
#: this one" is only reassuring if it also says why.
STARTING_MODEL_REASONS: dict[str, str] = {
    "primary": "MCC's configured MODEL",
    "first_paid": "the first entry that is not a free tier (MODEL is not listed)",
    "first": "the first entry (every listed model is a free tier)",
}


def select_starting_index[T](
    entries: Sequence[T],
    provider_model_ref: Callable[[T], str],
    is_primary_route: Callable[[T], bool],
) -> tuple[int, str] | None:
    """Index of the model a CLI that must pin one should open on, and why.

    Three CLIs cannot open a session without a model named up front, and all
    three used to take the first entry of the enumeration. That is an
    implementation detail wearing the clothes of a decision: on a real install
    it selected a free tier whose provider had withdrawn it, so every session
    opened on a model that answered 404 while eighty working ones sat below it.

    The rule, in order:

    1. The ref named by ``MODEL`` -- the route MCC itself starts on, chosen by
       the operator.
    2. Failing that (``MODEL`` hidden, or not among the listed entries), the
       first entry that is not a free tier.
    3. Failing that, the first entry: a list of only free tiers still has to
       open on something.

    ``None`` only for an empty list.
    """

    if not entries:
        return None
    for index, entry in enumerate(entries):
        if is_primary_route(entry):
            return index, "primary"
    for index, entry in enumerate(entries):
        if not is_free_ref(provider_model_ref(entry)):
            return index, "first_paid"
    return 0, "first"
