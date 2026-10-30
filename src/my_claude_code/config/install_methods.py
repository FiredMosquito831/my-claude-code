"""How each coding agent and desktop app is installed, declared as data (7.102.0).

The Coding agents page gives every card that is not installed an **Install**
button. This module is everything that button is allowed to do, one row per
method per operating system, so the runner in ``application/agent_installer``
reads fields and never branches on an agent's name.

**The boundary (WORKING-NOTES IV.3).** The installer installs MCC and its own
prerequisites, nothing else; agents are installed only by the dashboard button
you press. Nothing in ``scripts/``, ``cli/``, first start, the updater or any
background task reads this module. A method runs only after the user pressed
Install on one card, read the plan the server wrote from these fields, and
confirmed it.

**What a row holds**, per the spec (``PR-CODING-AGENT-INSTALL-BUTTONS-SPEC``
N1): the operating system it applies to (WSL counts as linux), the kind, the
vendor's URL or package, whether it needs Node.js and the version bound that
package states, whether it elevates, what it changes on the machine, the
vendor's documentation link and the date the line was last checked. The
version is never pinned: vendor scripts install "latest", which is the vendor's
own channel, exactly as the ``install_hint`` lines have always repeated it.

**Order is the recommendation.** The first method of an OS that can run on this
machine is the recommended one: the vendor's own installer first, npm only
where it is the only way (user decision 1, 2026-10-18), the others in a small
picker.

**Never** (each pinned by ``tests/config/test_install_methods_registry.py``):
an ``http://`` URL, a host off the method's own allow-list, ``sudo``, ``runas``
or ``--unsafe-perm`` in anything the runner executes, or a method for
Antigravity (``agy`` is never run). A ``.deb``/``.rpm`` that needs ``sudo``
is a printed line the user copies, never a step.
"""

from dataclasses import dataclass
from enum import StrEnum

#: The day every line below was re-read against the vendor's documentation
#: (Context7) for this release. Each method also names where it was read.
CHECKED_ON = "2026-10-19"
#: The investigator's fetch, for the rows Context7 had nothing newer on.
SPEC_CHECKED_ON = "2026-10-16"


class InstallOs(StrEnum):
    """The operating system a method applies to. WSL counts as ``linux``."""

    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"


class InstallKind(StrEnum):
    """What the runner does with a method."""

    #: The vendor's script, downloaded to a file and run by path with the
    #: interpreter named -- never ``iex`` of a string, never a shell.
    SCRIPT = "script"
    NPM = "npm"
    WINGET = "winget"
    BREW = "brew"
    #: An installer or app archive downloaded to a fresh temp folder and
    #: opened by path (see :class:`DownloadAction`).
    DOWNLOAD = "download"
    #: ``code --install-extension <id>`` for an editor extension.
    VSCODE_EXTENSION = "vscode_extension"
    #: No button: the vendor publishes no direct download MCC can open.
    LINK = "link"


class Elevation(StrEnum):
    """What a method asks of the operating system. MCC never elevates."""

    NONE = "none"
    #: The OS itself shows a prompt the user sees (Windows' administrator
    #: prompt, macOS Installer's password dialog).
    OS_PROMPT = "os_prompt"
    #: The line needs ``sudo``: shown for the user to copy, never run.
    SUDO_PRINTED_ONLY = "sudo_printed_only"


class Interpreter(StrEnum):
    """The interpreter a downloaded script is run with, named on argv."""

    POWERSHELL = "powershell"
    BASH = "bash"
    SH = "sh"


class DownloadAction(StrEnum):
    """What happens to a downloaded file."""

    #: A Windows ``.exe`` installer, opened by path (its own window).
    RUN = "run"
    #: macOS ``.dmg``: mounted read-only, the ``.app`` copied into
    #: ``~/Applications``, detached.
    DMG_COPY = "dmg_copy"
    #: Windows ``.zip``: extracted per user into
    #: ``%LOCALAPPDATA%\\Programs\\<app_name>`` (user decision 5).
    ZIP_TO_PROGRAMS = "zip_to_programs"
    #: macOS ``.zip`` holding an ``.app``: extracted into ``~/Applications``.
    ZIP_TO_APPLICATIONS = "zip_to_applications"
    #: A ``.deb``/``.rpm``: ``printed_command`` is shown, nothing runs.
    PRINT_ONLY = "print_only"


@dataclass(frozen=True, slots=True)
class NodeRequirement:
    """The Node.js an npm method needs, and where the bound was read.

    ``minimum`` is ``"0"`` where the package states no ``engines.node`` at
    all: the bound is still named (the static guard insists), it just never
    reads "too old".
    """

    minimum: str
    source: str

    @property
    def minimum_parts(self) -> tuple[int, ...]:
        """Return the bound as integers, e.g. ``(22, 19)``."""

        return tuple(int(part) for part in self.minimum.split("."))

    @property
    def label(self) -> str:
        """Return the bound as a reader writes it, e.g. ``Node.js 22.19 or newer``."""

        if self.minimum == "0":
            return "any Node.js (the package states no minimum)"
        return f"Node.js {self.minimum} or newer"


@dataclass(frozen=True, slots=True)
class InstallMethod:
    """One way to install one agent on one operating system."""

    #: Unique within one agent, one OS and one architecture.
    id: str
    label: str
    os: InstallOs
    kind: InstallKind
    #: The vendor's page this line came from.
    doc_url: str
    checked_on: str
    #: Where the line was re-read (a Context7 library id and its source page).
    checked_by: str
    #: The vendor's own one-liner, verbatim, for the copy button. It is for
    #: the user's own terminal; the runner never executes this text.
    copy_command: str
    #: The script or installer URL (https only), or the vendor page of a LINK.
    url: str = ""
    #: Every host the URL and each redirect it answers with may be on.
    hosts: tuple[str, ...] = ()
    interpreter: Interpreter | None = None
    #: Arguments after the script path, from the vendor's documented line.
    script_args: tuple[str, ...] = ()
    #: Variables the vendor's own line sets for its script (Goose's
    #: ``CONFIGURE=false``), added for that one step and shown in the plan.
    env: tuple[tuple[str, str], ...] = ()
    #: npm package, winget id, brew formula or cask, editor extension id.
    package: str = ""
    brew_cask: bool = False
    #: Extra npm flags the vendor documents (Pi's ``--ignore-scripts``).
    npm_args: tuple[str, ...] = ()
    #: Every command the method puts on PATH, so the plan can list them.
    bins: tuple[str, ...] = ()
    needs_node: NodeRequirement | None = None
    elevates: Elevation = Elevation.NONE
    #: What it changes on the machine, in words, for the plan.
    changes: tuple[str, ...] = ()
    #: Where the vendor puts the program, checked after the install when the
    #: server's own PATH (inherited at start) does not show it yet. ``~`` is
    #: the home folder, ``%NAME%`` an environment variable.
    verify_dirs: tuple[str, ...] = ()
    action: DownloadAction | None = None
    #: The folder (``ZIP_TO_PROGRAMS``) or bundle (``DMG_COPY``) name.
    app_name: str = ""
    #: ``x64`` or ``arm64``; empty for any machine.
    arch: str = ""
    #: The bound on the method's main step, in seconds.
    timeout_seconds: int = 900
    #: Opens a window the user must see; hidden on a headless server.
    gui: bool = False
    #: For ``PRINT_ONLY``: the line the user copies (may name ``sudo``).
    printed_command: str = ""

    @property
    def runnable(self) -> bool:
        """Return whether the runner may execute this method at all."""

        return self.kind is not InstallKind.LINK and (
            self.elevates is not Elevation.SUDO_PRINTED_ONLY
        )


