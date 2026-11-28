"""Zero-dependency CLI client for MCC's media rails (``mcc-media`` skill).

Uses Python's standard library only so the skill folder runs standalone in any
coding-agent environment without installing extra packages.
"""

import argparse
import base64
import importlib.util
import json
import mimetypes
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _load_env_module() -> Any:
    env_path = Path(__file__).resolve().parent / "_mcc_env.py"
    spec = importlib.util.spec_from_file_location("_mcc_env", env_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load _mcc_env from {env_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["_mcc_env"] = module
    spec.loader.exec_module(module)
    return module


_MCC_ENV = _load_env_module()
MccEnvError = _MCC_ENV.MccEnvError
MccSkillConfig = _MCC_ENV.MccSkillConfig
resolve_mcc_config = _MCC_ENV.resolve_mcc_config

SKILL_VERSION = "8.73.0"
DEFAULT_APP_ID = "mcc_media_skill"
INLINE_IMAGES_HEADER = "X-MCC-Inline-Images"
HARNESS_HEADER = "X-MCC-Harness"

_EXTENSIONS: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/pcm": ".pcm",
    "audio/l16": ".pcm",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
}

_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


class MccCliError(RuntimeError):
    """Actionable failure during media execution."""

    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True, slots=True)
class HttpResult:
    status: int
    content_type: str
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


def sniff_image_type(data: bytes) -> str:
    for sig, mime in _IMAGE_SIGNATURES:
        if data.startswith(sig):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def extension_for(content_type: str, fallback: str = ".bin") -> str:
    base = content_type.split(";", 1)[0].strip().lower()
    if base in _EXTENSIONS:
        return _EXTENSIONS[base]
    guessed = mimetypes.guess_extension(base) if base else None
    return guessed or fallback


def _numbered(name: str, number: int) -> str:
    stem, dot, suffix = name.rpartition(".")
    if not dot:
        return f"{name}-{number}"
    return f"{stem}-{number}.{suffix}"


def resolve_target(
    save_to: str | None, *, default_dir: Path, cwd: Path
) -> tuple[Path, str | None]:
    if not save_to or not save_to.strip():
        return default_dir, None
    text = save_to.strip()
    path = Path(text)
    if not path.is_absolute():
        path = cwd / path
    if path.is_dir() or text.endswith(("/", "\\")) or not path.suffix:
        return path, None
    return path.parent, path.name


def default_name(kind: str, extension: str, index: int = 0) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    suffix = f"-{index + 1}" if index else ""
    return f"mcc-{kind}-{stamp}{suffix}{extension}"


def named(asked: str | None, extension: str, kind: str, index: int) -> str:
    if asked is None:
        return default_name(kind, extension, index)
    if index == 0:
        return asked
    return _numbered(asked, index)


def save_new_file(data: bytes, folder: Path, name: str) -> Path:
    """Create a collision-safe new file exclusively without overwriting."""

    folder.mkdir(parents=True, exist_ok=True)
    number = 0
    while True:
        candidate = folder / (name if number == 0 else _numbered(name, number))
        try:
            with open(candidate, "xb") as handle:
                handle.write(data)
        except FileExistsError:
            number += 1
            continue
        return candidate.resolve()


def _encode_multipart(
    fields: Mapping[str, str],
    files: Sequence[tuple[str, Path]],
) -> tuple[bytes, str]:
    boundary = f"----mccmedia{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode()
        )
    for field_name, path in files:
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        data = path.read_bytes()
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{field_name}"; '
                f'filename="{path.name}"\r\n'
                f"Content-Type: {mime}\r\n\r\n"
            ).encode()
            + data
            + b"\r\n"
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _extract_error(body: bytes, status: int) -> str:
    try:
        payload = json.loads(body.decode("utf-8"))
    except ValueError, UnicodeDecodeError:
        payload = None
    if isinstance(payload, Mapping):
        err = payload.get("error")
        if isinstance(err, Mapping) and err.get("message"):
            msg = str(err["message"])
            param = err.get("param")
            return f"{msg} (param: {param})" if param else msg
        if payload.get("detail"):
            return str(payload["detail"])
    text = body.decode("utf-8", errors="replace").strip()
    return text[:500] if text else f"HTTP {status}"


