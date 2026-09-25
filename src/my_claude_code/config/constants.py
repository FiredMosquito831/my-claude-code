"""Shared defaults used by config models and provider adapters."""

# HTTP client connect timeout (seconds). Keep aligned with README.md and .env.example.
# 60s, not 10s: a cold TLS handshake to a provider that has not been called for
# a while regularly needs more than ten seconds, and a connect that times out
# spends a chain slot on a provider that was merely asleep.
HTTP_CONNECT_TIMEOUT_DEFAULT = 60.0

# Anthropic Messages API default when the client omits max_tokens, and nothing
# published a real per-model limit for the routed model. It is the last-resort
# per-profile default only: whenever a capability source knows what the model
# can actually emit, that number governs instead (WORKING-NOTES 54).
ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS = 81920

# Output-token budget for one request. Separate decisions, separate names --
# fusing them into one expression is how a fallback silently becomes a cap.
#
# 1. What to send when *no* source knows the model's output limit. A fallback,
#    never a limit: it supplies a value the client did not give, and it must
#    never reduce a value the client did give, because a guess has no standing
#    to override an explicit request.
MAX_OUTPUT_TOKENS_UNKNOWN_DEFAULT = 32768
# 2. The operator's absolute head on one answer, applied uniformly whether the
#    request reasons or not. 131,072 is the largest allowance any route on a
#    real install has needed and the number a thinking turn is widened *to*
#    rather than *past* (see application/output_tokens.py). It ships set
#    because the reasoning widening asks for the model's maximum: without a
#    head, one thinking turn on a 262,144-output model reserves 262,144 tokens
#    against a TPM limiter that pre-reserves max_tokens, and 429s a request
#    that would have been served.
#
#    It is still not a per-model opinion. A model that publishes less gets
#    less; the ceiling never raises anything. Set MAX_OUTPUT_TOKENS_CEILING=0
#    to lift it entirely and let every model's own published limit stand
#    (WORKING-NOTES 54).
MAX_OUTPUT_TOKENS_CEILING: int | None = 131072
# 3. Tokens held back from the context window when bounding output by the
#    remaining context. 1,117 of 7,440 models.dev entries report
#    ``limit.output == limit.context``, so on those the full output leaves no
#    room for the prompt. The margin absorbs the gap between FCC's own token
#    count and the upstream tokenizer plus whatever chat template the provider
#    wraps around the messages; 1,024 is small against any real context window
#    (the smallest in the catalogue is 4,096) and large enough to cover both.
MAX_OUTPUT_TOKENS_CONTEXT_MARGIN = 1024
# 4. The smallest budget that bounding by context is allowed to produce. The
#    headroom subtraction above is bounded below by 1, not by anything useful:
#    a catalogue ``context_length`` that is wrong or simply small against a
#    large prompt can leave a headroom of 3, and a request carrying
#    ``max_tokens: 3`` succeeds and returns a one-token answer. That reads as
#    "the model had nothing to say" when it is really "the configuration is
#    wrong", which is strictly worse than failing. Below this floor the request
#    is left unmodified so the provider reports the real context error, exactly
#    as the ``headroom <= 0`` case already does.
#
#    4,096 because it has to clear two bars at once. Large enough to be worth
#    sending: it is the entire output limit many catalogue entries publish, and
#    it is the smallest context window in the catalogue, so a model given 4,096
#    output tokens is being asked for no less than a real small model can do --
#    a tool call plus a genuine answer fits. Small enough not to reject workable
#    requests: it is a quarter of REASONING_ANSWER_FLOOR_MAX (16,384), which is
#    the *most* the reasoning split ever holds back for the visible answer and
#    is applied as ``min(that, output // 2)``. Setting the floor at or near
#    16,384 would reject prompts the reasoning path itself is content to run at
#    2,048 answer tokens. A quarter leaves the split its full working range and
#    still refuses the arbitrarily small budgets this floor exists to stop.
MAX_OUTPUT_TOKENS_CONTEXT_FLOOR = 4096
# 5. The smallest allowance one request may be sent with. The three clamps
#    above can only lower or supply; nothing raised a small ask to a workable
#    size, so a client that hardcoded ``max_tokens: 512`` got 512 tokens from a
#    model that can emit 131,072 and a truncated answer to show for it.
#
#    It raises, and it raises exactly once, *before* the context-headroom bound
#    so it can never re-inflate a budget the remaining context cannot hold. It
#    is bounded by the routed model's own published limit wherever one is
#    known: an unconditional floor above what a 16,384-output model can emit
#    (nvidia_nim/minimaxai/minimax-m3 is one, and live) would be a guaranteed
#    upstream 400, which is the exact defect application/output_tokens.py
#    exists to prevent. It also stands down on an explicit ``max_tokens: 0``
#    and wherever MAX_OUTPUT_TOKENS_UNKNOWN_DEFAULT=0 asked for no max_tokens
#    at all -- both are statements, not omissions.
#
#    8,192 because it is the smallest allowance that fits a real tool call plus
#    a genuine answer, it is at or below every published output limit in the
#    catalogue (the smallest is 4,096, and there the model's own limit wins),
#    and it is half of the smallest limit any model this project routes to in
#    anger publishes. Set MAX_OUTPUT_TOKENS_FLOOR=0 to apply no minimum at all
#    and restore the pre-6.47.0 behaviour.
MAX_OUTPUT_TOKENS_FLOOR: int | None = 8192

# Upper bound on the slice of the output allowance held back for the visible
# answer when thinking is enabled. Thinking tokens and answer tokens come out of
# the same ``max_tokens``; nothing reconciled them before, so a budget could
# consume the entire allowance and leave the model no room to reply.
#
# The floor actually applied is ``min(REASONING_ANSWER_FLOOR_MAX,
# effective_output // 2)`` -- proportional on purpose. A flat 16,384 on a
# 16,384-output model (nvidia_nim/minimaxai/minimax-m3) would leave a thinking
# budget of zero and silently disable reasoning; the halving gives a large
# reserve on a large model and an even split on a small one.
REASONING_ANSWER_FLOOR_MAX = 16384

# Share of the effective output allowance one named reasoning effort may spend
# on thinking, in ReasoningEffort declaration order: minimal, low, medium,
# high, xhigh, max. Comma-separated because it is one ladder, not six knobs --
# the same shape CREDENTIAL_LOCKOUT_TIERS uses, and the shape that makes an
# out-of-order set visible at a glance.
#
# These are the published industry ratios rather than FCC inventions; see the
# citation in application/reasoning_budget.py, which is where they are read.
REASONING_EFFORT_BUDGET_RATIOS_DEFAULT = "0.10,0.20,0.50,0.80,0.95,0.95"

# Non-secret marker stored in Settings when FCC owns renewable ChatGPT credentials.
CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE = "fcc-managed-oauth"

# Non-secret marker stored in Settings when FCC owns a renewable Claude
# subscription OAuth credential (imported from Claude Code, or from signing in
# directly). See docs/ANTHROPIC-SUBSCRIPTION.md.
ANTHROPIC_OAUTH_MANAGED_CREDENTIAL_REFERENCE = "fcc-managed-anthropic-oauth"