@dataclass(frozen=True, slots=True)
class NodeInstaller:
    """How Node.js is installed when an npm method needs it and it is missing.

    User decision 2 (2026-10-18): the official installer on Windows and macOS
    (the OS asks for permission itself), fnm per user on Linux/WSL, never
    sudo. Decision 3: an older Node.js is never upgraded -- this runs only
    when there is no Node.js at all.
    """

    os: InstallOs
    #: ``msi`` (Windows), ``pkg`` (macOS) or ``fnm`` (Linux/WSL).
    kind: str
    #: For msi/pkg: the release list the newest LTS is read from when the
    #: user presses Run. For fnm: the install script.
    url: str
    hosts: tuple[str, ...]
    elevates: Elevation
    changes: tuple[str, ...]
    doc_url: str
    checked_by: str
    checked_on: str
    #: Where the new ``node`` lands, checked before the npm step.
    verify_dirs: tuple[str, ...]
    timeout_seconds: int = 1200


#: GitHub release downloads answer from a redirect to one of these.
GITHUB_HOSTS = (
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "raw.githubusercontent.com",
)

NODE_DIST_INDEX_URL = "https://nodejs.org/dist/index.json"

NODE_INSTALLERS: dict[InstallOs, NodeInstaller] = {
    InstallOs.WINDOWS: NodeInstaller(
        os=InstallOs.WINDOWS,
        kind="msi",
        url=NODE_DIST_INDEX_URL,
        hosts=("nodejs.org",),
        elevates=Elevation.OS_PROMPT,
        changes=(
            "installs Node.js LTS and npm into C:\\Program Files\\nodejs",
            "adds that folder and your npm folder (%APPDATA%\\npm) to PATH",
        ),
        doc_url="https://nodejs.org/en/download",
        checked_by=(
            "Context7 /nodejs/nodejs.org (apps/site/util/url.ts: "
            "node-<v>-x64.msi, SHASUMS256.txt)"
        ),
        checked_on=CHECKED_ON,
        # Forward slashes: expanded with ``os.path.normpath`` on the server.
        verify_dirs=("%ProgramFiles%/nodejs",),
    ),
    InstallOs.MACOS: NodeInstaller(
        os=InstallOs.MACOS,
        kind="pkg",
        url=NODE_DIST_INDEX_URL,
        hosts=("nodejs.org",),
        elevates=Elevation.OS_PROMPT,
        changes=(
            "installs Node.js LTS and npm into /usr/local (macOS Installer "
            "asks for your password)",
        ),
        doc_url="https://nodejs.org/en/download",
        checked_by="Context7 /nodejs/nodejs.org (apps/site/util/url.ts: node-<v>.pkg)",
        checked_on=CHECKED_ON,
        verify_dirs=("/usr/local/bin",),
    ),
    InstallOs.LINUX: NodeInstaller(
        os=InstallOs.LINUX,
        kind="fnm",
        url="https://fnm.vercel.app/install",
        hosts=("fnm.vercel.app", *GITHUB_HOSTS),
        elevates=Elevation.NONE,
        changes=(
            "installs fnm per user into ~/.local/share/fnm (no sudo)",
            "adds one fnm loader line to your shell's file (~/.bashrc, "
            "~/.zshrc or fish's conf.d), chosen by $SHELL",
            "installs Node.js LTS with fnm and makes it fnm's default",
        ),
        doc_url="https://github.com/Schniz/fnm#installation",
        checked_by="Context7 /schniz/fnm (README: install script, --install-dir)",
        checked_on=CHECKED_ON,
        verify_dirs=("~/.local/share/fnm/aliases/default/bin",),
    ),
}

#: The ``changes`` line every npm method carries.
_NPM_CHANGES = ("adds the package to npm's global folder, which npm keeps on PATH",)


def _npm(
    os_: InstallOs,
    package: str,
    *,
    bound: NodeRequirement,
    doc_url: str,
    checked_by: str,
    bins: tuple[str, ...],
    label: str = "npm",
    npm_args: tuple[str, ...] = (),
    checked_on: str = CHECKED_ON,
) -> InstallMethod:
    flags = " ".join(npm_args)
    copy = f"npm install -g {flags + ' ' if flags else ''}{package}"
    return InstallMethod(
        id="npm",
        label=label,
        os=os_,
        kind=InstallKind.NPM,
        doc_url=doc_url,
        checked_on=checked_on,
        checked_by=checked_by,
        copy_command=copy,
        package=package,
        npm_args=npm_args,
        bins=bins,
        needs_node=bound,
        changes=_NPM_CHANGES,
    )


def _npm_everywhere(package: str, **kwargs) -> tuple[InstallMethod, ...]:
    return tuple(_npm(os_, package, **kwargs) for os_ in InstallOs)


def _ps1(
    url: str,
    hosts: tuple[str, ...],
    *,
    doc_url: str,
    checked_by: str,
    copy_command: str,
    changes: tuple[str, ...],
    verify_dirs: tuple[str, ...],
    bins: tuple[str, ...],
    label: str = "vendor installer",
    env: tuple[tuple[str, str], ...] = (),
    checked_on: str = CHECKED_ON,
) -> InstallMethod:
    return InstallMethod(
        id="native",
        label=label,
        os=InstallOs.WINDOWS,
        kind=InstallKind.SCRIPT,
        doc_url=doc_url,
        checked_on=checked_on,
        checked_by=checked_by,
        copy_command=copy_command,
        url=url,
        hosts=hosts,
        interpreter=Interpreter.POWERSHELL,
        env=env,
        bins=bins,
        changes=changes,
        verify_dirs=verify_dirs,
    )


