"""Which credential an OpenCode Zen request is sent under.

**Measured, 2026-09-20.** OpenCode's free-usage limiter is keyed on the API
key. One machine, one exit IP, seventy seconds: MCC on the operator's key was
refused ``429 FreeUsageLimitError`` at 00:43:49Z, the vendor's own CLI on the
literal key ``public`` was served ``200`` (29 807 tokens, ``cost: 0``) at
00:44:16Z, and MCC on the operator's key was refused again at 00:44:56Z. Per
IP, per session id and per header set are all ruled out by that table -- only
the credential separated the two outcomes. The full record is in
``specs/INVESTIGATION-ZEN-429-FREEUSAGELIMIT.md``.

``public`` is not a trick and not a guess: it is what the vendor's own client
does. ``opencode-ai@1.18.31`` ships the rule twice, once in the provider's
autoload hook and once in the catalogue reloader::

    J = Boolean(process.env.OPENCODE_API_KEY) || Boolean(auth) || Boolean(cfg);
    if (!J) for (let [Y, G] of Object.entries(Z.models)) {
        if (G.cost.input === 0) continue;   // keep the free models
        delete Z.models[Y];                 // drop every paid one
    }
    return { options: J ? {} : { apiKey: "public" } };

An unauthenticated OpenCode CLI therefore talks to Zen as
``Authorization: Bearer public`` and can reach only the zero-cost models. MCC
had no notion of ``public`` at all before this release.

**What this module does.** For ``opencode`` -- never ``opencode_go``, which is
a paid subscription endpoint with its own credential requirement -- it splits
the provider in two:

* the **paid side** is built exactly as every release before this one built
  it, from the operator's ``OPENCODE_API_KEY``, with its pool, its rotation
  policy and its health byte-for-byte unchanged;
* the **public side** is one extra credential slot, labelled ``public``, whose
  requests go out as ``Bearer public``.

A request picks a side at dispatch time from the model it routes to, through
the same predicate Z's tool-name translation uses --
:meth:`~my_claude_code.providers.openai_chat.opencode_catalogue.FreeTierToolCatalogue.applies_to`
-- and never from a model name written into a branch here. A free model goes
public; a paid model keeps the operator's key. Because the slot is its own,
a 429 on the shared bucket benches the shared bucket and leaves the operator's
key exactly as healthy as it was, and the request log, Analytics and the CSV
exports say ``public`` for those attempts rather than naming a key that was
never spent.

**Housekeeping travels with it.** The hourly discovery sweep and the Test
button both ask for the model *listing*, which names no model at all; under
``public`` they are answered on the public credential, which is the single
change that stops MCC re-confirming the operator's limit once an hour. A
listing that fails on ``public`` falls back to the operator's key once, so a
pruned or refused anonymous catalogue can never be the reason a paid Zen model
disappears from the Models page.

**The caveat, stated plainly.** ``public`` is a *shared* bucket. An operator
whose own key still has free allowance may prefer to spend it and keep the
shared one for the people who have no key at all; ``OPENCODE_FREE_TIER_CREDENTIAL=key``
is that opt-out, and it restores 7.33.0 byte for byte. The scope of the shared
bucket -- per IP, per device, global -- is not known; all that was measured is
that it was not exhausted from this address at that minute.

``OPENCODE_CLIENT_IDENTITY`` is a different switch and stays one: it chooses
what MCC *claims to be*, not whose allowance it spends, and the five identity
headers and the tool-name translation are identical on both credentials.
"""

import dataclasses
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from my_claude_code.application.errors import ApplicationUnavailableError
from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.config.provider_catalog import ProviderDescriptor
from my_claude_code.config.settings import (
    Settings,
    configured_opencode_free_tier_credential,
)
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import (
    DEFAULT_REASONING_POLICY,
    ReasoningDialect,
    ReasoningPolicy,
)
from my_claude_code.providers.base import BaseProvider, ProviderConfig
from my_claude_code.providers.credential_rotation import CredentialRotationState
from my_claude_code.providers.openai_chat import (
    OPENCODE_FREE_TIER_CATALOGUE,
    model_is_zero_cost,
)

from .config import build_provider_config
from .rotating import RotatingProvider

#: The registry id this whole module is about. ``opencode_go`` is deliberately
#: absent: it is a subscription endpoint, ``public`` buys nothing there, and
#: the investigation's fix note says to leave it alone.
OPENCODE_PROVIDER_ID = "opencode"

#: The literal credential the vendor's own unauthenticated client sends.
OPENCODE_PUBLIC_CREDENTIAL = "public"

