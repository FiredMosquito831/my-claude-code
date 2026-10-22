"""WireGuard runner sources: import a file, offer its addresses (7.108.0).

PR-R3 (+ R4, R5): a WireGuard file the operator pasted or uploaded becomes a
``runner`` source -- one wireproxy MCC starts and stops -- and the source
offers ordinary chain addresses, exactly as a scanned listener does:

* ``socks5h://<local user>:<local password>@127.0.0.1:<port>``, the tunnel's
  own exit, named ``<Vendor> · <server>`` from the file's ``# name`` comment
  or its file name, unique in the catalogue by S1's rule;
* one ``socks5h://127.0.0.1:<port>`` per in-tunnel exit (R4: Mullvad's and
  IVPN's per-server SOCKS5 proxies, reachable only through the tunnel),
  which wireproxy forwards byte for byte, so those take no local login --
  the vendor's proxy takes none either.

Saving contacts nothing and starts nothing: the runtime's supervisor starts
the runner when the operator presses *Start*. Adding an offer to a chain is
the chain page's one bulk add, which tests it against the provider's own
host first -- through the running tunnel.

The private key goes into the owner-only ``proxy_sources.json`` (a
``wireguard`` secret) and into the owner-only config the supervisor writes
from it; never into ``proxy_chains.json``, a response, a log line or the
request log. A WARP account file is read only to check its key matches the
profile's: its token and licence key are never stored (MCC never registers
or calls Cloudflare -- the user's decision 4).
"""

import secrets
import socket
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from my_claude_code.application.proxy_sources import SourceEditError
from my_claude_code.application.proxy_vendor_sources import (
    next_vendor_source_id,
    unique_source_name,
)
from my_claude_code.config.proxy_chains import (
    SOURCE_SOURCE,
    ProxyChains,
    ProxyEndpoint,
)
from my_claude_code.config.proxy_source_readers import parse_host_lines
from my_claude_code.config.proxy_sources import (
    KIND_RUNNER,
    LOCAL_HOST,
    RUNNER_SOCKS_KEY,
    SECRET_USERPASS,
    SECRET_WIREGUARD,
    ProxySource,
    ProxySources,
    RunnerExit,
    RunnerSettings,
    SourceSecret,
    mint_secret_id,
    vendor_offer_id,
)
from my_claude_code.config.settings import parse_port_range
from my_claude_code.config.wireguard_profiles import (
    ImportedProfile,
    ProfileError,
    RunnerTunnel,
    WireGuardProfile,
    parse_wireguard_file,
    render_wireproxy_config,
    wgcf_account_private_key,
    wireguard_vendor,
)

SOCKS_KEY = RUNNER_SOCKS_KEY
#: The most in-tunnel exits one runner forwards.
EXITS_MAX = 64

PortFree = Callable[[int], bool]


def port_binds(port: int) -> bool:
    """Whether ``127.0.0.1:<port>`` can be bound right now, by anybody.

    Exclusive on Windows, so a port another program holds is never reported
    free (``SO_REUSEADDR`` there would let us bind over it). Elsewhere
    ``SO_REUSEADDR`` is set, as wireproxy's own listener sets it: it never
    binds over a listening socket, but a port left in TIME_WAIT by the
    runner just stopped (its ``/readyz`` closes each connection first) is
    free for the runner that replaces it, so Restart is not refused.
    """

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((LOCAL_HOST, port))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def _ports_of_others(sources: ProxySources, own_id: str) -> set[int]:
    return {
        port
        for source_id, source in sources.sources.items()
        if source_id != own_id and source.runner is not None
        for port in source.runner.ports()
    }


def allocate_ports(
    count: int,
    *,
    port_range: str,
    taken: Iterable[int],
    port_free: PortFree = port_binds,
) -> tuple[int, ...]:
    """``count`` loopback ports from the range, none taken, each binding now."""

    if count <= 0:
        return ()
    low, high = parse_port_range(port_range)
    held = set(taken)
    chosen: list[int] = []
    for port in range(low, high + 1):
        if port in held:
            continue
        if port_free(port):
            chosen.append(port)
            held.add(port)
            if len(chosen) == count:
                return tuple(chosen)
    raise SourceEditError(
        f"Not enough free ports in PROXY_RUNNER_PORTS ({port_range}): this "
        f"needs {count}, {len(chosen)} are free. Widen the range on Limits & "
        "Resilience, or remove a runner you no longer use."
    )


