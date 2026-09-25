"""A setting the form can edit must reach the code that acts on it.

Each of these knobs replaced a hardcoded literal. A literal that becomes a
`Settings` field and a manifest entry but never reaches its consumer is worse
than the literal was: the dashboard shows a number, saving it restarts the
server, and nothing changes. One test per hop, from `Settings` to the object
that reads the value.
"""

from unittest.mock import patch

import pytest

from my_claude_code.application.execution import route_execution_policy
from my_claude_code.cli.harnesses.catalogue_client import fetch_catalogue_models
from my_claude_code.config.limits import range_for
from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
from my_claude_code.config.settings import (
    Settings,
    parse_effort_budget_ratios,
    parse_lockout_tiers,
)
from my_claude_code.core.credential_rotation import PROVIDER_TUNING
from my_claude_code.core.failures import FailureKind
from my_claude_code.core.rate_limit import (
    MAX_HOST_STATED_COOLDOWN_SECONDS,
    MAX_RATE_LIMIT_COOLDOWN_SECONDS,
)
from my_claude_code.providers.nvidia_nim import NvidiaNimProvider
from my_claude_code.providers.runtime.config import build_provider_config
from my_claude_code.providers.runtime.factory import create_provider
from my_claude_code.providers.runtime.rotating import RotatingProvider


def _settings(**overrides) -> Settings:
    return Settings(**overrides)


def _fixed(settings: Settings):
    """A stand-in for the cached accessor the provider layer calls."""

    def read() -> Settings:
        return settings

    return read


def test_the_tier_alias_switch_reaches_the_catalogue_builder() -> None:
    """HARNESS_TIER_ALIASES is the one new field, and it has one consumer.

    A master switch that becomes a field and a manifest entry but never reaches
    the builder is worse than no switch: the dashboard offers to shorten every
    coding agent's picker, saving it says it applied, and thirteen pickers stay
    exactly as long as they were.
    """

    from my_claude_code.application.catalogue_model import build_catalogue_models
    from my_claude_code.application.model_metadata import ProviderModelInfo
    from tests.application.test_catalogue_model import FakeRuntime

    def refs(enabled: bool) -> set[str]:
        settings = _settings(model="nvidia_nim/one", HARNESS_TIER_ALIASES=enabled)
        runtime = FakeRuntime(
            settings=settings,
            cached_infos=(ProviderModelInfo("nvidia_nim/one"),),
        )
        return {
            model.provider_model_ref
            for model in build_catalogue_models(settings, runtime)
        }

    assert "mcc/best" in refs(True)
    assert "mcc/best" not in refs(False)


def test_the_backoff_and_lockout_settings_reach_the_provider_config() -> None:
    config = build_provider_config(
        PROVIDER_CATALOG["nvidia_nim"],
        _settings(
            nvidia_nim_api_key="k1",
            PROVIDER_RETRY_BACKOFF_BASE_SECONDS=3.5,
            PROVIDER_RETRY_BACKOFF_MAX_SECONDS=99.0,
            PROVIDER_RETRY_BACKOFF_JITTER_SECONDS=0.25,
            CREDENTIAL_LOCKOUT_TIERS="1,2,3",
        ),
    )

    assert config.retry_backoff_base_seconds == 3.5
    assert config.retry_backoff_max_seconds == 99.0
    assert config.retry_backoff_jitter_seconds == 0.25
    assert config.lockout_tiers == (1.0, 2.0, 3.0)


def test_the_backoff_settings_reach_the_limiter_that_schedules_retries() -> None:
    provider = create_provider(
        "nvidia_nim",
        _settings(
            nvidia_nim_api_key="k1",
            PROVIDER_RETRY_BACKOFF_BASE_SECONDS=3.5,
            PROVIDER_RETRY_BACKOFF_MAX_SECONDS=99.0,
            PROVIDER_RETRY_BACKOFF_JITTER_SECONDS=0.25,
        ),
    )

    assert isinstance(provider, NvidiaNimProvider)
    limiter = provider._rate_limiter
    assert limiter._backoff_base_seconds == 3.5
    assert limiter._backoff_max_seconds == 99.0
    assert limiter._backoff_jitter_seconds == 0.25


