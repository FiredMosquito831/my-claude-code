"""Vendor presets for proxy sources: READERS, not sources (7.91.0, PR-S4 + PR-S5).

``config/proxy_feeds.py`` says why MCC ships no list of anybody's endpoints: a
*parser* is a fact about a format, a *catalogue* is a claim that a stranger's
host will be there tomorrow, and a fresh install must contact nobody. Vendor
presets keep that rule (the user's decision 5 of 2026-09-25: "readers, not
sources -- the vendor URL prefilled with its documentation link for the user to
confirm; no shipped vendor hostnames"):

* a preset **prefills a form** -- the host the vendor documents, its port, the
  session syntax, where its credentials come from, the documentation link and
  the day that fact was read -- and nothing more;
* nothing is stored, offered or contacted until the operator saves the form,
  and what is stored is what they confirmed (they may change every field);
* a vendor's server list is fetched only from the URL the operator confirmed,
  only when they press *Fetch now* or switch on that source's schedule.

The facts below come from ``specs/INVESTIGATION-PROXY-SOURCES-VPN-TOR.md`` §5
(read 2026-10-06) and ``specs/PR-PROXY-SOURCES-SPEC.md`` §4 (read 2026-09-25).
Each carries its evidence tag: ``V`` documented by the vendor, ``CS`` from a
search snippet of the vendor's own page, ``U`` third-party or unverified.

Tunnel-only VPNs (Proton VPN free and Plus, Surfshark, ExpressVPN, Windscribe)
have no per-request proxy at all, so they have no preset: the Guide says what
works with them (the app on, MCC going out Direct through it).
"""

from dataclasses import dataclass
from typing import Any

#: The three kinds a preset can prefill.
PRESET_ACCOUNT = "account"
PRESET_GATEWAY = "gateway"
PRESET_LIST = "list"

#: The readers a source can name. ``nordvpn_servers`` reads NordVPN's server
#: list JSON; ``webshare_list`` reads ``ip:port:username:password`` lines.
READER_NORDVPN_SERVERS = "nordvpn_servers"
READER_PROXY_LIST = "webshare_list"
READERS: tuple[str, ...] = (READER_NORDVPN_SERVERS, READER_PROXY_LIST)

#: How a gateway's session is spelled -- one per vendor, each a reader of
#: that vendor's documented syntax (see ``application.proxy_vendor_sources``).
TEMPLATE_BRIGHTDATA = "brightdata"
TEMPLATE_OXYLABS = "oxylabs"
TEMPLATE_IPROYAL = "iproyal"
TEMPLATE_DECODO = "decodo"
GATEWAY_TEMPLATES: tuple[str, ...] = (
    TEMPLATE_BRIGHTDATA,
    TEMPLATE_OXYLABS,
    TEMPLATE_IPROYAL,
    TEMPLATE_DECODO,
)

#: Bright Data zone types. Residential and mobile zones in Bright Data's
#: "native" mode need its certificate authority installed -- TLS interception,
#: which MCC refuses -- so they are refused here, with that reason (the user's
#: decision 13 of 2026-10-06: "Bright Data residential stays refused").
ZONE_DATACENTER = "datacenter"
ZONE_ISP = "isp"
ZONE_RESIDENTIAL = "residential"
ZONE_MOBILE = "mobile"
ZONE_TYPES: tuple[str, ...] = (ZONE_DATACENTER, ZONE_ISP, ZONE_RESIDENTIAL, ZONE_MOBILE)
ZONES_REFUSED: tuple[str, ...] = (ZONE_RESIDENTIAL, ZONE_MOBILE)
BRIGHTDATA_REFUSAL = (
    "Bright Data's residential and mobile zones need Bright Data's own "
    "certificate authority installed on this computer so it can decrypt your "
    "traffic (TLS interception). MCC never weakens certificate checking, so it "
    "refuses them -- use a datacenter or ISP zone, which Bright Data does not "
    "list as needing it."
)


