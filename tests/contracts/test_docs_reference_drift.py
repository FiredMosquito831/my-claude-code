"""Every name the docs spell out in code voice must exist in the product.

``test_docs_ui_labels`` already guards the *buttons* the docs tell you to press.
This guards the other four things a documentation page names mechanically, each
of which goes stale silently:

* **environment variables** -- ``Settings`` renames a field, the README keeps
  telling you to set the old one, and the user's edit does nothing at all;
* **console commands** -- ``mcc-<agent>`` launchers come and go with the harness
  registry, and a walkthrough that says ``mcc-kilo`` on a build without it is a
  dead end at the terminal;
* **HTTP routes** -- the "connect any client" recipes are copy-pasted base URLs;
  a path that moved turns every recipe into a 404;
* **dashboard page names** -- "open the Coding agents page from the left nav" is
  an instruction, and the nav is generated from ``VIEW_GROUPS``.

Part IX has carried "docs drift is unguarded" as a standing item. This closes
the mechanical half of it. It cannot check whether prose is *true* -- nothing
can -- only that every identifier it uses in code voice resolves.

Prose that deliberately names something outside the product (another tool's
variable, a setting documented as removed, an illustrative placeholder) belongs
in ``docs_reference_allowlist.txt`` beside this file, with the reason on the
line above it. Adding to that file is the intended escape hatch; editing this
test to weaken a pattern is not.
"""

import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ADMIN_STATIC = REPO_ROOT / "src/my_claude_code/api/admin_static"
ALLOWLIST_PATH = Path(__file__).resolve().parent / "docs_reference_allowlist.txt"

#: The user-facing documents this guard covers. The in-dashboard Guide is the
#: first place the house rules send a reader, so it is checked exactly like the
#: Markdown -- it is documentation that happens to be shipped as markup.
MARKDOWN_DOCS: tuple[str, ...] = (
    "README.md",
    "docs/USAGE.md",
)


def _read(path: Path) -> str:
    # The worktree checks out CRLF; read with universal newlines so the
    # patterns below never have to think about "\r".
    with open(path, encoding="utf-8", newline=None) as handle:
        return handle.read()


def _load_allowlist() -> frozenset[str]:
    if not ALLOWLIST_PATH.is_file():
        return frozenset()
    entries = set()
    for line in _read(ALLOWLIST_PATH).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        entries.add(stripped)
    return frozenset(entries)


ALLOWLIST = _load_allowlist()


# ------------------------------------------------------------------ surfaces


def _guide_text() -> str:
    """The Guide and Get Started views as plain text, tags stripped.

    Only the code-voice spans matter to this guard, so ``<code>`` contents are
    kept verbatim and every other tag is replaced by a space -- which also
    stops ``<a href="#guide-keys">`` contributing a false identifier.
    """
    markup = _read(ADMIN_STATIC / "index.html")
    start = markup.index('<section id="view-guide"')
    end = markup.index('<section id="view-docs"')
    guide = markup[start:end]

    onboarding_start = markup.index('id="getStartedPanel"')
    onboarding_end = markup.index("</section>", onboarding_start)
    guide += markup[onboarding_start:onboarding_end]

    # <code>FOO</code> -> `FOO` so the one code-voice pattern serves both
    # Markdown and markup.
    guide = re.sub(r"<code>(.*?)</code>", r"`\1`", guide, flags=re.S)
    return re.sub(r"<[^>]+>", " ", guide)


def _doc_surfaces() -> dict[str, str]:
    surfaces = {name: _read(REPO_ROOT / name) for name in MARKDOWN_DOCS}
    surfaces["admin_static/index.html (Guide + Get Started)"] = _guide_text()
    return surfaces


#: Anything inside single backticks or a fenced block -- "code voice". Prose
#: that merely mentions a word in passing is deliberately not checked; the
#: failure mode this guards is an instruction, and instructions are formatted.
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_FENCED = re.compile(r"```[a-z]*\n(.*?)```", re.S)


def _code_voice(text: str) -> list[str]:
    spans = [match.group(1) for match in _CODE_SPAN.finditer(text)]
    for block in _FENCED.finditer(text):
        spans.extend(block.group(1).splitlines())
    return spans


# ------------------------------------------------------------------- truths


def _settings_env_names() -> frozenset[str]:
    """Every env var name ``Settings`` will actually read."""
    from my_claude_code.config.settings import Settings

    names: set[str] = set()
    for field_name, field in Settings.model_fields.items():
        names.add(field_name.upper())
        alias = getattr(field, "alias", None)
        if alias:
            names.add(str(alias).upper())
        validation_alias = getattr(field, "validation_alias", None)
        if validation_alias is not None:
            if isinstance(validation_alias, str):
                names.add(validation_alias.upper())
            for attribute in ("choices", "aliases"):
                for choice in getattr(validation_alias, attribute, None) or ():
                    names.add(str(choice).upper())
    return frozenset(names)


