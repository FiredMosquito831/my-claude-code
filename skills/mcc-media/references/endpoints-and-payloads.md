# Endpoints & Payloads Reference

All requests travel over loopback (or `MCC_BASE_URL`) with proxy bypass
(`ProxyHandler({})`) and `Authorization: Bearer <token>` when configured.

## Operation Matrix

| Operation | Method & Path | Wire Format | Key Flags / Fields |
|---|---|---|---|
| `models` | `GET /v1/media/models` | Query/JSON | `--model`, `--op` |
| `voices` | `GET /v1/audio/voices` | Query/JSON | `--provider`, `--model` |
| `image-gen` | `POST /v1/images/generations` | JSON | `prompt`, `--model`, `--size`, `--quality`, `--background`, `--output-format`, `--n`, `--style`, `--moderation` |
| `image-edit` | `POST /v1/images/edits` | `multipart/form-data` | `prompt`, `--image` (repeatable -> `image` / `image[]`), `--mask`, `--size`, `--quality`, `--background`, `--input-fidelity`, `--output-format`, `--n` |
| `image-variation` | `POST /v1/images/variations` | `multipart/form-data` | `--image` (single file), `--model`, `--n`, `--size` |
| `speak` | `POST /v1/audio/speech` | JSON -> audio bytes | `text` -> `input`, `--model`, `--voice`, `--instructions`, `--response-format`, `--speed` |
| `transcribe` | `POST /v1/audio/transcriptions` | `multipart/form-data` | `file`, `--model`, `--language`, `--prompt`, `--response-format`, `--temperature`, `--timestamp-granularity` |
| `translate` | `POST /v1/audio/translations` | `multipart/form-data` | `file`, `--model`, `--prompt`, `--response-format`, `--temperature` |
| `video-create` | `POST /v1/videos` | `multipart/form-data` | `prompt`, `--input-reference`, `--seconds`, `--size`, `--aspect-ratio`, `--resolution`, `--negative-prompt`, `--seed`, `--wait` |
| `video-edit` | `POST /v1/videos/edits` | `multipart/form-data` | `prompt`, `--video` / `--video-url`, `--input-reference`, `--seconds`, `--size`, `--aspect-ratio`, `--resolution`, `--negative-prompt`, `--seed`, `--wait` |
| `video-status` | `GET /v1/videos/{id}` & `/content` | JSON / video bytes | `video_id`, `--wait`, `--wait-seconds`, `--poll-interval`, `--variant` |
| `video-list` | `GET /v1/videos` | Query/JSON | `--limit`, `--after`, `--order` (`asc`/`desc`) |
| `video-delete` | `DELETE /v1/videos/{id}` | JSON | `video_id`, `--yes` (required) |
| `ocr` | `POST /v1/ocr` | Multipart (file) or JSON (URL) | `[file]` or `--document-url` / `--image-url`, `--pages`, `--prompt`, `--include-image-base64`, `--output-format`, `--save-to`, `--save-images` |

## IP Masking (`X-MCC-Inline-Images: 1`)

Image commands (`image-gen`, `image-edit`, `image-variation`) always send
`X-MCC-Inline-Images: 1`. When an upstream returns a CDN URL instead of
`b64_json`, MCC downloads the asset through its configured provider/proxy
chain and returns `b64_json` to the local script—preventing the client machine
from connecting directly to third-party image CDNs.

## Local Files vs URLs

- `image-edit`, `image-variation`, `transcribe`, `translate`: always upload local
  files via `multipart/form-data`.
- `video-edit`: accepts a local `--video <path>` upload or `--video-url <url>`.
- `ocr`: pass a local `FILE` (uploaded as multipart `file`) OR `--document-url`
  / `--image-url` (sent as JSON). Passing `--save-to` writes extracted markdown;
  passing `--save-images` (or `--include-image-base64`) writes `pages[].images`
  next to the output.

## Async Video Lifecycle & `variant`

1. `video-create` / `video-edit` returns a job JSON with `id` and `status`
   (`queued`, `in_progress`, `completed`, or `failed`).
2. With `--wait` (or `--wait-seconds N`), the script polls `GET /v1/videos/{id}`
   every `--poll-interval` seconds (default 5s) until `completed`, `failed`, or
   timeout.
3. On `completed`, it downloads `GET /v1/videos/{id}/content` (with optional
   `?variant=<name>`, e.g. `thumbnail` or `spritesheet`) and saves it without
   overwriting existing files.
