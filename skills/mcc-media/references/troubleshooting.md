# Troubleshooting & Parity Notes

## Configuration & R14 `.env` Precedence

`scripts/_mcc_env.py` resolves MCC settings in this order:

1. **Config directory**: `--config-dir` flag -> `MCC_CONFIG_DIR` env ->
   `~/.mcc` (if it exists) -> legacy `~/.fcc` (only if `~/.mcc` does not exist)
   -> `~/.mcc`.
2. **Override precedence (R14)**:
   - Explicit CLI flags (`--base-url`, `--token`, `--out`) win first.
   - Values in `<config-dir>/.env` (`MCC_BASE_URL`, `HOST`, `PORT`,
     `ANTHROPIC_AUTH_TOKEN`, `MCC_MEDIA_OUTPUT_DIR`) **override inherited
     process environment variables** (`PORT=3000` in a web repo never redirects
     media calls).
   - Inherited `os.environ` is used only when `.env` does not set a key.
   - Defaults: `HOST=127.0.0.1`, `PORT=8082`.

If no config `.env` exists and no `--base-url` / `MCC_BASE_URL` is set, the CLI
exits with code `2` and instructs you to run `mcc-init` and `mcc-server`.

## Common HTTP Errors & Recovery

- **Exit 2 / Connection Refused**: Start MCC with `mcc-server` (or launch the
  desktop app), verify `HOST`/`PORT` in `~/.mcc/.env`, or pass `--base-url`.
- **401 / 403 Unauthorized**: `ANTHROPIC_AUTH_TOKEN` in `~/.mcc/.env` does not
  match the running MCC server. Pass `--token` or align `~/.mcc/.env`.
- **404 `MediaRailNotConfigured`**: The requested rail (`MODEL_IMAGE`,
  `MODEL_VIDEO`, `MODEL_TTS`, `MODEL_ASR`, `MODEL_OCR`) has no model configured
  in MCC Model Config.
- **400 / 422 Parameter Refused**: Run `models --model <id> --op <op>` to check
  `required_fields`, `refused_fields`, and `capabilities` (`accepted_sizes`,
  `accepted_aspect_ratios`, `accepted_durations`).
- **409 Conflict (Video job key missing)**: A video job can only be polled or
  deleted with the provider key that created it.

## MCP (`mcc-mcp`) vs Skill (`mcc-media`) Parity

- Both surfaces target the exact same MCC media doors (`/v1/media/models`,
  `/v1/images/*`, `/v1/audio/*`, `/v1/videos*`, `/v1/ocr`).
- Prefer `mcc-mcp` when an agent already attaches it; use the `mcc-media` skill
  for harnesses without MCP (`pi`, `openclaw`, `hermes`, etc.) or when keeping
  MCP tool schemas out of the prompt cache (`IV.7` zero-token footprint).
