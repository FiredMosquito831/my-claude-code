"""WireGuard files a runner source imports, and the wireproxy config it runs (7.108.0).

PR-R3 (and R4, R5): a VPN account's page exports a WireGuard file -- Proton
VPN's *Downloads -> WireGuard configuration*, Mullvad's and IVPN's generators,
Surfshark's, Windscribe's, AirVPN's, the file PIA's ``manual-connections``
script writes, ``wgcf generate``'s ``wgcf-profile.conf`` -- and MCC turns it
into one loopback SOCKS5 address by running wireproxy with it.

Two halves, both pure:

* :func:`parse_wireguard_file` reads the ``wg-quick`` text the operator pasted
  or uploaded into a :class:`WireGuardProfile`: one ``[Interface]`` and exactly
  one ``[Peer]``, every key and address checked. What ``wg-quick`` would run on
  the operator's machine -- ``PostUp``, ``PreDown`` and the rest -- is dropped
  and said so: MCC never runs a command a file names. An AmneziaWG file is
  refused (wireproxy speaks plain WireGuard).
* :func:`render_wireproxy_config` writes the file wireproxy is started with:
  the profile, a ``[Socks5]`` on ``127.0.0.1`` with a random local username and
  password (so no other program on this computer can borrow the tunnel),
  ``CheckAlive`` for ``/readyz``, ``PersistentKeepalive = 25`` and
  ``ResolveStrategy = auto`` -- spec §5.8 -- and, for Mullvad's and IVPN's
  in-tunnel per-server proxies (R4), one ``[TCPClientTunnel]`` per exit.

The private key and a preshared key are the two secrets here. They live in the
owner-only ``proxy_sources.json`` and in the owner-only generated config only;
nothing in this module logs or returns them in anything but those two
documents.

**Vendors are readers, not sources** (the user's decision 5 of 2026-09-25).
:data:`WIREGUARD_VENDORS` names how each vendor's file looks, where its
documentation is and what its plan allows -- shown on the card, never used to
pick a server. MCC ships no server list and no hostname of anybody's.
"""

import base64
import binascii
import ipaddress
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Self

#: Cloudflare WARP's WireGuard peer, as every ``wgcf-profile.conf`` names it,
#: and the host its endpoint is under. How a WARP file is recognised.
WARP_PEER_PUBLIC_KEY = "bmXOC+F1FxEMF9dyiK2H5/1SUtzH0JuVo51h2wPfgyo="
WARP_ENDPOINT_SUFFIX = ".cloudflareclient.com"

#: wg-quick keys MCC drops, because they are instructions to the operating
#: system that a userspace tunnel neither needs nor may follow.
_SYSTEM_KEYS = frozenset(
    {
        "listenport",
        "fwmark",
        "table",
        "preup",
        "postup",
        "predown",
        "postdown",
        "saveconfig",
    }
)
#: AmneziaWG's obfuscation keys: such a file needs an AmneziaWG client.
_AMNEZIA_KEYS = frozenset(
    {"jc", "jmin", "jmax", "s1", "s2", "h1", "h2", "h3", "h4", "i1", "i2", "i3"}
)
_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*\.?$"
)
#: What wireproxy is told with every runner.
PERSISTENT_KEEPALIVE_SECONDS = 25
RESOLVE_STRATEGY = "auto"
#: The longest a server name taken from the file may be.
NAME_MAX_LENGTH = 48


class ProfileError(ValueError):
    """A WireGuard file MCC will not import, with the sentence the page shows."""


@dataclass(frozen=True, slots=True)
class WireGuardVendor:
    """How one vendor's WireGuard file looks and what its plan allows."""

    id: str
    name: str
    #: The vendor's own page about exporting a WireGuard file.
    doc: str
    #: What the card says about this vendor's plan, in one or two sentences.
    caveat: str
    #: The vendor's documented in-tunnel SOCKS5 host shape (R4), or ``""``.
    in_tunnel_shape: str = ""
    in_tunnel_doc: str = ""
    in_tunnel_port: int = 1080
    #: Whether MCC has run this vendor's file against the real service.
    verified: bool = False
    #: The name an address's label starts with, when it is not :attr:`name`.
    short: str = ""

    @property
    def short_name(self) -> str:
        return self.short or self.name