class MccHttpClient:
    """Loopback HTTP client that bypasses environment HTTP(S)_PROXY settings."""

    def __init__(self, config: MccSkillConfig, *, app_id: str = DEFAULT_APP_ID) -> None:
        self.config = config
        self.app_id = app_id
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        fields: Mapping[str, str] | None = None,
        files: Sequence[tuple[str, Path]] | None = None,
        multipart: bool = False,
        extra_headers: Mapping[str, str] | None = None,
    ) -> HttpResult:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Only MCC relative paths are allowed.")
        url = f"{self.config.base_url}{path}"
        headers: dict[str, str] = {
            HARNESS_HEADER: self.app_id,
            "User-Agent": f"mcc-media-skill/{SKILL_VERSION}",
        }
        if self.config.token:
            headers["Authorization"] = f"Bearer {self.config.token}"
        if extra_headers:
            headers.update(extra_headers)

        body: bytes | None = None
        if multipart or files:
            body, content_type = _encode_multipart(fields or {}, files or ())
            headers["Content-Type"] = content_type
        elif json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=None) as resp:
                return HttpResult(
                    status=int(resp.status),
                    content_type=str(resp.headers.get("Content-Type", "")),
                    body=resp.read(),
                )
        except urllib.error.HTTPError as exc:
            err_body = exc.read()
            msg = _extract_error(err_body, exc.code)
            if exc.code in (401, 403):
                raise MccCliError(
                    f"MCC at {self.config.base_url} rejected authentication (HTTP {exc.code}): {msg}. "
                    "Check ANTHROPIC_AUTH_TOKEN in ~/.mcc/.env or pass --token.",
                    exit_code=1,
                ) from exc
            if exc.code == 404:
                raise MccCliError(
                    f"MCC answered 404 for {path}: {msg}. "
                    "Ensure the corresponding media rail (MODEL_IMAGE / MODEL_VIDEO / "
                    "MODEL_TTS / MODEL_ASR / MODEL_OCR) is configured in MCC.",
                    exit_code=1,
                ) from exc
            raise MccCliError(f"MCC answered {exc.code} for {path}: {msg}") from exc
        except urllib.error.URLError as exc:
            raise MccCliError(
                f"Cannot reach MCC at {self.config.base_url} ({exc.reason}). "
                "Start MCC with `mcc-server` or check ~/.mcc/.env.",
                exit_code=2,
            ) from exc