# Fallback timing. These live here rather than beside the executor because
# Settings needs them and the application layer already reads Settings.
#
# All five deadline defaults are 0 -- no limit -- since 6.16.0. The measured
# numbers they used to ship as (180s first token, 600s budget, 180s floor,
# 180s stall, 450s thinking; first-token latency 4.5s at p50 and 181.7s at
# p99.9 over a 51,000-request log) were right for the traffic they were
# measured on and wrong for a model that legitimately thinks for half an hour:
# a deadline that ends real work is a worse failure than a stall that has to
# be noticed. MCC is configured by the operator who runs it, so the shipped
# value is the one that decides nothing and the operator sets the one that
# does. The consequence, stated plainly here and in every doc: with these
# zeros MCC never ends a silent or stalled upstream on its own, and the
# fallback chain moves only on an error the provider actually returns. Set
# FALLBACK_FIRST_TOKEN_TIMEOUT / FALLBACK_STALL_TIMEOUT /
# FALLBACK_TOTAL_TIMEOUT to get time-based failover back. An install that
# already sets any of these keys keeps its own value.
#
# One thing these zeros deliberately do NOT decide, since 6.41.0: how long
# the server may take to STOP. A request with no deadline of its own used to
# mean a shutdown with no deadline either -- the response cleanup, the
# provider drain and the ASGI lifespan all waited on that request forever.
# Those waits are now bounded by SERVER_GRACEFUL_SHUTDOWN_SECONDS, which is a
# property of the stop rather than of the request. Leaving every deadline
# here at 0 is still the shipped behaviour and still means MCC never ends a
# stalled upstream on its own; it no longer means the process cannot exit.
FALLBACK_FIRST_TOKEN_TIMEOUT_DEFAULT = 0.0
# Whole-request budget across every attempt, retry and recovery. 0 disables it,
# which also makes the share division below moot: with no budget there is
# nothing to divide.
FALLBACK_TOTAL_TIMEOUT_DEFAULT = 0.0
# Smallest first-token allowance an attempt may be cut down to by sharing the
# total budget. The share division exists because on a long chain the equal
# share silently replaced the deadline the operator had configured: 600s over
# an eight-model chain is 75s, so a box reading 120 produced "produced no
# first token after 74.9494s" in the log. The floor is what makes the box mean
# what it says, and it is CHAIN-side -- it bounds each model's first-token
# allowance, never a retry of the same model.
# 0 restores the pure equal share, and is moot anyway while the total budget
# is 0: there is no budget to divide, so nothing can undercut the first-token
# deadline. The trade an operator who sets both takes on, spelled out in
# RouteExecutionPolicy._attempt_deadline and on the Limits & Resilience page:
# N silent models can spend up to N x this before the total budget clamps
# them, leaving later models less than the floor.
# 3600 is a floor no interactive attempt reaches, which is the point: it is
# inert while FALLBACK_TOTAL_TIMEOUT is 0 (there is no budget to divide) and
# becomes a real floor only for an operator who sets a total budget.
FALLBACK_ATTEMPT_SHARE_FLOOR_DEFAULT = 3600.0
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
# one output token every 2.27 seconds, so any value an operator picks here has
# a lot of room above the worst rate ever observed. Ships 0 -- disabled --
# with the rest of the deadlines: a stalled stream then runs until the
# transport read timeout ends it, not MCC.
FALLBACK_STALL_TIMEOUT_DEFAULT = 0.0

FALLBACK_SKIP_KINDS_DEFAULT = "invalid_request"

# Comma-separated globs deciding which provider/model refs are *listed*.
# Both empty by default, which lists everything the providers publish: a
# gateway's full catalogue is the honest default, and shrinking it is a
# preference, not a safety measure. Matching lives in core.model_visibility.
MODEL_VISIBILITY_ALLOW_DEFAULT = ""
MODEL_VISIBILITY_DENY_DEFAULT = ""

# Mirrors core.tier_refs.TIER_NAMESPACE and ModelTier. `config` is a leaf
# package by declared policy -- it imports nothing, not even core -- so the
# names are repeated here rather than imported, and
# tests/contracts/test_import_boundaries.py pins the two equal in both
# directions exactly as it does for FAILURE_KIND_NAMES. Two files need them on
# this side of the boundary: the per-harness tier store, which validates that a
# tier never points at another tier, and the provider registry, which reserves
# the namespace so a custom provider cannot shadow every alias.
TIER_NAMESPACE = "mcc"
MODEL_TIER_NAMES: tuple[str, ...] = (
    "cyber",
    "best",
    "good",
    "medium",
    "cheap",
    "vision",
)

# Mirrors providers.runtime.config.CREDENTIAL_ROTATION_POLICIES. `config` is a
# leaf package and may not import `providers` either, so the names are repeated
# here and tests/contracts/test_import_boundaries.py pins the two equal in both
# directions, exactly as it does for FAILURE_KIND_NAMES.
#
# The proxy chain store needs them because a chain rotates on the *same* four
# policies a credential pool does -- `single`, `round_robin`, `least_used`,
# `failover` -- and inventing a fifth name, or a second spelling of these four,
# would make two rotation controls on the same dashboard mean different things.
# `on_error` is the credential engine's own accepted alias of `failover`;
# it is normalised away on the way in rather than offered as a fifth choice.
ROTATION_POLICY_ORDER: tuple[str, ...] = (
    "single",
    "round_robin",
    "least_used",
    "failover",
)
ROTATION_POLICY_ALIASES: dict[str, str] = {"on_error": "failover"}

# Mirrors core.failures.FailureKind. `config` is a leaf package by declared
# policy -- it imports nothing, not even core -- so the names are repeated
# here rather than imported. A list that mirrors another file drifts, so
# tests/contracts/test_import_boundaries.py pins the two equal in both
# directions: a test, not discipline.
FAILURE_KIND_NAMES: frozenset[str] = frozenset(
    {
        "invalid_request",
        "model_rejected",
        "context_length",
        "authentication",
        "permission",
        "quota",
        "rate_limit",
        "overloaded",
        "timeout",
        "upstream",
        "unavailable",
        "free_tier",
    }
)

# Whether the route-level bench runs at all. Ships OFF: the bench was fed by
# request-shaped failures (a prompt too large for every model, a provider's
# 429s) and ejected the models that could actually have served the request,
# so a chain with healthy members answered with the last member's 400. Turn
# it on to have a model that keeps failing skipped for FALLBACK_EJECT_SECONDS.
FALLBACK_BENCH_ENABLED_DEFAULT = False

FALLBACK_EJECT_AFTER_FAILURES_DEFAULT = 3
# 10s, not 30s: a provider benched for half a minute outlives most of the
# sessions that benched it, so the chain keeps stepping over a model that
# recovered seconds after its one bad answer.
FALLBACK_EJECT_SECONDS_DEFAULT = 10.0
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
# Tries one model gets on ONE key before the failure is handed to the chain.
# Since 6.20.0 this ladder covers only 5xx and transport faults: a 429 is
# answered by routing to another model, not by waiting, so it consumes none of
# these. 3 at 2s/4s covers a transient gateway blip; the 5 this shipped as
# spent ~24s per key on failures the chain could have stepped over.
# 2, not 3: the third try lands ~6s after the first on a provider that has
# already failed twice, and the chain behind it can answer sooner than that.
PROVIDER_RETRY_ATTEMPTS_DEFAULT = 2
STREAM_EARLY_RETRY_ATTEMPTS_DEFAULT = 5
STREAM_MIDSTREAM_RECOVERY_ATTEMPTS_DEFAULT = 5
# Output is held this long before it commits. While held, a failure can still
# fall back invisibly, so this is the width of the fallback window itself.
STREAM_COMMIT_HOLDBACK_SECONDS_DEFAULT = 0.75
# Visible characters that must also arrive before the window may close. The
# seconds above answer "has the model had time to fail yet"; this answers "has
# it said enough to be worth keeping", which is the question that matters when
# a model emits one word and dies. 0 asks only the clock, which is how the
# holdback has always behaved.
STREAM_COMMIT_HOLDBACK_CHARS_DEFAULT = 0
# Whether a stream that has emitted only reasoning may still fall back.
# True holds reasoning back like scaffolding, so a model that thinks and
# never answers leaves the route uncommitted and the chain can take over.
FALLBACK_ON_REASONING_ONLY_DEFAULT = True
# What a stream that has already reached the client does when it then fails.
# True closes the open block and sends a stop reason meaning "cut short", so
# the client receives a short but valid message instead of an API error printed
# under a half-written answer. False restores the error.
FALLBACK_END_CLEANLY_AFTER_COMMIT_DEFAULT = True
# Whether a stream that has already reached the client may be continued by the
# next model on the route rather than only ended. Ships on: every way the
# continuation can fail lands on the truncated message above, so the worst
# outcome is the behaviour of the setting before it.
FALLBACK_RESUME_AFTER_COMMIT_DEFAULT = True
# How long a model held at the reasoning boundary may think before the route
# gives up on it. Measured on 21 days of traffic: every one of the 499 budget
# exhaustions ran the *full* 600s, while 98% of slow reasoning successes had
# started answering by 300s -- so a value between the two separates them
# almost exactly, and 450 is the one this shipped as until 6.16.0. It ships 0
# now for the same reason as the rest: a model that thinks for an hour is
# doing the work asked of it, and the operator, not MCC, decides when that
# stops being worth waiting for.
FALLBACK_REASONING_ANSWER_TIMEOUT_DEFAULT = 0.0
STREAM_COMMIT_HOLDBACK_MAX_BYTES_DEFAULT = 65_536
# Egress keepalive: while a streaming response is silent -- no model has
# produced a frame yet, or one has and then gone quiet -- MCC writes a frame
# the client's protocol defines as meaningless (``event: ping`` on
# /v1/messages, an SSE comment elsewhere) so a byte-level idle timer in the
# client or in anything between it and MCC does not decide the model is dead.
# 30 s of silence before the first one: measured over 133,183 streamed
# requests, 90.4% produce their first byte inside 30 s, so nine requests in
# ten never see a keepalive and keep their exact byte sequence. 0 turns it off.
STREAM_KEEPALIVE_IDLE_SECONDS_DEFAULT = 30.0
# Spacing of the keepalives after the first, while the silence lasts.
STREAM_KEEPALIVE_INTERVAL_SECONDS_DEFAULT = 20.0
# How long one stretch of silence may be kept alive. After it MCC stops
# writing keepalives and the client's own idle timer runs exactly as it did
# before keepalives existed, so a request can never hang longer than it would
# have without them. 300 is the lower of the two client idle floors seen in
# the request log. 0 keeps a silent stream alive for as long as it stays open.
STREAM_KEEPALIVE_MAX_SECONDS_DEFAULT = 300.0
# What the keepalive is on /v1/messages. ``ping`` is Anthropic's own ``ping``
# event, which the official Anthropic SDK -- and therefore Claude Code --
# throws away before its idle timer sees it. ``frames`` is opt-in: while the
# model's own text or tool-call block is open, the keepalive is an EMPTY delta
# of that block (``text_delta`` with ``""``, ``input_json_delta`` with ``""``),
# which Claude Code's timer does count and which concatenates to nothing.
# Anywhere else -- before ``message_start``, between blocks, inside a thinking
# block -- it is still a ping. Never a fabricated thinking block. Every other
# surface keeps its SSE comment whatever this says. ``ping`` by default because
# ``frames`` changes the wire shape, even though it changes no content.
STREAM_KEEPALIVE_MODE_NAMES: frozenset[str] = frozenset({"ping", "frames"})
STREAM_KEEPALIVE_MODE_DEFAULT = "ping"
# Used only when a rate-limited provider sends no Retry-After to obey.
RATE_LIMIT_COOLDOWN_SECONDS_DEFAULT = 60.0
# The ceiling on a wait a provider published in a header. 3600 was hard-coded
# as ``MAX_RATE_LIMIT_COOLDOWN_SECONDS`` until 7.22.0 and is the default here
# so that an install that says nothing behaves exactly as it did: an hour is
# the longest a per-request courtesy header is worth obeying, and beyond it a
# value is almost always a bug or a hostile number. 0 removes the ceiling and
# obeys whatever the host asked for.
RATE_LIMIT_COOLDOWN_MAX_SECONDS_DEFAULT = 3600.0
# What a 429 costs the credential that met it. ``provider`` is 7.21.0 exactly.
RATE_LIMIT_COOLDOWN_MODE_DEFAULT = "provider"
# Mirrors ``core.rate_limit.RATE_LIMIT_COOLDOWN_MODES``, which ``config`` -- a
# leaf package that imports nothing first-party -- may not import. Pinned in
# both directions by ``tests/contracts/test_import_boundaries.py``.
RATE_LIMIT_COOLDOWN_MODE_NAMES: tuple[str, ...] = ("provider", "fixed", "off")

