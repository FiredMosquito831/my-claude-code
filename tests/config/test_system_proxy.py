"""``system_proxy_for`` answers exactly what httpx does for a no-proxy client.

``config/system_proxy.py`` restates httpx 0.28's ``get_environment_proxies`` and
``URLPattern`` rather than importing a private module (and parses with
``urllib`` so ``config`` stays free of httpx). This holds the restatement to
the real thing: for a grid of environments and provider URLs, the proxy httpx
would mount for the URL -- httpx's own ``get_environment_proxies`` and
``URLPattern``, applied the way its client applies them -- is the proxy
:func:`system_proxy_url_for` names.
"""

import itertools

import httpx
import pytest
from httpx._utils import URLPattern, get_environment_proxies

from my_claude_code.config import system_proxy
from my_claude_code.config.system_proxy import system_proxy_for, system_proxy_url_for

ENVIRONMENTS: tuple[dict[str, str], ...] = (
    {},
    {"https": "http://corp:3128"},
    {"http": "http://plain:8080"},
    {"all": "socks5://all.test:1080"},
    {"https": "corp:3128"},  # no scheme: httpx assumes http://
    {"https": "http://corp:3128", "no": "provider.test"},
    {"https": "http://corp:3128", "no": ".provider.test"},
    {"https": "http://corp:3128", "no": "*"},
    {"https": "http://corp:3128", "no": "localhost,127.0.0.1"},
    {"https": "http://corp:3128", "no": "other.test, api.provider.test"},
    {"https": "http://corp:3128", "no": "::1"},
    {"https": "http://u:secret@corp:3128", "http": "http://plain:8080"},
    {"all": "http://all:1", "https": "http://specific:2"},
)
URLS: tuple[str, ...] = (
    "https://api.provider.test/v1",
    "https://provider.test/v1",
    "https://api.other.test/v1",
    "http://provider.test:8080/v1",
    "https://localhost:8443/v1",
    "https://127.0.0.1/v1",
    "https://[::1]:9000/v1",
    "https://api.provider.test:443/v1",
)


def _httpx_answer(proxies: dict[str, str], url: str, monkeypatch) -> str | None:
    """What a no-proxy httpx client would dial ``url`` through, or None.

    httpx's own two functions, in the order its client applies them: the
    environment's mounts, sorted most specific first, the first match wins.
    """

    monkeypatch.setattr("httpx._utils.getproxies", lambda: dict(proxies))
    mounts = {
        URLPattern(key): value for key, value in get_environment_proxies().items()
    }
    target = httpx.URL(url)
    for pattern in sorted(mounts):
        if pattern.matches(target):
            return mounts[pattern]
    return None


def _normalised(url: str | None) -> str | None:
    if url is None:
        return None
    parsed = httpx.URL(url if "://" in url else f"http://{url}")
    port = parsed.port
    if port is None:
        port = {"http": 80, "https": 443}.get(parsed.scheme)
    return f"{parsed.scheme}://{parsed.host}:{port}" if port else None


@pytest.mark.parametrize(
    ("proxies", "url"),
    list(itertools.product(ENVIRONMENTS, URLS)),
    ids=lambda value: str(value).replace(" ", ""),
)
def test_the_answer_matches_httpx(proxies, url, monkeypatch) -> None:
    expected = _httpx_answer(proxies, url, monkeypatch)
    ours = system_proxy_url_for(url, proxies=dict(proxies))

    assert _normalised(ours) == _normalised(expected), (proxies, url, ours, expected)


def test_the_label_is_masked() -> None:
    label = system_proxy_for(
        "https://api.provider.test/v1",
        proxies={"https": "http://alice:hunter2@corp.test:3128"},
    )

    assert label == "corp.test:3128"


def test_no_system_proxy_is_an_empty_label() -> None:
    assert system_proxy_for("https://api.provider.test/v1", proxies={}) == ""


def test_an_unreadable_environment_is_no_system_proxy(monkeypatch) -> None:
    def broken() -> dict[str, str]:
        raise OSError("registry unavailable")

    monkeypatch.setattr(system_proxy, "getproxies", broken)

    assert system_proxy_for("https://api.provider.test/v1") == ""
    assert system_proxy_url_for("not a url at all") is None
