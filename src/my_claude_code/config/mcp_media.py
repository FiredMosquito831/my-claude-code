"""MCC's media MCP server: which apps get it, and the entry each one is given.

7.99.0, user decisions of 2026-10-18 12:51 (``specs/PR-MCP-SERVER-SPEC.md``).
``mcc-mcp`` is a small stdio program shipped with MCC that offers a coding
agent MCC's media rails as tools. It is **opt-in per app**: each card on the
Coding agents page has an *Add MCP server* / *Remove MCP server* button that
writes one app id into ``MCP_MEDIA_APPS`` (or takes it out), and nothing else
reads that setting. With the setting empty -- the default -- no document MCC
writes and no launch line it builds carries a byte of this module's output.

This module is the one table and the pure builders. It decides nothing about a
request: the server never injects a tool (decision 1 of 2026-10-04), and the
entries below only tell an app where the program is. Every entry is written
into something MCC already owns -- a launch line (Claude Code, Codex), a config
layer under ``~/.mcc`` (OpenCode, OpenCode 2, Kilo, Crush, Qwen Code, Gemini
CLI) or MCC's own Claude Desktop document. The user's own files came in the
second release (decision 11, 7.100.0): Codex's ``config.toml``, OpenCode's
``opencode.json``, Command Code's ``mcp.json`` and, as a fallback, Claude
Desktop's ``claude_desktop_config.json`` -- each through its card under
Desktop apps, with the plan diff first, a one-time backup and Undo
(``config/desktop_apply.py``). :func:`desktop_file_entry` builds those entries.
Codex is the one app whose generated document changes in another way while
its button is on (7.107.0): MCC's Codex model list says "no tool search", and
the launch line marks the server required (:func:`mcp_media_document`,
:func:`codex_config_arguments`).

What is never written: the proxy token (``mcc-mcp`` reads it from MCC's own
settings at run time), a timeout or limit of MCC's choosing (IV.1), a
"trusted" / "always allow" switch (decision 8: each app's own prompt is the
gate). For OpenCode and Kilo, which do not ask before a tool by default, MCC's
own layer turns that app's ``ask`` on for the tools that spend money.

``config`` is a leaf: stdlib and ``config`` only.
"""

import json
import os
import shutil
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from my_claude_code.config.paths import mcp_media_output_dir

#: The name every app shows the server under. One spelling everywhere, so an
#: OpenCode permission key (``mcc_media_generate_image``) and a Claude Code
#: tool name (``mcp__mcc_media__generate_image``) both start with it.
MCP_MEDIA_SERVER_NAME = "mcc_media"
#: The console script (``pyproject.toml`` ``[project.scripts]``).
MCP_MEDIA_COMMAND = "mcc-mcp"
#: ``mcc-mcp`` arguments. ``--app`` names the app on every request MCC logs;
#: ``--out`` is the folder results go to when the app has no working folder.
MCP_MEDIA_APP_ARGUMENT = "--app"
MCP_MEDIA_OUT_ARGUMENT = "--out"
#: The one environment value a registration passes on, and only when the MCC
#: that wrote it runs with it: several apps start an MCP server with a short
#: allow-list of variables, so a non-default config folder would otherwise be
#: lost on the way and ``mcc-mcp`` would read another MCC's settings.
MCP_MEDIA_CONFIG_DIR_ENV = "MCC_CONFIG_DIR"
#: Request header that asks MCC's OpenAI image doors to hand every picture
#: over as bytes: a picture the provider answered with only a link to is
#: downloaded by MCC, through the provider's own client and proxy leg, exactly
#: as the Gemini image door already does -- so ``mcc-mcp`` never fetches a
#: provider's link itself (full IP masking). Sent by ``mcc-mcp`` only; a
#: request without it is answered exactly as before.
MCP_MEDIA_INLINE_IMAGES_HEADER = "x-mcc-inline-images"

# -- the tools, by name ----------------------------------------------------------