# How often the background sweep re-reads every usable provider's ``/models``.
# Hourly is where the field converges (OpenCode 60 min, CLIProxyAPI 3 h,
# claude-code-router 10 min), and a catalogue that only ever changed at startup
# meant a model a gateway added this morning stayed invisible until a restart.
# 0 turns the sweep off entirely; the enforced floor below stops a typo turning
# it into a hot loop against 57 upstreams.
MODEL_DISCOVERY_REFRESH_SECONDS_DEFAULT = 3600.0
# The smallest interval the loop will honour once the sweep is on at all.
MODEL_DISCOVERY_REFRESH_MINIMUM_SECONDS = 300.0
# Whether a model that appears in a sweep is probed unattended. Off: a sweep
# that found 40 new models overnight would otherwise be 40-120 upstream
# requests against credentials nobody is watching, and some hosts bill or 403
# before they validate a body.
MODEL_PROBE_NEW_MODELS_DEFAULT = False
# Escalating bench for a credential the provider keeps rejecting with 401/403,
# indexed by consecutive auth failures and clamped at the last entry. Auth is
# the one failure a key can own outright, so it is the one ladder that stays.
CREDENTIAL_LOCKOUT_TIERS_DEFAULT = "300,3600,86400"

# Distinct models that must be simultaneously rate-limited on ONE key before
# the key itself is benched. A 429 is scoped to the (key, model) pair by
# default, because a gateway that limits one model says nothing about the
# key's others: measured on NVIDIA NIM, kimi-k3 429s on all three keys inside
# 0.1s while nemotron and minimax answer on the same keys in the same second,
# and 6.18.0 removed every NIM model from the route for 60s as a result.
# 1 restores that behaviour (never scope); 0 never escalates to the whole key.
CREDENTIAL_MODEL_BENCH_ESCALATION_DEFAULT = 2
# The most times one request may move to the next address in a provider's
# proxy chain. The ceiling for the whole install; each chain carries its own
# number inside it, and the smaller of the two applies. Every switch spends
# wall-clock inside a single attempt -- three dead addresses is three connect
# timeouts -- and the executor's deadlines do not move to make room, which is
# why 5 is a real bound rather than a formality. 2 means at most three
# addresses are tried per request.
PROXY_MAX_SWITCHES_PER_REQUEST_DEFAULT = 2
PROXY_MAX_SWITCHES_PER_REQUEST_MIN = 1
PROXY_MAX_SWITCHES_PER_REQUEST_MAX = 5
# How many entries one provider's chain may hold. 0 is UNLIMITED and is the
# shipped default: 7.19.0 removed the hard 12 that 7.13 shipped, because the
# operators who use this feature paste lists of hundreds of free addresses and
# a tool-invented ceiling was the wrong place to bound them. A ceiling an
# operator sets for themselves is still honoured -- the API refuses a longer
# chain with a message rather than silently truncating it.
PROXY_CHAIN_MAX_ENTRIES_DEFAULT = 0
# How many of a chain's legs may hold an open HTTP client at once. A leg is
# built on its first use and closed again when it has been idle longest, so a
# three-hundred-entry chain costs three hundred entries of memory rather than
# three hundred connection pools. 0 removes the bound and keeps every leg that
# was ever used open, which is what 7.13-7.18 effectively did.
PROXY_MAX_OPEN_LEGS_DEFAULT = 32
PROXY_MAX_OPEN_LEGS_MIN = 0
PROXY_MAX_OPEN_LEGS_MAX = 4096
# How many addresses may fail LIVE -- inside one request, carrying real
# traffic -- before the request stops walking the chain and goes out Direct.
# Distinct from the switch bound above, which counts only the switches an
# operator armed with a trigger chip. 0 removes the bound, and on a chain of
# three hundred dead addresses that is three hundred connect timeouts in one
# attempt, which is why it is not the default.
PROXY_MAX_LIVE_FAILURES_DEFAULT = 5
PROXY_MAX_LIVE_FAILURES_MIN = 0
PROXY_MAX_LIVE_FAILURES_MAX = 1000
# How long a *proxied* leg waits for the TCP/SOCKS handshake with its address
# before giving up on it. Only the connect step: the read, write and pool
# timeouts stay on the provider's own settings, and an unproxied client is not
# touched at all. A dead address is the common case on a public chain and the
# provider-wide connect timeout is sized for an origin, not for a stranger --
# one measured dead entry cost 21 s, and with no bound on how many a chain may
# hold that is the number that decides how long a request spends finding a live
# one.
PROXY_CONNECT_TIMEOUT_SECONDS_DEFAULT = 10.0
PROXY_CONNECT_TIMEOUT_SECONDS_MIN = 1.0
PROXY_CONNECT_TIMEOUT_SECONDS_MAX = 120.0
# Whether the health re-prober runs. TRUE, and unlike the background *checker*
# above this is on by default because it is not a new outbound conversation:
# it re-tests only the addresses that have ALREADY failed while carrying this
# operator's traffic, against the provider host that operator already routes
# to, and it is the only way back into rotation now that a bench expiring no
# longer re-admits an address by itself. A disabled chain is never probed, so
# an install whose chains are all off makes no request from this loop at all.
PROXY_HEALTH_REPROBE_ENABLED_DEFAULT = True
# Whether the background proxy checker runs. OFF, and it stays off until an
# operator turns it on: a user who never opens the Proxying page must make no
# outbound request they did not ask for. The Test button on that page is always
# available and is one request the operator pressed a button for.
PROXY_CHECK_ENABLED_DEFAULT = False
# Minutes between background sweeps of the addresses in a chain. 0 is off even
# when the switch above is on. Thirty is a compromise: a free address that died
# is found inside half an hour, and a twelve-entry catalogue costs twelve
# HEAD requests in that time, which is less than one coding session sends to
# one provider.
PROXY_CHECK_INTERVAL_MINUTES_DEFAULT = 30
PROXY_CHECK_INTERVAL_MINUTES_MIN = 0
PROXY_CHECK_INTERVAL_MINUTES_MAX = 1440
# The URL the checker asks "what address did this request come from". EMPTY,
# and deliberately shipped empty: it proves a proxy really changed the source
# address, and it is an outbound request to a stranger. MCC names no default
# host for it; the operator types one or the check does not happen.
PROXY_CHECK_EXIT_IP_URL_DEFAULT = ""
# Whether the named proxy feeds an operator switched on are re-read on a timer.
# OFF, and it is the second of two switches: this one only decides whether the
# reading happens without a button press. Which feeds may be read at all is a
# separate choice on the Proxying page, and it is empty on a fresh install, so
# an install where nobody touched either setting contacts no third party.
PROXY_FEED_REFRESH_ENABLED_DEFAULT = False
# Minutes between passes over the enabled feeds. 0 is off even when the switch
# above is on. Sixty is deliberately unhurried: these lists are other people's
# servers, the addresses on them churn over hours rather than seconds, and
# nothing in this product uses a candidate until an operator moves it into a
# chain by hand. The loop's own floor is 30 minutes regardless of this value.
PROXY_FEED_REFRESH_MINUTES_DEFAULT = 60
PROXY_FEED_REFRESH_MINUTES_MIN = 0
PROXY_FEED_REFRESH_MINUTES_MAX = 10080
# The floor the refresh loop applies whatever the setting above says. These
# lists are other people's servers and none of them re-tests anything faster
# than this, so a shorter interval buys no new addresses and only sends more
# requests to strangers.
PROXY_FEED_MINIMUM_MINUTES = 30
# How many addresses one fetch may test and keep on offer. 0 is UNLIMITED and
# is the shipped default: a chain has held any number of entries since 7.19.0,
# so an offer artificially trimmed to sixty was the tool inventing a ceiling
# the operator never asked for. A number an operator sets for themselves is
# honoured, and it is applied in rank order -- the best N are tested, not an
# arbitrary N.
PROXY_CANDIDATES_MAX_DEFAULT = 0
PROXY_CANDIDATES_MAX_MIN = 0
PROXY_CANDIDATES_MAX_MAX = 100000
# How many addresses a fetch tests at once. This sweep is not the Test button:
# it is hundreds of strangers' machines, most of which are no longer listening,
# and the slow half of each check is a TLS handshake that spends its time
# waiting on somebody else's network rather than on this CPU. The number here
# is also exactly how many sockets are open at one moment, because the
# semaphore that paces the sweep is what bounds them.
#
# A hundred, not the thirty-two that shipped in 7.21.0. That number was chosen
# before anybody had run this against a real catalogue: seven feeds offered
# 1,592 addresses, a dead one costs the whole connect timeout, and at 32 the
# sweep had tested 848 of them after 186 seconds. The work is waiting, not
# computing, so the ceiling is raised to 500 for an operator who wants a list
# of several thousand finished while they watch it.
PROXY_FETCH_TEST_CONCURRENCY_DEFAULT = 100
PROXY_FETCH_TEST_CONCURRENCY_MIN = 4
PROXY_FETCH_TEST_CONCURRENCY_MAX = 500
# How the number above is read. ``fixed`` is 7.21.0 exactly: it is a count of
# addresses. ``percent`` reads it as a percentage of however many addresses the
# feeds actually offered in this pass, resolved after they are merged, with the
# same floor and ceiling as the fixed number -- so one setting paces a list of
# two hundred and a list of five thousand alike.
#
# Mirrors ``application.proxy_fetch.FETCH_CONCURRENCY_MODES``, which ``config``
# -- a leaf package that imports nothing first-party -- may not import. Pinned
# in both directions by ``tests/contracts/test_import_boundaries.py``.
PROXY_FETCH_CONCURRENCY_MODE_DEFAULT = "fixed"
PROXY_FETCH_CONCURRENCY_MODE_NAMES: tuple[str, ...] = ("fixed", "percent")
# How far a fetch sweep's test of one address goes.
#
# ``tls`` stops at the thing the check is actually for: open the tunnel, finish
# a full TLS handshake to the provider's own host through it with ordinary
# strict trust, and close. A certificate that does not verify is an
# intercepting proxy and is refused exactly as before -- that control is
# unchanged -- but no HTTP request is ever sent. It is the default because a
# sweep of 1,592 addresses used to send about a thousand HEAD requests to the
# provider from a thousand different source addresses, which is a thing to do
# to somebody else's service only when it buys something, and the handshake
# already proves the tunnel.
#
# ``request`` is 7.22.1's check byte for byte: the same tunnel, then a HEAD to
# the destination. The Test and Add buttons -- and "Add all working" -- always
# use it whatever this says, because an address entering a chain is proven end
# to end rather than up to the handshake.
#
# Mirrors ``application.proxy_check.CHECK_DEPTHS``; same leaf-package rule,
# same both-directions pin.
PROXY_FETCH_CHECK_DEPTH_DEFAULT = "tls"
PROXY_FETCH_CHECK_DEPTH_NAMES: tuple[str, ...] = ("tls", "request")
# How long the TCP step of a fetch-test waits. Short on purpose and only for
# this sweep: the commonest thing in a public list is an address that has
# stopped listening, and five seconds is the difference between a dead address
# costing five seconds and costing the full check timeout. The HTTPS leg that
# follows keeps PROXY_CHECK_TIMEOUT_SECONDS -- a tunnel that answered deserves
# the time to finish its handshake.
PROXY_FETCH_CONNECT_TIMEOUT_SECONDS_DEFAULT = 5.0
PROXY_FETCH_CONNECT_TIMEOUT_SECONDS_MIN = 1.0
PROXY_FETCH_CONNECT_TIMEOUT_SECONDS_MAX = 60.0
# How long any single leg of an address check may take -- the HTTPS handshake
# through the tunnel, and the optional HEAD that follows it. Ten seconds is
# what every release up to 7.23.0 used and is therefore the default here: this
# setting exposes the number, it does not change it. Deliberately shorter than
# the request path's own connect timeout, because a checker that waits sixty
# seconds on a dead address turns a sweep of twelve into four minutes of
# nothing. Raise it for a chain that has to reach the other side of the world;
# lower it to write off a slow address sooner.
#
# Mirrored by ``application.proxy_check.PROXY_CHECK_TIMEOUT_SECONDS``, which is
# where the check reads it -- ``config`` is a leaf package and may not import
# ``application``, so the literal lives here and that module aliases it.
PROXY_CHECK_TIMEOUT_SECONDS_DEFAULT = 10.0
PROXY_CHECK_TIMEOUT_SECONDS_MIN = 1.0
PROXY_CHECK_TIMEOUT_SECONDS_MAX = 120.0
# How hard a check tries before it calls an address dead (7.53.0). A FETCH
# re-tests its screen failures in this many confirm rounds AFTER the screen
# (user decision 1: three). Measured on 1,000 feed addresses
# (specs/PR-PROXY-CHECK-VERDICTS-AND-SPEED-SPEC.md §4.2): of the 775 a shipped
# fetch called dead, three re-tests 30 s apart at the fetch's own pace found
# 82, then 33, then 47 working. The health re-prober, "Test all" and "Add all
# working" give an address this many tries IN TOTAL, the first included -- so
# 1 there is every release before 7.53.0. The single-row Test stays one try,
# because the operator pressed it and wants an answer now.
PROXY_CHECK_CONFIRM_ATTEMPTS_DEFAULT = 3
PROXY_CHECK_CONFIRM_ATTEMPTS_MIN = 1
PROXY_CHECK_CONFIRM_ATTEMPTS_MAX = 10
# Seconds between those tries. Thirty is the spacing the measurement above
# used; zero re-tests at once.
PROXY_CHECK_CONFIRM_SPACING_SECONDS_DEFAULT = 30.0
PROXY_CHECK_CONFIRM_SPACING_SECONDS_MIN = 0.0
PROXY_CHECK_CONFIRM_SPACING_SECONDS_MAX = 600.0
# Setup time (connect + tunnel + TLS) above which a working address is called
# "slow" rather than "working". A label, never a verdict: a slow address is
# stored and selectable. 3,000 ms marked the slowest ~35 % of shipped-fetch
# passes and ~58 % of the passes the re-tests recovered.
PROXY_CHECK_SLOW_MS_DEFAULT = 3000
PROXY_CHECK_SLOW_MS_MIN = 100
PROXY_CHECK_SLOW_MS_MAX = 60000
# Whether a fetch watches this machine's own connection while it sweeps: one
# direct TLS handshake (no proxy, no request) to the destination host before
# the sweep and every 15 s during it. A failure or a handshake over 5 s pauses
# the sweep and marks nothing dead until the link answers again.
PROXY_CHECK_LINK_GUARD_DEFAULT = True
# How many addresses the background health re-prober has in flight. This is NOT
# the fetch sweep, and not the operator's Add either -- both of those are paced
# by PROXY_FETCH_TEST_CONCURRENCY. This one number belongs to the loop that
# re-tests benched addresses until they pass, which is its only consumer
# (``runtime.proxy_check_timer``).
#
# Four is what 7.19.0 through 7.23.0 used and is the default for that reason.
# The ceiling is 128 rather than the fetch sweep's 500 because these checks
# always send the full end-to-end request to a provider's own host, and a
# hundred of those at once from one machine is already a lot to ask of it.
PROXY_CHECK_MAX_CONCURRENCY_DEFAULT = 4
PROXY_CHECK_MAX_CONCURRENCY_MIN = 1
PROXY_CHECK_MAX_CONCURRENCY_MAX = 128
# How long one proxy feed has to answer before it is written off for that pass.
# Fifteen seconds is 7.20.0's number and the shipped default. Feeds are read
# one after another, so this bounds the Fetch button by the number of feeds
# rather than by the patience of the slowest list. Raise it for a big list on
# a slow mirror; a public list that cannot answer at all is not the one to
# build a chain on.
PROXY_FEED_TIMEOUT_SECONDS_DEFAULT = 15.0
PROXY_FEED_TIMEOUT_SECONDS_MIN = 1.0
PROXY_FEED_TIMEOUT_SECONDS_MAX = 300.0
# How many feed URLs the store will hold. Twenty is what the Proxying page has
# enforced since 7.20.0 and is the default. It is a bound on a list a person
# types, not on anything the network does: each feed is one request per refresh
# pass, so a hundred feeds is a hundred requests to strangers every time the
# loop runs. Raise it if you have the lists; the ceiling of 1000 is a sanity
# bound on a pasted document, not an opinion.
PROXY_FEED_MAX_DEFAULT = 20
PROXY_FEED_MAX_MIN = 1
PROXY_FEED_MAX_MAX = 1000
# How many addresses one bulk Add or Discard may carry. A hundred is 7.21.0's
# number and the default. It bounds a single HTTP request's body and the sweep
# it starts, not the number of addresses a chain may hold -- that has been
# unlimited since 7.19.0, and "Add all working" on a big fetch is exactly the
# press this ceiling used to refuse. Raise it to add a whole sweep at once.
PROXY_CANDIDATE_BULK_MAX_DEFAULT = 100
PROXY_CANDIDATE_BULK_MAX_MIN = 1
PROXY_CANDIDATE_BULK_MAX_MAX = 100000
# How often a running fetch writes what has passed so far. Five seconds is
# 7.22.0's number and the default. Since 7.22.0 a sweep persists in batches
# while it runs rather than only at the end, so a server killed mid-sweep
# keeps the addresses it had already proven; this is the longest a proven
# address may sit unwritten. Lower it to lose less to a crash, raise it to
# write the store less often while a long sweep runs.
PROXY_FETCH_PERSIST_INTERVAL_SECONDS_DEFAULT = 5.0
PROXY_FETCH_PERSIST_INTERVAL_SECONDS_MIN = 0.5
PROXY_FETCH_PERSIST_INTERVAL_SECONDS_MAX = 300.0
# How long an address is benched for a *triggering* failure -- the upstream
# answered with a class the operator armed -- when the provider published no
# wait of its own. Five minutes is what 7.19.0 through 7.31.0 used and is the
# default here: this setting exposes the number, it does not change it. Long
# enough that a per-address allowance has a chance of rolling over, short
# enough that a chain of two recovers inside one working session. 0 means an
# address is never benched for a trigger the provider did not time itself,
# the same reading RATE_LIMIT_COOLDOWN_SECONDS has for a credential.
#
# Mirrored by ``core.proxy_rotation.PROXY_COOLDOWN_SECONDS_DEFAULT``, which is
# where the engine reads it -- ``config`` is a leaf package and may not import
# ``core``, so the literal lives in both and is pinned in both directions.
PROXY_COOLDOWN_SECONDS_DEFAULT = 300.0
PROXY_COOLDOWN_SECONDS_MIN = 0.0
PROXY_COOLDOWN_SECONDS_MAX = 86400.0
# The ceiling on a wait the provider *did* publish for an address. An hour,
# not a day: the credential pool's day-long cap exists because a key the host
# refused until midnight really is refused until midnight, while an address is
# one of several and the cheap move is to try it again. 7.19.0's number,
# unchanged.
PROXY_COOLDOWN_MAX_SECONDS_DEFAULT = 3600.0
PROXY_COOLDOWN_MAX_SECONDS_MIN = 1.0
PROXY_COOLDOWN_MAX_SECONDS_MAX = 86400.0
# The reachability ladder: how long a *dead* address sits out before it is
# re-probed, one step per consecutive failure and clamped at the last entry.
# Comma-separated seconds, the same shape CREDENTIAL_LOCKOUT_TIERS uses.
# "60,300,3600" is 7.19.0's ladder written out, so the default changes nothing.
PROXY_REACHABILITY_TIERS_DEFAULT = "60,300,3600"
# What a 429 on a pooled credential means. True: it benches the (key, model)
# pair and the executor moves to another model on the SAME provider first,
# because a gateway that limits one model usually still answers another on the
# same key in the same second. Nothing sleeps, no reactive block is installed
# and no key is rotated for that 429. False restores 6.19.0 exactly --
# retry-then-rotate, whole-key bench, and the backoff ladder in between, which
# on one measured request spent 51 of its 57 seconds asleep.
RATE_LIMIT_ROUTES_AROUND_MODEL_DEFAULT = True
# When the chain holds no other model on the rate-limited provider, one cheap
# question decides whether the 429 was about the model or about the key. It is
# bounded by its own clock, in the executor, and expiry means "inconclusive".
CREDENTIAL_PROBE_TIMEOUT_SECONDS_DEFAULT = 5.0
# The probe asks for nothing worth paying for: enough to see a status.
CREDENTIAL_PROBE_MAX_TOKENS = 16
# Stepping a cooled-down model over costs the chain a slot, so the wait has to
# outlive the hop it saves before routing is worth doing.
FALLBACK_COOLDOWN_STEP_OVER_FLOOR_DEFAULT = 5.0
# Exponential backoff between one provider's own retries of a 429 or 5xx:
# first wait, ceiling, and the random spread added to each so a pool of
# clients does not retry in lockstep. The ceiling is the longest SINGLE wait,
# and every one of those waits is spent before the fallback chain is consulted
# at all, so it is bounded by what a caller will wait rather than by what an
# upstream limit takes to clear: at 60 the ladder ran 2/4/8/16 per key, which
# measured ~100s across a three-key pool while the first-token deadline kept
# ticking. Since 6.20.0 only a 5xx or a transport fault ever walks it.
# The client-side pace MCC applies of its own accord, per provider, before a
# request is sent. 0 -- the default since 6.62.0 -- means it applies none.
# It shipped at 40 requests per 60 seconds against limits no provider had
# published, and because every routing attempt spends a slot, a route whose
# measured attempt count was 3.0 began throttling after ~13 client requests a
# minute: 24 concurrent requests measured p50 938 ms against p95 54 397 ms, all
# of it MCC waiting for its own window. Providers answer 429 with a Retry-After
# when they mean it, and the reactive block obeys that. A positive value is
# still honoured exactly as before, for a metered key an operator wants paced.
# 300 per 2s (150/s per provider) is far above any interactive volume, so it
# throttles nothing a person can generate while still capping a runaway loop.
PROVIDER_RATE_LIMIT_DEFAULT = 300
# The window a positive limit is counted over. Meaningless while the limit
# is 0, and 0 is not a window, so this one has no off value.
PROVIDER_RATE_WINDOW_DEFAULT = 2
PROVIDER_RETRY_BACKOFF_BASE_SECONDS_DEFAULT = 2.0
# A 10s backoff inside an interactive request is indistinguishable from a
# hang; 5s is the longest wait that still reads as "retrying". The jitter is
# kept below the backoff so spreading clients cannot outlast the wait itself.
PROVIDER_RETRY_BACKOFF_MAX_SECONDS_DEFAULT = 5.0
PROVIDER_RETRY_BACKOFF_JITTER_SECONDS_DEFAULT = 0.5