WIREGUARD_VENDORS: tuple[WireGuardVendor, ...] = (
    WireGuardVendor(
        "proton",
        "Proton VPN",
        "https://protonvpn.com/support/wireguard-configurations",
        "Proton VPN Free allows one device: disconnect the Proton app before you "
        "start this runner, or one of the two may be refused or disconnected. "
        "Plus allows ten. One file is one server.",
        short="Proton",
    ),
    WireGuardVendor(
        "mullvad",
        "Mullvad",
        "https://mullvad.net/en/help/wireguard-and-mullvad-vpn",
        "Five devices per account; this key is one of them. Through one tunnel "
        "every Mullvad server's own SOCKS5 proxy is reachable, so one key can "
        "give many exits.",
        in_tunnel_shape="<country>-<city>-wg-socks5-<NNN>.relays.mullvad.net:1080",
        in_tunnel_doc=(
            "https://mullvad.net/en/help/"
            "different-entryexit-node-using-wireguard-and-socks5-proxy"
        ),
    ),
    WireGuardVendor(
        "ivpn",
        "IVPN",
        "https://www.ivpn.net/knowledgebase/linux/linux-wireguard-kill-switch/",
        "Five devices (Standard, Plus) or ten (Pro); this key is one of them. "
        "IVPN documents a SOCKS5 proxy inside its tunnel on every server.",
        in_tunnel_shape="socks5.<server>.gw.ivpn.net:1080",
        in_tunnel_doc="https://www.ivpn.net/knowledgebase/general/socks5-proxy-service/",
    ),
    WireGuardVendor(
        "surfshark",
        "Surfshark",
        "https://support.surfshark.com/hc/en-us/articles/6586553044498",
        "Surfshark shows the private key once, when you create the key pair.",
    ),
    WireGuardVendor(
        "windscribe",
        "Windscribe",
        "https://windscribe.com/getconfig/wireguard",
        "The WireGuard generator is a Pro feature.",
    ),
    WireGuardVendor(
        "airvpn",
        "AirVPN",
        "https://airvpn.org/generator/",
        "Generated per server in the AirVPN client area.",
    ),
    WireGuardVendor(
        "pia",
        "Private Internet Access",
        "https://github.com/pia-foss/manual-connections",
        "PIA's own manual-connections script writes this file; the key it "
        "registers expires, so run the script again when the tunnel stops.",
        short="PIA",
    ),
    WireGuardVendor(
        "warp",
        "Cloudflare WARP (wgcf)",
        "https://github.com/ViRb3/wgcf",
        "Import only: MCC never registers a WARP device (the user's decision "
        "4). WARP is one shared exit per Cloudflare location, and Cloudflare-"
        "fronted providers can see a request came through it (warp=on). "
        "wireproxy has no field for WARP's reserved bytes; whether WARP "
        "answers without them is reported, not documented.",
        short="WARP",
    ),
    WireGuardVendor(
        "other",
        "Another WireGuard server",
        "https://www.wireguard.com/quickstart/",
        "Any WireGuard server you have a file for: your own, or a vendor not "
        "listed here.",
        short="WireGuard",
    ),
)
_VENDORS_BY_ID = {vendor.id: vendor for vendor in WIREGUARD_VENDORS}


def wireguard_vendor(vendor_id: str) -> WireGuardVendor:
    return _VENDORS_BY_ID.get(vendor_id.strip().lower(), _VENDORS_BY_ID["other"])


def vendors_document() -> list[dict[str, Any]]:
    """The import form's vendor list: data, never an offer."""

    return [
        {
            "id": vendor.id,
            "name": vendor.name,
            "short": vendor.short_name,
            "doc": vendor.doc,
            "caveat": vendor.caveat,
            "in_tunnel_shape": vendor.in_tunnel_shape,
            "in_tunnel_doc": vendor.in_tunnel_doc,
            "in_tunnel_port": vendor.in_tunnel_port,
            "verified": vendor.verified,
        }
        for vendor in WIREGUARD_VENDORS
    ]


