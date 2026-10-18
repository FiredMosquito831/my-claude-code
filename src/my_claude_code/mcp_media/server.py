"""``mcc-mcp``: MCC's media rails as MCP tools, over stdio (7.99.0).

An app that has the server (each Coding agents card's *Add MCP server*
button) starts this program and talks MCP to it on stdin/stdout. It offers
the tools :mod:`.tools` derives from ``GET /v1/media/models`` and carries each
call to MCC's own media doors with MCC's own address and token, so the call
routes, falls back, is priced and is logged under the app's name exactly as
the same request from any client would be. Nothing here routes, retries or
chooses a model; MCC does all of that (IV.8).

**Both protocol eras** (risk R11): built on the official MCP SDK's low-level
server, whose ``run`` answers the ``initialize`` handshake of 2025-11-25 and
earlier and the stateless 2026-07-28 protocol on the same stdio stream.

**One tool list per process** (IV.7, risk R1): read from one catalogue at
start, never re-derived, no ``listChanged``, empty instructions. A rail
change reaches the agent at its next session, never in the middle of one.
When MCC is not answering at start, the last catalogue read is used -- the
same bytes as the session before -- and with none, ``media_status`` alone.

**A call the app stops waiting for still finishes** (decision 7): every
spending call runs in this process's own task group, not in the request's;
an app that cancels gets its cancellation, and the work carries on, saves its
file and is listed by ``media_status`` as finished after the app stopped
waiting. The provider bills it either way.
"""

import argparse
import base64
import json
import mimetypes
import os
import re
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import anyio
import mcp.types as types
from anyio.abc import TaskGroup
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server

from my_claude_code.config.atomic_json import write_json_document_atomically_if_changed
from my_claude_code.config.mcp_media import (
    MCP_MEDIA_INLINE_IMAGES_HEADER,
    MCP_MEDIA_SERVER_NAME,
    TOOL_CREATE_VIDEO,
    TOOL_EDIT_IMAGE,
    TOOL_GENERATE_IMAGE,
    TOOL_GET_VIDEO,
    TOOL_MEDIA_STATUS,
    TOOL_SPEAK,
    TOOL_TRANSCRIBE,
    TOOL_TRANSLATE_AUDIO,
)
from my_claude_code.config.paths import mcp_media_catalogue_cache_path

from .client import (
    DEFAULT_APP_ID,
    HEALTH_PATH,
    MEDIA_CATALOGUE_PATH,
    FilePart,
    MccAddress,
    MccClient,
    MccNotAnswering,
    MccResponse,
    error_message,
    mcc_version,
    resolve_address,
)
from .outputs import (
    SessionLog,
    describe_folder,
    extension_for,
    named,
    resolve_target,
    save_new_file,
    sniff_image_type,
)
from .tools import (
    SAVE_TO_ARGUMENT,
    VIDEO_ID_ARGUMENT,
    MediaTool,
    derive_tools,
    tool_list_bytes,
)

#: What an ``--app`` value may hold: it becomes a request-log label.
_APP_ID = re.compile(r"[^a-z0-9._-]+")

_START_MCC = "Start MCC (mcc-server, or the My Claude Code desktop app)"


class ToolFailure(Exception):
    """A call that ends with an honest error the agent can read (``isError``)."""


@dataclass(frozen=True, slots=True)
class Outcome:
    """What one finished call returns, and how ``media_status`` lists it."""

    result: types.CallToolResult
    summary: str
    paths: tuple[str, ...] = ()


@dataclass(slots=True)
class _Job:
    done: anyio.Event = field(default_factory=anyio.Event)
    result: types.CallToolResult | None = None
    #: The app cancelled the call before it finished.
    abandoned: bool = False


@dataclass(slots=True)
class ServerState:
    """Everything one ``mcc-mcp`` process holds."""

    app_id: str
    address: MccAddress
    client: MccClient
    #: The folder the app started this server in.
    cwd: Path
    #: Where results go unless the agent says otherwise.
    out_dir: Path
    log: SessionLog = field(default_factory=SessionLog)
    tools: tuple[MediaTool, ...] = ()
    catalogue: Mapping[str, Any] | None = None
    #: Why the tool list is short, when it is.
    startup_problem: str | None = None
    #: The tool list came from the saved catalogue, not from MCC.
    from_cache: bool = False
    ready: anyio.Event = field(default_factory=anyio.Event)
    jobs: TaskGroup | None = None

    def tool(self, name: str) -> MediaTool | None:
        return next((tool for tool in self.tools if tool.name == name), None)