# Graceful shutdown budget (seconds). Since 6.41.0 this bounds the WHOLE stop,
# not one wait inside it: at the instant a stop is requested it becomes a single
# What the server does when its configured port is already held at start.
# ``always`` stops the holder and takes the port -- the user's rule, and the
# only one that recovers from MCC's own leftovers without a human. ``mcc-only``
# limits that to processes this install can positively identify as its own;
# ``never`` is the pre-6.59.0 behaviour, which was to diagnose and exit.
SERVER_PORT_TAKEOVER_DEFAULT = "always"
SERVER_PORT_TAKEOVER_CHOICES = ("always", "mcc-only", "never")

# How quiet another MCC server's heartbeat must go before this install will
# even use the word "stale" about it. The request log touches a session row
# every 30 seconds, so the default is thirty missed beats -- deliberately far
# past any plausible pause, because the cost of calling a busy server stale is
# a user's work stopped mid-flight and the cost of waiting is a log line.
SERVER_STALE_SESSION_SECONDS_DEFAULT = 900.0

# What the server does about OTHER My Claude Code servers it finds running when
# it starts, and what an install does about one holding a file it wants.
# ``report`` names them in the log and in the desktop status document and
# stops nothing at all -- the default, and the only safe default: a server that
# owns no listening socket in one scan may be starting, draining, or streaming
# an answer to a request it accepted before the socket closed. ``stop`` also
# stops the ones this install can *prove* are finished, which is a much smaller
# set than "owns no socket": see core/server_inventory.py for the two
# arguments that qualify.
SERVER_STALE_SERVER_ACTION_DEFAULT = "report"
SERVER_STALE_SERVER_ACTION_CHOICES = ("report", "stop")