#: What the pool, the request log, Analytics and the exports call that slot.
#: A masked key label would be a lie here -- there is no secret to mask, and
#: ``public`` is the value the operator can read in the vendor's own source.
OPENCODE_PUBLIC_KEY_LABEL = "public"

#: The two values of ``OPENCODE_FREE_TIER_CREDENTIAL``.
FREE_TIER_CREDENTIAL_PUBLIC = "public"
FREE_TIER_CREDENTIAL_KEY = "key"


def public_credential_enabled(settings: Settings | None = None) -> bool:
    """Whether free Zen models are fetched on the shared anonymous slot.

    Read per call rather than captured in a module-level table, so the
    dashboard's Save moves this without a restart. ``settings`` is passed at
    *construction*, where the generation being built is the authority on its
    own configuration; everywhere else the process-wide answer is the one the
    request should follow.

    The two can disagree for the moment between a Save and the provider
    rebuild it triggers, and both orders of that disagreement fail the same
    safe way -- to the operator's key, which is 7.33.0.
    """

    if settings is not None:
        raw = str(getattr(settings, "opencode_free_tier_credential", "") or "")
        return raw.strip().lower() != FREE_TIER_CREDENTIAL_KEY
    return configured_opencode_free_tier_credential() == FREE_TIER_CREDENTIAL_PUBLIC


def model_is_free_tier(model_id: str) -> bool:
    """Whether one Zen model is inside the free-tier scope.

    The same three doors Z opened for the tool-name translation, asked through
    the same object: the vendor's own ``-free``/``:free`` tag, the operator's
    ``OPENCODE_FREE_TIER_MODELS`` roster, or a published price of zero on this
    host. One predicate, so the credential and the catalogue can never
    disagree about what "free" means.
    """

    return OPENCODE_FREE_TIER_CATALOGUE.applies_to(
        model_id,
        zero_cost=model_is_zero_cost(OPENCODE_PROVIDER_ID, model_id),
    )


def request_uses_public_credential(model_id: str) -> bool:
    """Whether one routed model is served on the shared anonymous slot."""

    return public_credential_enabled() and model_is_free_tier(model_id)


def probe_credential(provider_id: str, model_id: str, api_key: str) -> str:
    """The credential one capability probe of one model should carry.

    Identity for every provider but Zen, and for a paid Zen model. A probe of
    a *free* Zen model is the same request the free tier meters, so under
    ``public`` it is spent out of the shared bucket like any other free-model
    request -- otherwise pressing Probe on a card listing thirty free models
    would exhaust the operator's allowance in one press, which is half of how
    this release's key was exhausted in the first place.
    """

    if provider_id != OPENCODE_PROVIDER_ID:
        return api_key
    if request_uses_public_credential(model_id):
        return OPENCODE_PUBLIC_CREDENTIAL
    return api_key


def probe_fallback_credential(provider_id: str, credential: str) -> str:
    """The credential a Zen card may probe with when no key is configured.

    ``_probe_target`` refuses a provider with no credential, which is right
    for every host that has none to offer. Zen has one: the anonymous slot
    the vendor's own client falls back to. Free models are probeable without
    a key under ``public``; paid ones answer 401 and are recorded as
    unprobeable, which is the honest outcome and exactly what a wrong key
    already produces.
    """

    if credential:
        return credential
    if provider_id == OPENCODE_PROVIDER_ID and public_credential_enabled():
        return OPENCODE_PUBLIC_CREDENTIAL
    return credential