@dataclass(frozen=True, slots=True)
class VendorPreset:
    """One vendor's documented shape. Prefills a form; stores nothing."""

    id: str
    kind: str
    name: str
    #: The scheme and port the vendor documents for its proxy.
    scheme: str
    port: int
    #: Hosts the vendor documents (an account) -- shown in the form for the
    #: operator to keep, edit or replace; never offered on their own.
    hosts: tuple[str, ...] = ()
    #: A gateway's one host.
    host: str = ""
    #: Another scheme/port pair the vendor documents, shown as a hint.
    alternative: str = ""
    #: Where the credentials come from, in the vendor's words.
    credentials: str = ""
    #: ``True``: the addresses answer only while the vendor's own tunnel is up
    #: (Mullvad, IVPN): the offers say so.
    in_tunnel: bool = False
    #: A server-list reader and the vendor URL it reads, prefilled for the
    #: operator to confirm (account) or replace with their own link (list).
    reader: str = ""
    list_url: str = ""
    #: A gateway's session syntax (one of :data:`GATEWAY_TEMPLATES`).
    template: str = ""
    #: The session lifetime field's prefill, in minutes, and its bounds as
    #: the vendor documents them (``0`` = the vendor has no such field).
    minutes: int = 0
    minutes_max: int = 0
    doc: str = ""
    observed: str = ""
    evidence: str = ""
    note: str = ""

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "scheme": self.scheme,
            "port": self.port,
            "hosts": list(self.hosts),
            "host": self.host,
            "alternative": self.alternative,
            "credentials": self.credentials,
            "in_tunnel": self.in_tunnel,
            "reader": self.reader,
            "list_url": self.list_url,
            "template": self.template,
            "minutes": self.minutes,
            "minutes_max": self.minutes_max,
            "doc": self.doc,
            "observed": self.observed,
            "evidence": self.evidence,
            "note": self.note,
        }
        if self.template == TEMPLATE_BRIGHTDATA:
            payload["zone_types"] = list(ZONE_TYPES)
            payload["zones_refused"] = list(ZONES_REFUSED)
            payload["refusal"] = BRIGHTDATA_REFUSAL
        return payload


