"""Shared defaults used by config models and provider adapters."""

# HTTP client connect timeout (seconds). Keep aligned with README.md and .env.example.
HTTP_CONNECT_TIMEOUT_DEFAULT = 10.0

# Anthropic Messages API default when the client omits max_tokens.
ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS = 81920

# Non-secret marker stored in Settings when FCC owns renewable ChatGPT credentials.
CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE = "fcc-managed-oauth"

# Non-secret marker stored in Settings when FCC owns a renewable Claude
# subscription OAuth credential (imported from Claude Code, or from signing in
# directly). See docs/ANTHROPIC-SUBSCRIPTION.md.
ANTHROPIC_OAUTH_MANAGED_CREDENTIAL_REFERENCE = "fcc-managed-anthropic-oauth"

# Fallback timing. These live here rather than beside the executor because
# Settings needs them and the application layer already reads Settings.
#
# Measured against a 51,000-request log: first-token latency is 4.5s at p50 and
# 181.7s at p99.9, so a 120s deadline re-rolls 0.21% of healthy requests onto
# the next model while ending stalls that otherwise ran for 9 minutes.
FALLBACK_FIRST_TOKEN_TIMEOUT_DEFAULT = 120.0
# Whole-request durations on that same log: 7.5s at p50, 255.7s at p99.9. A 600s
# budget cuts 0.03% of healthy requests short and caps the rest.
FALLBACK_TOTAL_TIMEOUT_DEFAULT = 600.0
# Consecutive failures before routing skips a provider/model, and for how long.
# Failure kinds that end a route instead of moving to the next model.
#
# A malformed request is the caller's, not the model's: the same body fails
# identically on every model, so walking a three-model chain costs three round
# trips to arrive at the same 400. Everything else -- timeout, upstream,
# rate_limit, overloaded, authentication, unavailable -- is a property of the
# model or the moment, and is exactly what a chain exists for.
#
# A deny-list rather than an allow-list on purpose: a failure kind added later
# falls back by default, which is the safe direction. An allow-list would
# silently stop covering it.
# Seconds a stream that has already produced output may then say nothing.
#
# Deliberately the same number as the first-token deadline rather than a new
# one: it answers the same question -- how long may this model be silent --
# and mid-answer silence is if anything more suspicious than silence before
# the answer, which at least covers queueing and cold starts.
#
# Measured against 146,857 successful requests, the slowest of them averaged
# one output token every 2.27 seconds, so this sits about fifty times beyond
# the worst rate ever observed here. 0 disables it.
FALLBACK_STALL_TIMEOUT_DEFAULT = 120.0

FALLBACK_SKIP_KINDS_DEFAULT = "invalid_request"

# Mirrors core.failures.FailureKind. `config` is a leaf package by declared
# policy -- it imports nothing, not even core -- so the names are repeated
# here rather than imported. A list that mirrors another file drifts, so
# tests/contracts/test_import_boundaries.py pins the two equal in both
# directions: a test, not discipline.
FAILURE_KIND_NAMES: frozenset[str] = frozenset(
    {
        "invalid_request",
        "context_length",
        "authentication",
        "permission",
        "rate_limit",
        "overloaded",
        "timeout",
        "upstream",
        "unavailable",
    }
)

FALLBACK_EJECT_AFTER_FAILURES_DEFAULT = 3
FALLBACK_EJECT_SECONDS_DEFAULT = 30.0
# Rate-based ejection policy: skip a model when at least this fraction of the
# last `FALLBACK_EJECT_WINDOW` requests have failed (with at least
# `FALLBACK_EJECT_MIN_SAMPLES` requests seen so the rate is meaningful).
# Consecutive-count mode (FALLBACK_BEHAVIOR=legacy) ignores these and uses
# `FALLBACK_EJECT_AFTER_FAILURES` + `FALLBACK_EJECT_SECONDS` instead.
FALLBACK_BEHAVIOR_DEFAULT = "rate_based"
FALLBACK_RETRY_FIRST_DEFAULT = "skip"
FALLBACK_EJECT_WINDOW_DEFAULT = 10
FALLBACK_EJECT_FAILURE_RATE_DEFAULT = 0.5
FALLBACK_EJECT_MIN_SAMPLES_DEFAULT = 8

