"""MCC's media MCP server, ``mcc-mcp`` (7.99.0).

``tools`` derives the tool list from ``GET /v1/media/models`` (pure),
``client`` talks to MCC on loopback with MCC's own address and token,
``outputs`` saves results without ever overwriting a file, and ``server`` is
the stdio program an app starts. Only ``server`` imports the MCP SDK; the
dashboard reads ``tools`` to show what the tool list costs, and must not pay
for the SDK's import to do it.

May import ``config`` and ``core`` only.
"""