# wall-clock deadline (core/stop_deadline.py) shared by uvicorn's connection
# drain, the streaming-response cleanup, the provider-generation drain and the
# ASGI lifespan shutdown. A small fixed teardown margin sits past it for the
# forced close, and a watchdog hard-exits the process one beat after that, so a
# stop takes at most this number plus a few seconds however the time was spent.
# New requests are refused with 503 from the same instant, so a busy client
# cannot extend the drain. This is a deployment choice, not a protocol fact, so
# it is a configurable, bounded Settings field (see config/limits.py).
#
# Grounding for the default: it used to be 300s, chosen to sit just over the
# p99.9 whole-request duration (255.7s) measured on a ~51,000-request log, so
# that a handoff let nearly every healthy request drain. That reasoning was
# sound while the number bounded only uvicorn's connection wait and the rest of
# the stop was unbounded anyway -- but as the bound on the whole stop it is the
# time an operator waits, staring at a tray icon, for a restart to happen. 20s
# is the trade taken instead: an update or reload completes in a handful of
# seconds, and the rare request still running at 20s is cut. Raise it if long
# requests matter more than a fast restart; the range is 1s to 600s.
SERVER_GRACEFUL_SHUTDOWN_SECONDS_DEFAULT = 20.0

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

# Per-request cost estimation.
# How the pricing ladder may be walked. Mirrors ``application.cost.COST_MODES``,
# which cannot be imported here because ``config`` imports nothing.
COST_ESTIMATION_MODE_DEFAULT = "auto"
COST_ESTIMATION_MODES = ("auto", "reported_only", "computed_only")