# ------------------------------------------------------------------ start-up


def _read_cached_catalogue() -> Mapping[str, Any] | None:
    try:
        payload = json.loads(mcp_media_catalogue_cache_path().read_text("utf-8"))
    except OSError, ValueError:
        return None
    return payload if isinstance(payload, Mapping) else None


def _save_catalogue(catalogue: Mapping[str, Any]) -> None:
    try:
        path = mcp_media_catalogue_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_document_atomically_if_changed(path, catalogue)
    except OSError:
        # A cache that cannot be written costs the next start its fallback,
        # nothing more.
        return


def _token_refused(address: MccAddress, status: int) -> str:
    return (
        f"MCC at {address.root} refused the token mcc-mcp read from MCC's "
        f"settings in {address.config_dir} (HTTP {status}). Is this the same "
        "MCC whose dashboard you use? Its ANTHROPIC_AUTH_TOKEN must match."
    )


async def load_catalogue(state: ServerState) -> None:
    """Read ``GET /v1/media/models`` once and build the session's tool list."""

    catalogue: Mapping[str, Any] | None = None
    problem: str | None = None
    try:
        response = await state.client.request("GET", MEDIA_CATALOGUE_PATH)
    except MccNotAnswering as exc:
        catalogue = _read_cached_catalogue()
        state.from_cache = catalogue is not None
        problem = (
            f"{exc}. {_START_MCC} and start a new session to refresh the tools."
            + (
                " The tools offered are the ones the last session had."
                if catalogue is not None
                else ""
            )
        )
    else:
        if response.status == 200:
            try:
                body = response.json()
            except ValueError, UnicodeDecodeError:
                body = None
            if isinstance(body, Mapping):
                catalogue = body
                _save_catalogue(body)
            else:
                problem = "MCC's media catalogue was not a JSON object."
        elif response.status in (401, 403):
            problem = _token_refused(state.address, response.status)
        elif response.status == 404:
            problem = (
                f"The MCC at {state.address.root} has no media catalogue "
                f"({MEDIA_CATALOGUE_PATH} answered 404): it is older than "
                "7.98.0. Update MCC (the dashboard's Update button, or the "
                "installer), restart it, then start a new session."
            )
        else:
            problem = (
                f"MCC answered {response.status} for {MEDIA_CATALOGUE_PATH}: "
                f"{error_message(response)}"
            )
    state.catalogue = catalogue
    state.startup_problem = problem
    state.tools = derive_tools(catalogue)
    state.ready.set()


# ------------------------------------------------------------------- results


def _text(text: str, *, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)], is_error=error
    )


def _field_value(value: Any) -> str:
    """A form field's text: strings as they are, anything else as JSON."""

    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value, ensure_ascii=False)


