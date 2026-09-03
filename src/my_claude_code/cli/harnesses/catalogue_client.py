"""Fetch a harness's generated catalogue from the running proxy.

A launcher runs in its own process. It has no ``RequestRuntimePort``, so it
cannot reach the resolution ladder, and ``GET /v1/models`` -- the only route it
used to call -- carries no capability fields at all. That is why every
generated catalogue used to contain the same invented numbers for every model.

``GET /admin/api/catalogue-models`` closes the gap: the server resolves the
capabilities and runs the harness's own serialiser, and the launcher writes the
bytes it is handed. Nothing about a CLI's schema is duplicated in the launcher,
so a mapping can never drift between the launch-time path and the background
refresh.
"""

import json
import os
import sys
from collections.abc import Mapping
from typing import Any
from urllib.request import Request, urlopen

from my_claude_code.config.harness_attribution import with_harness_id
from my_claude_code.config.settings import get_settings

CATALOGUE_MODELS_PATH = "/admin/api/catalogue-models"


def catalogue_fetch_timeout() -> float:
    """Return this install's budget for one cold-start catalogue build.

    Resolved here rather than threaded through ten launcher signatures: every
    launcher fetches through :func:`fetch_catalogue_models` and none of them has
    any business holding a different number. ``Settings`` clamps the field to
    ``config.limits.LIMIT_RANGES``, whose floor is 1 s, so this can never return
    the ``0`` that would make ``urlopen`` fail before it connected.
    """

    return float(get_settings().catalogue_fetch_timeout_seconds)