@pytest.mark.asyncio
async def test_the_lockout_ladder_and_429_window_reach_the_credential_pool() -> None:
    provider = create_provider(
        "nvidia_nim",
        _settings(
            nvidia_nim_api_key="k1,k2",
            NVIDIA_NIM_API_KEY_ROTATION="round_robin",
            CREDENTIAL_LOCKOUT_TIERS="11,22",
            RATE_LIMIT_COOLDOWN_SECONDS=44.0,
        ),
    )

    assert isinstance(provider, RotatingProvider)
    tuning = provider._state._engine.tuning
    assert tuning.lockout_tiers == (11.0, 22.0)
    assert tuning.rate_limit_seconds == 44.0


def test_the_step_over_floor_reaches_the_route_policy() -> None:
    policy = route_execution_policy(_settings(FALLBACK_COOLDOWN_STEP_OVER_FLOOR=12.0))
    assert policy.cooldown_step_over_floor == 12.0


def test_the_attempt_share_floor_reaches_the_route_policy() -> None:
    """The one hop between the operator's number and the code that divides.

    A setting nothing reads looks identical to a setting that works, right up
    until a silent model is cut at 75s again.
    """
    policy = route_execution_policy(_settings(FALLBACK_ATTEMPT_SHARE_FLOOR=210.0))
    assert policy.attempt_share_floor == 210.0

    # Ships 3600 since 6.68.0. Still nothing to floor out of the box --
    # FALLBACK_TOTAL_TIMEOUT ships at 0, so there is no budget to divide --
    # but the number an operator inherits when they do set a budget is an
    # hour-wide floor rather than an equal split.
    assert route_execution_policy(_settings()).attempt_share_floor == 3600.0
    assert (
        route_execution_policy(
            _settings(FALLBACK_ATTEMPT_SHARE_FLOOR=0.0)
        ).attempt_share_floor
        == 0.0
    )


def test_the_429_bench_is_capped_by_the_bound_that_admits_a_stated_reset() -> None:
    """A hostile number cannot bench a key past a day, and no longer past an hour.

    Until 7.6.3 this pinned the pool's cap to the *header* bound, because a
    header was the only place MCC read a reset from. A host that publishes its
    reset in JSON instead -- the OpenCode free tier states the seconds to the
    next UTC midnight -- now reaches the pool with a value already capped at
    :data:`MAX_HOST_STATED_COOLDOWN_SECONDS` by the reader, and an hour here
    would clamp it back one line later and re-create the defect.

    The two bounds still both apply, in the place each belongs: a header is
    capped at an hour where it is parsed, a body-stated reset at a day. The
    pool's job is only to refuse a number that got past both.
    """
    assert PROVIDER_TUNING.rate_limit_max_seconds == MAX_HOST_STATED_COOLDOWN_SECONDS
    assert MAX_RATE_LIMIT_COOLDOWN_SECONDS < MAX_HOST_STATED_COOLDOWN_SECONDS


def test_a_removed_env_key_is_ignored_rather_than_fatal() -> None:
    """An existing ``.env`` still carrying CREDENTIAL_CIRCUIT_THRESHOLD must start.

    The breaker it configured is gone. ``Settings`` is declared with
    ``extra="ignore"``, so the stale line is inert -- but the whole point of
    removing a key is that nobody has to edit a file for the upgrade, so this
    is pinned rather than assumed.
    """
    with patch.dict("os.environ", {"CREDENTIAL_CIRCUIT_THRESHOLD": "3"}, clear=False):
        settings = _settings()
    assert not hasattr(settings, "credential_circuit_threshold")


@pytest.mark.parametrize("value", ["", "0", "-5", "abc", "300,nope", "300,0"])
def test_a_lockout_ladder_that_cannot_be_walked_is_refused(value: str) -> None:
    with pytest.raises(ValueError):
        parse_lockout_tiers(value)


def test_a_bad_lockout_ladder_is_refused_at_load() -> None:
    with pytest.raises(ValueError, match="CREDENTIAL_LOCKOUT_TIERS"):
        _settings(CREDENTIAL_LOCKOUT_TIERS="300,nope")


def test_quota_is_a_skip_kind_an_operator_can_choose_but_never_inherits() -> None:
    """The new kind round-trips settings -> policy, and is absent by default.

    An account out of credits ends nothing by default: the next key, then the
    next model, is exactly what a chain is for. An operator who wants the old
    abort can still ask for it, and the validator has to accept the name.
    """
    default = route_execution_policy(_settings())
    assert FailureKind.QUOTA not in default.skip_kinds

    chosen = route_execution_policy(_settings(FALLBACK_SKIP_KINDS="quota"))
    assert chosen.skip_kinds == frozenset({FailureKind.QUOTA})