PRESETS: tuple[VendorPreset, ...] = (
    # ------------------------------------------------------- VPN accounts
    VendorPreset(
        id="nordvpn",
        kind=PRESET_ACCOUNT,
        name="NordVPN",
        scheme="socks5h",
        port=1080,
        hosts=(
            "nl.socks.nordhold.net",
            "se.socks.nordhold.net",
            "us.socks.nordhold.net",
        ),
        credentials=(
            "Service credentials, not your login: Nord Account -> Set up "
            "NordVPN manually -> service credentials (an e-mailed code confirms)."
        ),
        reader=READER_NORDVPN_SERVERS,
        list_url=(
            "https://api.nordvpn.com/v1/servers"
            "?filters[servers_technologies][identifier]=socks&limit=200"
        ),
        doc="https://support.nordvpn.com/hc/en-us/articles/20195967385745",
        observed="2026-10-06",
        evidence="V",
        note=(
            "SOCKS5 servers exist in the Netherlands, Sweden and the United "
            "States only (71 listed on 2026-09-25); at most 10 connections at "
            "once per account. Nord also documents an HTTPS proxy on port 89: "
            "it is untested with MCC (an httpx user reported a TLS error "
            "through it), so it is not offered."
        ),
    ),
    VendorPreset(
        id="pia",
        kind=PRESET_ACCOUNT,
        name="Private Internet Access",
        scheme="socks5h",
        port=1080,
        hosts=("proxy-nl.privateinternetaccess.com",),
        credentials=(
            "Generated SOCKS credentials (they start with x): Client Control "
            "Panel -> Downloads -> SOCKS -> Generate."
        ),
        doc=(
            "https://helpdesk.privateinternetaccess.com/hc/en-us/articles/"
            "46565552041883-How-to-Generate-Proxy-Credentials"
        ),
        observed="2026-10-06",
        evidence="CS",
        note="One exit, in the Netherlands.",
    ),
    VendorPreset(
        id="ipvanish",
        kind=PRESET_ACCOUNT,
        name="IPVanish",
        scheme="socks5h",
        port=1080,
        hosts=("ams.socks.ipvanish.com",),
        credentials=(
            "The separate SOCKS5 proxy credentials in your IPVanish account, "
            "not your login."
        ),
        doc="https://www.ipvanish.com/",
        observed="2026-09-25",
        evidence="U",
        note=(
            "Host names follow <city>.socks.ipvanish.com; the one shown is a "
            "third party's 2021 example (vpnuniversity.com) -- IPVanish's "
            "support pages could not be read, so check your account's list."
        ),
    ),
    VendorPreset(
        id="torguard",
        kind=PRESET_ACCOUNT,
        name="TorGuard",
        scheme="socks5h",
        port=1080,
        hosts=("proxy.torguard.org",),
        alternative="SOCKS5 also on 1085 and 1090; an HTTP proxy on 8080",
        credentials="Your TorGuard proxy (service) credentials.",
        doc="https://torguard.net/",
        observed="2026-09-25",
        evidence="U",
        note="Read from search snippets of TorGuard's support pages (403 to a fetcher).",
    ),
    VendorPreset(
        id="mullvad",
        kind=PRESET_ACCOUNT,
        name="Mullvad (inside its tunnel)",
        scheme="socks5h",
        port=1080,
        hosts=("10.64.0.1",),
        credentials="None: Mullvad's SOCKS5 proxies take no login.",
        in_tunnel=True,
        doc="https://mullvad.net/en/help/socks5-proxy",
        observed="2026-10-06",
        evidence="V",
        note=(
            "Answers only while the Mullvad app's WireGuard tunnel is "
            "connected. 10.64.0.1 is the server you are connected to; each "
            "other server's proxy is "
            "<country>-<city>-wg-socks5-<number>.relays.mullvad.net "
            "(e.g. nl-ams-wg-socks5-001.relays.mullvad.net), reachable from "
            "any Mullvad server -- add the ones you want, one per line."
        ),
    ),
    VendorPreset(
        id="ivpn",
        kind=PRESET_ACCOUNT,
        name="IVPN (inside its tunnel)",
        scheme="socks5h",
        port=1080,
        hosts=("10.1.0.1",),
        credentials="None: IVPN's SOCKS5 proxies take no login.",
        in_tunnel=True,
        doc="https://www.ivpn.net/knowledgebase/general/socks5-proxy-service/",
        observed="2026-09-25",
        evidence="V",
        note=(
            "Answers only while the IVPN app's tunnel is connected. Each "
            "server's own proxy is socks5.<server id>.gw.ivpn.net, listed on "
            "ivpn.net/status."
        ),
    ),
    # ---------------------------------------------------- commercial gateways
    VendorPreset(
        id="brightdata",
        kind=PRESET_GATEWAY,
        name="Bright Data",
        scheme="socks5h",
        port=22228,
        host="brd.superproxy.io",
        alternative="HTTP proxy on port 44445 (22225 and 33335 retired 2026-09-25)",
        credentials=(
            "Your customer id (brd-customer-<id>), the zone's name and the "
            "zone's password, from the zone's Access parameters."
        ),
        template=TEMPLATE_BRIGHTDATA,
        doc="https://docs.brightdata.com/proxy-networks/socks5",
        observed="2026-10-06",
        evidence="V",
        note=(
            "SOCKS5 takes host names only (socks5h). Each unique session id "
            "gets its own address. Datacenter and ISP zones only: residential "
            "and mobile zones need Bright Data's certificate installed, which "
            "MCC refuses."
        ),
    ),
    VendorPreset(
        id="oxylabs",
        kind=PRESET_GATEWAY,
        name="Oxylabs",
        scheme="socks5h",
        port=7777,
        host="pr.oxylabs.io",
        credentials="Your Oxylabs proxy user name and password.",
        template=TEMPLATE_OXYLABS,
        minutes=30,
        minutes_max=1440,
        doc=(
            "https://developers.oxylabs.io/products/proxies/residential-proxies/"
            "session-control"
        ),
        observed="2026-10-06",
        evidence="V",
        note=(
            "A session keeps its address for its session time or until it is "
            "idle 60 s, whichever comes first; 30 minutes or more keeps a "
            "quiet thinking pause from changing the address mid-answer."
        ),
    ),
    VendorPreset(
        id="iproyal",
        kind=PRESET_GATEWAY,
        name="IPRoyal",
        scheme="http",
        port=12321,
        host="geo.iproyal.com",
        alternative="SOCKS5 on port 32325 (unverified)",
        credentials=(
            "Your IPRoyal proxy user name and password. The session goes in "
            "the PASSWORD: MCC adds _country-, _session- and _lifetime- to it."
        ),
        template=TEMPLATE_IPROYAL,
        minutes=30,
        minutes_max=10080,
        doc="https://docs.iproyal.com/proxies/residential/proxy/rotation",
        observed="2026-10-06",
        evidence="V",
        note="Without a session IPRoyal gives a new address on every request.",
    ),
    VendorPreset(
        id="decodo",
        kind=PRESET_GATEWAY,
        name="Decodo (Smartproxy)",
        scheme="socks5h",
        port=7000,
        host="gate.decodo.com",
        alternative="HTTP and HTTPS on the same port",
        credentials="Your Decodo proxy user name and password.",
        template=TEMPLATE_DECODO,
        minutes=30,
        minutes_max=1440,
        doc="https://help.decodo.com/docs/residential-proxy-custom-sticky-sessions",
        observed="2026-10-06",
        evidence="CS",
        note="SOCKS5 keeps an address only with a session, which MCC always adds.",
    ),
    # ----------------------------------------------------------- proxy lists
    VendorPreset(
        id="webshare",
        kind=PRESET_LIST,
        name="Webshare",
        scheme="socks5h",
        port=0,
        alternative="the same ports answer HTTP (unverified)",
        credentials=(
            "Paste your list (ip:port:username:password, one per line), or the "
            "list's download link from your Webshare dashboard -- the link "
            "carries your token, so MCC keeps it like a password."
        ),
        reader=READER_PROXY_LIST,
        list_url=(
            "https://proxy.webshare.io/api/v2/proxy/list/download/"
            "YOUR-TOKEN/-/any/username/direct/-/"
        ),
        doc="https://apidocs.webshare.io/proxy-list/download",
        observed="2026-10-06",
        evidence="CS",
        note=(
            "The free plan has 10 datacenter proxies and 1 GB a month, shared "
            "with other users, and some sites block them (Webshare's own "
            "words, page dated 2024-10-04)."
        ),
    ),
)

