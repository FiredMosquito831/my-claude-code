# Media routing

My Claude Code routes **media requests** -- image generation and editing today, with speech,
transcription and video arriving one release at a time -- over **media rails** on Model
Config, the way it routes chat over tiers. A client that speaks the OpenAI media API points
its base URL at the proxy and keeps its own code.

## What ships

| Endpoint | Since | Rail | Settings |
|---|---|---|---|
| `POST /v1/images/generations` | 7.60.0 | Image | `MODEL_IMAGE`, `MODEL_IMAGE_FALLBACKS`, `MODEL_IMAGE_PAUSED` |
| `POST /v1/images/edits` | 7.61.0 | Image | the same rail |

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8082/v1", api_key="your-proxy-token")
image = client.images.generate(model="gpt-image-2", prompt="a red cube on a white table")
edited = client.images.edit(
    model="gpt-image-2",
    image=[open("scene.png", "rb")],
    mask=open("mask.png", "rb"),
    prompt="make the cube blue",
)
```

The proxy token is the same one every other client uses (`Authorization: Bearer`,
`x-api-key` or `x-goog-api-key`).

## How a request finds its model

- The **endpoint** picks the rail: `images/generations` and `images/edits` are the Image rail.
- A `model` written as `provider/model` (for example `xai/grok-2-image`) pins that one model.
- Any other `model` -- a vendor name such as `gpt-image-2`, or none -- uses the rail:
  `MODEL_IMAGE` first, then `MODEL_IMAGE_FALLBACKS` in order, skipping anything in
  `MODEL_IMAGE_PAUSED`.
- An empty rail answers `404` with an OpenAI error naming `MODEL_IMAGE`. A media request is
  never sent to a chat model.

A media rail is **not a chat tier**: nothing on it appears in `/v1/models` or in any coding
agent's model list, and `/v1/messages` cannot reach it.

## Which providers can serve it

A provider serves a media operation only when it **declares** that endpoint; MCC never guesses
from a model name. A model on a provider that declares nothing for the operation is skipped
and **not charged** a failure -- the request log shows it as `unsupported`.

| Provider | Generation | Edits (relative to the provider's base URL) |
|---|---|---|
| Gemini | `images/generations` on its OpenAI-compatible base URL | -- |
| xAI | `images/generations` | `images/edits` |
| Together | `images/generations` | -- |
| DeepInfra | `images/generations` on its OpenAI-compatible base URL | -- |
| Agnes AI | `images/generations` | -- |
| SiliconFlow | `images/generations` | -- |
| ZenMux | `images/generations` | -- |

An edit on the Image rail therefore skips every model whose provider declares no edit endpoint,
uncharged, and is served by the first one that does.

The body the client sent is forwarded field for field, with `model` replaced by the rail's
model id, so a parameter only one host understands still reaches it. What the host answers is
what the client receives -- `b64_json` or `url`, exactly as the host returned it.

**Edits** are forwarded in the client's own encoding. A multipart upload (`image`, `image[]`,
`mask` and the text fields) goes out as multipart, streamed from the server's spooled copy of
each file -- in memory below the multipart library's own threshold, on disk above it -- and
re-read from the start if the rail falls back to the next model; nothing is ever read whole on
the server's event loop. The JSON variant (`images: [{"image_url": ...}]`) goes out as JSON.
MCC sets no upload size limit of its own; the provider's limit applies, and a refusal is an
ordinary failure.

**Streaming** (`"stream": true`, partial images as server-sent events) is routed only to a
surface documented to stream it. None of the providers above documents it yet, so a streaming
request answers `400` rather than being sent somewhere that would reject it.

## Retry, fallback, keys, 429s and proxies

The Image rail follows the same rules as a chat chain, applied by a separate copy of the chat
engine that is held to it by a contract test (`tests/contracts/test_media_chat_parity.py`):

- `FALLBACK_SKIP_KINDS` decides which failures end the route (default: a malformed request).
  Anything else moves to the next model.
- `FALLBACK_RETRY_FIRST=retry_once` retries the primary once on a transient failure.
- Key rotation, lockouts and credits benching follow `{KEY}_ROTATION`, `CREDENTIAL_LOCKOUT_TIERS`
  and the rest of Limits & Resilience.
- With `RATE_LIMIT_ROUTES_AROUND_MODEL` on, a 429 benches the (key, model) pair and the rail
  moves on; the same diagnostic probe the chat engine uses -- a 16-token *chat* question to a
  chat model you configured on that provider, never an image -- decides whether the key or the
  model was limited.
- Proxy chains, `PROXY_MAX_LIVE_FAILURES`, trigger chips and the direct fallback apply to each
  key as they do for chat.
- The deadlines (`FALLBACK_FIRST_TOKEN_TIMEOUT`, `FALLBACK_TOTAL_TIMEOUT`, ...) apply with "the
  first token" meaning "the answer arrived". They ship at `0` (off).

**Media keeps its own health records.** A key locked out, a (key, model) pair benched or a
proxy address benched by a media request is benched for media only; the chat engine never
sees it, and the other way round. A non-streaming request is answered only once a model has
produced the whole answer, so a failure before that falls back without the client noticing.

## What is recorded

Every request writes an ordinary request-log row (its endpoint, e.g. `/v1/images/generations`)
with the chain's attempts, plus:

| Column | Meaning |
|---|---|
| `media_operation` | `image_generate` or `image_edit` |
| `input_image_count` | images uploaded to an edit |
| `output_image_count` | items the host returned |
| `media_bytes_out` | decoded size of the base64 images (empty for a URL-only answer: not measured) |
| `media_sha_out` | SHA-256 of the first image |

Token usage, when the host reports it, fills the usual `tokens_in` / `tokens_out`. An edit's
uploaded files are always recorded by SHA-256, size and type (never their bytes); with
`MEDIA_STORE_ENABLED` on, each uploaded image also gets the request log's usual thumbnail
(`REQUEST_LOG_CAPTURE_IMAGES`, `REQUEST_LOG_IMAGE_MAX_PIXELS`).

### Keeping the images: `MEDIA_STORE_ENABLED`

Off by default. When on, each generated image is also written as a file named by its SHA-256
in the `media` folder beside the request log (`~/.mcc/logs/media/`). The same image is stored
once. A file is deleted when the last request row that references it is pruned or the log is
cleared; turning the setting off stops new copies and leaves existing files alone. It lives on
**Analytics -> Request log**.

## Not yet

Speech, transcription, video, Gemini-native media requests, media pricing and the
Analytics media block each arrive in their own release.