# Request log storage.
# Sized for a log worth reading months later rather than for the smallest
# possible file: the rows are zstd-compressed and 700k of them cost a few
# hundred megabytes, which is the right trade for a local proxy on a laptop.
REQUEST_LOG_MAX_ROWS_DEFAULT = 700_000
REQUEST_LOG_TEXT_MAX_CHARS_DEFAULT = 10_000_000
# How much of each outbound request body the log stores. The cap bounds the
# stored *message and tool structure* only: sampling and reasoning parameters
# are always stored whole, because a cut knob is unrecoverable and a cut turn
# list is not. Mirrors ``core.wire_capture.DEFAULT_WIRE_BODY_MAX_CHARS``,
# which cannot be imported here because ``core`` may not import ``config``.
REQUEST_LOG_WIRE_BODY_MAX_CHARS_DEFAULT = 8_000
# How much of each upstream error body the retry ladder keeps, per try. Small
# on purpose: a ladder is bounded evidence, not a log. Mirrors
# ``core.upstream_ladder.DEFAULT_LADDER_BODY_MAX_CHARS``, which cannot be
# imported here because ``core`` may not import ``config``.
REQUEST_LOG_LADDER_BODY_MAX_CHARS_DEFAULT = 800
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
DESKTOP_SERVER_START_TIMEOUT_DEFAULT = 20.0
# How many *extra* start attempts follow the first one when a spawned server
# has not answered inside ``DESKTOP_SERVER_START_TIMEOUT_DEFAULT``. Two, so a
# start gets 3 x 20 s = 60 s of probing before anything that looks like a
# failure is shown -- a real configuration here takes 22-25 s to bind, which a
# single 20 s budget cannot fit, and the window used to park on a Retry button
# the moment that budget expired. Retries never mean "spawn again": a child
# that is still running is still coming up, and a second server would only
# lose the bind race. Zero restores the pre-6.58.1 single attempt.
DESKTOP_SERVER_START_RETRIES_DEFAULT = 2
DESKTOP_ADMIN_REQUEST_TIMEOUT_DEFAULT = 5.0
DESKTOP_ACTIVATION_POLL_SECONDS_DEFAULT = 1.0
DESKTOP_HEALTH_POLL_SECONDS_DEFAULT = 5.0
DESKTOP_HEALTH_FAILURE_THRESHOLD_DEFAULT = 3
# How often a client waiting out a restart re-reads the whole status document
# instead of only re-probing /health. 30s is roughly every sixth poll at the
# default 5s health poll: often enough that a server nobody is going to restart
# is noticed in well under a minute, rare enough that a 17-minute reconnect
# costs about 34 short-lived child processes rather than 200.
DESKTOP_RECONNECT_RESTATUS_SECONDS_DEFAULT = 30.0
# The desktop app's lifecycle tick: how often it probes the server, and -- when
# the server is dead -- how often it starts one. Decision Q4 (2026-09-08), and
# the number is the user's: "probe every 10 seconds forever; if the server is
# dead, FORCE start it on that tick." There is no attempt cap above it and no
# exponential backoff beyond it, which is why the page can honestly say "next
# start attempt in M s" instead of parking on a Retry button.
# How often the server measures its own event loop, in milliseconds. One task
# asks for this much sleep and records how much later than that it woke; the
# difference is the only honest measure of how long a gesture held the loop.
# Ten times a second is small enough that half a second of lateness is five
# missed beats rather than a rounding error, and cheap enough to be free.
HEALTH_HEARTBEAT_INTERVAL_MS_DEFAULT = 100
# How late that beat has to be before /health says so. Half a second is well
# above any healthy scheduling jitter on the machines this runs on and well
# below the shortest probe timeout any client uses, so a busy answer means the
# loop really was held. 0 turns the marker off entirely without touching the
# beat, which is the measurement.
HEALTH_BUSY_LAG_MS_DEFAULT = 500
# The stuck-request watchdog. On by default, because the whole point of it is
# to be already running the next time a request goes quiet: the 2026-09-16 park
# is unexplained precisely because nothing was watching and the log that would
# have said rotated away.
REQUEST_WATCHDOG_ENABLED_DEFAULT = True
# How long a request may make no progress at all before its stack is written
# down. Chosen from this operator's own log rather than from taste: legitimate
# first-token waits reach 190 s and streams run past 300 s, and the client's
# own stream-idle watchdog is 300 s -- so a request that has been *completely
# still* for five minutes has already outlived the deadline its own client
# would have applied. Lower it to 5 or 10 when reproducing a stall on purpose;
# 0 stops the watchdog reporting without stopping the registry, so
# ``/admin/api/tasks/stacks`` still answers.
REQUEST_WATCHDOG_STALL_SECONDS_DEFAULT = 300
# How often the watchdog looks. One pass is a dict snapshot plus one cheap
# progress read per in-flight request, and it builds a stack only for a request
# that has already crossed the threshold; thirty seconds is a tenth of the
# default threshold, which is fine granularity for a five-minute fact.
REQUEST_WATCHDOG_INTERVAL_SECONDS_DEFAULT = 30
# How large ``logs/stuck-requests.jsonl`` may grow before it is rotated, in
# megabytes. Bounded for the same reason the start log was bounded in 7.10.1:
# a diagnostic file that nobody prunes is a diagnostic file that eventually
# fills a disk. Rotated copies are kept under ``SERVER_LOG_RETAIN_FILES``, the
# cap that already governs every other file in that directory. 0 keeps the
# WARNING line in ``server.log`` and writes no JSONL at all.
REQUEST_WATCHDOG_LOG_MAX_MB_DEFAULT = 5

