# Routing, limits and optimizer reference

The subsections the README used to carry inline: key rotation, fallback chains, output budgets, limits and resilience, and the token optimizer. Moved out of the README in 6.57.0; nothing here changed. Task-oriented versions of the same material are in the [Usage Guide](./USAGE.md).

### Multi-Key Rotation

Put multiple API keys in one variable, comma-separated, and choose a policy with `{ENV}_ROTATION`:

```bash
OPENROUTER_API_KEY="sk-or-key1,sk-or-key2,sk-or-key3"
OPENROUTER_API_KEY_ROTATION=round_robin
```

Policies:

| Policy | Behavior |
| --- | --- |
| `single` | Always the first key (default when one key is set). |
| `round_robin` | Spread requests across healthy keys in turn. |
| `least_used` | Healthy key with the fewest requests goes first. |
| `failover` (alias `on_error`) | Stick to the first healthy key until it fails, then move to the next (default when multiple keys are set). |

Each key gets its own upstream client and its own rate-limit window, so one key saturating or stalling never throttles the others.

**Health model — a key is only ever judged on signals about the key.** There are exactly three:

- **401/403** — the provider rejected the credential. It is locked out on an escalating ladder (`CREDENTIAL_LOCKOUT_TIERS`, 5 min → 1 h → 24 h by default), on its own counter.
- **429** — the credential is throttled **for that model**. The (key, model) pair is benched for exactly as long as the provider asked via `Retry-After` / `x-ratelimit-reset-*`, or for `RATE_LIMIT_COOLDOWN_SECONDS` when it sent no header, capped at one hour. The key stays `HEALTHY` and every other model on it keeps serving, because a gateway that limits one model has said nothing about the key's others — measured on NVIDIA NIM, one model 429s on all three keys inside 0.1 s while two other models answer on those same keys in the same second. The key itself is benched only once `CREDENTIAL_MODEL_BENCH_ESCALATION` (default 2) distinct models hold a live bench on it at the same time. No ladder, no escalation beyond that one step.
- **Exhausted credits** (since 6.34.0) — the provider said in words that the account behind the key has no balance left: HTTP 402, or a 400/403 whose structured error body names an explicit billing phrase (`insufficient credits`, `purchase more credits`, `insufficient balance`, `NOT_ENOUGH_BALANCE`, `quota exceeded`, `insufficient_quota`, `payment required`, `out of credits`, `credit balance is too low`, `CreditsError`). The whole key is benched for `RATE_LIMIT_COOLDOWN_SECONDS` — a wallet is not per-model — and the request rotates to the next key, then to the next model. A **bare 402 with no recognisable phrase** rotates and falls through but charges nothing: the matcher reads only the provider's own words, through the same echo-safe reader the recovery ladder uses, so a prompt that happens to contain "insufficient credits" can never bench a key.

Everything else — timeouts, 5xx, 410, and every other 4xx — leaves a key's health untouched.

| Setting | Default | What it does |
| --- | --- | --- |
| `CREDENTIAL_LOCKOUT_TIERS` | `300,3600,86400` | The escalating bench for a key the provider keeps rejecting with 401/403, in comma-separated seconds. One step per consecutive rejection, staying at the last entry. This is the only ladder left — a 429 waits exactly as long as the provider asked, and nothing else changes a key's health. |
| `RATE_LIMIT_COOLDOWN_SECONDS` | `60` | Used **only** when a 429 carries no `Retry-After`, `retry-after-ms` or `x-ratelimit-reset-*` header at all. A header always wins, and whatever the header asks for is capped at one hour. |
| `CREDENTIAL_MODEL_BENCH_ESCALATION` | `2` | How many *different* models have to be rate-limited on one key at the same time before the key itself is benched instead of just the (key, model) pair. `1` benches the whole key on every 429 (the pre-6.19.0 behaviour, and the no-redeploy rollback); `0` never escalates past the pair. |

Both live on **Admin UI → Limits & Resilience → Credential health**. Rotation *policy* is not there: it is per pool, on each provider's card.

**Everything else leaves every key untouched** — timeouts, 5xx, `410 model gone`, overloaded, 400s, context overflows, transport faults. Those are properties of the model, the request or the moment, and the same keys serve every model in a fallback chain, so charging them benched working credentials for faults they did not cause. A model that will not answer is the model's problem: the **fallback chain moves to the next model**, not the next key.

**Rotation follows the same rule.** The pool tries another key for an auth rejection, a 429, or a connection fault — cases where a different key or a different connection can genuinely help. Anything else is raised so routing can spend the time on a different model instead of on the rest of the pool.

**Availability, not just health.** A key can be perfectly healthy and still unable to serve right now: rate-limited, or out of daily budget. Rotation skips those keys and picks one that can answer immediately, instead of queueing behind a throttled key while an idle key sits unused. If *every* key is unavailable the request still goes out rather than failing — a soft guardrail should never become a self-inflicted outage.

**Provider-declared backoff.** On a 429, MCC reads the upstream's own `Retry-After`, `retry-after-ms`, and `X-RateLimit-Reset-*` headers (all the formats providers actually ship, including `6m0s` and `250ms`) and waits exactly that long, capped at an hour. Only when a provider says nothing does it fall back to a fixed minute.

