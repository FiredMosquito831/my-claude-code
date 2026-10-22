"""7.108.0 PR-R3/R4/R5: WireGuard files in, wireproxy configs out.

Fixture files are Proton-, Mullvad- and wgcf-shaped, with keys invented for
the fixtures (``tests/fixtures/wireguard/``); documentation-range endpoints.
"""

from pathlib import Path

import pytest

from my_claude_code.config.wireguard_profiles import (
    PERSISTENT_KEEPALIVE_SECONDS,
    WARP_PEER_PUBLIC_KEY,
    WIREGUARD_VENDORS,
    ProfileError,
    RunnerTunnel,
    WireGuardProfile,
    parse_wireguard_file,
    render_wireproxy_config,
    split_endpoint,
    vendors_document,
    wgcf_account_private_key,
    wireguard_vendor,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "wireguard"
PROTON = (FIXTURES / "wg-NL-FREE-12.conf").read_text("utf-8")
MULLVAD = (FIXTURES / "se-sto-wg-001.conf").read_text("utf-8")
WGCF = (FIXTURES / "wgcf-profile.conf").read_text("utf-8")
WGCF_ACCOUNT = (FIXTURES / "wgcf-account.toml").read_text("utf-8")
KEY = "a" * 43 + "="  # 32 bytes of base64, invented


def _private_key(text: str) -> str:
    return next(
        line.split("=", 1)[1].strip()
        for line in text.splitlines()
        if line.startswith("PrivateKey")
    )


def test_a_proton_file_is_one_server_named_from_its_peer_comment() -> None:
    imported = parse_wireguard_file(PROTON, "wg-NL-FREE-12.conf")

    profile = imported.profile
    assert imported.name == "NL-FREE#12"
    assert imported.vendor == "proton"
    assert profile.addresses == ("10.2.0.2/32",)
    assert profile.dns == ("10.2.0.1",)
    assert profile.endpoint == "192.0.2.12:51820"
    assert profile.allowed_ips == ("0.0.0.0/0",)
    assert profile.check_alive == "10.2.0.1"
    assert profile.private_key == _private_key(PROTON)
    assert not profile.is_warp
    assert imported.notes == ()


def test_the_config_is_loopback_only_with_a_local_login_and_the_spec_s_keys() -> None:
    profile = parse_wireguard_file(PROTON).profile

    text = render_wireproxy_config(
        profile,
        source_id="src_runner_x",
        socks_port=41000,
        username="mcc-0a1b2c3d",
        password="f" * 32,
    )

    assert "[Socks5]\nBindAddress = 127.0.0.1:41000\n" in text
    assert "Username = mcc-0a1b2c3d\nPassword = " + "f" * 32 in text
    assert "CheckAlive = 10.2.0.1" in text
    assert f"PersistentKeepalive = {PERSISTENT_KEEPALIVE_SECONDS}" in text
    assert "[Resolve]\nResolveStrategy = auto" in text
    assert f"PrivateKey = {profile.private_key}" in text
    assert "0.0.0.0:" not in text
    assert "TCPClientTunnel" not in text
    # Nothing a wg-quick file could make a system do.
    for word in ("PostUp", "PreUp", "Table", "ListenPort"):
        assert word not in text


def test_mullvad_exits_become_tunnels_inside_the_one_tunnel() -> None:
    imported = parse_wireguard_file(MULLVAD, "se-sto-wg-001.conf")
    assert imported.vendor == "mullvad"
    assert imported.name == "se-sto-wg-001"
    assert imported.profile.addresses == (
        "10.66.1.2/32",
        "fc00:bbbb:bbbb:bb01::1:102/128",
    )

    text = render_wireproxy_config(
        imported.profile,
        source_id="src_runner_m",
        socks_port=41000,
        username="mcc-00000000",
        password="0" * 32,
        tunnels=[
            RunnerTunnel(41002, "se-sto-wg-socks5-001.relays.mullvad.net", 1080),
            RunnerTunnel(41003, "nl-ams-wg-socks5-002.relays.mullvad.net", 1080),
        ],
    )

    assert text.count("[TCPClientTunnel]") == 2
    assert (
        "BindAddress = 127.0.0.1:41002\n"
        "Target = se-sto-wg-socks5-001.relays.mullvad.net:1080"
    ) in text


def test_a_wgcf_profile_is_warp_and_its_account_key_must_match() -> None:
    imported = parse_wireguard_file(WGCF, "wgcf-profile.conf")

    profile = imported.profile
    assert imported.vendor == "warp"
    assert profile.is_warp
    assert profile.peer_public_key == WARP_PEER_PUBLIC_KEY
    assert profile.addresses == ("172.16.0.2/32", "2606:4700:110:8f00::2/128")
    assert profile.allowed_ips == ("0.0.0.0/0", "::/0")
    assert profile.mtu == 1280
    assert profile.check_alive == "1.1.1.1"
    assert wgcf_account_private_key(WGCF_ACCOUNT) == profile.private_key
    with pytest.raises(ProfileError, match="not TOML"):
        wgcf_account_private_key("private_key = 'unterminated")
    with pytest.raises(ProfileError, match="no private_key"):
        wgcf_account_private_key("device_id = 'x'\n")


def test_the_profile_round_trips_through_its_document() -> None:
    for text in (PROTON, MULLVAD, WGCF):
        profile = parse_wireguard_file(text).profile
        assert WireGuardProfile.from_document(profile.as_document()) == profile
    assert WireGuardProfile.from_document({"private_key": "nope"}) is None


def test_a_preshared_key_is_a_second_secret() -> None:
    text = PROTON.replace("AllowedIPs", f"PresharedKey = {KEY}\nAllowedIPs")
    profile = parse_wireguard_file(text).profile
    assert profile.preshared_key == KEY
    assert profile.secrets == (profile.private_key, KEY)
    assert "PresharedKey = " + KEY in render_wireproxy_config(
        profile, source_id="s", socks_port=1, username="u", password="p"
    )


def test_what_wg_quick_would_run_is_dropped_and_said_so() -> None:
    text = PROTON.replace(
        "DNS = 10.2.0.1",
        "DNS = 10.2.0.1, corp.example\nPostUp = iptables -A x\nTable = off\nListenPort = 51820",
    )
    imported = parse_wireguard_file(text)
    assert imported.profile.dns == ("10.2.0.1",)
    joined = " ".join(imported.notes)
    assert "never runs a command" in joined
    assert "postup" in joined and "table" in joined
    assert "corp.example" in joined


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("", "Paste or upload"),
        (PROTON.split("[Peer]")[0], r"0 \[Peer\]"),
        (
            PROTON + "\n[Peer]\nPublicKey = " + KEY + "\nEndpoint = 192.0.2.1:1\n",
            r"2 \[Peer\]",
        ),
        (PROTON.replace("PrivateKey", "Jc = 4\nPrivateKey"), "AmneziaWG"),
        (PROTON.replace(_private_key(PROTON), "not-a-key"), "PrivateKey is not"),
        (
            PROTON.replace("Endpoint = 192.0.2.12:51820", "Endpoint = 192.0.2.12"),
            "names no port",
        ),
        (PROTON.replace("Address = 10.2.0.2/32", "Address = ten"), "is not an address"),
        (PROTON.replace("[Interface]", "[Interfaze]"), "not a WireGuard section"),
        ("PrivateKey = " + KEY, "inside \\[Interface\\]"),
        (PROTON.replace("Endpoint", "# Endpoint"), "no Endpoint"),
    ],
    ids=[
        "empty",
        "no-peer",
        "two-peers",
        "amnezia",
        "bad-key",
        "no-port",
        "bad-address",
        "bad-section",
        "no-section",
        "no-endpoint",
    ],
)
def test_files_that_are_not_one_plain_wireguard_server_are_refused(
    text: str, match: str
) -> None:
    with pytest.raises(ProfileError, match=match):
        parse_wireguard_file(text)