DESKTOP_TICK_SECONDS_DEFAULT = 10.0
# The shortest gap between two starts. Equal to the tick on purpose: Q4 asks
# for one attempt per tick. An operator whose server crash-loops on start can
# raise this without slowing the probe -- the two used to be the same knob and
# could not be separated.
DESKTOP_START_BACKOFF_SECONDS_DEFAULT = 10.0
# How long one /health probe may take. One number, in one place, replacing the
# two constants that were meant to be equal and were not: the launchers used
# 1.5 s and the desktop shell compiled in its own 1.5 s, so changing one moved
# only half the behaviour.
DESKTOP_HEALTH_PROBE_TIMEOUT_DEFAULT = 1.5
# How long an unidentifiable process may hold the port before the desktop app
# calls it foreign and stops starting servers into it (audit BUG-5). Longer
# than a normal start, because an unidentifiable holder during our own startup
# is overwhelmingly us -- which is the mistake this window used to make, on
# this user's machine, about this user's own python.exe.
DESKTOP_FOREIGN_GRACE_SECONDS_DEFAULT = 45.0
# How long the desktop app leaves a server of its own alone after it last
# answered, whatever a later probe says. The 2026-09-18 report is this number:
# a 300-address bulk add held the server's event loop for longer than one
# 1.5 s /health probe, the window read a single late answer as "the server is
# gone", started a second one, and the second took the port from the first by
# pid. Six times between 09-16 and 09-18, with no crash anywhere in the logs.
# A server that answered a moment ago is busy, and busy is not a reason to
# replace anything. Fifteen seconds is the floor; the escalating ladder below
# is where the rest of the tolerance comes from. Zero restores the old
# behaviour exactly.
DESKTOP_BUSY_GRACE_SECONDS_DEFAULT = 15.0
# The escalating /health timeouts the desktop app uses once it has seen a
# server answer: one per consecutive failed probe, the last repeating for
# ever after. 5 + 10 + 15 is about thirty seconds of patience before a server
# whose process is alive is called gone -- roughly twice the longest
# event-loop hold measured on the reporting machine (1.6-9.7 s per sweep).
#
# It does not replace DESKTOP_HEALTH_PROBE_TIMEOUT. That one still times every
# probe taken before the window has ever seen this server answer, so a cold
# start, a refused connection and a genuinely dead server all cost exactly
# what they cost today.
DESKTOP_HEALTH_PROBE_TIMEOUTS_DEFAULT = "5,10,15"

# How long the desktop app may wait for one `mcc-desktop --print-status` before
# it gives up on that read and paints something. Out of the shell's binary in
# 6.61.0 (audit S5.4): it decides whether a slow machine gets a window at all,
# which is a property of the machine and not of the binary.
DESKTOP_STATUS_WALL_SECONDS_DEFAULT = 15.0
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

# How an image a tool handed back travels to a non-Anthropic model.
# `auto` follows the model's own published capability -- attach unless the
# model is known to reject images -- which is the same `is not False` test
# vision routing applies. `attach` always sends it, `strip` never does.
# There is deliberately no value for the pre-6.49.0 behaviour of sending
# the base64 as text: nobody should be able to ask for that.
TOOL_RESULT_IMAGE_DELIVERY_NAMES: frozenset[str] = frozenset(
    {"auto", "attach", "strip"}
)
TOOL_RESULT_IMAGE_DELIVERY_DEFAULT = "auto"

# How much fidelity an OpenAI-family host is asked to spend on one picture.
# `auto` emits no `detail` key at all, which is what every release before
# 6.53.0 sent and what OpenAI applies when the field is absent. `low` bills the
# base tokens only and the model sees a thumbnail, so it is a large, silent
# fidelity cut and can never be the default. The value never reaches the token
# estimator: an estimate keyed on a field the Anthropic protocol does not carry
# is how LiteLLM ends up charging every Anthropic image a flat 85 tokens.
IMAGE_DETAIL_NAMES: frozenset[str] = frozenset({"auto", "low", "high"})
IMAGE_DETAIL_DEFAULT = "auto"

# The longest edge, in pixels, an outbound image may have. The default is the
# number Anthropic itself resizes to and the number Claude Code ships
# (`maxTargetPx: 1568`, `maxTargetTokens: 1568`); combined with the destination
# family's own token budget it turns a 1920x1080 screenshot into 1456x819,
# which is the size Anthropic would have billed for anyway. 0 turns resizing
# off entirely and sends what the client sent, byte for byte.
IMAGE_MAX_LONG_EDGE_DEFAULT = 1568

# JPEG quality for a re-encode on the way out. 0 -- the default -- means never
# change the format: a PNG stays a PNG. The images in question are screenshots
# of text and code, which is the worst case for JPEG ringing, so this is opt-in
# only, and it is skipped outright for any image carrying an alpha channel.
IMAGE_JPEG_QUALITY_DEFAULT = 0

# What the vision adapter does with a request whose images the route's own
# model is published as unable to read.
# `route` sends the whole request to MODEL_VISION and lets that model answer
# -- the behaviour every release up to 6.50.1 had, and the default, because a
# mode toggle that changes what happens on upgrade is a surprise rather than a
# toggle. `describe` asks MODEL_VISION what each picture shows, puts its words
# in the picture's place, and lets the model the route actually picked answer
# the question it was asked.
VISION_ADAPTER_MODE_NAMES: frozenset[str] = frozenset({"route", "describe"})
VISION_ADAPTER_MODE_DEFAULT = "route"

# How many images of one request describe mode has in flight at once. Three is
# what 6.51.0 through 7.31.0 used and is the default here: the setting exposes
# the number, it does not change it. Concurrency is what keeps a
# five-screenshot turn from costing five round trips in series; the bound is
# what keeps it from opening five upstream connections on a provider that
# meters by concurrency. 1 describes them strictly one at a time.
DESCRIBE_CONCURRENCY_DEFAULT = 3
DESCRIBE_CONCURRENCY_MIN = 1
DESCRIBE_CONCURRENCY_MAX = 64