# ------------------------------------------------------------------ profile


def _key(text: str, what: str) -> str:
    cleaned = text.strip()
    try:
        raw = base64.b64decode(cleaned, validate=True)
    except binascii.Error, ValueError:
        raw = b""
    if len(raw) != 32:
        raise ProfileError(
            f"The {what} is not a WireGuard key (44 characters of base64 that "
            "decode to 32 bytes)."
        )
    return cleaned


def _interfaces(values: Sequence[str], what: str) -> tuple[str, ...]:
    kept: list[str] = []
    for value in values:
        try:
            parsed = ipaddress.ip_interface(value)
        except ValueError:
            raise ProfileError(f"{what} {value!r} is not an address.") from None
        text = str(parsed)
        if text not in kept:
            kept.append(text)
    return tuple(kept)


def _networks(values: Sequence[str]) -> tuple[str, ...]:
    kept: list[str] = []
    for value in values:
        try:
            parsed = ipaddress.ip_network(value, strict=False)
        except ValueError:
            raise ProfileError(f"AllowedIPs {value!r} is not a network.") from None
        text = str(parsed)
        if text not in kept:
            kept.append(text)
    return tuple(kept)


def split_endpoint(text: str) -> tuple[str, int]:
    """``host:port`` or ``[v6]:port`` -> ``(host, port)``, checked."""

    cleaned = text.strip()
    if cleaned.startswith("["):
        host, _, rest = cleaned[1:].partition("]")
        port_text = rest[1:] if rest.startswith(":") else ""
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise ProfileError(f"Endpoint {cleaned!r} is not an address.") from None
    else:
        host, _, port_text = cleaned.rpartition(":")
        if not host:
            raise ProfileError(f"Endpoint {cleaned!r} names no port.")
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            if not _HOSTNAME.match(host):
                raise ProfileError(
                    f"Endpoint {cleaned!r} is not a host name or address."
                ) from None
    try:
        port = int(port_text)
    except ValueError:
        raise ProfileError(f"Endpoint {cleaned!r} names no port.") from None
    if not 0 < port < 65536:
        raise ProfileError(f"Endpoint {cleaned!r} names no port.")
    return host.rstrip("."), port


def join_endpoint(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


@dataclass(frozen=True, slots=True)
class WireGuardProfile:
    """One WireGuard tunnel: this side's key and addresses, and its one peer."""

    private_key: str
    addresses: tuple[str, ...]
    peer_public_key: str
    endpoint_host: str
    endpoint_port: int
    allowed_ips: tuple[str, ...] = ("0.0.0.0/0", "::/0")
    dns: tuple[str, ...] = ()
    mtu: int = 0
    preshared_key: str = ""

    @property
    def secrets(self) -> tuple[str, ...]:
        """The two values that must never leave the owner-only files."""

        return tuple(value for value in (self.private_key, self.preshared_key) if value)

    @property
    def endpoint(self) -> str:
        return join_endpoint(self.endpoint_host, self.endpoint_port)

    @property
    def is_warp(self) -> bool:
        return self.peer_public_key == WARP_PEER_PUBLIC_KEY or (
            self.endpoint_host.lower().endswith(WARP_ENDPOINT_SUFFIX)
        )

    @property
    def check_alive(self) -> str:
        """The address ``/readyz`` pings inside the tunnel: the file's own DNS
        server (the vendor's, reached only through the tunnel), IPv4 first."""

        for value in self.dns:
            try:
                if isinstance(ipaddress.ip_address(value), ipaddress.IPv4Address):
                    return value
            except ValueError:
                continue
        return self.dns[0] if self.dns else ""

    def as_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "private_key": self.private_key,
            "addresses": list(self.addresses),
        }
        if self.dns:
            document["dns"] = list(self.dns)
        if self.mtu:
            document["mtu"] = self.mtu
        peer: dict[str, Any] = {
            "public_key": self.peer_public_key,
            "endpoint": self.endpoint,
            "allowed_ips": list(self.allowed_ips),
        }
        if self.preshared_key:
            peer["preshared_key"] = self.preshared_key
        document["peer"] = peer
        return document

    @classmethod
    def from_document(cls, raw: object) -> Self | None:
        """Read back what :meth:`as_document` wrote; ``None`` for anything else."""

        if not isinstance(raw, Mapping):
            return None
        peer = raw.get("peer")
        if not isinstance(peer, Mapping):
            return None
        try:
            host, port = split_endpoint(str(peer.get("endpoint") or ""))
            raw_mtu = raw.get("mtu")
            mtu = raw_mtu if isinstance(raw_mtu, int) and 0 < raw_mtu < 65536 else 0
            return cls(
                private_key=_key(str(raw.get("private_key") or ""), "PrivateKey"),
                addresses=_interfaces(_strings(raw.get("addresses")), "Address"),
                peer_public_key=_key(str(peer.get("public_key") or ""), "PublicKey"),
                endpoint_host=host,
                endpoint_port=port,
                allowed_ips=_networks(_strings(peer.get("allowed_ips")))
                or ("0.0.0.0/0", "::/0"),
                dns=_dns(_strings(raw.get("dns")))[0],
                mtu=mtu,
                preshared_key=_key(str(peer.get("preshared_key")), "PresharedKey")
                if peer.get("preshared_key")
                else "",
            )
        except ProfileError:
            return None


