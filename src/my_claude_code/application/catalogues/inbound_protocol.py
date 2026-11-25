"""Post-serialisation inbound-protocol transformer for multi-protocol harnesses.

Re-exports the pure transformation helpers from
``config/harness_inbound_protocol.py`` so ``application``, ``api``,
``runtime``, and ``cli`` share one single source of truth without violating
package import boundaries.
"""

from my_claude_code.config.harness_inbound_protocol import (
    apply_inbound_protocol,
    document_inbound_protocol,
    document_matches_inbound_protocol,
    inbound_protocol_base_url_shape,
)

__all__ = [
    "apply_inbound_protocol",
    "document_inbound_protocol",
    "document_matches_inbound_protocol",
    "inbound_protocol_base_url_shape",
]
