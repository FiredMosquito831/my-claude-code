"""Limits and defaults for outbound web server tool HTTP."""

from my_claude_code.core.version import package_version

_REQUEST_TIMEOUT_S = 20.0
_MAX_SEARCH_RESULTS = 10
_MAX_FETCH_CHARS = 24_000
# Fewer characters than this from the extractor is treated as "it found
# nothing" and the page falls back to today's parser, so a fetch never returns
# less than it did before 7.120.0 (RESEARCH-DDG-WEBSEARCH.md R3). 200 is the
# research's own failure line -- the extraction table counts a page under 200
# characters as an empty answer (1.5), and ``scoring.UNDER_CHARS`` in the DDG
# harness is the same number.
_WEB_FETCH_EXTRACTOR_MIN_CHARS = 200
# Hard cap on raw bytes read from HTTP responses before decode / HTML parse (memory bound).
_MAX_WEB_FETCH_RESPONSE_BYTES = 2 * 1024 * 1024
# Drain at most this many bytes from redirect responses before following Location.
_REDIRECT_RESPONSE_BODY_CAP_BYTES = 65_536
_MAX_WEB_FETCH_REDIRECTS = 10
_WEB_FETCH_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
# The Cloudflare answer a fetch retries once (R3, opencode's technique): a 403
# whose ``cf-mitigated`` header says ``challenge``. Declared here rather than
# spelled in a branch so the rule is one edit and the tests name it.
_CLOUDFLARE_CHALLENGE_STATUS = 403
_CLOUDFLARE_CHALLENGE_HEADER = "cf-mitigated"
_CLOUDFLARE_CHALLENGE_VALUE = "challenge"

#: MCC's product User-Agent, sent by ``web_fetch`` (7.120.0, USER DECISIONS
#: Q12). It replaces the stale name an earlier release sent; no browser is
#: impersonated -- a browser-class UA lives only in the DuckDuckGo hop's
#: declared profile (``config/duckduckgo_profiles.py``).
_WEB_FETCH_USER_AGENT = f"my-claude-code/{package_version()}"

_WEB_TOOL_HTTP_HEADERS = {
    "User-Agent": _WEB_FETCH_USER_AGENT,
    # Markdown first (R3, gap G14): a server that can answer in markdown is
    # asked for it; the HTML extractor is what happens when it answers HTML.
    "Accept": (
        "text/markdown;q=1.0, text/x-markdown;q=0.9, text/plain;q=0.8, "
        "text/html;q=0.7, */*;q=0.1"
    ),
}