**No invented ceilings.** MCC never caps a key at a number it made up. Every limit it applies comes from the provider's own response — the reset window on a 429, the status on a rejection. Providers change their limits without notice, so a hardcoded budget is wrong the moment it ships; reading what the upstream actually reports stays right.

**When rotation happens.** Exactly three cases, because they are the only ones a different key could fix: a 401/403, a 429, and a transport fault (a different key means a different connection). A timeout, a 5xx, an overload, a `410`, a plain 400 — every key in the pool talks to the same model and would meet the same answer, so those raise out of the rotating loop and the **fallback chain** gets its turn instead. One consequence is worth stating: a **timeout is not a transport fault** here. `openai.APITimeoutError` subclasses `APIConnectionError`, and reading a model that never answered as a broken socket would spend the whole pool on it, so it is excluded by name.

Failover happens before the first streamed chunk; once output has started, switching credentials would corrupt the response, so a mid-stream failure is recorded against the key but propagated to the client.

**The deliberate cost of that rule.** A key that fails with a 5xx or a dropped connection on *every* request is no longer benched — rotation tries it once per request and the chain absorbs the wasted attempt. That was the trade: on a live three-key pool, the failure classes that could have identified a dead key were the same ones benching healthy keys **1,529 times in one day** (a `410 model gone`, a rejected `top_p`, a model that stayed silent). A truly dead credential still answers 401/403, and that still locks it out.

All of this is visible and manageable from **Admin UI → Providers**: press **Configure** on a provider's card to open its key pool, which lists every key with its own health and usage, lets you add keys (one, or several comma-separated) and remove them individually, and carries the rotation policy. **Refresh models** on the card face makes a real call to the provider. For historical per-key request volume, error rate, tokens, and latency, see [Per-Key Attribution](#per-key-attribution).