@dataclass(frozen=True, slots=True)
class RunnerEdit:
    """The import form, as the store applies it. ``None`` keeps a field.

    ``profile_text`` and ``account_text`` are write-only: empty keeps the
    stored profile, and nothing ever sends either back.
    """

    profile_text: str = ""
    filename: str = ""
    account_text: str = ""
    name: str | None = None
    vendor: str | None = None
    exits: str | None = None
    auto_restart: bool | None = None
    repick_ports: bool = False


def _imported(edit: RunnerEdit) -> ImportedProfile | None:
    if not edit.profile_text.strip():
        if edit.account_text.strip():
            raise SourceEditError(
                "A wgcf-account.toml alone is not a tunnel: paste the "
                "wgcf-profile.conf that `wgcf generate` writes beside it. MCC "
                "never registers a WARP device or calls Cloudflare's API."
            )
        return None
    try:
        imported = parse_wireguard_file(edit.profile_text, edit.filename)
        if edit.account_text.strip():
            account_key = wgcf_account_private_key(edit.account_text)
            if account_key != imported.profile.private_key:
                raise SourceEditError(
                    "The wgcf-account.toml's private_key is not this profile's: "
                    "they are from two different registrations."
                )
    except ProfileError as exc:
        raise SourceEditError(str(exc)) from None
    return imported


def _exits(text: str, default_port: int) -> tuple[tuple[str, int], ...]:
    hosts, bad = parse_host_lines(text)
    if bad:
        raise SourceEditError(
            "Not a host or host:port: " + ", ".join(bad[:5]) + ". One per line."
        )
    wanted: list[tuple[str, int]] = []
    for typed in hosts:
        item = (typed.host, typed.port or default_port)
        if item not in wanted:
            wanted.append(item)
    if len(wanted) > EXITS_MAX:
        raise SourceEditError(f"One runner forwards at most {EXITS_MAX} exits.")
    return tuple(wanted)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _socks_url(login: SourceSecret, port: int) -> str:
    return f"socks5h://{login.username}:{login.password}@{LOCAL_HOST}:{port}"


def _exit_url(port: int) -> str:
    return f"socks5h://{LOCAL_HOST}:{port}"


def _exit_label(short: str, host: str) -> str:
    return f"{short} · {host.split('.', 1)[0]}"


def _runner_offers(
    chains: ProxyChains,
    source_id: str,
    wanted: Sequence[tuple[str, str, str]],
    at: str,
) -> list[tuple[str, ProxyEndpoint]]:
    """``(key, url, label)`` -> offers. A known row keeps its name and takes
    the new URL; a new one's name steps around every name in the catalogue."""

    keyed = [
        (vendor_offer_id(source_id, key), url, label) for key, url, label in wanted
    ]
    own = {proxy_id for proxy_id, _, _ in keyed}
    taken = {
        label
        for proxy_id, label in chains.ledger_labels().items()
        if proxy_id not in own and label
    }
    offers: list[tuple[str, ProxyEndpoint]] = []
    for proxy_id, url, label in keyed:
        known = chains.endpoint(proxy_id)
        if known is not None:
            taken.add(chains.ledger_label(proxy_id))
            offers.append((proxy_id, replace(known, url=url)))
            continue
        unique = label
        number = 2
        while unique in taken:
            unique = f"{label} ({number})"
            number += 1
        taken.add(unique)
        offers.append(
            (
                proxy_id,
                ProxyEndpoint(
                    url=url,
                    label=unique,
                    added_at=at,
                    source=SOURCE_SOURCE,
                    source_id=source_id,
                ),
            )
        )
    return offers


