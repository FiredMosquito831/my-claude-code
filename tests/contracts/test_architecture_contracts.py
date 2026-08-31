import re
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlsplit


def test_architecture_document_exists() -> None:
    repo_root = Path(__file__).resolve().parents[2]

    assert (repo_root / "ARCHITECTURE.md").is_file()


def test_architecture_document_relative_links_resolve() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    architecture = repo_root / "ARCHITECTURE.md"
    text = architecture.read_text(encoding="utf-8")

    missing: list[str] = []
    for match in re.finditer(r"(?<!!)\[[^\]]+\]\(([^)]+)\)", text):
        raw_target = match.group(1).strip()
        target = raw_target.split("#", 1)[0]
        if not target or urlsplit(target).scheme:
            continue
        if not (repo_root / unquote(target)).exists():
            missing.append(raw_target)

    assert missing == []


def test_root_env_example_is_the_single_template_source() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    root_example = repo_root / ".env.example"
    duplicate_example = repo_root / "src" / "my_claude_code" / "config" / "env.example"

    assert root_example.is_file()
    assert not duplicate_example.exists()


def test_root_env_example_is_packaged_for_config_template_loader() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text("utf-8"))

    force_include = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"][
        "force-include"
    ]

    assert force_include[".env.example"] == "my_claude_code/config/env.example"


def test_pyproject_first_party_packages_match_packaged_roots() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    pyproject = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r"known-first-party = \[(?P<items>[^\]]+)\]", pyproject)

    assert match is not None
    configured = {
        item.strip().strip('"')
        for item in match.group("items").split(",")
        if item.strip()
    }
    expected = {"my_claude_code", "smoke"}
    assert configured == expected


def test_the_shipped_template_agrees_with_the_retry_backoff_ceiling() -> None:
    """``.env.example`` is what an operator copies; it must not ship a longer ladder.

    ``constants.py``, the manifest, the README, ``docs/USAGE.md`` and
    ``tests/config/test_limit_bounds.py`` all said 10 while the template
    shipped 60 -- a six-times-longer ladder for anyone who started from the
    file the install instructions hand them. Nothing pinned the two together.
    """
    from my_claude_code.config.constants import (
        PROVIDER_RETRY_ATTEMPTS_DEFAULT,
        PROVIDER_RETRY_BACKOFF_BASE_SECONDS_DEFAULT,
        PROVIDER_RETRY_BACKOFF_JITTER_SECONDS_DEFAULT,
        PROVIDER_RETRY_BACKOFF_MAX_SECONDS_DEFAULT,
        RATE_LIMIT_ROUTES_AROUND_MODEL_DEFAULT,
    )

    repo_root = Path(__file__).resolve().parents[2]
    template = (repo_root / ".env.example").read_text(encoding="utf-8")
    shipped = dict(
        line.split("=", 1)
        for line in template.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )

    expected = {
        "PROVIDER_RETRY_BACKOFF_MAX_SECONDS": PROVIDER_RETRY_BACKOFF_MAX_SECONDS_DEFAULT,
        "PROVIDER_RETRY_BACKOFF_BASE_SECONDS": (
            PROVIDER_RETRY_BACKOFF_BASE_SECONDS_DEFAULT
        ),
        "PROVIDER_RETRY_BACKOFF_JITTER_SECONDS": (
            PROVIDER_RETRY_BACKOFF_JITTER_SECONDS_DEFAULT
        ),
        "PROVIDER_RETRY_ATTEMPTS": PROVIDER_RETRY_ATTEMPTS_DEFAULT,
    }
    for key, default in expected.items():
        assert key in shipped, f"{key} is missing from .env.example"
        assert float(shipped[key]) == float(default), (
            f"{key} ships {shipped[key]} in .env.example but the code uses {default}"
        )
    assert shipped["RATE_LIMIT_ROUTES_AROUND_MODEL"].strip().lower() == (
        "true" if RATE_LIMIT_ROUTES_AROUND_MODEL_DEFAULT else "false"
    )
