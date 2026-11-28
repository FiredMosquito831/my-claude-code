"""Zero-dependency R14 environment & MCC connection resolver for the mcc-media skill.

Reads MCC's ``.env`` file using Python's standard library only so the skill
directory remains completely standalone wherever it is installed.

R14 precedence:
1. Explicit CLI flags (``--base-url``, ``--token``, ``--out``).
2. Values defined inside ``<config_dir>/.env`` (``MCC_BASE_URL``, ``HOST``,
   ``PORT``, ``ANTHROPIC_AUTH_TOKEN``, ``MCC_MEDIA_OUTPUT_DIR``), which override
   inherited process environment variables such as a dev server's ``PORT=3000``.
3. Inherited process environment variables (``MCC_BASE_URL``, ``MCC_HOST``,
   ``HOST``, ``MCC_PORT``, ``PORT``, ``ANTHROPIC_AUTH_TOKEN``,
   ``MCC_MEDIA_OUTPUT_DIR``).
4. MCC loopback defaults (``127.0.0.1:8082``).
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8082


class MccEnvError(RuntimeError):
    """Raised when MCC server connection details cannot be resolved."""


@dataclass(frozen=True, slots=True)
class MccSkillConfig:
    """Resolved MCC endpoint and credentials (token kept separate in repr)."""

    base_url: str
    token: str
    config_dir: Path
    env_file: Path | None
    output_dir: Path
    source: str

    def __repr__(self) -> str:
        return (
            f"MccSkillConfig(base_url={self.base_url!r}, "
            f"token_set={bool(self.token)!r}, "
            f"config_dir={str(self.config_dir)!r}, "
            f"source={self.source!r})"
        )


def _strip_quotes(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        inner = value[1:-1]
        if value[0] == '"':
            return (
                inner.replace("\\n", "\n")
                .replace("\\r", "\r")
                .replace('\\"', '"')
                .replace("\\\\", "\\")
            )
        return inner
    # Strip inline comment on unquoted value.
    for idx, ch in enumerate(value):
        if ch == "#" and (idx == 0 or value[idx - 1].isspace()):
            return value[:idx].strip()
    return value


def parse_dotenv(path: Path) -> dict[str, str]:
    """Parse a ``.env`` file into a string mapping using stdlib only."""

    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise MccEnvError(f"Could not read MCC env file at {path}: {exc}") from exc

    parsed: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        clean_key = key.strip()
        if not clean_key:
            continue
        parsed[clean_key] = _strip_quotes(val)
    return parsed


def resolve_config_dir(
    *,
    config_dir: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> tuple[Path, bool]:
    """Return ``(config_dir_path, explicit)`` following MCC's directory rungs."""

    env = os.environ if environ is None else environ
    if config_dir is not None and str(config_dir).strip():
        return Path(os.path.expanduser(str(config_dir).strip())).resolve(), True

    env_override = (env.get("MCC_CONFIG_DIR") or "").strip()
    if env_override:
        return Path(os.path.expanduser(env_override)).resolve(), True

    base_home = (Path.home() if home is None else home).resolve()
    primary = base_home / ".mcc"
    legacy = base_home / ".fcc"
    if primary.is_dir():
        return primary, False
    if legacy.is_dir():
        return legacy, False
    return primary, False