def _posix_script(
    os_: InstallOs,
    url: str,
    hosts: tuple[str, ...],
    *,
    interpreter: Interpreter,
    doc_url: str,
    checked_by: str,
    copy_command: str,
    changes: tuple[str, ...],
    verify_dirs: tuple[str, ...],
    bins: tuple[str, ...],
    label: str = "vendor installer",
    env: tuple[tuple[str, str], ...] = (),
    script_args: tuple[str, ...] = (),
    checked_on: str = CHECKED_ON,
) -> InstallMethod:
    return InstallMethod(
        id="native",
        label=label,
        os=os_,
        kind=InstallKind.SCRIPT,
        doc_url=doc_url,
        checked_on=checked_on,
        checked_by=checked_by,
        copy_command=copy_command,
        url=url,
        hosts=hosts,
        interpreter=interpreter,
        env=env,
        script_args=script_args,
        bins=bins,
        changes=changes,
        verify_dirs=verify_dirs,
    )


def _posix_scripts(
    url: str, hosts: tuple[str, ...], **kwargs
) -> tuple[InstallMethod, ...]:
    return tuple(
        _posix_script(os_, url, hosts, **kwargs)
        for os_ in (InstallOs.MACOS, InstallOs.LINUX)
    )


def _brew(
    package: str,
    *,
    cask: bool,
    doc_url: str,
    checked_by: str,
    bins: tuple[str, ...],
    checked_on: str = CHECKED_ON,
) -> InstallMethod:
    return InstallMethod(
        id="brew",
        label="Homebrew",
        os=InstallOs.MACOS,
        kind=InstallKind.BREW,
        doc_url=doc_url,
        checked_on=checked_on,
        checked_by=checked_by,
        copy_command=f"brew install {'--cask ' if cask else ''}{package}",
        package=package,
        brew_cask=cask,
        bins=bins,
        changes=("installs through Homebrew, which keeps its own folder on PATH",),
    )


def _winget(
    package: str, *, doc_url: str, checked_by: str, bins: tuple[str, ...]
) -> InstallMethod:
    return InstallMethod(
        id="winget",
        label="winget",
        os=InstallOs.WINDOWS,
        kind=InstallKind.WINGET,
        doc_url=doc_url,
        checked_on=CHECKED_ON,
        checked_by=checked_by,
        copy_command=f"winget install {package}",
        package=package,
        bins=bins,
        changes=(
            "installs through winget, accepting winget's source and package "
            "agreements on your behalf (you confirm this plan first)",
            "winget adds the program to PATH; a package that installs for "
            "every user makes Windows ask for administrator permission",
        ),
        verify_dirs=("%LOCALAPPDATA%/Microsoft/WinGet/Links",),
        elevates=Elevation.OS_PROMPT,
    )


_LOCAL_BIN = ("~/.local/bin",)
_PATH_AND_PROFILE = (
    "adds its folder to your PATH (on macOS/Linux by a line in your shell's "
    "profile file)",
)

# ---------------------------------------------------------------- Node bounds

_NODE_22 = NodeRequirement("22", "engines.node >=22.0.0 (npm registry, 2026-10-16)")
_NODE_22_19 = NodeRequirement(
    "22.19", "engines.node >=22.19.0 (npm registry, 2026-10-16)"
)
_NODE_20 = NodeRequirement("20", "engines.node >=20 (npm registry, 2026-10-16)")
_NODE_16 = NodeRequirement("16", "engines.node >=16 (npm registry, 2026-10-16)")
_NODE_NONE = NodeRequirement(
    "0", "the package declares no engines.node (npm registry, 2026-10-16)"
)
_NODE_CLINE = NodeRequirement(
    "20",
    "docs.cline.bot: 'Install Node.js 20+ (22 recommended)' (the package declares none)",
)
#: OpenClaw's own ``engines.node`` is a range -- ``>=24.22.1 <25 || >=26.12.1``
#: -- and ``NodeRequirement`` holds one floor, so the floor is what it holds.
#: The range is stated here and in the card's copy line so a reader on Node 25
#: (which the package excludes) can see why the check refuses.
_NODE_OPENCLAW = NodeRequirement(
    "24.22.1",
    "engines.node '>=24.22.1 <25 || >=26.12.1' (openclaw package.json, 2026-10-29)",
)

# ---------------------------------------------------------------- per agent

_CLAUDE_DOC = "https://code.claude.com/docs/en/setup"
_CLAUDE_BY = "Context7 /websites/code_claude (code.claude.com/docs/en/setup)"
_CLAUDE_HOSTS = ("claude.ai", "downloads.claude.ai", "storage.googleapis.com")
_CLAUDE_CHANGES = (
    "puts claude in ~/.local/bin and adds that folder to your PATH",
    "Claude Code then updates itself in the background",
)

CLAUDE_METHODS: tuple[InstallMethod, ...] = (
    _ps1(
        "https://claude.ai/install.ps1",
        _CLAUDE_HOSTS,
        label="Anthropic's installer (recommended)",
        doc_url=_CLAUDE_DOC,
        checked_by=_CLAUDE_BY,
        copy_command="irm https://claude.ai/install.ps1 | iex",
        changes=_CLAUDE_CHANGES,
        verify_dirs=_LOCAL_BIN,
        bins=("claude",),
    ),
    _winget(
        "Anthropic.ClaudeCode",
        doc_url=_CLAUDE_DOC,
        checked_by=_CLAUDE_BY,
        bins=("claude",),
    ),
    *_posix_scripts(
        "https://claude.ai/install.sh",
        _CLAUDE_HOSTS,
        interpreter=Interpreter.BASH,
        label="Anthropic's installer (recommended)",
        doc_url=_CLAUDE_DOC,
        checked_by=_CLAUDE_BY,
        copy_command="curl -fsSL https://claude.ai/install.sh | bash",
        changes=_CLAUDE_CHANGES,
        verify_dirs=_LOCAL_BIN,
        bins=("claude",),
    ),
    _brew(
        "claude-code",
        cask=True,
        doc_url=_CLAUDE_DOC,
        checked_by=_CLAUDE_BY,
        bins=("claude",),
    ),
    *_npm_everywhere(
        "@anthropic-ai/claude-code",
        bound=_NODE_22,
        doc_url=_CLAUDE_DOC,
        checked_by=_CLAUDE_BY,
        bins=("claude",),
    ),
)

