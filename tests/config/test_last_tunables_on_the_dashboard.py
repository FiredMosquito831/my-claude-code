"""The 7.32.0 knobs: every one ships as the literal it replaced.

Three groups came off the 7.24.0 audit's follow-up list, and all three carry
the same promise -- an install that never touches the new field behaves
exactly as 7.31.0 did:

1. the proxy cooldown pair and the reachability ladder, plumbed into ``core``
   the way 7.22.0 plumbed ``RateLimitCooldown``;
2. a published bound for each of the twenty-three fields 7.24.0 froze;
3. the catalogue, price-table and learned-fact clocks, plus describe-mode
   concurrency.

The tests below are the proof of "ships as the literal it replaced": each one
reads the OLD constant, still in its old module, and asserts the setting's
default is that number. A release that changes one of these numbers has to
change a test that says out loud what it is changing.
"""

import pytest

from my_claude_code.application.vision_describe import DESCRIBE_CONCURRENCY
from my_claude_code.config.admin.manifest import FIELDS
from my_claude_code.config.settings import Settings, parse_lockout_tiers
from my_claude_code.core.proxy_rotation import (
    PROXY_COOLDOWN_MAX_SECONDS,
    PROXY_COOLDOWN_SECONDS_DEFAULT,
    PROXY_REACHABILITY_TIERS,
    PROXY_TUNING,
    ReachabilityLedger,
    configure_proxy_rotation,
)
from my_claude_code.providers.recovery.facts import (
    INFERRED_FACT_TTL_SECONDS,
    STATED_FACT_TTL_SECONDS,
    WITHHELD_FACT_TTL_SECONDS,
)
from my_claude_code.providers.runtime.litellm_prices import (
    LITELLM_CACHE_TTL_SECONDS,
    LITELLM_FETCH_TIMEOUT_SECONDS,
)
from my_claude_code.providers.runtime.models_dev import (
    MODELS_DEV_CACHE_TTL_SECONDS,
    MODELS_DEV_FETCH_TIMEOUT_SECONDS,
)

FIELD_BY_KEY = {field.key: field for field in FIELDS}


# --------------------------------------------------- 1. defaults are literals


def test_the_proxy_cooldown_pair_ships_as_the_engine_s_own_numbers() -> None:
    settings = Settings()
    assert settings.proxy_cooldown_seconds == PROXY_COOLDOWN_SECONDS_DEFAULT
    assert settings.proxy_cooldown_max_seconds == PROXY_COOLDOWN_MAX_SECONDS


def test_the_reachability_ladder_ships_as_the_engine_s_own_ladder() -> None:
    """The comma list and the tuple in ``core`` are the same three numbers."""

    typed = parse_lockout_tiers(Settings().proxy_reachability_tiers)
    assert typed == PROXY_REACHABILITY_TIERS


def test_the_catalogue_clocks_ship_as_their_modules_literals() -> None:
    settings = Settings()
    assert settings.models_dev_cache_ttl_seconds == MODELS_DEV_CACHE_TTL_SECONDS
    assert settings.models_dev_fetch_timeout_seconds == MODELS_DEV_FETCH_TIMEOUT_SECONDS
    assert settings.litellm_cache_ttl_seconds == LITELLM_CACHE_TTL_SECONDS
    assert settings.litellm_fetch_timeout_seconds == LITELLM_FETCH_TIMEOUT_SECONDS


def test_the_learned_fact_clocks_ship_as_their_modules_literals() -> None:
    settings = Settings()
    assert settings.stated_fact_ttl_seconds == STATED_FACT_TTL_SECONDS
    assert settings.inferred_fact_ttl_seconds == INFERRED_FACT_TTL_SECONDS
    assert settings.withheld_fact_ttl_seconds == WITHHELD_FACT_TTL_SECONDS


def test_describe_concurrency_ships_as_the_adapter_s_own_number() -> None:
    assert Settings().describe_concurrency == DESCRIBE_CONCURRENCY