def _local_file(state: ServerState, raw: object, argument: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ToolFailure(f"{argument} must be a path to a local file.")
    path = Path(os.path.expanduser(raw.strip()))
    if not path.is_absolute():
        path = state.cwd / path
    if not path.is_file():
        raise ToolFailure(f"There is no file at {describe_folder(path)} ({argument}).")
    return path.resolve()


def _upload(path: Path, door_field: str) -> FilePart:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return door_field, (path.name, path.read_bytes(), mime)


def _refused(state: ServerState, tool: MediaTool, response: MccResponse) -> ToolFailure:
    if response.status in (401, 403):
        return ToolFailure(_token_refused(state.address, response.status))
    return ToolFailure(
        f"MCC answered {response.status} for {tool.name}: {error_message(response)}"
    )


def _json_object(response: MccResponse) -> Mapping[str, Any] | None:
    try:
        payload = response.json()
    except ValueError, UnicodeDecodeError:
        return None
    return payload if isinstance(payload, Mapping) else None


def _split_arguments(
    tool: MediaTool, arguments: Mapping[str, Any]
) -> tuple[dict[str, Any], str | None]:
    """The door fields to send, and ``save_to``; file arguments stay out."""

    body: dict[str, Any] = {}
    save_to: str | None = None
    for name, value in arguments.items():
        if name == SAVE_TO_ARGUMENT:
            save_to = value if isinstance(value, str) else None
            continue
        if name in tool.file_arguments or value is None:
            continue
        body[name] = value
    return body, save_to


def _multipart(
    state: ServerState, tool: MediaTool, arguments: Mapping[str, Any]
) -> tuple[dict[str, str], list[FilePart], list[str]]:
    """Form fields, uploads, and the local files read (named in the result, R10)."""

    body, _save_to = _split_arguments(tool, arguments)
    data = {name: _field_value(value) for name, value in body.items()}
    files: list[FilePart] = []
    read: list[str] = []
    for argument, spec in tool.file_arguments.items():
        raw = arguments.get(argument)
        if raw is None or raw == []:
            continue
        values = raw if isinstance(raw, list) else [raw]
        paths = [_local_file(state, value, argument) for value in values]
        door_field = (
            f"{spec.door_field}[]" if spec.many and len(paths) > 1 else spec.door_field
        )
        for path in paths:
            files.append(_upload(path, door_field))
            read.append(str(path))
    return data, files, read


async def _images(
    state: ServerState, tool: MediaTool, arguments: Mapping[str, Any]
) -> Outcome:
    # MCC hands every picture over as bytes; one the provider answered with
    # only a link to is downloaded by MCC through the provider's own client
    # (risk R8): this program never fetches a provider's address.
    headers = {MCP_MEDIA_INLINE_IMAGES_HEADER: "1"}
    body, save_to = _split_arguments(tool, arguments)
    read: list[str] = []
    if tool.file_arguments:
        data, files, read = _multipart(state, tool, arguments)
        if not files:
            raise ToolFailure(f"{tool.name} needs image_paths: the picture(s) to edit.")
        response = await state.client.request(
            "POST", tool.endpoint, data=data, files=files, headers=headers
        )
    else:
        response = await state.client.request(
            "POST", tool.endpoint, json_body=body, headers=headers
        )
    if response.status != 200:
        raise _refused(state, tool, response)
    payload = _json_object(response)
    if payload is None:
        raise ToolFailure("MCC's answer was not a JSON object.")
    items = payload.get("data")
    folder, asked = resolve_target(save_to, default_dir=state.out_dir, cwd=state.cwd)
    saved: list[str] = []
    pictures: list[types.ImageContent] = []
    notes: list[str] = []
    for index, item in enumerate(items if isinstance(items, list) else []):
        if not isinstance(item, Mapping):
            continue
        encoded = item.get("b64_json")
        if not isinstance(encoded, str) or not encoded:
            notes.append(f"Picture {index + 1} came back without its bytes.")
            continue
        raw = base64.b64decode(encoded)
        mime = sniff_image_type(raw)
        path = save_new_file(
            raw, folder, named(asked, extension_for(mime), "image", len(saved))
        )
        saved.append(str(path))
        # Decision 6: the picture also goes back to the model, so a vision
        # model can check its work and Claude Desktop shows it inline.
        pictures.append(types.ImageContent(type="image", data=encoded, mime_type=mime))
        revised = item.get("revised_prompt")
        if isinstance(revised, str) and revised:
            notes.append(f"Revised prompt: {revised}")
    if not saved:
        raise ToolFailure(
            " ".join(["MCC answered, but with no picture to save.", *notes])
        )
    lines = [f"Saved {len(saved)} picture(s):", *saved]
    if read:
        lines.append("Read: " + ", ".join(read))
    lines.extend(notes)
    return Outcome(
        result=types.CallToolResult(
            content=[types.TextContent(type="text", text="\n".join(lines)), *pictures],
            is_error=False,
        ),
        summary=f"{len(saved)} picture(s)",
        paths=tuple(saved),
    )


async def _speech(
    state: ServerState, tool: MediaTool, arguments: Mapping[str, Any]
) -> Outcome:
    body, save_to = _split_arguments(tool, arguments)
    response = await state.client.request("POST", tool.endpoint, json_body=body)
    if response.status != 200:
        raise _refused(state, tool, response)
    folder, asked = resolve_target(save_to, default_dir=state.out_dir, cwd=state.cwd)
    path = save_new_file(
        response.content,
        folder,
        named(asked, extension_for(response.content_type), "speech", 0),
    )
    # Decision 6: audio is a file the user plays, never put in the conversation.
    return Outcome(_text(f"Saved the speech: {path}"), "speech", (str(path),))


async def _transcript(
    state: ServerState, tool: MediaTool, arguments: Mapping[str, Any]
) -> Outcome:
    data, files, read = _multipart(state, tool, arguments)
    if not files:
        raise ToolFailure(f"{tool.name} needs audio_path: the audio file to read.")
    response = await state.client.request("POST", tool.endpoint, data=data, files=files)
    if response.status != 200:
        raise _refused(state, tool, response)
    text = response.content.decode("utf-8", errors="replace")
    if "json" in response.content_type:
        payload = _json_object(response)
        if payload is not None and set(payload) <= {"text", "usage", "logprobs"}:
            # The plain answer: just its text. A verbose one (segments,
            # words) is returned whole, as JSON.
            text = str(payload.get("text") or "")
    return Outcome(_text(text), f"text from {read[0]}")


async def _create_video(
    state: ServerState, tool: MediaTool, arguments: Mapping[str, Any]
) -> Outcome:
    # Multipart, as the OpenAI SDK always sends a create: the form every
    # declared video host was proven on.
    data, files, read = _multipart(state, tool, arguments)
    response = await state.client.request(
        "POST", tool.endpoint, data=data, files=files, multipart=True
    )
    if response.status not in (200, 201, 202):
        raise _refused(state, tool, response)
    payload = _json_object(response) or {}
    job_id = payload.get("id")
    if not isinstance(job_id, str) or not job_id:
        raise ToolFailure("MCC accepted the video but named no job id.")
    status = payload.get("status")
    state.log.video_jobs[job_id] = tool.rail
    lines = [
        f"Video job started: {job_id} (status: {status or 'unknown'}).",
        f"Call get_video with video_id={job_id} to see its progress; it saves "
        "the video when the job is done.",
    ]
    if read:
        lines.append("Read: " + ", ".join(read))
    return Outcome(_text("\n".join(lines)), f"video job {job_id} started")


async def _get_video(
    state: ServerState, tool: MediaTool, arguments: Mapping[str, Any]
) -> Outcome:
    job_id = arguments.get(VIDEO_ID_ARGUMENT)
    if not isinstance(job_id, str) or not job_id.strip():
        raise ToolFailure("get_video needs the video_id create_video returned.")
    job_id = job_id.strip()
    job_path = f"{tool.endpoint}/{quote(job_id, safe='')}"
    response = await state.client.request("GET", job_path)
    if response.status != 200:
        raise _refused(state, tool, response)
    payload = _json_object(response)
    if payload is None:
        raise ToolFailure("MCC's answer about the job was not a JSON object.")
    status = str(payload.get("status") or "unknown")
    if status == "failed":
        error = payload.get("error")
        message = error.get("message") if isinstance(error, Mapping) else error
        raise ToolFailure(f"Video job {job_id} failed: {message or 'no reason given'}")
    if status != "completed":
        progress = payload.get("progress")
        shown = f", {progress}% done" if isinstance(progress, int | float) else ""
        return Outcome(
            _text(
                f"Video job {job_id} is {status}{shown}. Call get_video again later."
            ),
            f"video {job_id} {status}",
        )
    content = await state.client.request("GET", f"{job_path}/content")
    if content.status != 200:
        raise _refused(state, tool, content)
    save_to = arguments.get(SAVE_TO_ARGUMENT)
    folder, asked = resolve_target(
        save_to if isinstance(save_to, str) else None,
        default_dir=state.out_dir,
        cwd=state.cwd,
    )
    path = save_new_file(
        content.content,
        folder,
        named(asked, extension_for(content.content_type or "video/mp4"), "video", 0),
    )
    return Outcome(_text(f"Saved the video: {path}"), f"video {job_id}", (str(path),))


Handler = Callable[[ServerState, MediaTool, Mapping[str, Any]], Awaitable[Outcome]]

_HANDLERS: Mapping[str, Handler] = {
    TOOL_GENERATE_IMAGE: _images,
    TOOL_EDIT_IMAGE: _images,
    TOOL_SPEAK: _speech,
    TOOL_TRANSCRIBE: _transcript,
    TOOL_TRANSLATE_AUDIO: _transcript,
    TOOL_CREATE_VIDEO: _create_video,
    TOOL_GET_VIDEO: _get_video,
}


async def media_status(state: ServerState) -> types.CallToolResult:
    """Free: what is configured, where MCC is, and this session's results."""

    try:
        health = await state.client.request("GET", HEALTH_PATH)
        answering = "answering" if health.status == 200 else f"answered {health.status}"
    except MccNotAnswering as exc:
        answering = f"not answering ({exc.reason}); {_START_MCC.lower()}"
    address = state.address
    token = (
        "read from MCC's settings (never shown)."
        if address.token_set
        else "none set in MCC's settings (MCC then accepts requests without one)."
    )
    where = describe_folder(state.out_dir)
    if state.out_dir == state.cwd:
        where += " (the folder the app started this server in)"
    lines = [
        f"MCC media server (mcc-mcp {mcc_version()}) for app: {state.app_id}",
        f"MCC: {address.root} -- {answering}. Port read from: "
        f"{address.port_source} (MCC settings folder {address.config_dir}).",
        f"Token: {token}",
        f"Results are saved in: {where}.",
    ]
    if state.startup_problem:
        lines.append(f"At start: {state.startup_problem}")
    rails = state.catalogue.get("rails") if state.catalogue is not None else None
    if isinstance(rails, list):
        lines.append("Rails (as read at the start of this session):")
        for rail in rails:
            if not isinstance(rail, Mapping):
                continue
            models = rail.get("models")
            refs = [str(ref) for ref in models] if isinstance(models, list) else []
            offered = [
                tool.name for tool in state.tools if tool.rail == rail.get("rail")
            ]
            lines.append(
                f"- {rail.get('label') or rail.get('rail')}: "
                + (", ".join(refs) if refs else "empty, so no tool")
                + (f" -> {', '.join(offered)}" if offered else "")
            )
    lines.append(
        f"Tools this session: {', '.join(tool.name for tool in state.tools)} "
        f"({tool_list_bytes(state.tools)} bytes of tool text)."
    )
    results = state.log.snapshot()
    if results:
        lines.append("This session:")
        for result in results:
            paths = (" -> " + ", ".join(result.paths)) if result.paths else ""
            late = (
                " (finished after the app stopped waiting; the provider billed it)"
                if result.after_app_stopped_waiting
                else ""
            )
            failed = " (failed)" if result.error else ""
            lines.append(
                f"- {result.finished_at} {result.tool}: "
                f"{result.summary}{failed}{paths}{late}"
            )
    if state.log.video_jobs:
        lines.append(
            "Video jobs started this session (check with get_video): "
            + ", ".join(state.log.video_jobs)
        )
    return _text("\n".join(lines))


async def run_tool(
    state: ServerState, tool: MediaTool, arguments: Mapping[str, Any], job: _Job
) -> None:
    """Carry one call to MCC and keep its result, whether or not anyone waits."""

    handler = _HANDLERS[tool.name]
    try:
        outcome = await handler(state, tool, arguments)
    except ToolFailure as exc:
        outcome = Outcome(_text(str(exc), error=True), str(exc)[:200])
    except MccNotAnswering as exc:
        message = (
            f"{exc}. MCC may still have finished it: its Requests page shows "
            "every request."
            if exc.connected
            else f"{exc}. {_START_MCC} and try again."
        )
        outcome = Outcome(_text(message, error=True), message[:200])
    except OSError as exc:
        outcome = Outcome(
            _text(f"Could not save the result: {exc}", error=True),
            "could not save the result",
        )
    state.log.add(
        tool.name,
        outcome.summary,
        paths=outcome.paths,
        error=bool(outcome.result.is_error),
        after_app_stopped_waiting=job.abandoned,
    )
    job.result = outcome.result
    job.done.set()


async def call_tool(
    state: ServerState, name: str, arguments: Mapping[str, Any] | None
) -> types.CallToolResult:
    """One ``tools/call``: ``media_status`` inline, everything else as a job."""

    await state.ready.wait()
    tool = state.tool(name)
    if tool is None:
        return _text(
            f"{name} is not offered in this session. media_status lists the tools.",
            error=True,
        )
    if tool.name == TOOL_MEDIA_STATUS:
        return await media_status(state)
    job = _Job()
    if state.jobs is None:
        await run_tool(state, tool, dict(arguments or {}), job)
    else:
        state.jobs.start_soon(run_tool, state, tool, dict(arguments or {}), job)
    try:
        await job.done.wait()
    except anyio.get_cancelled_exc_class():
        # Decision 7: the app stopped waiting. The job keeps running in this
        # process's own task group, saves its file and is listed by
        # media_status; the cancellation itself is the app's to have.
        job.abandoned = True
        raise
    assert job.result is not None
    return job.result


def build_server(state: ServerState) -> Server[Any]:
    """The low-level MCP server, wired to this process's state."""

    async def on_list_tools(
        _ctx: ServerRequestContext[Any], _params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        await state.ready.wait()
        return types.ListToolsResult(
            tools=[
                types.Tool.model_validate(dict(tool.definition)) for tool in state.tools
            ]
        )

    async def on_call_tool(
        _ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        return await call_tool(state, params.name, params.arguments)

    return Server(
        MCP_MEDIA_SERVER_NAME,
        version=mcc_version(),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


# ---------------------------------------------------------------------- main


def app_id_from(raw: str | None) -> str:
    """An ``--app`` value as a request-log label (``mcc_mcp`` when empty)."""

    cleaned = _APP_ID.sub("_", (raw or "").strip().lower()).strip("_")
    return cleaned[:64] or DEFAULT_APP_ID


def build_state(
    *,
    app_id: str,
    out_dir: str | None,
    environ: Mapping[str, str],
    cwd: Path | None = None,
) -> ServerState:
    """Resolve MCC's address and token, and where results go."""

    address, token = resolve_address(environ)
    here = (cwd or Path.cwd()).resolve()
    out = Path(os.path.expanduser(out_dir)).resolve() if out_dir else here
    return ServerState(
        app_id=app_id,
        address=address,
        client=MccClient(address, token, app_id=app_id),
        cwd=here,
        out_dir=out,
    )


async def serve(state: ServerState) -> None:
    """Answer MCP on stdin/stdout until the app closes the stream."""

    server = build_server(state)
    try:
        async with anyio.create_task_group() as jobs:
            state.jobs = jobs
            jobs.start_soon(load_catalogue, state)
            async with stdio_server() as (read_stream, write_stream):
                await server.run(
                    read_stream, write_stream, server.create_initialization_options()
                )
            # Leaving the group waits for every call still running: a call
            # the app stopped waiting for finishes and saves before exit.
    finally:
        await state.client.aclose()


def _arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mcc-mcp",
        description=(
            "MCC's media server for MCP apps: image, speech, transcription and "
            "video tools from the media rails you configured. An app starts it "
            "and talks to it on stdin/stdout; add it to an app with the Add "
            "MCP server button on the dashboard's Coding agents page."
        ),
    )
    parser.add_argument(
        "--app",
        default=DEFAULT_APP_ID,
        help="the app's id, as MCC's request log names it (default: mcc_mcp)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="folder for results (default: the folder the app starts it in)",
    )
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> None:
    """Console entry point: ``mcc-mcp [--app <id>] [--out <folder>]``."""

    options = _arguments(sys.argv[1:] if argv is None else argv)
    state = build_state(
        app_id=app_id_from(options.app), out_dir=options.out, environ=os.environ
    )
    anyio.run(serve, state)


if __name__ == "__main__":
    # ``python -m my_claude_code.mcp_media.server`` -- what the tests start,
    # so they need no installed console script.
    main()