# --------------------------------------------------------------------------
# Catalogue caches and learned facts
#
# Every number below is the literal the module that reads it shipped with, so
# exposing them moves nothing. They are the two model catalogues MCC keeps on
# disk and the three clocks the learned-fact store runs.
# --------------------------------------------------------------------------
# How long the models.dev catalogue on disk is treated as fresh. A day is what
# 6.x through 7.31.0 used. Past it the next refresh revalidates with the
# server's ETag rather than re-downloading blindly, so a longer TTL saves a
# conditional request and a shorter one asks more often; nothing is deleted
# either way. 0 means "always stale", i.e. revalidate on every pass.
MODELS_DEV_CACHE_TTL_SECONDS_DEFAULT = 86_400
MODELS_DEV_CACHE_TTL_SECONDS_MIN = 0
MODELS_DEV_CACHE_TTL_SECONDS_MAX = 31_536_000
# How long one models.dev fetch may take before that pass is written off. Ten
# seconds, unchanged. The catalogue is a convenience, never the request path,
# so the honest failure is "this pass did not land" rather than a long wait.
MODELS_DEV_FETCH_TIMEOUT_SECONDS_DEFAULT = 10.0
MODELS_DEV_FETCH_TIMEOUT_SECONDS_MIN = 1.0
MODELS_DEV_FETCH_TIMEOUT_SECONDS_MAX = 300.0
# The same two numbers for the LiteLLM price table, which is the other half of
# what a request costs. Identical defaults, separate settings: the price table
# is 2.3 MB and the catalogue is not, and an operator who wants prices checked
# more often rarely wants capabilities checked more often too.
LITELLM_CACHE_TTL_SECONDS_DEFAULT = 86_400
LITELLM_CACHE_TTL_SECONDS_MIN = 0
LITELLM_CACHE_TTL_SECONDS_MAX = 31_536_000
LITELLM_FETCH_TIMEOUT_SECONDS_DEFAULT = 10.0
LITELLM_FETCH_TIMEOUT_SECONDS_MIN = 1.0
LITELLM_FETCH_TIMEOUT_SECONDS_MAX = 300.0
# The three clocks of the learned-fact store, one per evidence class. A fact
# older than its class's TTL stops being applied and is asked again.
#
# STATED: a cap or an enum the host published in its own sentence. Thirty days.
# INFERRED: a refusal proven by a retry that worked once a field was dropped.
# Seven days -- weaker evidence, shorter clock.
# WITHHELD: a model id that went missing from a catalogue. Seventy-two hours,
# the weakest negative here: a 404 that was really an outage, or a model the
# vendor has since launched, must not be a permanent hole in the catalogue.
#
# All three are read per fact rather than at startup, so a change applies to
# the next row the store reads without a restart.
STATED_FACT_TTL_SECONDS_DEFAULT = 2_592_000.0
INFERRED_FACT_TTL_SECONDS_DEFAULT = 604_800.0
WITHHELD_FACT_TTL_SECONDS_DEFAULT = 259_200.0
# One shared bound for all three. The floor is 60 rather than 0 because a TTL
# of zero would expire every fact the instant it was written, which is not
# "learn less" but "never learn"; the ceiling is a year.
FACT_TTL_SECONDS_MIN = 60.0
FACT_TTL_SECONDS_MAX = 31_536_000.0

# Nous Portal rejects an API-key request that carries no `tags` array with a
# `user=` entry: HTTP 400 "This request is not valid. Check the model name and
# other parameters. Additional info: missing tags". OAuth callers are identified
# by their bearer token instead, which is why the requirement is undocumented in
# the OpenAPI spec. The value after `user=` is free-form; only the prefix is
# mandatory. Enforcement began 2026-08-27, when every previously-working
# `tencent/hy3:free` request started failing.
NOUS_PORTAL_USER_TAG_DEFAULT = "user=my-claude-code"

# Seconds a launcher gives the server to build a harness catalogue document
# that is not on disk yet. It is deliberately NOT the health-preflight budget:
# ``GET /health`` answers in milliseconds and 1.5s is a generous budget for it,
# while ``GET /admin/api/catalogue-models`` serialises every registered
# harness's document from the resolution ladder and, measured on a real install
# with 292 routable models, takes 1.8-4.0s. Sharing the preflight constant made
# that fetch fail on every single launch, and because only the launcher could
# create the file and the server's fan-out only refreshed files that already
# existed, the failure was permanent rather than transient.
#
# 20s because it is a cold-start cost paid at most once per harness -- the
# steady state reads the file the server maintains and issues no request at all
# -- and because the same route on a cold models.dev index measured 5s of
# provenance work before that walk was made opt-in. A budget that fails on a
# first run of a busy install buys nothing; a launch that waits a few seconds
# once and then never again costs nothing worth naming.
CATALOGUE_FETCH_TIMEOUT_SECONDS = 20.0


# Which client MCC presents itself as to OpenCode Zen and OpenCode Go.
#
# Those two hosts are the first this proxy has met that read identity headers
# off the request and act on them. Measured 2026-09-10: the Zen free tier
# answers a request carrying only a bearer token with 400 MissingSessionID,
# and the same request carrying the identity with 200. So this is not a
# preference -- without it the free tier does not answer at all.
#
# The header the host was measured acting on is the conversation id, which
# both settings below send. What this one chooses is the *claim*: `opencode`
# presents the official client's user-agent and client id, `mcc` presents this
# proxy's own name and version. A deliberately invalid client id was answered
# 200, so the truthful answer is not known to cost anything -- but the vendor
# documents no rule either way, so the choice is written down here rather
# than assumed, and the default is the one that reproduces a working client.
OPENCODE_CLIENT_IDENTITY_DEFAULT = "opencode"
OPENCODE_CLIENT_IDENTITY_CHOICES = ("opencode", "mcc")

# The OpenCode release MCC names when it cannot read one off this machine.
#
# A version string in a user-agent is only worth sending if it is a version
# that exists: an invented one is both a false claim and a fingerprint. So the
# value is read from the installed `opencode-ai` package when there is one, and
# this pinned constant -- the release verified on 2026-09-10, whose own binary
# emits `opencode/1.18.30` -- is what a machine without OpenCode installed
# sends. `OPENCODE_CLIENT_VERSION` overrides both.
OPENCODE_CLIENT_VERSION_FALLBACK = "1.18.30"

# An operator's pin for the version above. Empty -- the default -- means "read
# it from this machine, and fall back to the pinned release".
OPENCODE_CLIENT_VERSION_DEFAULT = ""

# The other two segments of the real client's user-agent.
#
# `opencode/<ver>` is the only segment the client's own source writes. The
# other two are appended underneath by the ai-sdk fetch wrapper, which sets
# `user-agent` to `[existing, "ai-sdk/provider-utils/<ver>",
# runtimeEnvironmentUserAgent()].join(" ")`. Captured off the real
# `opencode-ai@1.18.31` wire on 2026-09-19 (scratch HOME, provider baseURL
# pointed at a local recorder):
#
#     opencode/1.18.31 ai-sdk/provider-utils/4.0.40 runtime/bun/1.3.14
#
# Neither segment can be read off this machine: the ai-sdk version lives
# inside the compiled bundle and the bun version is the runtime that bundle
# was built with, so unlike the release above there is nothing on disk to
# prefer. They are pinned, and overridable, for the reason the survey in
# `specs/INVESTIGATION-ZEN-403-FREETIER.md` gives: one env var is the whole
# cost of following the vendor the next time the floor moves.
#
# Sending them is not what lifts the 403 -- probe B says the three-segment
# form alone is still refused -- it removes the last known divergence between
# what MCC puts on the wire and what the client it names puts on the wire.
OPENCODE_CLIENT_AI_SDK_VERSION_FALLBACK = "4.0.40"
OPENCODE_CLIENT_RUNTIME_FALLBACK = "bun/1.3.14"

# Operator pins for the two segments above. Empty -- the default -- means
# "send the pinned segment". A single space or any other blank-looking value
# is normalised away rather than emitted into a header.
OPENCODE_CLIENT_AI_SDK_VERSION_DEFAULT = ""
OPENCODE_CLIENT_RUNTIME_DEFAULT = ""

# The Zen and Go models that are on the free tier without saying so in their
# id.
#
# The free-tier gate (2026-09-18, see
# `providers/openai_chat/opencode_catalogue.py`) refuses any request whose
# tool catalogue is not OpenCode's own, and MCC answers it by translating tool
# names -- but only for models actually on that tier, because every other
# model is entitled to the request MCC has always sent. Most free Zen models
# carry `-free` in the id and need no help. `big-pickle` is free and untagged,
# which is why this list exists and why it is a setting rather than a
# constant: the vendor's free roster changes without a release, and so should
# this.
OPENCODE_FREE_TIER_MODELS_DEFAULT = "big-pickle"

# Which credential a *free* Zen model is fetched with.
#
# OpenCode's free-usage limiter is keyed on the API key, not on the source
# address: on 2026-09-20 one machine, one exit IP and one minute produced a
# 429 on the operator's key at 00:43:49Z, a 200 on `public` at 00:44:16Z and
# another 429 on the operator's key at 00:44:56Z
# (`specs/INVESTIGATION-ZEN-429-FREEUSAGELIMIT.md`). The official client with
# no credential configured sends the literal key `public` and prunes every
# priced model -- `apiKey:"public"` appears twice in `opencode-ai@1.18.31` --
# so `public` is the vendor's own anonymous tenant rather than a trick.
#
# `public` (the default) spends that shared anonymous bucket on free models
# and leaves the operator's own free allowance unspent; `key` is the opt-out
# for an operator who would rather spend their own. Paid models are never
# affected: `public` cannot buy them, and the operator's key is what makes
# them reachable at all.
OPENCODE_FREE_TIER_CREDENTIAL_DEFAULT = "public"
