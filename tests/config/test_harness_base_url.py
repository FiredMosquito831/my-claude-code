"""Resolving the base-URL sentinel in a document MCC owns whole.

The rule that matters is the one PR 6 got wrong for a different CLI: which
shape of base URL each SDK wants. Qwen Code and Crush both reach MCC through
an official Anthropic SDK, and both of those append ``/v1/messages``
themselves, so the value written is the proxy root.
"""

from my_claude_code.config.harness_base_url import root_base_url, with_root_base_url

SENTINEL = "https://base-url.mcc.invalid/v1"


def test_the_root_is_written_without_a_v1_suffix() -> None:
    assert root_base_url("http://127.0.0.1:8199") == "http://127.0.0.1:8199"


def test_a_caller_supplied_v1_is_stripped_rather_than_doubled() -> None:
    # A trailing /v1 here would produce POST /v1/v1/messages once the SDK
    # appends its own path.
    assert root_base_url("http://127.0.0.1:8199/v1") == "http://127.0.0.1:8199"
    assert root_base_url("http://127.0.0.1:8199/v1/") == "http://127.0.0.1:8199"


def test_a_trailing_slash_is_removed() -> None:
    assert root_base_url("http://127.0.0.1:8199/") == "http://127.0.0.1:8199"


def test_the_sentinel_is_replaced_at_any_depth() -> None:
    document = {
        "providers": {"mcc": {"base_url": SENTINEL}},
        "modelProviders": {"anthropic": [{"baseUrl": SENTINEL}]},
    }

    resolved = with_root_base_url(document, SENTINEL, "http://127.0.0.1:8199")

    assert resolved["providers"]["mcc"]["base_url"] == "http://127.0.0.1:8199"
    assert (
        resolved["modelProviders"]["anthropic"][0]["baseUrl"] == "http://127.0.0.1:8199"
    )


def test_only_a_whole_value_matches() -> None:
    """A partially substituted URL is a worse failure than an untouched one."""

    document = {"note": f"write {SENTINEL} here", "url": SENTINEL}

    resolved = with_root_base_url(document, SENTINEL, "http://127.0.0.1:8199")

    assert resolved["note"] == f"write {SENTINEL} here"
    assert resolved["url"] == "http://127.0.0.1:8199"


def test_the_input_document_is_not_mutated() -> None:
    document = {"providers": {"mcc": {"base_url": SENTINEL}}}

    with_root_base_url(document, SENTINEL, "http://127.0.0.1:8199")

    assert document["providers"]["mcc"]["base_url"] == SENTINEL


def test_non_string_leaves_survive_untouched() -> None:
    document = {"n": 1, "b": True, "none": None, "list": [1, SENTINEL]}

    resolved = with_root_base_url(document, SENTINEL, "http://127.0.0.1:8199")

    assert resolved == {
        "n": 1,
        "b": True,
        "none": None,
        "list": [1, "http://127.0.0.1:8199"],
    }