@pytest.mark.parametrize(
    "key",
    [
        "PROXY_COOLDOWN_SECONDS",
        "PROXY_COOLDOWN_MAX_SECONDS",
        "PROXY_REACHABILITY_TIERS",
        "MODELS_DEV_CACHE_TTL_SECONDS",
        "MODELS_DEV_FETCH_TIMEOUT_SECONDS",
        "LITELLM_CACHE_TTL_SECONDS",
        "LITELLM_FETCH_TIMEOUT_SECONDS",
        "STATED_FACT_TTL_SECONDS",
        "INFERRED_FACT_TTL_SECONDS",
        "WITHHELD_FACT_TTL_SECONDS",
        "DESCRIBE_CONCURRENCY",
    ],
)
def test_the_manifest_default_is_the_settings_default(key: str) -> None:
    """The number the form offers is the number the code would have used."""

    field = FIELD_BY_KEY[key]
    assert field.settings_attr
    shipped = getattr(Settings(), field.settings_attr)
    if isinstance(shipped, str):
        assert field.default == shipped
    else:
        assert float(field.default) == float(shipped)


# ------------------------------------------- 2. the ladder change re-arm rules


def test_a_shorter_window_shortens_a_pending_bench() -> None:
    """The operator just said a third failure is worth twenty seconds."""

    now = [1000.0]
    ledger = ReachabilityLedger(tiers=(60.0, 300.0, 3600.0), clock=lambda: now[0])
    for _ in range(3):
        ledger.note_failure("a:1", "ConnectError")
    assert ledger.remaining("a:1") == pytest.approx(3600.0)

    ledger.set_tiers((5.0, 10.0, 20.0))
    assert ledger.remaining("a:1") == pytest.approx(20.0)
    # And it is still out: only a pass clears a bench, never a re-ladder.
    assert ledger.is_unhealthy("a:1") is True


def test_a_tier_index_past_a_shortened_ladder_clamps_to_the_last_entry() -> None:
    now = [1000.0]
    ledger = ReachabilityLedger(tiers=(60.0, 300.0, 3600.0), clock=lambda: now[0])
    for _ in range(5):
        ledger.note_failure("a:1", "ConnectError")
    assert ledger.failures("a:1") == 5

    ledger.set_tiers((7.0,))
    assert ledger.remaining("a:1") == pytest.approx(7.0)


def test_a_longer_ladder_never_lengthens_a_bench_already_running() -> None:
    """Re-arming is a clamp, not a reschedule: nothing is benched for longer."""

    now = [1000.0]
    ledger = ReachabilityLedger(tiers=(60.0,), clock=lambda: now[0])
    ledger.note_failure("a:1", "ConnectError")
    ledger.set_tiers((3600.0,))
    assert ledger.remaining("a:1") == pytest.approx(60.0)


def test_a_stored_bench_is_re_armed_against_the_new_ladder() -> None:
    """``restore`` reads a file written by whatever ladder ran last time."""

    now = [1000.0]
    ledger = ReachabilityLedger(tiers=(5.0, 10.0, 20.0), clock=lambda: now[0])
    # An hour still to wait, written by 60,300,3600 before the ladder changed.
    ledger.restore("a:1", 3, 3600.0, "ConnectError")
    assert ledger.remaining("a:1") == pytest.approx(20.0)
    assert ledger.is_unhealthy("a:1") is True


def test_restore_is_unchanged_when_the_ladder_is() -> None:
    """The clamp cannot fire on an install that changed nothing."""

    now = [1000.0]
    ledger = ReachabilityLedger(tiers=PROXY_REACHABILITY_TIERS, clock=lambda: now[0])
    ledger.restore("a:1", 2, 123.0, "ConnectError")
    assert ledger.remaining("a:1") == pytest.approx(123.0)


def test_an_expired_stored_bench_is_still_due_immediately() -> None:
    """7.19.0's rule, unchanged by the clamp: expiry is not a pass."""

    now = [1000.0]
    ledger = ReachabilityLedger(tiers=(5.0,), clock=lambda: now[0])
    ledger.restore("a:1", 1, 0.0, "ConnectError")
    assert ledger.remaining("a:1") == 0.0
    assert ledger.due_for_reprobe("a:1") is True
    assert ledger.is_unhealthy("a:1") is True