_CODEX_DOC = "https://github.com/openai/codex#installing-and-running-codex-cli"
_CODEX_BY = "Context7 /openai/codex (README.md)"
_CODEX_HOSTS = ("chatgpt.com", "releases.openai.com", *GITHUB_HOSTS)
_CODEX_DIRS = ("~/.local/bin", "~/.codex/packages/standalone/current/bin")

CODEX_METHODS: tuple[InstallMethod, ...] = (
    _ps1(
        "https://chatgpt.com/codex/install.ps1",
        _CODEX_HOSTS,
        label="OpenAI's installer (recommended)",
        doc_url=_CODEX_DOC,
        checked_by=_CODEX_BY,
        copy_command=(
            'powershell -ExecutionPolicy ByPass -c "irm '
            'https://chatgpt.com/codex/install.ps1 | iex"'
        ),
        changes=("installs Codex under ~/.codex/packages and adds it to your PATH",),
        verify_dirs=_CODEX_DIRS,
        bins=("codex",),
    ),
    *_posix_scripts(
        "https://chatgpt.com/codex/install.sh",
        _CODEX_HOSTS,
        interpreter=Interpreter.SH,
        label="OpenAI's installer (recommended)",
        doc_url=_CODEX_DOC,
        checked_by=_CODEX_BY,
        copy_command="curl -fsSL https://chatgpt.com/codex/install.sh | sh",
        changes=(
            "installs Codex under ~/.codex/packages and links it into a PATH folder",
        ),
        verify_dirs=_CODEX_DIRS,
        bins=("codex",),
    ),
    _brew(
        "codex",
        cask=True,
        doc_url=_CODEX_DOC,
        checked_by=_CODEX_BY,
        bins=("codex",),
    ),
    *_npm_everywhere(
        "@openai/codex",
        bound=_NODE_16,
        doc_url=_CODEX_DOC,
        checked_by=_CODEX_BY,
        bins=("codex",),
    ),
)

_PI_DOC = "https://pi.dev/docs/latest/quickstart"
_PI_BY = "Context7 /websites/pi_dev (pi.dev, quickstart)"
_PI_HOSTS = ("pi.dev", *GITHUB_HOSTS)

PI_METHODS: tuple[InstallMethod, ...] = (
    _ps1(
        "https://pi.dev/install.ps1",
        _PI_HOSTS,
        label="Pi's installer (recommended)",
        doc_url=_PI_DOC,
        checked_by=_PI_BY,
        copy_command='powershell -c "irm https://pi.dev/install.ps1 | iex"',
        changes=_PATH_AND_PROFILE,
        verify_dirs=("~/.pi/bin", *_LOCAL_BIN),
        bins=("pi",),
    ),
    *_posix_scripts(
        "https://pi.dev/install.sh",
        _PI_HOSTS,
        interpreter=Interpreter.SH,
        label="Pi's installer (recommended)",
        doc_url=_PI_DOC,
        checked_by=_PI_BY,
        copy_command="curl -fsSL https://pi.dev/install.sh | sh",
        changes=_PATH_AND_PROFILE,
        verify_dirs=("~/.pi/bin", *_LOCAL_BIN),
        bins=("pi",),
    ),
    *_npm_everywhere(
        "@earendil-works/pi-coding-agent",
        bound=_NODE_22_19,
        doc_url=_PI_DOC,
        checked_by=_PI_BY,
        bins=("pi",),
        npm_args=("--ignore-scripts",),
    ),
)

_OPENCODE_DOC = "https://opencode.ai/docs"
_OPENCODE_BY = "Context7 /websites/opencode_ai (opencode.ai/docs)"

OPENCODE_METHODS: tuple[InstallMethod, ...] = (
    # Windows: the vendor recommends WSL, and an MCC on Windows installs for
    # Windows (user decision 7), so npm is the one Windows line MCC can run.
    *_posix_scripts(
        "https://opencode.ai/install",
        ("opencode.ai", *GITHUB_HOSTS),
        interpreter=Interpreter.BASH,
        label="OpenCode's installer (recommended)",
        doc_url=_OPENCODE_DOC,
        checked_by=_OPENCODE_BY,
        copy_command="curl -fsSL https://opencode.ai/install | bash",
        changes=(
            "installs into ~/.opencode/bin and adds a PATH line to your shell's "
            "profile file",
        ),
        verify_dirs=("~/.opencode/bin",),
        bins=("opencode",),
    ),
    _brew(
        "anomalyco/tap/opencode",
        cask=False,
        doc_url=_OPENCODE_DOC,
        checked_by=_OPENCODE_BY,
        bins=("opencode",),
    ),
    *_npm_everywhere(
        "opencode-ai",
        bound=_NODE_NONE,
        doc_url=_OPENCODE_DOC,
        checked_by=_OPENCODE_BY,
        bins=("opencode",),
    ),
)

OPENCODE2_METHODS: tuple[InstallMethod, ...] = _npm_everywhere(
    "@opencode-ai/cli@beta",
    bound=_NODE_NONE,
    doc_url="https://www.npmjs.com/package/@opencode-ai/cli",
    checked_by="npm registry @opencode-ai/cli (2026-10-16); MCC's own install_hint",
    bins=("opencode2",),
    label="npm (beta channel)",
    checked_on=SPEC_CHECKED_ON,
)

_KILO_DOC = "https://kilo.ai/docs/cli"
_KILO_BY = "Context7 /kilo-org/kilo (install script; docs install-cli.md)"

KILO_METHODS: tuple[InstallMethod, ...] = (
    *_posix_scripts(
        "https://kilo.ai/cli/install",
        ("kilo.ai", *GITHUB_HOSTS),
        interpreter=Interpreter.BASH,
        label="Kilo's installer (recommended)",
        doc_url=_KILO_DOC,
        checked_by=_KILO_BY,
        copy_command="curl -fsSL https://kilo.ai/cli/install | bash",
        changes=_PATH_AND_PROFILE,
        verify_dirs=("~/.kilo/bin", *_LOCAL_BIN),
        bins=("kilo",),
    ),
    *_npm_everywhere(
        "@kilocode/cli",
        bound=_NODE_NONE,
        doc_url=_KILO_DOC,
        checked_by=_KILO_BY,
        bins=("kilo", "kilocode"),
    ),
)

COMMANDCODE_METHODS: tuple[InstallMethod, ...] = _npm_everywhere(
    "command-code",
    bound=_NODE_22,
    doc_url="https://commandcode.ai/docs/quickstart",
    checked_by="Context7 /websites/commandcode_ai (docs/quickstart)",
    # Every bin the package adds, so the plan can say so: ``cmd`` is one of
    # them, and on Windows System32's cmd.exe normally precedes npm's folder.
    bins=("command-code", "cmd", "cmdc", "commandcode"),
)

