---
name: mcc-media
description: >-
  Generate and edit images, create image variations, synthesize speech, list
  TTS voices, transcribe and translate audio, create/edit/poll/list/delete
  videos, and OCR documents or images through My Claude Code (MCC). Use when
  the user asks to generate, edit, vary, speak, voice, transcribe, translate,
  render video, or extract markdown/images from media via MCC.
license: AGPL-3.0-or-later AND MIT
compatibility: Requires a running MCC proxy and Python 3.10+ (standard library only).
metadata:
  author: my-claude-code
  version: "8.73.0"
---

# MCC Media Skill (`mcc-media`)

Invoke MCC's media rails through the self-contained stdlib CLI at
`scripts/mcc_media.py`. Never hand-write `curl` multipart boundaries or print
secrets from `~/.mcc/.env`.

## Quick Decision Table

| Task | Subcommand | Notes |
|---|---|---|
| Discover configured rails, models, fields | `models [--model ID] [--op OP]` | Zero-spend preflight |
| List available TTS voices | `voices [--provider P] [--model M]` | Zero-spend discovery |
| Generate images from text | `image-gen "prompt" [--size ...] [--n 1]` | Sends `X-MCC-Inline-Images: 1` |
| Edit or combine local images | `image-edit "prompt" --image a.png [--mask m.png]` | Repeatable `--image` |
| Create variations of a local image | `image-variation --image a.png [--n 1]` | Falls back to edit if needed |
| Synthesize speech audio | `speak "text" [--voice alloy]` | Saves `.mp3`/`.wav`/etc. |
| Transcribe local audio | `transcribe audio.mp3 [--language en]` | Returns text / JSON |
| Translate local audio to English | `translate audio.mp3` | Returns English text |
| Start a video generation job | `video-create "prompt" [--wait] [--seconds 5]` | Bounded polling with `--wait` |
| Edit or extend a video | `video-edit "prompt" --video clip.mp4 [--wait]` | Accepts `--video` or `--video-url` |
| Poll & download a video job | `video-status VIDEO_ID [--wait] [--variant ...]` | Saves video on `completed` |
| List video jobs | `video-list [--limit 10] [--order desc]` | Zero-spend status list |
| Delete a video job | `video-delete VIDEO_ID --yes` | Requires `--yes` |
| OCR a PDF or image | `ocr doc.pdf [--save-to out.md] [--save-images]` | Also accepts `--document-url` / `--image-url` |

## Required Workflow

1. **Preflight before spending (`models` / `voices`):**
   ```bash
   python3 scripts/mcc_media.py models --op image_generate
   python3 scripts/mcc_media.py models --model <model_ref> --op image_edit
   ```
   Check `required_fields`, `supported_fields`, `refused_fields`, and
   `capabilities` (`accepted_sizes`, `accepted_aspect_ratios`,
   `accepted_durations`, `supports_mask`, `max_input_images`) before issuing a
   paid call. For speech, run `voices` first when unsure of voice IDs.

2. **Execute with `scripts/mcc_media.py`:**
   - Pass `--json` on any subcommand for machine-readable output.
   - For video jobs, prefer `--wait` (default 120s window, 5s poll) or poll with
     `video-status <id> --wait`.
   - For OCR, pass `--save-to <path>` to write markdown and `--save-images`
     (with `--include-image-base64`) to save extracted page images.

3. **Output files (collision-safe):**
   - Outputs never overwrite existing files (`-1`, `-2`, ... suffixing).
   - Resolution order: `--out <dir_or_file>` -> `MCC_MEDIA_OUTPUT_DIR` ->
     current working directory.

4. **Credential & IP hygiene:**
   - Auth and address come from `MCC_CONFIG_DIR` -> `~/.mcc/.env` (legacy
     `~/.fcc/.env` fallback) via `scripts/_mcc_env.py` (R14: `.env` overrides
     inherited `os.environ` like dev-server `PORT=3000`).
   - Never print, log, or echo `ANTHROPIC_AUTH_TOKEN`.
   - Image commands send `X-MCC-Inline-Images: 1` so MCC downloads remote CDN
     results on your behalf without exposing client IP to third-party CDNs.

## References

- [Models & Capabilities](references/models-and-capabilities.md) — Reading
  `/v1/media/models` rails, `capabilities`, `request_schema`, and `/v1/audio/voices`.
- [Endpoints & Payloads](references/endpoints-and-payloads.md) — Multipart vs
  JSON rules, video job lifecycle, `variant`, and OCR export options.
- [Troubleshooting](references/troubleshooting.md) — `~/.mcc/.env` setup, R14
  precedence, HTTP 400/401/404/409/422 recovery, and MCP-vs-Skill parity.
