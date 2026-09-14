"""What a surface probe asks a Messages endpoint.

Its own module so the constants are shared with
:mod:`my_claude_code.providers.openai_chat.responses_transport`'s pair by
value rather than by import: a provider package importing a sibling
provider package to borrow two integers is the kind of edge that makes an
import graph hard to reason about, and these are a *question*, not a
protocol detail either side owns.
"""

#: What a surface probe asks for. Small enough that a host which answers it has
#: cost the user a rounding error, and large enough that a model with a
#: minimum-output rule still accepts it.
MESSAGES_PROBE_MAX_OUTPUT_TOKENS = 16

#: The probe's prompt. One word, no system prompt, no tools: the question is
#: "does this endpoint serve this model at all", and anything more would be
#: asking a second question at the same time.
MESSAGES_PROBE_PROMPT = "hi"
