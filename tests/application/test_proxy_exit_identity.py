"""PR-S2 (7.89.0): what an exit check learns about the address a request
leaves from, and that it never asks anybody new.

``PROXY_CHECK_EXIT_IP_URL`` stays empty by default. ``provider`` is the
one-click option: the provider's own ``/cdn-cgi/trace``, through the tunnel
being checked. Three answer shapes are read: a Cloudflare trace, JSON, and the
plain text every release before kept as it came.
"""

import json

import pytest

from my_claude_code.application.proxy_check import (
    CHECK_DEPTH_REQUEST,
    CHECK_DEPTH_TLS,
    EXIT_IP_MAX_CHARS,
    ExitIdentity,
    check_proxy,
    exit_check_url,
    parse_exit_answer,
)
from my_claude_code.config.proxy_chains import ProxyCheckRecord
from tests.support.masking_harness import SeenRequest, start_masking_rig

TRACE = (
    "fl=123f45\nh=opencode.ai\nip=104.28.1.2\nts=1760000000.1\n"
    "visit_scheme=https\nuag=python-httpx\ncolo=AMS\nsliver=none\nhttp=http/1.1\n"
    "loc=NL\ntls=TLSv1.3\nsni=plaintext\nwarp=on\ngateway=off\nrbi=off\nkex=X25519\n"
)


# ------------------------------------------------------------------ parsers


def test_a_cloudflare_trace_gives_the_address_the_country_and_warp() -> None:
    assert parse_exit_answer(TRACE) == ExitIdentity(
        ip="104.28.1.2", country="NL", warp="on"
    )
    assert parse_exit_answer(TRACE, trace_only=True).ip == "104.28.1.2"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            # ipinfo-style: country name-or-code under "country", network in "org".
            {"ip": "203.0.113.7", "country": "NL", "org": "AS64500 Example BV"},
            ExitIdentity(ip="203.0.113.7", country="NL", asn="AS64500 Example BV"),
        ),
        (
            # A code beats a name when both are there.
            {
                "ip": "198.51.100.9",
                "country": "Netherlands",
                "country_code": "NL",
                "asn": 64501,
            },
            ExitIdentity(ip="198.51.100.9", country="NL", asn="64501"),
        ),
        (
            {"ip": "192.0.2.44", "countryCode": "SE", "asn": "AS64502"},
            ExitIdentity(ip="192.0.2.44", country="SE", asn="AS64502"),
        ),
    ],
)
def test_three_json_shapes(body: dict, expected: ExitIdentity) -> None:
    assert parse_exit_answer(json.dumps(body)) == expected


def test_plain_text_is_kept_exactly_as_every_release_before_kept_it() -> None:
    """The equality half: an operator URL answering text stores what it did."""

    assert parse_exit_answer("  203.0.113.7\n") == ExitIdentity(ip="203.0.113.7")
    junk = "<html><body>" + "x" * 200 + "</body></html>"
    assert parse_exit_answer(junk) == ExitIdentity(ip=junk[:EXIT_IP_MAX_CHARS])
    # JSON that names no address is text too, as it always was.
    no_ip = json.dumps({"country": "NL"})
    assert parse_exit_answer(no_ip) == ExitIdentity(ip=no_ip[:EXIT_IP_MAX_CHARS])


def test_the_provider_trace_never_stores_a_page_as_an_address() -> None:
    """A host not on Cloudflare: "not available", not its HTML."""

    assert parse_exit_answer("<html>404</html>", trace_only=True) == ExitIdentity()
    assert parse_exit_answer('{"ip": "1.2.3.4"}', trace_only=True) == ExitIdentity()


# --------------------------------------------------------- the trace's URL


def test_provider_resolves_to_the_destination_s_own_host_and_nothing_else() -> None:
    assert (
        exit_check_url("provider", "https://opencode.ai/zen/v1")
        == "https://opencode.ai/cdn-cgi/trace"
    )
    assert (
        exit_check_url(" Provider ", "https://user:pw@api.example.com:8443/v1")
        == "https://api.example.com:8443/cdn-cgi/trace"
    )
    assert exit_check_url("", "https://opencode.ai/zen/v1") == ""
    assert exit_check_url("https://ip.example.org/", "https://a.example") == (
        "https://ip.example.org/"
    )


# ------------------------------------------------------------ the record