def test_the_catalogue_fetch_budget_reaches_the_launcher_socket() -> None:
    """The dashboard number has to be the number urlopen is handed.

    This one is easy to get wrong because the consumer is not the server: the
    launchers run as their own processes, so nothing about a running server
    proves the field arrives. It shipped as a hardcoded 1.5 s borrowed from the
    health preflight, which failed every OpenCode launch on a real install.
    """

    settings = _settings(CATALOGUE_FETCH_TIMEOUT_SECONDS=37.5)
    timeouts: list[float] = []

    def fake_urlopen(request, *, timeout: float):
        timeouts.append(timeout)
        raise TimeoutError("stop here; the budget is the whole claim")

    with (
        patch(
            "my_claude_code.cli.harnesses.catalogue_client.get_settings",
            return_value=settings,
        ),
        patch(
            "my_claude_code.cli.harnesses.catalogue_client.urlopen",
            side_effect=fake_urlopen,
        ),
        pytest.raises(TimeoutError),
    ):
        fetch_catalogue_models("http://127.0.0.1:8082", "token")

    assert timeouts == [37.5]


def test_the_catalogue_fetch_budget_is_clamped_to_a_usable_floor() -> None:
    """``urlopen(timeout=0)`` fails before it connects, so 0 must be unreachable.

    Every other deadline in this product treats 0 as "no limit". This one
    cannot: a 0 here would mean "never build a missing catalogue", which is the
    deadlock the setting exists to end. The range's floor is what stops it.
    """

    bounds = range_for("catalogue_fetch_timeout_seconds")
    assert bounds is not None
    assert bounds.minimum > 0
    assert (
        _settings(CATALOGUE_FETCH_TIMEOUT_SECONDS=0).catalogue_fetch_timeout_seconds
        == bounds.minimum
    )


# --------------------------------------------------------------------------- #
# ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS (6.47.0)
# --------------------------------------------------------------------------- #


def _profile_body(**overrides):
    """Build one OpenAI-dialect body through a profile that carries a default."""

    from my_claude_code.core.anthropic.models import Message, MessagesRequest
    from my_claude_code.core.reasoning import ReasoningPolicy
    from my_claude_code.providers.openai_chat.profiles import OPENAI_CHAT_PROFILES
    from my_claude_code.providers.openai_chat.request_policy import (
        build_openai_chat_request_body,
    )

    profile = OPENAI_CHAT_PROFILES["kimi"]
    assert profile.request_policy.default_max_tokens is not None
    request = MessagesRequest(
        model="kimi-k2",
        messages=[Message(role="user", content="hi")],
    )
    with patch.dict("os.environ", overrides, clear=False):
        from my_claude_code.config.settings import get_settings

        get_settings.cache_clear()
        try:
            return build_openai_chat_request_body(
                request,
                reasoning=ReasoningPolicy.off(),
                policy=profile.request_policy,
            )
        finally:
            get_settings.cache_clear()


def test_the_last_resort_output_default_reaches_a_provider_profile() -> None:
    """The 81,920 in ~25 profile literals is now one operator-settable number.

    Read per request rather than captured when the profile table is built, so
    a dashboard save takes effect without a restart. This is the hop that
    proves it: the profile still says "I want a last resort", and the number it
    gets comes from Settings.
    """

    assert (
        _profile_body(ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS="4096")["max_tokens"] == 4096
    )


def test_a_zero_last_resort_output_default_sends_no_max_tokens() -> None:
    assert "max_tokens" not in _profile_body(ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS="0")


def test_the_shipped_last_resort_output_default_is_unchanged() -> None:
    from my_claude_code.config.constants import ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS

    assert _settings(_env_file=None).anthropic_default_max_output_tokens == (
        ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS
    )


# --------------------------------------------------------------------------- #
# REASONING_EFFORT_BUDGET_RATIOS (6.47.0)
# --------------------------------------------------------------------------- #


def test_the_shipped_ratios_are_todays_table_element_for_element() -> None:
    """The setting's default has to be exactly what the code used before it.

    Asserted elementwise rather than as a string so a reordering shows up as
    the effort it belongs to.
    """

    from my_claude_code.application.reasoning_budget import EFFORT_BUDGET_RATIOS
    from my_claude_code.core.reasoning import ReasoningEffort

    shipped = parse_effort_budget_ratios(
        _settings(_env_file=None).reasoning_effort_budget_ratios
    )
    assert len(shipped) == len(tuple(ReasoningEffort))
    for effort, ratio in zip(tuple(ReasoningEffort), shipped, strict=True):
        assert EFFORT_BUDGET_RATIOS[effort] == pytest.approx(ratio)