TOOL_MEDIA_STATUS = "media_status"
TOOL_GENERATE_IMAGE = "generate_image"
TOOL_EDIT_IMAGE = "edit_image"
TOOL_SPEAK = "speak"
TOOL_TRANSCRIBE = "transcribe"
TOOL_TRANSLATE_AUDIO = "translate_audio"
TOOL_CREATE_VIDEO = "create_video"
TOOL_GET_VIDEO = "get_video"

#: Tools a provider bills. Never marked read-only, so an app that asks before a
#: tool asks before these (decision 8); the OpenCode-family layer names them.
MCP_MEDIA_SPENDING_TOOLS: tuple[str, ...] = (
    TOOL_GENERATE_IMAGE,
    TOOL_EDIT_IMAGE,
    TOOL_SPEAK,
    TOOL_TRANSCRIBE,
    TOOL_TRANSLATE_AUDIO,
    TOOL_CREATE_VIDEO,
)
#: Tools that spend nothing: what is configured, and a video job's state.
MCP_MEDIA_FREE_TOOLS: tuple[str, ...] = (TOOL_MEDIA_STATUS, TOOL_GET_VIDEO)


class McpMediaSupport(StrEnum):
    """What a card can offer, measured per app (spec §2)."""

    #: MCC owns where the app reads its MCP servers from: the button works.
    BUTTON = "button"
    #: The app speaks MCP; where MCC could register it has not been measured
    #: on an installed copy yet.
    NOT_MEASURED = "not_measured"
    #: No MCP client; MCC's own bundled extension will offer the tools later.
    EXTENSION_LATER = "extension_later"
    #: The app has no MCP client at all.
    NO_MCP = "no_mcp"
    #: The app reads MCP servers only from the user's own file, which MCC
    #: does not write in this release (Configure + backup + Undo, next).
    OWN_FILE_LATER = "own_file_later"
    #: The app reads MCP servers from the user's own file, and MCC adds its
    #: entry there from the app's card under Desktop apps -- with the plan
    #: diff first, a one-time backup and Undo (7.100.0).
    DESKTOP_CARD = "desktop_card"


class McpMediaShape(StrEnum):
    """The form an app is told about the server in."""

    #: ``claude --mcp-config=<MCC's file>``: ``{"mcpServers": {...}}``.
    CLAUDE_MCP_CONFIG = "claude_mcp_config"
    #: ``codex -c mcp_servers.mcc_media.*=...`` for one launch.
    CODEX_CONFIG_ARGS = "codex_config_args"
    #: ``mcp.<name> = {type: "local", command: [...]}`` (OpenCode, Kilo).
    OPENCODE_MCP = "opencode_mcp"
    #: ``mcp.<name> = {type: "stdio", command, args}`` (Crush).
    CRUSH_MCP = "crush_mcp"
    #: ``mcpServers.<name> = {command, args}`` (Qwen Code, Gemini CLI).
    MCP_SERVERS = "mcp_servers"
    #: ``managedMcpServers: [{name, transport: "stdio", ...}]`` in MCC's own
    #: Claude Desktop document.
    CLAUDE_DESKTOP_MANAGED = "claude_desktop_managed"
    #: ``mcpServers.<name> = {transport: "stdio", enabled, command, args}``
    #: in Command Code's ``~/.commandcode/mcp.json`` (its bundled 1.65.0
    #: documentation, ``reference/mcp.md``, "Config schema").
    COMMANDCODE_MCP = "commandcode_mcp"


@dataclass(frozen=True, slots=True)
class McpMediaApp:
    """One card's media-server row."""

    app_id: str
    display_name: str
    support: McpMediaSupport
    shape: McpMediaShape | None = None
    #: How the app gates a tool that spends money, in the card's words.
    asks: str = ""
    #: Anything else the reader needs before pressing the button.
    note: str = ""