Web search provider keys share the same rotation engine — see [Web Search → Multi-key rotation](#multi-key-rotation-web-search-keys).

<div align="center">
  <img src="../assets/admin-credential-health.png" alt="Credential health card showing the auth lockout ladder and the rate-limit cooldown fallback" width="820">
  <p><em>Credential health, on Limits &amp; Resilience: the two settings that can bench a key, and nothing else.</em></p>
</div>


### Fallback Chains

Every tier can carry an ordered list of stand-ins. If the model a request routes to cannot serve it, MCC tries the next entry in that tier's chain, then the next, until one answers.

| Setting | Chain used |
| --- | --- |
| `MODEL_FALLBACKS` | after `MODEL`, for any tier with no override of its own |
| `MODEL_FABLE_FALLBACKS` | after `MODEL_FABLE` |
| `MODEL_OPUS_FALLBACKS` | after `MODEL_OPUS` |
| `MODEL_SONNET_FALLBACKS` | after `MODEL_SONNET` |
| `MODEL_HAIKU_FALLBACKS` | after `MODEL_HAIKU` |

**Pausing one entry.** Each row on a route rail has a **Pause** button. A paused model keeps its place in the chain and stays fully on screen, but the router never tries it: no attempt is spent, no deadline is consumed, and the request log still lists it as *not tried* with the reason `paused`. Unlike everything else on that page a pause is written the moment you click it, with no Apply, and the status panel offers an Undo. Pausing is per route — the same model paused on Opus keeps serving Sonnet — and it is stored in `MODEL_PAUSED`, `MODEL_FABLE_PAUSED`, `MODEL_OPUS_PAUSED`, `MODEL_SONNET_PAUSED`, `MODEL_HAIKU_PAUSED` and `MODEL_VISION_PAUSED`. **Pausing is not hiding:** the Models page's visibility lists change what appears in `/v1/models` and never change routing, and these change routing and never change listings. A route whose every model is paused fails with an error naming the key rather than quietly falling through to another route.


Each is a comma-separated list of `provider/model` refs, in priority order — for example `MODEL_OPUS_FALLBACKS="cerebras/qwen-3-coder-480b,groq/moonshotai/kimi-k2"`. Edit them in **Admin UI → Model Config**, where each chain sits directly under the model it backs up. Drag a row by its grip to reorder it, Ctrl/Cmd-click or Shift-click to pick several, drag onto another tier to copy (hold Shift to move), and drop onto a card's top slot to make that model the route's own. The up/down arrows still do the same job one step at a time, Ctrl+Z undoes the last drag, and nothing is saved until you press **Apply**.

A tier with its own override uses only its own chain; the two are never merged. So `MODEL_OPUS` set means Opus tries `MODEL_OPUS` then `MODEL_OPUS_FALLBACKS`, while an unset `MODEL_SONNET` means Sonnet tries `MODEL` then `MODEL_FALLBACKS`.

**Failover stops when the client has actually seen bytes.** A model that fails while connecting, authenticating, rate-limiting, or before emitting anything is replaced silently. What counts as "seen" depends on the client:

- **Streaming requests** commit at the first chunk. A model that fails *after* it has begun answering is not replaced, because the reply is already on the wire and switching mid-answer would splice two different completions together.
- **Non-streaming requests** commit at the end. The response is assembled into a single message before the client sees anything, so a failure at *any* point still falls back, and the failed attempt's partial output is discarded with it.

Requests that name a provider and model directly (`open_router/…`) are never redirected — an explicit choice is honoured as given.

**A silent model counts as a failure too — but only once you say when.** A provider that accepts the request and then produces nothing holds it until the transport read timeout, and the chain gets its turn long after the client gave up. These settings bound it, on **Admin UI → Limits & Resilience** (deadlines on the **Deadlines** card, the last two on **Chain benching**).

> **Since 6.16.0 all four deadlines ship at `0`, meaning no limit. With the shipped zeros MCC never ends a silent or stalled upstream on its own: the fallback chain moves only on an error the provider actually returns.** A model that thinks for forty minutes is left to think; a stream that goes quiet and never resumes is left open until the transport gives up (`HTTP_READ_TIMEOUT`, 300 s by default, applied per read rather than per request). This is deliberate — a deadline that kills real work is a worse failure than a stall you can see in Analytics, and MCC is configured by the operator who runs it. **To get time-based failover, set the ones that matter to you:** `FALLBACK_FIRST_TOKEN_TIMEOUT` (silence before any output — the only one that produces a *failover*, since nothing has streamed yet), `FALLBACK_STALL_TIMEOUT` (silence after output started — ends the request; no failover is possible past the first token), `FALLBACK_TOTAL_TIMEOUT` (the whole request, across every attempt and retry). The Deadlines calculator on that page shows the resulting per-model allowance for your own chains. An install that already sets any of these keys keeps its own value; nothing is rewritten on upgrade. Every error MCC raises from one of these limits now names the env var that set it and the card that edits it.

| Setting | Default | What it does |
| --- | --- | --- |
| `FALLBACK_FIRST_TOKEN_TIMEOUT` | `0` (no limit) | The first-token deadline: seconds a model may stay silent before the next model takes over. Nothing has streamed yet, so the handover is invisible — this is the only deadline that produces a *failover* rather than an ending. `0` (shipped) waits indefinitely. When you set it, each attempt also gets an equal share of what is left of `FALLBACK_TOTAL_TIMEOUT`, counting itself and every model still behind it, and what applies is whichever is smaller — unless `FALLBACK_ATTEMPT_SHARE_FLOOR` raises that share. **Admin UI → Limits & Resilience** computes it per route for your own chains. |
| `FALLBACK_ATTEMPT_SHARE_FLOOR` | `3600` (one hour) | **Changed in 6.68.0** — was `0`. Smallest slice of `FALLBACK_TOTAL_TIMEOUT` one attempt may be cut down to. A **chain-side** allowance — it bounds each model's first-token wait, never a retry of the same model. Without it the equal share alone decides, and on a long chain it silently undercuts the deadline above: 600 ÷ 8 models = 75s, so a box reading `120` logged `produced no first token after 74.9494s`. `3600` (shipped) is an hour-wide floor that cannot fire while `FALLBACK_TOTAL_TIMEOUT` is `0` — there is no budget to divide — and becomes a real floor the moment you set a budget; `0` divides the budget equally with no floor. It is moot out of the box either way, because the budget ships at `0`. The trade once you set both: N silent models can spend up to N × this floor before the budget clamps them, and the models after that get less, or nothing — the Deadlines calculator shows both numbers and warns when the floor cannot fit. |
| `FALLBACK_TOTAL_TIMEOUT` | `0` (no limit) | Whole-request budget across every attempt, retry and recovery — the backstop for a stream that committed and then stalled. Divided between the models still to try, floored by `FALLBACK_ATTEMPT_SHARE_FLOOR`. `0` (shipped) disables it: a request may run for as long as the upstream keeps the connection open. |
| `FALLBACK_STALL_TIMEOUT` | `0` (no limit) | Seconds a stream that *has* started producing may then go quiet. Measured from the last chunk that moved the answer forward, so keepalives cannot hold a dead stream open and a model producing steadily is never cut. `0` (shipped) allows an unlimited pause. No failover is possible here — the reader has already seen the first model's words — so this ends the request rather than moving the chain. |
| `FALLBACK_END_CLEANLY_AFTER_COMMIT` | `true` | What happens when a model that has *already started answering* then fails. The chain cannot step in — the reader has seen its words — so instead of an API error printed under a half-written answer, the message is ended: the open block is closed and the client is told the answer was cut short. The session continues with a short but complete reply. Set `false` to go back to the error. |
| `FALLBACK_RESUME_AFTER_COMMIT` | `true` | Goes one step further than the row above: rather than only *ending* a half-written answer, the next model on the route is handed the text already on screen and asked to carry on from it, and its output is spliced into the same message — so the turn survives. Same chain, same bench, same budget as an ordinary fallback; nothing new is retried. Continuation is model-dependent (many models answer nothing, and one that starts the answer over is rejected rather than shown twice), and every one of those outcomes falls back to the short message above rather than an error. A half-written tool call is never continued. Set `false` to stop at the short message. |
| `FALLBACK_EJECT_SECONDS` | `10` | **Changed in 6.68.0** — was `30`. How long a benched model stays out of the chains that name it. Half a minute outlives most of the sessions that bench a model. |
| `FALLBACK_COOLDOWN_STEP_OVER_FLOOR` | `5` | Seconds of remaining rate-limit cooldown that make it worth trying the next model rather than waiting. Shorter waits are waited out, because stepping over costs the chain a slot. |

**A model that dies after it started answering ends the message, not the turn.** The commit boundary above cuts both ways: once real text has reached the client no other model can take over, and until 6.15.0 that meant every failure past that point — the stall deadline, the total budget, a mid-stream 5xx, a dropped connection — reached Claude Code as an API error under a partial answer, and the turn was dead. One incident streamed 1,333 characters, went quiet, and was reported as `API Error … stopped producing output`, with seven healthy fallback models recorded as `not tried`. Since 6.15.0 the stream is closed properly instead: the open text or thinking block is stopped, a `message_delta` carrying `stop_reason: max_tokens` says the answer was cut short rather than finished, and `message_stop` ends it. Nothing about *when* the stream is stopped changed — only what the client is handed. The answer is genuinely incomplete, and the request detail says so: the attempt still reads `failed` with its real cause, alongside `ended early after N chars`. One case still errors and cannot be rescued — a stream that stopped halfway through a tool call's arguments, because a tool call cannot be completed honestly and Claude Code would *run* it.

**And since 6.18.0 it can be finished rather than only ended.** With `FALLBACK_RESUME_AFTER_COMMIT` on (the default), the next model on the route is given the text already on screen and asked to continue from it, and its output is spliced into the same message: one envelope, one text block, one ending. There is no visible seam, deliberately — the reader gets an answer, not a report about routing — and the model change is recorded in the request detail instead, as *continued here after `<model>` stalled at N chars*. It is the same chain as an ordinary fallback: benched models are skipped, `FALLBACK_SKIP_KINDS` still ends a route, the attempt shares the same budget, and no new retry layer or deadline exists. What the second model does with the request is not guaranteed: measured across thirteen live model/host pairs, some continue cleanly, most answer nothing, and a few start the answer over — and a restart is detected and thrown away rather than shown twice. Every unusable continuation lands on the truncated message above, never on an error, which is why this can ship on.

Ejection can never empty a chain: if every model on a route is benched, they are tried in order anyway — skipping a bad model is an optimisation, refusing to try anything is an outage.

**What benches a model.** `FALLBACK_BENCH_ENABLED` is the master switch, and it **ships off**: every model in the chain is tried every time, so a model failing half its requests is still retried at chain position 0 on every single request. It reads on two pages — **Model Config**, at the top of the routing view, beside the routes it governs, and **Limits & Resilience → Chain benching**, where it gates the tuning below. They are the same setting; changing either changes both. With it off *every* control below is inert. Turn it on and `FALLBACK_BEHAVIOR` picks the evidence:

**Only model-shaped failures count.** With benching on, a failure benches a model only when it says something about the *model*: an upstream 5xx, an overloaded response, or a 401/403 from the provider. Timeouts (first-token, stall, budget), 429s, exhausted credits, `context_length` and malformed requests are facts about the *request* or the *account* — they fail identically on a healthy model and a dead one — and never bench anything. That distinction is why the switch could be turned off by default: the bench used to count everything, so one prompt larger than any model's context window ejected the entire chain and the request was answered by whichever model was left holding the 400.

| Setting | Default | What it does |
| --- | --- | --- |
| `FALLBACK_BENCH_ENABLED` | `false` | Master switch for benching. Off (the default) tries every model every time; on makes the whole card live. Set on **Model Config** or on this card — one setting, two places. |
| `FALLBACK_BEHAVIOR` | `rate_based` | `rate_based` benches on a failure rate over a window; `legacy` restores the older consecutive-count rule. |
| `FALLBACK_EJECT_WINDOW` / `FALLBACK_EJECT_FAILURE_RATE` | `10` / `0.5` | `rate_based` only: benched once 5 of the last 10 requests fail. |
| `FALLBACK_EJECT_MIN_SAMPLES` | `8` | `rate_based` only: nothing is benched until that many of its requests have been seen, so one failure on a barely-used model cannot trip it. |
| `FALLBACK_EJECT_AFTER_FAILURES` | `3` | `legacy` only: consecutive failures. `0` turns counting off. |
| `FALLBACK_RETRY_FIRST` | `skip` | `retry_once` gives the **primary only** one more attempt on a transient error before the chain moves on. An already-failed fallback is never retried. |

The card keeps the *unselected* mode's fields visible but disabled, each carrying its own note — "Not used while eject mode is `legacy`", "Not used while benching is off" — rather than dimming them and leaving you to infer why. Disabled fields are skipped by the change tracker, so switching modes cannot save the other mode's values by accident.

**A rate-limited model is routed around, not waited on.** Since 6.20.0 a `429` never sleeps and never spends a retry: the (key, model) pair is benched and the request moves to the next model on the **same provider**, because a gateway that limits one model usually still answers another on the same key in the same second. One measured request spent 51 of its 57 seconds asleep between retries of a model that was refusing in 0.2 s, with a healthy sibling one chain slot away. `RATE_LIMIT_ROUTES_AROUND_MODEL=false` restores retry-then-rotate.

**One model is still retried on a 5xx before the chain is used.** An upstream `5xx` or a dropped connection is retried against the same model on an exponential backoff — that is the one failure a second knock on the same key can fix — and four settings shape it:

| Setting | Default | What it does |
| --- | --- | --- |
| `PROVIDER_RETRY_ATTEMPTS` | `2` | **Changed in 6.68.0** — was `3`, and `5` before 6.20.0. Tries one model gets on the same key after a 5xx or a dropped connection. A 429 uses none of them. The third try lands about six seconds after the first, by which time a healthy model behind it could have answered. |
| `PROVIDER_RETRY_BACKOFF_BASE_SECONDS` | `2` | How long a provider waits before its first retry of a 5xx. Each further retry doubles it. |
| `PROVIDER_RETRY_BACKOFF_MAX_SECONDS` | `5` | **Changed in 6.68.0** — was `10`. The longest single wait — the ceiling the doubling backoff stops growing past. The chain is not consulted until the ladder is spent, so this is added to how long a request waits before another model is tried. A 429 walks no ladder at all. |
| `PROVIDER_RETRY_BACKOFF_JITTER_SECONDS` | `0.5` | **Changed in 6.68.0** — was `1`, and is kept below the ceiling above. Random spread added to each wait, so several clients hitting the same limit do not retry in lockstep. |

**A context overflow is not a malformed request.** Both usually arrive as HTTP `400`, and until 5.43.0 MCC treated every `400` the same way — as a client error that would fail identically everywhere, so the whole chain was abandoned. That is right for a malformed body and wrong for a conversation that outgrew the model's window, which is exactly the case a larger-window fallback exists to cover. Context-length failures are now classified as their own kind and fall through to the next model.

| Setting | Default | What it does |
| --- | --- | --- |
| `FALLBACK_SKIP_KINDS` | `invalid_request` | Comma-separated failure kinds that abort the chain instead of falling through. Known kinds: `invalid_request`, `model_rejected`, `context_length`, `authentication`, `permission`, `quota`, `rate_limit`, `overloaded`, `timeout`, `upstream`, `unavailable`. Since 6.46.0 `invalid_request` means only a 400 whose own message says the request is *malformed*; every other 400 is `model_rejected` and falls through. Set `FALLBACK_SKIP_KINDS=invalid_request,model_rejected,context_length` to restore the pre-5.43.0 behaviour of giving up on any `400`. `quota` is deliberately absent from the default: an account out of credits says nothing about the request, and the next key or the next model may well answer it. |

**Thinking is not answering.** A reasoning model that streams its thoughts and never writes an answer used to commit the route on its very first thought: from that moment no other model could take over, the stall guard never fired because thoughts kept arriving, and the request ran until the whole budget ended it. Measured across 21 days of real traffic, 44 of 499 budget exhaustions were a stream that had only reasoned, and 490 of the 499 never left the first model on the chain.

Since 5.50.0 reasoning is held back like an envelope frame, so the attempt stays abandonable and the next model can still answer. Two settings control it, both on **Admin UI → Limits & Resilience → Deadlines** — beside the first-token deadline they pre-empt, rather than on Model Config where the reasoning deadline used to sit:

| Setting | Default | What it does |
| --- | --- | --- |
| `FALLBACK_ON_REASONING_ONLY` | `true` | Hold reasoning back so a model that only thinks does not commit the route. The cost is that thinking no longer streams live — it arrives with the answer, or once 64 KB have accumulated. Set `false` to watch a model think in real time and accept that it commits the route. |
| `STREAM_COMMIT_HOLDBACK_CHARS` | `0` | Visible characters that must arrive before output is released to the client, *on top of* `STREAM_COMMIT_HOLDBACK_SECONDS` — both conditions, or the stream ending. While output is held a failure is still invisible, so the route can start over on the next model with nothing shown; raising it buys that for a model that writes a word and dies, and costs exactly that much time-to-first-visible-word on every request. `0` (shipped) asks only the clock. |
| `FALLBACK_REASONING_ANSWER_TIMEOUT` | `0` (no limit) | Seconds a model may think before the chain moves on. A flat allowance on purpose: the attempt's share of `FALLBACK_TOTAL_TIMEOUT` is sized for a model that has shown *nothing*, and 600s split across an eleven-model chain leaves 54s, far too little to think in. `0` (shipped) lets a thinking model run to the whole request budget — and, while that is `0` too, to no limit at all. |

Every attempt is recorded, and since 6.12.0 so is every *try* inside an attempt. **Analytics** shows the model that actually answered rather than the one the route started from, and the request detail draws the whole chain — every model the chain reached, and under each one every upstream try it made, with the status it met, the credential it used and the seconds it spent waiting — see [Route tracing](#route-tracing).

### Output Token Budget

**Each model is asked for what it can actually produce.** MCC reads the routed model's published output limit — the provider's own `/models` payload first, the [models.dev](https://models.dev) catalog second — and sizes `max_tokens` from it:

- The client asked for **less** than the limit → it gets exactly what it asked for.
- The client asked for **more** → the request is lowered to the model's maximum and a `MAX TOKENS CLAMPED` warning names the model, the ask and the limit. Sending the original value instead just buys a 400 from the provider.
- The client asked for **nothing** → the model's **full** limit is sent. A model that can write 230,400 tokens is used as one.
- The request is going to **think** → the ask is raised to the model's published limit first, and the clamps above then apply to *that*. Thinking tokens and answer tokens are spent from one `max_tokens`, so a client that sized the number for an answer unknowingly sized the thinking too — and the model, not the client, is the one that knows what it can emit. An unknown limit is never widened: a number nobody published has no standing to raise an explicit request, exactly as it has none to lower one.

For a 64,000-token ask at the `max` reasoning tier on a 262,144-output model, that is the difference between starving the answer and not:

| | before | after |
| --- | --- | --- |
| wire `max_tokens` | 64,000 | 131,072 (the model's limit, held to the ceiling) |
| answer reserve | 16,384 | 16,384 |
| thinking budget | 47,616 | 114,688 |
| answer room left | 16,384 | 16,384 |

**The rung does not widen anything — the presence of reasoning does.** `low` and `max` produce the same `max_tokens`; the rung then decides how much of that allowance the thinking may spend. On a host that takes an effort word rather than a number, nothing about the level changes at all — only the answer stops being squeezed out by the thinking in front of it. With reasoning off, the wire is byte-identical to 6.7.0 apart from the ceiling, which now applies to every request.

This replaces a flat 81,920 that every model got regardless. On real routes that number was simultaneously too high (`minimaxai/minimax-m3` and `thinkingmachines/inkling` both stop at 16,384) and too low (`tencent/hy3:free` does 128,000, `meituan/longcat-2.0:free` 131,072).

These cover what the model itself cannot answer, all on **Admin UI → Limits & Resilience → Output & thinking budgets**:

| Setting | Default | What it does |
| --- | --- | --- |
| `MAX_OUTPUT_TOKENS_UNKNOWN_DEFAULT` | `32768` | Used **only** when no source publishes a limit for the routed model. A fallback for a missing client value, never a cap on a present one — a number nobody published has no business shrinking an explicit request. |
| `MAX_OUTPUT_TOKENS_FLOOR` | `8192` | **New in 6.47.0, and it changes behaviour on update.** The smallest allowance any request is sent with — the one setting here that raises a number rather than lowering it. A client that hardcodes a tiny `max_tokens` gets a truncated answer out of a model that could have finished it. Bounded twice: never above the routed model's own published limit, and applied *before* the context headroom so it can never re-inflate a budget the context cannot hold. It stands down on an explicit `max_tokens: 0`, and the ceiling below beats it. Not the same setting as `MAX_OUTPUT_TOKENS_CONTEXT_FLOOR`. **Set `MAX_OUTPUT_TOKENS_FLOOR=0` to turn it off** and restore 6.46.0. |
| `MAX_OUTPUT_TOKENS_CEILING` | `131072` | Absolute head on every request, whatever the model can do — reasoning or not. It ships set because a thinking request is sized from the routed model's full published limit, and OpenAI/Azure-style limiters reserve `max_tokens` against the TPM bucket *before* generating — so an unbounded thinking turn on a 262,144-output model can 429 a request that would otherwise have been served. It never raises a model above its own limit. Range `0`–`1048576`; **`0` is the sentinel for "no ceiling"**, and blank resolves to the default rather than to off. |
| `MAX_OUTPUT_TOKENS_CONTEXT_MARGIN` | `1024` | Tokens reserved for the prompt when a model's output limit is as large as its whole context window — about 15% of the catalog reports exactly that, and on those, asking for the full output leaves no room for the messages. |
| `MAX_OUTPUT_TOKENS_CONTEXT_FLOOR` | `4096` | Smallest budget the reserve above may produce. A wrong or small published context can leave a handful of tokens, and a request carrying `max_tokens: 3` succeeds with a one-token answer — which looks like a useless model rather than a misconfigured catalog. Below this, the request is sent unchanged so the provider reports the real context error. `0` sends any positive headroom. |
| `REASONING_ANSWER_FLOOR_MAX` | `16384` | Most tokens ever held back from the output allowance for the visible answer while extended thinking is on. Thinking and the answer share one `max_tokens`. The reserve applied is `min(this, output // 2)`, so a 16,384-output model keeps a working thinking budget instead of zero. |
| `ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS` | `81920` | **Settable since 6.47.0** — it was a hardcoded literal threaded through every provider profile. The `max_tokens` written onto a request that reached a provider with none of its own and no published limit to take one from. A fallback, never a cap: a routed request was bound to the model's real limit long before this applies. `0` sends no `max_tokens` at all. |
| `REASONING_EFFORT_BUDGET_RATIOS` | `0.10,0.20,0.50,0.80,0.95,0.95` | **New in 6.47.0.** Share of the output allowance each effort may spend on thinking, in order: minimal, low, medium, high, xhigh, max. Six values, each strictly between 0 and 1, never decreasing. A small ratio does not disable thinking — Anthropic's 1,024-token minimum is applied afterwards and wins above an allowance of 1,025. |

**One more ceiling lives off this card.** NVIDIA NIM carries its own `max_tokens` in the nested NIM settings, and where it is set it lowers whatever the table above resolved — for NIM routes only. It ships unset, so it is inert on a default install. It is deliberately not on the Budgets card: the nested NIM group has no flat environment variable of its own, and giving it one is a change of its own rather than part of this table.

**Context is respected.** Where the provider publishes a context window, the budget is bounded by what the prompt left of it, minus the margin. If the prompt already fills the window the request is sent unchanged, so the provider reports the real error rather than MCC guessing at it.

**A provider's own rejection still wins.** Some upstreams cap output below what they publish and say so in a 400 (`max_completion_tokens must be less than or equal to 40960`). MCC parses that, retries once, and remembers the cap for that model — and a learned cap always beats a catalogue value, because it came from the deployment actually serving the request.


### Limits & Resilience

Everything above that is a number rather than a model lives on one page, **Admin UI → Limits & Resilience**. It was a single flat grid of 37 fields until 6.2.0; it is now six cards behind a sticky section rail, each stating in one line what it decides:

| Card | What it decides |
| --- | --- |
| **Output & thinking budgets** | How large one answer may be, and how it is split between thinking and the answer. A per-model limit published by the provider always wins over anything here. |
| **Deadlines** | How long one model may hold a request before the chain moves on — including `FALLBACK_REASONING_ANSWER_TIMEOUT`, which used to sit on Model Config away from the deadline it pre-empts. |
| **Chain benching** | Whether a model that keeps failing is skipped, and on what evidence. |
| **Provider retries & throughput** | How hard one model is retried, and how fast requests may leave — `HTTP_*_TIMEOUT`, `PROVIDER_RATE_*` and `PROVIDER_MAX_CONCURRENCY` moved here from Providers, where the transport ceiling underneath every deadline had no description at all. |
| **Credential health** | What one API key's failures cost it. Nothing else can bench a key. |
| **Diagnostics** | Logging and debugging flags, `LOG_LEVEL` included. |

Two groups deliberately left: the nine `REQUEST_LOG_*` settings are at the bottom of **Analytics**, and the nine `DESKTOP_*` ones on **Providers** — the page that shows the consequence owns the control. `FALLBACK_SKIP_KINDS` stays on **Model Config** with a cross-link, because it is a routing decision, not a limit.

<div align="center">
  <img src="../assets/admin-limits.png" alt="Limits and Resilience page showing the section rail and the output budgets card" width="820">
  <p><em>Limits &amp; Resilience: six cards behind a section rail, each one subsystem.</em></p>
</div>

**The number in the box is not the number you get unless the floor holds it there.** The allowance is `min(first-token deadline, this attempt's share of the total budget)`, and that share is `total ÷ models still to try`. On a ten-model Opus chain with a 600 s budget a field reading `120` gives each model 60 s — the field is a ceiling, not the allowance. `FALLBACK_ATTEMPT_SHARE_FLOOR` stops that share falling below the deadline you set, so the box means what it says; `0` is the pure equal share and `3600` (shipped since 6.68.0) is a floor wider than any interactive request. With the budget at the shipped `0` none of this arithmetic runs whatever the floor says, and the calculator agrees: every route reads **no limit**, and the headline names `HTTP_READ_TIMEOUT` as the only thing left that ends a silent model.

The trade is real and the page states it rather than hiding it: a floor of 180 s across a ten-model chain would need 1,800 s of budget, so with 600 s only the first three silent models can use the whole floor and the rest get what is left, then nothing. The Deadlines card carries a calculator that computes all of this per route from your own chains, names the floor in the formula when the floor is what decides, and warns with the fix rather than the complaint: the budget that would fit the floor, the total budget that *would* honour the deadline you configured, or that `HTTP_READ_TIMEOUT` sits below the deadline above it — in which case a slow model produces a transport error instead of a clean handover.

<div align="center">
  <img src="../assets/admin-limits-calculator.png" alt="Per-route deadline calculator showing each route's chain length and real first-token share" width="820">
  <p><em>The deadline calculator: one row per route, the real first-token share, and the budget that would honour the number you typed.</em></p>
</div>

What it deliberately does *not* model: time already spent on the request, a primary retried under `FALLBACK_RETRY_FIRST`, benched models shortening the chain, or the reasoning deadline taking over once a model starts thinking. It is the worst case for the first model on a route, not a fixed slot — time an attempt does not use flows to the models behind it.

<a id="saving-settings"></a>

### Saving Settings: Blank Means Unset

Pressing **Apply** writes only what you actually changed. Until 6.1.0 it did the opposite: the first Save of any field materialised *every* manifest default into `~/.mcc/.env` as a real value — and a value on disk outranks a code default forever, so on any install that had pressed Save once, **no shipped default could ever change again**. `FALLBACK_BENCH_ENABLED=false` outliving the release that flipped its default to `true` is the casualty people actually hit — and the same rule cuts the other way now that the default is `false` again: an install with `FALLBACK_BENCH_ENABLED=true` written into `~/.mcc/.env` keeps benching on until that line is removed.

Now the managed file is the starting point, and an untouched field is written as a commented placeholder that records what it would do:

```bash
# FALLBACK_BENCH_ENABLED= (default: false)
```

The rules that follow from that:

- **Blank means unset.** Clearing a field removes its line and the code default applies again. Every field carries its default underneath it and a **Use default** button that does exactly that.
- **Setting a field *to* its current default still writes it.** That is an explicit choice, and it survives a later change to the shipped default — which is the whole point of the distinction.
- **Unless the repo `.env` sets the key.** Then blanking writes `KEY=` to mask it where the type accepts an empty string, and Save returns a named warning where it does not — surfaced in the dashboard rather than swallowed.
- **Booleans are three-state selects** — `Default (On)` / `On` / `Off` — and selects carry an explicit `Default (…)` option. Before, an unset select rendered its first option as if you had chosen it, and merely loading the page could save it.
- A source chip reads **"set here"** for a value the managed file owns, so "this is my value" and "this is the shipped default" are never the same pixel.

No migration ran: defaults already materialised in an existing `.env` are left alone, because a value on disk is effective configuration and silently rewriting it would be the worse bug. Clear them yourself with **Use default** if you want the shipped default back.

<a id="web-search"></a>


### Token Optimizer

A dedicated dashboard page — **Admin UI → Token Optimizer** — reports what MCC kept off the wire, what is keeping it off, and what could keep more off. Every number on it is measured from your own request log. **Nothing on the page is enabled for you.**

| Panel | What it shows |
| --- | --- |
| **Ledger** | "Tokens never sent" — prompt tokens no provider ever received. Not a bill estimate: what a provider *would* have charged for the reply is unknowable and is deliberately not guessed at. |
| **Local rules** | Per-rule fire counts for the rules that answer a request inside the proxy. |
| **Candidates** | Recurring request families that no rule covers, ranked by tokens actually spent. |
| **Cache effectiveness** | Prompt-cache hit rates per provider — the largest lever on the page, and not one the optimizer controls. An em dash means the provider never reported the field, which is a different fact from reporting zero. |

**The local rules, and how to switch one off.** Each answers a request MCC can answer correctly without a provider, and each has its own kill switch — set it to `false` and that request goes upstream exactly as it did before.

| Rule | What it matches | Env key |
| --- | --- | --- |
| Title generation | Claude Code asking for a short conversation title. | `ENABLE_TITLE_GENERATION_SKIP` |
| Suggestion mode | A `[SUGGESTION MODE:` turn, which expects no model output. | `ENABLE_SUGGESTION_MODE_SKIP` |
| Model routing probe | An agent harness's startup reachability check: one `Say OK` user turn, no system text, no tools, not streaming, `max_tokens` ≤ 32. The reply echoes the model that *would* have answered — the first model on the route your fallback health registry has not benched — so a proxy silently substituting a different model is still detected. | `ENABLE_PROBE_AUTO_RESPONSE` |

**Candidates are scanned on demand only.** Pressing **Scan the log** runs a fresh, bounded scan of the request log (`GET /admin/api/requests/discover-optimizations`). It is never scheduled and never runs on page load, it proposes nothing and changes nothing about how any request is answered, and asked for more rows than its ceiling it returns `422` rather than silently sampling a subset and reporting the result as if it were complete.

The page also reports RTK's measured savings, read from `rtk gain --all --format json` and served at `GET /admin/api/rtk/gain`, and tells you when the RTK binary on your machine has drifted from the version MCC pins.

<a id="tool-result-trimming"></a>

#### Tool-Result Trimming — Off By Default, And Probably Should Stay Off

Claude Code resends the whole conversation every turn, tool results included, so one large file read is paid for again on every later turn. MCC can shorten oversized `Read`, `Grep` and `Glob` results before they reach the model. The controls live on the Token Optimizer page (they were on **Limits** before 5.48.0).

**This is not a recommended optimization, and it is not presented as one.** A controlled 24-turn experiment against a prefix-caching model found that at the shipped `TOOL_RESULT_TRIM_PROTECT_RECENT_RESULTS=2` trimming costs **10.9% more fresh input tokens than leaving it off entirely**. Rewriting bytes in the middle of an already-established prompt invalidates the prefix cache, and the cache is worth more than the bytes removed. Switching it on mid-conversation costs one near-total cache miss — a 3.8% hit rate on that turn. Break-even is a baseline cache hit rate of about **90.9%**: below that trimming wins, above it trimming loses, and a healthy provider is usually above it.

The full measured table, including why the cheaper `protect_recent_results=0` was *not* made the default, is in the docstring of [`core/anthropic/tool_result_trimming.py`](src/my_claude_code/core/anthropic/tool_result_trimming.py) and in `.env.example`. Read it there before enabling anything.

What it does **not** do:

- It does not run unless you turn it on. The master switch `ENABLE_TOOL_RESULT_TRIMMING` defaults to `false` **and** all three per-tool rules default to `off`; both have to change.
- It never touches `Bash` results — client-side compressors already own those, and two layers compressing the same bytes makes neither one's savings attributable.
- It never trims silently. Every elision carries an inline marker naming MCC as the actor, stating how much was removed, and telling the model not to describe content it did not see.
- It leaves anything ambiguous byte-for-byte alone — an unmatched or duplicated `tool_use_id`, an error result, or any content shape it does not fully understand.

Each rule has three states rather than two, and the middle one is the point:

| Setting | Default | What it does |
| --- | --- | --- |
| `ENABLE_TOOL_RESULT_TRIMMING` | `false` | Master switch. With this off, no rule runs whatever its own state. |
| `TOOL_RESULT_TRIM_READ` / `_GREP` / `_GLOB` | `off` | Per-tool state: `off`, `observe`, or `on`. **`observe` measures what the rule *would* have removed against your real traffic while the bytes on the wire stay exactly as the client sent them** — the safe way to find out whether trimming would pay for you. |
| `TOOL_RESULT_TRIM_THRESHOLD_CHARS` | `20000` | A result smaller than this is never touched. |
| `TOOL_RESULT_TRIM_KEEP_HEAD_CHARS` / `_KEEP_TAIL_CHARS` | `4000` / `4000` | How much of the start and end survive the elision. |
| `TOOL_RESULT_TRIM_PROTECT_RECENT_RESULTS` | `2` | How many of the most recent results are exempt. This is the setting the measurement above is about. |

Measure with `observe`, check your cache hit rate on the Token Optimizer page against the 90.9% break-even, and only then decide.

<a id="version--updates"></a>