def _strings(raw: object) -> list[str]:
    if isinstance(raw, Sequence) and not isinstance(raw, str):
        return [str(item) for item in raw]
    return []


def _dns(values: Sequence[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(addresses kept, names dropped)``: wireproxy resolves through
    addresses; a search domain in ``DNS =`` is a ``wg-quick`` system setting."""

    kept: list[str] = []
    dropped: list[str] = []
    for value in values:
        try:
            text = str(ipaddress.ip_address(value.strip()))
        except ValueError:
            dropped.append(value.strip())
            continue
        if text not in kept:
            kept.append(text)
    return tuple(kept), tuple(dropped)


# -------------------------------------------------------------- the parser


@dataclass(frozen=True, slots=True)
class ImportedProfile:
    """What a pasted file said: the profile, a name, a vendor guess, notes."""

    profile: WireGuardProfile
    name: str
    vendor: str
    notes: tuple[str, ...] = field(default_factory=tuple)


_COMMENT_KEY_VALUE = re.compile(r"^[A-Za-z][A-Za-z0-9 ()_-]*\s=")


def _clean_name(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9#._ -]+", "", text).strip(" .-_")
    return cleaned[:NAME_MAX_LENGTH].strip()


def _name_from_filename(filename: str) -> str:
    stem = filename.replace("\\", "/").rsplit("/", 1)[-1]
    for suffix in (".conf", ".CONF", ".txt"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    return _clean_name(stem)


def _guess_vendor(profile: WireGuardProfile, comments: Sequence[str]) -> str:
    """Which vendor's file this looks like, as a suggestion the form shows.

    Only what each vendor documents or always writes: WARP's peer and
    endpoint; Proton's ``# NetShield`` / ``# VPN Accelerator`` comments;
    Mullvad's in-tunnel DNS ``10.64.0.1``. Anything else is "other", and the
    operator picks.
    """

    if profile.is_warp:
        return "warp"
    joined = "\n".join(comments).lower()
    if "netshield" in joined or "vpn accelerator" in joined:
        return "proton"
    if "10.64.0.1" in profile.dns:
        return "mullvad"
    return "other"


def parse_wireguard_file(text: str, filename: str = "") -> ImportedProfile:
    """Read one ``wg-quick`` file into a profile, or refuse it in a sentence."""

    if not text.strip():
        raise ProfileError("Paste or upload a WireGuard file (a .conf).")
    if len(text) > 64 * 1024:
        raise ProfileError("That is not a WireGuard file: it is over 64 KB.")
    sections: list[tuple[str, dict[str, list[str]], list[str]]] = []
    notes: list[str] = []
    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip().lstrip("﻿")
        if not line:
            continue
        if line.startswith(("#", ";")):
            if sections:
                sections[-1][2].append(line.lstrip("#;").strip())
            continue
        if line.startswith("["):
            title = line.strip("[] \t").lower()
            if title not in {"interface", "peer"}:
                raise ProfileError(
                    f"Line {number}: [{line.strip('[] ')}] is not a WireGuard "
                    "section ([Interface] or [Peer])."
                )
            sections.append((title, {}, []))
            continue
        key, separator, value = line.partition("=")
        if not separator or not sections:
            raise ProfileError(
                f"Line {number} is not 'Key = value' inside [Interface] or [Peer]."
            )
        if " #" in value:
            value = value.split(" #", 1)[0]
        values = sections[-1][1].setdefault(key.strip().lower(), [])
        values.extend(part.strip() for part in value.split(",") if part.strip())
    interfaces = [item for item in sections if item[0] == "interface"]
    peers = [item for item in sections if item[0] == "peer"]
    if len(interfaces) != 1:
        raise ProfileError("A WireGuard file has exactly one [Interface] section.")
    if len(peers) != 1:
        raise ProfileError(
            f"This file has {len(peers)} [Peer] sections; a runner takes a file "
            "with exactly one server."
        )
    _, interface, interface_comments = interfaces[0]
    _, peer, peer_comments = peers[0]
    amnezia = sorted(_AMNEZIA_KEYS & set(interface))
    if amnezia:
        raise ProfileError(
            "This is an AmneziaWG file (it sets "
            + ", ".join(key.capitalize() for key in amnezia)
            + "); wireproxy speaks plain WireGuard."
        )
    dropped = sorted(_SYSTEM_KEYS & set(interface))
    if dropped:
        notes.append(
            "Dropped what wg-quick would run on this computer ("
            + ", ".join(dropped)
            + "): MCC never runs a command or changes a route a file names."
        )
    if not interface.get("privatekey"):
        raise ProfileError("The [Interface] section has no PrivateKey.")
    if not interface.get("address"):
        raise ProfileError("The [Interface] section has no Address.")
    if not peer.get("publickey"):
        raise ProfileError("The [Peer] section has no PublicKey.")
    if not peer.get("endpoint"):
        raise ProfileError("The [Peer] section has no Endpoint.")
    dns, names = _dns(interface.get("dns", []))
    if names:
        notes.append("Dropped DNS search domains: " + ", ".join(names) + ".")
    mtu = 0
    if interface.get("mtu"):
        try:
            mtu = int(interface["mtu"][-1])
        except ValueError:
            raise ProfileError("MTU is not a number.") from None
        if not 0 < mtu < 65536:
            raise ProfileError("MTU is not a usable size.")
    allowed = _networks(peer.get("allowedips", []))
    if not allowed:
        allowed = ("0.0.0.0/0", "::/0")
        notes.append("No AllowedIPs: everything goes through the tunnel.")
    host, port = split_endpoint(peer["endpoint"][-1])
    profile = WireGuardProfile(
        private_key=_key(interface["privatekey"][-1], "PrivateKey"),
        addresses=_interfaces(interface["address"], "Address"),
        peer_public_key=_key(peer["publickey"][-1], "PublicKey"),
        endpoint_host=host,
        endpoint_port=port,
        allowed_ips=allowed,
        dns=dns,
        mtu=mtu,
        preshared_key=_key(peer["presharedkey"][-1], "PresharedKey")
        if peer.get("presharedkey")
        else "",
    )
    if not profile.check_alive:
        notes.append(
            "The file names no DNS server, so the tunnel's own check has "
            "nothing to ask: Ready means the SOCKS5 port answers."
        )
    name = next(
        (
            _clean_name(comment)
            for comment in peer_comments
            if comment and not _COMMENT_KEY_VALUE.match(comment)
        ),
        "",
    )
    name = name or _name_from_filename(filename) or "WireGuard"
    vendor = _guess_vendor(profile, interface_comments + peer_comments)
    return ImportedProfile(profile, name, vendor, tuple(notes))


def wgcf_account_private_key(text: str) -> str:
    """The ``private_key`` of a ``wgcf-account.toml``; nothing else is kept.

    The account file also holds the device's access token and licence key.
    They are read here only to be ignored: MCC never calls Cloudflare's API,
    so it has no use for either and stores neither.
    """

    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise ProfileError(
            "That is not a wgcf-account.toml (it is not TOML)."
        ) from None
    value = document.get("private_key")
    if not isinstance(value, str) or not value:
        raise ProfileError("That wgcf-account.toml has no private_key.")
    return _key(value, "private_key in wgcf-account.toml")


# ------------------------------------------------------------- the config


@dataclass(frozen=True, slots=True)
class RunnerTunnel:
    """One in-tunnel exit (R4): a loopback port forwarded to a SOCKS5 host
    reachable only through the tunnel."""

    local_port: int
    host: str
    port: int


def _safe(value: str, what: str) -> str:
    """A value written into wireproxy's file: one line, nothing it could read
    as another key or section."""

    if not value or any(character in value for character in "\r\n#;="):
        raise ProfileError(f"{what} cannot go into a wireproxy config.")
    return value


def render_wireproxy_config(
    profile: WireGuardProfile,
    *,
    source_id: str,
    socks_port: int,
    username: str,
    password: str,
    tunnels: Sequence[RunnerTunnel] = (),
) -> str:
    """The wireproxy config for one runner, as text. Written owner-only.

    The private key is in it -- that is what the file is for -- so it is never
    logged, never echoed and deleted with the source.
    """

    interface = [
        "[Interface]",
        f"PrivateKey = {profile.private_key}",
        f"Address = {', '.join(profile.addresses)}",
    ]
    if profile.dns:
        interface.append(f"DNS = {', '.join(profile.dns)}")
    if profile.mtu:
        interface.append(f"MTU = {profile.mtu}")
    if profile.check_alive:
        interface.append(f"CheckAlive = {profile.check_alive}")
    peer = [
        "[Peer]",
        f"PublicKey = {profile.peer_public_key}",
    ]
    if profile.preshared_key:
        peer.append(f"PresharedKey = {profile.preshared_key}")
    peer += [
        f"Endpoint = {_safe(profile.endpoint, 'The endpoint')}",
        f"AllowedIPs = {', '.join(profile.allowed_ips)}",
        f"PersistentKeepalive = {PERSISTENT_KEEPALIVE_SECONDS}",
    ]
    blocks = [
        "\n".join(
            [
                f"# Written by My Claude Code for the source {_safe(source_id, 'The id')}.",
                "# Rewritten from proxy_sources.json on every start; an edit here",
                "# is lost. It holds the tunnel's private key: readable by you only.",
            ]
        ),
        "\n".join(interface),
        "\n".join(peer),
        "\n".join(["[Resolve]", f"ResolveStrategy = {RESOLVE_STRATEGY}"]),
        "\n".join(
            [
                "[Socks5]",
                f"BindAddress = 127.0.0.1:{socks_port}",
                f"Username = {_safe(username, 'The local username')}",
                f"Password = {_safe(password, 'The local password')}",
            ]
        ),
    ]
    blocks.extend(
        "\n".join(
            [
                "[TCPClientTunnel]",
                f"BindAddress = 127.0.0.1:{tunnel.local_port}",
                f"Target = {_safe(join_endpoint(tunnel.host, tunnel.port), 'An exit')}",
            ]
        )
        for tunnel in tunnels
    )
    return "\n\n".join(blocks) + "\n"


__all__ = [
    "PERSISTENT_KEEPALIVE_SECONDS",
    "RESOLVE_STRATEGY",
    "WARP_ENDPOINT_SUFFIX",
    "WARP_PEER_PUBLIC_KEY",
    "WIREGUARD_VENDORS",
    "ImportedProfile",
    "ProfileError",
    "RunnerTunnel",
    "WireGuardProfile",
    "WireGuardVendor",
    "join_endpoint",
    "parse_wireguard_file",
    "render_wireproxy_config",
    "split_endpoint",
    "vendors_document",
    "wgcf_account_private_key",
    "wireguard_vendor",
]
