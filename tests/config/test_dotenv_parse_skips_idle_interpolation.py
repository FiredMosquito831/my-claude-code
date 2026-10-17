"""A settings file with no ``${`` skips python-dotenv's interpolation pass (7.97.0).

python-dotenv resolves ``${NAME}`` by copying all of ``os.environ`` once per
key, which was most of a cold settings save (#266: 208,300 ``os.environ``
reads for one first-start ``.env``). Only ``${NAME}`` and ``${NAME:-default}``
are ever rewritten, so a text without ``${`` is parsed with interpolation off.
These hold that the answer is the same mapping, in the same order, for every
text the dashboard parses, and that a text with ``${`` is interpolated as
before.
"""

from io import StringIO

import dotenv.main
import pytest
from dotenv import dotenv_values

from my_claude_code.config.admin import sources
from my_claude_code.config.env_template import (
    load_env_template_or_empty,
    render_default_env,
)


def _reference(text: str) -> list[tuple[str, str]]:
    """What the parse returned before 7.97.0: interpolation always on."""

    values = dotenv_values(stream=StringIO(text))
    return [(key, "" if value is None else value) for key, value in values.items()]


TEXTS = {
    "template": load_env_template_or_empty(),
    "first start": render_default_env(),
    "dollars without braces": (
        'A=$HOME\nB="cost $5 {not a var}"\nC=\'$PATH\'\nD="a\\"b"\nE=\nF\n'
        "G=multi\\nline\n"
    ),
}


@pytest.mark.parametrize("name", sorted(TEXTS))
def test_the_same_mapping_in_the_same_order(name: str) -> None:
    text = TEXTS[name]
    assert "${" not in text
    assert list(sources._parse_dotenv_text(text).items()) == _reference(text)


def test_a_text_without_a_brace_never_runs_the_interpolation_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    real = dotenv.main.resolve_variables

    def counting(values, override):
        calls.append(1)
        return real(values, override)

    monkeypatch.setattr(dotenv.main, "resolve_variables", counting)
    sources._parse_dotenv_text(TEXTS["template"])
    assert calls == []
    sources._parse_dotenv_text("A=${HOME}\n")
    assert calls == [1]


def test_a_text_with_a_brace_is_interpolated_exactly_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCC_TEST_DOTENV_VAR", "from-env")
    text = (
        "A=${MCC_TEST_DOTENV_VAR}\n"
        "B=${MCC_TEST_DOTENV_MISSING:-fallback}\n"
        "C=${A}-suffix\n"
        "D='${MCC_TEST_DOTENV_VAR}'\n"
        "E=plain\n"
    )
    parsed = sources._parse_dotenv_text(text)
    assert list(parsed.items()) == _reference(text)
    assert parsed["A"] == "from-env"
    assert parsed["B"] == "fallback"


def test_the_cached_readers_answer_through_it(tmp_path) -> None:
    sources.clear_env_parse_cache()
    path = tmp_path / ".env"
    raw = TEXTS["first start"].encode("utf-8")
    path.write_bytes(raw)
    assert list(sources.dotenv_values_from_bytes(path, raw).items()) == _reference(
        TEXTS["first start"]
    )
    assert list(
        sources.dotenv_values_from_text(TEXTS["template"]).items()
    ) == _reference(TEXTS["template"])
