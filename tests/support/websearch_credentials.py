"""The web-search credentials a ``Settings()`` picks up from the environment.

Five tests asserted on "no web-search provider is configured" while building a
``Settings`` straight from ``os.environ``, so on a developer machine with
``EXA_API_KEY`` exported they asserted the opposite of what they read. Same
class of defect as reading the real ``~/.fcc/.env``: the outcome came from the
machine rather than from the repository. State the credentials the case needs.
"""

from my_claude_code.config.settings import Settings

#: Field names on ``Settings`` that hold a web-search credential. Their
#: environment names are the validation aliases, resolved once here so a
#: renamed alias cannot silently stop being cleared.
WEB_SEARCH_CREDENTIAL_FIELDS: tuple[str, ...] = (
    "ollama_search_api_key",
    "exa_api_key",
    "tavily_api_key",
    "brave_search_api_key",
    "jina_api_key",
    "serper_api_key",
    "firecrawl_api_key",
    "linkup_api_key",
    "perplexity_search_api_key",
    "parallel_api_key",
    "searchapi_api_key",
    "serpapi_api_key",
    "searxng_base_url",
)

WEB_SEARCH_CREDENTIAL_ENVS: tuple[str, ...] = tuple(
    str(Settings.model_fields[name].validation_alias or name.upper())
    for name in WEB_SEARCH_CREDENTIAL_FIELDS
)

#: Ready to splat into ``Settings.model_validate``. A blank string means unset
#: for every one of these fields -- see ``BLANK_MEANS_UNSET_FIELDS``.
NO_WEB_SEARCH_CREDENTIALS: dict[str, str] = dict.fromkeys(
    WEB_SEARCH_CREDENTIAL_ENVS, ""
)


def forget_web_search_credentials(monkeypatch) -> None:
    """Make ``Settings()`` see an environment with no search provider keyed."""

    for name in WEB_SEARCH_CREDENTIAL_ENVS:
        monkeypatch.delenv(name, raising=False)