#: The table. Order is the order a setting is written in, so the value the
#: buttons produce is the same whichever card was pressed first.
MCP_MEDIA_APP_TABLE: tuple[McpMediaApp, ...] = (
    McpMediaApp(
        "claude",
        "Claude Code",
        McpMediaSupport.BUTTON,
        McpMediaShape.CLAUDE_MCP_CONFIG,
        asks="Claude Code asks before it runs an MCP tool you have not allowed.",
        note=(
            "Takes effect at the next mcc-claude launch: MCC hands Claude Code "
            "its own file with --mcp-config=, so ~/.claude.json is never edited."
        ),
    ),
    McpMediaApp(
        "codex",
        "Codex CLI",
        McpMediaSupport.BUTTON,
        McpMediaShape.CODEX_CONFIG_ARGS,
        asks=(
            "Codex asks before each media tool that spends money: none of them "
            "is marked read-only."
        ),
        note=(
            "Takes effect at the next mcc-codex launch, as -c settings for that "
            "launch only; your config.toml is not touched. While it is added, "
            "MCC's Codex model list says its models have no tool search, so "
            "Codex gives its model the media tools directly -- and its own "
            "sub-agent tools with them -- and the server is marked required: "
            "every Codex start waits about 3 seconds for it, and if mcc-mcp is "
            "broken Codex will not start until you press Remove MCP server. "
            "The tools Codex sends grow from about 9.5 KB to about 31 KB per "
            "request."
        ),
    ),
    McpMediaApp(
        "opencode",
        "OpenCode",
        McpMediaSupport.BUTTON,
        McpMediaShape.OPENCODE_MCP,
        asks=(
            "OpenCode does not ask before a tool by default, so MCC's own "
            "config layer sets its ask for the tools that spend money."
        ),
        note="Takes effect at the next mcc-opencode launch.",
    ),
    McpMediaApp(
        "opencode2",
        "OpenCode 2",
        McpMediaSupport.BUTTON,
        McpMediaShape.OPENCODE_MCP,
        asks=(
            "OpenCode does not ask before a tool by default, so MCC's own "
            "config layer sets its ask for the tools that spend money."
        ),
        note=(
            "Takes effect at the next mcc-opencode2 launch. The preview offers "
            "MCP tools inside its Code Mode once its background service has "
            "connected the server; the first run after the service starts can "
            "begin before it has."
        ),
    ),
    McpMediaApp(
        "kilo",
        "Kilo CLI",
        McpMediaSupport.BUTTON,
        McpMediaShape.OPENCODE_MCP,
        asks=(
            "Kilo does not ask before a tool by default, so MCC's own config "
            "layer sets its ask for the tools that spend money."
        ),
        note="Takes effect at the next mcc-kilo launch.",
    ),
    McpMediaApp(
        "crush",
        "Crush",
        McpMediaSupport.BUTTON,
        McpMediaShape.CRUSH_MCP,
        asks="Crush asks before it runs a tool you have not allowed.",
        note="Takes effect at the next mcc-crush launch.",
    ),
    McpMediaApp(
        "qwen_code",
        "Qwen Code",
        McpMediaSupport.BUTTON,
        McpMediaShape.MCP_SERVERS,
        asks=("Qwen Code asks before an MCP tool: MCC never marks the server trusted."),
        note="Takes effect at the next mcc-qwen launch.",
    ),
    McpMediaApp(
        "gemini_cli",
        "Gemini CLI",
        McpMediaSupport.BUTTON,
        McpMediaShape.MCP_SERVERS,
        asks=(
            "Gemini CLI asks before an MCP tool: MCC never marks the server trusted."
        ),
        note=(
            "Gemini CLI starts MCP servers only in a folder you have trusted; "
            "in an untrusted one it lists the server as disabled."
        ),
    ),
    McpMediaApp(
        "claude_desktop",
        "Claude Desktop",
        McpMediaSupport.BUTTON,
        McpMediaShape.CLAUDE_DESKTOP_MANAGED,
        asks="Claude Desktop asks before each tool: MCC writes no tool policy.",
        note=(
            "Written into MCC's own Claude Desktop document (Desktop apps, "
            "below) when it is configured; results are saved in the mcp-media "
            "folder of MCC's configuration folder. Relaunch Claude Desktop "
            "once to load it."
        ),
    ),
    McpMediaApp(
        "commandcode_cli",
        "Command Code",
        McpMediaSupport.DESKTOP_CARD,
        note=(
            "Command Code reads MCP servers from your own "
            "~/.commandcode/mcp.json. Press Add MCP server on the Command Code "
            "card under Desktop apps: it shows what it will write first, takes "
            "a one-time backup, and Undo takes it back out."
        ),
    ),
    McpMediaApp(
        "kimi_code",
        "Kimi Code",
        McpMediaSupport.NOT_MEASURED,
        note="Speaks MCP; not measured on an installed copy yet.",
    ),
    McpMediaApp(
        "goose",
        "Goose",
        McpMediaSupport.NOT_MEASURED,
        note="Speaks MCP; not measured on an installed copy yet.",
    ),
    McpMediaApp(
        "cline_cli",
        "Cline CLI",
        McpMediaSupport.NOT_MEASURED,
        note="Speaks MCP; not measured on an installed copy yet.",
    ),
    McpMediaApp(
        "droid",
        "Droid",
        McpMediaSupport.NOT_MEASURED,
        note="Speaks MCP; not measured on an installed copy yet.",
    ),
    McpMediaApp(
        "pi",
        "Pi",
        McpMediaSupport.EXTENSION_LATER,
        note=(
            "Pi has no MCP client; MCC's bundled Pi extension will offer the "
            "same tools in a later release."
        ),
    ),
    McpMediaApp(
        "aider",
        "Aider",
        McpMediaSupport.NO_MCP,
        note="Aider has no MCP client, so there is nothing to add.",
    ),
)