#: Where a variable a launched agent reads can be declared. The registry is not
#: the only owner: each launcher and each catalogue serialiser names the
#: variables of the CLI it drives, and scanning only ``config/harnesses.py``
#: reported ``GOOSE_DISABLE_KEYRING`` and ``KIMI_CODE_HOME`` as nonexistent.
_ENV_DECLARING_DIRS: tuple[str, ...] = (
    "src/my_claude_code/config",
    "src/my_claude_code/cli",
    "src/my_claude_code/application/catalogues",
    "src/my_claude_code/websearch",
)

#: A SHOUTING string is treated as an env var name when it is assigned to a
#: module constant or used as a mapping key. Accepting every uppercase literal
#: anywhere would make this guard vacuous.
_ENV_CONSTANT = re.compile(
    r'^[A-Z_][A-Z0-9_]*\s*(?::\s*[^=\n]+)?=\s*"([A-Z][A-Z0-9_]*)"',
    re.M,
)
_ENV_IN_MAPPING = re.compile(r'"([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)"\s*[:,)\]]')


def _harness_env_names() -> frozenset[str]:
    """Env vars the harness layer defines: the launcher hands these out."""
    names: set[str] = set()
    for directory in _ENV_DECLARING_DIRS:
        for source in (REPO_ROOT / directory).rglob("*.py"):
            text = _read(source)
            names.update(_ENV_CONSTANT.findall(text))
            names.update(_ENV_IN_MAPPING.findall(text))
    return frozenset(names)


