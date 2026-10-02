"""One INFO line per rail whose pause list actually changed (item 5).

INVESTIGATION-PAUSE-UNDER-LOAD.md fix F3 / decision 5: a pause click left no
record at all. ``ApplicationRuntime._log_pause_changes`` (wired in at the two
``_commit_admin_update`` call sites inside ``_apply_admin_config_prepared``,
~application.py:580-612) diffs every ``ROUTE_PAUSE_KEYS`` rail between the
settings before and after a commit, and logs one line per rail that actually
changed -- never one for a save that changed nothing.

The module's ``logger`` is stubbed with a ``MagicMock`` rather than added to
as a real loguru sink, because a real sink sees each record twice whenever an
earlier test in the same worker left stdlib-to-loguru interception installed
(``tests/runtime/test_listener_guard.py`` uses the same pattern).
"""

from unittest.mock import patch

import pytest

from my_claude_code.config.admin.persistence import PreparedAdminUpdate
from my_claude_code.config.settings import Settings
from my_claude_code.runtime import application as application_module
from my_claude_code.runtime.application import ApplicationRuntime
from my_claude_code.runtime.provider_manager import ProviderRuntimeManager


def _settings(**overrides: object) -> Settings:
    return Settings().model_copy(
        update={
            "model": "nvidia_nim/nvidia/model-a",
            "model_opus_fallbacks": "nvidia_nim/nvidia/model-a",
            "nvidia_api_key": "nvidia-test-key",
            "port": 8124,
            **overrides,
        }
    )


def _prepared(settings: Settings, tmp_path) -> PreparedAdminUpdate:
    return PreparedAdminUpdate(
        target_values={"MODEL_OPUS_PAUSED": settings.model_opus_paused or ""},
        settings=settings,
        errors=(),
        pending_fields=(),
        path=tmp_path / ".env",
    )


def _applied_response() -> dict[str, object]:
    return {
        "applied": True,
        "valid": True,
        "errors": [],
        "warnings": [],
        "env_preview": "MODEL_OPUS_PAUSED=updated\n",
        "path": ".env",
        "pending_fields": [],
    }


async def _apply(runtime, updates, prepared) -> None:
    with (
        patch(
            "my_claude_code.runtime.application.prepare_admin_update",
            return_value=prepared,
        ),
        patch(
            "my_claude_code.runtime.application.commit_prepared_admin_update",
            side_effect=lambda _prepared: _applied_response(),
        ),
    ):
        await runtime.apply_admin_config(updates)


def _pause_lines(stub) -> list:
    return [
        call for call in stub.info.call_args_list if call.args[0].startswith("PAUSE:")
    ]


@pytest.mark.asyncio
async def test_a_real_pause_logs_one_line_naming_the_rail(tmp_path) -> None:
    manager = ProviderRuntimeManager(_settings())
    runtime = ApplicationRuntime(manager, transcriber=None)

    with patch.object(application_module, "logger") as stub:
        await _apply(
            runtime,
            {"MODEL_OPUS_PAUSED": "nvidia_nim/nvidia/model-a"},
            _prepared(
                _settings(model_opus_paused="nvidia_nim/nvidia/model-a"), tmp_path
            ),
        )
    await manager.close()

    pause_calls = _pause_lines(stub)
    assert len(pause_calls) == 1, stub.info.call_args_list
    assert pause_calls[0].args[1] == "MODEL_OPUS"
    assert pause_calls[0].args[2] == ["nvidia_nim/nvidia/model-a"]  # added
    assert pause_calls[0].args[3] == []  # removed


@pytest.mark.asyncio
async def test_a_no_op_save_logs_nothing(tmp_path) -> None:
    """The byte-identical save (no pause change) must not log a PAUSE line."""
    manager = ProviderRuntimeManager(_settings())
    runtime = ApplicationRuntime(manager, transcriber=None)

    with patch.object(application_module, "logger") as stub:
        await _apply(
            runtime,
            {"MODEL_OPUS_PAUSED": ""},
            _prepared(_settings(model_opus_paused=None), tmp_path),
        )
    await manager.close()

    assert _pause_lines(stub) == []


@pytest.mark.asyncio
async def test_resume_logs_the_removal(tmp_path) -> None:
    """Going from paused to unpaused is also a change worth one line."""
    manager = ProviderRuntimeManager(
        _settings(model_opus_paused="nvidia_nim/nvidia/model-a")
    )
    runtime = ApplicationRuntime(manager, transcriber=None)

    with patch.object(application_module, "logger") as stub:
        await _apply(
            runtime,
            {"MODEL_OPUS_PAUSED": ""},
            _prepared(_settings(model_opus_paused=None), tmp_path),
        )
    await manager.close()

    pause_calls = _pause_lines(stub)
    assert len(pause_calls) == 1, stub.info.call_args_list
    assert pause_calls[0].args[2] == []  # added
    assert pause_calls[0].args[3] == ["nvidia_nim/nvidia/model-a"]  # removed