#: OpenClaw ships only as an npm package, and its name is the one thing the
#: method must get exactly right: a package called ``@openclaw-ai/openclawai``
#: exists and is not the project. The version is npm's ``latest``, as every
#: other row here installs the vendor's own channel, and the Node floor is the
#: package's own ``engines.node``.
OPENCLAW_METHODS: tuple[InstallMethod, ...] = _npm_everywhere(
    "openclaw",
    bound=_NODE_OPENCLAW,
    doc_url="https://www.npmjs.com/package/openclaw",
    checked_by=(
        "npm registry: openclaw 2026.10.8 package.json -- bin openclaw, "
        "engines.node '>=24.22.1 <25 || >=26.12.1' (2026-10-29)"
    ),
    bins=("openclaw",),
    checked_on="2026-10-29",
)

_QWEN_DOC = "https://github.com/QwenLM/qwen-code#installation"
_QWEN_BY = "Context7 /qwenlm/qwen-code (README.md, docs/users/quickstart.md)"
_QWEN_HOST = ("qwen-code-assets.oss-cn-hangzhou.aliyuncs.com",)
_QWEN_BASE = "https://qwen-code-assets.oss-cn-hangzhou.aliyuncs.com/installation/"

QWEN_METHODS: tuple[InstallMethod, ...] = (
    _ps1(
        f"{_QWEN_BASE}install-qwen-standalone.ps1",
        _QWEN_HOST,
        label="Qwen's installer (recommended)",
        doc_url=_QWEN_DOC,
        checked_by=_QWEN_BY,
        copy_command=f"irm {_QWEN_BASE}install-qwen-standalone.ps1 | iex",
        changes=_PATH_AND_PROFILE,
        verify_dirs=("~/.qwen/bin", *_LOCAL_BIN),
        bins=("qwen",),
    ),
    *_posix_scripts(
        f"{_QWEN_BASE}install-qwen-standalone.sh",
        _QWEN_HOST,
        interpreter=Interpreter.BASH,
        label="Qwen's installer (recommended)",
        doc_url=_QWEN_DOC,
        checked_by=_QWEN_BY,
        copy_command=f"curl -fsSL {_QWEN_BASE}install-qwen-standalone.sh | bash",
        changes=_PATH_AND_PROFILE,
        verify_dirs=("~/.qwen/bin", *_LOCAL_BIN),
        bins=("qwen",),
    ),
    _brew(
        "qwen-code",
        cask=False,
        doc_url=_QWEN_DOC,
        checked_by=_QWEN_BY,
        bins=("qwen",),
    ),
    *_npm_everywhere(
        "@qwen-code/qwen-code",
        bound=_NODE_22,
        doc_url=_QWEN_DOC,
        checked_by=_QWEN_BY,
        bins=("qwen",),
    ),
)

_CRUSH_DOC = "https://github.com/charmbracelet/crush#installation"
_CRUSH_BY = "Context7 /charmbracelet/crush (README.md)"

CRUSH_METHODS: tuple[InstallMethod, ...] = (
    _winget(
        "charmbracelet.crush",
        doc_url=_CRUSH_DOC,
        checked_by=_CRUSH_BY,
        bins=("crush",),
    ),
    _brew(
        "charmbracelet/tap/crush",
        cask=False,
        doc_url=_CRUSH_DOC,
        checked_by=_CRUSH_BY,
        bins=("crush",),
    ),
    # Linux's apt/yum repository lines need sudo; npm is the per-user line.
    *_npm_everywhere(
        "@charmland/crush",
        bound=_NODE_NONE,
        doc_url=_CRUSH_DOC,
        checked_by=_CRUSH_BY,
        bins=("crush",),
    ),
)

CLINE_METHODS: tuple[InstallMethod, ...] = _npm_everywhere(
    "cline",
    bound=_NODE_CLINE,
    doc_url="https://docs.cline.bot/getting-started/installing-cline",
    checked_by="Context7 /websites/cline_bot (getting-started/installing-cline)",
    bins=("cline",),
)

_GOOSE_DOC = "https://goose-docs.ai/docs/getting-started/installation"
_GOOSE_BY = "Context7 /websites/goose-docs_ai (getting-started/installation)"
_GOOSE_ENV = (("CONFIGURE", "false"),)

GOOSE_METHODS: tuple[InstallMethod, ...] = (
    _ps1(
        "https://raw.githubusercontent.com/aaif-goose/goose/main/download_cli.ps1",
        GITHUB_HOSTS,
        label="Goose's installer (recommended)",
        doc_url=_GOOSE_DOC,
        checked_by=_GOOSE_BY,
        copy_command=(
            'Invoke-WebRequest -Uri "https://raw.githubusercontent.com/aaif-goose/'
            'goose/main/download_cli.ps1" -OutFile "download_cli.ps1"; '
            ".\\download_cli.ps1"
        ),
        changes=_PATH_AND_PROFILE,
        verify_dirs=_LOCAL_BIN,
        bins=("goose",),
        env=_GOOSE_ENV,
    ),
    *_posix_scripts(
        "https://github.com/aaif-goose/goose/releases/download/stable/download_cli.sh",
        GITHUB_HOSTS,
        interpreter=Interpreter.BASH,
        label="Goose's installer (recommended)",
        doc_url=_GOOSE_DOC,
        checked_by=_GOOSE_BY,
        copy_command=(
            "curl -fsSL https://github.com/aaif-goose/goose/releases/download/"
            "stable/download_cli.sh | CONFIGURE=false bash"
        ),
        changes=("installs goose into ~/.local/bin",),
        verify_dirs=_LOCAL_BIN,
        bins=("goose",),
        env=_GOOSE_ENV,
    ),
    _brew(
        "block-goose-cli",
        cask=False,
        doc_url=_GOOSE_DOC,
        checked_by=_GOOSE_BY,
        bins=("goose",),
        checked_on=SPEC_CHECKED_ON,
    ),
)

_AIDER_DOC = "https://aider.chat/docs/install.html"
_AIDER_BY = "Context7 /websites/aider_chat (docs/install.html)"
_AIDER_HOSTS = ("aider.chat", "astral.sh", *GITHUB_HOSTS)
_AIDER_CHANGES = (
    "installs uv (if missing) and Python 3.12 if needed, then aider as a uv "
    "tool in ~/.local/bin, adding that folder to your PATH",
)