_BY_ID: dict[str, McpMediaApp] = {app.app_id: app for app in MCP_MEDIA_APP_TABLE}


def mcp_media_app(app_id: str) -> McpMediaApp | None:
    """Return one app's row, or ``None`` for an id the table does not know."""

    return _BY_ID.get(app_id)


def button_app_ids() -> tuple[str, ...]:
    """Every app whose card has the Add / Remove MCP server button."""

    return tuple(
        app.app_id
        for app in MCP_MEDIA_APP_TABLE
        if app.support is McpMediaSupport.BUTTON
    )


def parse_mcp_media_apps(value: str | None) -> tuple[str, ...]:
    """The app ids a ``MCP_MEDIA_APPS`` value turns on, in the table's order.

    Commas or whitespace separate ids; case is ignored; an id with no button
    (unknown, or an app MCC cannot register yet) is ignored rather than
    refused, so a hand-edited value never stops the server starting.
    """

    named = {
        part.strip().lower()
        for chunk in (value or "").split(",")
        for part in chunk.split()
        if part.strip()
    }
    return tuple(app_id for app_id in button_app_ids() if app_id in named)


def format_mcp_media_apps(app_ids: Iterable[str]) -> str:
    """The canonical ``MCP_MEDIA_APPS`` value for a set of app ids."""

    wanted = set(app_ids)
    return ",".join(app_id for app_id in button_app_ids() if app_id in wanted)


def with_app(value: str | None, app_id: str, *, on: bool) -> str:
    """``MCP_MEDIA_APPS`` with one app added or taken out (canonical form)."""

    current = set(parse_mcp_media_apps(value))
    if on:
        current.add(app_id)
    else:
        current.discard(app_id)
    return format_mcp_media_apps(current)


def mcp_media_on(value: str | None, app_id: str) -> bool:
    """Whether ``MCP_MEDIA_APPS`` turns the server on for this app."""

    return app_id in parse_mcp_media_apps(value)


