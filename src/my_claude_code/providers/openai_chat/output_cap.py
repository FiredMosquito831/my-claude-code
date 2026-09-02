"""This family's view of the fleet-wide output-cap matcher.

The implementation moved to
:mod:`my_claude_code.providers.recovery.output_cap` in 6.33.0, when the
Anthropic Messages family needed the same net for its own
``max_tokens: N > M, which is the maximum allowed number of output tokens``
rejection. The names re-exported here default to this dialect's body keys
(``max_completion_tokens``, ``max_tokens``), so callers in this family read
exactly as they did before.
"""

from my_claude_code.providers.recovery import (
    clamp_output_tokens,
    parse_output_token_cap,
)

__all__ = ["clamp_output_tokens", "parse_output_token_cap"]