def test_endpoints_are_checked() -> None:
    assert split_endpoint("[2001:db8::1]:51820") == ("2001:db8::1", 51820)
    assert split_endpoint("engage.cloudflareclient.com:2408") == (
        "engage.cloudflareclient.com",
        2408,
    )
    for bad in ("host:0", "a b:1", "[nothex]:1", "host:port"):
        with pytest.raises(ProfileError):
            split_endpoint(bad)


def test_nothing_can_be_smuggled_into_the_config() -> None:
    profile = parse_wireguard_file(PROTON).profile
    with pytest.raises(ProfileError, match="local username"):
        render_wireproxy_config(
            profile,
            source_id="s",
            socks_port=1,
            username="u\n[Socks5]",
            password="p",
        )
    with pytest.raises(ProfileError, match="local password"):
        render_wireproxy_config(
            profile, source_id="s", socks_port=1, username="u", password="a=b"
        )


def test_a_name_falls_back_to_the_file_name_then_wireguard() -> None:
    bare = PROTON.replace("# NL-FREE#12\n", "")
    assert (
        parse_wireguard_file(bare, "C:\\Downloads\\my-server.conf").name == "my-server"
    )
    assert parse_wireguard_file(bare).name == "WireGuard"
    assert (
        parse_wireguard_file(
            bare.replace("# NetShield = 1\n", "").replace(
                "# VPN Accelerator = on\n", ""
            )
        ).vendor
        == "other"
    )


def test_vendors_are_readers_with_caveats_and_no_server_list() -> None:
    ids = [vendor.id for vendor in WIREGUARD_VENDORS]
    assert ids == [
        "proton",
        "mullvad",
        "ivpn",
        "surfshark",
        "windscribe",
        "airvpn",
        "pia",
        "warp",
        "other",
    ]
    assert "one device" in wireguard_vendor("proton").caveat
    assert "never registers" in wireguard_vendor("warp").caveat
    assert wireguard_vendor("nonsense").id == "other"
    assert not any(vendor.verified for vendor in WIREGUARD_VENDORS)
    document = vendors_document()
    # A documented host SHAPE for the in-tunnel exits, never a host list.
    assert document[1]["in_tunnel_shape"].startswith("<country>-<city>")
    assert all(item["doc"].startswith("https://") for item in document)
