"""What one failed search costs its key when no search chain is set (7.112.1).

User decision Q8 (``specs/INVESTIGATION-WEBSEARCH-ROUTING.md``): the search
route used without ``WEB_SEARCH_CHAIN`` follows the model rules. Before
7.112.1 every failure benched the key that met it -- 10 s after a 5xx, a
timeout, a refused connection or an unreadable body, 60 s after a 429 that
named no wait, 300 s after a 401/403/402 -- and a provider with one key (or
none) then refused every search with no call made until the bench ended.

Nothing here is a new rule. The failure is classified by the search chain's
own translation (``search_books.translate_search_error``, 7.112.0), and that
classification is judged by the model provider pool's own reader
(``providers/credential_rotation.credential_failure_class``) with the same
country-refusal exemption ``CredentialRotationState.report_failure`` applies.
The chain and the default route therefore cannot disagree about which
failures charge a key; only the bench lengths stay each pool's own (the
default route keeps its pool's lockout ladder and 429 window).
"""

from my_claude_code.core.rate_limit import DEFAULT_RATE_LIMIT_COOLDOWN
from my_claude_code.providers.credential_rotation import credential_failure_class
from my_claude_code.providers.recovery import region_refusal_on_cause_chain

from .search_books import translate_search_error


def no_chain_key_charge(error: BaseException) -> str | None:
    """The model path's verdict on one failed search try, for the search pool.

    ``"auth"`` (401/403), ``"rate_limit"`` (429) or ``"quota"`` (an exact
    out-of-credits phrase) charge the key; ``None`` -- a 5xx, a timeout, a
    refused connection, an unreadable body, a bare 402, a country refusal,
    anything unclassified -- leaves it untouched.

    The cooldown handed to the translation only bounds the numbers it carries
    (a header wait, the credits bench); which class comes out does not depend
    on it, and the default route's pool keeps its own windows.
    """
    failure = translate_search_error(error, cooldown=DEFAULT_RATE_LIMIT_COOLDOWN)
    judged: BaseException = failure if failure is not None else error
    charge = credential_failure_class(judged)
    if charge == "auth" and region_refusal_on_cause_chain(judged):
        # 7.96.0's rule: a 403 whose host words refuse the caller's country is
        # about the address, not the key.
        return None
    return charge