PRESETS_BY_ID: dict[str, VendorPreset] = {preset.id: preset for preset in PRESETS}


def preset(preset_id: str) -> VendorPreset | None:
    return PRESETS_BY_ID.get(str(preset_id or "").strip().lower())


def presets_document() -> dict[str, Any]:
    """What the Sources forms prefill from: data, never an offer."""

    return {
        "presets": [item.as_payload() for item in PRESETS],
        "readers": list(READERS),
        "templates": list(GATEWAY_TEMPLATES),
    }


__all__ = [
    "BRIGHTDATA_REFUSAL",
    "GATEWAY_TEMPLATES",
    "PRESETS",
    "PRESETS_BY_ID",
    "PRESET_ACCOUNT",
    "PRESET_GATEWAY",
    "PRESET_LIST",
    "READERS",
    "READER_NORDVPN_SERVERS",
    "READER_PROXY_LIST",
    "TEMPLATE_BRIGHTDATA",
    "TEMPLATE_DECODO",
    "TEMPLATE_IPROYAL",
    "TEMPLATE_OXYLABS",
    "ZONES_REFUSED",
    "ZONE_DATACENTER",
    "ZONE_ISP",
    "ZONE_MOBILE",
    "ZONE_RESIDENTIAL",
    "ZONE_TYPES",
    "VendorPreset",
    "preset",
    "presets_document",
]