def test_the_operators_ratios_reach_the_budget_arithmetic() -> None:
    """One knob, one consumer: budget_for_effort's only dereference."""

    from my_claude_code.application.reasoning_budget import (
        budget_for_effort,
        effort_budget_ratios,
    )
    from my_claude_code.config.settings import get_settings
    from my_claude_code.core.reasoning import ReasoningEffort

    with patch.dict(
        "os.environ",
        {"REASONING_EFFORT_BUDGET_RATIOS": "0.10,0.10,0.10,0.10,0.10,0.10"},
        clear=False,
    ):
        get_settings.cache_clear()
        try:
            assert effort_budget_ratios()[ReasoningEffort.HIGH] == pytest.approx(0.10)
            # 100,000 * 0.10, well clear of both the answer floor and the
            # 1,024-token minimum, so the ratio is what decides.
            assert budget_for_effort(ReasoningEffort.HIGH, 100_000) == 10_000
        finally:
            get_settings.cache_clear()


@pytest.mark.parametrize(
    "value",
    [
        "0.10,0.20,0.50,0.80,0.95",
        "0.10,0.20,0.50,0.80,0.95,0.95,0.95",
        "0,0.20,0.50,0.80,0.95,0.95",
        "0.10,0.20,0.50,0.80,0.95,1",
        "0.10,0.20,0.50,0.80,0.95,1.5",
        "0.10,0.20,0.50,0.80,0.95,-0.1",
        "0.10,0.20,0.50,0.80,0.95,nope",
    ],
)
def test_a_ratio_ladder_that_cannot_be_walked_is_refused(value: str) -> None:
    with pytest.raises(ValueError):
        parse_effort_budget_ratios(value)


def test_a_decreasing_ratio_ladder_names_the_offending_pair() -> None:
    """effort_for_budget inverts the table, so order is arithmetic, not taste."""

    with pytest.raises(ValueError, match=r"0\.8 is followed by 0\.3"):
        parse_effort_budget_ratios("0.10,0.20,0.50,0.80,0.30,0.95")


def test_a_bad_ratio_ladder_is_refused_at_load() -> None:
    with pytest.raises(ValueError, match="REASONING_EFFORT_BUDGET_RATIOS"):
        _settings(_env_file=None, REASONING_EFFORT_BUDGET_RATIOS="0.5,0.5")


def test_a_cleared_ratio_ladder_restores_the_shipped_default() -> None:
    """``text`` fields are blank-tolerant on the admin write path, so blank has
    to be a value the server can start on."""

    from my_claude_code.config.constants import REASONING_EFFORT_BUDGET_RATIOS_DEFAULT

    resolved = _settings(_env_file=None, REASONING_EFFORT_BUDGET_RATIOS="")
    assert resolved.reasoning_effort_budget_ratios == (
        REASONING_EFFORT_BUDGET_RATIOS_DEFAULT
    )


# --------------------------------------------------------------------------- #
# MAX_OUTPUT_TOKENS_FLOOR above MAX_OUTPUT_TOKENS_CEILING (6.47.0)
# --------------------------------------------------------------------------- #


def test_a_floor_above_the_ceiling_warns_and_does_not_refuse_to_start(caplog) -> None:
    """A number typed on the dashboard must never become an outage.

    The ceiling is applied after the floor and lowers unconditionally, so the
    contradiction resolves itself; what it cannot do is explain itself, which
    is what the warning is for.
    """

    with caplog.at_level("WARNING"):
        resolved = _settings(
            _env_file=None,
            MAX_OUTPUT_TOKENS_FLOOR="65536",
            MAX_OUTPUT_TOKENS_CEILING="8192",
        )
    assert resolved.max_output_tokens_floor == 65_536
    assert resolved.max_output_tokens_ceiling == 8_192
    assert "MAX_OUTPUT_TOKENS_FLOOR" in caplog.text
    assert "inert" in caplog.text


def test_a_floor_under_the_ceiling_says_nothing(caplog) -> None:
    with caplog.at_level("WARNING"):
        _settings(_env_file=None, MAX_OUTPUT_TOKENS_FLOOR="8192")
    assert "inert" not in caplog.text