AIDER_METHODS: tuple[InstallMethod, ...] = (
    _ps1(
        "https://aider.chat/install.ps1",
        _AIDER_HOSTS,
        label="Aider's installer",
        doc_url=_AIDER_DOC,
        checked_by=_AIDER_BY,
        copy_command=(
            'powershell -ExecutionPolicy ByPass -c "irm https://aider.chat/install.ps1 | iex"'
        ),
        changes=_AIDER_CHANGES,
        verify_dirs=_LOCAL_BIN,
        bins=("aider",),
    ),
    *_posix_scripts(
        "https://aider.chat/install.sh",
        _AIDER_HOSTS,
        interpreter=Interpreter.SH,
        label="Aider's installer",
        doc_url=_AIDER_DOC,
        checked_by=_AIDER_BY,
        copy_command="curl -LsSf https://aider.chat/install.sh | sh",
        changes=_AIDER_CHANGES,
        verify_dirs=_LOCAL_BIN,
        bins=("aider",),
    ),
)

_DROID_DOC = "https://docs.factory.ai/cli/getting-started/quickstart"
_DROID_BY = "Context7 /factory-ai/factory (docs/cli/getting-started/quickstart.mdx)"
_DROID_HOSTS = ("app.factory.ai", "downloads.factory.ai")

DROID_METHODS: tuple[InstallMethod, ...] = (
    _ps1(
        "https://app.factory.ai/cli/windows",
        _DROID_HOSTS,
        label="Factory's installer (recommended)",
        doc_url=_DROID_DOC,
        checked_by=_DROID_BY,
        copy_command="irm https://app.factory.ai/cli/windows | iex",
        changes=_PATH_AND_PROFILE,
        verify_dirs=("~/.factory/bin", *_LOCAL_BIN),
        bins=("droid",),
    ),
    *_posix_scripts(
        "https://app.factory.ai/cli",
        _DROID_HOSTS,
        interpreter=Interpreter.SH,
        label="Factory's installer (recommended)",
        doc_url=_DROID_DOC,
        checked_by=_DROID_BY,
        copy_command="curl -fsSL https://app.factory.ai/cli | sh",
        changes=_PATH_AND_PROFILE,
        verify_dirs=(*_LOCAL_BIN, "~/.factory/bin"),
        bins=("droid",),
    ),
    _brew(
        "droid",
        cask=True,
        doc_url=_DROID_DOC,
        checked_by=_DROID_BY,
        bins=("droid",),
    ),
    *_npm_everywhere(
        "droid",
        bound=_NODE_20,
        doc_url=_DROID_DOC,
        checked_by=_DROID_BY,
        bins=("droid",),
    ),
)

_GEMINI_DOC = "https://github.com/google-gemini/gemini-cli#-installation"
_GEMINI_BY = "Context7 /google-gemini/gemini-cli (README.md)"

GEMINI_METHODS: tuple[InstallMethod, ...] = (
    _brew(
        "gemini-cli",
        cask=False,
        doc_url=_GEMINI_DOC,
        checked_by=_GEMINI_BY,
        bins=("gemini",),
    ),
    *_npm_everywhere(
        "@google/gemini-cli",
        bound=_NODE_20,
        doc_url=_GEMINI_DOC,
        checked_by=_GEMINI_BY,
        bins=("gemini",),
    ),
)

#: Hermes Agent installs from Nous Research's own installer, and MCC's button
#: is a **LINK** to it rather than a method it runs: Hermes' own config code
#: lists ``pip`` and ``homebrew`` as unsupported install methods
#: (``hermes_cli/config.py:578`` of 0.19.0), so a package-manager button would
#: produce an install Hermes itself will not update. The vendor publishes
#: ``install.sh`` and ``install.ps1``; both are on the page below, so one row
#: per OS pointing at that page is the whole button (user decision 5,
#: 2026-10-29).
_HERMES_DOC = "https://hermes-agent.nousresearch.com/docs/getting-started/installation"

HERMES_METHODS: tuple[InstallMethod, ...] = tuple(
    InstallMethod(
        id="link",
        label="Nous Research's installer",
        os=os_,
        kind=InstallKind.LINK,
        doc_url=_HERMES_DOC,
        checked_on="2026-10-30",
        checked_by=(
            "Nous Research's install page "
            "(hermes-agent.nousresearch.com/docs/getting-started/installation, "
            "fetched 2026-10-30: install.sh for Linux/macOS/WSL2, install.ps1 "
            "for Windows); pip/homebrew unsupported per "
            "hermes_cli/config.py:578 of the published 0.19.0 wheel"
        ),
        copy_command=(
            'powershell -c "irm https://hermes-agent.nousresearch.com/install.ps1 | iex"'
            if os_ is InstallOs.WINDOWS
            else "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash"
        ),
        url=_HERMES_DOC,
        hosts=("hermes-agent.nousresearch.com",),
        changes=(
            "opens Nous Research's installation page in your browser -- MCC "
            "runs nothing: Hermes documents pip and Homebrew as unsupported "
            "install methods, so its own installer is the only path",
        ),
    )
    for os_ in InstallOs
)

#: Kimi Code (user decision 6): Moonshot's new ``@moonshot-ai/kimi-code``
#: replaces the legacy ``kimi-cli`` the launcher's hint names, and the button
#: waits for a check that ``mcc-kimi`` works with it. That check needs Kimi
#: Code installed somewhere to run against, which this release could not do
#: without downloading it -- so there is no button, and the card says why.
KIMI_NO_BUTTON_REASON = (
    "No Install button yet: Moonshot now ships Kimi Code (@moonshot-ai/kimi-code) "
    "in place of the kimi-cli this launcher was built for, and MCC has not yet "
    "checked that mcc-kimi works with it (7.102.0, 2026-10-19). Install it "
    "yourself from Moonshot's page if you want it now."
)
KIMI_DOC_URL = "https://github.com/MoonshotAI/kimi-cli"

ANTIGRAVITY_NO_BUTTON_REASON = (
    "No Install button: MCC cannot serve Antigravity through a launcher and "
    "never runs agy, so it does not install it either."
)

# ---------------------------------------------------------------- desktop apps

_CLAUDE_DESKTOP_DOC = "https://claude.com/download"
_CLAUDE_DESKTOP_BY = "spec §4 (claude.com/download, fetched 2026-10-16)"