def fetch_catalogue_models(
    proxy_root_url: str,
    auth_token: str,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Fetch the capability-bearing catalogue payload from the local proxy.

    The budget is this route's own, never
    ``cli.launchers.common.PROXY_PREFLIGHT_TIMEOUT_SECONDS``. That constant is
    sized for ``GET /health``, which answers in milliseconds; this route
    serialises every registered harness's document out of the resolution ladder
    and measured 1.8-4.0 s on a 292-model install, so sharing the one number
    made this call fail on every launch. ``tests/cli/test_opencode_launcher.py``
    holds the two apart as a static guard, because the failure they produced
    together was silent and permanent.
    """

    url = f"{proxy_root_url.rstrip('/')}{CATALOGUE_MODELS_PATH}"
    headers: dict[str, str] = {}
    if token := auth_token.strip():
        headers["Authorization"] = f"Bearer {token}"

    budget = catalogue_fetch_timeout() if timeout_seconds is None else timeout_seconds
    request = Request(url, headers=headers, method="GET")
    with urlopen(request, timeout=budget) as response:
        payload = json.loads(response.read().decode("utf-8"))

    if not isinstance(payload, dict):
        raise ValueError("catalogue-models response was not a JSON object")
    return payload


def harness_catalogue(payload: Mapping[str, Any], harness_id: str) -> dict[str, Any]:
    """Return one harness's serialised catalogue document from the payload.

    The harness-id sentinel is resolved here, which is the one place on the
    launch path where the document and the id it was fetched under are both in
    hand. Every launcher funnels through this function, so a harness added
    later inherits the substitution without doing anything: the alternative was
    the same three lines in each of five launchers, and the four that got them
    right would never have shown that the fifth did not. The base URL is
    resolved by the launcher instead, because only the launcher knows which
    proxy root it is pointing the CLI at -- this one does not vary.

    A document with no sentinel in it -- most of them -- passes through
    unchanged.
    """

    catalogues = payload.get("catalogues")
    if not isinstance(catalogues, Mapping):
        raise ValueError("catalogue-models response carried no catalogues")
    entry = catalogues.get(harness_id)
    if not isinstance(entry, Mapping):
        raise ValueError(f"catalogue-models response carried no {harness_id} catalogue")
    document = entry.get("document")
    if not isinstance(document, dict):
        raise ValueError(f"{harness_id} catalogue document was not a JSON object")
    return with_harness_id(document, harness_id)


def catalogue_model_count(payload: Mapping[str, Any], harness_id: str) -> int:
    """Return how many models one harness's document carries.

    The count is the server's, not the launcher's: every catalogue format
    nests its model entries somewhere different, and a launcher that dug the
    shape out for itself would be the second place a schema is described.
    """

    catalogues = payload.get("catalogues")
    if not isinstance(catalogues, Mapping):
        return 0
    entry = catalogues.get(harness_id)
    if not isinstance(entry, Mapping):
        return 0
    count = entry.get("model_count")
    return count if isinstance(count, int) else 0


#: Set to ``1`` to get the full per-model breakdown on stderr instead of the
#: one-line summary. Named for what it verbosifies, not for the whole product:
#: a launch already prints other things, and this switch governs exactly one
#: of them.
VERBOSE_ENV_VAR = "MCC_CATALOGUE_VERBOSE"


def catalogue_defaulted(
    payload: Mapping[str, Any], harness_id: str
) -> Mapping[str, list[str]]:
    """Return the record of what one harness's serialiser had to guess.

    Read from the *payload* rather than from the written document, because one
    harness's document cannot carry it: Kilo CLI rejects unknown root keys, so
    its file has no ``_mcc_defaulted`` block and reading the file back would
    report nothing guessed for the harness that guesses exactly as much as
    OpenCode does. The payload always has it.
    """

    catalogues = payload.get("catalogues")
    if not isinstance(catalogues, Mapping):
        return {}
    entry = catalogues.get(harness_id)
    if not isinstance(entry, Mapping):
        return {}
    defaulted = entry.get("defaulted")
    if not isinstance(defaulted, Mapping):
        return {}
    return {
        str(model_id): [str(name) for name in fields]
        for model_id, fields in defaulted.items()
        if isinstance(fields, list)
    }


def defaulted_summary(
    defaulted: Mapping[str, list[str]],
) -> tuple[int, list[tuple[str, int]]]:
    """Return ``(models affected, [(field, models) ...])``, commonest first."""

    counts: dict[str, int] = {}
    for fields in defaulted.values():
        for name in fields:
            counts[name] = counts.get(name, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return len(defaulted), ranked


def defaulted_summary_lines(defaulted: Mapping[str, list[str]]) -> list[str]:
    """Return one line per model whose catalogue entry needed a CLI default."""

    return [
        f"  {model_id}: {', '.join(fields)}"
        for model_id, fields in sorted(defaulted.items())
    ]


def print_defaulted_summary(
    display_name: str,
    defaulted: Mapping[str, list[str]],
    environ: Mapping[str, str] | None = None,
) -> None:
    """Say what the CLI had to guess -- in one line, unless asked for more.

    This used to print one line per affected model on every launch. On a real
    install that is 142 lines of stderr before the agent has said anything,
    every time, and a wall that long is not read: the user who reported it
    quoted six of the lines and could not tell that the seventh said the
    document had been refused.

    So the default is one line with the field histogram, and the per-model
    detail moves behind :data:`VERBOSE_ENV_VAR`. Nothing is lost by it -- the
    detail has two other homes, the generated file's own ``_mcc_defaulted``
    block and the Coding agents card -- and the one line still names the
    fields, which is the part that tells a user whether to care.
    """

    model_count, ranked = defaulted_summary(defaulted)
    if not model_count:
        return
    fields = ", ".join(f"{name} x{count}" for name, count in ranked)
    print(
        f"My Claude Code: {model_count} model(s) carry a value {display_name} "
        f"supplied because no provider published one ({fields}). Details: the "
        "Coding agents page, or set MCC_CATALOGUE_VERBOSE=1.",
        file=sys.stderr,
    )
    source = os.environ if environ is None else environ
    if source.get(VERBOSE_ENV_VAR, "").strip() not in {"1", "true", "yes", "on"}:
        return
    for line in defaulted_summary_lines(defaulted):
        print(line, file=sys.stderr)


def harness_sidecar(payload: Mapping[str, Any], harness_id: str) -> list[Any] | None:
    """Return one harness's *second* generated document, when it has one.

    Aider is the only harness that reads two files: limits and prices go in the
    LiteLLM-shaped metadata JSON, and what the model *accepts* goes in the
    model-settings YAML. ``None`` for every other harness.
    """

    catalogues = payload.get("catalogues")
    if not isinstance(catalogues, Mapping):
        return None
    entry = catalogues.get(harness_id)
    if not isinstance(entry, Mapping):
        return None
    document = entry.get("sidecar_document")
    return document if isinstance(document, list) else None


def catalogue_model_summaries(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the neutral records, for a harness MCC configures through the env.

    Goose has no generated file at all -- see ``cli/launchers/goose.py`` -- but
    it still needs a model to start on and a context limit to gauge against,
    and both are environment variables. This is the one accessor that reaches
    past the per-CLI documents to the records they were all built from.
    """

    models = payload.get("models")
    if not isinstance(models, list):
        return []
    return [model for model in models if isinstance(model, dict)]
