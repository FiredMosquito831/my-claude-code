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
from urllib.parse import quote, urlencode

import anyio
import mcp.types as types
from anyio.abc import TaskGroup
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server

from my_claude_code.config.atomic_json import write_json_document_atomically_if_changed
from my_claude_code.config.mcp_media import (
    MCC_MCP_INLINE_IMAGES_ENV,
    MCC_MEDIA_OUTPUT_DIR_ENV,
    MCP_MEDIA_INLINE_IMAGES_HEADER,
    MCP_MEDIA_SERVER_NAME,
    TOOL_CREATE_IMAGE_VARIATION,
    TOOL_CREATE_VIDEO,
    TOOL_DELETE_VIDEO,
    TOOL_EDIT_IMAGE,
    TOOL_EDIT_VIDEO,
    TOOL_GENERATE_IMAGE,
    TOOL_GET_VIDEO,
    TOOL_LIST_VIDEOS,
    TOOL_LIST_VOICES,
    TOOL_MEDIA_STATUS,
    TOOL_OCR_DOCUMENT,
    TOOL_SPEAK,
    TOOL_TRANSCRIBE,
    TOOL_TRANSLATE_AUDIO,
)
from my_claude_code.config.paths import (
    mcp_media_catalogue_cache_path,
    mcp_media_output_dir,
)

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
    default_name,
    describe_folder,
    extension_for,
    named,
    resolve_target,
    save_new_file,
    sniff_image_type,
)
from .tools import (
    AFTER_ARGUMENT,
    LIMIT_ARGUMENT,
    MODEL_ARGUMENT,
    OPERATION_ARGUMENT,
    ORDER_ARGUMENT,
    POLL_INTERVAL_SECONDS_ARGUMENT,
    PROVIDER_ARGUMENT,
    REQUIRED_FILE_ARGUMENTS,
    SAVE_IMAGES_ARGUMENT,
    SAVE_TO_ARGUMENT,
    VARIANT_ARGUMENT,
    VIDEO_ID_ARGUMENT,
    WAIT_SECONDS_ARGUMENT,
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
    #: Whether inline ImageContent blocks are attached (MCC_MCP_INLINE_IMAGES).
    inline_images: bool = True
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


_MCP_LOCAL_ARGUMENTS: frozenset[str] = frozenset(
    {
        SAVE_TO_ARGUMENT,
        SAVE_IMAGES_ARGUMENT,
        WAIT_SECONDS_ARGUMENT,
        POLL_INTERVAL_SECONDS_ARGUMENT,
    }
)
_MAX_INLINE_IMAGES = 2
_MAX_WAIT_SECONDS = 45
_DEFAULT_POLL_INTERVAL_SECONDS = 5
_MAX_POLL_INTERVAL_SECONDS = 15


def _text(
    text: str,
    *,
    error: bool = False,
    structured_content: dict[str, Any] | None = None,
) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structured_content=structured_content,
        is_error=error,
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


def _catalogue_param_hint(
    state: ServerState, operation: str, model: str | None, param: str
) -> str:
    if not operation or state.catalogue is None or not param:
        return ""
    notes: list[str] = []
    for entry in state.catalogue.get("data") or ():
        if not isinstance(entry, Mapping):
            continue
        if model and str(entry.get("id") or "") != model:
            continue
        op_info = (entry.get("operations") or {}).get(operation)
        if not isinstance(op_info, Mapping):
            continue
        caps = op_info.get("capabilities")
        if not isinstance(caps, Mapping):
            continue
        refused = caps.get("refused_params")
        if isinstance(refused, list) and param in refused:
            notes.append(f"{param} is refused by {entry.get('id')}")
        cap_key_map = {
            "size": "accepted_sizes",
            "aspect_ratio": "accepted_aspect_ratios",
            "resolution": "accepted_resolutions",
            "seconds": "accepted_durations",
            "response_format": "accepted_formats",
            "output_format": "accepted_formats",
            "voice": "accepted_voices",
        }
        cap_key = cap_key_map.get(param)
        if cap_key and isinstance(caps.get(cap_key), list) and caps[cap_key]:
            vals = ", ".join(str(v) for v in caps[cap_key][:10])
            notes.append(f"{entry.get('id')} accepts {param}: {vals}")
    return f" ({'; '.join(notes)})" if notes else ""


def _refused(
    state: ServerState,
    tool: MediaTool,
    response: MccResponse,
    *,
    model: str | None = None,
) -> ToolFailure:
    if response.status in (401, 403):
        return ToolFailure(_token_refused(state.address, response.status))
    msg = f"MCC answered {response.status} for {tool.name}: {error_message(response)}"
    if response.status in (400, 422):
        payload = _json_object(response)
        err = payload.get("error") if isinstance(payload, Mapping) else None
        param = str(err.get("param") or "").strip() if isinstance(err, Mapping) else ""
        if param and param not in msg:
            msg += f" (parameter: {param})"
        if tool.operation:
            msg += _catalogue_param_hint(state, tool.operation, model, param)
            target = f"operation='{tool.operation}'"
            if model:
                target += f" and model='{model}'"
            msg += (
                f" Hint: call media_status with {target} to see accepted "
                "parameters and learned constraints."
            )
    return ToolFailure(msg)


def _json_object(response: MccResponse) -> Mapping[str, Any] | None:
    try:
        payload = response.json()
    except ValueError, UnicodeDecodeError:
        return None
    return payload if isinstance(payload, Mapping) else None


def _split_arguments(
    tool: MediaTool, arguments: Mapping[str, Any]
) -> tuple[dict[str, Any], str | None]:
    """The door fields to send, and ``save_to``; local/file arguments stay out."""

    body: dict[str, Any] = {}
    save_to: str | None = None
    for name, value in arguments.items():
        if name == SAVE_TO_ARGUMENT:
            save_to = value if isinstance(value, str) else None
            continue
        if name in _MCP_LOCAL_ARGUMENTS or name in tool.file_arguments or value is None:
            continue
        body[name] = value
    return body, save_to


def _required_file_message(tool: MediaTool, required_arg: str) -> str:
    if required_arg == "image_paths":
        return f"{tool.name} needs image_paths: the picture(s) to edit."
    if required_arg == "image_path":
        return f"{tool.name} needs image_path: the picture to create variations of."
    if required_arg == "audio_path":
        return f"{tool.name} needs audio_path: the audio file to read."
    return f"{tool.name} needs {required_arg}."


def _multipart(
    state: ServerState,
    tool: MediaTool,
    arguments: Mapping[str, Any],
    *,
    require_files: bool = True,
) -> tuple[dict[str, str], list[FilePart], list[str]]:
    """Form fields, uploads, and the local files read (named in the result, R10)."""

    body, _save_to = _split_arguments(tool, arguments)
    data = {name: _field_value(value) for name, value in body.items()}
    files: list[FilePart] = []
    read: list[str] = []
    provided: set[str] = set()
    for argument, spec in tool.file_arguments.items():
        raw = arguments.get(argument)
        if raw is None or raw == []:
            continue
        values = raw if isinstance(raw, list) else [raw]
        paths = [_local_file(state, value, argument) for value in values]
        if paths:
            provided.add(argument)
        door_field = (
            f"{spec.door_field}[]" if spec.many and len(paths) > 1 else spec.door_field
        )
        for path in paths:
            files.append(_upload(path, door_field))
            read.append(str(path))
    if require_files:
        required_arg = REQUIRED_FILE_ARGUMENTS.get(tool.operation)
        if (
            required_arg
            and required_arg in tool.file_arguments
            and required_arg not in provided
        ):
            raise ToolFailure(_required_file_message(tool, required_arg))
    return data, files, read


async def _images(
    state: ServerState, tool: MediaTool, arguments: Mapping[str, Any]
) -> Outcome:
    # MCC hands every picture over as bytes; one the provider answered with
    # only a link to is downloaded by MCC through the provider's own client
    # (risk R8): this program never fetches a provider's address.
    headers = {MCP_MEDIA_INLINE_IMAGES_HEADER: "1"}
    body, save_to = _split_arguments(tool, arguments)
    model_arg = str(body.get(MODEL_ARGUMENT)) if body.get(MODEL_ARGUMENT) else None
    read: list[str] = []
    if tool.file_arguments:
        data, files, read = _multipart(state, tool, arguments)
        if not files:
            req = REQUIRED_FILE_ARGUMENTS.get(tool.operation, "image_paths")
            raise ToolFailure(_required_file_message(tool, req))
        response = await state.client.request(
            "POST", tool.endpoint, data=data, files=files, headers=headers
        )
    else:
        response = await state.client.request(
            "POST", tool.endpoint, json_body=body, headers=headers
        )
    if response.status != 200:
        raise _refused(state, tool, response, model=model_arg)
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
    if not state.inline_images:
        inline_pictures: list[types.ImageContent] = []
        notes.append("Inline image previews disabled (MCC_MCP_INLINE_IMAGES=0).")
    elif len(pictures) > _MAX_INLINE_IMAGES:
        inline_pictures = pictures[:_MAX_INLINE_IMAGES]
        notes.append(
            f"Showing first {_MAX_INLINE_IMAGES} of {len(saved)} images inline "
            f"to preserve context; all {len(saved)} saved on disk."
        )
    else:
        inline_pictures = pictures
    lines = [f"Saved {len(saved)} picture(s):", *saved]
    if read:
        lines.append("Read: " + ", ".join(read))
    lines.extend(notes)
    structured: dict[str, Any] = {
        "operation": tool.operation,
        "paths": saved,
        "saved_count": len(saved),
    }
    if model_arg:
        structured["model"] = model_arg
    return Outcome(
        result=types.CallToolResult(
            content=[
                types.TextContent(type="text", text="\n".join(lines)),
                *inline_pictures,
            ],
            structured_content=structured,
            is_error=False,
        ),
        summary=f"{len(saved)} picture(s)",
        paths=tuple(saved),
    )


async def _speech(
    state: ServerState, tool: MediaTool, arguments: Mapping[str, Any]
) -> Outcome:
    body, save_to = _split_arguments(tool, arguments)
    model_arg = str(body.get(MODEL_ARGUMENT)) if body.get(MODEL_ARGUMENT) else None
    response = await state.client.request("POST", tool.endpoint, json_body=body)
    if response.status != 200:
        raise _refused(state, tool, response, model=model_arg)
    folder, asked = resolve_target(save_to, default_dir=state.out_dir, cwd=state.cwd)
    path = save_new_file(
        response.content,
        folder,
        named(asked, extension_for(response.content_type), "speech", 0),
    )
    structured: dict[str, Any] = {
        "operation": tool.operation,
        "paths": [str(path)],
        "saved_count": 1,
    }
    if model_arg:
        structured["model"] = model_arg
    # Decision 6: audio is a file the user plays, never put in the conversation.
    return Outcome(
        _text(f"Saved the speech: {path}", structured_content=structured),
        "speech",
        (str(path),),
    )


async def _list_voices(
    state: ServerState, tool: MediaTool, arguments: Mapping[str, Any]
) -> Outcome:
    params: dict[str, str] = {}
    for key in (PROVIDER_ARGUMENT, MODEL_ARGUMENT):
        raw = arguments.get(key)
        if isinstance(raw, str) and raw.strip():
            params[key] = raw.strip()
    path = f"{tool.endpoint}?{urlencode(params)}" if params else tool.endpoint
    response = await state.client.request("GET", path)
    if response.status != 200:
        raise _refused(state, tool, response)
    payload = _json_object(response)
    if payload is None:
        raise ToolFailure("MCC's voices answer was not a JSON object.")
    voices = payload.get("voices")
    entries = voices if isinstance(voices, list) else []
    if not entries:
        return Outcome(
            _text(
                "No voices configured or reported.", structured_content=dict(payload)
            ),
            "0 voices",
        )
    lines = [f"Available voices ({len(entries)}):"]
    for item in entries:
        if not isinstance(item, Mapping):
            continue
        voice_id = item.get("id") or item.get("voice") or item.get("name") or ""
        model_ref = item.get("model_ref") or item.get("provider") or ""
        source = item.get("source") or ""
        suffix = f" [{model_ref}]" if model_ref else ""
        src_note = f" ({source})" if source else ""
        lines.append(f"- {voice_id}{suffix}{src_note}")
    return Outcome(
        _text("\n".join(lines), structured_content=dict(payload)),
        f"{len(entries)} voice(s)",
    )


async def _transcript(
    state: ServerState, tool: MediaTool, arguments: Mapping[str, Any]
) -> Outcome:
    data, files, read = _multipart(state, tool, arguments)
    if not files:
        raise ToolFailure(f"{tool.name} needs audio_path: the audio file to read.")
    model_arg = data.get(MODEL_ARGUMENT) or None
    response = await state.client.request("POST", tool.endpoint, data=data, files=files)
    if response.status != 200:
        raise _refused(state, tool, response, model=model_arg)
    text = response.content.decode("utf-8", errors="replace")
    if "json" in response.content_type:
        payload = _json_object(response)
        if payload is not None and set(payload) <= {"text", "usage", "logprobs"}:
            # The plain answer: just its text. A verbose one (segments,
            # words) is returned whole, as JSON.
            text = str(payload.get("text") or "")
    structured: dict[str, Any] = {"operation": tool.operation, "text": text}
    if model_arg:
        structured["model"] = model_arg
    return Outcome(_text(text, structured_content=structured), f"text from {read[0]}")


def _parse_wait_seconds(arguments: Mapping[str, Any]) -> int:
    raw = arguments.get(WAIT_SECONDS_ARGUMENT)
    if raw is None:
        return 0
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ToolFailure(
            f"{WAIT_SECONDS_ARGUMENT} must be an integer between 0 and {_MAX_WAIT_SECONDS}."
        )
    return max(0, min(_MAX_WAIT_SECONDS, raw))


def _parse_poll_interval(arguments: Mapping[str, Any]) -> int:
    raw = arguments.get(POLL_INTERVAL_SECONDS_ARGUMENT)
    if raw is None:
        return _DEFAULT_POLL_INTERVAL_SECONDS
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ToolFailure(
            f"{POLL_INTERVAL_SECONDS_ARGUMENT} must be an integer between 1 and {_MAX_POLL_INTERVAL_SECONDS}."
        )
    return max(1, min(_MAX_POLL_INTERVAL_SECONDS, raw))


def _jobs_base(state: ServerState, tool: MediaTool) -> str:
    if tool.jobs_endpoint:
        return tool.jobs_endpoint
    get_tool = state.tool(TOOL_GET_VIDEO)
    if get_tool is not None and get_tool.endpoint:
        return get_tool.endpoint
    return tool.endpoint.removesuffix("/edits")


async def _download_video(
    state: ServerState,
    tool: MediaTool,
    job_path: str,
    job_id: str,
    *,
    save_to: str | None,
    variant: object,
) -> Outcome:
    content_path = f"{job_path}/content"
    if isinstance(variant, str) and variant.strip():
        content_path = f"{content_path}?{urlencode({'variant': variant.strip()})}"
    content = await state.client.request("GET", content_path)
    if content.status != 200:
        raise _refused(state, tool, content)
    folder, asked = resolve_target(save_to, default_dir=state.out_dir, cwd=state.cwd)
    path = save_new_file(
        content.content,
        folder,
        named(asked, extension_for(content.content_type or "video/mp4"), "video", 0),
    )
    return Outcome(
        _text(
            f"Saved the video: {path}",
            structured_content={
                "job_id": job_id,
                "status": "completed",
                "paths": [str(path)],
                "saved_count": 1,
            },
        ),
        f"video {job_id}",
        (str(path),),
    )


async def _poll_video_job(
    state: ServerState,
    tool: MediaTool,
    job_id: str,
    *,
    wait_seconds: int,
    poll_interval: int,
    save_to: str | None,
    variant: object = None,
    initial_sleep: bool = False,
) -> Outcome:
    job_path = f"{_jobs_base(state, tool)}/{quote(job_id, safe='')}"
    deadline = anyio.current_time() + wait_seconds
    if initial_sleep and wait_seconds > 0:
        await anyio.sleep(min(poll_interval, wait_seconds))
    while True:
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
            raise ToolFailure(
                f"Video job {job_id} failed: {message or 'no reason given'}"
            )
        if status == "completed":
            return await _download_video(
                state, tool, job_path, job_id, save_to=save_to, variant=variant
            )
        remaining = deadline - anyio.current_time()
        progress = payload.get("progress")
        shown = f", {progress}% done" if isinstance(progress, int | float) else ""
        if wait_seconds <= 0 or remaining <= 0:
            structured: dict[str, Any] = {
                "job_id": job_id,
                "status": status,
                "poll_after_seconds": poll_interval,
                "next_tool": TOOL_GET_VIDEO,
            }
            if isinstance(progress, int | float):
                structured["progress"] = progress
            suffix = f" after waiting {wait_seconds}s" if wait_seconds > 0 else ""
            return Outcome(
                _text(
                    f"Video job {job_id} is {status}{shown}{suffix}. "
                    f"Call get_video with video_id={job_id} again later.",
                    structured_content=structured,
                ),
                f"video {job_id} {status}",
            )
        await anyio.sleep(min(poll_interval, remaining))


async def _create_video(
    state: ServerState, tool: MediaTool, arguments: Mapping[str, Any]
) -> Outcome:
    wait_seconds = _parse_wait_seconds(arguments)
    poll_interval = _parse_poll_interval(arguments)
    raw_save_to = arguments.get(SAVE_TO_ARGUMENT)
    save_to = raw_save_to if isinstance(raw_save_to, str) else None
    # Multipart, as the OpenAI SDK always sends a create: the form every
    # declared video host was proven on.
    data, files, read = _multipart(state, tool, arguments)
    model_arg = data.get(MODEL_ARGUMENT) or None
    response = await state.client.request(
        "POST", tool.endpoint, data=data, files=files, multipart=True
    )
    if response.status not in (200, 201, 202):
        raise _refused(state, tool, response, model=model_arg)
    payload = _json_object(response) or {}
    job_id = payload.get("id")
    if not isinstance(job_id, str) or not job_id:
        raise ToolFailure("MCC accepted the video but named no job id.")
    status = str(payload.get("status") or "unknown")
    state.log.video_jobs[job_id] = tool.rail
    if status == "completed":
        job_path = f"{_jobs_base(state, tool)}/{quote(job_id, safe='')}"
        return await _download_video(
            state, tool, job_path, job_id, save_to=save_to, variant=None
        )
    if wait_seconds > 0:
        return await _poll_video_job(
            state,
            tool,
            job_id,
            wait_seconds=wait_seconds,
            poll_interval=poll_interval,
            save_to=save_to,
            initial_sleep=True,
        )
    lines = [
        f"Video job started: {job_id} (status: {status}).",
        f"Call get_video with video_id={job_id} to see its progress; it saves "
        "the video when the job is done.",
    ]
    if read:
        lines.append("Read: " + ", ".join(read))
    return Outcome(
        _text(
            "\n".join(lines),
            structured_content={
                "job_id": job_id,
                "status": status,
                "next_tool": TOOL_GET_VIDEO,
            },
        ),
        f"video job {job_id} started",
    )


async def _get_video(
    state: ServerState, tool: MediaTool, arguments: Mapping[str, Any]
) -> Outcome:
    job_id = arguments.get(VIDEO_ID_ARGUMENT)
    if not isinstance(job_id, str) or not job_id.strip():
        raise ToolFailure("get_video needs the video_id create_video returned.")
    wait_seconds = _parse_wait_seconds(arguments)
    poll_interval = _parse_poll_interval(arguments)
    raw_save_to = arguments.get(SAVE_TO_ARGUMENT)
    save_to = raw_save_to if isinstance(raw_save_to, str) else None
    variant = arguments.get(VARIANT_ARGUMENT)
    return await _poll_video_job(
        state,
        tool,
        job_id.strip(),
        wait_seconds=wait_seconds,
        poll_interval=poll_interval,
        save_to=save_to,
        variant=variant,
        initial_sleep=False,
    )


async def _list_videos(
    state: ServerState, tool: MediaTool, arguments: Mapping[str, Any]
) -> Outcome:
    params: dict[str, str] = {}
    limit = arguments.get(LIMIT_ARGUMENT)
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ToolFailure("limit must be a positive integer.")
        params[LIMIT_ARGUMENT] = str(limit)
    after = arguments.get(AFTER_ARGUMENT)
    if isinstance(after, str) and after.strip():
        params[AFTER_ARGUMENT] = after.strip()
    order = arguments.get(ORDER_ARGUMENT)
    if isinstance(order, str) and order.strip():
        params[ORDER_ARGUMENT] = order.strip()
    base = _jobs_base(state, tool)
    path = f"{base}?{urlencode(params)}" if params else base
    response = await state.client.request("GET", path)
    if response.status != 200:
        raise _refused(state, tool, response)
    payload = _json_object(response)
    if payload is None:
        raise ToolFailure("MCC's video list answer was not a JSON object.")
    items = payload.get("data")
    jobs = items if isinstance(items, list) else []
    if not jobs:
        return Outcome(
            _text("No video jobs found.", structured_content=dict(payload)),
            "0 videos",
        )
    lines = [f"Video jobs ({len(jobs)}):"]
    for item in jobs:
        if not isinstance(item, Mapping):
            continue
        vid = item.get("id") or ""
        status = item.get("status") or "unknown"
        model_id = item.get("model") or ""
        progress = item.get("progress")
        prog = f", {progress}%" if isinstance(progress, int | float) else ""
        model_note = f" [{model_id}]" if model_id else ""
        lines.append(f"- {vid}: {status}{prog}{model_note}")
    return Outcome(
        _text("\n".join(lines), structured_content=dict(payload)),
        f"{len(jobs)} video(s)",
    )


async def _delete_video(
    state: ServerState, tool: MediaTool, arguments: Mapping[str, Any]
) -> Outcome:
    job_id = arguments.get(VIDEO_ID_ARGUMENT)
    if not isinstance(job_id, str) or not job_id.strip():
        raise ToolFailure("delete_video needs the video_id to delete.")
    job_id = job_id.strip()
    base = _jobs_base(state, tool)
    response = await state.client.request("DELETE", f"{base}/{quote(job_id, safe='')}")
    if response.status != 200:
        raise _refused(state, tool, response)
    payload = _json_object(response)
    state.log.video_jobs.pop(job_id, None)
    structured = (
        dict(payload) if payload is not None else {"id": job_id, "deleted": True}
    )
    return Outcome(
        _text(f"Deleted video job: {job_id}.", structured_content=structured),
        f"deleted {job_id}",
    )


def _decode_base64_image(raw_value: str) -> bytes:
    encoded = raw_value.strip()
    if encoded.startswith("data:") and ";base64," in encoded:
        encoded = encoded.split(";base64,", 1)[1]
    return base64.b64decode(encoded)


def _ocr_image_filename(image_entry: Mapping[str, Any], mime: str, index: int) -> str:
    ext = extension_for(mime)
    raw_id = image_entry.get("id")
    if isinstance(raw_id, str) and raw_id.strip():
        candidate = Path(raw_id.strip()).name
        if candidate and not candidate.startswith("."):
            return candidate if Path(candidate).suffix else f"{candidate}{ext}"
    return default_name("ocr-image", ext, index)


async def _ocr_document(
    state: ServerState, tool: MediaTool, arguments: Mapping[str, Any]
) -> Outcome:
    body, save_to = _split_arguments(tool, arguments)
    model_arg = str(body.get(MODEL_ARGUMENT)) if body.get(MODEL_ARGUMENT) else None
    data, files, read = _multipart(state, tool, arguments, require_files=False)
    if not files:
        has_json_source = any(
            (isinstance(body.get(key), str) and str(body[key]).strip())
            or (
                key == "document"
                and isinstance(body.get(key), Mapping)
                and bool(body[key])
            )
            for key in ("document", "document_url", "image_url")
        )
        if has_json_source:
            response = await state.client.request("POST", tool.endpoint, json_body=body)
        else:
            raise ToolFailure(
                f"{tool.name} needs file_path (or document_url / image_url / document)."
            )
    else:
        response = await state.client.request(
            "POST", tool.endpoint, data=data, files=files
        )
    if response.status != 200:
        raise _refused(state, tool, response, model=model_arg)
    payload = _json_object(response)
    text = ""
    if payload is not None:
        raw_text = payload.get("text")
        if isinstance(raw_text, str) and raw_text:
            text = raw_text
        else:
            pages = payload.get("pages")
            if isinstance(pages, list):
                text = "\n\n".join(
                    str(page.get("markdown") or "")
                    for page in pages
                    if isinstance(page, Mapping) and page.get("markdown")
                )
    if not text:
        text = response.content.decode("utf-8", errors="replace")

    saved_paths: list[str] = []
    folder, asked = resolve_target(save_to, default_dir=state.out_dir, cwd=state.cwd)
    if save_to and save_to.strip():
        md_path = save_new_file(
            text.encode("utf-8"),
            folder,
            named(asked, ".md", "ocr", 0),
        )
        saved_paths.append(str(md_path))

    raw_save_images = arguments.get(SAVE_IMAGES_ARGUMENT)
    should_save_images = (
        bool(raw_save_images)
        if raw_save_images is not None
        else bool(
            (save_to and save_to.strip()) or arguments.get("include_image_base64")
        )
    )
    if (
        should_save_images
        and payload is not None
        and isinstance(payload.get("pages"), list)
    ):
        img_index = 0
        for page in payload["pages"]:
            if not isinstance(page, Mapping):
                continue
            images = page.get("images")
            if not isinstance(images, list):
                continue
            for item in images:
                if not isinstance(item, Mapping):
                    continue
                encoded = item.get("image_base64")
                if not isinstance(encoded, str) or not encoded.strip():
                    continue
                try:
                    raw_img = _decode_base64_image(encoded)
                except ValueError:
                    continue
                mime = sniff_image_type(raw_img)
                img_path = save_new_file(
                    raw_img,
                    folder,
                    _ocr_image_filename(item, mime, img_index),
                )
                saved_paths.append(str(img_path))
                img_index += 1

    summary = f"ocr from {read[0]}" if read else "ocr"
    structured: dict[str, Any] = {
        "operation": tool.operation,
        "text": text,
        "paths": saved_paths,
        "saved_count": len(saved_paths),
    }
    if model_arg:
        structured["model"] = model_arg
    if not saved_paths:
        return Outcome(_text(text, structured_content=structured), summary)
    output_text = "Saved OCR output:\n" + "\n".join(saved_paths) + "\n\n" + text
    return Outcome(
        _text(output_text, structured_content=structured),
        summary,
        tuple(saved_paths),
    )


Handler = Callable[[ServerState, MediaTool, Mapping[str, Any]], Awaitable[Outcome]]

_HANDLERS: Mapping[str, Handler] = {
    TOOL_GENERATE_IMAGE: _images,
    TOOL_EDIT_IMAGE: _images,
    TOOL_CREATE_IMAGE_VARIATION: _images,
    TOOL_SPEAK: _speech,
    TOOL_LIST_VOICES: _list_voices,
    TOOL_TRANSCRIBE: _transcript,
    TOOL_TRANSLATE_AUDIO: _transcript,
    TOOL_CREATE_VIDEO: _create_video,
    TOOL_EDIT_VIDEO: _create_video,
    TOOL_GET_VIDEO: _get_video,
    TOOL_LIST_VIDEOS: _list_videos,
    TOOL_DELETE_VIDEO: _delete_video,
    TOOL_OCR_DOCUMENT: _ocr_document,
}


def _inspect_catalogue(
    state: ServerState, model_filter: str, op_filter: str
) -> types.CallToolResult:
    """Zero-spend inspection of ``state.catalogue`` for ``model`` / ``operation``."""

    if state.catalogue is None:
        return _text(
            state.startup_problem or "No media catalogue is loaded in this session.",
            structured_content={"matched_models": [], "matched_operations": []},
        )
    lines: list[str] = []
    matched_models: list[dict[str, Any]] = []
    matched_operations: list[dict[str, Any]] = []

    for entry in state.catalogue.get("data") or ():
        if not isinstance(entry, Mapping):
            continue
        ref = str(entry.get("id") or "")
        if model_filter and not (
            ref == model_filter
            or ref.endswith(f"/{model_filter}")
            or model_filter.lower() in ref.lower()
        ):
            continue
        operations = entry.get("operations")
        if not isinstance(operations, Mapping):
            continue
        selected_ops = {
            str(op): op_info
            for op, op_info in operations.items()
            if isinstance(op_info, Mapping) and (not op_filter or str(op) == op_filter)
        }
        if not selected_ops:
            continue
        lines.append(f"Model: {ref} (provider: {entry.get('provider') or 'unknown'})")
        model_record: dict[str, Any] = {"id": ref, "operations": {}}
        for op, op_info in selected_ops.items():
            endpoint = op_info.get("endpoint") or ""
            lines.append(f"  Operation {op} ({endpoint}):")
            caps = op_info.get("capabilities")
            if isinstance(caps, Mapping) and caps:
                for key, val in caps.items():
                    lines.append(f"    {key}: {val}")
            schema = op_info.get("request_schema")
            if isinstance(schema, Mapping):
                req_fields = schema.get("required")
                if isinstance(req_fields, list) and req_fields:
                    lines.append(
                        f"    required: {', '.join(str(x) for x in req_fields)}"
                    )
                props = schema.get("properties")
                if isinstance(props, Mapping):
                    accepted: list[str] = []
                    refused: list[str] = []
                    not_forwarded: list[str] = []
                    enums: list[str] = []
                    for fname, fprop in props.items():
                        if fname == "model" or not isinstance(fprop, Mapping):
                            continue
                        xmcc = fprop.get("x-mcc")
                        state_label = (
                            str(xmcc.get("state") or "")
                            if isinstance(xmcc, Mapping)
                            else ""
                        )
                        if "refused" in state_label:
                            refused.append(str(fname))
                        elif (
                            "not_forwarded" in state_label
                            or "not forwarded" in state_label
                        ):
                            not_forwarded.append(str(fname))
                        else:
                            accepted.append(str(fname))
                        if isinstance(fprop.get("enum"), list):
                            enums.append(f"{fname}={fprop['enum']}")
                    if accepted:
                        lines.append(f"    accepted fields: {', '.join(accepted)}")
                    if refused:
                        lines.append(f"    refused fields: {', '.join(refused)}")
                    if not_forwarded:
                        lines.append(f"    not forwarded: {', '.join(not_forwarded)}")
                    if enums:
                        lines.append(f"    enums: {'; '.join(enums)}")
            model_record["operations"][op] = dict(op_info)
        matched_models.append(model_record)

    for rail in state.catalogue.get("rails") or ():
        if not isinstance(rail, Mapping):
            continue
        operations = rail.get("operations")
        if not isinstance(operations, Mapping):
            continue
        for op, info in operations.items():
            if not isinstance(info, Mapping):
                continue
            if op_filter and str(op) != op_filter:
                continue
            if not op_filter and model_filter:
                models = info.get("models")
                if not isinstance(models, list) or not any(
                    model_filter == str(m)
                    or str(m).endswith(f"/{model_filter}")
                    or model_filter.lower() in str(m).lower()
                    for m in models
                ):
                    continue
            if not info.get("models"):
                continue
            matched_operations.append(
                {"rail": rail.get("rail"), "operation": op, **info}
            )
            if not model_filter:
                lines.append(
                    f"Rail {rail.get('label') or rail.get('rail')} · {op} ({info.get('endpoint') or ''}):"
                )
                lines.append(
                    f"  models: {', '.join(str(m) for m in info.get('models') or ())}"
                )
                fields = info.get("fields")
                if isinstance(fields, Mapping):
                    for fname, fentry in fields.items():
                        if isinstance(fentry, Mapping):
                            summary_parts = [
                                f"{bucket}={fentry[bucket]}"
                                for bucket in (
                                    "required_by",
                                    "accepted_by",
                                    "refused_by",
                                    "not_forwarded_by",
                                )
                                if fentry.get(bucket)
                            ]
                            if summary_parts:
                                lines.append(
                                    f"  field {fname}: {'; '.join(summary_parts)}"
                                )

    if not lines:
        available_models = [
            str(e.get("id"))
            for e in (state.catalogue.get("data") or ())
            if isinstance(e, Mapping) and e.get("id")
        ]
        lines.append(
            f"No matching model/operation in catalogue (model={model_filter!r}, "
            f"operation={op_filter!r}). Configured models: "
            + (", ".join(available_models) if available_models else "none")
        )
    return _text(
        "\n".join(lines),
        structured_content={
            "matched_models": matched_models,
            "matched_operations": matched_operations,
        },
    )


async def media_status(
    state: ServerState, arguments: Mapping[str, Any] | None = None
) -> types.CallToolResult:
    """Free: what is configured, where MCC is, and this session's results."""

    args = arguments or {}
    raw_model = args.get(MODEL_ARGUMENT)
    raw_op = args.get(OPERATION_ARGUMENT)
    model_filter = raw_model.strip() if isinstance(raw_model, str) else ""
    op_filter = raw_op.strip() if isinstance(raw_op, str) else ""
    if model_filter or op_filter:
        return _inspect_catalogue(state, model_filter, op_filter)

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
        return await media_status(state, arguments)
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


def _inline_images_enabled(environ: Mapping[str, str]) -> bool:
    raw = (environ.get(MCC_MCP_INLINE_IMAGES_ENV) or "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


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
    env_out = (environ.get(MCC_MEDIA_OUTPUT_DIR_ENV) or "").strip()
    if out_dir:
        out = Path(os.path.expanduser(out_dir)).resolve()
    elif env_out:
        out = Path(os.path.expanduser(env_out)).resolve()
    elif here == Path(here.anchor) and not os.access(here, os.W_OK):
        out = mcp_media_output_dir()
    else:
        out = here
    return ServerState(
        app_id=app_id,
        address=address,
        client=MccClient(address, token, app_id=app_id),
        cwd=here,
        out_dir=out,
        inline_images=_inline_images_enabled(environ),
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
