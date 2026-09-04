"""Browser-friendly local server URLs shared by runtime and launchers."""

from my_claude_code.config.settings import Settings


def local_browser_host(settings: Settings) -> str:
    """Host fragment for URLs shown to humans on the same machine as the server.

    Public because a second process needs the same answer: the bind
    host is ``0.0.0.0`` by default and is not a host anything can
    connect to, so ``mcc-desktop --print-status`` reports the mapped
    loopback address rather than teaching its reader the mapping.
    """

    host = settings.host.strip() if settings.host else "127.0.0.1"
    if host in {"0.0.0.0", "::", "[::]"}:
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return host


def local_proxy_root_url(settings: Settings) -> str:
    """Return the proxy root URL (no path) for clients on the same machine."""

    return f"http://{local_browser_host(settings)}:{settings.port}"


def local_admin_url(settings: Settings) -> str:
    """Return a browser-friendly URL for the localhost-only admin UI."""

    return f"{local_proxy_root_url(settings)}/admin"
