"""Declared model ids are not secrets; everything else is scrubbed as before (7.97.0).

The "Everything known" view showed about twenty long Hugging Face model ids as
``<redacted>``: ``meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8`` is a run
of 40+ base64-alphabet characters with digits and both cases, which is exactly
the shape the scrubber's entropy rung was written to catch. ``known_ids`` lets
a caller name the ids a catalogue lists; a value EQUAL to one of them skips
that one rung. These hold both halves: a real-shaped id beside real-shaped
keys, the id kept and every key still redacted.
"""

import pytest

from my_claude_code.core.wire_capture import (
    REDACTED,
    install_credential_digests,
    redact_string_value,
    redact_wire_value,
    reset_credential_digests,
)

HF_ID = "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8"
HF_IDS = (
    HF_ID,
    "Qwen/Qwen3-Coder-480B-A35B-Instruct-Turbo",
    "nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B",
    "deepinfra/deepseek-ai/DeepSeek-V4-Flash-0731",
)
#: Key-shaped values, fake, each caught by a different rung: a bare base64 run
#: (entropy), 32 and 64 hex (entropy), a prefixed key (shape scrubber).
BASE64_KEY = "Xq7Zk2LmP9vRt4WbN8sYc3HdJ6gFa1QeU5oTiK0zM4nB"
HEX32_KEY = "9f2c4b7e1a0d3c5f8e6b2a4d7c9e1f30"
HEX64_KEY = HEX32_KEY * 2
PREFIXED_KEY = "sk-ant-api03-" + "AbCdEf0123456789" * 4


@pytest.fixture(autouse=True)
def _no_digests():
    reset_credential_digests()
    yield
    reset_credential_digests()


def test_without_known_ids_the_rule_is_what_it_always_was() -> None:
    for model_id in HF_IDS:
        assert redact_string_value(model_id) == REDACTED
        assert redact_wire_value({"id": model_id}) == {"id": REDACTED}


def test_a_known_hf_id_reads_as_itself_beside_keys_that_stay_redacted() -> None:
    row = {
        "id": HF_ID,
        "hugging_face_id": HF_ID,
        "aliases": [HF_ID, "short-alias"],
        "note": BASE64_KEY,
        "session": HEX32_KEY,
        "signature": HEX64_KEY,
        "smuggled": PREFIXED_KEY,
        "api_key": HF_ID,
    }
    scrubbed = redact_wire_value(row, known_ids=frozenset(HF_IDS))
    assert scrubbed == {
        "id": HF_ID,
        "hugging_face_id": HF_ID,
        "aliases": [HF_ID, "short-alias"],
        "note": REDACTED,
        "session": REDACTED,
        "signature": REDACTED,
        "smuggled": scrubbed["smuggled"],
        # A credential-named key is redacted whatever it holds.
        "api_key": REDACTED,
    }
    assert "AbCdEf0123456789" not in scrubbed["smuggled"]


def test_only_an_exact_known_id_is_spared() -> None:
    known = frozenset({HF_ID})
    # The same id with anything around it is not the id.
    for near in (HF_ID + "x", " " + HF_ID + "Z", HF_ID + "7", BASE64_KEY + HF_ID):
        assert redact_string_value(near, known_ids=known) == REDACTED
    # A key is a key even when a catalogue lists ids beside it.
    assert redact_string_value(BASE64_KEY, known_ids=known) == REDACTED


def test_a_known_id_that_is_also_a_configured_credential_is_still_redacted() -> None:
    install_credential_digests([HF_ID])
    assert redact_string_value(HF_ID, known_ids=frozenset({HF_ID})) == REDACTED


def test_a_known_id_still_passes_through_the_shape_scrubber() -> None:
    assert redact_string_value(PREFIXED_KEY, known_ids=frozenset({PREFIXED_KEY})) != (
        PREFIXED_KEY
    )