def test_a_lifted_ceiling_leaves_the_floor_alone(caplog) -> None:
    """0 on the ceiling is "no ceiling", which no floor can contradict."""

    with caplog.at_level("WARNING"):
        resolved = _settings(
            _env_file=None,
            MAX_OUTPUT_TOKENS_FLOOR="65536",
            MAX_OUTPUT_TOKENS_CEILING="0",
        )
    assert resolved.max_output_tokens_ceiling is None
    assert "inert" not in caplog.text


def test_the_opencode_client_identity_reaches_the_outbound_header_set() -> None:
    """The switch on the OpenCode card must change what leaves the process.

    Asserted through the settings accessor the provider layer actually calls,
    not by reading the field back: a field that validates and is never read is
    the failure mode this file exists to catch.
    """
    from my_claude_code.config import settings as settings_module
    from my_claude_code.providers.openai_chat.opencode_identity import (
        opencode_constant_headers,
    )

    for choice, expected in (("opencode", "opencode/"), ("mcc", "my-claude-code/")):
        chosen = _settings(OPENCODE_CLIENT_IDENTITY=choice)
        original = settings_module.get_settings
        settings_module.get_settings = _fixed(chosen)
        try:
            headers = opencode_constant_headers()
        finally:
            settings_module.get_settings = original
            settings_module.get_settings.cache_clear()
        assert headers["User-Agent"].startswith(expected)


def test_the_opencode_client_version_pin_reaches_the_user_agent() -> None:
    from my_claude_code.config import settings as settings_module
    from my_claude_code.providers.openai_chat.opencode_identity import (
        opencode_client_version,
    )

    chosen = _settings(OPENCODE_CLIENT_VERSION="9.9.9")
    original = settings_module.get_settings
    settings_module.get_settings = _fixed(chosen)
    try:
        assert opencode_client_version() == ("9.9.9", "operator")
    finally:
        settings_module.get_settings = original
        settings_module.get_settings.cache_clear()


def _run_survey(action: str) -> tuple[list[float], list[object]]:
    """Run the supervisor's start-time survey under one action, capturing it.

    A function rather than a loop body so each closure binds its own lists;
    ruff's B023 is right that a closure over a loop variable is a trap.
    """

    from my_claude_code.cli import commands

    settings = _settings(
        SERVER_STALE_SERVER_ACTION=action,
        SERVER_STALE_SESSION_SECONDS=1234.0,
    )
    budgets: list[float] = []
    stopped: list[object] = []

    def fake_observe(**kwargs):
        budgets.append(kwargs["stale_after_seconds"])
        return []

    def fake_stop(observations, **_kwargs):
        stopped.append(observations)
        return []

    with (
        patch.object(commands, "observe_servers", fake_observe),
        patch.object(commands, "stop_stale_servers", fake_stop),
        patch.object(commands, "report_servers", lambda *a, **k: None),
        patch.object(commands, "write_survey", lambda *a, **k: None),
        patch.object(commands, "threading") as threads,
    ):
        commands._survey_other_servers(settings)
        # The survey runs on a daemon thread; call the target directly so the
        # assertion is about the work, not about thread scheduling.
        threads.Thread.assert_called_once()
        assert threads.Thread.call_args.kwargs["daemon"] is True
        threads.Thread.call_args.kwargs["target"]()
    return budgets, stopped


def test_the_stale_session_budget_reaches_the_start_time_survey() -> None:
    """The dashboard number has to be the number the classifier is handed."""

    budgets, _ = _run_survey("report")
    assert budgets == [1234.0]


def test_report_is_the_default_and_stops_nothing() -> None:
    """The binding rule of 2026-09-11: MCC does not stop somebody's server.

    The consumer is a background thread in the supervisor, so nothing about a
    running server proves the field arrives -- and the cost of it not arriving
    is a server stopped when the operator never asked for one to be.
    """

    from my_claude_code.config.constants import SERVER_STALE_SERVER_ACTION_DEFAULT

    assert SERVER_STALE_SERVER_ACTION_DEFAULT == "report"
    _, stopped = _run_survey("report")
    assert stopped == []


def test_stop_is_what_turns_the_sweep_on() -> None:
    _, stopped = _run_survey("stop")
    assert len(stopped) == 1