def _validate_base_url(raw: str) -> str:
    cleaned = raw.strip().rstrip("/")
    parsed = urlsplit(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise MccEnvError(
            f"Invalid MCC_BASE_URL {raw!r}: must start with http:// or https:// and include a host."
        )
    return cleaned


def _normalize_host(raw: str) -> str:
    host = raw.strip()
    if not host or host in {"0.0.0.0", "::", "[::]"}:
        return DEFAULT_HOST
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _parse_port(raw: str, source: str) -> int:
    text = raw.strip()
    if not text.isdigit():
        raise MccEnvError(f"Invalid port {raw!r} from {source}: expected an integer.")
    port = int(text)
    if not (1 <= port <= 65535):
        raise MccEnvError(f"Invalid port {port} from {source}: must be 1..65535.")
    return port


def _pick(
    dotenv: Mapping[str, str],
    environ: Mapping[str, str],
    *keys: str,
) -> tuple[str | None, str]:
    """Return ``(value, source)`` with R14 precedence: ``.env`` beats ``environ``."""

    for key in keys:
        if key in dotenv and dotenv[key].strip():
            return dotenv[key].strip(), ".env"
    for key in keys:
        val = environ.get(key)
        if val is not None and val.strip():
            return val.strip(), "environment"
    return None, "default"


def resolve_mcc_config(
    *,
    config_dir: str | Path | None = None,
    base_url: str | None = None,
    token: str | None = None,
    out_dir: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    cwd: Path | None = None,
) -> MccSkillConfig:
    """Resolve MCC's URL, bearer token, and output folder per R14."""

    env = os.environ if environ is None else environ
    resolved_dir, explicit_dir = resolve_config_dir(
        config_dir=config_dir, environ=env, home=home
    )
    env_path = resolved_dir / ".env"
    has_env_file = env_path.is_file()
    dotenv = parse_dotenv(env_path) if has_env_file else {}

    resolved_base_url: str
    source: str
    if base_url is not None and base_url.strip():
        resolved_base_url = _validate_base_url(base_url)
        source = "cli"
    else:
        raw_url, url_source = _pick(dotenv, env, "MCC_BASE_URL")
        if raw_url is not None:
            resolved_base_url = _validate_base_url(raw_url)
            source = url_source
        else:
            has_mcc_host_port = any(
                (env.get(k) or "").strip() for k in ("MCC_HOST", "MCC_PORT")
            )
            if not has_env_file and not has_mcc_host_port:
                hint = (
                    f"Explicit config directory {resolved_dir} has no .env file. "
                    if explicit_dir
                    else f"MCC configuration not found at {env_path}. "
                )
                raise MccEnvError(
                    hint
                    + "Run `mcc-init` to create ~/.mcc/.env and `mcc-server` to start "
                    "MCC, or pass --base-url / set MCC_CONFIG_DIR."
                )

            raw_host, _host_src = _pick(dotenv, env, "MCC_HOST", "HOST")
            host = _normalize_host(raw_host) if raw_host else DEFAULT_HOST

            if "MCC_PORT" in dotenv and dotenv["MCC_PORT"].strip():
                port = _parse_port(dotenv["MCC_PORT"], ".env:MCC_PORT")
                port_src = ".env"
            elif "PORT" in dotenv and dotenv["PORT"].strip():
                port = _parse_port(dotenv["PORT"], ".env:PORT")
                port_src = ".env"
            elif (env.get("MCC_PORT") or "").strip():
                port = _parse_port(env["MCC_PORT"], "environment:MCC_PORT")
                port_src = "environment"
            elif has_env_file and (env.get("PORT") or "").strip():
                # Only honor generic process PORT when .env exists without PORT,
                # never from an uninitialized project shell without .env.
                port = _parse_port(env["PORT"], "environment:PORT")
                port_src = "environment"
            else:
                port = DEFAULT_PORT
                port_src = "default"
            resolved_base_url = f"http://{host}:{port}"
            source = port_src

    if token is not None and token.strip():
        resolved_token = token.strip()
    else:
        raw_token, _ = _pick(dotenv, env, "ANTHROPIC_AUTH_TOKEN")
        resolved_token = raw_token or ""

    if out_dir is not None and str(out_dir).strip():
        resolved_out = Path(os.path.expanduser(str(out_dir).strip())).resolve()
    else:
        raw_out, _ = _pick(dotenv, env, "MCC_MEDIA_OUTPUT_DIR")
        if raw_out:
            resolved_out = Path(os.path.expanduser(raw_out)).resolve()
        else:
            resolved_out = (cwd or Path.cwd()).resolve()

    return MccSkillConfig(
        base_url=resolved_base_url,
        token=resolved_token,
        config_dir=resolved_dir,
        env_file=env_path if has_env_file else None,
        output_dir=resolved_out,
        source=source,
    )
