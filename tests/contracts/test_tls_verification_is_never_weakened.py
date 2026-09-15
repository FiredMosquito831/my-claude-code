"""No shipped source file may weaken TLS certificate verification.

This is the finding that clears proxy chains as a feature rather than the
feature itself, which is why it is pinned here and not beside them.

MCC talks to every provider over HTTPS, and with a proxy configured it does so
through a ``CONNECT`` tunnel. Because nothing in this codebase passes
``verify=`` to ``httpx``, every client it builds gets the library default --
``ssl.create_default_context(cafile=certifi.where())``, ``check_hostname=True``,
``verify_mode=CERT_REQUIRED``. That single fact is what makes routing through a
stranger's proxy a *reliability* question rather than a credential one: the
proxy relays an opaque tunnel and cannot read or alter an API key, an OAuth
token, a prompt or a reply. It can only break the connection, which MCC sees as
an ordinary failure.

The property is invisible: it holds because of code that is *absent*. Nothing
in the type checker, the linter or any unit test would notice a ``verify=False``
added in a hurry to make one stubborn provider work, and the symptom would be
silence -- the request would start succeeding. So the absence is asserted.

Three honest caveats, recorded here because a reader of this file is exactly
the person who needs them and they do not belong in a docstring nobody opens:

* ``httpx`` defaults ``trust_env=True`` and MCC never overrides it, so
  ``SSL_CERT_FILE`` / ``SSL_CERT_DIR`` in the *server process's* environment
  replace the trust root, and ``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``ALL_PROXY``
  are honoured by any client MCC does not hand an explicit ``proxy=``. Neither
  *disables* verification -- swapping a CA bundle requires the operator to have
  already installed that root on the machine -- and both are standard Python
  behaviour, not an MCC setting. This test guards what MCC's own code does.
* ``providers/vertex/auth.py`` uses ``requests`` rather than ``httpx`` for the
  Google token exchange. Same conclusion, different library and different
  environment variables, which is why ``REQUESTS_CA_BUNDLE`` is on the list.
* Strict TLS hides the payload, not the metadata. A proxy still sees the
  destination hostname through the ``CONNECT`` target and SNI, the timing, and
  the byte volume in each direction. It learns which provider you use, when,
  and how much you send; it does not learn your key, your prompts or your
  replies. The Proxying page says exactly that, once.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "src"

#: Every way this codebase's dependencies can be told to trust less, plus the
#: two environment variables that would carry a key or a bundle in from
#: outside. ``verify`` is matched only as a keyword argument so that the word
#: in ordinary prose or an identifier such as ``verify_signature`` does not
#: trip it.
FORBIDDEN_PATTERNS: dict[str, re.Pattern[str]] = {
    # httpx / requests: the one keyword that turns verification off.
    "verify=": re.compile(r"\bverify\s*="),
    # ssl: the two settings a hand-built context uses to do the same thing.
    "CERT_NONE": re.compile(r"\bCERT_NONE\b"),
    "check_hostname": re.compile(r"\bcheck_hostname\b"),
    # ssl: the documented shortcut for an unverified context.
    "ssl._create_unverified": re.compile(r"_create_unverified"),
    # httpx: switching this off is not a weakening, but switching it *on*
    # deliberately in code would mean somebody is steering trust through the
    # environment on purpose, which is the same conversation.
    "trust_env=": re.compile(r"\btrust_env\s*="),
    # requests: an operator-supplied bundle read by MCC's own code rather than
    # by the library's default environment handling.
    "REQUESTS_CA_BUNDLE": re.compile(r"\bREQUESTS_CA_BUNDLE\b"),
    # openssl: the file that writes out session keys, which makes a tunnel
    # readable after the fact.
    "SSLKEYLOGFILE": re.compile(r"\bSSLKEYLOGFILE\b"),
}

#: Files allowed to name one of the strings above. This test and nothing else:
#: an entry here is a place where verification could be weakened, so adding one
#: is the change that needs the review, not the failure it produces.
ALLOWLIST: frozenset[str] = frozenset()


def _python_sources() -> list[Path]:
    return sorted(SOURCE_ROOT.rglob("*.py"))


def test_the_source_scan_is_not_vacuous() -> None:
    """A path that matched nothing would make the assertion below pass for free."""

    sources = _python_sources()
    assert len(sources) > 100, f"only {len(sources)} python files under {SOURCE_ROOT}"
    joined = "\n".join(path.read_text(encoding="utf-8") for path in sources[:50])
    assert joined.strip(), "read no source text at all"


def test_no_source_file_disables_certificate_verification() -> None:
    """Nothing in `src/` may weaken, redirect or record TLS verification.

    Measured rather than assumed: on the commit this test was written against,
    every pattern below returns zero lines across the whole package. The value
    of the test is entirely in the day that stops being true.
    """

    hits: list[str] = []
    for path in _python_sources():
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative in ALLOWLIST:
            continue
        text = path.read_text(encoding="utf-8")
        for name, pattern in FORBIDDEN_PATTERNS.items():
            for number, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    hits.append(f"{relative}:{number}: {name}: {line.strip()}")

    assert not hits, (
        "these source lines can weaken, redirect or record TLS certificate "
        "verification:\n"
        + "\n".join(hits)
        + "\n\nMCC speaks HTTPS to every provider with strict verification, and "
        "that is what makes routing through someone else's proxy safe for your "
        "credentials: the proxy relays a tunnel it cannot read. If a change "
        "here is genuinely required, it needs a reviewed entry in ALLOWLIST in "
        "tests/contracts/test_tls_verification_is_never_weakened.py and a line "
        "on the Proxying page, because it reverses what that page tells the "
        "operator."
    )