class OpenCodeCredentialSplitProvider(BaseProvider):
    """One Zen provider with two credentials and one rule for choosing.

    Not a rotation and deliberately not built out of one: rotation picks the
    *next healthy* credential for a request any of them could serve, while
    these two are not interchangeable in either direction. ``public`` cannot
    buy a paid model, and the operator's key must not be spent on a free one
    while the shared slot exists. So the choice is a function of the routed
    model, evaluated per request, and each side keeps the health, the limiter
    and the rotation policy it would have had on its own.
    """

    def __init__(
        self,
        config: ProviderConfig,
        *,
        paid: BaseProvider | None,
        public: BaseProvider,
        missing_credential_message: str = "",
    ) -> None:
        super().__init__(config)
        self._paid = paid
        self._public = public
        # What a paid model is refused with when no key is configured: the
        # message ``build_provider_config`` raised before this class existed,
        # carried rather than reworded so the operator reads the same
        # sentence, pointing at the same page, that 7.33.0 showed them.
        self._missing_credential_message = missing_credential_message

    # -- choosing ---------------------------------------------------------

    def uses_public(self, model_id: str) -> bool:
        """Whether this model is served on the shared anonymous slot.

        **This is the seam.** One question, asked of the routed model at
        dispatch and of nothing else -- not of a model-name branch, not of a
        flag captured when the provider was built, and not of which side
        happens to be healthy.
        """

        return request_uses_public_credential(model_id)

    def _side(self, model_id: str) -> BaseProvider:
        """The sub-provider that serves one model, or the 7.33.0 refusal."""

        if self.uses_public(model_id):
            return self._public
        if self._paid is not None:
            return self._paid
        if self._missing_credential_message:
            raise ApplicationUnavailableError(self._missing_credential_message)
        return self._public

    @property
    def paid(self) -> BaseProvider | None:
        """The operator-key side, or ``None`` when no key is configured."""
        return self._paid

    @property
    def public(self) -> BaseProvider:
        """The shared anonymous side. Always built; sometimes never used."""
        return self._public

    # -- provider surface -------------------------------------------------

    @property
    def credential_label(self) -> str | None:
        """The request log's baseline before a side has been chosen.

        The operator's key, because that is what 7.33.0 recorded and what a
        paid request still spends. A request that goes public overwrites it
        with ``public`` from inside the public pool, which is the same
        overwrite a rotating pool has always performed.
        """

        if self._paid is None:
            return OPENCODE_PUBLIC_KEY_LABEL
        return self._paid.credential_label

    def throttle_remaining(self, model: str | None = None) -> float:
        if model:
            try:
                return self._side(model).throttle_remaining(model)
            except ApplicationUnavailableError:
                return 0.0
        waits = [self._public.throttle_remaining(model)]
        if self._paid is not None:
            waits.append(self._paid.throttle_remaining(model))
        # The pool's best case, for the same reason ``RotatingProvider``
        # reports its best case: a caller with no model in hand is asking
        # whether anything here can serve at all.
        return min(waits)

    def reasoning_dialect(self, model_id: str) -> ReasoningDialect | None:
        # Both sides front the same endpoint with the same profile, so the
        # answer cannot differ; asking the side that will serve keeps a
        # learned per-model rejection attached to the client that learned it.
        try:
            return self._side(model_id).reasoning_dialect(model_id)
        except ApplicationUnavailableError:
            return self._public.reasoning_dialect(model_id)

    def preflight_stream(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> None:
        self._side(request.model).preflight_stream(request, reasoning=reasoning)

    def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> AsyncIterator[str]:
        return self._side(request.model).stream_response(
            request,
            input_tokens,
            request_id=request_id,
            reasoning=reasoning,
        )

    # -- housekeeping -----------------------------------------------------

    async def list_model_ids(self) -> frozenset[str]:
        return await self._listing(lambda side: side.list_model_ids())

    async def list_model_infos(self) -> frozenset[ProviderModelInfo]:
        return await self._listing(lambda side: side.list_model_infos())

    async def _listing[T](
        self,
        fetch: Callable[[BaseProvider], Awaitable[T]],
    ) -> T:
        """One model listing, on the credential that costs the least.

        A listing names no model, so under ``public`` it is asked anonymously
        -- and that one line is what stops the hourly discovery sweep and the
        Test button re-confirming the operator's free-usage limit once an
        hour, which is what spent it. The key is still the authority on what
        this host sells: an anonymous listing that raises is answered by
        asking again with the key, so a refused or pruned anonymous catalogue
        can never remove a paid model from the Models page.
        """

        if self._paid is None:
            return await fetch(self._public)
        if not public_credential_enabled():
            return await fetch(self._paid)
        try:
            listing = await fetch(self._public)
        except Exception:
            return await fetch(self._paid)
        if not listing:
            return await fetch(self._paid)
        return listing

    async def cleanup(self) -> None:
        errors: list[Exception] = []
        for side in (self._paid, self._public):
            if side is None:
                continue
            try:
                await side.cleanup()
            except Exception as exc:
                errors.append(exc)
        if len(errors) == 1:
            raise errors[0]
        if len(errors) > 1:
            raise ExceptionGroup("One or more sub-provider cleanups failed", errors)

    # -- pooled-credential surface ----------------------------------------

    def stream_on_credential(
        self,
        key_index: int,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> AsyncIterator[str]:
        """The executor's diagnostic probe, asked of the right side.

        The model decides the side here exactly as it does for an ordinary
        request: a probe answered on the wrong credential would measure a
        bucket the 429 did not come from.
        """

        side = self._side(request.model)
        probe = getattr(side, "stream_on_credential", None)
        if probe is None:
            raise IndexError(f"no credential at index {key_index}")
        return probe(
            key_index,
            request,
            input_tokens,
            request_id=request_id,
            reasoning=reasoning,
        )

    async def escalate_model_bench_to_key(
        self, key_index: int, model: str, retry_after: float | None
    ) -> bool:
        side = self._side(model)
        escalate = getattr(side, "escalate_model_bench_to_key", None)
        if escalate is None:
            return False
        return await escalate(key_index, model, retry_after)

    # -- admin ------------------------------------------------------------

    def key_health(self) -> list[dict[str, Any]]:
        """The operator pool's rows, index-aligned with the configured keys.

        The public slot is deliberately not in this list: the dashboard zips
        it positionally against ``OPENCODE_API_KEY``'s entries, and a row for
        a credential the operator never typed would land on a key that is not
        it. :meth:`public_key_health` is where that slot's health lives.
        """

        health = getattr(self._paid, "key_health", None)
        return list(health()) if callable(health) else []

    def public_key_health(self) -> list[dict[str, Any]]:
        """The shared anonymous slot's own health rows."""

        health = getattr(self._public, "key_health", None)
        return list(health()) if callable(health) else []


def _public_side(
    descriptor: ProviderDescriptor,
    config: ProviderConfig,
    settings: Settings,
    build_leaf: Callable[[ProviderDescriptor, ProviderConfig, Settings], BaseProvider],
) -> BaseProvider:
    """The one-slot pool the shared credential lives in.

    A pool of one rather than a bare leaf, for two things only a pool has: a
    ``key_label`` channel, so the log says ``public`` instead of a mask of a
    word, and a health record of its own, so a 429 on the shared bucket is
    recorded against the shared bucket.
    """

    public_config = dataclasses.replace(
        config,
        api_key=OPENCODE_PUBLIC_CREDENTIAL,
        api_keys=(OPENCODE_PUBLIC_CREDENTIAL,),
        credential_rotation="single",
    )
    leaf = build_leaf(descriptor, public_config, settings)
    state = CredentialRotationState(
        1,
        "single",
        rate_limit_seconds=config.rate_limit_cooldown_seconds,
        lockout_tiers=config.lockout_tiers,
        model_bench_escalation=config.credential_model_bench_escalation,
        cooldown=config.rate_limit_cooldown(),
    )
    return RotatingProvider(
        public_config,
        [leaf],
        state,
        key_labels=(OPENCODE_PUBLIC_KEY_LABEL,),
        provider_id=descriptor.provider_id,
        routes_around_model=config.routes_around_model,
    )


def build_opencode_provider(
    descriptor: ProviderDescriptor,
    settings: Settings,
    build_pool: Callable[[ProviderDescriptor, ProviderConfig, Settings], BaseProvider],
    build_leaf: Callable[[ProviderDescriptor, ProviderConfig, Settings], BaseProvider],
) -> BaseProvider | None:
    """Zen's construction, or ``None`` to take the line every provider takes.

    ``None`` -- and therefore 7.33.0 exactly -- whenever the operator opted
    out with ``OPENCODE_FREE_TIER_CREDENTIAL=key``. Nothing extra is built,
    nothing extra is chosen, and the boundary between this feature and the
    rest of the factory is this one return.
    """

    if descriptor.provider_id != OPENCODE_PROVIDER_ID:
        return None
    if not public_credential_enabled(settings):
        return None

    paid: BaseProvider | None = None
    missing = ""
    try:
        config = build_provider_config(descriptor, settings)
    except ApplicationUnavailableError as exc:
        # No ``OPENCODE_API_KEY``. Before this release that ended the request
        # here for every Zen model; now it ends it only for the paid ones,
        # because the free ones never needed the operator's key.
        missing = str(exc)
        config = build_provider_config(
            dataclasses.replace(
                descriptor, static_credential=OPENCODE_PUBLIC_CREDENTIAL
            ),
            settings,
        )
    else:
        paid = build_pool(descriptor, config, settings)

    return OpenCodeCredentialSplitProvider(
        config,
        paid=paid,
        public=_public_side(descriptor, config, settings, build_leaf),
        missing_credential_message=missing,
    )


__all__ = [
    "FREE_TIER_CREDENTIAL_KEY",
    "FREE_TIER_CREDENTIAL_PUBLIC",
    "OPENCODE_PROVIDER_ID",
    "OPENCODE_PUBLIC_CREDENTIAL",
    "OPENCODE_PUBLIC_KEY_LABEL",
    "OpenCodeCredentialSplitProvider",
    "build_opencode_provider",
    "model_is_free_tier",
    "probe_credential",
    "probe_fallback_credential",
    "public_credential_enabled",
    "request_uses_public_credential",
]