CLAUDE_DESKTOP_METHODS: tuple[InstallMethod, ...] = (
    *(
        InstallMethod(
            id="download",
            label="Anthropic's installer",
            os=InstallOs.WINDOWS,
            kind=InstallKind.DOWNLOAD,
            doc_url=_CLAUDE_DESKTOP_DOC,
            checked_on=SPEC_CHECKED_ON,
            checked_by=_CLAUDE_DESKTOP_BY,
            copy_command=url,
            url=url,
            hosts=_CLAUDE_HOSTS,
            action=DownloadAction.RUN,
            arch=arch,
            gui=True,
            timeout_seconds=1800,
            changes=(
                "opens Anthropic's per-user setup.exe, which installs Claude "
                "under %LOCALAPPDATA% and adds a Start menu entry",
            ),
        )
        for arch, url in (
            ("x64", "https://claude.ai/api/desktop/win32/x64/setup/latest/redirect"),
            (
                "arm64",
                "https://claude.ai/api/desktop/win32/arm64/setup/latest/redirect",
            ),
        )
    ),
    InstallMethod(
        id="download",
        label="Anthropic's disk image",
        os=InstallOs.MACOS,
        kind=InstallKind.DOWNLOAD,
        doc_url=_CLAUDE_DESKTOP_DOC,
        checked_on=SPEC_CHECKED_ON,
        checked_by=_CLAUDE_DESKTOP_BY,
        copy_command="https://claude.ai/api/desktop/darwin/universal/dmg/latest/redirect",
        url="https://claude.ai/api/desktop/darwin/universal/dmg/latest/redirect",
        hosts=_CLAUDE_HOSTS,
        action=DownloadAction.DMG_COPY,
        app_name="Claude.app",
        gui=True,
        changes=("copies Claude.app into ~/Applications",),
    ),
    InstallMethod(
        id="link",
        label="Linux beta (Anthropic's page)",
        os=InstallOs.LINUX,
        kind=InstallKind.LINK,
        doc_url="https://code.claude.com/docs/en/desktop-linux",
        checked_on=SPEC_CHECKED_ON,
        checked_by=_CLAUDE_DESKTOP_BY,
        copy_command="https://code.claude.com/docs/en/desktop-linux",
        url="https://code.claude.com/docs/en/desktop-linux",
        hosts=("code.claude.com",),
    ),
)

_CODEX_APP_DOC = "https://developers.openai.com/codex/app"

#: A link everywhere. The spec's 2026-10-16 macOS ``Codex.dmg`` URL predates
#: what Context7 read on 2026-10-19 (/websites/developers_openai, changelog
#: 26.707): "Codex is now part of the ChatGPT desktop app on macOS and
#: Windows". A download MCC cannot re-confirm is not a button (decision 8).
CODEX_DESKTOP_METHODS: tuple[InstallMethod, ...] = tuple(
    InstallMethod(
        id="link",
        label="OpenAI's page (Codex is now part of the ChatGPT desktop app)",
        os=os_,
        kind=InstallKind.LINK,
        doc_url=_CODEX_APP_DOC,
        checked_on=CHECKED_ON,
        checked_by="Context7 /websites/developers_openai (codex/app, changelog 26.707)",
        copy_command=_CODEX_APP_DOC,
        url=_CODEX_APP_DOC,
        hosts=("developers.openai.com",),
    )
    for os_ in InstallOs
)

_OPENCODE_APP_DOC = "https://opencode.ai/download"
_OPENCODE_APP_BY = "spec §4 (opencode.ai/download, fetched 2026-10-16)"
_OPENCODE_APP_HOSTS = ("opencode.ai", *GITHUB_HOSTS)


def _opencode_app(
    os_: InstallOs, slug: str, arch: str, action: DownloadAction, changes: str
) -> InstallMethod:
    url = f"https://opencode.ai/download/stable/{slug}"
    return InstallMethod(
        id="download",
        label="OpenCode's installer",
        os=os_,
        kind=InstallKind.DOWNLOAD,
        doc_url=_OPENCODE_APP_DOC,
        checked_on=SPEC_CHECKED_ON,
        checked_by=_OPENCODE_APP_BY,
        copy_command=url,
        url=url,
        hosts=_OPENCODE_APP_HOSTS,
        action=action,
        arch=arch,
        app_name="OpenCode.app" if action is DownloadAction.DMG_COPY else "",
        gui=True,
        timeout_seconds=1800,
        elevates=Elevation.OS_PROMPT if os_ is InstallOs.WINDOWS else Elevation.NONE,
        changes=(changes,),
    )


OPENCODE_DESKTOP_METHODS: tuple[InstallMethod, ...] = (
    _opencode_app(
        InstallOs.WINDOWS,
        "windows-x64-nsis",
        "x64",
        DownloadAction.RUN,
        "opens OpenCode's setup .exe (its own window; if it installs for every "
        "user, Windows asks for administrator permission itself)",
    ),
    _opencode_app(
        InstallOs.WINDOWS,
        "windows-aarch64-nsis",
        "arm64",
        DownloadAction.RUN,
        "opens OpenCode's setup .exe (its own window; if it installs for every "
        "user, Windows asks for administrator permission itself)",
    ),
    _opencode_app(
        InstallOs.MACOS,
        "darwin-aarch64-dmg",
        "arm64",
        DownloadAction.DMG_COPY,
        "copies OpenCode.app into ~/Applications",
    ),
    _opencode_app(
        InstallOs.MACOS,
        "darwin-x64-dmg",
        "x64",
        DownloadAction.DMG_COPY,
        "copies OpenCode.app into ~/Applications",
    ),
    InstallMethod(
        id="deb",
        label="OpenCode's .deb (needs sudo: shown, not run)",
        os=InstallOs.LINUX,
        kind=InstallKind.DOWNLOAD,
        doc_url=_OPENCODE_APP_DOC,
        checked_on=SPEC_CHECKED_ON,
        checked_by=_OPENCODE_APP_BY,
        copy_command=(
            "curl -fLo opencode-desktop.deb https://opencode.ai/download/stable/"
            "linux-x64-deb && sudo apt install ./opencode-desktop.deb"
        ),
        url="https://opencode.ai/download/stable/linux-x64-deb",
        hosts=_OPENCODE_APP_HOSTS,
        action=DownloadAction.PRINT_ONLY,
        elevates=Elevation.SUDO_PRINTED_ONLY,
        printed_command=(
            "curl -fLo opencode-desktop.deb https://opencode.ai/download/stable/"
            "linux-x64-deb && sudo apt install ./opencode-desktop.deb"
        ),
    ),
)

_GOOSE_RELEASES = "https://github.com/aaif-goose/goose/releases/download/stable/"
_GOOSE_APP_BY = "spec §4 (goose-docs.ai, fetched 2026-10-16)"