def _require_file(raw: str, label: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_file():
        raise MccCliError(f"File not found for {label}: {path}")
    return path.resolve()


def _emit(payload: Any, lines: Sequence[str], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        for line in lines:
            print(line)


# ----------------------------------------------------------------- subcommands


def _cmd_models(
    client: MccHttpClient, args: argparse.Namespace, *, as_json: bool
) -> None:
    res = client.request("GET", "/v1/media/models")
    payload = res.json()
    if not isinstance(payload, Mapping):
        raise MccCliError("MCC returned an unexpected /v1/media/models payload.")

    model_filter = (args.model or "").strip()
    op_filter = (args.op or "").strip()

    matched_models: list[dict[str, Any]] = []
    for entry in payload.get("data") or ():
        if not isinstance(entry, Mapping):
            continue
        ref = str(entry.get("id") or "")
        if model_filter and model_filter.lower() not in ref.lower():
            continue
        ops = entry.get("operations")
        if not isinstance(ops, Mapping):
            continue
        filtered_ops = {
            str(op): op_info
            for op, op_info in ops.items()
            if isinstance(op_info, Mapping) and (not op_filter or str(op) == op_filter)
        }
        if filtered_ops:
            matched_models.append(
                {
                    "id": ref,
                    "provider": entry.get("provider"),
                    "operations": filtered_ops,
                }
            )

    matched_rails: list[dict[str, Any]] = []
    for rail in payload.get("rails") or ():
        if not isinstance(rail, Mapping):
            continue
        ops = rail.get("operations")
        if not isinstance(ops, Mapping):
            continue
        filtered_rail_ops = {}
        for op, info in ops.items():
            if not isinstance(info, Mapping):
                continue
            if op_filter and str(op) != op_filter:
                continue
            models = info.get("models") or []
            if model_filter and not any(
                model_filter.lower() in str(m).lower() for m in models
            ):
                continue
            filtered_rail_ops[str(op)] = dict(info)
        if filtered_rail_ops:
            matched_rails.append(
                {
                    "rail": rail.get("rail"),
                    "label": rail.get("label"),
                    "models": rail.get("models"),
                    "operations": filtered_rail_ops,
                }
            )

    out_payload = (
        {"data": matched_models, "rails": matched_rails}
        if (model_filter or op_filter)
        else dict(payload)
    )
    lines: list[str] = []
    for rail in matched_rails:
        lines.append(
            f"Rail {rail.get('label') or rail.get('rail')}: "
            + (", ".join(str(m) for m in rail.get("models") or ()) or "empty")
        )
        for op, info in (rail.get("operations") or {}).items():
            lines.append(
                f"  {op} ({info.get('endpoint', '')}): "
                + (", ".join(str(m) for m in info.get("models") or ()) or "none")
            )
    for entry in matched_models:
        lines.append(f"Model {entry['id']}:")
        for op, op_info in entry["operations"].items():
            lines.append(f"  {op} -> {op_info.get('endpoint', '')}")
            caps = op_info.get("capabilities")
            if isinstance(caps, Mapping) and caps:
                lines.append(f"    capabilities: {json.dumps(caps)}")
            schema = op_info.get("request_schema")
            if isinstance(schema, Mapping):
                req = schema.get("required") or []
                if req:
                    lines.append(f"    required: {', '.join(str(r) for r in req)}")
                props = schema.get("properties")
                if isinstance(props, Mapping):
                    lines.append(f"    fields: {', '.join(str(k) for k in props)}")
    if not lines:
        lines.append("No matching media models or operations configured.")
    _emit(out_payload, lines, as_json=as_json)


def _cmd_voices(
    client: MccHttpClient, args: argparse.Namespace, *, as_json: bool
) -> None:
    params: dict[str, str] = {}
    if args.provider:
        params["provider"] = args.provider.strip()
    if args.model:
        params["model"] = args.model.strip()
    path = "/v1/audio/voices" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    res = client.request("GET", path)
    payload = res.json()
    voices = payload.get("voices") if isinstance(payload, Mapping) else []
    entries = voices if isinstance(voices, list) else []
    lines = [f"Voices ({len(entries)}):"]
    for item in entries:
        if not isinstance(item, Mapping):
            continue
        vid = item.get("id") or item.get("voice") or item.get("name") or ""
        ref = item.get("model_ref") or item.get("provider") or ""
        src = item.get("source") or ""
        suffix = f" [{ref}]" if ref else ""
        src_note = f" ({src})" if src else ""
        lines.append(f"- {vid}{suffix}{src_note}")
    _emit(payload, lines, as_json=as_json)


def _save_image_response(
    payload: Any,
    *,
    config: MccSkillConfig,
    save_to: str | None,
    as_json: bool,
) -> None:
    if not isinstance(payload, Mapping):
        raise MccCliError("MCC returned an unexpected image response.")
    items = payload.get("data")
    if not isinstance(items, list) or not items:
        raise MccCliError("MCC returned no image data.")
    folder, asked = resolve_target(
        save_to, default_dir=config.output_dir, cwd=Path.cwd()
    )
    saved: list[str] = []
    revised_notes: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        b64 = item.get("b64_json")
        if not isinstance(b64, str) or not b64:
            continue
        raw = base64.b64decode(b64)
        mime = sniff_image_type(raw)
        path = save_new_file(
            raw, folder, named(asked, extension_for(mime, ".png"), "image", len(saved))
        )
        saved.append(str(path))
        if isinstance(item.get("revised_prompt"), str) and item["revised_prompt"]:
            revised_notes.append(f"Revised prompt: {item['revised_prompt']}")
    if not saved:
        raise MccCliError(
            "Image response contained no inline b64_json bytes (ensure X-MCC-Inline-Images is supported)."
        )
    out_payload = {"paths": saved, "saved_count": len(saved)}
    lines = [f"Saved {len(saved)} image(s):", *saved, *revised_notes]
    _emit(out_payload, lines, as_json=as_json)


def _cmd_image_gen(
    client: MccHttpClient, args: argparse.Namespace, *, as_json: bool
) -> None:
    body: dict[str, Any] = {"prompt": args.prompt}
    for attr, key in (
        ("model", "model"),
        ("size", "size"),
        ("quality", "quality"),
        ("background", "background"),
        ("output_format", "output_format"),
        ("style", "style"),
        ("moderation", "moderation"),
    ):
        val = getattr(args, attr, None)
        if val is not None:
            body[key] = val
    if args.n is not None:
        body["n"] = args.n
    res = client.request(
        "POST",
        "/v1/images/generations",
        json_body=body,
        extra_headers={INLINE_IMAGES_HEADER: "1"},
    )
    _save_image_response(
        res.json(), config=client.config, save_to=args.out, as_json=as_json
    )


def _cmd_image_edit(
    client: MccHttpClient, args: argparse.Namespace, *, as_json: bool
) -> None:
    fields: dict[str, str] = {"prompt": args.prompt}
    for attr, key in (
        ("model", "model"),
        ("size", "size"),
        ("quality", "quality"),
        ("background", "background"),
        ("input_fidelity", "input_fidelity"),
        ("output_format", "output_format"),
    ):
        val = getattr(args, attr, None)
        if val is not None:
            fields[key] = str(val)
    if args.n is not None:
        fields["n"] = str(args.n)
    images = [_require_file(p, "--image") for p in args.image]
    field_name = "image[]" if len(images) > 1 else "image"
    files: list[tuple[str, Path]] = [(field_name, p) for p in images]
    if args.mask:
        files.append(("mask", _require_file(args.mask, "--mask")))
    res = client.request(
        "POST",
        "/v1/images/edits",
        fields=fields,
        files=files,
        multipart=True,
        extra_headers={INLINE_IMAGES_HEADER: "1"},
    )
    _save_image_response(
        res.json(), config=client.config, save_to=args.out, as_json=as_json
    )


def _cmd_image_variation(
    client: MccHttpClient, args: argparse.Namespace, *, as_json: bool
) -> None:
    fields: dict[str, str] = {}
    if args.model is not None:
        fields["model"] = args.model
    if args.n is not None:
        fields["n"] = str(args.n)
    if args.size is not None:
        fields["size"] = args.size
    img = _require_file(args.image, "--image")
    res = client.request(
        "POST",
        "/v1/images/variations",
        fields=fields,
        files=[("image", img)],
        multipart=True,
        extra_headers={INLINE_IMAGES_HEADER: "1"},
    )
    _save_image_response(
        res.json(), config=client.config, save_to=args.out, as_json=as_json
    )


def _cmd_speak(
    client: MccHttpClient, args: argparse.Namespace, *, as_json: bool
) -> None:
    body: dict[str, Any] = {"input": args.text}
    for attr, key in (
        ("model", "model"),
        ("voice", "voice"),
        ("instructions", "instructions"),
        ("response_format", "response_format"),
    ):
        val = getattr(args, attr, None)
        if val is not None:
            body[key] = val
    if args.speed is not None:
        body["speed"] = args.speed
    res = client.request("POST", "/v1/audio/speech", json_body=body)
    folder, asked = resolve_target(
        args.out, default_dir=client.config.output_dir, cwd=Path.cwd()
    )
    ext = extension_for(res.content_type, ".mp3")
    saved = save_new_file(res.body, folder, named(asked, ext, "speech", 0))
    _emit(
        {"path": str(saved), "bytes": len(res.body), "content_type": res.content_type},
        [f"Saved speech: {saved}"],
        as_json=as_json,
    )


def _cmd_transcript(
    client: MccHttpClient,
    args: argparse.Namespace,
    *,
    endpoint: str,
    as_json: bool,
) -> None:
    audio_path = _require_file(args.file, "file")
    fields: dict[str, str] = {}
    for attr, key in (
        ("model", "model"),
        ("language", "language"),
        ("prompt", "prompt"),
        ("response_format", "response_format"),
    ):
        val = getattr(args, attr, None)
        if val is not None:
            fields[key] = str(val)
    if args.temperature is not None:
        fields["temperature"] = str(args.temperature)
    granularities = getattr(args, "timestamp_granularity", None)
    if granularities:
        fields["timestamp_granularities"] = json.dumps(list(granularities))

    res = client.request(
        "POST",
        endpoint,
        fields=fields,
        files=[("file", audio_path)],
        multipart=True,
    )
    text = res.body.decode("utf-8", errors="replace")
    parsed: Any = {"text": text}
    if "json" in res.content_type:
        try:
            parsed = res.json()
            if isinstance(parsed, Mapping) and "text" in parsed:
                text = str(parsed["text"])
        except ValueError:
            pass
    _emit(parsed, [text], as_json=as_json)


def _download_video(
    client: MccHttpClient,
    video_id: str,
    *,
    variant: str | None,
    save_to: str | None,
) -> Path:
    quoted = urllib.parse.quote(video_id, safe="")
    path = f"/v1/videos/{quoted}/content"
    if variant and variant.strip():
        path += "?" + urllib.parse.urlencode({"variant": variant.strip()})
    res = client.request("GET", path)
    folder, asked = resolve_target(
        save_to, default_dir=client.config.output_dir, cwd=Path.cwd()
    )
    ext = extension_for(res.content_type or "video/mp4", ".mp4")
    return save_new_file(res.body, folder, named(asked, ext, "video", 0))


def _poll_video(
    client: MccHttpClient,
    video_id: str,
    *,
    wait_seconds: int,
    poll_interval: int,
    variant: str | None,
    save_to: str | None,
    initial_sleep: bool,
    as_json: bool,
) -> None:
    quoted = urllib.parse.quote(video_id, safe="")
    job_path = f"/v1/videos/{quoted}"
    deadline = time.monotonic() + max(0, wait_seconds)
    if initial_sleep and wait_seconds > 0:
        time.sleep(min(poll_interval, wait_seconds))
    while True:
        res = client.request("GET", job_path)
        payload = res.json()
        if not isinstance(payload, Mapping):
            raise MccCliError("MCC returned an invalid video job payload.")
        status = str(payload.get("status") or "unknown")
        if status == "failed":
            err = payload.get("error")
            msg = err.get("message") if isinstance(err, Mapping) else err
            raise MccCliError(
                f"Video job {video_id} failed: {msg or 'no reason given'}"
            )
        if status == "completed":
            saved = _download_video(client, video_id, variant=variant, save_to=save_to)
            out_payload = dict(payload)
            out_payload["path"] = str(saved)
            _emit(
                out_payload,
                [f"Video job {video_id} completed.", f"Saved video: {saved}"],
                as_json=as_json,
            )
            return
        remaining = deadline - time.monotonic()
        if wait_seconds <= 0 or remaining <= 0:
            _emit(
                payload,
                [
                    f"Video job {video_id}: {status}",
                    f"Poll again with: video-status {video_id} --wait",
                ],
                as_json=as_json,
            )
            return
        time.sleep(min(poll_interval, remaining))


def _video_fields_and_files(
    args: argparse.Namespace, *, edit: bool
) -> tuple[dict[str, str], list[tuple[str, Path]]]:
    fields: dict[str, str] = {"prompt": args.prompt}
    for attr, key in (
        ("model", "model"),
        ("seconds", "seconds"),
        ("size", "size"),
        ("aspect_ratio", "aspect_ratio"),
        ("resolution", "resolution"),
        ("negative_prompt", "negative_prompt"),
    ):
        val = getattr(args, attr, None)
        if val is not None:
            fields[key] = str(val)
    if args.seed is not None:
        fields["seed"] = str(args.seed)
    files: list[tuple[str, Path]] = []
    if args.input_reference:
        files.append(
            (
                "input_reference",
                _require_file(args.input_reference, "--input-reference"),
            )
        )
    if edit:
        if args.video_url:
            fields["video_url"] = args.video_url
        if args.video:
            files.append(("video", _require_file(args.video, "--video")))
    return fields, files


def _effective_wait(args: argparse.Namespace) -> tuple[int, int]:
    wait_seconds = (
        args.wait_seconds
        if args.wait_seconds is not None
        else (120 if args.wait else 0)
    )
    poll_interval = max(1, args.poll_interval)
    return max(0, wait_seconds), poll_interval


def _cmd_video_submit(
    client: MccHttpClient,
    args: argparse.Namespace,
    *,
    edit: bool,
    as_json: bool,
) -> None:
    fields, files = _video_fields_and_files(args, edit=edit)
    endpoint = "/v1/videos/edits" if edit else "/v1/videos"
    res = client.request("POST", endpoint, fields=fields, files=files, multipart=True)
    payload = res.json()
    if not isinstance(payload, Mapping) or not payload.get("id"):
        raise MccCliError("MCC accepted the video request but returned no job id.")
    job_id = str(payload["id"])
    status = str(payload.get("status") or "unknown")
    wait_seconds, poll_interval = _effective_wait(args)
    if status == "completed":
        saved = _download_video(client, job_id, variant=None, save_to=args.out)
        out_payload = dict(payload)
        out_payload["path"] = str(saved)
        _emit(
            out_payload,
            [f"Video job {job_id} completed.", f"Saved video: {saved}"],
            as_json=as_json,
        )
        return
    if wait_seconds > 0:
        _poll_video(
            client,
            job_id,
            wait_seconds=wait_seconds,
            poll_interval=poll_interval,
            variant=None,
            save_to=args.out,
            initial_sleep=True,
            as_json=as_json,
        )
        return
    _emit(
        payload,
        [
            f"Video job started: {job_id} (status: {status})",
            f"Check progress: video-status {job_id} --wait",
        ],
        as_json=as_json,
    )


def _cmd_video_status(
    client: MccHttpClient, args: argparse.Namespace, *, as_json: bool
) -> None:
    wait_seconds, poll_interval = _effective_wait(args)
    _poll_video(
        client,
        args.video_id.strip(),
        wait_seconds=wait_seconds,
        poll_interval=poll_interval,
        variant=args.variant,
        save_to=args.out,
        initial_sleep=False,
        as_json=as_json,
    )


def _cmd_video_list(
    client: MccHttpClient, args: argparse.Namespace, *, as_json: bool
) -> None:
    params: dict[str, str] = {}
    if args.limit is not None:
        params["limit"] = str(args.limit)
    if args.after:
        params["after"] = args.after
    if args.order:
        params["order"] = args.order
    path = "/v1/videos" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    res = client.request("GET", path)
    payload = res.json()
    items = payload.get("data") if isinstance(payload, Mapping) else []
    jobs = items if isinstance(items, list) else []
    lines = [f"Video jobs ({len(jobs)}):"]
    for item in jobs:
        if not isinstance(item, Mapping):
            continue
        vid = item.get("id") or ""
        status = item.get("status") or "unknown"
        model = item.get("model") or ""
        model_note = f" [{model}]" if model else ""
        lines.append(f"- {vid}: {status}{model_note}")
    _emit(payload, lines, as_json=as_json)


def _cmd_video_delete(
    client: MccHttpClient, args: argparse.Namespace, *, as_json: bool
) -> None:
    if not args.yes:
        raise MccCliError("Refusing to delete video job without --yes.", exit_code=2)
    quoted = urllib.parse.quote(args.video_id.strip(), safe="")
    res = client.request("DELETE", f"/v1/videos/{quoted}")
    payload = res.json()
    _emit(payload, [f"Deleted video job: {args.video_id}"], as_json=as_json)


def _decode_b64_image(raw: str) -> bytes:
    encoded = raw.strip()
    if encoded.startswith("data:") and ";base64," in encoded:
        encoded = encoded.split(";base64,", 1)[1]
    return base64.b64decode(encoded)


def _ocr_image_filename(entry: Mapping[str, Any], mime: str, index: int) -> str:
    ext = extension_for(mime, ".png")
    raw_id = entry.get("id")
    if isinstance(raw_id, str) and raw_id.strip():
        candidate = Path(raw_id.strip()).name
        if candidate and not candidate.startswith("."):
            return candidate if Path(candidate).suffix else f"{candidate}{ext}"
    return default_name("ocr-image", ext, index)


def _cmd_ocr(client: MccHttpClient, args: argparse.Namespace, *, as_json: bool) -> None:
    if not args.file and not args.document_url and not args.image_url:
        raise MccCliError(
            "ocr requires a local FILE or --document-url / --image-url.",
            exit_code=2,
        )
    include_imgs = bool(args.include_image_base64 or args.save_images)
    if args.file:
        doc_path = _require_file(args.file, "file")
        fields: dict[str, str] = {}
        for attr, key in (
            ("model", "model"),
            ("prompt", "prompt"),
            ("pages", "pages"),
            ("output_format", "output_format"),
        ):
            val = getattr(args, attr, None)
            if val is not None:
                fields[key] = str(val)
        if include_imgs:
            fields["include_image_base64"] = "true"
        res = client.request(
            "POST", "/v1/ocr", fields=fields, files=[("file", doc_path)], multipart=True
        )
    else:
        body: dict[str, Any] = {}
        if args.document_url:
            body["document_url"] = args.document_url
        if args.image_url:
            body["image_url"] = args.image_url
        for attr, key in (
            ("model", "model"),
            ("prompt", "prompt"),
            ("pages", "pages"),
            ("output_format", "output_format"),
        ):
            val = getattr(args, attr, None)
            if val is not None:
                body[key] = val
        if include_imgs:
            body["include_image_base64"] = True
        res = client.request("POST", "/v1/ocr", json_body=body)

    payload = res.json()
    text = ""
    if isinstance(payload, Mapping):
        if isinstance(payload.get("text"), str) and payload["text"]:
            text = payload["text"]
        elif isinstance(payload.get("pages"), list):
            text = "\n\n".join(
                str(p.get("markdown") or "")
                for p in payload["pages"]
                if isinstance(p, Mapping) and p.get("markdown")
            )
    if not text:
        text = res.body.decode("utf-8", errors="replace")

    saved_paths: list[str] = []
    save_target = args.save_to or args.out
    folder, asked = resolve_target(
        save_target, default_dir=client.config.output_dir, cwd=Path.cwd()
    )
    if args.save_to:
        md_path = save_new_file(
            text.encode("utf-8"), folder, named(asked, ".md", "ocr", 0)
        )
        saved_paths.append(str(md_path))

    should_save_images = (
        args.save_images
        if args.save_images is not None
        else bool(args.save_to or args.include_image_base64)
    )
    if (
        should_save_images
        and isinstance(payload, Mapping)
        and isinstance(payload.get("pages"), list)
    ):
        img_index = 0
        for page in payload["pages"]:
            if not isinstance(page, Mapping):
                continue
            images = page.get("images")
            if not isinstance(images, list):
                continue
            for img_entry in images:
                if not isinstance(img_entry, Mapping):
                    continue
                b64 = img_entry.get("image_base64")
                if not isinstance(b64, str) or not b64.strip():
                    continue
                try:
                    raw_img = _decode_b64_image(b64)
                except ValueError:
                    continue
                mime = sniff_image_type(raw_img)
                img_path = save_new_file(
                    raw_img,
                    folder,
                    _ocr_image_filename(img_entry, mime, img_index),
                )
                saved_paths.append(str(img_path))
                img_index += 1

    if isinstance(payload, Mapping):
        out_payload = dict(payload)
        out_payload["text"] = text
        out_payload["paths"] = saved_paths
    else:
        out_payload = {"text": text, "paths": saved_paths}
    lines = ["Saved OCR output:", *saved_paths, "", text] if saved_paths else [text]
    _emit(out_payload, lines, as_json=as_json)


# ---------------------------------------------------------------------- parser


def _common_parent() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config-dir", default=argparse.SUPPRESS)
    common.add_argument("--base-url", default=argparse.SUPPRESS)
    common.add_argument("--token", default=argparse.SUPPRESS)
    common.add_argument("--app", default=argparse.SUPPRESS)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _common_parent()
    parser = argparse.ArgumentParser(
        prog="mcc_media.py",
        description="Invoke MCC media rails via pure-stdlib HTTP requests.",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_models = sub.add_parser(
        "models", parents=[common], help="inspect media models & rails"
    )
    p_models.add_argument("--model", default=None)
    p_models.add_argument("--op", default=None)

    p_voices = sub.add_parser("voices", parents=[common], help="list TTS voices")
    p_voices.add_argument("--provider", default=None)
    p_voices.add_argument("--model", default=None)

    p_igen = sub.add_parser("image-gen", parents=[common], help="generate image(s)")
    p_igen.add_argument("prompt")
    p_igen.add_argument("--model", default=None)
    p_igen.add_argument("--size", default=None)
    p_igen.add_argument("--quality", default=None)
    p_igen.add_argument("--background", default=None)
    p_igen.add_argument("--output-format", default=None)
    p_igen.add_argument("--style", default=None)
    p_igen.add_argument("--moderation", default=None)
    p_igen.add_argument("--n", type=int, default=None)
    p_igen.add_argument("--out", default=None)

    p_iedit = sub.add_parser("image-edit", parents=[common], help="edit image(s)")
    p_iedit.add_argument("prompt")
    p_iedit.add_argument("--image", action="append", required=True)
    p_iedit.add_argument("--mask", default=None)
    p_iedit.add_argument("--model", default=None)
    p_iedit.add_argument("--size", default=None)
    p_iedit.add_argument("--quality", default=None)
    p_iedit.add_argument("--background", default=None)
    p_iedit.add_argument("--input-fidelity", default=None)
    p_iedit.add_argument("--output-format", default=None)
    p_iedit.add_argument("--n", type=int, default=None)
    p_iedit.add_argument("--out", default=None)

    p_ivar = sub.add_parser("image-variation", parents=[common], help="image variation")
    p_ivar.add_argument("--image", required=True)
    p_ivar.add_argument("--model", default=None)
    p_ivar.add_argument("--n", type=int, default=None)
    p_ivar.add_argument("--size", default=None)
    p_ivar.add_argument("--out", default=None)

    p_speak = sub.add_parser("speak", parents=[common], help="text to speech")
    p_speak.add_argument("text")
    p_speak.add_argument("--model", default=None)
    p_speak.add_argument("--voice", default=None)
    p_speak.add_argument("--instructions", default=None)
    p_speak.add_argument("--response-format", default=None)
    p_speak.add_argument("--speed", type=float, default=None)
    p_speak.add_argument("--out", default=None)

    for cmd_name, help_text, with_lang in (
        ("transcribe", "transcribe audio file", True),
        ("translate", "translate audio file to English", False),
    ):
        p_asr = sub.add_parser(cmd_name, parents=[common], help=help_text)
        p_asr.add_argument("file")
        p_asr.add_argument("--model", default=None)
        if with_lang:
            p_asr.add_argument("--language", default=None)
            p_asr.add_argument("--timestamp-granularity", action="append", default=None)
        p_asr.add_argument("--prompt", default=None)
        p_asr.add_argument("--response-format", default=None)
        p_asr.add_argument("--temperature", type=float, default=None)

    for cmd_name, help_text, is_edit in (
        ("video-create", "start a video generation job", False),
        ("video-edit", "edit or extend a video", True),
    ):
        p_vid = sub.add_parser(cmd_name, parents=[common], help=help_text)
        p_vid.add_argument("prompt")
        if is_edit:
            p_vid.add_argument("--video", default=None)
            p_vid.add_argument("--video-url", default=None)
        p_vid.add_argument("--input-reference", default=None)
        p_vid.add_argument("--model", default=None)
        p_vid.add_argument("--seconds", default=None)
        p_vid.add_argument("--size", default=None)
        p_vid.add_argument("--aspect-ratio", default=None)
        p_vid.add_argument("--resolution", default=None)
        p_vid.add_argument("--negative-prompt", default=None)
        p_vid.add_argument("--seed", type=int, default=None)
        p_vid.add_argument("--wait", action="store_true")
        p_vid.add_argument("--wait-seconds", type=int, default=None)
        p_vid.add_argument("--poll-interval", type=int, default=5)
        p_vid.add_argument("--out", default=None)

    p_vst = sub.add_parser("video-status", parents=[common], help="poll/download video")
    p_vst.add_argument("video_id")
    p_vst.add_argument("--wait", action="store_true")
    p_vst.add_argument("--wait-seconds", type=int, default=None)
    p_vst.add_argument("--poll-interval", type=int, default=5)
    p_vst.add_argument("--variant", default=None)
    p_vst.add_argument("--out", default=None)

    p_vls = sub.add_parser("video-list", parents=[common], help="list video jobs")
    p_vls.add_argument("--limit", type=int, default=None)
    p_vls.add_argument("--after", default=None)
    p_vls.add_argument("--order", choices=["asc", "desc"], default=None)

    p_vdel = sub.add_parser("video-delete", parents=[common], help="delete a video job")
    p_vdel.add_argument("video_id")
    p_vdel.add_argument("--yes", action="store_true")

    p_ocr = sub.add_parser("ocr", parents=[common], help="OCR a document or image")
    p_ocr.add_argument("file", nargs="?", default=None)
    p_ocr.add_argument("--document-url", default=None)
    p_ocr.add_argument("--image-url", default=None)
    p_ocr.add_argument("--model", default=None)
    p_ocr.add_argument("--prompt", default=None)
    p_ocr.add_argument("--pages", default=None)
    p_ocr.add_argument("--include-image-base64", action="store_true")
    p_ocr.add_argument("--output-format", default=None)
    p_ocr.add_argument("--save-to", default=None)
    p_ocr.add_argument(
        "--save-images",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    p_ocr.add_argument("--out", default=None)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    as_json = bool(getattr(args, "json", False))
    config_dir = getattr(args, "config_dir", None)
    base_url = getattr(args, "base_url", None)
    token = getattr(args, "token", None)
    app_id = getattr(args, "app", DEFAULT_APP_ID)
    out_dir = getattr(args, "out", None)

    try:
        config = resolve_mcc_config(
            config_dir=config_dir,
            base_url=base_url,
            token=token,
            out_dir=out_dir,
        )
        client = MccHttpClient(config, app_id=app_id)
        match args.command:
            case "models":
                _cmd_models(client, args, as_json=as_json)
            case "voices":
                _cmd_voices(client, args, as_json=as_json)
            case "image-gen":
                _cmd_image_gen(client, args, as_json=as_json)
            case "image-edit":
                _cmd_image_edit(client, args, as_json=as_json)
            case "image-variation":
                _cmd_image_variation(client, args, as_json=as_json)
            case "speak":
                _cmd_speak(client, args, as_json=as_json)
            case "transcribe":
                _cmd_transcript(
                    client, args, endpoint="/v1/audio/transcriptions", as_json=as_json
                )
            case "translate":
                _cmd_transcript(
                    client, args, endpoint="/v1/audio/translations", as_json=as_json
                )
            case "video-create":
                _cmd_video_submit(client, args, edit=False, as_json=as_json)
            case "video-edit":
                _cmd_video_submit(client, args, edit=True, as_json=as_json)
            case "video-status":
                _cmd_video_status(client, args, as_json=as_json)
            case "video-list":
                _cmd_video_list(client, args, as_json=as_json)
            case "video-delete":
                _cmd_video_delete(client, args, as_json=as_json)
            case "ocr":
                _cmd_ocr(client, args, as_json=as_json)
    except MccEnvError as exc:
        print(f"mcc-media: {exc}", file=sys.stderr)
        return 2
    except MccCliError as exc:
        print(f"mcc-media: {exc}", file=sys.stderr)
        return exc.exit_code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