# ------------------------------------------------------- 3. the bounds' shape


#: Every key in the reporting operator's own ``~/.mcc/.env``, values replaced.
#: The point of the list is the KEY SET: these are the fields a real install is
#: configuring today, and every one of the newly bounded numbers among them
#: must still load. No real value is reproduced here.
USER_SHAPED_ENV_KEYS: frozenset[str] = frozenset(
    {
        "PORT",
        "HOST",
        "HTTP_CONNECT_TIMEOUT",
        "HTTP_READ_TIMEOUT",
        "HTTP_WRITE_TIMEOUT",
        "MAX_MESSAGE_LOG_ENTRIES_PER_CHAT",
        "MESSAGING_RATE_LIMIT",
        "MESSAGING_RATE_WINDOW",
        "FALLBACK_REASONING_ANSWER_TIMEOUT",
        "PROVIDER_MAX_CONCURRENCY",
        "PROVIDER_RATE_LIMIT",
        "PROVIDER_RATE_WINDOW",
        "PROVIDER_RETRY_ATTEMPTS",
        "REQUEST_LOG_MAX_ROWS",
        "REQUEST_LOG_TEXT_MAX_CHARS",
        "REQUEST_LOG_COMPRESSION_LEVEL",
        "REQUEST_LOG_QUEUE_MAX_SIZE",
        "REQUEST_LOG_IMAGE_MAX_PIXELS",
        "SERVER_GRACEFUL_SHUTDOWN_SECONDS",
        "WEBSEARCH_DIGEST_CONTENT_CHARS",
        "WEBSEARCH_LOG_CONTENT_MAX_CHARS",
        "WEBSEARCH_LOG_MAX_ROWS",
        "CREDENTIAL_LOCKOUT_TIERS",
        "RATE_LIMIT_COOLDOWN_SECONDS",
        "MAX_OUTPUT_TOKENS_CEILING",
        "MAX_OUTPUT_TOKENS_CONTEXT_FLOOR",
        "MAX_OUTPUT_TOKENS_CONTEXT_MARGIN",
        "MAX_OUTPUT_TOKENS_UNKNOWN_DEFAULT",
        "STREAM_COMMIT_HOLDBACK_SECONDS",
        "STREAM_EARLY_RETRY_ATTEMPTS",
        "STREAM_MIDSTREAM_RECOVERY_ATTEMPTS",
        "TOOL_RESULT_TRIM_KEEP_HEAD_CHARS",
        "TOOL_RESULT_TRIM_KEEP_TAIL_CHARS",
        "TOOL_RESULT_TRIM_THRESHOLD_CHARS",
        "DESKTOP_WINDOW_HEIGHT",
        "DESKTOP_WINDOW_WIDTH",
        "DESKTOP_HEALTH_CHECK_INTERVAL",
        "DESKTOP_HEALTH_FAILURE_THRESHOLD",
        "DESKTOP_HEALTH_POLL_SECONDS",
        "DESKTOP_SERVER_START_TIMEOUT",
        "DESKTOP_ADMIN_REQUEST_TIMEOUT",
        "DESKTOP_ACTIVATION_POLL_SECONDS",
        "FALLBACK_EJECT_AFTER_FAILURES",
        "FALLBACK_EJECT_FAILURE_RATE",
        "FALLBACK_EJECT_MIN_SAMPLES",
        "FALLBACK_EJECT_SECONDS",
        "FALLBACK_EJECT_WINDOW",
        "FALLBACK_FIRST_TOKEN_TIMEOUT",
        "FALLBACK_STALL_TIMEOUT",
        "FALLBACK_TOTAL_TIMEOUT",
        "FALLBACK_ATTEMPT_SHARE_FLOOR",
        "REASONING_ANSWER_FLOOR_MAX",
    }
)