GOOSE_DESKTOP_METHODS: tuple[InstallMethod, ...] = (
    InstallMethod(
        id="download",
        label="Goose's .zip, unpacked per user",
        os=InstallOs.WINDOWS,
        kind=InstallKind.DOWNLOAD,
        doc_url=_GOOSE_DOC,
        checked_on=SPEC_CHECKED_ON,
        checked_by=_GOOSE_APP_BY,
        copy_command=f"{_GOOSE_RELEASES}Goose-win32-x64.zip",
        url=f"{_GOOSE_RELEASES}Goose-win32-x64.zip",
        hosts=GITHUB_HOSTS,
        action=DownloadAction.ZIP_TO_PROGRAMS,
        app_name="Goose",
        arch="x64",
        changes=(
            "unpacks Goose into %LOCALAPPDATA%\\Programs\\Goose (Goose ships "
            "only a .zip for Windows, so MCC picks this per-user folder; no "
            "Start menu entry is made -- open Goose.exe from there)",
        ),
    ),
    *(
        InstallMethod(
            id="download",
            label="Goose's .zip",
            os=InstallOs.MACOS,
            kind=InstallKind.DOWNLOAD,
            doc_url=_GOOSE_DOC,
            checked_on=SPEC_CHECKED_ON,
            checked_by=_GOOSE_APP_BY,
            copy_command=f"{_GOOSE_RELEASES}{name}",
            url=f"{_GOOSE_RELEASES}{name}",
            hosts=GITHUB_HOSTS,
            action=DownloadAction.ZIP_TO_APPLICATIONS,
            app_name="Goose.app",
            arch=arch,
            changes=("unpacks Goose.app into ~/Applications",),
        )
        for arch, name in (("arm64", "Goose.zip"), ("x64", "Goose_intel_mac.zip"))
    ),
    InstallMethod(
        id="link",
        label="Goose's .deb / .rpm / Flatpak (needs sudo: Goose's page)",
        os=InstallOs.LINUX,
        kind=InstallKind.LINK,
        doc_url=_GOOSE_DOC,
        checked_on=SPEC_CHECKED_ON,
        checked_by=_GOOSE_APP_BY,
        copy_command=_GOOSE_DOC,
        url=_GOOSE_DOC,
        hosts=("goose-docs.ai",),
    ),
)

LM_STUDIO_METHODS: tuple[InstallMethod, ...] = (
    # User decision 8: an app MCC cannot route through gets a button only
    # where the vendor publishes a direct download -- LM Studio on Windows.
    InstallMethod(
        id="download",
        label="LM Studio's installer",
        os=InstallOs.WINDOWS,
        kind=InstallKind.DOWNLOAD,
        doc_url="https://lmstudio.ai/download",
        checked_on=SPEC_CHECKED_ON,
        checked_by="spec §4 (lmstudio.ai/download, fetched 2026-10-16)",
        copy_command="https://lmstudio.ai/download/latest/win32/x64",
        url="https://lmstudio.ai/download/latest/win32/x64",
        hosts=("lmstudio.ai", "installers.lmstudio.ai"),
        action=DownloadAction.RUN,
        arch="x64",
        gui=True,
        timeout_seconds=1800,
        elevates=Elevation.OS_PROMPT,
        changes=(
            "opens LM Studio's setup .exe (its own window; if it installs for "
            "every user, Windows asks for administrator permission itself)",
        ),
    ),
    *(
        InstallMethod(
            id="link",
            label="LM Studio's download page",
            os=os_,
            kind=InstallKind.LINK,
            doc_url="https://lmstudio.ai/download",
            checked_on=SPEC_CHECKED_ON,
            checked_by="spec §4 (lmstudio.ai/download)",
            copy_command="https://lmstudio.ai/download",
            url="https://lmstudio.ai/download",
            hosts=("lmstudio.ai",),
        )
        for os_ in (InstallOs.MACOS, InstallOs.LINUX)
    ),
)


def link_everywhere(url: str, host: str, label: str) -> tuple[InstallMethod, ...]:
    """Return one LINK row per OS: no direct download, the vendor's page."""

    return tuple(
        InstallMethod(
            id="link",
            label=label,
            os=os_,
            kind=InstallKind.LINK,
            doc_url=url,
            checked_on=SPEC_CHECKED_ON,
            checked_by="spec §4 (no direct download verified)",
            copy_command=url,
            url=url,
            hosts=(host,),
        )
        for os_ in InstallOs
    )


def _extension(
    extension_id: str, doc_url: str, checked_by: str
) -> tuple[InstallMethod, ...]:
    return tuple(
        InstallMethod(
            id="extension",
            label="VS Code extension (code --install-extension)",
            os=os_,
            kind=InstallKind.VSCODE_EXTENSION,
            doc_url=doc_url,
            checked_on=CHECKED_ON,
            checked_by=checked_by,
            copy_command=f"code --install-extension {extension_id}",
            package=extension_id,
            changes=("adds the extension to VS Code's ~/.vscode/extensions",),
        )
        for os_ in InstallOs
    )


VSCODE_COPILOT_METHODS = _extension(
    "github.copilot-chat",
    "https://code.visualstudio.com/docs/copilot/setup",
    "the id from MCC's own detection marker (~/.vscode/extensions/github.copilot-chat-*)",
)
ROO_CODE_METHODS = _extension(
    "rooveterinaryinc.roo-cline",
    "https://docs.roocode.com/getting-started/installing",
    "Context7 /roocodeinc/roo-code-docs (getting-started/installing: publisher "
    "RooVeterinaryInc) and MCC's detection marker rooveterinaryinc.roo-cline-*",
)
WARP_METHODS = link_everywhere(
    "https://www.warp.dev/download", "www.warp.dev", "Warp's download page"
)
KIMI_DESKTOP_METHODS = link_everywhere(
    "https://www.kimi.com/", "www.kimi.com", "Kimi's page"
)
QWEN_DESKTOP_METHODS = link_everywhere(
    "https://chat.qwen.ai/", "chat.qwen.ai", "Qwen's page"
)

#: Desktop cards that are the same program as a CLI card: installed from
#: that card's button, never twice.
DESKTOP_SAME_AS_CLI: dict[str, str] = {
    "crush_desktop": "crush",
    "commandcode": "commandcode_cli",
}


def methods_for_os(
    methods: tuple[InstallMethod, ...], os_: InstallOs, arch: str
) -> tuple[InstallMethod, ...]:
    """Return the methods that apply to one OS and one machine architecture."""

    return tuple(
        method
        for method in methods
        if method.os is os_ and (not method.arch or method.arch == arch)
    )
