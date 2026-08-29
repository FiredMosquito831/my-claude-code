"""The dashboard must never display a value nobody chose.

A select rendered ``field.value || field.options[0]?.value`` showed the first
option for a field nobody had ever set, while ``dataset.original`` stayed
empty -- so the control disagreed with itself, every Save counted it as an
edit, and the value was written into the managed .env. That is how installs
ended up with ``FALLBACK_BENCH_ENABLED=false`` after the release that made the
default true.

Static assertions on the shipped JavaScript, because they run on every
platform; the jsdom suite proves the behaviour where node and jsdom exist and
skips silently where they do not.
"""

from pathlib import Path

ADMIN_JS = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "my_claude_code"
    / "api"
    / "admin_static"
    / "admin.js"
)


def _script() -> str:
    return ADMIN_JS.read_text(encoding="utf-8")


def test_no_control_falls_back_to_its_first_option() -> None:
    assert "field.options[0]?.value" not in _script()


def test_the_unset_option_is_built_for_every_option_control() -> None:
    script = _script()
    assert "function selectWithDefaultOption(field, options) {" in script
    assert 'option("", `Default (${match ? match.label : fallback || "none"})`)' in (
        script
    )


def test_booleans_are_not_rendered_as_checkboxes() -> None:
    """A checkbox has two positions; a setting has three states."""
    script = _script()
    boolean_branch = script.split('if (field.type === "boolean") {', 1)[1].split(
        "\n  }\n", 1
    )[0]
    assert 'input.type = "checkbox"' not in boolean_branch
    assert "selectWithDefaultOption(field, [" in boolean_branch


def test_a_save_reports_what_it_could_not_do() -> None:
    assert "result.warnings || []" in _script()