# Resilience knobs that used to be module constants. Each one decides how long a
# failing model is allowed to hold a request, which is a deployment question,
# not a protocol fact.
PROVIDER_RETRY_ATTEMPTS_DEFAULT = 5
STREAM_EARLY_RETRY_ATTEMPTS_DEFAULT = 5
STREAM_MIDSTREAM_RECOVERY_ATTEMPTS_DEFAULT = 5
# Output is held this long before it commits. While held, a failure can still
# fall back invisibly, so this is the width of the fallback window itself.
STREAM_COMMIT_HOLDBACK_SECONDS_DEFAULT = 0.75
# Whether a stream that has emitted only reasoning may still fall back.
# True holds reasoning back like scaffolding, so a model that thinks and
# never answers leaves the route uncommitted and the chain can take over.
FALLBACK_ON_REASONING_ONLY_DEFAULT = True
# How long a model held at the reasoning boundary may think before the route
# gives up on it. Measured on 21 days of traffic: every one of the 499 budget
# exhaustions ran the *full* 600s, while 98% of slow reasoning successes had
# started answering by 300s -- so this separates the two almost exactly.
FALLBACK_REASONING_ANSWER_TIMEOUT_DEFAULT = 300.0
STREAM_COMMIT_HOLDBACK_MAX_BYTES_DEFAULT = 65_536
# Used only when a rate-limited provider sends no Retry-After to obey.
RATE_LIMIT_COOLDOWN_SECONDS_DEFAULT = 60.0
CREDENTIAL_CIRCUIT_THRESHOLD_DEFAULT = 3

# Graceful shutdown budget (seconds) handed to the supervisor for each server
# generation. It bounds how long in-flight requests get to finish while the
# runtime is closing (RELOAD or REPLACE_PROCESS) before the supervisor force-drops
# them. This is a deployment choice, not a protocol fact, so it is a configurable,
# bounded Settings field (see config/limits.py) rather than a fixed module value.
#
# Grounding for the default and bounds: the same ~51,000-request log behind the
# fallback budgets shows whole-request durations at p50 7.5s and p99.9 255.7s. The
# default sits just over that p99.9 (300s) so an update/reload handoff lets nearly
# all healthy requests drain; only requests longer than the budget (up to the 600s
# total-request budget) can still be force-cut. The floor is 1s because uvicorn
# treats 0 as an immediate, no-drain shutdown rather than "wait forever".
SERVER_GRACEFUL_SHUTDOWN_SECONDS_DEFAULT = 300.0

# Dashboard reconnect budget (seconds) the admin UI waits for the server to come
# back after a self-triggered update. Composed from the real phases of the handoff
# rather than a bare number: the install (uv tool install --force, up to 900s), the
# graceful drain of the old process (SERVER_GRACEFUL_SHUTDOWN_SECONDS_DEFAULT), and a
# startup/bind margin for the new process to come back. The old fixed 120s abandoned
# a healthy update mid-handoff, so the dashboard now reads this from the version
# status instead of hard-coding it.
DASHBOARD_RECONNECT_TIMEOUT_SECONDS = (
    900.0 + SERVER_GRACEFUL_SHUTDOWN_SECONDS_DEFAULT + 120.0
)

