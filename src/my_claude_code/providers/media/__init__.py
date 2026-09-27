"""The media side of every provider: key pool, proxy pool and leaf.

Copies of the chat wrappers' loops (``providers/runtime/rotating.py`` and
``providers/runtime/proxy_rotating.py`` stay frozen), each with health books
of its own so a media failure never changes a chat decision.
"""