def test_the_proxy_switch_bound_reaches_the_pool_that_spends_it(
    tmp_path, monkeypatch
) -> None:
    """A ceiling that stopped at the manifest would be worse than a literal.

    The dashboard would offer to bound how far one request walks a proxy chain,
    saving it would restart the server, and every chain would keep spending
    whatever its own card said. This is the hop that was missing in the release
    that shipped the page: a ``Settings`` field with no consumer, which is why
    the setting could not ship until there was a runtime to read it.
    """

    from my_claude_code.config.proxy_chains import (
        ProxyChain,
        ProxyChainEntry,
        ProxyChains,
        ProxyEndpoint,
        reset_proxy_chains_cache,
        save_proxy_chains,
    )
    from my_claude_code.providers.runtime.proxy_rotating import ProxyRotatingProvider

    path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(
        "my_claude_code.config.proxy_chains.proxy_chains_path", lambda: path
    )
    reset_proxy_chains_cache()
    save_proxy_chains(
        ProxyChains(
            proxies={
                "px_a": ProxyEndpoint(url="http://198.51.100.9:8080"),
                "px_b": ProxyEndpoint(url="http://198.51.100.10:8080"),
            },
            chains={
                "nvidia_nim": ProxyChain(
                    enabled=True,
                    entries=(
                        ProxyChainEntry(proxy="px_a"),
                        ProxyChainEntry(proxy="px_b"),
                        ProxyChainEntry(proxy=""),
                    ),
                    # The card asks for the maximum the range allows.
                    max_switches=5,
                )
            },
        ),
        path,
    )
    try:
        provider = create_provider(
            "nvidia_nim",
            Settings.model_validate(
                {"nvidia_nim_api_key": "k1", "PROXY_MAX_SWITCHES_PER_REQUEST": 1}
            ),
        )
        assert isinstance(provider, ProxyRotatingProvider)
        assert provider._max_switches == 1
    finally:
        reset_proxy_chains_cache()


# ---------------------------------------------------------------------------
# 7.24.0: six literals became settings. Each pair below is the promise that
# nothing moved -- the setting's shipped default IS the number the code used
# before it was settable -- read from the module that still holds that number
# rather than restated here, so the two cannot drift.
# ---------------------------------------------------------------------------


def test_the_promoted_proxy_knobs_default_to_what_the_code_already_used() -> None:
    from my_claude_code.api import admin_proxy_routes
    from my_claude_code.application import proxy_check, proxy_fetch, proxy_ingest

    settings = Settings.model_validate({})
    assert settings.proxy_check_timeout_seconds == (
        proxy_check.PROXY_CHECK_TIMEOUT_SECONDS
    )
    assert settings.proxy_check_max_concurrency == (
        proxy_check.PROXY_CHECK_MAX_CONCURRENCY
    )
    assert settings.proxy_feed_timeout_seconds == proxy_ingest.FEED_TIMEOUT_SECONDS
    assert settings.proxy_feed_max == admin_proxy_routes.PROXY_FEED_MAX
    assert settings.proxy_candidate_bulk_max == (
        admin_proxy_routes.PROXY_CANDIDATE_BULK_MAX
    )
    assert settings.proxy_fetch_persist_interval_seconds == (
        proxy_fetch.FETCH_PERSIST_INTERVAL_SECONDS
    )


def test_the_promoted_proxy_knobs_still_ship_the_7_23_0_literals() -> None:
    """The literal, written out once, so a change of default cannot be silent.

    The test above pins setting-to-module; this one pins module-to-history. A
    single edit that moved both would pass the first and fail this.
    """

    settings = Settings.model_validate({})
    assert settings.proxy_check_timeout_seconds == 10.0
    assert settings.proxy_check_max_concurrency == 4
    assert settings.proxy_feed_timeout_seconds == 15.0
    assert settings.proxy_feed_max == 20
    assert settings.proxy_candidate_bulk_max == 100
    assert settings.proxy_fetch_persist_interval_seconds == 5.0


def test_the_fetch_persist_interval_reaches_the_sweep() -> None:
    """The setting has to arrive at the loop that decides when to write."""

    import inspect

    from my_claude_code.application.proxy_fetch import run_fetch_pass, start_fetch

    for func in (run_fetch_pass, start_fetch):
        params = inspect.signature(func).parameters
        assert "persist_interval" in params, f"{func.__name__} cannot be told"
        assert "feed_timeout" in params, f"{func.__name__} cannot be told"

    source = inspect.getsource(run_fetch_pass)
    assert "waited >= persist_interval" in source, (
        "run_fetch_pass reads the module constant again instead of the value "
        "it was handed, so the setting would be a number that does nothing"
    )