def test_the_user_shaped_env_still_validates_at_its_shipped_values() -> None:
    """A real install's key set, loaded as strings, at today's numbers.

    Every one of these keys is a number a running install is already carrying,
    and sixteen of them are newly bounded by this release. They are handed in
    as *strings*, which is what a ``.env`` layer produces, and the result must
    equal the shipped default -- nothing rejected, nothing clamped, nothing
    coerced to something else.
    """

    settings = Settings()
    payload: dict[str, object] = {}
    for key in sorted(USER_SHAPED_ENV_KEYS):
        field = FIELD_BY_KEY.get(key)
        assert field is not None, f"{key} is not a manifest field"
        assert field.settings_attr, f"{key} has no setting behind it"
        shipped = getattr(settings, field.settings_attr)
        # One field in the set ships empty and means "the built-in cap"; an
        # install that set it at all set a number, so the shape being tested
        # is a number well inside the new bound.
        payload[key] = "5000" if shipped is None else str(shipped)

    loaded = Settings.model_validate(payload)
    for key in sorted(USER_SHAPED_ENV_KEYS):
        attr = FIELD_BY_KEY[key].settings_attr
        assert attr
        shipped = getattr(settings, attr)
        if shipped is None:
            assert getattr(loaded, attr) == 5000, key
            continue
        assert getattr(loaded, attr) == shipped, key


@pytest.mark.parametrize("key", sorted(USER_SHAPED_ENV_KEYS))
def test_a_newly_bounded_field_cannot_refuse_its_own_shipped_value(key: str) -> None:
    """The bound published for a field contains the value that field ships.

    A bound that excluded its own default would refuse every install on the
    day it landed. This is the cheap version of the argument made per field in
    the release notes, applied mechanically to the whole key set.
    """

    field = FIELD_BY_KEY[key]
    if field.field_type != "number":
        pytest.skip(f"{key} is not a number field")
    assert field.settings_attr
    value = float(getattr(Settings(), field.settings_attr) or 0)
    if field.minimum is not None:
        assert value >= field.minimum, f"{key} default {value} < {field.minimum}"
    if field.maximum is not None:
        assert value <= field.maximum, f"{key} default {value} > {field.maximum}"


def test_every_number_on_the_dashboard_contains_its_own_default() -> None:
    """The same argument, for all 486 fields rather than one install's."""

    settings = Settings()
    offenders: list[str] = []
    for field in FIELDS:
        if field.field_type != "number" or not field.settings_attr:
            continue
        raw = getattr(settings, field.settings_attr, None)
        if raw is None or isinstance(raw, str):
            continue
        value = float(raw)
        if field.minimum is not None and value < field.minimum:
            offenders.append(f"{field.key}: {value} < {field.minimum}")
        if field.maximum is not None and value > field.maximum:
            offenders.append(f"{field.key}: {value} > {field.maximum}")
    assert not offenders, "\n".join(offenders)


# --------------------------------------------- 4. the policy reaches the core


def test_configure_hands_the_engine_the_operator_s_policy() -> None:
    """The 7.22.0 shape: built where Settings is, passed into ``core``."""

    try:
        configure_proxy_rotation(
            cooldown_seconds=42.0,
            cooldown_max_seconds=99.0,
            reachability_tiers=(5.0, 10.0, 20.0),
        )
        assert PROXY_TUNING.rate_limit_seconds == 42.0
        assert PROXY_TUNING.rate_limit_max_seconds == 99.0
        assert PROXY_TUNING.lockout_tiers == (5.0, 10.0, 20.0)
        from my_claude_code.core.proxy_rotation import PROXY_REACHABILITY

        assert PROXY_REACHABILITY.tiers == (5.0, 10.0, 20.0)
    finally:
        configure_proxy_rotation()

    # And calling it with nothing restores exactly what 7.19.0 shipped, which
    # is what makes the defaults above the whole behaviour contract.
    assert PROXY_TUNING.rate_limit_seconds == PROXY_COOLDOWN_SECONDS_DEFAULT
    assert PROXY_TUNING.rate_limit_max_seconds == PROXY_COOLDOWN_MAX_SECONDS
    assert PROXY_TUNING.lockout_tiers == PROXY_REACHABILITY_TIERS