def save_runner_source(
    chains: ProxyChains,
    sources: ProxySources,
    source_id: str,
    edit: RunnerEdit,
    *,
    port_range: str,
    port_free: PortFree = port_binds,
) -> tuple[ProxyChains, ProxySources, str]:
    """Create (``source_id`` empty) or change a runner source. Contacts nothing.

    Answers the two stores to save and the source's id. The ports are chosen
    once and kept, so the offered addresses keep their URLs and names; an
    exit added later gets a port of its own, and *Pick new ports* chooses
    every port again.
    """

    existing = sources.source(source_id) if source_id else None
    if source_id and (existing is None or existing.runner is None):
        raise SourceEditError(f"No WireGuard runner {source_id!r}.")
    before = existing.runner if existing is not None else None
    imported = _imported(edit)
    if before is None and imported is None:
        raise SourceEditError("Paste or upload the WireGuard file to import.")
    vendor = wireguard_vendor(
        edit.vendor
        if edit.vendor is not None
        else (
            before.vendor
            if before is not None
            else imported.vendor
            if imported is not None
            else ""
        )
    )
    if imported is not None and imported.profile.is_warp and edit.vendor is None:
        vendor = wireguard_vendor("warp")
    stored = sources
    profile_id = (
        before.profile if before is not None else mint_secret_id(sources.secrets)
    )
    if imported is not None:
        stored = stored.with_secret(
            profile_id, SourceSecret(type=SECRET_WIREGUARD, profile=imported.profile)
        )
    elif stored.secret(profile_id) is None:
        raise SourceEditError(
            "This runner's WireGuard profile is missing from proxy_sources.json: "
            "import the file again."
        )
    login_id = before.login if before is not None else mint_secret_id(stored.secrets)
    login = stored.secret(login_id)
    if login is None or not login.username:
        login = SourceSecret(
            type=SECRET_USERPASS,
            username=f"mcc-{secrets.token_hex(4)}",
            password=secrets.token_hex(16),
        )
        stored = stored.with_secret(login_id, login)
    if edit.exits is not None:
        wanted_exits = _exits(edit.exits, vendor.in_tunnel_port)
    else:
        wanted_exits = tuple(
            (item.host, item.port) for item in (before.exits if before else ())
        )
    previous_ports = (
        {item.key: item.local_port for item in before.exits} if before else {}
    )
    others = _ports_of_others(sources, source_id)
    keep = before is not None and not edit.repick_ports
    kept: set[int] = set()
    if keep and before is not None:
        kept = {before.socks_port, before.health_port}
        kept |= {
            port
            for key, port in previous_ports.items()
            if key in {f"exit:{host}:{port_}" for host, port_ in wanted_exits}
        }
    needed = (0 if keep else 2) + sum(
        1
        for host, port in wanted_exits
        if not keep or f"exit:{host}:{port}" not in previous_ports
    )
    fresh = list(
        allocate_ports(
            needed,
            port_range=port_range,
            taken=others | kept,
            port_free=port_free,
        )
    )
    if keep and before is not None:
        socks_port, health_port = before.socks_port, before.health_port
    else:
        socks_port, health_port = fresh.pop(0), fresh.pop(0)
    exits: list[RunnerExit] = []
    for host, port in wanted_exits:
        key = f"exit:{host}:{port}"
        local_port = (
            previous_ports[key] if keep and key in previous_ports else fresh.pop(0)
        )
        exits.append(RunnerExit(host=host, port=port, local_port=local_port))
    runner = RunnerSettings(
        profile=profile_id,
        login=login_id,
        socks_port=socks_port,
        health_port=health_port,
        vendor=vendor.id,
        exits=tuple(exits),
        auto_restart=edit.auto_restart
        if edit.auto_restart is not None
        else bool(before and before.auto_restart),
    )
    server = imported.name if imported is not None else ""
    wanted_name = (
        edit.name.strip()
        if edit.name is not None and edit.name.strip()
        else existing.name
        if existing is not None
        else f"{vendor.short_name} {server}".strip()
    )
    new_id = source_id or next_vendor_source_id(sources, KIND_RUNNER, wanted_name)
    name = unique_source_name(stored, wanted_name, new_id)
    source = ProxySource(
        id=new_id,
        kind=KIND_RUNNER,
        name=name,
        enabled=existing.enabled if existing is not None else False,
        added_at=existing.added_at if existing is not None else _now(),
        runner=runner,
    )
    stored = stored.with_source(source)
    wanted = [
        (
            SOCKS_KEY,
            _socks_url(login, socks_port),
            f"{vendor.short_name} · {server or name}",
        ),
        *(
            (
                item.key,
                _exit_url(item.local_port),
                _exit_label(vendor.short_name, item.host),
            )
            for item in exits
        ),
    ]
    offers = _runner_offers(chains, new_id, wanted, _now())
    return chains.with_source_offers(new_id, offers), stored, new_id


