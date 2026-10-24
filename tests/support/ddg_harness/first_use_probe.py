"""Fresh-interpreter probe: what the FIRST ``web_fetch`` of a process costs.

Run by ``first_use.run_probe`` as ``python -I first_use_probe.py <page> <content-type>``
(never imported). It builds the app the way a server start does, then times,
in order: importing the fetch path (``api.web_tools.outbound``), the first
``_run_web_fetch`` of one recorded page from a stdlib loopback server (so any
import the extractor defers to its first call -- trafilatura in DDG-4 -- is
inside this number), and a second, warm fetch of the same page. Prints one JSON
line. Loopback only: the server binds 127.0.0.1 and the egress policy allows
nothing but ``http``.
"""

import asyncio
import importlib
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PAGE = Path(sys.argv[1]).read_bytes()
CONTENT_TYPE = sys.argv[2]


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPE)
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{server.server_address[1]}/page"


def _top_level(names: set[str]) -> list[str]:
    return sorted({name.split(".", 1)[0] for name in names})


# The imports below are timed, so they are made here, by name, in order.
started = time.perf_counter()
importlib.import_module("my_claude_code.runtime.bootstrap")
importlib.import_module("my_claude_code.api.app")
app_ms = (time.perf_counter() - started) * 1000.0
before = set(sys.modules)
aiohttp_before = "aiohttp" in sys.modules

started = time.perf_counter()
outbound = importlib.import_module("my_claude_code.api.web_tools.outbound")
egress = importlib.import_module("my_claude_code.api.web_tools.egress")
import_ms = (time.perf_counter() - started) * 1000.0
policy = egress.WebFetchEgressPolicy(
    allow_private_network_targets=True, allowed_schemes=frozenset({"http"})
)

started = time.perf_counter()
first = asyncio.run(outbound._run_web_fetch(url, policy))
first_fetch_ms = (time.perf_counter() - started) * 1000.0
after_first = set(sys.modules)

started = time.perf_counter()
second = asyncio.run(outbound._run_web_fetch(url, policy))
second_fetch_ms = (time.perf_counter() - started) * 1000.0
server.shutdown()

new_modules = after_first - before
print(
    json.dumps(
        {
            "app_import_ms": round(app_ms, 1),
            "fetch_path_import_ms": round(import_ms, 1),
            "first_fetch_ms": round(first_fetch_ms, 1),
            "first_use_total_ms": round(import_ms + first_fetch_ms, 1),
            "second_fetch_ms": round(second_fetch_ms, 1),
            "modules_loaded_by_first_use": len(new_modules),
            "packages_loaded_by_first_use": _top_level(new_modules),
            "aiohttp_loaded_before_first_use": aiohttp_before,
            "first_data_chars": len(first["data"]),
            "second_equals_first": first == second,
        }
    )
)
