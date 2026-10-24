"""The DuckDuckGo search and page-extraction measurement harness (DDG-1).

Recorded pages only, never the network: ``corpus`` loads the fixtures under
``tests/fixtures/ddg`` and ``tests/fixtures/extraction`` (each with its
``SOURCE`` line), ``methods`` drives MCC's real search and fetch code over them
through fake transports and a loopback server, ``scoring`` turns what comes
back into numbers, ``baseline`` pins today's outputs byte for byte, and
``table`` prints the before/after table. See ``README.md`` here.
"""
