"""Ranges for the numeric limits, and what each end of a range means.

One table, read by three places that must agree: Settings clamps to it so the
server always starts, the admin manifest publishes it so a number input can
refuse before saving, and the validator quotes it back when someone is outside.
Three separate copies of "sensible" would drift, and the disagreement would show
up as a form that accepts a value the server then quietly changes.

Bounds are deliberately wide. They exist to rule out values that cannot work --
a zstd level the compressor rejects, a retry count that never tries -- not to
express a preference about how anyone should run their proxy.
"""

from dataclasses import dataclass

# zstd rejects anything above this outright, so a higher setting would fail on
# every single body write rather than merely compressing badly.
ZSTD_MAX_LEVEL = 22


@dataclass(frozen=True, slots=True)
class LimitRange:
    """Bounds for one numeric setting, with the reason a bound exists."""

    minimum: float
    maximum: float
    # What the low end means when it is a real value rather than a floor,
    # e.g. "0 waits indefinitely". Empty when the minimum is just a floor.
    minimum_note: str = ""

    def clamp(self, value: float) -> float:
        return min(max(value, self.minimum), self.maximum)

    def contains(self, value: float) -> bool:
        return self.minimum <= value <= self.maximum


HOUR = 3600.0
DAY = 86400.0

LIMIT_RANGES: dict[str, LimitRange] = {
    # --- how many tokens one answer may be ---------------------------------
    # The upper bound is the largest output limit anything in the models.dev
    # catalogue publishes, rounded up to a power of two. It is a sanity bound
    # on a hand-typed number, not an opinion about model capacity: a real
    # per-model limit is read from the model, never from this table.
    "max_output_tokens_unknown_default": LimitRange(1, 1_048_576),
    # 0 lifts the head entirely. Needed because the field now ships set, and
    # a blank value resolves to the default rather than to "unset".
    "max_output_tokens_ceiling": LimitRange(
        0, 1_048_576, "0 lifts the ceiling entirely"
    ),
    # 0 turns the reservation off, which is only sane if you trust FCC's token
    # count to match the upstream's exactly.
    "max_output_tokens_context_margin": LimitRange(
        0, 65_536, "0 reserves nothing for the prompt"
    ),
    # 0 restores the pre-floor behaviour: any positive headroom is sent, however
    # small. The upper bound is the same catalogue-wide output maximum used
    # above -- a floor larger than that would reject every bounded request.
    "max_output_tokens_context_floor": LimitRange(
        0, 1_048_576, "0 sends any positive headroom, however small"
    ),
    # 0 lets thinking take the whole output allowance, which is the
    # unreconciled behaviour this setting exists to remove.
    "reasoning_answer_floor_max": LimitRange(
        0, 1_048_576, "0 leaves no tokens reserved for the answer"
    ),
    # --- when to stop waiting ---------------------------------------------
    "fallback_first_token_timeout": LimitRange(
        0.0, HOUR, "0 waits indefinitely for the first token"
    ),
    "fallback_total_timeout": LimitRange(0.0, DAY, "0 disables the budget"),
    "fallback_attempt_share_floor": LimitRange(
        0.0, HOUR, "0 divides the budget equally with no floor"
    ),
    "fallback_stall_timeout": LimitRange(
        0.0, HOUR, "0 allows an unlimited pause mid-answer"
    ),
    "fallback_eject_after_failures": LimitRange(0, 1_000, "0 never benches a model"),
    "fallback_eject_seconds": LimitRange(0.0, DAY),
    # A provider has to be allowed to try once, so the floor is 1 attempt.
    "provider_retry_attempts": LimitRange(1, 20),
    "stream_early_retry_attempts": LimitRange(1, 20),
    "stream_midstream_recovery_attempts": LimitRange(0, 20, "0 disables recovery"),
    # Above a few seconds the holdback is no longer a recovery window, it is
    # just latency the client cannot explain.
    "stream_commit_holdback_seconds": LimitRange(
        0.0, 30.0, "0 commits the first chunk immediately"
    ),
    # A character count, not a byte cap: the buffer's own 65,536-byte ceiling
    # still ends the window whatever this says, so a model that writes a novel
    # in one frame cannot be held indefinitely.
    "stream_commit_holdback_chars": LimitRange(
        0, 8_192, "0 uses the holdback clock alone"
    ),
    "rate_limit_cooldown_seconds": LimitRange(0.0, DAY, "0 does not pause"),
    # Below this a cooldown is not worth spending a chain slot on; the ceiling
    # is ten minutes, past which nothing would ever be stepped over.
    "fallback_cooldown_step_over_floor": LimitRange(
        0.0, 600.0, "0 steps over any cooldown at all"
    ),
    # Backoff between a provider's own retries. 0 retries immediately, which
    # is a real choice for a local runtime that has no rate limit to respect.
    "provider_retry_backoff_base_seconds": LimitRange(
        0.0, 60.0, "0 retries immediately"
    ),
    "provider_retry_backoff_max_seconds": LimitRange(0.0, HOUR),
    "provider_retry_backoff_jitter_seconds": LimitRange(
        0.0, 60.0, "0 makes every client retry in lockstep"
    ),
    # --- how much of a tool result to keep ---------------------------------
    # Longest a single Read/Grep/Glob result may be before a trim rule looks at
    # it. 0 would mean "consider everything", which is exactly the setting most
    # likely to hurt an answer, so the floor is deliberately not 0: the smallest
    # accepted threshold still leaves room for a head, a tail and the marker
    # that explains the hole between them. The ceiling is the point past which
    # no realistic tool result would ever qualify.
    "tool_result_trim_threshold_chars": LimitRange(2_000, 10_000_000),
    # Kept from each end of a trimmed body. 0 is allowed and means "keep no
    # head" / "keep no tail" -- a real choice for Glob, where the tail is as
    # informative as the head. The ceiling matches the threshold ceiling; the
    # transform itself refuses any combination that would not shrink the body.
    "tool_result_trim_keep_head_chars": LimitRange(
        0, 10_000_000, "0 keeps nothing before the elision"
    ),
    "tool_result_trim_keep_tail_chars": LimitRange(
        0, 10_000_000, "0 keeps nothing after the elision"
    ),
    # Newest attributable results never trimmed.
    "tool_result_trim_protect_recent_results": LimitRange(
        0, 1_000, "0 leaves even the result the model just received trimmable"
    ),
    # --- what to keep ------------------------------------------------------
    "request_log_max_rows": LimitRange(0, 100_000_000, "0 keeps every request"),
    "request_log_text_max_chars": LimitRange(0, 10_000_000, "0 stores no text"),
    "request_log_wire_body_max_chars": LimitRange(
        0, 1_000_000, "0 stores the knobs only, with no message or tool structure"
    ),
    "request_log_compression_level": LimitRange(1, ZSTD_MAX_LEVEL),
    # Longest edge of a stored image thumbnail. The ceiling is a full-HD edge:
    # past that it stops being a thumbnail and starts being the original.
    "request_log_image_max_pixels": LimitRange(
        0, 1920, "0 records that an image arrived without storing any pixels"
    ),
    # Below a few hundred a burst drops records; the queue is a buffer, not a
    # throttle.
    "request_log_queue_max_size": LimitRange(100, 10_000_000),
    # Graceful shutdown budget for one server generation: how long in-flight
    # requests get to finish before the supervisor force-drops them on a
    # reload/replace. The default sits just over the measured p99.9 whole-request
    # budget (255.7s) so most healthy requests drain; longer ones (up to the 600s
    # total budget) can still be force-cut. The
    # floor is 1s because uvicorn treats 0 as an immediate, no-drain shutdown.
    "server_graceful_shutdown_seconds": LimitRange(
        1.0,
        600.0,
        "1s minimum drain before connections are force-closed; uvicorn treats 0 as immediate shutdown, not infinite",
    ),
    # --- desktop tray/window process (mcc-desktop) --------------------------
    # mcc-desktop is a separate process from the server; it reads these via
    # get_settings() once, at launch, so a change here applies to the next
    # tray start, not to one already running.
    #
    # The tight poll used while waiting for a spawned server to become
    # healthy. Below the floor this becomes a busy loop; above a couple of
    # seconds "tight" stops meaning anything.
    "desktop_health_check_interval": LimitRange(
        0.05, 5.0, "0.05s floor keeps this from becoming a busy loop"
    ),
    # How long to wait for a spawned mcc-server child to answer healthy before
    # giving up and reporting a start failure.
    "desktop_server_start_timeout": LimitRange(1.0, 300.0),
    # Timeout for one loopback call to the server's admin API.
    "desktop_admin_request_timeout": LimitRange(0.5, 60.0),
    # How often the tray polls the activation file that a second launch
    # writes to say "show my window". The floor keeps this from becoming a
    # busy loop; the file-based doorbell design already tolerates a slow poll.
    "desktop_activation_poll_seconds": LimitRange(
        0.1, HOUR, "0.1s floor keeps this from becoming a busy loop"
    ),
    # How often the tray's background thread probes the server for the
    # ongoing health monitor (distinct from the tight startup poll above).
    "desktop_health_poll_seconds": LimitRange(
        0.5, HOUR, "0.5s floor keeps this from becoming a busy loop"
    ),
    # Consecutive failed health probes before the tray reports an outage. The
    # floor is 1 (report on the first failed probe); this is what keeps a
    # self-update restart -- one or two missed probes while the server
    # replaces its own process -- from being read as death.
    "desktop_health_failure_threshold": LimitRange(1, 1_000),
    # Desktop window size, in CSS pixels, for the app-mode/embedded window.
    # The floor keeps the dashboard usable; the ceiling is an 8K edge, past
    # which this stops being a window size and starts being a typo.
    "desktop_window_width": LimitRange(640, 7680),
    "desktop_window_height": LimitRange(480, 4320),
}


def range_for(settings_attr: str | None) -> LimitRange | None:
    """Return the range for a settings attribute, if it has one."""

    if settings_attr is None:
        return None
    return LIMIT_RANGES.get(settings_attr)


def describe_range(limit: LimitRange, *, unit: str = "") -> str:
    """Return a short human range, for a field description or an error."""

    def fmt(value: float) -> str:
        return str(int(value)) if float(value).is_integer() else str(value)

    suffix = f" {unit}" if unit else ""
    text = f"{fmt(limit.minimum)} to {fmt(limit.maximum)}{suffix}"
    if limit.minimum_note:
        text = f"{text} ({limit.minimum_note})"
    return text