def remove_runner_source(
    chains: ProxyChains, sources: ProxySources, source_id: str
) -> tuple[ProxyChains, ProxySources]:
    """Forget a runner source, its two secrets and its offers. A chain that
    took one of its addresses keeps it (dead, then benched, like any other)."""

    return chains.with_source_offers(source_id, []), sources.without_source(source_id)


def set_runner_enabled(
    sources: ProxySources, source_id: str, enabled: bool
) -> ProxySources:
    """Whether the runner starts with the server: what Start and Stop record."""

    source = sources.source(source_id)
    if source is None or source.runner is None:
        raise SourceEditError(f"No WireGuard runner {source_id!r}.")
    if source.enabled == enabled:
        return sources
    return sources.with_source(replace(source, enabled=enabled))


# --------------------------------------------------------------- the launch


@dataclass(frozen=True, slots=True)
class RunnerLaunch:
    """Everything the supervisor needs to start one runner, read from the store.

    ``scrub`` holds the values no log line may carry -- the private key, a
    preshared key and the local login -- so the supervisor can redact them
    from wireproxy's own output.
    """

    source_id: str
    config_text: str
    socks_port: int
    health_port: int
    ports: tuple[int, ...]
    check_alive: str
    scrub: tuple[str, ...]
    auto_restart: bool


def runner_launch(source: ProxySource, sources: ProxySources) -> RunnerLaunch:
    """The config to start ``source`` with, regenerated from the store now."""

    runner = source.runner
    if runner is None:
        raise SourceEditError(f"{source.name or source.id} is not a WireGuard runner.")
    profile_secret = sources.secret(runner.profile)
    login = sources.secret(runner.login)
    profile: WireGuardProfile | None = (
        profile_secret.profile if profile_secret is not None else None
    )
    if profile is None or login is None or not login.username:
        raise SourceEditError(
            f"{source.name}'s WireGuard profile or local login is missing from "
            "proxy_sources.json: import the file again."
        )
    try:
        text = render_wireproxy_config(
            profile,
            source_id=source.id,
            socks_port=runner.socks_port,
            username=login.username,
            password=login.password,
            tunnels=[
                RunnerTunnel(item.local_port, item.host, item.port)
                for item in runner.exits
            ],
        )
    except ProfileError as exc:
        raise SourceEditError(str(exc)) from None
    return RunnerLaunch(
        source_id=source.id,
        config_text=text,
        socks_port=runner.socks_port,
        health_port=runner.health_port,
        ports=runner.ports(),
        check_alive=profile.check_alive,
        scrub=(*profile.secrets, login.username, login.password),
        auto_restart=runner.auto_restart,
    )


def runner_source_ids(
    sources: ProxySources, *, enabled_only: bool = False
) -> list[str]:
    return [
        source_id
        for source_id, source in sources.sources.items()
        if source.runner is not None and (source.enabled or not enabled_only)
    ]


__all__ = [
    "EXITS_MAX",
    "SOCKS_KEY",
    "RunnerEdit",
    "RunnerLaunch",
    "allocate_ports",
    "port_binds",
    "remove_runner_source",
    "runner_launch",
    "runner_source_ids",
    "save_runner_source",
    "set_runner_enabled",
]