def _websearch_env_names() -> frozenset[str]:
    """Search-provider settings, which live in their own catalogue.

    ``Settings`` does not declare ``BRAVE_COUNTRY`` and friends -- the web
    search catalogue does -- so a guard that reads only ``Settings`` calls
    every tuning knob in section 9 of USAGE.md stale.
    """
    from my_claude_code.config.websearch_catalog import WEBSEARCH_CATALOG

    names: set[str] = set()

    def harvest(value: object) -> None:
        if isinstance(value, str):
            if re.fullmatch(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+", value):
                names.add(value)
            return
        if isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                harvest(item)
            return
        for attribute in ("key", "env", "credential_env"):
            candidate = getattr(value, attribute, None)
            if isinstance(candidate, str):
                harvest(candidate)
        fields = getattr(value, "__dict__", None)
        if isinstance(fields, dict):
            for item in fields.values():
                harvest(item)

    for descriptor in WEBSEARCH_CATALOG.values():
        harvest(descriptor)
    return frozenset(names)


def _rotation_names(base: frozenset[str]) -> frozenset[str]:
    """``<CREDENTIAL>_ROTATION`` is generated, never declared as a field."""
    from my_claude_code.websearch.registry import ROTATION_ENV_SUFFIX

    return frozenset(f"{name}{ROTATION_ENV_SUFFIX}" for name in base)


def _console_commands() -> frozenset[str]:
    """Console *and* GUI entry points.

    ``mcc-desktop`` lives in ``[project.gui-scripts]``; reading only
    ``[project.scripts]`` reported the tray launcher as a command that does not
    exist, which was this guard's own first false positive.
    """
    pyproject = tomllib.loads(_read(REPO_ROOT / "pyproject.toml"))
    project = pyproject["project"]
    return frozenset(project.get("scripts", {})) | frozenset(
        project.get("gui-scripts", {})
    )


def _known_routes() -> frozenset[str]:
    """Route paths, read from the decorators plus the wire-surface constants.

    Built statically rather than by constructing the app: a contract test that
    needs a live ``ApiServices`` would be an integration test, and the thing
    being guarded is a string in a decorator.
    """
    from my_claude_code.api import wire_surfaces

    paths: set[str] = set()
    for source in (REPO_ROOT / "src/my_claude_code/api").glob("*.py"):
        text = _read(source)
        for match in re.finditer(
            r"@\w+\.(?:get|post|put|delete|patch|head|options|api_route)\(\s*"
            r'(?:"([^"]+)"|([A-Z_]+)(?:\s*\+\s*"([^"]+)")?)',
            text,
        ):
            literal, constant, suffix = match.groups()
            if literal is not None:
                paths.add(literal)
                continue
            base = getattr(wire_surfaces, constant, None)
            if isinstance(base, str):
                paths.add(base + (suffix or ""))
    return frozenset(paths)


def _nav_page_names() -> frozenset[str]:
    """The left-nav labels, read out of ``VIEW_GROUPS`` in admin.js."""
    script = _read(ADMIN_STATIC / "admin.js")
    start = script.index("const VIEW_GROUPS = [")
    end = script.index("\n];", start)
    block = script[start:end]
    return frozenset(
        match.group(1) for match in re.finditer(r'label:\s*"([^"]+)"', block)
    ) | frozenset(match.group(1) for match in re.finditer(r'title:\s*"([^"]+)"', block))


# ------------------------------------------------------------- config-dir paths

#: The config directories the product actually uses. ``~/.mcc`` is the new
#: default; ``~/.fcc`` is the legacy home and ``~/.fcc-old`` the rollback-note
#: dir ``mcc-migrate`` writes. Quoted in code voice, a path must be one of
#: these -- anything else is a stale ``~/.fcc`` reference the docs drift back to.
_KNOWN_CONFIG_DIRS = frozenset({".mcc", ".fcc", ".fcc-old"})


def _config_dir_spans(text: str) -> list[str]:
    """Home-rooted directory paths in code voice, e.g. ``~/.mcc/.env``."""
    spans: list[str] = []
    for span in _code_voice(text):
        for match in re.finditer(r"~(/\.[A-Za-z0-9_.-]+)+/?", span):
            tail = match.group(0).split("~", 1)[1].rstrip("/")
            name = tail.rsplit("/", 1)[-1] if "/" in tail else tail.lstrip("/")
            if name in _KNOWN_CONFIG_DIRS:
                spans.append(match.group(0))
    return spans


# -------------------------------------------------------------------- tests


_ENV_TOKEN = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")

#: Markers that make a page reference a statement about the past rather than an
#: instruction to the reader. "They sat on the Limits page until 6.2.0" is a
#: true sentence naming a page that has since been renamed; only instructions
#: are guarded.
_HISTORICAL = re.compile(
    r"\b(until|moved|sat on|used to|no longer|renamed|was|were|before \d)\b",
    re.I,
)


@pytest.mark.parametrize("doc_name", sorted(_doc_surfaces()))
def test_documented_env_vars_exist(doc_name: str) -> None:
    """An env var named in code voice must be one the server reads."""
    declared = _settings_env_names() | _harness_env_names() | _websearch_env_names()
    known = declared | _rotation_names(declared) | ALLOWLIST
    text = _doc_surfaces()[doc_name]

    unknown: set[str] = set()
    for span in _code_voice(text):
        # ``NAME=value`` in a fenced .env block, or a bare ``NAME``.
        candidate = span.split("=", 1)[0].strip().lstrip("#").strip()
        if not _ENV_TOKEN.fullmatch(candidate):
            continue
        if candidate in known:
            continue
        unknown.add(candidate)

    assert not unknown, (
        f"{doc_name} names environment variables that no longer exist: "
        f"{sorted(unknown)}. Either the docs are stale, or the name belongs in "
        f"{ALLOWLIST_PATH.name} with a comment saying why it is not ours."
    )


@pytest.mark.parametrize("doc_name", sorted(_doc_surfaces()))
def test_documented_commands_exist(doc_name: str) -> None:
    """Every ``mcc-*``/``fcc-*`` command the docs tell you to run must ship."""
    known = _console_commands() | ALLOWLIST
    text = _doc_surfaces()[doc_name]

    unknown: set[str] = set()
    for span in _code_voice(text):
        # (?<![.\w-]) so ``providers.json.mcc-backup`` and the
        # ``x-mcc-harness`` header are not read as commands -- both were
        # reported as missing launchers on this guard's first run.
        for match in re.finditer(r"(?<![.\w-])((?:mcc|fcc)-[a-z0-9][a-z0-9-]*)", span):
            command = match.group(1)
            if command not in known:
                unknown.add(command)

    assert not unknown, (
        f"{doc_name} tells the reader to run commands that are not in "
        f"[project.scripts]: {sorted(unknown)}. A launcher that was renamed or "
        f"never existed leaves the walkthrough dead at the terminal."
    )


@pytest.mark.parametrize("doc_name", sorted(_doc_surfaces()))
def test_documented_routes_exist(doc_name: str) -> None:
    """A route quoted in a connect-your-client recipe must be served."""
    known = _known_routes()
    text = _doc_surfaces()[doc_name]

    unknown: set[str] = set()
    for span in _code_voice(text):
        for match in re.finditer(r"(/(?:v1|v1beta|admin)/[A-Za-z0-9_./{}:-]*)", span):
            path = match.group(1).rstrip("./")
            if path in known or path in ALLOWLIST:
                continue
            # A concrete instance of a templated route: ``/admin/api/requests/
            # {request_id}`` is served, so ``/admin/api/requests/abc123`` is a
            # real URL and not drift. Compare shape, not text.
            if any(re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", k), path) for k in known):
                continue
            shape = re.sub(r"\{[^}]+\}", "*", path)
            if shape in {re.sub(r"\{[^}]+\}", "*", k) for k in known}:
                continue
            # A base URL, not an endpoint: readers paste ``http://host/v1``
            # into an OpenAI SDK and the SDK appends the rest.
            if any(k.startswith(path + "/") for k in known):
                continue
            unknown.add(path)

    assert not unknown, (
        f"{doc_name} quotes HTTP paths the server does not serve: "
        f"{sorted(unknown)}. Every one of these is copy-pasted by a reader."
    )


@pytest.mark.parametrize("doc_name", sorted(_doc_surfaces()))
def test_documented_page_names_exist(doc_name: str) -> None:
    """ "Open the X page" must name a page the left nav actually has."""
    known = _nav_page_names()
    text = _doc_surfaces()[doc_name]

    unknown: set[str] = set()
    for match in re.finditer(
        r"(?:open|on|from) the\s+\*{0,2}([A-Z][A-Za-z& ]{2,28}?)\*{0,2}\s+"
        r"(?:page|tab|view)",
        text,
    ):
        name = match.group(1).strip()
        if name in known or name in ALLOWLIST:
            continue
        sentence = text[max(0, match.start() - 200) : match.end() + 80]
        if _HISTORICAL.search(sentence):
            continue
        unknown.add(name)

    assert not unknown, (
        f"{doc_name} sends the reader to pages the dashboard nav does not have: "
        f"{sorted(unknown)}. Nav labels come from VIEW_GROUPS in admin.js."
    )


def test_allowlist_has_no_dead_entries() -> None:
    """An allow-list entry that is now real should be removed, not left to rot.

    Without this the file only ever grows, and a name that was legitimately
    external until the product grew it stays permanently unchecked.
    """
    declared = _settings_env_names() | _harness_env_names() | _websearch_env_names()
    real = (
        declared
        | _rotation_names(declared)
        | _console_commands()
        | _known_routes()
        | _nav_page_names()
    )
    redundant = sorted(entry for entry in ALLOWLIST if entry in real)
    assert not redundant, (
        f"{ALLOWLIST_PATH.name} exempts names that now exist for real: "
        f"{redundant}. Delete those lines so the guard covers them."
    )


#: A ``~/.<name>`` path in code voice whose top segment is one of these is an
#: MCC config-dir reference and is checked; paths naming other tools
#: (``~/.claude``, ``~/.codex`` ...) are theirs and are never ours to guard.
_MCC_CONFIG_DIR_PATTERNS = (
    re.compile(r"~/\.fcc(?:/|$)"),
    re.compile(r"~/\.fcc-old(?:/|$)|~/\.fccold(?:/|$)"),
)


def _is_legacy_context(text: str, position: int) -> bool:
    """A ``~/.fcc`` mention sitting inside the legacy subsection is deliberate."""
    window = text[max(0, position - 260) : position + 80]
    return bool(_LEGACY_CONTEXT.search(window))


_LEGACY_CONTEXT = re.compile(
    r"\b(legacy|older|migrated|pre[- ]6\.40|mcc[- ]migrate|move (it|your data) "
    r"to|formerly|previous(ly)?|retired|was the old)\b",
    re.I,
)


@pytest.mark.parametrize("doc_name", sorted(_doc_surfaces()))
def test_docs_default_to_dot_mcc_not_dot_fcc(doc_name: str) -> None:
    """An ``~/.fcc`` reference outside the legacy subsection is drift.

    This is the fifth check the rename needs: the new default is ``~/.mcc``,
    so the docs should say ``~/.mcc`` everywhere the legacy home is not being
    discussed on purpose. The "Legacy ~/.fcc" subsection and the
    ``mcc-migrate`` walkthrough legitimately quote ``~/.fcc``; everything else
    is a re-drift to the old name. Other tools' directories (``~/.claude``,
    ``~/.codex``) are not MCC config dirs and are never flagged.
    """
    text = _doc_surfaces()[doc_name]
    drifted: set[str] = set()
    for pattern in _MCC_CONFIG_DIR_PATTERNS:
        for match in pattern.finditer(text):
            if _is_legacy_context(text, match.start()):
                continue
            drifted.add(match.group(0))

    assert not drifted, (
        f"{doc_name} quotes {sorted(drifted)} outside the legacy subsection; "
        f"the new default is ~/.mcc. Move the mention into a Legacy ~/.fcc "
        f"subsection, or update it to ~/.mcc."
    )