def test_a_record_without_exit_identity_round_trips_byte_for_byte() -> None:
    document = {
        "at": "2026-10-01T00:00:00Z",
        "ok": True,
        "latency_ms": 41,
        "tls": "strict",
        "detail": "",
        "exit_ip": "203.0.113.7",
        "depth": "request",
    }
    record = ProxyCheckRecord.from_document(document)
    assert record is not None
    assert record.as_document() == document


def test_the_new_fields_are_written_only_when_they_say_something() -> None:
    record = ProxyCheckRecord(
        at="2026-10-01T00:00:00Z",
        ok=True,
        exit_ip="104.28.1.2",
        exit_country="NL",
        exit_warp="on",
        exit_via="provider",
    )
    document = record.as_document()
    assert document["exit_country"] == "NL"
    assert document["exit_warp"] == "on"
    assert document["exit_via"] == "provider"
    assert "exit_asn" not in document
    assert ProxyCheckRecord.from_document(document) == record


# ------------------------------------------------- through a real tunnel


def _answers(request: SeenRequest) -> tuple[int, bytes] | tuple[int, bytes, str]:
    if request.path == "/cdn-cgi/trace":
        return 200, TRACE.encode(), "text/plain"
    return 200, b"{}"


@pytest.fixture
def rig():
    world = start_masking_rig(_answers)
    try:
        yield world
    finally:
        world.close()


def _traces(rig) -> list[SeenRequest]:
    return [seen for seen in rig.host.requests if seen.path == "/cdn-cgi/trace"]


@pytest.mark.asyncio
@pytest.mark.local_serial
async def test_the_trace_goes_through_the_tunnel_being_checked(rig) -> None:
    proxy = rig.proxy_urls[0]
    destination = rig.host.base_url()

    record = await check_proxy(
        proxy, destination, exit_ip_url="provider", depth=CHECK_DEPTH_REQUEST
    )

    assert record.ok, record.detail
    assert (record.exit_ip, record.exit_country, record.exit_warp, record.exit_via) == (
        "104.28.1.2",
        "NL",
        "on",
        "provider",
    )
    traces = _traces(rig)
    assert len(traces) == 1
    # It reached the provider's own host, through the proxy, by name.
    assert traces[0].peer in rig.proxies[0].outbound
    rig.assert_masked()


@pytest.mark.asyncio
@pytest.mark.local_serial
async def test_an_empty_setting_sends_no_extra_request(rig) -> None:
    record = await check_proxy(
        rig.proxy_urls[0], rig.host.base_url(), depth=CHECK_DEPTH_REQUEST
    )

    assert record.ok, record.detail
    assert _traces(rig) == []
    assert [seen.method for seen in rig.host.requests] == ["HEAD"]
    assert record.as_document().keys() == {
        "at",
        "ok",
        "latency_ms",
        "tls",
        "detail",
        "exit_ip",
        "depth",
        "connect_ms",
        "first_byte_ms",
    }


@pytest.mark.asyncio
@pytest.mark.local_serial
async def test_a_host_with_no_trace_is_recorded_as_not_available(rig) -> None:
    rig.host.responder = lambda request: (404, b"<html>no</html>", "text/html")

    record = await check_proxy(
        rig.proxy_urls[1],
        rig.host.base_url(),
        exit_ip_url="provider",
        depth=CHECK_DEPTH_REQUEST,
    )

    assert record.ok
    assert record.exit_ip == ""
    assert record.exit_via == "provider"


@pytest.mark.asyncio
async def test_the_trace_is_never_sent_at_tls_depth(monkeypatch) -> None:
    """A fetch sweep promises the provider no request at all."""

    from my_claude_code.application import proxy_check

    asked: list[str] = []

    async def handshake(*args, **kwargs) -> None:
        return None

    async def identity(*args, **kwargs) -> ExitIdentity:
        asked.append(args[1])
        return ExitIdentity()

    monkeypatch.setattr(proxy_check, "_verified_handshake", handshake)
    monkeypatch.setattr(proxy_check, "exit_identity", identity)

    record = await check_proxy(
        "socks5h://127.0.0.1:9",
        "https://opencode.ai/zen/v1",
        exit_ip_url="provider",
        depth=CHECK_DEPTH_TLS,
    )
    assert record.ok
    assert asked == []

    # An operator URL is still fetched at that depth, as it always was.
    await check_proxy(
        "socks5h://127.0.0.1:9",
        "https://opencode.ai/zen/v1",
        exit_ip_url="https://ip.example.org/",
        depth=CHECK_DEPTH_TLS,
    )
    assert asked == ["https://ip.example.org/"]
