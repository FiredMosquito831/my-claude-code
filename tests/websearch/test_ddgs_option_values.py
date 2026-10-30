"""The ``ddgs`` provider's options say what they do, and a saved ``strict`` works (7.120.2).

RESEARCH-DDG-WEBSEARCH.md R5 and its doc-bug finding: MCC's docs gave
``DDGS_SAFESEARCH=strict``, which the ``ddgs`` package does not know -- measured
on 9.16.1, its Brave engine reads it as off and its Startpage engine raises on
it. The package's own values are ``on``, ``moderate`` and ``off``, and those
(like anything else) still reach it exactly as written. A ``strict`` saved from
the old docs is read as ``on``; the dashboard's select offers it, because a
select whose saved value is not one of its options is drawn blank and the next
save would empty the line; and the saved line is never rewritten.

The four help texts of the ``ddgs`` drawer are pinned here as well: the Web
Search page's DOM comparison with the 7.112.1 base tree
(``tests/api/test_admin_static_jsdom.py``) leaves out the ones this release
corrects, so this is what keeps them right.
"""

import pytest

from my_claude_code.config.admin.manifest import FIELD_BY_KEY
from my_claude_code.config.admin.values import load_config_response
from my_claude_code.config.env_files import settings_env_files
from my_claude_code.config.settings import Settings
from my_claude_code.config.websearch_catalog import (
    DDGS_SAFESEARCH_ALIASES,
    WEBSEARCH_CATALOG,
    WebSearchOptionSpec,
)
from my_claude_code.websearch.adapters.ddgs import DdgsWebSearchProvider
from my_claude_code.websearch.options import read_websearch_options
from tests.websearch.support import build_config

#: The values ``ddgs`` itself takes (its CLI's ``click.Choice``, and the keys
#: of the tables its Google and Startpage engines look them up in).
DDGS_VALUES = ("on", "moderate", "off")


def _spec(env: str) -> WebSearchOptionSpec:
    return next(
        spec for spec in WEBSEARCH_CATALOG["ddgs"].advanced_options if spec.env == env
    )


def _capture_ddgs_calls(monkeypatch: pytest.MonkeyPatch) -> dict:
    captured: dict = {}

    class FakeDDGS:
        def __init__(self, proxy=None, timeout=None) -> None:
            pass

        def text(self, query, **kwargs):
            captured.update(kwargs)
            return []

    monkeypatch.setattr("my_claude_code.websearch.adapters.ddgs.DDGS", FakeDDGS)
    return captured


async def _search_with(monkeypatch: pytest.MonkeyPatch, options: dict) -> dict:
    captured = _capture_ddgs_calls(monkeypatch)
    provider = DdgsWebSearchProvider(
        build_config(api_keys=(), rotation="single", options=options)
    )
    await provider.search("q", max_results=3)
    return captured


# --------------------------------------------------------------- the values


def test_the_select_offers_the_ddgs_values_and_the_older_strict() -> None:
    spec = _spec("DDGS_SAFESEARCH")
    assert [value for value, _label in spec.options] == [
        "",
        "on",
        "moderate",
        "off",
        "strict",
    ]
    assert dict(spec.options)["strict"] == "strict (older spelling, read as on)"


def test_every_older_spelling_is_offered_and_reads_as_a_ddgs_value() -> None:
    offered = {value for value, _label in _spec("DDGS_SAFESEARCH").options}
    assert DDGS_SAFESEARCH_ALIASES == {"strict": "on"}
    for spelling, value in DDGS_SAFESEARCH_ALIASES.items():
        assert spelling in offered, spelling
        assert value in DDGS_VALUES, value


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [*DDGS_VALUES, "ON", "unknown"])
async def test_every_other_value_reaches_ddgs_exactly_as_written(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    captured = await _search_with(monkeypatch, {"DDGS_SAFESEARCH": value})
    assert captured == {"max_results": 3, "safesearch": value}


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["strict", "Strict", "STRICT"])
async def test_strict_reaches_ddgs_as_on(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    captured = await _search_with(monkeypatch, {"DDGS_SAFESEARCH": value})
    assert captured == {"max_results": 3, "safesearch": "on"}


@pytest.mark.asyncio
async def test_unset_sends_no_safesearch(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = await _search_with(monkeypatch, {})
    assert captured == {"max_results": 3}


@pytest.mark.asyncio
async def test_a_saved_strict_is_shown_kept_and_searched_as_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The settings round trip: the saved line, the dashboard's view, a search."""

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DDGS_SAFESEARCH", raising=False)
    env_file = tmp_path / ".mcc" / ".env"
    # The suite turns dotenv reading off; a search reads the files a server
    # resolves (the repository's .env, then the managed one, from HOME).
    server_files = settings_env_files()
    assert env_file in server_files
    monkeypatch.setattr(
        Settings, "model_config", {**Settings.model_config, "env_file": server_files}
    )
    env_file.parent.mkdir(parents=True)
    saved = b"DDGS_SAFESEARCH=strict\n"
    env_file.write_bytes(saved)

    fields = load_config_response()["fields"]
    field = next(field for field in fields if field["key"] == "DDGS_SAFESEARCH")
    assert field["value"] == "strict"
    assert field["source"] == "managed_env"
    # One of the select's options: the page shows it selected, and an
    # unchanged save sends nothing for it.
    assert "strict" in [option["value"] for option in field["options"]]

    options = read_websearch_options("ddgs", WEBSEARCH_CATALOG["ddgs"])
    assert options == {"DDGS_SAFESEARCH": "strict"}
    captured = await _search_with(monkeypatch, options)
    assert captured == {"max_results": 3, "safesearch": "on"}

    assert env_file.read_bytes() == saved


# ----------------------------------------------------------- the help texts


@pytest.mark.parametrize(
    ("env", "phrases"),
    [
        (
            "DDGS_SAFESEARCH",
            (
                "Empty = moderate",
                "DuckDuckGo's own engine ignores it",
                "Brave, Mojeek and Startpage in ddgs 9.16.1",
                "strict, the spelling older MCC docs gave, is read as on",
            ),
        ),
        (
            "DDGS_BACKEND",
            (
                "pin one engine",
                "bing, google and yandex are switched off inside ddgs 9.16.1",
                "choosing one runs auto",
            ),
        ),
        (
            "DDGS_REGION",
            (
                "Empty = us-en",
                "region-language",
                "keyless DuckDuckGo hop as its kl field",
            ),
        ),
        (
            "DDGS_TIMELIMIT",
            (
                "Empty = no recency filter",
                "keyless DuckDuckGo hop as its df field",
            ),
        ),
    ],
)
def test_each_ddgs_help_text_says_what_the_option_does(
    env: str, phrases: tuple[str, ...]
) -> None:
    note = _spec(env).cost_note
    # The dashboard shows the catalog's note as the field's help text.
    assert FIELD_BY_KEY[env].description == note
    for phrase in phrases:
        assert phrase in note, (env, phrase)
    # The false claim 7.120.2 removed (ddgs asks for us-en when it is empty).
    assert "picks by IP" not in note