def test_the_proxy_ladder_and_cooldown_pair_reach_the_engine() -> None:
    """7.32.0's three, plumbed the way 7.22.0 plumbed the credential pool.

    ``core`` may not import ``config``, and ``providers/runtime/proxy_rotating``
    -- which holds the imported reference to ``PROXY_TUNING`` and hands it to
    every engine it builds -- is invariant for this release. So the policy is
    built where ``Settings`` is and passed in, and the engine's own object is
    what moves. A field that never reached it would be a form that offers a
    ladder and benches on the old one.
    """

    from my_claude_code.core.proxy_rotation import (
        PROXY_COOLDOWN_MAX_SECONDS,
        PROXY_COOLDOWN_SECONDS_DEFAULT,
        PROXY_REACHABILITY,
        PROXY_REACHABILITY_TIERS,
        PROXY_TUNING,
        configure_proxy_rotation,
    )

    settings = _settings(
        PROXY_COOLDOWN_SECONDS=11.0,
        PROXY_COOLDOWN_MAX_SECONDS=22.0,
        PROXY_REACHABILITY_TIERS="5,10,20",
    )
    try:
        configure_proxy_rotation(
            cooldown_seconds=settings.proxy_cooldown_seconds,
            cooldown_max_seconds=settings.proxy_cooldown_max_seconds,
            reachability_tiers=parse_lockout_tiers(settings.proxy_reachability_tiers),
        )
        assert PROXY_TUNING.rate_limit_seconds == 11.0
        assert PROXY_TUNING.rate_limit_max_seconds == 22.0
        assert PROXY_TUNING.lockout_tiers == (5.0, 10.0, 20.0)
        assert PROXY_REACHABILITY.tiers == (5.0, 10.0, 20.0)
    finally:
        configure_proxy_rotation()
    assert PROXY_TUNING.rate_limit_seconds == PROXY_COOLDOWN_SECONDS_DEFAULT
    assert PROXY_TUNING.rate_limit_max_seconds == PROXY_COOLDOWN_MAX_SECONDS
    assert PROXY_REACHABILITY.tiers == PROXY_REACHABILITY_TIERS


def test_the_startup_path_is_what_calls_configure_proxy_rotation() -> None:
    """A function nobody calls is a setting that does nothing.

    Pinned on the source rather than by running startup: the call sits inside
    the readiness sequence, and its *ordering* is the load-bearing part -- the
    ladder has to be the operator's before the durable bench store is re-armed
    against it.
    """

    import inspect

    from my_claude_code.runtime import application

    source = inspect.getsource(application)
    assert "configure_proxy_rotation(" in source
    assert source.index("configure_proxy_rotation(\n") < source.index(
        "arm_health_from_store)"
    )


def test_the_catalogue_clocks_reach_their_readers() -> None:
    """Each accessor asks ``Settings`` rather than its module constant."""

    from my_claude_code.providers.runtime import litellm_prices, models_dev

    settings = _settings(
        MODELS_DEV_CACHE_TTL_SECONDS=111,
        MODELS_DEV_FETCH_TIMEOUT_SECONDS=12.0,
        LITELLM_CACHE_TTL_SECONDS=222,
        LITELLM_FETCH_TIMEOUT_SECONDS=13.0,
    )
    with patch.object(models_dev, "get_settings", _fixed(settings)):
        assert models_dev.models_dev_cache_ttl_seconds() == 111.0
        assert models_dev.models_dev_fetch_timeout_seconds() == 12.0
    with patch.object(litellm_prices, "get_settings", _fixed(settings)):
        assert litellm_prices.litellm_cache_ttl_seconds() == 222.0
        assert litellm_prices.litellm_fetch_timeout_seconds() == 13.0


