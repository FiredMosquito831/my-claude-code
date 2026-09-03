"""The OpenCode family is pointed at MCC without touching the user's config.

The whole point of this launcher is what it does *not* do. OpenCode, its v2
preview and Kilo all read provider configuration from files, and the obvious
implementation -- merge a provider block into
``~/.config/opencode/opencode.json`` -- would make MCC a co-owner of a document
the user wrote by hand. Each CLI publishes an environment variable naming an
extra config file instead, so MCC owns a file under ``~/.fcc`` and hands over
its path. These tests pin that, and pin the token staying out of the file.
"""

import json
from importlib import import_module
from pathlib import Path
from unittest.mock import patch

import pytest

from my_claude_code.cli.harnesses import catalogue_client
from my_claude_code.cli.harnesses.catalogue_client import (
    catalogue_defaulted,
    print_defaulted_summary,
)
from my_claude_code.cli.launchers.common import PROXY_PREFLIGHT_TIMEOUT_SECONDS
from my_claude_code.cli.launchers.opencode import (
    build_opencode_launcher_env,
    is_passthrough,
    messages_base_url,
    write_harness_config,
)
from my_claude_code.config.constants import CATALOGUE_FETCH_TIMEOUT_SECONDS
from my_claude_code.config.harnesses import (
    MCC_HARNESS_ID_SENTINEL,
    OPENCODE_API_KEY_ENV,
    OPENCODE_BASE_URL_ENV,
    harness_spec,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.client_fingerprint import HARNESS_HEADER

#: The generated document as the server writes it. Small on purpose: these
#: tests are about *whether* a fetch happens, not about the mapping, which
#: ``tests/application/test_opencode_serialiser.py`` owns.
_ON_DISK_DOCUMENT = {
    "$schema": "https://opencode.ai/config.json",
    "provider": {"mcc": {"npm": "@ai-sdk/anthropic", "models": {"a/b": {"name": "B"}}}},
}


class _JsonResponse:
    """The shape ``urlopen`` returns, as much of it as the client reads."""

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _JsonResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _fake_urlopen(timeouts: list[float]):
    """Return a ``urlopen`` double that records the budget it was given."""

    def fake(request: object, *, timeout: float) -> _JsonResponse:
        timeouts.append(timeout)
        return _JsonResponse(
            {
                "models": [],
                "catalogues": {
                    "opencode": {
                        "document": _ON_DISK_DOCUMENT,
                        "model_count": 1,
                        "defaulted": {},
                    }
                },
            }
        )

    return fake


def _opencode_settings(**overrides: object) -> Settings:
    return Settings().model_copy(
        update={"anthropic_auth_token": "proxy-token", **overrides}
    )


def test_each_harness_is_pointed_with_its_own_documented_variable() -> None:
    expected = {
        "opencode": "OPENCODE_CONFIG",
        "opencode2": "OPENCODE_CONFIG",
        "kilo": "KILO_CONFIG",
    }
    for harness_id, variable in expected.items():
        spec = harness_spec(harness_id)
        assert spec.catalogue is not None
        assert spec.catalogue.config_env_var == variable

        env = build_opencode_launcher_env(
            spec=spec,
            config_path=Path("/home/u/.fcc/generated.json"),
            proxy_root_url="http://127.0.0.1:8082/",
            auth_token="token",
            base_env={"PATH": "p"},
        )
        assert env[variable] == str(Path("/home/u/.fcc/generated.json"))


def test_the_launched_environment_carries_the_url_and_token_only() -> None:
    env = build_opencode_launcher_env(
        spec=harness_spec("opencode"),
        config_path=Path("cfg.json"),
        proxy_root_url="http://127.0.0.1:8082/",
        auth_token="token",
        base_env={"PATH": "p", OPENCODE_BASE_URL_ENV: "stale"},
    )

    assert env["PATH"] == "p"
    # A stale value inherited from a parent shell must not outrank the one
    # this launch resolved.
    assert env[OPENCODE_BASE_URL_ENV] == "http://127.0.0.1:8082/v1"
    assert env[OPENCODE_API_KEY_ENV] == "token"


def test_no_config_means_no_mcc_variables_at_all() -> None:
    """A failed catalogue fetch launches the CLI plain, not half-configured."""

    env = build_opencode_launcher_env(
        spec=harness_spec("opencode"),
        config_path=None,
        proxy_root_url="http://127.0.0.1:8082/",
        auth_token="token",
        base_env={"PATH": "p"},
    )

    assert env == {"PATH": "p"}


def test_the_base_url_is_what_the_anthropic_sdk_appends_messages_to() -> None:
    assert messages_base_url("http://127.0.0.1:8082") == "http://127.0.0.1:8082/v1"
    assert messages_base_url("http://127.0.0.1:8082/v1/") == "http://127.0.0.1:8082/v1"


def test_maintenance_commands_reach_the_cli_without_a_running_proxy() -> None:
    spec = harness_spec("opencode")

    assert is_passthrough(spec, ["upgrade"])
    assert is_passthrough(spec, ["--version"])
    assert not is_passthrough(spec, ["run", "hello"])
    assert not is_passthrough(spec, [])


def test_the_launch_summary_is_one_line_unless_verbose(capsys) -> None:
    """142 lines of stderr before the agent has said anything is not read.

    The user who reported the launch failure quoted six of those lines and
    could not tell that the seventh said the document had been refused. The
    default is now one line with the field histogram; the per-model detail is
    behind ``MCC_CATALOGUE_VERBOSE=1`` and has two other homes -- the
    generated file's own ``_mcc_defaulted`` block and the Coding agents card.
    """

    defaulted = {
        "custom_b_ai/glm-5.3-flash": ["limit.context", "cost"],
        "chatgpt_oauth/gpt-5.5": ["cost"],
    }

    print_defaulted_summary("OpenCode", defaulted, environ={})
    quiet = capsys.readouterr().err.strip().splitlines()

    assert len(quiet) == 1
    assert "2 model(s)" in quiet[0]
    assert "cost x2" in quiet[0]
    assert "limit.context x1" in quiet[0]
    assert "MCC_CATALOGUE_VERBOSE=1" in quiet[0]
    assert "custom_b_ai/glm-5.3-flash" not in quiet[0]

    print_defaulted_summary(
        "OpenCode", defaulted, environ={"MCC_CATALOGUE_VERBOSE": "1"}
    )
    verbose = capsys.readouterr().err.strip().splitlines()

    assert len(verbose) == 3
    assert verbose[1].strip().startswith("chatgpt_oauth/gpt-5.5:")
    assert verbose[2].strip().startswith("custom_b_ai/glm-5.3-flash:")


def test_nothing_is_printed_when_the_ladder_answered_everything(capsys) -> None:
    print_defaulted_summary("OpenCode", {}, environ={})

    assert capsys.readouterr().err == ""


def test_the_defaulted_record_is_read_from_the_payload_not_the_document() -> None:
    """Kilo's document cannot carry it, and its stderr must still report it."""

    payload = {
        "catalogues": {
            "kilo": {"document": {}, "defaulted": {"a/b": ["limit.context"]}},
        }
    }

    assert catalogue_defaulted(payload, "kilo") == {"a/b": ["limit.context"]}
    assert catalogue_defaulted(payload, "not_a_harness") == {}


# --------------------------------------------------------------- file-first


def test_a_launcher_uses_the_document_on_disk_without_fetching(tmp_path) -> None:
    """The steady state is zero HTTP: the server owns the file, the launcher reads it.

    Before this, every launch fetched a 1.41 MB payload in order to rewrite a
    73 KB document that had not changed, on a 1.5 s budget the route could not
    meet.
    """

    spec = harness_spec("opencode")
    config_path = tmp_path / "opencode-config.json"
    config_path.write_text(json.dumps(_ON_DISK_DOCUMENT), encoding="utf-8")

    with (
        patch(
            "my_claude_code.cli.launchers.opencode.harness_catalogue_path",
            return_value=config_path,
        ),
        patch("my_claude_code.cli.harnesses.catalogue_client.urlopen") as urlopen,
    ):
        result = write_harness_config(
            spec, "http://127.0.0.1:8082", _opencode_settings()
        )

    assert result == config_path
    urlopen.assert_not_called()
    # The file is handed over untouched -- a read is not a rewrite.
    assert json.loads(config_path.read_text(encoding="utf-8")) == _ON_DISK_DOCUMENT


def test_a_corrupt_document_on_disk_falls_back_to_a_fetch(tmp_path) -> None:
    """Half a JSON file is not a catalogue, and OpenCode would refuse it."""

    spec = harness_spec("opencode")
    config_path = tmp_path / "opencode-config.json"
    config_path.write_text('{"provider": ', encoding="utf-8")
    timeouts: list[float] = []

    with (
        patch(
            "my_claude_code.cli.launchers.opencode.harness_catalogue_path",
            return_value=config_path,
        ),
        patch(
            "my_claude_code.cli.harnesses.catalogue_client.urlopen",
            side_effect=_fake_urlopen(timeouts),
        ),
    ):
        result = write_harness_config(
            spec, "http://127.0.0.1:8082", _opencode_settings()
        )

    assert result == config_path
    assert len(timeouts) == 1
    assert json.loads(config_path.read_text(encoding="utf-8"))["provider"]["mcc"]


def test_a_missing_document_is_fetched_once_with_the_catalogue_timeout_not_the_preflight_one(
    tmp_path,
) -> None:
    """Exactly one GET, on this route's own budget.

    ``PROXY_PREFLIGHT_TIMEOUT_SECONDS`` is 1.5 s, sized for ``GET /health``.
    The route this fetches measured 1.8-4.0 s on a real install, so sharing the
    one constant made the fetch fail -- and because the launcher was the only
    thing that could create the file, it then failed identically forever.
    """

    spec = harness_spec("opencode")
    config_path = tmp_path / "opencode-config.json"
    timeouts: list[float] = []

    with (
        patch(
            "my_claude_code.cli.launchers.opencode.harness_catalogue_path",
            return_value=config_path,
        ),
        patch(
            "my_claude_code.cli.harnesses.catalogue_client.urlopen",
            side_effect=_fake_urlopen(timeouts),
        ),
    ):
        result = write_harness_config(
            spec, "http://127.0.0.1:8082", _opencode_settings()
        )

    assert result == config_path
    assert timeouts == [CATALOGUE_FETCH_TIMEOUT_SECONDS]
    assert CATALOGUE_FETCH_TIMEOUT_SECONDS != PROXY_PREFLIGHT_TIMEOUT_SECONDS
    assert config_path.exists()


def test_the_catalogue_budget_is_the_one_the_dashboard_saved(tmp_path) -> None:
    """The manifest field has to reach the socket, or the form is decoration."""

    spec = harness_spec("opencode")
    timeouts: list[float] = []
    settings = _opencode_settings(catalogue_fetch_timeout_seconds=45.0)

    with (
        patch(
            "my_claude_code.cli.launchers.opencode.harness_catalogue_path",
            return_value=tmp_path / "opencode-config.json",
        ),
        patch(
            "my_claude_code.cli.harnesses.catalogue_client.get_settings",
            return_value=settings,
        ),
        patch(
            "my_claude_code.cli.harnesses.catalogue_client.urlopen",
            side_effect=_fake_urlopen(timeouts),
        ),
    ):
        write_harness_config(spec, "http://127.0.0.1:8082", settings)

    assert timeouts == [45.0]


def test_the_catalogue_fetch_never_imports_the_health_check_budget() -> None:
    """A static guard: one module must not be able to reach both budgets.

    The name is where the bug lived -- nothing about
    ``PROXY_PREFLIGHT_TIMEOUT_SECONDS`` says it is also the budget for a
    multi-second serialisation of thirteen catalogues, and nothing stopped it
    being used as one.
    """

    source = Path(catalogue_client.__file__ or "").read_text(encoding="utf-8")
    imports = [
        line for line in source.splitlines() if line.startswith(("import ", "from "))
    ]

    assert imports
    assert not [line for line in imports if "PROXY_PREFLIGHT_TIMEOUT_SECONDS" in line]


def test_a_slow_server_never_yields_a_config_less_launch_silently(
    tmp_path, capsys
) -> None:
    """The warning has to be actionable; its absence is what let this ship.

    The user's report was one line -- ``could not prepare the OpenCode config
    (timed out); launching without an MCC provider`` -- which named the symptom
    and nothing else: not the file that would have fixed it, not the request
    that failed, and not that it would never recover on its own.
    """

    spec = harness_spec("opencode")
    config_path = tmp_path / "opencode-config.json"

    with (
        patch(
            "my_claude_code.cli.launchers.opencode.harness_catalogue_path",
            return_value=config_path,
        ),
        patch(
            "my_claude_code.cli.harnesses.catalogue_client.urlopen",
            side_effect=TimeoutError("timed out"),
        ),
    ):
        result = write_harness_config(
            spec, "http://127.0.0.1:8082", _opencode_settings()
        )

    assert result is None
    err = capsys.readouterr().err
    assert str(config_path) in err
    assert "timed out" in err
    assert "/admin/api/catalogue-models" in err
    assert "CATALOGUE_FETCH_TIMEOUT_SECONDS" in err
    assert "mcc-server" in err
    assert "mcc-opencode" in err
    assert "Coding agents" in err


def test_every_catalogue_owning_launcher_reads_its_document_first() -> None:
    """One harness fixed and nine left fetching is the same bug, renamed."""

    for name in (
        "opencode",
        "codex",
        "kimi",
        "qwen",
        "crush",
        "cline",
        "aider",
        "droid",
        "gemini",
    ):
        module = import_module(f"my_claude_code.cli.launchers.{name}")
        source = Path(module.__file__ or "").read_text(encoding="utf-8")
        assert "catalogue_documents" in source, name
        assert "read_document(" in source or "document_on_disk(" in source, name


# -------------------------------------------------------- harness attribution


#: The document as the *server* hands it over: the harness-id sentinel is
#: still in it, because the serialiser that wrote it is shared by all three of
#: these harnesses and cannot tell which one asked.
_SENTINEL_DOCUMENT = {
    "$schema": "https://opencode.ai/config.json",
    "provider": {
        "mcc": {
            "npm": "@ai-sdk/anthropic",
            "options": {"headers": {HARNESS_HEADER: MCC_HARNESS_ID_SENTINEL}},
            "models": {"a/b": {"name": "B"}},
        }
    },
}


def _sentinel_urlopen(harness_id: str):
    def fake(request: object, *, timeout: float) -> _JsonResponse:
        return _JsonResponse(
            {
                "models": [],
                "catalogues": {
                    harness_id: {
                        "document": _SENTINEL_DOCUMENT,
                        "model_count": 1,
                        "defaulted": {},
                    }
                },
            }
        )

    return fake


@pytest.mark.parametrize("harness_id", ["opencode", "opencode2", "kilo"])
def test_each_family_member_writes_its_own_harness_id(
    harness_id: str, tmp_path: Path
) -> None:
    """One serialiser, three files, three different ids -- and no sentinel left.

    Without this the request log cannot tell an OpenCode session from a Kilo
    one: all three send the same user-agent, so the header is the only signal
    that separates them, and it is the one thing a shared pure serialiser
    cannot supply.
    """

    spec = harness_spec(harness_id)
    config_path = tmp_path / f"{harness_id}-config.json"

    with (
        patch(
            "my_claude_code.cli.launchers.opencode.harness_catalogue_path",
            return_value=config_path,
        ),
        patch(
            "my_claude_code.cli.harnesses.catalogue_client.urlopen",
            _sentinel_urlopen(harness_id),
        ),
    ):
        result = write_harness_config(
            spec, "http://127.0.0.1:8082", _opencode_settings()
        )

    assert result == config_path
    text = config_path.read_text(encoding="utf-8")
    assert MCC_HARNESS_ID_SENTINEL not in text
    assert "{{" not in text
    document = json.loads(text)
    assert document["provider"]["mcc"]["options"]["headers"] == {
        HARNESS_HEADER: harness_id
    }
