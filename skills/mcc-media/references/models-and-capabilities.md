# Models & Capabilities Discovery

Zero-spend discovery prevents wasted requests and parameter-refusal errors.

## `GET /v1/media/models` (`models` subcommand)

```bash
python3 scripts/mcc_media.py models
python3 scripts/mcc_media.py models --op image_edit
python3 scripts/mcc_media.py models --model openai/gpt-image-1 --op image_edit
python3 scripts/mcc_media.py models --json
```

### Response Structure

1. **`rails[]`**: One entry per configured media rail (`image`, `tts`, `asr`,
   `video`, `ocr`), containing:
   - `models`: Ordered primary + fallback model references.
   - `operations.<op>`:
     - `endpoint`: Target OpenAI/REST route on MCC.
     - `models`: Models serving `<op>` (including surface fallbacks such as
       `image_variation` -> `image_edit` or `video_edit` -> `video_create`).
     - `fields.<name>`: Compatibility buckets across serving models:
       - `required_by`
       - `accepted_by`
       - `refused_by`
       - `not_forwarded_by`
       - `not_known_for`

2. **`data[]`**: Per-model entries keyed by `id` (`<provider>/<model>`), each
   with `operations.<op>`:
   - `endpoint`: Routed endpoint path.
   - `request_schema`: JSON Schema for that model + operation.
   - `capabilities`: Resolved `MediaCapabilityProfile` constraints:
     - `supports_mask`, `max_input_images`
     - `accepted_sizes`, `accepted_aspect_ratios`, `accepted_resolutions`
     - `accepted_durations`, `accepted_formats`, `accepted_voices`
     - `supports_streaming`, `refused_params`, `not_forwarded`

## `GET /v1/audio/voices` (`voices` subcommand)

```bash
python3 scripts/mcc_media.py voices
python3 scripts/mcc_media.py voices --provider openai
python3 scripts/mcc_media.py voices --model gemini/gemini-2.5-flash-preview-tts
```

Returns `{ "object": "list", "voices": [...], "by_model": {...} }`, listing
configured/discovered TTS voice IDs per model and provider without billing.