def test_the_learned_fact_clocks_reach_the_store_per_fact() -> None:
    """Hot, not restart-required: the answer is asked for each fact."""

    from my_claude_code.providers.recovery import facts

    settings = _settings(
        STATED_FACT_TTL_SECONDS=100.0,
        INFERRED_FACT_TTL_SECONDS=200.0,
        WITHHELD_FACT_TTL_SECONDS=300.0,
    )
    with patch.object(facts, "get_settings", _fixed(settings)):
        assert facts.fact_ttl_seconds(facts.FACT_OUTPUT_CAP) == 100.0
        assert facts.fact_ttl_seconds(facts.FACT_RESPONSE_SURFACE) == 200.0
        assert facts.fact_ttl_seconds(facts.FACT_MODEL_WITHHELD) == 300.0
        # An unknown kind keeps the weaker of the two clocks, as it always did.
        assert facts.fact_ttl_seconds("not-a-kind") == 200.0

    # And the fact object itself reads through the same function, so a store
    # walking its rows sees the change on the next row rather than on restart.
    fact = facts.LearnedFact(
        provider_id="p",
        model_id="m",
        fact_kind=facts.FACT_OUTPUT_CAP,
        value=1,
        learned_at="2026-01-01T00:00:00+00:00",
        last_confirmed_at="2026-01-01T00:00:00+00:00",
        source=facts.SOURCE_REJECTION,
    )
    with patch.object(facts, "get_settings", _fixed(settings)):
        assert fact.ttl_seconds == 100.0


def test_describe_concurrency_reaches_the_adapter() -> None:
    """Built once, in the handler's constructor, from the handler's settings."""

    import inspect

    from my_claude_code.api.handlers import messages

    source = inspect.getsource(messages)
    assert "concurrency=settings.describe_concurrency" in source

    from my_claude_code.application.vision_describe import VisionDescribeAdapter

    # And the adapter really takes it, rather than reading the module constant
    # after being handed a number: the parameter is what the body uses.
    parameters = inspect.signature(VisionDescribeAdapter.__init__).parameters
    assert "concurrency" in parameters
    body = inspect.getsource(VisionDescribeAdapter.__init__)
    assert "self._concurrency = max(1, concurrency)" in body


def test_the_confirm_settings_reach_every_checker() -> None:
    """7.53.0's four settings arrive at the fetch, the routes and the re-prober."""

    import inspect

    from my_claude_code.api import admin_proxy_routes
    from my_claude_code.application.proxy_check import check_endpoints
    from my_claude_code.application.proxy_fetch import run_fetch_pass, start_fetch
    from my_claude_code.runtime import proxy_check_timer, proxy_feed_timer

    settings = Settings.model_validate({})
    assert settings.proxy_check_confirm_attempts == 3
    assert settings.proxy_check_confirm_spacing_seconds == 30.0
    assert settings.proxy_check_slow_ms == 3000
    assert settings.proxy_check_link_guard is True

    for func in (run_fetch_pass, start_fetch):
        params = inspect.signature(func).parameters
        for name in (
            "confirm_attempts",
            "confirm_spacing",
            "confirm_connect_timeout",
            "slow_ms",
            "link_guard",
        ):
            assert name in params, f"{func.__name__} cannot be told {name}"
    params = inspect.signature(check_endpoints).parameters
    assert "attempts" in params and "spacing" in params

    for module in (admin_proxy_routes, proxy_feed_timer):
        source = inspect.getsource(module)
        for attr in (
            "proxy_check_confirm_attempts",
            "proxy_check_confirm_spacing_seconds",
            "proxy_check_slow_ms",
            "proxy_check_link_guard",
            "proxy_connect_timeout_seconds",
        ):
            assert f"settings.{attr}" in source, f"{module.__name__} never reads {attr}"
    timer = inspect.getsource(proxy_check_timer)
    assert '"proxy_check_confirm_attempts"' in timer
    assert '"proxy_check_confirm_spacing_seconds"' in timer


def test_the_resort_interval_reaches_the_speed_order_writer() -> None:
    """7.56.0's PROXY_ORDER_RESORT_MINUTES: default 30, bounds 5-1440, read."""

    import inspect

    from pydantic import ValidationError

    from my_claude_code.api import admin_proxy_routes
    from my_claude_code.runtime import application as runtime_application

    assert Settings.model_validate({}).proxy_order_resort_minutes == 30
    assert (
        Settings.model_validate(
            {"PROXY_ORDER_RESORT_MINUTES": 90}
        ).proxy_order_resort_minutes
        == 90
    )
    for bad in (4, 1441):
        with pytest.raises(ValidationError):
            Settings.model_validate({"PROXY_ORDER_RESORT_MINUTES": bad})
    writer = inspect.getsource(admin_proxy_routes.commit_speed_order)
    assert '"proxy_order_resort_minutes"' in writer
    assert "resort_minutes=resort_minutes" in writer
    # The loop is handed the live Settings on every tick, not a snapshot.
    assert "ProxyOrderTimer(" in inspect.getsource(runtime_application)
    assert "lambda: self.settings" in inspect.getsource(
        runtime_application.ApplicationRuntime.__init__
    )