def mcp_media_command(
    *,
    executable: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> str | None:
    """The absolute path of ``mcc-mcp``, or ``None`` when it cannot be found.

    First the one installed beside the Python running this process -- the
    same install, so the same version as the server or launcher asking --
    then a search of ``PATH``. Absolute because Claude Desktop requires it,
    and because an app started from a desktop icon may have no ``PATH`` entry
    for MCC at all.
    """

    interpreter = Path(executable or sys.executable)
    name = f"{MCP_MEDIA_COMMAND}.exe" if os.name == "nt" else MCP_MEDIA_COMMAND
    beside = interpreter.parent / name
    if beside.is_file():
        return str(beside)
    found = which(MCP_MEDIA_COMMAND)
    return os.path.abspath(found) if found else None


def server_arguments(app_id: str, *, out_dir: str | None = None) -> list[str]:
    """``--app <id>`` (and ``--out <folder>`` for an app with no working folder)."""

    arguments = [MCP_MEDIA_APP_ARGUMENT, app_id]
    if out_dir:
        arguments.extend([MCP_MEDIA_OUT_ARGUMENT, out_dir])
    return arguments


def server_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """The environment a registration passes on: MCC's config folder, if set."""

    config_dir = (environ.get(MCP_MEDIA_CONFIG_DIR_ENV) or "").strip()
    return {MCP_MEDIA_CONFIG_DIR_ENV: config_dir} if config_dir else {}


# -- the entries, one builder per shape ----------------------------------------


def claude_mcp_config_document(
    command: str, environ: Mapping[str, str]
) -> dict[str, Any]:
    """The document ``mcc-claude`` hands Claude Code with ``--mcp-config=``."""

    entry: dict[str, Any] = {
        "type": "stdio",
        "command": command,
        "args": server_arguments("claude"),
    }
    env = server_environment(environ)
    if env:
        entry["env"] = env
    return {"mcpServers": {MCP_MEDIA_SERVER_NAME: entry}}


def codex_config_arguments(command: str, environ: Mapping[str, str]) -> list[str]:
    """``-c`` assignments that give one Codex launch the server.

    Codex parses each right-hand side as TOML, whose basic string, array of
    strings and inline-table forms are written here with ``json.dumps`` for
    every string -- the same quoting rule the launcher's own assignments use.

    ``required = true`` (7.107.0, user decision 3 of 2026-10-19): before a
    turn Codex waits only about one second for a server that is not
    required (its ``mcp_optional_startup_grace_ms``), and ``mcc-mcp``
    answers ``initialize`` after about three, so ``mcc-codex exec`` and a
    quick first prompt never saw the tools. ``required`` makes Codex wait for
    the server at the start of the session, under Codex's own startup
    deadline -- a declaration Codex documents, not a timeout of MCC's (IV.1).
    The cost, stated on the card: a server that cannot start stops Codex
    until *Remove MCP server*. Only this launch line carries it; the entry
    the Desktop apps card writes into the user's own ``config.toml``
    (:func:`desktop_file_entry`) never does, so a broken ``mcc-mcp`` cannot
    stop plain ``codex`` or the Codex desktop app (risk R6).
    """

    prefix = f"mcp_servers.{MCP_MEDIA_SERVER_NAME}"
    arguments = [
        "-c",
        f"{prefix}.command={json.dumps(command)}",
        "-c",
        f"{prefix}.args=[{', '.join(json.dumps(a) for a in server_arguments('codex'))}]",
    ]
    env = server_environment(environ)
    if env:
        pairs = ", ".join(
            f"{json.dumps(name)}={json.dumps(value)}" for name, value in env.items()
        )
        arguments.extend(["-c", f"{prefix}.env={{{pairs}}}"])
    arguments.extend(["-c", f"{prefix}.required=true"])
    return arguments


def claude_desktop_managed_server(
    command: str, out_dir: str, environ: Mapping[str, str]
) -> dict[str, Any]:
    """MCC's element of Claude Desktop's ``managedMcpServers``.

    No ``toolPolicy``: an unlisted tool is the user's to allow, so Claude
    Desktop asks before each one (its own documented default). No
    ``startupTimeoutSec``: the app's own default applies.
    """

    entry: dict[str, Any] = {
        "name": MCP_MEDIA_SERVER_NAME,
        "transport": "stdio",
        "command": command,
        "args": server_arguments("claude_desktop", out_dir=out_dir),
    }
    env = server_environment(environ)
    if env:
        entry["env"] = env
    return entry


def desktop_mcp_server(
    app_id: str,
    *,
    apps_value: str | None,
    environ: Mapping[str, str],
    out_dir: str | None = None,
    command: str | None = None,
) -> dict[str, Any] | None:
    """The entry MCC's own desktop-app document carries, or ``None`` when off.

    ``None`` too for an app whose document holds no MCP servers, and when
    ``mcc-mcp`` cannot be found -- a registration naming a program that is
    not there is a server the app would fail to start on every launch.
    """

    app = mcp_media_app(app_id)
    if app is None or app.shape is not McpMediaShape.CLAUDE_DESKTOP_MANAGED:
        return None
    if not mcp_media_on(apps_value, app_id):
        return None
    resolved = command if command is not None else mcp_media_command()
    if not resolved:
        return None
    folder = out_dir if out_dir is not None else str(mcp_media_output_dir())
    return claude_desktop_managed_server(resolved, folder, environ)


def desktop_file_entry(
    shape: McpMediaShape,
    app_id: str,
    command: str,
    environ: Mapping[str, str],
    *,
    out_dir: str | None = None,
) -> dict[str, Any]:
    """MCC's server entry for an app's *own* file (7.100.0, decision 11).

    The same shapes the launcher layers use, built by the same helpers --
    never through a catalogue serialiser (risk R13): the OpenCode desktop
    entry is ``_opencode_entry`` exactly as MCC's own layer has it, with this
    card's ``--app`` id. No token, no timeout, no trust switch (decisions 1
    and 8, IV.1): ``mcc-mcp`` reads MCC's address and token at run time.
    """

    match shape:
        case McpMediaShape.OPENCODE_MCP:
            return _opencode_entry(app_id, command, environ)
        case McpMediaShape.COMMANDCODE_MCP:
            entry: dict[str, Any] = {
                "transport": "stdio",
                "enabled": True,
                "command": command,
                "args": server_arguments(app_id, out_dir=out_dir),
            }
            env = server_environment(environ)
            if env:
                entry["env"] = env
            return entry
        case _:
            entry = {
                "command": command,
                "args": server_arguments(app_id, out_dir=out_dir),
            }
            env = server_environment(environ)
            if env:
                entry["env"] = env
            return entry


def _opencode_entry(
    app_id: str, command: str, environ: Mapping[str, str]
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "type": "local",
        "command": [command, *server_arguments(app_id)],
        "enabled": True,
    }
    env = server_environment(environ)
    if env:
        entry["environment"] = env
    return entry


def _crush_entry(
    app_id: str, command: str, environ: Mapping[str, str]
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "type": "stdio",
        "command": command,
        "args": server_arguments(app_id),
    }
    env = server_environment(environ)
    if env:
        entry["env"] = env
    return entry


def _mcp_servers_entry(
    app_id: str, command: str, environ: Mapping[str, str]
) -> dict[str, Any]:
    # Never ``trust``: on Qwen Code and Gemini CLI it skips the confirmation
    # prompt, which is the spending gate (decision 8, risk R16).
    entry: dict[str, Any] = {"command": command, "args": server_arguments(app_id)}
    env = server_environment(environ)
    if env:
        entry["env"] = env
    return entry


def opencode_permission_keys() -> tuple[str, ...]:
    """The OpenCode/Kilo permission keys MCC sets to ``ask`` (decision 8).

    OpenCode names an MCP tool ``<server>_<tool>``; only the tools that spend
    money are named, so ``media_status`` and ``get_video`` stay free to call.
    """

    return tuple(f"{MCP_MEDIA_SERVER_NAME}_{tool}" for tool in MCP_MEDIA_SPENDING_TOOLS)


def with_mcp_media_block(
    shape: McpMediaShape,
    app_id: str,
    document: Mapping[str, Any],
    command: str,
    environ: Mapping[str, str],
) -> dict[str, Any]:
    """A copy of an MCC-owned config document with the server added.

    Added beside whatever the document already declares under the same key,
    and only MCC's own entry is replaced -- the document is MCC's, but the
    merge is the same one an app performs when it layers it over the user's
    own config, so an entry of the user's elsewhere keeps its name.
    """

    result: dict[str, Any] = dict(document)
    match shape:
        case McpMediaShape.OPENCODE_MCP:
            servers = dict(result.get("mcp") or {})
            servers[MCP_MEDIA_SERVER_NAME] = _opencode_entry(app_id, command, environ)
            result["mcp"] = servers
            existing = result.get("permission")
            permission: dict[str, Any] = (
                dict(existing) if isinstance(existing, Mapping) else {}
            )
            for key in opencode_permission_keys():
                permission[key] = "ask"
            result["permission"] = permission
        case McpMediaShape.CRUSH_MCP:
            servers = dict(result.get("mcp") or {})
            servers[MCP_MEDIA_SERVER_NAME] = _crush_entry(app_id, command, environ)
            result["mcp"] = servers
        case McpMediaShape.MCP_SERVERS:
            servers = dict(result.get("mcpServers") or {})
            servers[MCP_MEDIA_SERVER_NAME] = _mcp_servers_entry(
                app_id, command, environ
            )
            result["mcpServers"] = servers
        case _:
            return dict(document)
    return result


#: The key in each entry of Codex's ``model_catalog_json`` that tells Codex
#: the model takes its ``tool_search`` tool (``application/catalogues/codex``).
CODEX_SEARCH_TOOL_KEY = "supports_search_tool"


def codex_catalogue_without_tool_search(
    document: Mapping[str, Any],
) -> dict[str, Any]:
    """A copy of MCC's Codex model list whose models say "no tool search".

    7.107.0, user decision 1 of 2026-10-19 (option D of the Codex MCP
    investigation). A model that supports tool search gets every MCP
    tool -- and Codex's own sub-agent tools -- only behind Codex's
    ``tool_search`` tool, which MCC's Responses door does not carry
    (``core/openai_responses/tools.py``), so the model never saw
    ``generate_image``. With the key ``false`` Codex lists those tools
    directly. Only that one key changes, on every entry; every other key,
    the entries' order and the rest of the document are as the serialiser
    wrote them.
    """

    result = dict(document)
    models = document.get("models")
    if isinstance(models, list):
        result["models"] = [
            {**entry, CODEX_SEARCH_TOOL_KEY: False}
            if isinstance(entry, Mapping)
            else entry
            for entry in models
        ]
    return result


def mcp_media_document(
    app_id: str,
    document: Mapping[str, Any],
    *,
    apps_value: str | None,
    environ: Mapping[str, str],
    command: str | None = None,
) -> Mapping[str, Any]:
    """One generated document, with the server added when this app's button is on.

    Returns ``document`` itself -- the same object, so the same bytes -- when
    the app is off, has no document shape, or ``mcc-mcp`` cannot be found.
    Called **after** the catalogue serialiser and outside it (risk R13): the
    OpenCode desktop app's Configure document comes from the same serialiser
    and must never carry a launcher's switch.

    Codex (7.107.0) is told about the server on its launch line
    (:func:`codex_config_arguments`), so its document -- the model list --
    does not gain an entry: while its button is on, the list says its models
    have no tool search (:func:`codex_catalogue_without_tool_search`), so the
    tools the launch line adds reach the model.
    """

    app = mcp_media_app(app_id)
    if app is None or app.shape not in (
        McpMediaShape.OPENCODE_MCP,
        McpMediaShape.CRUSH_MCP,
        McpMediaShape.MCP_SERVERS,
        McpMediaShape.CODEX_CONFIG_ARGS,
    ):
        return document
    if not mcp_media_on(apps_value, app_id):
        return document
    resolved = command if command is not None else mcp_media_command()
    if not resolved:
        return document
    if app.shape is McpMediaShape.CODEX_CONFIG_ARGS:
        return codex_catalogue_without_tool_search(document)
    assert app.shape is not None
    return with_mcp_media_block(app.shape, app_id, document, resolved, environ)