# Request log storage.
REQUEST_LOG_MAX_ROWS_DEFAULT = 50_000
REQUEST_LOG_TEXT_MAX_CHARS_DEFAULT = 50_000
REQUEST_LOG_COMPRESSION_LEVEL_DEFAULT = 9
REQUEST_LOG_QUEUE_MAX_SIZE_DEFAULT = 10_000
# Longest edge of the thumbnail kept for an image a request carried. A pasted
# screenshot is megabytes; at 512px it is tens of kilobytes and still legible,
# and identical images are stored once however many turns re-send them.
REQUEST_LOG_IMAGE_MAX_PIXELS_DEFAULT = 512

# Desktop tray/window process timing and sizing. mcc-desktop is a separate
# process from the server: it calls get_settings() at launch, so a change
# made in the dashboard applies to the next mcc-desktop start, not to a tray
# already running. See config/limits.py for the bounds and their reasons.
DESKTOP_HEALTH_CHECK_INTERVAL_DEFAULT = 0.25
DESKTOP_SERVER_START_TIMEOUT_DEFAULT = 15.0
DESKTOP_ADMIN_REQUEST_TIMEOUT_DEFAULT = 5.0
DESKTOP_ACTIVATION_POLL_SECONDS_DEFAULT = 1.0
DESKTOP_HEALTH_POLL_SECONDS_DEFAULT = 5.0
DESKTOP_HEALTH_FAILURE_THRESHOLD_DEFAULT = 3
DESKTOP_WINDOW_WIDTH_DEFAULT = 1400
DESKTOP_WINDOW_HEIGHT_DEFAULT = 900

# Tool-result trimming (Read / Grep / Glob). Off by default: this layer changes
# what the model sees, so a fresh install must behave exactly as it did before
# the layer existed.
#
# Grounding for the size default, measured rather than chosen. Rendering every
# source, doc and config file in this repository the way Claude Code's `Read`
# renders one (a right-aligned line number, a tab, the line) gives 970 whole-file
# results: p50 3,012 chars, p75 8,534, p90 20,735, p99 93,433, max 374,612. The
# default sits at that p90, so roughly nine reads in ten are never touched --
# while the tenth holds 59.6% of all the bytes, because size distribution here is
# extremely long-tailed. A threshold is a real setting rather than a constant in
# code precisely because that distribution is per-repository.
TOOL_RESULT_TRIM_THRESHOLD_CHARS_DEFAULT = 20_000
# Kept from each end of a trimmed body. 4,000 each means a trimmed result still
# carries more text than the p50 whole-file read (3,012 chars) at both its head
# and its tail, so the opening structure and the closing lines both survive.
TOOL_RESULT_TRIM_KEEP_HEAD_CHARS_DEFAULT = 4_000
TOOL_RESULT_TRIM_KEEP_TAIL_CHARS_DEFAULT = 4_000
# Newest attributable results never trimmed. The result the model just received
# is the one it is reasoning about now, and it is also the cheapest to keep: an
# old result is re-sent on every later turn, the newest is sent once. 2 covers
# the common Read-then-act and Grep-then-Read pairs. 0 protects nothing.
TOOL_RESULT_TRIM_PROTECT_RECENT_DEFAULT = 2

# Mirrors core.anthropic.tool_result_trimming.TrimMode. `config` is a leaf
# package by declared policy -- it imports nothing, not even core -- so the
# names are repeated here rather than imported, and
# tests/contracts/test_import_boundaries.py pins the two equal in both
# directions exactly as it does for FAILURE_KIND_NAMES.
TRIM_MODE_NAMES: frozenset[str] = frozenset({"off", "observe", "on"})

# Nous Portal rejects an API-key request that carries no `tags` array with a
# `user=` entry: HTTP 400 "This request is not valid. Check the model name and
# other parameters. Additional info: missing tags". OAuth callers are identified
# by their bearer token instead, which is why the requirement is undocumented in
# the OpenAPI spec. The value after `user=` is free-form; only the prefix is
# mandatory. Enforcement began 2026-08-27, when every previously-working
# `tencent/hy3:free` request started failing.
NOUS_PORTAL_USER_TAG_DEFAULT = "user=my-claude-code"
